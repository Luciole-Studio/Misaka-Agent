"""Shared tool exports for the coding-agent runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from misaka.agent.types import AgentTool
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.bash import (
    BashOperations,
    BashSpawnContext,
    BashSpawnHook,
    BashToolDetails,
    BashToolInput,
    BashToolOptions,
    create_bash_tool,
    create_bash_tool_definition,
    createBashTool,
    createBashToolDefinition,
    createLocalBashOperations,
)
from misaka.core.tools.edit import (
    EditOperations,
    EditToolDetails,
    EditToolInput,
    EditToolOptions,
    create_edit_tool,
    create_edit_tool_definition,
    createEditTool,
    createEditToolDefinition,
)
from misaka.core.tools.file_mutation_queue import (
    with_file_mutation_queue,
    withFileMutationQueue,
)
from misaka.core.tools.find import (
    FindOperations,
    FindToolDetails,
    FindToolInput,
    FindToolOptions,
    create_find_tool,
    create_find_tool_definition,
    createFindTool,
    createFindToolDefinition,
)
from misaka.core.tools.grep import (
    GrepOperations,
    GrepToolDetails,
    GrepToolInput,
    GrepToolOptions,
    create_grep_tool,
    create_grep_tool_definition,
    createGrepTool,
    createGrepToolDefinition,
)
from misaka.core.tools.ls import (
    LsOperations,
    LsToolDetails,
    LsToolInput,
    LsToolOptions,
    create_ls_tool,
    create_ls_tool_definition,
    createLsTool,
    createLsToolDefinition,
)
from misaka.core.tools.read import (
    ReadOperations,
    ReadToolDetails,
    ReadToolInput,
    ReadToolOptions,
    create_read_tool,
    create_read_tool_definition,
    createReadTool,
    createReadToolDefinition,
)
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationResult,
    formatSize,
    truncateHead,
    truncateLine,
    truncateTail,
)
from misaka.core.tools.write import (
    WriteOperations,
    WriteToolInput,
    WriteToolOptions,
    create_write_tool,
    create_write_tool_definition,
    createWriteTool,
    createWriteToolDefinition,
)

type Tool = AgentTool
type ToolDef = ToolDefinition[Any, Any]
type ToolName = Literal["read", "bash", "edit", "write", "grep", "find", "ls"]

class ToolsOptions(TypedDict, total=False):
    read: ReadToolOptions | Mapping[str, Any]
    bash: BashToolOptions | Mapping[str, Any]
    write: WriteToolOptions | Mapping[str, Any]
    edit: EditToolOptions | Mapping[str, Any]
    grep: GrepToolOptions | Mapping[str, Any]
    find: FindToolOptions | Mapping[str, Any]
    ls: LsToolOptions | Mapping[str, Any]


def _get_tool_options(options: ToolsOptions | Mapping[str, Any] | None, key: ToolName) -> Any:
    if options is None:
        return None
    return options.get(key)


def create_all_tool_definitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> dict[ToolName, ToolDef]:
    return {
        "read": create_read_tool_definition(cwd, _get_tool_options(options, "read")),
        "bash": create_bash_tool_definition(cwd, _get_tool_options(options, "bash")),
        "edit": create_edit_tool_definition(cwd, _get_tool_options(options, "edit")),
        "write": create_write_tool_definition(cwd, _get_tool_options(options, "write")),
        "grep": create_grep_tool_definition(cwd, _get_tool_options(options, "grep")),
        "find": create_find_tool_definition(cwd, _get_tool_options(options, "find")),
        "ls": create_ls_tool_definition(cwd, _get_tool_options(options, "ls")),
    }


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "BashOperations",
    "BashSpawnContext",
    "BashSpawnHook",
    "BashToolDetails",
    "BashToolInput",
    "BashToolOptions",
    "EditOperations",
    "EditToolDetails",
    "EditToolInput",
    "EditToolOptions",
    "FindOperations",
    "FindToolDetails",
    "FindToolInput",
    "FindToolOptions",
    "GrepOperations",
    "GrepToolDetails",
    "GrepToolInput",
    "GrepToolOptions",
    "LsOperations",
    "LsToolDetails",
    "LsToolInput",
    "LsToolOptions",
    "ReadOperations",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    "Tool",
    "ToolDef",
    "ToolName",
    "ToolsOptions",
    "TruncationOptions",
    "TruncationResult",
    "WriteOperations",
    "WriteToolInput",
    "WriteToolOptions",
    "createBashTool",
    "createBashToolDefinition",
    "createEditTool",
    "createEditToolDefinition",
    "createFindTool",
    "createFindToolDefinition",
    "createGrepTool",
    "createGrepToolDefinition",
    "createLocalBashOperations",
    "createLsTool",
    "createLsToolDefinition",
    "createReadTool",
    "createReadToolDefinition",
    "createWriteTool",
    "createWriteToolDefinition",
    "create_bash_tool",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_read_tool",
    "create_write_tool",
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "withFileMutationQueue",
    "with_file_mutation_queue",
    ]
