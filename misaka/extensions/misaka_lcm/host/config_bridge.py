"""Native paths/settings; original LCM_* algorithm configuration, without environment mutation."""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager

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
        settings = json.loads(home.path("settings").read_text(encoding="utf-8"))
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
    # The pinned LCM plugin registers no auxiliary-task defaults. MISAKA has no
    # Hermes plugin-discovery registry; do not discover another install's plugins.
    return ()


def _scoped_key_env(name):
    # Native workers already inherit their role's scrubbed environment. Hermes'
    # profile-secret store is not part of this host.
    return (os.environ.get(name) or "").strip() if name else ""


def database_path(ctx=None) -> str:
    """LCM content belongs to the session's project, never a global home DB."""
    return str(storage.directory(storage.project(ctx)) / "lcm.db")


def load_config(*, database=None, home=None, ctx=None) -> LCMConfig:
    """Keep upstream algorithm settings; the host owns all content paths."""
    from . import settings
    config = settings.apply(LCMConfig.from_env(host_config=load_auxiliary_config()))
    if "LCM_EMBEDDING_QUERY_TIMEOUT_S" not in os.environ:
        # Upstream's 3 s deadline also bounds lcm_grep's full-text arm (it interrupts the
        # SQLite query through a progress handler), and a research project's lcm.db runs to
        # tens of megabytes, where that arm regularly overran it and was dropped in silence
        # (2026-09-18, B32). The knob is upstream's own; only misaka's default differs.
        config.embedding_query_timeout_s = 30.0
        config.config_sources["embedding_query_timeout_s"] = "misaka.default"
    config.database_path = str(database) if database is not None else database_path(ctx)
    directory = str(home) if home is not None else os.path.dirname(config.database_path)
    config.large_output_externalization_path = os.path.join(directory, "lcm-large-outputs")
    config.extraction_output_path = os.path.join(directory, "lcm-extractions")
    return config
