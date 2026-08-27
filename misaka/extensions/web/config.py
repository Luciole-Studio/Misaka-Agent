"""Backend selection and credentials for the web providers.

Hermes splits this across two files a user edits by hand: ``~/.hermes/config.yaml``
carries the ``web:`` section (which backend, whether the keyless tier is on, per-vendor
tier pins) and ``~/.hermes/.env`` carries the vendor API keys. MISAKA has neither file,
so both halves live in one JSON document, ``~/.misaka/web.json``:

    {
      "backend": "tavily",            // shared fallback selection
      "search_backend": "searxng",    // per-capability override, wins over `backend`
      "keyless_fallback": true,       // the no-key vendor ring (default on)
      "keyless_rescue": true,         // one-shot ring rescue for a failed backend
      "provider_tier": {"exa": "free"},
      "env": {"TAVILY_API_KEY": "tvly-..."}
    }

Every key is optional and the file itself is optional: with no file at all the keyless
ring serves searches, which is the whole point of porting it.

``env`` exists for the same reason Hermes reads ``~/.hermes/.env``: a credential set
through the config layer has to be visible to sessions that never had it exported --
sub-agent children, cron runs, anything spawned with a scrubbed environment. The
process environment still wins, so an export always overrides the file.
"""

from __future__ import annotations

import json
import os

from misaka.config.product import CFG


def _config_path() -> str:
    return os.path.expanduser(CFG["web_config"])


def web_config() -> dict:
    """Return the parsed config document, or ``{}`` when there is none.

    Read on every call rather than cached: the document is a few hundred bytes next to
    a network round trip, and a cache would need invalidating from three directions
    (the user editing the file, a test writing one, a long-lived session).
    """
    try:
        with open(_config_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def config_name(key: str) -> str:
    """Return a backend-name config value, lowercased and stripped, or ``""``."""
    value = web_config().get(key)
    return value.lower().strip() if isinstance(value, str) else ""


def config_flag(key: str, default: bool = True) -> bool:
    """Return a boolean config value; a missing entry keeps *default*.

    Truthiness, not type, as in Hermes (``bool(web_cfg.get("keyless_fallback", True))``),
    so ``0`` and ``""`` switch a tier off the way ``false`` does -- and so this agrees
    with :func:`misaka.extensions.web.cache.cache_enabled`, which reads the same document.
    ``null`` is the one departure: YAML has no way to write it that is not also "absent",
    JSON does, and a key written as null reads as never set.
    """
    value = web_config().get(key)
    return default if value is None else bool(value)


def provider_env(name: str) -> str:
    """Config-aware credential lookup: the process environment, then ``web.json``'s ``env``.

    Hermes' ``get_provider_env``. Returns the stripped value, or ``""`` when unset.
    """
    value = os.environ.get(name)
    if value is None:
        env = web_config().get("env")
        value = env.get(name) if isinstance(env, dict) else None
    return str(value or "").strip()


def has_env(name: str) -> bool:
    return bool(provider_env(name))


# Every vendor credential this package puts on the wire. Anything here that turns up in
# text bound for the model is replaced first -- see :func:`redact_secrets`.
_CREDENTIAL_VARS = (
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "KEENABLE_API_KEY",
    "FIRECRAWL_API_KEY",
)

# Endpoint settings a user may legitimately write with credentials in the userinfo
# (``http://user:pass@host``). Only the password half is secret: the host has to stay
# readable, or "Could not reach SearXNG at ..." stops naming what it could not reach.
_ENDPOINT_VARS = ("SEARXNG_URL", "TAVILY_BASE_URL", "FIRECRAWL_API_URL")

REDACTED = "<redacted>"

# A one- or two-character "credential" would blank out half a sentence, and an empty one
# would land between every pair of characters. No real key is this short.
_MIN_SECRET_CHARS = 8


def _configured_secrets() -> list[str]:
    """Every credential value currently readable, longest first."""
    values = [provider_env(name) for name in _CREDENTIAL_VARS]
    for name in _ENDPOINT_VARS:
        raw = provider_env(name)
        if "@" not in raw:
            continue
        userinfo = raw.partition("://")[2].partition("@")[0]
        # ``user:pass`` -> the password; a bare ``token@host`` -> the whole userinfo.
        values.append(userinfo.partition(":")[2] or userinfo)
    # Longest first so a key that contains a shorter value cannot leave a fragment behind.
    return sorted({v for v in values if len(v) >= _MIN_SECRET_CHARS}, key=len, reverse=True)


def redact_secrets(text: str) -> str:
    """Replace every configured credential in *text* with :data:`REDACTED`.

    MISAKA-only; Hermes has no equivalent. Every backend hands a non-2xx response body
    straight to the model, and the thing that answers is not always the vendor: any HTTP
    proxy httpx picks up from the environment can reflect the request -- headers included
    -- back as an error body. The provider layer must not do this itself (its return value
    is the contract), so the single chokepoint is the tool, next to the ``untrusted``
    fence: the last place before a search result becomes model context.
    """
    for secret in _configured_secrets():
        text = text.replace(secret, REDACTED)
    return text


def keyless_tier_enabled() -> bool:
    """Whether the keyless vendor ring may serve at all (``keyless_fallback``, default on)."""
    return config_flag("keyless_fallback", True)


def keyless_rescue_enabled() -> bool:
    """Whether a failed keyed backend gets a one-shot ring rescue (``keyless_rescue``).

    Implicitly off whenever the keyless tier itself is off: the rescue rides the ring.
    """
    return config_flag("keyless_rescue", True) and keyless_tier_enabled()


def provider_tier(name: str) -> str:
    """Return the user-selected tier for *name*: ``free``, ``paid``, or ``auto``.

    ``free`` forces the keyless public endpoint even when the vendor API key is present;
    ``paid`` forces the keyed path (a missing key then surfaces the standard
    "X_API_KEY not set" error instead of silently downgrading to the free tier).
    Anything else -- including unset -- is ``auto``: key present -> keyed, otherwise
    keyless when the tier is enabled.
    """
    tiers = web_config().get("provider_tier")
    if not isinstance(tiers, dict):
        return "auto"
    value = str(tiers.get(name, "") or "").lower().strip()
    return value if value in ("free", "paid") else "auto"


def use_keyless(name: str, api_key: str) -> bool:
    """Decide whether provider *name* should route via the keyless endpoint.

    Single chokepoint so tier semantics cannot drift between vendors:

    - tier ``free``  -> keyless, even when *api_key* is set
    - tier ``paid``  -> keyed, even when *api_key* is missing (the keyed path then
      raises its usual missing-key error)
    - tier ``auto``  -> keyed when *api_key* is set; otherwise keyless when
      ``keyless_fallback`` is enabled
    """
    tier = provider_tier(name)
    if tier == "free":
        return True
    if tier == "paid":
        return False
    return not api_key and keyless_tier_enabled()
