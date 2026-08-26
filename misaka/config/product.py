"""MISAKA product-side configuration.

Defaults are the engine's own (``~/.misaka/agent/settings.json``: ``defaultProvider`` and
``defaultModel``, what ``/model`` saves) with a builtin provider behind them, so a fresh install
runs; Last Order's model is her profile's pinned one (``profiles/last_order/config.json``, the
same ``{"model": ...}`` a Sister carries), else the default. ``MISAKA_*`` environment variables
override everything. Numbers are parsed here, once, so a bad value names itself.
"""
import json
import os

from misaka.config.engine import get_agent_dir

ROLES_ROOT = os.path.expanduser("~/.misaka/profiles")


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _number(name, default, cast):
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a number") from None


def _models():
    """``(provider, model, last_order_model)``: environment, then the saved settings, then builtin."""
    settings = _json(os.path.join(get_agent_dir(), "settings.json"))
    provider = os.environ.get("MISAKA_PROVIDER") or settings.get("defaultProvider") or "anthropic"
    model = os.environ.get("MISAKA_MODEL") or settings.get("defaultModel") or "claude-sonnet-4-5"
    pinned = _json(os.path.join(ROLES_ROOT, "last_order", "config.json")).get("model")
    return provider, model, os.environ.get("MISAKA_LO_MODEL") or pinned or model


_PROVIDER, _MODEL, _LO_MODEL = _models()

CFG = {
    "db": os.environ.get("MISAKA_DB", "~/.misaka/board.db"),
    "messages_db": os.environ.get("MISAKA_MESSAGES", "~/.misaka/messages.db"),
    # Hand-maintained list of recognised ally CLIs. This file is the single source of
    # truth (seeded on first run; deliberately no environment-variable override).
    "allies": "~/.misaka/allies.json",
    "net_sock": os.environ.get("MISAKA_NET_SOCK", "~/.misaka/net.sock"),
    "net_snapshot": os.environ.get("MISAKA_NET_SNAPSHOT", "~/.misaka/net.json"),
    "provider": _PROVIDER,
    "default_model": _MODEL,
    "lo_model": _LO_MODEL,
    "tasks_root": os.path.expanduser(os.environ.get("MISAKA_TASKS", "~/.misaka/tasks")),
    # As in pi: personalities are user data and live next to skills/MCP under
    # ~/.misaka/profiles/<role>/, never in the source tree.
    "profiles_root": os.path.join(ROLES_ROOT, "sisters"),
    "roles_root": ROLES_ROOT,
    "judge_timeout": _number("MISAKA_JUDGE_TIMEOUT", "600", int),
    "token_cap": _number("MISAKA_TOKEN_CAP", "0", int),
    # Context engine: lcm = lossless compaction (originals kept in lcm.db and
    # retrievable); native = the engine's built-in one-shot summary.  Any failure
    # inside LCM falls back to native automatically (fail-open); this switch is
    # the explicit escape hatch.
    "context_engine": os.environ.get("MISAKA_CONTEXT_ENGINE", "lcm"),
    "lcm_db": os.environ.get("MISAKA_LCM_DB", "~/.misaka/lcm.db"),
    "lcm_summary_provider": os.environ.get("MISAKA_LCM_SUMMARY_PROVIDER", ""),
    "lcm_summary_model": os.environ.get("MISAKA_LCM_SUMMARY_MODEL", ""),
    "lcm_summary_fallback_models": os.environ.get("MISAKA_LCM_SUMMARY_FALLBACK_MODELS", ""),
    "lcm_summary_timeout": _number("MISAKA_LCM_SUMMARY_TIMEOUT", "60", float),
    "lcm_retrieval_mode": os.environ.get("MISAKA_LCM_RETRIEVAL_MODE", "fts"),
    "lcm_embedding_model": os.environ.get("MISAKA_LCM_EMBEDDING_MODEL", ""),
}


def sisters():
    """Return the registered Sister IDs (subdirectory names under ~/.misaka/profiles/sisters/)."""
    root = CFG["profiles_root"]
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
