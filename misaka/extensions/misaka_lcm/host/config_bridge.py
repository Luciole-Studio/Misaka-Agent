"""Native paths/settings; original LCM_* algorithm configuration, without environment mutation."""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager

from misaka.utils.values import read_field

from ..vendor.config import LCMConfig
from . import storage

_AUXILIARY_CONFIG = contextvars.ContextVar("lcm_auxiliary_config", default=None)


def load_auxiliary_config() -> dict:
    """Read native global settings only, never Hermes or an untrusted project file.

    The auxiliary call binds this snapshot through route resolution and dispatch.
    No config file is created, migrated, or written on this read path.
    """
    import json

    from misaka.config import home

    config = _AUXILIARY_CONFIG.get()
    if config is not None:
        return config
    try:
        settings = json.loads(home.path("settings").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    raw = settings.get("auxiliary", {})
    auxiliary = dict(raw) if isinstance(raw, dict) else {}
    return {**settings, "auxiliary": auxiliary}



@contextmanager
def auxiliary_config():
    token = _AUXILIARY_CONFIG.set(load_auxiliary_config())
    try:
        yield
    finally:
        _AUXILIARY_CONFIG.reset(token)


def get_plugin_auxiliary_tasks():
    return ()


def _scoped_key_env(name):
    return (os.environ.get(name) or "").strip() if name else ""


def database_path(ctx=None) -> str:
    """LCM content belongs to the session's project, never a global home DB."""
    return str(storage.directory(storage.project(ctx)) / "lcm.db")


HERMES_COMPRESSION_DEFAULTS = {"threshold": 0.50}


def _session_host_config(ctx) -> dict:
    """The host config this session reads: settings.json over Hermes' compression defaults, with
    the session's own compaction threshold (``misaka.core.session_overrides``, a research run's
    choice) as its ``lcm.context_threshold`` -- the same channel, so upstream's precedence and its
    explicit-choice rules (no Codex autoraise over it) treat it exactly as the global setting."""
    config = load_auxiliary_config()
    compression = config.get("compression")
    if compression is None or isinstance(compression, dict):
        config = {**config, "compression": {**HERMES_COMPRESSION_DEFAULTS, **(compression or {})}}
    if ctx is None:
        return config
    from misaka.core import session_overrides

    from . import ingest
    threshold = session_overrides.value(ingest.session_id(ctx), "context_threshold")
    if threshold is None:
        return config
    lcm = config.get("lcm") if isinstance(config.get("lcm"), dict) else {}
    return {**config, "lcm": {**lcm, "context_threshold": float(threshold)}}


REFERENCE_WINDOW = 128_000
SUMMARISER_SHARE = 0.6


def scale_to_window(config, window, summariser_window=None):
    """MISAKA's default for the leaf chunk, in proportion to ``window``; an explicit
    ``LCM_LEAF_CHUNK_TOKENS`` is never replaced. Never below upstream's own size, and never above
    what the summariser model can take in one call."""
    scale = max(1.0, window / REFERENCE_WINDOW) if window > 0 else 1.0
    if "LCM_LEAF_CHUNK_TOKENS" not in os.environ:
        leaf = round(LCMConfig.leaf_chunk_tokens * scale)
        if summariser_window:
            leaf = min(leaf, int(summariser_window * SUMMARISER_SHARE))
        config.leaf_chunk_tokens = max(LCMConfig.leaf_chunk_tokens, leaf)
        config.config_sources["leaf_chunk_tokens"] = "misaka.default"


def summariser_window(ctx, session_window):
    """The window of the model that writes the summaries: ``auxiliary.compression`` when set,
    else the session's own model."""
    compression = load_auxiliary_config()["auxiliary"].get("compression")
    compression = compression if isinstance(compression, dict) else {}
    provider, model = str(compression.get("provider") or ""), str(compression.get("model") or "")
    registry = read_field(ctx, "modelRegistry")
    if not model or model.lower() == "auto" or registry is None:
        return session_window
    found = registry.find(provider, model) if provider and provider.lower() != "auto" else None
    return int(read_field(found, "contextWindow", 0) or 0) or session_window


def load_config(*, database=None, home=None, ctx=None) -> LCMConfig:
    """Keep upstream algorithm settings; the host owns all content paths."""
    from . import settings
    config = settings.apply(LCMConfig.from_env(host_config=_session_host_config(ctx)))
    if "LCM_EMBEDDING_QUERY_TIMEOUT_S" not in os.environ:
        config.embedding_query_timeout_s = 30.0
        config.config_sources["embedding_query_timeout_s"] = "misaka.default"
    if "LCM_RESERVE_TOKENS_FLOOR" not in os.environ:
        config.reserve_tokens_floor = 16_384
        config.config_sources["reserve_tokens_floor"] = "misaka.default"
    if "LCM_DYNAMIC_LEAF_CHUNK_ENABLED" not in os.environ:
        config.dynamic_leaf_chunk_enabled = True
        config.config_sources["dynamic_leaf_chunk_enabled"] = "misaka.default"
    config.database_path = str(database) if database is not None else database_path(ctx)
    directory = str(home) if home is not None else os.path.dirname(config.database_path)
    config.large_output_externalization_path = os.path.join(directory, "lcm-large-outputs")
    config.extraction_output_path = os.path.join(directory, "lcm-extractions")
    return config
