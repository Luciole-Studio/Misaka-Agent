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
from misaka.core.tools.powershell import (
    PowerShellOperations,
    PowerShellSpawnContext,
    PowerShellSpawnHook,
    PowerShellToolDetails,
    PowerShellToolInput,
    PowerShellToolOptions,
    create_powershell_tool,
    create_powershell_tool_definition,
    createLocalPowerShellOperations,
    createPowerShellTool,
    createPowerShellToolDefinition,
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
type ToolName = Literal[
    "read", "bash", "powershell", "edit", "write", "grep", "find", "ls"
]
all_tool_names: set[ToolName] = {
    "read", "bash", "powershell", "edit", "write", "grep", "find", "ls"
}
allToolNames = all_tool_names

class ToolsOptions(TypedDict, total=False):
    read: ReadToolOptions | Mapping[str, Any]
    bash: BashToolOptions | Mapping[str, Any]
    powershell: PowerShellToolOptions | Mapping[str, Any]
    write: WriteToolOptions | Mapping[str, Any]
    edit: EditToolOptions | Mapping[str, Any]
    grep: GrepToolOptions | Mapping[str, Any]
    find: FindToolOptions | Mapping[str, Any]
    ls: LsToolOptions | Mapping[str, Any]


def _get_tool_options(options: ToolsOptions | Mapping[str, Any] | None, key: ToolName) -> Any:
    if options is None:
        return None
    return options.get(key)


def create_tool_definition(
    tool_name: ToolName,
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> ToolDef:
    match tool_name:
        case "read":
            return create_read_tool_definition(cwd, _get_tool_options(options, "read"))
        case "bash":
            return create_bash_tool_definition(cwd, _get_tool_options(options, "bash"))
        case "powershell":
            return create_powershell_tool_definition(
                cwd, _get_tool_options(options, "powershell")
            )
        case "edit":
            return create_edit_tool_definition(cwd, _get_tool_options(options, "edit"))
        case "write":
            return create_write_tool_definition(
                cwd, _get_tool_options(options, "write")
            )
        case "grep":
            return create_grep_tool_definition(cwd, _get_tool_options(options, "grep"))
        case "find":
            return create_find_tool_definition(cwd, _get_tool_options(options, "find"))
        case "ls":
            return create_ls_tool_definition(cwd, _get_tool_options(options, "ls"))
        case _:
            raise RuntimeError(f"Unknown tool name: {tool_name}")


def create_tool(
    tool_name: ToolName,
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> Tool:
    match tool_name:
        case "read":
            return create_read_tool(cwd, _get_tool_options(options, "read"))
        case "bash":
            return create_bash_tool(cwd, _get_tool_options(options, "bash"))
        case "powershell":
            return create_powershell_tool(cwd, _get_tool_options(options, "powershell"))
        case "edit":
            return create_edit_tool(cwd, _get_tool_options(options, "edit"))
        case "write":
            return create_write_tool(cwd, _get_tool_options(options, "write"))
        case "grep":
            return create_grep_tool(cwd, _get_tool_options(options, "grep"))
        case "find":
            return create_find_tool(cwd, _get_tool_options(options, "find"))
        case "ls":
            return create_ls_tool(cwd, _get_tool_options(options, "ls"))
        case _:
            raise RuntimeError(f"Unknown tool name: {tool_name}")


def create_coding_tool_definitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[ToolDef]:
    return [
        create_read_tool_definition(cwd, _get_tool_options(options, "read")),
        create_bash_tool_definition(cwd, _get_tool_options(options, "bash")),
        create_edit_tool_definition(cwd, _get_tool_options(options, "edit")),
        create_write_tool_definition(cwd, _get_tool_options(options, "write")),
    ]


def create_read_only_tool_definitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[ToolDef]:
    return [
        create_read_tool_definition(cwd, _get_tool_options(options, "read")),
        create_grep_tool_definition(cwd, _get_tool_options(options, "grep")),
        create_find_tool_definition(cwd, _get_tool_options(options, "find")),
        create_ls_tool_definition(cwd, _get_tool_options(options, "ls")),
    ]


def create_all_tool_definitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> dict[ToolName, ToolDef]:
    return {
        "read": create_read_tool_definition(cwd, _get_tool_options(options, "read")),
        "bash": create_bash_tool_definition(cwd, _get_tool_options(options, "bash")),
        "powershell": create_powershell_tool_definition(
            cwd, _get_tool_options(options, "powershell")
        ),
        "edit": create_edit_tool_definition(cwd, _get_tool_options(options, "edit")),
        "write": create_write_tool_definition(cwd, _get_tool_options(options, "write")),
        "grep": create_grep_tool_definition(cwd, _get_tool_options(options, "grep")),
        "find": create_find_tool_definition(cwd, _get_tool_options(options, "find")),
        "ls": create_ls_tool_definition(cwd, _get_tool_options(options, "ls")),
    }


def create_coding_tools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[Tool]:
    return [
        create_read_tool(cwd, _get_tool_options(options, "read")),
        create_bash_tool(cwd, _get_tool_options(options, "bash")),
        create_edit_tool(cwd, _get_tool_options(options, "edit")),
        create_write_tool(cwd, _get_tool_options(options, "write")),
    ]


def create_read_only_tools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[Tool]:
    return [
        create_read_tool(cwd, _get_tool_options(options, "read")),
        create_grep_tool(cwd, _get_tool_options(options, "grep")),
        create_find_tool(cwd, _get_tool_options(options, "find")),
        create_ls_tool(cwd, _get_tool_options(options, "ls")),
    ]


def create_all_tools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> dict[ToolName, Tool]:
    return {
        "read": create_read_tool(cwd, _get_tool_options(options, "read")),
        "bash": create_bash_tool(cwd, _get_tool_options(options, "bash")),
        "powershell": create_powershell_tool(
            cwd, _get_tool_options(options, "powershell")
        ),
        "edit": create_edit_tool(cwd, _get_tool_options(options, "edit")),
        "write": create_write_tool(cwd, _get_tool_options(options, "write")),
        "grep": create_grep_tool(cwd, _get_tool_options(options, "grep")),
        "find": create_find_tool(cwd, _get_tool_options(options, "find")),
        "ls": create_ls_tool(cwd, _get_tool_options(options, "ls")),
    }


createToolDefinition = create_tool_definition
createTool = create_tool
createCodingToolDefinitions = create_coding_tool_definitions
createReadOnlyToolDefinitions = create_read_only_tool_definitions
createAllToolDefinitions = create_all_tool_definitions
createCodingTools = create_coding_tools
createReadOnlyTools = create_read_only_tools
createAllTools = create_all_tools


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
    "PowerShellOperations",
    "PowerShellSpawnContext",
    "PowerShellSpawnHook",
    "PowerShellToolDetails",
    "PowerShellToolInput",
    "PowerShellToolOptions",
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
    "allToolNames",
    "all_tool_names",
    "createAllToolDefinitions",
    "createAllTools",
    "createBashTool",
    "createBashToolDefinition",
    "createCodingToolDefinitions",
    "createCodingTools",
    "createEditTool",
    "createEditToolDefinition",
    "createFindTool",
    "createFindToolDefinition",
    "createGrepTool",
    "createGrepToolDefinition",
    "createLocalBashOperations",
    "createLocalPowerShellOperations",
    "createLsTool",
    "createLsToolDefinition",
    "createPowerShellTool",
    "createPowerShellToolDefinition",
    "createReadOnlyToolDefinitions",
    "createReadOnlyTools",
    "createReadTool",
    "createReadToolDefinition",
    "createTool",
    "createToolDefinition",
    "createWriteTool",
    "createWriteToolDefinition",
    "create_bash_tool",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_powershell_tool",
    "create_read_tool",
    "create_write_tool",
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "withFileMutationQueue",
    "with_file_mutation_queue",
    ]
