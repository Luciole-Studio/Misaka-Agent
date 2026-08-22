"""config package: engine settings, product CFG, role paths, and migrations.

Existing ``from misaka.config import X`` imports keep working: this __init__
re-exports the public names of both halves.
"""
from misaka.config import engine as _engine
from misaka.config.engine import *  # noqa: F401,F403
from misaka.config.product import CFG, REPO, sisters  # noqa: F401

import sys as _sys

_self = _sys.modules[__name__]
for _k in dir(_engine):          # snake_case names outside __all__ (get_agent_dir etc.) must stay importable too
    if not _k.startswith("_") and not hasattr(_self, _k):
        setattr(_self, _k, getattr(_engine, _k))
del _sys, _self, _k
