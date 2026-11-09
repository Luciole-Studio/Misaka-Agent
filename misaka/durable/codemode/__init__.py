"""Durable code mode: the ``codemode`` tool and extension of the durable Harness.

Translated from pi's ``packages/durable/src/codemode/index.ts`` (v1.3.0). Unrelated to the product's
``misaka.extensions.codemode``, which serves misaka's own sessions; both run scripts with ``misaka.codemode``.
"""

from __future__ import annotations

from .codemode import (
    CODEMODE_MODULE_VERSION,
    CODEMODE_TOOL_NAME,
    CodemodeGlobal,
    CodemodeNestedCall,
    CodemodeStoreDoc,
    CodemodeToolDetails,
    CodemodeToolInput,
    CodemodeToolOptions,
    createCodemodeExtension,
    createCodemodeTool,
)
from .describe import DEFAULT_CODEMODE_INLINE_BUDGET, CodemodeNamespace

__all__ = [
    "CODEMODE_MODULE_VERSION",
    "CODEMODE_TOOL_NAME",
    "DEFAULT_CODEMODE_INLINE_BUDGET",
    "CodemodeGlobal",
    "CodemodeNamespace",
    "CodemodeNestedCall",
    "CodemodeStoreDoc",
    "CodemodeToolDetails",
    "CodemodeToolInput",
    "CodemodeToolOptions",
    "createCodemodeExtension",
    "createCodemodeTool",
]
