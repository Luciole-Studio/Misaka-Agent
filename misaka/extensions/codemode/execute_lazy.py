"""Loads the codemode sandbox runtime on the first script (see ``execute.py``).

Translated from pi's ``coding-agent/src/extensions/codemode/execute.lazy.ts``.
"""

from __future__ import annotations

import importlib
from types import ModuleType


async def loadCodemodeExecutor() -> ModuleType:
    return importlib.import_module("misaka.extensions.codemode.execute")


__all__ = ["loadCodemodeExecutor"]
