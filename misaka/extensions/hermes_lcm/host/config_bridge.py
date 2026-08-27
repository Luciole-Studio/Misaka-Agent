"""``MISAKA_LCM_*`` (the shipped user contract) -> upstream's ``LCM_*`` names.

Upstream owns 115 environment variables and its own documentation for them, so the port
keeps those names verbatim: anything a user learned from hermes-lcm works here unchanged,
including every knob nothing below mentions. misaka's own ``MISAKA_LCM_*`` names came
first and keep working as *aliases* with lower precedence -- an explicit ``LCM_*`` always
wins, because that is the name upstream's docs, its ``lcm status`` provenance table, and
the next resync all speak.

Four of misaka's seven map cleanly and are below. The other three do not, and saying so
is the point of listing them: ``MISAKA_LCM_SUMMARY_PROVIDER`` has no upstream
counterpart (upstream routes a provider through the model string and its host's
registry; misaka names it separately, and ``host/llm.py`` reads it there), while
``MISAKA_LCM_RETRIEVAL_MODE`` and ``MISAKA_LCM_EMBEDDING_MODEL`` belong to the
embeddings phase -- mapping them onto ``LCM_EMBEDDINGS_ENABLED`` now would switch on a
subsystem this phase has not wired or tested.

``LCMConfig.from_env`` reads ``os.environ`` directly, so the alias values are laid over
the environment for the duration of that one call and then removed again: no misaka
process, and no child it spawns, is left carrying names its own configuration already
holds.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from misaka.config import CFG

from ..vendor.config import LCMConfig
from .switch import misaka_database_path


def _aliases() -> dict[str, str]:
    """Upstream names misaka's own settings supply when the upstream name is unset."""
    timeout_s = float(CFG.get("lcm_summary_timeout") or 60)
    values = {
        "LCM_DATABASE_PATH": misaka_database_path(),
        "LCM_SUMMARY_MODEL": str(CFG.get("lcm_summary_model") or ""),
        "LCM_SUMMARY_FALLBACK_MODELS": str(CFG.get("lcm_summary_fallback_models") or ""),
        "LCM_SUMMARY_TIMEOUT_MS": str(int(timeout_s * 1000)),
    }
    return {name: value for name, value in values.items() if value}


@contextmanager
def _aliased_environment():
    """Lay the aliases over ``os.environ``, then restore it exactly."""
    applied = [
        (name, value) for name, value in _aliases().items()
        if not os.environ.get(name)
    ]
    for name, value in applied:
        os.environ[name] = value
    try:
        yield
    finally:
        for name, _ in applied:
            os.environ.pop(name, None)


def load_config() -> LCMConfig:
    """Build the upstream config for this misaka process."""
    with _aliased_environment():
        return LCMConfig.from_env()
