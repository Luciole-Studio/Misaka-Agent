"""MISAKA product-side configuration.

Every product knob is a section of the home's ``settings.json`` (``research``, ``network``,
``mcp``, ``documents``, ``panel``, ``subagents``, ``skills`` ...), read at use so a long-lived
process sees an edit; ``setting()`` is the one reader and names a bad value by its key. Models
are the engine's own (``defaultProvider``/``defaultModel``, what ``/model`` saves) with a builtin
pair behind them so a fresh install runs; Last Order's model is her role's pin
(``profiles/last_order/settings.json``), else the default. No environment variable overrides a
setting: ``MISAKA_*`` names in the environment are what a parent hands a child, never a knob.
"""
import json
import os

from misaka.config import home

# The product paths ``CFG`` answers for. Each is a row of ``home.LAYOUT`` under the same name.
_PATHS = ("db", "messages_db", "web_cache", "office_cache", "office_intent",
          "net_sock", "net_snapshot", "tasks_root", "profiles_root", "roles_root")

# The product knobs ``CFG`` answers for, by their settings.json section and key, with the default.
_KNOBS = {
    "token_cap": ("research", "token_cap", 0, int),
    "research_plan_approval": ("research", "plan_approval", True, bool),
}

_settings_cache: tuple[str, int, dict] | None = None     # (path, mtime_ns, document)


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
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


def setting(section: str, key: str, default, cast=None):
    """One product knob: ``settings.json[section][key]``, else ``default``. ``cast`` (int, float,
    bool or str) is applied to what the file holds; a value that does not fit refuses to start
    the process rather than being silently ignored."""
    block = settings_document().get(section)
    if not isinstance(block, dict) or key not in block:
        return default
    raw = block[key]
    if cast is None or cast is str and isinstance(raw, str):
        return raw
    if cast is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off", ""}:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        raise SystemExit(f"settings.json: {section}.{key}={raw!r} is not a boolean")
    if isinstance(raw, bool):
        raise SystemExit(f"settings.json: {section}.{key}={raw!r} is not a {cast.__name__}")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"settings.json: {section}.{key}={raw!r} is not a {cast.__name__}") from None


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
