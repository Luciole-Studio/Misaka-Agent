"""pi's ``packages/codemode``: a sandbox that runs model-written JavaScript against the session's
tools, and the TypeScript declarations scripts are shown.

Translated from pi's ``packages/codemode/src/index.ts``.
"""

from misaka.codemode.declarations import (
    DEFAULT_INPUT_SCHEMA_MAX_CHARS,
    RenderDeclarationsOptions,
    renderDeclarations,
    renderToolOutputType,
    renderToolSample,
    renderToolSignature,
    schemaToType,
)
from misaka.codemode.identifier import toCodemodeIdentifier
from misaka.codemode.runtime.execution import CodemodeSandboxBase
from misaka.codemode.runtime.host import CodemodeSandbox
from misaka.codemode.runtime.prelude_source import (
    MAX_OUTPUT_CHARS,
    MAX_OUTPUT_ITEMS,
    MAX_STORE_TOTAL_CHARS,
    MAX_STORE_VALUE_CHARS,
)
from misaka.codemode.source import (
    CODEMODE_OPTIONS_PREFIX,
    CODEMODE_SOURCE_GRAMMAR,
    CodemodeSourceError,
    CodemodeSourceOptions,
    ParsedCodemodeSource,
    parseCodemodeSource,
)
from misaka.codemode.types import (
    CodemodeCall,
    CodemodeCallStatus,
    CodemodeDeclaredGlobal,
    CodemodeDeclaredTool,
    CodemodeDuring,
    CodemodeError,
    CodemodeErrorKind,
    CodemodeExecuteOptions,
    CodemodeHostCall,
    CodemodeJsonSchema,
    CodemodeOutcome,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeRun,
    CodemodeRunOptions,
    CodemodeSandboxBaseOptions,
    CodemodeSandboxOptions,
    CodemodeStoreWrites,
    CodemodeTool,
    CodemodeToolContext,
)
from misaka.codemode.wasm import CodemodeWasmModule, loadQuickJSWasm

__all__ = [
    "CODEMODE_OPTIONS_PREFIX",
    "CODEMODE_SOURCE_GRAMMAR",
    "DEFAULT_INPUT_SCHEMA_MAX_CHARS",
    "MAX_OUTPUT_CHARS",
    "MAX_OUTPUT_ITEMS",
    "MAX_STORE_TOTAL_CHARS",
    "MAX_STORE_VALUE_CHARS",
    "CodemodeCall",
    "CodemodeCallStatus",
    "CodemodeDeclaredGlobal",
    "CodemodeDeclaredTool",
    "CodemodeDuring",
    "CodemodeError",
    "CodemodeErrorKind",
    "CodemodeExecuteOptions",
    "CodemodeHostCall",
    "CodemodeJsonSchema",
    "CodemodeOutcome",
    "CodemodeOutputItem",
    "CodemodeResult",
    "CodemodeRun",
    "CodemodeRunOptions",
    "CodemodeSandbox",
    "CodemodeSandboxBase",
    "CodemodeSandboxBaseOptions",
    "CodemodeSandboxOptions",
    "CodemodeSourceError",
    "CodemodeSourceOptions",
    "CodemodeStoreWrites",
    "CodemodeTool",
    "CodemodeToolContext",
    "CodemodeWasmModule",
    "ParsedCodemodeSource",
    "RenderDeclarationsOptions",
    "loadQuickJSWasm",
    "parseCodemodeSource",
    "renderDeclarations",
    "renderToolOutputType",
    "renderToolSample",
    "renderToolSignature",
    "schemaToType",
    "toCodemodeIdentifier",
]
