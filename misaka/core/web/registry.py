"""Which backend serves a search or an extract, and whether anything can serve one at all.

Ported from Hermes' ``agent/web_search_registry.py`` plus the selection half of
``tools/web_tools.py`` (``_get_backend`` / ``_get_search_backend`` /
``_get_extract_backend`` / ``_get_capability_backend`` / ``_is_backend_available`` /
``check_web_api_key``). Both halves are kept because they are not the same mechanism and
Hermes runs both: the *selection ladder* answers "which name", strictly and without
probing, so a broken stored selection surfaces the vendor's own error; the *resolution
walk* answers "which registered provider", filtered by availability, and only runs when
the ladder's name is not registered.

Hermes' registry has two dimensions MISAKA's flat process-global table does not: it is
scoped, one map per plugin home. The other one -- the capability filter -- is real here.
Search and extract are separate questions with separate config keys, and the filter runs
at *every* step of the walk, not just the configured one: a search-only backend named as
``extract_backend`` has to fall through rather than be handed a batch of URLs it has no
renderer for, and equally must not be reached by the single-eligible shortcut, the legacy
preference order or the keyless ring. Everything else -- the preference order, the
"explicit config wins even when unavailable" rule, the last-resort keyless walk -- is
carried over as it stands.

Two names in Hermes' ladders are absent from the tables below rather than merely
unimplemented. The Nous managed tool-gateway (``NOUS_MANAGED_PROVIDER``,
``_is_tool_gateway_ready``, the ``firecrawl`` gateway client) is Hermes' subscription
product, and there is nothing to point it at. ``_disabled_web_plugin_for`` -- which tells
a user "you configured this backend but you also disabled its plugin" -- has nothing to
key that diagnosis on here: no plugin identity, no enable/disable state, so the only way
to name a backend that is not registered is a typo, which the dispatcher's existing error
already says.

``xai`` is present, and -- as in Hermes -- is a selectable backend that the preference
walk never reaches on its own. Hermes keeps it out of ``_LEGACY_PREFERENCE`` because its
results are model-generated rather than index-backed, a different trust model that should
be chosen deliberately rather than inherited from an availability scan.
"""

from __future__ import annotations

import logging
import threading

from misaka.core.web.config import (
    config_name,
    has_env,
    keyless_tier_enabled,
)
from misaka.core.web.keyless import keyless_walk_order
from misaka.core.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

_providers: dict[str, WebSearchProvider] = {}
_lock = threading.Lock()
_builtins_registered = False

# The built-in backends whose availability is driven by the hardcoded env-var / package
# probes below. Any name NOT in this set is a candidate externally-registered provider
# and is resolved through its own ``is_available()`` instead. Kept as one named constant
# so the whitelist early-returns and the availability chokepoint stay in sync.
_BUILTIN_BACKENDS = frozenset(
    {
        "parallel",
        "firecrawl",
        "tavily",
        "exa",
        "searxng",
        "brave-free",
        "ddgs",
        "keenable",
        "xai",
    }
)

# Legacy preference order -- preserves behaviour for users who set no backend config key
# at all. Paid providers first so an existing paid setup is not downgraded to a free tier
# on upgrade. Filtered by ``is_available()`` at walk time so a provider the user has no
# credentials for is never surfaced.
_LEGACY_PREFERENCE = (
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)

# The shared name when nothing decided one. Two branches of the ladder end here -- a
# configured section whose shared key is blank, and a walk that found nothing -- and they
# have to agree, or the answer would depend on which one ran. Firecrawl for backward
# compatibility, as in Hermes, and it serves both capabilities, which is what a blank
# shared key needs it to do.
_DEFAULT_BACKEND = "firecrawl"


def register_provider(provider: WebSearchProvider) -> None:
    """Register a web search provider.

    Re-registration (same ``name``) overwrites the previous entry and logs a debug
    message -- makes hot-reload scenarios (tests, dev loops) behave predictably.
    """
    if not isinstance(provider, WebSearchProvider):
        raise TypeError(
            f"register_provider() expects a WebSearchProvider instance, "
            f"got {type(provider).__name__}"
        )
    raw_name = provider.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("Web provider .name must be a non-empty string")
    name = raw_name.strip()
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug(
            "Web provider '%s' re-registered (was %r)", name, type(existing).__name__
        )
    else:
        logger.debug(
            "Registered web provider '%s' (%s)", name, type(provider).__name__
        )


def list_providers() -> list[WebSearchProvider]:
    """Return all registered providers, sorted by name."""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str) -> WebSearchProvider | None:
    """Return the provider registered under *name*, or None."""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


def ensure_backends_registered() -> None:
    """Register the bundled backends once, if they have not been.

    Hermes' ``_ensure_web_plugins_loaded``. Both the registration-time availability gate
    and the first dispatch reach the registry, and either may be first; like Hermes'
    discovery call this is idempotent and cheap after the first time.

    The guard is a flag rather than "is the map empty", because an externally registered
    provider landing first would otherwise convince this that the built-ins are loaded.
    """
    global _builtins_registered
    with _lock:
        if _builtins_registered:
            return
    from misaka.core.web.backends import register_builtin_providers

    try:
        register_builtin_providers()
    except Exception as exc:  # noqa: BLE001 - a broken backend must not cost the others
        # Warning, not debug, and the flag stays down. Hermes' equivalent
        # (``tools/web_tools.py:826-836``) is non-fatal for the same reason: an import
        # that breaks halfway would otherwise leave the registry permanently
        # half-populated, and the user would meet "no provider configured" for the rest
        # of the session with nothing saying why. Registration skips names already
        # present, so the retry this allows is free.
        logger.warning("web backend registration failed: %s", exc)
        return
    with _lock:
        _builtins_registered = True


def reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    global _builtins_registered
    with _lock:
        _providers.clear()
        _builtins_registered = False


# ---------------------------------------------------------------------------
# Availability probes
# ---------------------------------------------------------------------------


def ddgs_package_importable() -> bool:
    """Return True when the ``ddgs`` Python package can be imported.

    ddgs is the only backend whose availability is driven by a package presence rather
    than an env var or config entry. Wrapped in a helper so auto-detect and
    :func:`is_backend_available` share one check (and tests can patch a single symbol).
    """
    try:
        import ddgs  # noqa: F401 - availability probe only
    except ImportError:
        return False
    return True


def _tavily_explicitly_configured() -> bool:
    return any(
        config_name(key) == "tavily"
        for key in ("backend", "search_backend", "extract_backend")
    )


def _is_available_safe(provider: WebSearchProvider) -> bool:
    """Wrap ``is_available()`` so a buggy provider does not kill resolution."""
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001 - a broken provider is "unavailable"
        logger.debug("provider %s.is_available() raised %s", provider.name, exc)
        return False


def is_backend_available(backend: str) -> bool:
    """Return True when the named backend is currently usable.

    The single chokepoint through which selection and the registration gate both resolve
    availability. A name outside :data:`_BUILTIN_BACKENDS` is delegated to its registered
    provider; the built-ins keep their cheap hardcoded probes so this stays callable at
    registration time and on every dispatch.
    """
    backend = (backend or "").lower().strip()
    if backend not in _BUILTIN_BACKENDS:
        provider = get_provider(backend)
        if provider is not None:
            return _is_available_safe(provider)
    if backend == "exa":
        return has_env("EXA_API_KEY")
    if backend == "parallel":
        return has_env("PARALLEL_API_KEY")
    if backend == "keenable":
        return has_env("KEENABLE_API_KEY")
    if backend == "firecrawl":
        # Hermes calls ``check_firecrawl_api_key()`` here, which additionally answers True
        # for an explicit firecrawl selection with no credentials (it would be served
        # keyless) and for a ready Nous gateway. The gateway does not exist here, and the
        # keyless half is already covered: an explicit selection resolves to the
        # registered provider, whose ``is_keyless_available()`` :func:`provider_is_ready`
        # then accepts. Same answer, one probe instead of two.
        return has_env("FIRECRAWL_API_KEY") or has_env("FIRECRAWL_API_URL")
    if backend == "tavily":
        return has_env("TAVILY_API_KEY") or _tavily_explicitly_configured()
    if backend == "searxng":
        return has_env("SEARXNG_URL")
    if backend == "brave-free":
        return has_env("BRAVE_SEARCH_API_KEY")
    if backend == "ddgs":
        return ddgs_package_importable()
    if backend == "xai":
        # Delegated rather than probed with ``has_env`` because the credential may be an
        # OAuth grant in the auth store instead of ``XAI_API_KEY``; the provider's own
        # probe reads both and is documented as taking no lock and touching no network,
        # which is what makes it callable from here. Hermes delegates the same way, to
        # ``has_xai_credentials()``.
        provider = get_provider("xai")
        return _is_available_safe(provider) if provider is not None else False
    return False


# ---------------------------------------------------------------------------
# Selection ladder -- "which name", strictly
# ---------------------------------------------------------------------------


def selection_stored() -> bool:
    """Whether the user ever named a backend, so an unknown name is their typo.

    All three keys count. An install that set only ``extract_backend`` has still made a
    deliberate choice, and must get the strict "you named a backend that does not exist"
    error rather than being quietly walked onto whatever else is registered.

    Hermes' ``selection_exists("web")`` (``tools/tool_backend_helpers.py:381-402``),
    minus the two signals that have no counterpart here: the ``use_gateway`` legacy key
    and the ``nous`` managed row, both of which belong to the subscription product this
    port does not carry.
    """
    return bool(
        config_name("backend")
        or config_name("search_backend")
        or config_name("extract_backend")
    )


def backend_name() -> str:
    """Determine the shared backend name.

    A stored ``backend`` is returned as-is -- no availability probe, no fallback -- so a
    typo surfaces as the vendor path's honest error rather than silently rerouting
    through the credential ladder. The ladder runs ONLY when nothing was ever stored:
    explicit credentials first (a deliberate setup is not pre-empted), free tiers behind
    them, externally-registered providers after that, and the keyless ring last of all.

    "Nothing was ever stored" is :func:`selection_stored`, all three keys -- Hermes'
    ``selection_exists("web")`` branch (``tools/web_tools.py:246-250``). An install that
    named only a per-capability backend HAS configured its web section, so the shared
    name it left blank falls to the plain default instead of to whatever credential the
    environment happens to hold. That branch is what keeps a split config working: a
    ``search_backend: searxng`` install with no extract half asks this function for the
    extract name, and the ladder would answer "searxng" off ``SEARXNG_URL`` -- a
    search-only backend, so ``web_extract`` would refuse every call for want of exactly
    the renderer the default names.
    """
    configured = config_name("backend")
    if configured:
        return configured

    if selection_stored():
        # A per-capability key is set but the shared name is empty: configured, so the
        # autodetect ladder is not this install's answer. Same value the walk below ends
        # on, reached without consulting the environment.
        return _DEFAULT_BACKEND

    for candidate, available in (
        ("tavily", has_env("TAVILY_API_KEY")),
        ("exa", has_env("EXA_API_KEY")),
        ("parallel", has_env("PARALLEL_API_KEY")),
        ("keenable", has_env("KEENABLE_API_KEY")),
        ("firecrawl", has_env("FIRECRAWL_API_KEY") or has_env("FIRECRAWL_API_URL")),
        ("searxng", has_env("SEARXNG_URL")),
        ("brave-free", has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", ddgs_package_importable()),
    ):
        if available:
            return candidate

    # Everything below consults the registry, so it has to be populated first --
    # Hermes calls ``_ensure_web_plugins_loaded()`` at exactly this point. Without it a
    # caller that reached the ladder before anything registered would fall past both
    # walks and get the "firecrawl" default on a machine the keyless ring could serve.
    ensure_backends_registered()

    # A provider registered from outside this package (no built-in credential probe
    # covers it), gated by its own is_available().
    for provider in list_providers():
        if provider.name in _BUILTIN_BACKENDS:
            continue
        if _is_available_safe(provider):
            return provider.name

    # Keyless free-tier walk -- zero credentials anywhere. Strictly last so it never
    # pre-empts any keyed or importable backend above.
    if keyless_tier_enabled():
        for name in keyless_walk_order():
            provider = get_provider(name)
            if provider is None:
                continue
            try:
                if provider.is_keyless_available():
                    return name
            except Exception as exc:  # noqa: BLE001 - skip a broken provider
                logger.debug("provider %r.is_keyless_available() raised: %s", name, exc)

    return _DEFAULT_BACKEND  # default (backward compat)


def search_backend_name() -> str:
    """The backend name for search: ``search_backend``, else the shared ``backend``."""
    return config_name("search_backend") or backend_name()


def extract_backend_name() -> str:
    """The backend name for extract: ``extract_backend``, else the shared ``backend``.

    Hermes' ``_get_capability_backend("extract")``. Separate from search because the two
    capabilities are commonly split -- a self-hosted SearXNG index cannot render a page,
    so an install points ``search_backend`` at it and ``extract_backend`` at a vendor
    that can. Strict in the same way its twin is: a stored name is returned unprobed.
    """
    return config_name("extract_backend") or backend_name()


# ---------------------------------------------------------------------------
# Resolution walk -- "which registered provider"
# ---------------------------------------------------------------------------


def _resolve(configured: str | None, *, capability: str) -> WebSearchProvider | None:
    """Resolve the active provider for *capability* (``"search"`` or ``"extract"``).

    1. **Explicit config wins, ignoring availability.** A configured name that is
       registered *and can serve this capability* is returned even when its
       :meth:`is_available` is False -- the dispatcher then surfaces a precise
       "X_API_KEY is not set" error instead of silently routing somewhere else.
    2. **Single-provider shortcut.** When exactly one registered provider can serve this
       capability and reports available, use it.
    3. **Legacy preference walk, filtered by capability and availability** -- firecrawl,
       parallel, tavily, exa, searxng, brave-free, ddgs. The path that fires when no
       config key is set: pick the highest-priority backend the user actually has
       credentials for.
    4. **Keyless walk.** No credentialed backend at all: fall back to a provider that can
       serve anonymously, unless the tier is disabled. Never pre-empts a keyed setup --
       it is only reachable when the walk above found nothing.

    The capability filter runs at all four steps, as in Hermes' ``_resolve``. Skipping it
    at any one of them would route a batch of URLs to a backend with no renderer, which is
    worse than nothing: returning None lets the caller say "that backend is search-only"
    while a wrong provider produces a plausible-looking empty answer.

    Returns None when nothing matches; the dispatcher then tells the user to set a
    provider up.
    """
    with _lock:
        snapshot = dict(_providers)

    def _capable(provider: WebSearchProvider) -> bool:
        if capability == "extract":
            return bool(provider.supports_extract())
        return bool(provider.supports_search())

    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider):
            return provider
        if provider is None:
            logger.debug(
                "web backend '%s' configured but not registered; falling back", configured
            )
        else:
            logger.debug(
                "web backend '%s' configured but does not support '%s'; falling back",
                configured,
                capability,
            )

    eligible = [p for p in snapshot.values() if _capable(p) and _is_available_safe(p)]
    if len(eligible) == 1:
        return eligible[0]

    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if provider is not None and _capable(provider) and _is_available_safe(provider):
            return provider

    if keyless_tier_enabled():
        for name in keyless_walk_order():
            provider = snapshot.get(name)
            if provider is None or not _capable(provider):
                continue
            try:
                if provider.is_keyless_available():
                    return provider
            except Exception as exc:  # noqa: BLE001 - buggy provider skipped
                logger.debug("provider %s.is_keyless_available() raised %s", name, exc)

    return None


def resolve_search_provider(configured: str | None = None) -> WebSearchProvider | None:
    """Resolve the active search provider; see :func:`_resolve` for the four steps."""
    return _resolve(configured, capability="search")


def resolve_extract_provider(configured: str | None = None) -> WebSearchProvider | None:
    """Resolve the active extract provider; see :func:`_resolve` for the four steps."""
    return _resolve(configured, capability="extract")


def active_search_provider() -> WebSearchProvider | None:
    """Resolve the currently-active search provider from config."""
    return resolve_search_provider(config_name("search_backend") or config_name("backend"))


def active_extract_provider() -> WebSearchProvider | None:
    """Resolve the currently-active extract provider from config."""
    return resolve_extract_provider(
        config_name("extract_backend") or config_name("backend")
    )


def provider_is_ready(provider: WebSearchProvider | None) -> bool:
    """Return True when *provider* reports readiness without raising.

    :func:`resolve_search_provider` intentionally returns an explicitly configured
    backend even when ``is_available()`` is False, so the dispatcher can emit a precise
    missing-credential error. A registration gate must still require a true availability
    probe -- otherwise the tool is offered for a backend that cannot run. A provider that
    can serve anonymously IS ready: keyless mode is a working state, not a
    misconfiguration.
    """
    if provider is None:
        return False
    try:
        if provider.is_available():
            return True
    except Exception as exc:  # noqa: BLE001 - broken provider == not ready
        logger.debug("provider %r.is_available() raised: %s", provider, exc)
        return False
    try:
        return bool(provider.is_keyless_available())
    except Exception as exc:  # noqa: BLE001 - broken provider == not ready
        logger.debug("provider %r.is_keyless_available() raised: %s", provider, exc)
        return False


def web_search_available() -> bool:
    """Whether anything can serve a search: the gate for registering the tool.

    Hermes' ``check_web_api_key``. Config-and-environment only, never a network call:
    this runs when a session is assembled.
    """
    ensure_backends_registered()
    configured = config_name("backend")
    if configured and is_backend_available(configured):
        return True
    # Any built-in backend with credentials present. A boolean OR, so unlike
    # backend_name() the probe order is irrelevant.
    if any(is_backend_available(backend) for backend in _BUILTIN_BACKENDS):
        return True
    # Both capabilities, as in Hermes (``tools/web_tools.py:1562-1566``). This one gate
    # registers web_search AND web_extract, so an install whose only ready provider
    # extracts would otherwise lose both tools rather than the one it cannot use -- and
    # the one it can use is the one it configured.
    return provider_is_ready(active_search_provider()) or provider_is_ready(
        active_extract_provider()
    )
