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
    createBashTool,
    createBashToolDefinition,
    createLocalBashOperations,
)
from misaka.core.tools.edit import (
    EditOperations,
    EditToolDetails,
    EditToolInput,
    EditToolOptions,
    createEditTool,
    createEditToolDefinition,
)
from misaka.core.tools.file_mutation_queue import (
    withFileMutationQueue,
)
from misaka.core.tools.find import (
    FindOperations,
    FindToolDetails,
    FindToolInput,
    FindToolOptions,
    createFindTool,
    createFindToolDefinition,
)
from misaka.core.tools.grep import (
    GrepOperations,
    GrepToolDetails,
    GrepToolInput,
    GrepToolOptions,
    createGrepTool,
    createGrepToolDefinition,
)
from misaka.core.tools.ls import (
    LsOperations,
    LsToolDetails,
    LsToolInput,
    LsToolOptions,
    createLsTool,
    createLsToolDefinition,
)
from misaka.core.tools.office import (
    OfficeToolInput,
    createOfficeTool,
    createOfficeToolDefinition,
)
from misaka.core.tools.powershell import (
    PowerShellOperations,
    PowerShellSpawnContext,
    PowerShellSpawnHook,
    PowerShellToolDetails,
    PowerShellToolInput,
    PowerShellToolOptions,
    createLocalPowerShellOperations,
    createPowerShellTool,
    createPowerShellToolDefinition,
)
from misaka.core.tools.read import (
    ReadOperations,
    ReadToolDetails,
    ReadToolInput,
    ReadToolOptions,
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
    createWriteTool,
    createWriteToolDefinition,
)

type Tool = AgentTool
type ToolDef = ToolDefinition[Any, Any]
type ToolName = Literal[
    "read", "bash", "powershell", "edit", "write", "office", "grep", "find", "ls"
]
allToolNames: set[ToolName] = {
    "read", "bash", "powershell", "edit", "write", "office", "grep", "find", "ls"
}

class ToolsOptions(TypedDict, total=False):
    read: ReadToolOptions | Mapping[str, Any]
    bash: BashToolOptions | Mapping[str, Any]
    powershell: PowerShellToolOptions | Mapping[str, Any]
    write: WriteToolOptions | Mapping[str, Any]
    edit: EditToolOptions | Mapping[str, Any]
    office: Mapping[str, Any]
    grep: GrepToolOptions | Mapping[str, Any]
    find: FindToolOptions | Mapping[str, Any]
    ls: LsToolOptions | Mapping[str, Any]


def _get_tool_options(options: ToolsOptions | Mapping[str, Any] | None, key: ToolName) -> Any:
    if options is None:
        return None
    return options.get(key)


def createToolDefinition(
    tool_name: ToolName,
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> ToolDef:
    match tool_name:
        case "read":
            return createReadToolDefinition(cwd, _get_tool_options(options, "read"))
        case "bash":
            return createBashToolDefinition(cwd, _get_tool_options(options, "bash"))
        case "powershell":
            return createPowerShellToolDefinition(
                cwd, _get_tool_options(options, "powershell")
            )
        case "edit":
            return createEditToolDefinition(cwd, _get_tool_options(options, "edit"))
        case "write":
            return createWriteToolDefinition(
                cwd, _get_tool_options(options, "write")
            )
        case "office":
            return createOfficeToolDefinition(
                cwd, _get_tool_options(options, "office")
            )
        case "grep":
            return createGrepToolDefinition(cwd, _get_tool_options(options, "grep"))
        case "find":
            return createFindToolDefinition(cwd, _get_tool_options(options, "find"))
        case "ls":
            return createLsToolDefinition(cwd, _get_tool_options(options, "ls"))
        case _:
            raise RuntimeError(f"Unknown tool name: {tool_name}")


def createTool(
    tool_name: ToolName,
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> Tool:
    match tool_name:
        case "read":
            return createReadTool(cwd, _get_tool_options(options, "read"))
        case "bash":
            return createBashTool(cwd, _get_tool_options(options, "bash"))
        case "powershell":
            return createPowerShellTool(cwd, _get_tool_options(options, "powershell"))
        case "edit":
            return createEditTool(cwd, _get_tool_options(options, "edit"))
        case "write":
            return createWriteTool(cwd, _get_tool_options(options, "write"))
        case "office":
            return createOfficeTool(cwd, _get_tool_options(options, "office"))
        case "grep":
            return createGrepTool(cwd, _get_tool_options(options, "grep"))
        case "find":
            return createFindTool(cwd, _get_tool_options(options, "find"))
        case "ls":
            return createLsTool(cwd, _get_tool_options(options, "ls"))
        case _:
            raise RuntimeError(f"Unknown tool name: {tool_name}")


def createCodingToolDefinitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[ToolDef]:
    return [
        createReadToolDefinition(cwd, _get_tool_options(options, "read")),
        createBashToolDefinition(cwd, _get_tool_options(options, "bash")),
        createEditToolDefinition(cwd, _get_tool_options(options, "edit")),
        createWriteToolDefinition(cwd, _get_tool_options(options, "write")),
    ]


def createReadOnlyToolDefinitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[ToolDef]:
    return [
        createReadToolDefinition(cwd, _get_tool_options(options, "read")),
        createGrepToolDefinition(cwd, _get_tool_options(options, "grep")),
        createFindToolDefinition(cwd, _get_tool_options(options, "find")),
        createLsToolDefinition(cwd, _get_tool_options(options, "ls")),
    ]


def createAllToolDefinitions(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> dict[ToolName, ToolDef]:
    return {
        "read": createReadToolDefinition(cwd, _get_tool_options(options, "read")),
        "bash": createBashToolDefinition(cwd, _get_tool_options(options, "bash")),
        "powershell": createPowerShellToolDefinition(
            cwd, _get_tool_options(options, "powershell")
        ),
        "edit": createEditToolDefinition(cwd, _get_tool_options(options, "edit")),
        "write": createWriteToolDefinition(cwd, _get_tool_options(options, "write")),
        "office": createOfficeToolDefinition(cwd, _get_tool_options(options, "office")),
        "grep": createGrepToolDefinition(cwd, _get_tool_options(options, "grep")),
        "find": createFindToolDefinition(cwd, _get_tool_options(options, "find")),
        "ls": createLsToolDefinition(cwd, _get_tool_options(options, "ls")),
    }


def createCodingTools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[Tool]:
    return [
        createReadTool(cwd, _get_tool_options(options, "read")),
        createBashTool(cwd, _get_tool_options(options, "bash")),
        createEditTool(cwd, _get_tool_options(options, "edit")),
        createWriteTool(cwd, _get_tool_options(options, "write")),
    ]


def createReadOnlyTools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> list[Tool]:
    return [
        createReadTool(cwd, _get_tool_options(options, "read")),
        createGrepTool(cwd, _get_tool_options(options, "grep")),
        createFindTool(cwd, _get_tool_options(options, "find")),
        createLsTool(cwd, _get_tool_options(options, "ls")),
    ]


def createAllTools(
    cwd: str,
    options: ToolsOptions | Mapping[str, Any] | None = None,
) -> dict[ToolName, Tool]:
    return {
        "read": createReadTool(cwd, _get_tool_options(options, "read")),
        "bash": createBashTool(cwd, _get_tool_options(options, "bash")),
        "powershell": createPowerShellTool(
            cwd, _get_tool_options(options, "powershell")
        ),
        "edit": createEditTool(cwd, _get_tool_options(options, "edit")),
        "write": createWriteTool(cwd, _get_tool_options(options, "write")),
        "office": createOfficeTool(cwd, _get_tool_options(options, "office")),
        "grep": createGrepTool(cwd, _get_tool_options(options, "grep")),
        "find": createFindTool(cwd, _get_tool_options(options, "find")),
        "ls": createLsTool(cwd, _get_tool_options(options, "ls")),
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
    "OfficeToolInput",
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
    "createOfficeTool",
    "createOfficeToolDefinition",
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
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "withFileMutationQueue",
]
