"""Backend selection and credentials for the web providers.

Hermes splits this across two files a user edits by hand: ``~/.hermes/config.yaml``
carries the ``web:`` section (which backend, whether the keyless tier is on, per-vendor
tier pins) and ``~/.hermes/.env`` carries the vendor API keys. MISAKA keeps the same
split: the ``web`` section of ``settings.json`` (global, and a role's own overlay) and the
vendor keys in the home's ``.env`` (``config.env``; a role's own ``.env`` overlays it). This
module still works on one merged document, the way Hermes' web code sees it:

    {
      "backend": "tavily",            // shared fallback selection
      "search_backend": "searxng",    // per-capability override, wins over `backend`
      "extract_backend": "firecrawl", // the other capability; searxng cannot render a page
      "keyless_fallback": true,       // the no-key vendor ring (default on)
      "keyless_rescue": true,         // one-shot ring rescue for a failed backend
      "provider_tier": {"exa": "free"},
      "cache_enabled": true,          // the search memo and the extract disk cache
      "cache_ttl_minutes": 20,
      "cache_exempt_hosts": ["staging.example"],  // public DNS, but must always be live
      "extract_char_limit": 15000,    // per-page budget web_extract sends the model
      "allow_private_urls": false,    // let the web tools reach private addresses
      "website_blocklist": {"enabled": true, "domains": ["ads.example"],
                            "shared_files": ["blocked.txt"]},
      "xai": {"model": "grok-build-0.1", "excluded_domains": ["example.com"]},
      "env": {"TAVILY_API_KEY": "tvly-..."}
    }

Every key is optional and so is the section: with nothing configured the keyless ring
serves searches, which is the whole point of porting it.

A role's ``web`` section overlays the global one (``settings_manager.ROLE_KEYS``). Mappings
merge; lists and scalar values replace. Unsetting a role key reveals the shared value.
Writes change only the selected layer.

``env`` is split by what a name is: a credential (``TAVILY_API_KEY``, anything shaped like
a key or token) lives in ``.env`` -- the home's, or the role's own when set with
``--profile``; a proxy, a CA bundle or an endpoint (``HTTPS_PROXY``, ``SSL_CERT_FILE``,
``SEARXNG_URL``) is a setting, in the ``web`` section. Both layer the same way, role over
home. ``misaka web set env.X`` routes by the same rule. Readers see one ``env`` mapping
either way; of the ``.env`` files only the credential-shaped names are the web layer's.

``env`` exists for the same reason Hermes reads ``~/.hermes/.env``: a credential set
through the config layer has to be visible to sessions that never had it exported --
sub-agent children, cron runs, anything spawned with a scrubbed environment. The
process environment still wins, so an export always overrides the file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from misaka.config import home
from misaka.core.web.scope import current_scope

logger = logging.getLogger(__name__)


def _manager():
    from misaka.core.settings_manager import SettingsManager

    return SettingsManager.forRole(current_scope().profile_dir)


def _config_path() -> str:
    """The settings file the current scope's ``web`` section is in: the role's, else the home's."""
    profile = current_scope().profile_dir
    if profile is not None:
        return str(Path(profile) / "settings.json")
    try:
        return str(home.path("settings"))
    except RuntimeError:            # no home directory at all: nothing is configured anywhere
        return ""


def _credentials_path(profile_dir: str | None = None) -> str:
    """The file a layer's vendor credentials are in: the role's own ``.env``, else the home's."""
    from misaka.config import env

    return str(env.path(profile_dir))


def config_label() -> str:
    """The settings file in effect, the way a user would type it -- for messages."""
    return home.display(_config_path())


def _read_credentials(profile_dir: str | None = None) -> dict:
    """The credential-shaped names one layer's ``.env`` holds (the rest of that file is other
    code's business). A file that cannot be read counts as holding none."""
    from misaka.config import env

    try:
        stored = env.read(profile_dir)
    except env.EnvFileError as error:
        logger.warning("%s", error)
        return {}
    return {name: value for name, value in stored.items() if is_credential_var(name)}


def is_credential_var(name: str) -> bool:
    """Whether an ``env`` entry is a credential (kept in the credentials file) rather than a
    setting such as a proxy or endpoint (kept with the other web settings)."""
    upper = name.upper()
    if upper in _PUBLIC_VARS or upper in _ENDPOINT_VARS:
        return False
    return upper in _CREDENTIAL_VARS or upper.endswith(_SECRET_NAME_SUFFIXES)


def own_section() -> dict:
    """The current scope's ``web`` section as its own file holds it (no inherited values)."""
    from misaka.core.settings_manager import SettingsManager

    profile = current_scope().profile_dir
    manager = SettingsManager.forRole(profile)
    return manager.getScopedSection("role" if profile is not None else "global", "web")


def load_config(profile_dir: str | None = None) -> dict:
    """The global ``web`` section, then the role's own, then the user's credentials.

    An unreadable settings file is an error, never a writable empty default.
    """
    from misaka.core.settings_manager import SettingsManager, deep_merge_settings

    manager = SettingsManager.forRole(profile_dir)
    if manager.globalSettingsLoadError is not None:
        raise ValueError(f"{home.path('settings')}: {manager.globalSettingsLoadError}")
    if manager.roleSettingsLoadError is not None:
        raise ValueError(f"{profile_dir}/settings.json: {manager.roleSettingsLoadError}")
    result: dict = {}
    for scope, base in (("global", str(home.home())), ("role", profile_dir)):
        if base is None:
            continue
        doc = manager.getScopedSection(scope, "web")
        policy = doc.get("website_blocklist")
        if isinstance(policy, dict) and isinstance(policy.get("shared_files"), list):
            # Resolve in the declaring layer before merging. An inherited global list
            # does not become relative to whichever profile happens to read it.
            policy["shared_files"] = [
                str((Path(base) / Path(item).expanduser()).resolve())
                if isinstance(item, str) and item.strip() else item
                for item in policy["shared_files"]
            ]
        result = deep_merge_settings(result, doc)
    credentials = {**_read_credentials(), **(_read_credentials(profile_dir) if profile_dir else {})}
    if credentials:
        result["env"] = {**(result.get("env") if isinstance(result.get("env"), dict) else {}), **credentials}
    return result


def web_config(*, strict: bool = False) -> dict:
    """One snapshot per tool call; fresh reads between calls and in the settings CLI."""
    scope = current_scope()
    if scope.config is not None:
        if strict and scope.config_error is not None:
            raise scope.config_error
        return scope.config
    try:
        return load_config(scope.profile_dir)
    except (OSError, ValueError, RuntimeError):     # RuntimeError: no home directory at all
        if strict:
            raise
        return {}


def config_name(key: str) -> str:
    """Return a backend-name config value, lowercased and stripped, or ``""``."""
    value = web_config().get(key)
    return value.lower().strip() if isinstance(value, str) else ""


def provider_selected(name: str) -> bool:
    """Whether any explicit Web capability selection names this provider."""
    cfg = web_config()
    return any(isinstance(value := cfg.get(key), str) and value.strip().lower() == name
               for key in ("backend", "search_backend", "extract_backend"))


def config_flag(key: str, default: bool = True) -> bool:
    """Return a boolean config value; a missing entry keeps *default*.

    Truthiness, not type, as in Hermes (``bool(web_cfg.get("keyless_fallback", True))``),
    so ``0`` and ``""`` switch a tier off the way ``false`` does -- and so this agrees
    with :func:`misaka.core.web.cache.cache_enabled`, which reads the same document.
    ``null`` is the one departure: YAML has no way to write it that is not also "absent",
    JSON does, and a key written as null reads as never set.
    """
    value = web_config().get(key)
    return default if value is None else bool(value)


def provider_env(name: str) -> str:
    """Config-aware credential lookup: the process environment, then the ``env`` map of the
    ``web`` settings section.

    Hermes' ``get_provider_env``. Returns the stripped value, or ``""`` when unset.
    """
    environment = current_scope().environment
    value = (os.environ if environment is None else environment).get(name)
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
    "XAI_API_KEY",
    "PERPLEXITY_API_KEY", "BROWSERBASE_API_KEY", "BROWSER_USE_API_KEY", "CAMOFOX_API_KEY",
    "TOOL_GATEWAY_USER_TOKEN",
)

# Endpoint settings a user may legitimately write with credentials in the userinfo
# (``http://user:pass@host``). Only the password half is secret: the host has to stay
# readable, or "Could not reach SearXNG at ..." stops naming what it could not reach.
_ENDPOINT_VARS = ("SEARXNG_URL", "TAVILY_BASE_URL", "PARALLEL_BASE_URL", "FIRECRAWL_API_URL", "XAI_BASE_URL",
                  "PERPLEXITY_BASE_URL", "DDGS_PROXY", "BROWSERBASE_BASE_URL", "BROWSER_USE_BASE_URL",
                  "CAMOFOX_URL", "BROWSER_CDP_URL", "NOUS_PORTAL_URL", "FIRECRAWL_GATEWAY_URL", "BROWSER_USE_GATEWAY_URL")

REDACTED = "<redacted>"

# Suffixes that make an environment variable name credential-shaped. Used only to decide
# what a helper subprocess must NOT inherit, so a false positive costs a child a variable
# it had no business reading and a false negative leaks a key.
_SECRET_NAME_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")

# Account identifiers and endpoint metadata are not bearer credentials.
_PUBLIC_VARS = ("TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME", "BROWSERBASE_PROJECT_ID", "CAMOFOX_USER_ID")


def provider_variables() -> set[str]:
    scope = current_scope()
    names = set(_CREDENTIAL_VARS + _ENDPOINT_VARS + _PUBLIC_VARS)
    for provider in [*{**scope.providers, **scope.extensions}.values(), *scope.browser_providers.values()]:
        schema = provider.get_setup_schema()
        for row in [schema, *schema.get("variants", [])]:
            names.update(item["key"] for item in row.get("env_vars", []))
    return names


def without_credentials(env: dict[str, str]) -> dict[str, str]:
    """Return *env* with every credential-shaped variable removed.

    MISAKA runs DDGS and browser engines in owned helper processes. These run
    third-party libraries or binaries. That library needs a search string and a
    network route; it has no reason to see ``ANTHROPIC_API_KEY`` or ``EXA_API_KEY``, and a
    compromised or merely careless dependency reading ``os.environ`` is the whole reason
    Hermes runs its own search worker under ``_sanitize_subprocess_env``
    (``tools/environments/local.py``).

    A denylist rather than an allowlist, as in Hermes: an allowlist that forgets
    ``HTTPS_PROXY`` or ``SSL_CERT_FILE`` breaks a working corporate install, and the cost
    of that is worse than the residual risk of a credential named in some shape this misses.
    Networking, locale and path variables pass through untouched.
    """
    denied = provider_variables()
    return {
        name: value
        for name, value in env.items()
        if name not in denied and not name.upper().endswith(_SECRET_NAME_SUFFIXES)
    }

# A one- or two-character "credential" would blank out half a sentence, and an empty one
# would land between every pair of characters. No real key is this short.
_MIN_SECRET_CHARS = 8


def remember_secret(value):
    if isinstance(value, str) and len(value) >= _MIN_SECRET_CHARS:
        values = current_scope().runtime_secrets
        values[value] = None
        while len(values) > 128:
            values.pop(next(iter(values)))


def _configured_secrets() -> list[str]:
    """Every credential value currently readable, longest first."""
    names = provider_variables()
    endpoints = {name for name in names if name in _ENDPOINT_VARS or name.endswith("_URL")}
    scope = current_scope()
    with scope.lock:
        vault_values = set(scope.vault_secrets)
        remembered = list(scope.runtime_secrets)
    values = [provider_env(name) for name in names - endpoints - set(_PUBLIC_VARS)] + remembered
    for name in endpoints:
        raw = provider_env(name)
        if "@" not in raw:
            continue
        userinfo = raw.partition("://")[2].partition("@")[0]
        # ``user:pass`` -> the password; a bare ``token@host`` -> the whole userinfo.
        values.append(userinfo.partition(":")[2] or userinfo)
    from misaka.core.web.network import proxy_secrets

    try:
        values.extend(proxy_secrets())
    except ValueError:
        pass  # A malformed non-string proxy setting has no credential to put on the wire.
    # Longest first so a key that contains a shorter value cannot leave a fragment behind.
    # Vault values (including short PINs) must survive API-key ring eviction:
    # later page readback can still contain a password filled earlier in this session.
    return sorted({v for v in values if len(v) >= _MIN_SECRET_CHARS} | vault_values, key=len, reverse=True)


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

    Firecrawl direct credentials/self-hosting win over tier, as in Hermes.
    For the other providers:

    - tier ``free``  -> keyless, even when *api_key* is set
    - tier ``paid``  -> keyed, even when *api_key* is missing (the keyed path then
      raises its usual missing-key error)
    - tier ``auto``  -> keyed when *api_key* is set; otherwise keyless when
      ``keyless_fallback`` is enabled
    """
    if name == "firecrawl" and (api_key or provider_env("FIRECRAWL_API_URL")):
        return False
    tier = provider_tier(name)
    if tier == "free":
        return True
    if tier == "paid":
        return False
    return not api_key and keyless_tier_enabled()


# ── Writing the config document ───────────────────────────────────────────────
# Read-only until here. The CLI (``misaka web``) is the one writer, so a user does not
# have to hand-edit JSON; the file can hold vendor API keys, so it is written 0600.

_BOOL_KEYS = frozenset(
    {"keyless_fallback", "keyless_rescue", "cache_enabled", "allow_private_urls", "debug_enabled", "proxy_dns"}
)
_NESTED_KEYS = frozenset({"env", "provider_tier", "xai", "x_search", "browser", "vault", "website_blocklist",
                          "http_timeout", "operation_timeout"})
_SCALAR_KEYS = frozenset(
    {"backend", "search_backend", "extract_backend", "cache_ttl_minutes", "extract_char_limit", "extract_timeout"}
)
# Written from the CLI as one comma-separated argument, stored as a JSON list. A host
# pattern cannot contain a comma, so splitting on it costs nothing and spares the user a
# text editor for what is usually one entry.
_LIST_KEYS = frozenset({"cache_exempt_hosts", "disabled_providers", "trusted_private_hosts"})
_VALID_TIERS = frozenset({"free", "paid", "auto"})

# Subkeys of a nested section that are not plain strings. Booleans have to be coerced or
# ``config_flag`` reads the string "off" as truthy; lists have to be split or a blocklist
# of three domains is stored as one 40-character "domain" that matches nothing.
_NESTED_BOOL_KEYS = {("website_blocklist", "enabled"), *(("browser", key) for key in
    ("enabled", "headed", "record_sessions", "use_real_profile", "camofox_managed_persistence", "camofox_adopt_existing_tab", "camofox_rewrite_loopback_urls"))}
_NESTED_LIST_KEYS = {
    ("website_blocklist", "domains"),
    ("website_blocklist", "shared_files"),
    ("xai", "allowed_domains"),
    ("xai", "excluded_domains"),
}


def _as_bool(key: str, value: str) -> bool:
    low = value.strip().lower()
    if low in ("true", "on", "1", "yes"):
        return True
    if low in ("false", "off", "0", "no"):
        return False
    raise ValueError(f"{key} takes true/false, not {value!r}")


def _as_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _coerce(key: str, value: str) -> bool | str | list[str]:
    if key == 'trusted_private_hosts':
        from misaka.core.web.network import trusted_private_hosts

        return list(trusted_private_hosts(_as_list(value)))
    if key in ("backend", "search_backend", "extract_backend"):
        return value.strip().lower()
    if key in _BOOL_KEYS:
        return _as_bool(key, value)
    if key in _LIST_KEYS:
        return _as_list(value)
    return value


def _coerce_nested(section: str, key: str, value: str) -> Any:
    if section == "vault":
        if key == "enabled":
            return _as_bool("vault.enabled", value)
        if key not in {"onepassword", "bitwarden"}:
            raise ValueError("vault takes enabled, onepassword or bitwarden")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError(f"vault.{key} requires a JSON object")
        return parsed
    if (section == "x_search" and key in {"timeout_seconds", "retries"}) or (section == "browser" and key in {"command_timeout", "open_timeout", "recording_retention"}):
        parsed = json.loads(value)
        from misaka.core.web.timeouts import _seconds
        _seconds(parsed, f"{section}.{key}", positive=key != "retries")
        if key in {"retries", "recording_retention"} and type(parsed) is not int:
            raise ValueError(f"{section}.{key} takes an integer")
        return parsed
    if section == "browser" and key in {"controller_command", "controller_capabilities"}:
        parsed = json.loads(value)
        if not isinstance(parsed, list) or any(not isinstance(v, str) for v in parsed):
            raise ValueError(f"browser.{key} requires a JSON array of strings")
        return parsed
    if section in {"http_timeout", "operation_timeout"}:
        from misaka.core.web.timeouts import validate_setting

        try:
            parsed = json.loads(value)
        except ValueError as error:
            raise ValueError(f"{section}.{key} requires a JSON number, null or HTTP phase object") from error
        return validate_setting(section, key, parsed)
    if (section, key) in _NESTED_BOOL_KEYS:
        return _as_bool(f"{section}.{key}", value)
    if (section, key) in _NESTED_LIST_KEYS:
        return _as_list(value)
    return value.strip() if section == "provider_tier" else value


def _set_value(doc: dict, dotted_key: str, value: str) -> None:
    """Update one document value, addressed by ``key`` or ``section.key``.

    ``section.key`` reaches the nested maps a user needs -- ``env.TAVILY_API_KEY`` for a
    credential, ``provider_tier.exa`` for a tier pin, ``website_blocklist.domains`` for a
    blocklist, ``xai.model`` for the Grok model. Booleans are coerced so
    ``misaka web set keyless_rescue off`` does not store the string ``"off"``, which
    :func:`config_flag` would read as truthy, and list-valued keys are split on commas so
    a blocklist of three domains is three rules rather than one long non-matching one.
    """
    parts = dotted_key.split(".")
    if any(not part for part in parts):
        raise ValueError("config keys must not be empty")
    if len(parts) > 2:
        raise ValueError("a config key nests at most one level (section.key)")
    if len(parts) == 2 and parts[0] not in _NESTED_KEYS:
        raise ValueError(f"{parts[0]!r} is not a nested section; try env.<VAR> or provider_tier.<vendor>")
    if len(parts) == 1 and parts[0] in _NESTED_KEYS:
        raise ValueError(f"{parts[0]!r} is a section; set {parts[0]}.<name> instead")
    if len(parts) == 1 and parts[0] not in _SCALAR_KEYS | _BOOL_KEYS | _LIST_KEYS:
        # A misspelt top-level key would otherwise be written and silently never read.
        known = sorted(_SCALAR_KEYS | _BOOL_KEYS | _LIST_KEYS | _NESTED_KEYS)
        raise ValueError(f"unknown key {parts[0]!r}; known keys: {known}")
    if parts[0] == "provider_tier" and value.strip().lower() not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}, not {value!r}")

    if len(parts) == 1:
        doc[parts[0]] = _coerce(parts[0], value)
        if parts[0] in ("backend", "search_backend", "extract_backend"):
            # Hermes' picker pops a stale tier pin when the chosen row has no tier of its
            # own (``hermes_cli/tools_config.py:4733-4737``). Same reason here: a leftover
            # ``provider_tier.<vendor>: free`` from an earlier choice silently decides
            # where the keyless ring starts, and nothing in the config names it as the
            # cause. Re-pin with ``misaka web set provider_tier.<vendor> free``.
            tiers = doc.get("provider_tier")
            if isinstance(tiers, dict):
                tiers.pop(str(doc[parts[0]]), None)
                if not tiers:
                    doc.pop("provider_tier", None)
            if current_scope().profile_dir is not None and doc[parts[0]]:
                # Override an inherited pin too, without copying shared credentials.
                doc.setdefault("provider_tier", {})[str(doc[parts[0]])] = "auto"
    else:
        section = doc.get(parts[0])
        if not isinstance(section, dict):
            section = doc[parts[0]] = {}
        section[parts[1]] = _coerce_nested(parts[0], parts[1], value)


def _remove_key(doc: dict, key: str) -> None:
    parts = key.split(".")
    if len(parts) == 1:
        doc.pop(parts[0], None)
    elif len(parts) == 2 and isinstance(doc.get(parts[0]), dict):
        doc[parts[0]].pop(parts[1], None)
        if not doc[parts[0]]:
            doc.pop(parts[0], None)
    elif len(parts) > 2:
        raise ValueError("a config key nests at most one level (section.key)")


def _to_credentials(key: str) -> bool:
    section, _, name = key.partition(".")
    return section == "env" and bool(name) and is_credential_var(name)


def update_config(changes: dict[str, str], *, remove: tuple[str, ...] = ()) -> str:
    """Commit one setup transaction. A credential (``env.<KEY>``) goes to the credentials
    file; everything else to the ``web`` section of the scope in effect. Returns the
    settings file."""
    if any(_to_credentials(key) for key in (*changes, *remove)):
        from misaka.config import env

        env.write({key.partition(".")[2]: value for key, value in changes.items() if _to_credentials(key)},
                  current_scope().profile_dir,
                  remove=tuple(key.partition(".")[2] for key in remove if _to_credentials(key)))
    settings_changes = {key: value for key, value in changes.items() if not _to_credentials(key)}
    settings_removes = tuple(key for key in remove if not _to_credentials(key))
    if settings_changes or settings_removes:
        def mutate(section: dict) -> None:
            for key in settings_removes:
                _remove_key(section, key)
            for key, value in settings_changes.items():
                _set_value(section, key, value)
        _manager().updateSection("web", mutate)
    return _config_path()


def set_config(dotted_key: str, value: str) -> str:
    return update_config({dotted_key: value})


def unset_config(dotted_key: str) -> str:
    """Remove one config value. A no-op on an absent key. Returns the path."""
    return update_config({}, remove=(dotted_key,))


def provider_disabled(name: str) -> bool:
    disabled = web_config().get("disabled_providers")
    return isinstance(disabled, list) and name in disabled


def set_provider_enabled(name: str, enabled: bool) -> str:
    disabled = web_config(strict=True).get("disabled_providers") or []
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise ValueError("disabled_providers must be a list")
    names = set(disabled)
    names.discard(name) if enabled else names.add(name)

    def mutate(section: dict) -> None:
        section["disabled_providers"] = sorted(names)

    _manager().updateSection("web", mutate)
    return _config_path()


def credential_status() -> list[tuple[str, bool, str]]:
    """``(var, is_set, source)`` for every vendor credential, without revealing the value.

    ``source`` is ``env`` when the process environment supplies it (an export wins over the
    file) or the credentials file when it does, so a user can see why an export is or is
    not taking effect.
    """
    rows: list[tuple[str, bool, str]] = []
    file_env = web_config().get("env")
    file_env = file_env if isinstance(file_env, dict) else {}
    environment = current_scope().environment
    environment = os.environ if environment is None else environment
    for name in sorted(provider_variables()):
        if name in environment:
            rows.append((name, bool(environment[name].strip()), "env"))
        elif name in file_env:
            profile = current_scope().profile_dir
            if is_credential_var(name):
                own = name in _read_credentials(profile) if profile else False
                source = _credentials_path(profile if own else None)
            else:
                source = _config_path()
            rows.append((name, bool(str(file_env.get(name) or "").strip()), home.display(source)))
        else:
            rows.append((name, False, ""))
    return rows


def redact_values(value: Any) -> Any:
    """Copy response values with secrets removed before JSON escaping or truncation."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {redact_secrets(key) if isinstance(key, str) else key: redact_values(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    return value
