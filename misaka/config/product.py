"""MISAKA product-side configuration.

Defaults are the engine's own (``~/.misaka/agent/settings.json``: ``defaultProvider`` and
``defaultModel``, what ``/model`` saves) with a builtin provider behind them, so a fresh install
runs; Last Order's model is her profile's pinned one (``profiles/last_order/config.json``, the
same ``{"model": ...}`` a Sister carries), else the default. ``MISAKA_*`` environment variables
override everything. Model settings are read when a session is assembled because ``/model`` can
change them in a long-lived process. Numbers are parsed here once, so a bad value names itself.
"""
import json
import os

from misaka.config.engine import get_agent_dir

# The one path in ~/.misaka that had no override, which is also the one a test run
# reaches by accident: every sibling below is redirectable, so this is too.
ROLES_ROOT = os.path.expanduser(os.environ.get("MISAKA_PROFILES") or "~/.misaka/profiles")


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _number(name, default, cast):
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a number") from None


def _models():
    """Return one provider/model pair, then Last Order's model, from the live settings."""
    settings = _json(os.path.join(get_agent_dir(), "settings.json"))
    env_provider = str(os.environ.get("MISAKA_PROVIDER") or "").strip()
    env_model = str(os.environ.get("MISAKA_MODEL") or "").strip()
    if bool(env_provider) != bool(env_model):
        raise SystemExit("MISAKA_PROVIDER and MISAKA_MODEL must be set together")
    saved_provider = str(settings.get("defaultProvider") or "").strip()
    saved_model = str(settings.get("defaultModel") or "").strip()
    provider, model = (
        (env_provider, env_model)
        if env_provider
        else (saved_provider, saved_model)
        if saved_provider and saved_model
        else ("anthropic", "claude-sonnet-4-5")
    )
    pinned = str(
        _json(os.path.join(ROLES_ROOT, "last_order", "config.json")).get("model") or ""
    ).strip()
    lo_model = str(os.environ.get("MISAKA_LO_MODEL") or "").strip() or pinned or model
    return provider, model, lo_model

CFG = {
    "db": os.environ.get("MISAKA_DB", "~/.misaka/board.db"),
    "messages_db": os.environ.get("MISAKA_MESSAGES", "~/.misaka/messages.db"),
    # Hand-maintained list of recognised ally CLIs. This file is the single source of
    # truth (seeded on first run; deliberately no environment-variable override).
    "allies": "~/.misaka/allies.json",
    # Web search: which backend, whether the no-key vendor ring may serve, and the
    # vendor credentials -- one file rather than an environment variable per vendor,
    # because a sub-agent child inherits a scrubbed environment but reads the same file.
    "web_config": os.environ.get("MISAKA_WEB_CONFIG", "~/.misaka/web.json"),
    # Extracted page text, kept for a TTL so a re-read of the same URL costs nothing.
    # Addressed here rather than expanded from ``~`` at the call site so a test run
    # cannot reach the developer's own cache -- the same reason web_config is here.
    "web_cache": os.environ.get("MISAKA_WEB_CACHE", "~/.misaka/cache/web"),
    # Rendered Office markdown, keyed by (path, size, mtime): a continuation read of a
    # 50-sheet workbook must not re-parse it. Addressed here for the same reason
    # ``web_cache`` is -- a test run must not reach the developer's own cache.
    "office_cache": os.environ.get("MISAKA_OFFICE_CACHE", "~/.misaka/cache/office"),
    # What the model actually asked the office tool to write, one JSON per call. A
    # deliverable that came out wrong is otherwise unexplainable: the ops that produced it
    # are gone the moment the call returns, leaving only the file and a receipt saying it
    # worked.
    "office_intent": os.environ.get("MISAKA_OFFICE_INTENT", "~/.misaka/office_intent"),
    "net_sock": os.environ.get("MISAKA_NET_SOCK", "~/.misaka/net.sock"),
    "net_snapshot": os.environ.get("MISAKA_NET_SNAPSHOT", "~/.misaka/net.json"),
    "tasks_root": os.path.realpath(os.path.expanduser(os.environ.get("MISAKA_TASKS", "~/.misaka/tasks"))),
    # As in pi: personalities are user data and live next to skills/MCP under
    # ~/.misaka/profiles/<role>/, never in the source tree.
    "profiles_root": os.path.join(ROLES_ROOT, "sisters"),
    "roles_root": ROLES_ROOT,
    "judge_timeout": _number("MISAKA_JUDGE_TIMEOUT", "600", int),
    "token_cap": _number("MISAKA_TOKEN_CAP", "0", int),
    # A node's plan waits for the user's go-ahead (recorded by its Last Order once they agree in
    # conversation) before any card is created. Off for unattended runs and tests.
    "research_plan_approval": os.environ.get("MISAKA_RESEARCH_PLAN_APPROVAL", "1").strip().lower()
    not in {"0", "false", "no", "off", ""},
    "lcm_db": os.environ.get("MISAKA_LCM_DB", "~/.misaka/lcm.db"),
}


def current_config():
    """Return product configuration with the current saved provider/model pair."""
    provider, model, lo_model = _models()
    return {**CFG, "provider": provider, "default_model": model, "lo_model": lo_model}


def sisters():
    """Return the registered Sister IDs (subdirectory names under ~/.misaka/profiles/sisters/)."""
    root = CFG["profiles_root"]
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
