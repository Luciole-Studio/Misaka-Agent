"""JSONL storage over the local file system.

Translated from pi's ``packages/durable/src/storage/jsonl/node.ts``.
"""

from __future__ import annotations

import os
from typing import Any

from misaka.chord.context import Context

from ...env.node import NodeExecutionEnv
from .storage import JsonlStorage


async def openNodeJsonlStorage(directory: str, context: Context, options: dict[str, Any] | None = None) -> JsonlStorage:
    """Open or create a JSONL storage directory using the local filesystem."""
    return await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": os.getcwd()}), context, options)


__all__ = ["JsonlStorage", "openNodeJsonlStorage"]
