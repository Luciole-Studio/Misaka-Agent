"""MISAKA product-side configuration.

Every product knob is a section of the home's ``settings.json`` (``research``, ``network``,
``mcp``, ``documents``, ``panel``, ``subagents``, ``skills`` ...), read at use so a long-lived
process sees an edit; ``setting()`` is the one reader. ``KNOBS`` lists every knob with its type:
a process entry checks them all before it starts (``validate_settings``) and refuses to run on a
value that does not fit, naming each one; an edit that breaks a knob while a process is running
is logged once and that knob falls back to its default instead of stopping the process. Models
are the engine's own (``defaultProvider``/``defaultModel``, what ``/model`` saves) with a builtin
pair behind them so a fresh install runs; Last Order's model is her role's pin
(``profiles/last_order/settings.json``), else the default. No environment variable overrides a
setting: ``MISAKA_*`` names in the environment are what a parent hands a child, never a knob.
"""
import json
import logging
import os

from misaka.config import home

logger = logging.getLogger(__name__)

# The product paths ``CFG`` answers for. Each is a row of ``home.LAYOUT`` under the same name.
_PATHS = ("db", "messages_db", "web_cache", "office_cache", "office_intent",
          "net_sock", "net_snapshot", "tasks_root", "profiles_root", "roles_root")

# The product knobs ``CFG`` answers for, by their settings.json section and key, with the default.
_KNOBS = {
    "token_cap": ("research", "token_cap", 0, int),
    "research_plan_approval": ("research", "plan_approval", True, bool),
}

# Every knob ``setting()`` is asked for, by settings.json section and key, with the type its value
# must fit (None: any JSON value). Defaults stay with the callers -- two of them are computed.
KNOBS = {
    ("documents", "ocr_langs"): str,
    ("mcp", "required_wait"): float,
    ("network", "max_concurrent_per_sister"): int,
    ("network", "max_concurrent_sisters"): int,
    ("panel", "prefix"): str,
    ("research", "beast_at"): float,
    ("research", "plan_approval"): bool,
    ("research", "token_cap"): int,
    ("skills", "copy_cap_mb"): int,
    ("subagents", "agent_list_in_messages"): bool,
    ("subagents", "auto_background_tasks"): bool,
    ("subagents", "auto_memory"): bool,
    ("subagents", "background_tasks"): bool,
    ("subagents", "builtin_agents"): bool,
    ("subagents", "coordinator_mode"): bool,
    ("subagents", "effort_level"): None,
    ("subagents", "inherit_process_group"): bool,
    ("subagents", "managed_agents_dir"): str,
    ("subagents", "max_concurrent"): int,
    ("subagents", "memory_home"): str,
    ("subagents", "memory_snapshot"): bool,
    ("subagents", "simple"): bool,
    ("subagents", "small_fast_model"): str,
    ("subagents", "task_max_output"): int,
    ("subagents", "verification_agent"): bool,
    ("tui", "esc_timeout_ms"): float,
    ("vision", "model"): str,
}

_settings_cache: tuple[str, int, dict] | None = None     # (path, mtime_ns, document)
_warned: set[tuple[str, str, str]] = set()


def _json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def settings_document() -> dict:
    """The home's ``settings.json`` as it is now (re-read when the file changes)."""
    global _settings_cache
    path = str(home.path("settings"))
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = -1
    if _settings_cache is None or _settings_cache[0] != path or _settings_cache[1] != stamp:
        _settings_cache = (path, stamp, _json(path) if stamp >= 0 else {})
    return _settings_cache[2]


class _Unfit(Exception):
    """A settings.json value that does not fit its knob's type; the message says how."""


def _fit(raw, cast):
    """``raw`` converted to ``cast`` (int, float, bool or str; None keeps it as is), or ``_Unfit``."""
    if cast is None or cast is str and isinstance(raw, str):
        return raw
    if cast is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off", ""}:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        raise _Unfit("is not a boolean")
    kind = "an integer" if cast is int else "a number" if cast is float else f"a {cast.__name__}"
    if isinstance(raw, bool):
        raise _Unfit(f"is not {kind}")
    try:
        return cast(raw)
    except (TypeError, ValueError, OverflowError):
        raise _Unfit(f"is not {kind}") from None


def setting(section: str, key: str, default, cast=None):
    """One product knob: ``settings.json[section][key]``, else ``default``. ``cast`` (int, float,
    bool or str) is applied to what the file holds. A value that does not fit was refused at
    process start (``validate_settings``); one edited in since is logged once and read as
    ``default``, so a running daemon or TUI is never stopped by an edit."""
    block = settings_document().get(section)
    if not isinstance(block, dict) or key not in block:
        return default
    raw = block[key]
    try:
        return _fit(raw, cast)
    except _Unfit as error:
        if (section, key, repr(raw)) not in _warned:
            _warned.add((section, key, repr(raw)))
            logger.warning("settings.json: %s.%s=%r %s; using the default %r", section, key, raw, error, default)
        return default


def validate_settings() -> list[str]:
    """One message per knob in ``KNOBS`` whose value in settings.json does not fit its type."""
    document = settings_document()
    errors = []
    for (section, key), cast in KNOBS.items():
        block = document.get(section)
        if not isinstance(block, dict) or key not in block:
            continue
        try:
            value = _fit(block[key], cast)
        except _Unfit as error:
            errors.append(f"{section}.{key}={block[key]!r} {error}")
            continue
        if (section, key) in NOT_NEGATIVE and value < 0:
            errors.append(f"{section}.{key}={block[key]!r} is negative (0 is none)")
    return errors


# A negative cap stopped every research run at its start. The settings schema says so too (`minimum: 0`).
NOT_NEGATIVE = {("research", "token_cap")}


def _models():
    """Return one provider/model pair, then Last Order's model, from the live settings."""
    from misaka.config import profiles

    settings = settings_document()
    provider = str(settings.get("defaultProvider") or "").strip()
    model = str(settings.get("defaultModel") or "").strip()
    if not (provider and model):
        provider, model = "anthropic", "claude-sonnet-4-5"
    lo_model = profiles.pinned_model(str(home.path("roles_root") / "last_order")) or model
    return provider, model, lo_model


class _Config(dict):
    """Product settings, plus the product's paths, both resolved at each lookup.

    Nothing is stored: ``CFG["db"]`` asks ``home`` every time, ``CFG["token_cap"]`` asks
    ``settings.json`` every time, so a process pointed at another home (``MISAKA_HOME``) or a
    user who edited the file sees it at once. Storing one -- ``monkeypatch.setitem`` in a test --
    overrides that lookup until the key is deleted again. ``CFG.get(key)`` does not resolve
    (a dict's ``get`` never asks ``__missing__``): index with ``CFG[key]``.
    """

    def __missing__(self, key):
        if key in _PATHS:
            return str(home.path(key))
        if key in _KNOBS:
            return setting(*_KNOBS[key])
        raise KeyError(key)


CFG = _Config()


def current_config():
    """Return product configuration with the current saved provider/model pair."""
    provider, model, lo_model = _models()
    return {**{key: CFG[key] for key in (*_PATHS, *_KNOBS)}, **CFG,
            "provider": provider, "default_model": model, "lo_model": lo_model}


def sisters():
    """Return the registered Sister IDs (subdirectory names under the Sisters' profiles root)."""
    root = CFG["profiles_root"]
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
