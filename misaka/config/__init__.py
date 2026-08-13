"""config 包：engine 机关＋产品 CFG＋角色路径＋迁移。

旧 `from misaka.config import X` 全部照常——本 __init__ 把两半的公有名重导出。
"""
from misaka.config import engine as _engine
from misaka.config.engine import *  # noqa: F401,F403
from misaka.config.product import CFG, REPO, sisters  # noqa: F401

import sys as _sys

_self = _sys.modules[__name__]
for _k in dir(_engine):          # __all__ 外的 snake_case 原名（get_agent_dir 等）也要可 import
    if not _k.startswith("_") and not hasattr(_self, _k):
        setattr(_self, _k, getattr(_engine, _k))
del _sys, _self, _k
