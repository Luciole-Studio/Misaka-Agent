"""MISAKA product-side configuration.

Constants are the configuration; a config file can come later if ever needed.
MISAKA_* environment variables override the defaults.
"""
import os
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])

CFG = {
    "db": os.environ.get("MISAKA_DB", "~/.misaka/board.db"),
    "messages_db": os.environ.get("MISAKA_MESSAGES", "~/.misaka/messages.db"),
    # Hand-maintained list of recognised ally CLIs. This file is the single source of
    # truth (seeded on first run; deliberately no environment-variable override).
    "allies": "~/.misaka/allies.json",
    "net_sock": os.environ.get("MISAKA_NET_SOCK", "~/.misaka/net.sock"),
    "net_snapshot": os.environ.get("MISAKA_NET_SNAPSHOT", "~/.misaka/net.json"),
    "provider": os.environ.get("MISAKA_PROVIDER", "sub2api-claude"),
    "default_model": os.environ.get("MISAKA_MODEL", "claude-sonnet-5"),
    # Each new card records its workspace at creation time.
    "workspaces_root": os.path.expanduser(os.environ.get("MISAKA_WS", "~/Documents/Misaka/workspaces")),
    "tasks_root": os.path.expanduser(os.environ.get("MISAKA_TASKS", "~/.misaka/tasks")),
    # As in pi: personalities are user data and live next to skills/MCP under
    # ~/.misaka/profiles/<role>/, never in the source tree.
    "profiles_root": os.path.expanduser("~/.misaka/profiles/sisters"),
    "roles_root": os.path.expanduser("~/.misaka/profiles"),
    "judge_timeout": int(os.environ.get("MISAKA_JUDGE_TIMEOUT", "600")),
    "hooks_dir": os.path.join(REPO, "hooks"),
    "token_cap": int(os.environ.get("MISAKA_TOKEN_CAP", "0")),
    # Context engine: lcm = lossless compaction (originals kept in lcm.db and
    # retrievable); native = the engine's built-in one-shot summary.  Any failure
    # inside LCM falls back to native automatically (fail-open); this switch is
    # the explicit escape hatch.
    "context_engine": os.environ.get("MISAKA_CONTEXT_ENGINE", "lcm"),
    "lcm_db": os.environ.get("MISAKA_LCM_DB", "~/.misaka/lcm.db"),
    "lcm_summary_provider": os.environ.get("MISAKA_LCM_SUMMARY_PROVIDER", ""),
    "lcm_summary_model": os.environ.get("MISAKA_LCM_SUMMARY_MODEL", ""),
    "lcm_summary_fallback_models": os.environ.get("MISAKA_LCM_SUMMARY_FALLBACK_MODELS", ""),
    "lcm_summary_timeout": float(os.environ.get("MISAKA_LCM_SUMMARY_TIMEOUT", "60")),
    "lcm_retrieval_mode": os.environ.get("MISAKA_LCM_RETRIEVAL_MODE", "fts"),
    "lcm_embedding_model": os.environ.get("MISAKA_LCM_EMBEDDING_MODEL", ""),
}


def sisters():
    """Return the registered Sister IDs (subdirectory names under ~/.misaka/profiles/sisters/)."""
    root = CFG["profiles_root"]
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
