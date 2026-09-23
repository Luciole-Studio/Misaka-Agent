"""Which provider serves each Web capability, scoped to its WebPart/extension owners.

Hermes keeps separate selection and resolution ladders: an explicit selection wins
without hiding missing credentials; fallback walks filter capability and availability.
MISAKA preserves those ladders, with a built-in base and an overlay rebuilt from the
existing loader's successfully loaded Extensions. Removing an owner reveals the lower
owner. No separate plugin manager or process-wide extension registration table.

xAI remains explicit-only. Nous uses a distinct managed provider, never a hidden
billing fallback for a stored vendor selection.
"""

from __future__ import annotations

import logging
import secrets

from misaka.core.web.config import (
    config_name,
    has_env,
    keyless_tier_enabled,
    provider_disabled,
    provider_selected,
)
from misaka.core.web.keyless import keyless_walk_order
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.scope import current_scope

logger = logging.getLogger(__name__)

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
        "perplexity",
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
    "perplexity",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)

# The shared fallback when autodetect finds nothing, as in Hermes.
_DEFAULT_BACKEND = "firecrawl"


def validate_provider(provider: WebSearchProvider) -> str:
    """The same boundary for direct registration and the session extension API."""
    if not isinstance(provider, WebSearchProvider):
        raise TypeError(
            f"register_provider() expects a WebSearchProvider instance, "
            f"got {type(provider).__name__}"
        )
    raw_name = provider.name
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("Web provider .name must be a non-empty string")
    name = raw_name.strip()
    if name != name.lower() or any(char.isspace() for char in name):
        raise ValueError("Web provider names must be lowercase with no spaces")
    validate_setup_schema(provider.get_setup_schema())
    return name


def validate_setup_schema(schema):
    """Shared Web/browser plugin metadata consumed by setup and secret isolation."""
    if not isinstance(schema, dict) or not isinstance(schema.get("variants", []), list):
        raise TypeError("Web setup schema must be an object with an optional variants list")
    for row in [schema, *schema.get("variants", [])]:
        if not isinstance(row, dict) or not isinstance(row.get("env_vars", []), list):
            raise TypeError("Web setup rows must be objects with an env_vars list")
        for variable in row.get("env_vars", []):
            key = variable.get("key") if isinstance(variable, dict) else None
            if not isinstance(key, str) or not key.isascii() or not key.isidentifier():
                raise ValueError("Web setup environment keys must be ASCII identifiers")


def register_provider(provider: WebSearchProvider) -> None:
    """Register in the current scope. Extensions use registerWebSearchProvider instead."""
    name = validate_provider(provider)
    scope = current_scope()
    with scope.lock:
        scope.providers[name] = provider
        scope.registration_id = secrets.token_hex(16)


def replace_extension_providers(extensions) -> None:
    """Rebuild from live owners: failed loads never publish; unload restores lower owners."""
    providers, owners = {}, {}
    for extension in extensions:
        for name, provider in extension.webProviders.items():
            providers[name], owners[name] = provider, extension.path
    scope = current_scope()
    with scope.lock:
        if (scope.owners == owners and scope.extensions.keys() == providers.keys()
                and all(scope.extensions[name] is provider for name, provider in providers.items())):
            return
        scope.extensions, scope.owners = providers, owners
        scope.registration_id = secrets.token_hex(16)


def list_providers(*, include_disabled: bool = False) -> list[WebSearchProvider]:
    """Return all registered providers, sorted by name."""
    scope = current_scope()
    with scope.lock:
        items = list({**scope.providers, **scope.extensions}.values())
    if not include_disabled:
        items = [provider for provider in items if not provider_disabled(provider.name)]
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str, *, include_disabled: bool = False) -> WebSearchProvider | None:
    """Return the provider registered under *name*, or None."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not include_disabled and provider_disabled(name):
        return None
    scope = current_scope()
    with scope.lock:
        return scope.extensions.get(name, scope.providers.get(name))


def ensure_backends_registered() -> None:
    """Register the bundled backends once, if they have not been.

    Hermes' ``_ensure_web_plugins_loaded``. Both the registration-time availability gate
    and the first dispatch reach the registry, and either may be first; like Hermes'
    discovery call this is idempotent and cheap after the first time.

    The guard is a flag rather than "is the map empty", because an externally registered
    provider landing first would otherwise convince this that the built-ins are loaded.
    """
    scope = current_scope()
    with scope.lock:
        if scope.builtins_registered:
            return
        from misaka.core.web.backends import register_builtin_providers

        try:
            register_builtin_providers()
        except Exception as exc:  # noqa: BLE001 - retry a broken import on the next probe
            logger.warning("web backend registration failed: %s", exc)
            return
        scope.builtins_registered = True


def reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    scope = current_scope()
    with scope.lock:
        scope.providers.clear()
        scope.builtins.clear()
        scope.extensions.clear()
        scope.owners.clear()
        scope.builtins_registered = False
        scope.registration_id = ""


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
    except Exception:  # noqa: BLE001 - a broken provider is "unavailable"
        return False


def is_backend_available(backend: str) -> bool:
    """Return True when the named backend is currently usable.

    The single chokepoint through which selection and the registration gate both resolve
    availability. A name outside :data:`_BUILTIN_BACKENDS` is delegated to its registered
    provider; the built-ins keep their cheap hardcoded probes so this stays callable at
    registration time and on every dispatch.
    """
    backend = (backend or "").lower().strip()
    if provider_disabled(backend):
        return False
    provider = get_provider(backend)
    if provider is not None and provider is not current_scope().builtins.get(backend):
        return _is_available_safe(provider)
    if backend == "exa":
        return has_env("EXA_API_KEY")
    if backend == "parallel":
        return has_env("PARALLEL_API_KEY")
    if backend == "perplexity":
        return has_env("PERPLEXITY_API_KEY")
    if backend == "keenable":
        return has_env("KEENABLE_API_KEY")
    if backend == "firecrawl":
        return has_env("FIRECRAWL_API_KEY") or has_env("FIRECRAWL_API_URL") or provider_selected("firecrawl")
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
    through the credential ladder. Autodetect runs when no SHARED backend was stored:
    per-capability keys name only their own capability, never the other one.

    Ported from Hermes 010a45097e49 (#113017). Its remaining shared-selection guard
    handles legacy ``use_gateway``, which MISAKA does not use; an explicit native
    ``backend: nous`` is already handled by the configured-name return below.
    """
    configured = config_name("backend")
    if configured:
        return configured

    from misaka.core.web.gateway import available as gateway_available

    for candidate, available in (
        ("tavily", has_env("TAVILY_API_KEY")),
        ("perplexity", has_env("PERPLEXITY_API_KEY")),
        ("exa", has_env("EXA_API_KEY")),
        ("parallel", has_env("PARALLEL_API_KEY")),
        ("keenable", has_env("KEENABLE_API_KEY")),
        ("firecrawl", has_env("FIRECRAWL_API_KEY") or has_env("FIRECRAWL_API_URL")),
        ("nous", gateway_available()),
        ("searxng", has_env("SEARXNG_URL")),
        ("brave-free", has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", ddgs_package_importable()),
    ):
        if available and not provider_disabled(candidate):
            provider = get_provider(candidate)
            if (provider is not None and provider is not current_scope().builtins.get(candidate)
                    and not _is_available_safe(provider)):
                continue
            return candidate

    # Everything below consults the registry, so it has to be populated first --
    # Hermes calls ``_ensure_web_plugins_loaded()`` at exactly this point. Without it a
    # caller that reached the ladder before anything registered would fall past both
    # walks and get the "firecrawl" default on a machine the keyless ring could serve.
    ensure_backends_registered()

    # A provider registered from outside this package (no built-in credential probe
    # covers it), gated by its own is_available().
    for provider in list_providers():
        if provider is current_scope().builtins.get(provider.name):
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
            except Exception:  # noqa: BLE001, S112 - skip a broken provider
                continue

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
    snapshot = {provider.name.strip(): provider for provider in list_providers()}

    def _capable(provider: WebSearchProvider) -> bool:
        if capability == "extract":
            return bool(provider.supports_extract())
        return bool(provider.supports_search())

    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider):
            return provider

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
            except Exception:  # noqa: BLE001, S112 - buggy provider skipped
                continue

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
    except Exception:  # noqa: BLE001 - broken provider == not ready
        return False
    try:
        return bool(provider.is_keyless_available())
    except Exception:  # noqa: BLE001 - broken provider == not ready
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
