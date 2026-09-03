"""``MISAKA_LCM_*`` (the shipped user contract) -> upstream's ``LCM_*`` names.

Upstream owns 115 environment variables and its own documentation for them, so the port
keeps those names verbatim: anything a user learned from hermes-lcm works here unchanged,
including every knob nothing below mentions. misaka's own ``MISAKA_LCM_*`` names came
first and keep working as *aliases* with lower precedence -- an explicit ``LCM_*`` always
wins, because that is the name upstream's docs, its ``lcm status`` provenance table, and
the next resync all speak.

Six of misaka's seven map cleanly and are below. The seventh does not, and saying so is
the point of naming it: ``MISAKA_LCM_SUMMARY_PROVIDER`` has no upstream counterpart
(upstream routes a provider through the model string and its host's registry; misaka
names it separately, and ``host/llm.py`` reads it there).

The two embedding names arrive as a pair because the pre-port implementation used them
as one: ``MISAKA_LCM_RETRIEVAL_MODE=hybrid`` was the whole on-switch, and
``MISAKA_LCM_EMBEDDING_MODEL`` was always a FastEmbed model because FastEmbed was the
only provider it had. So the pair maps onto ``LCM_EMBEDDINGS_ENABLED`` plus a
``fastembed`` provider, and both halves of the default (``fts``, empty) map onto
nothing -- embeddings stay off, which is upstream's default too. Reaching voyage or
ollama is what the upstream names are for.

One alias has no ``MISAKA_LCM_*`` name behind it and is here anyway:
``LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH``. Upstream leaves it empty and resolves the
directory from ``hermes_home``, which misaka does not have -- so an empty value sends
externalized payloads to ``~/.hermes/lcm-large-outputs``, a directory that on this
machine belongs to a *different* install's data. Worse, it is not even consistent:
``MessageStore`` falls back to the database's own parent while the engine passes the
empty string through, so the writer and the reader resolve different directories and
``lcm_expand(externalized_ref=...)`` answers "not found" for a payload that exists. A
configured path outranks ``hermes_home`` everywhere it is read, so naming it once here
settles both.

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


def misaka_database_path() -> str:
    """The database misaka's own settings name, before any upstream override."""
    return os.path.expanduser(str(CFG.get("lcm_db") or "~/.misaka/lcm.db"))


def database_path() -> str:
    """The database in use: upstream's own environment name first, then misaka's."""
    return os.environ.get("LCM_DATABASE_PATH") or misaka_database_path()

# Externalized payloads live beside the database they belong to, under the name upstream
# gives the directory. Read before the aliases are applied, `database_path()` is already
# the database this process will open.
LARGE_OUTPUT_DIRNAME = "lcm-large-outputs"


def _aliases() -> dict[str, str]:
    """Upstream names misaka's own settings supply when the upstream name is unset."""
    timeout_s = float(CFG.get("lcm_summary_timeout") or 60)
    values = {
        "LCM_DATABASE_PATH": misaka_database_path(),
        "LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH": os.path.join(
            os.path.dirname(database_path()), LARGE_OUTPUT_DIRNAME
        ),
        "LCM_SUMMARY_MODEL": str(CFG.get("lcm_summary_model") or ""),
        "LCM_SUMMARY_FALLBACK_MODELS": str(CFG.get("lcm_summary_fallback_models") or ""),
        "LCM_SUMMARY_TIMEOUT_MS": str(int(timeout_s * 1000)),
    }
    if str(CFG.get("lcm_retrieval_mode") or "fts").strip().lower() == "hybrid":
        values["LCM_EMBEDDINGS_ENABLED"] = "true"
    # The provider name travels with the model name and only with it: on its own it
    # would answer "fastembed" for a `LCM_EMBEDDING_MODEL` the user set to a Voyage
    # model, turning upstream's "set both" error into a wrong provider.
    model = str(CFG.get("lcm_embedding_model") or "").strip()
    if model and not os.environ.get("LCM_EMBEDDING_MODEL"):
        values["LCM_EMBEDDING_MODEL"] = model
        values["LCM_EMBEDDING_PROVIDER"] = "fastembed"
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
