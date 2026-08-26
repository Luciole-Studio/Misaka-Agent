"""Shared tool exports for the coding-agent runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from misaka.agent.types import AgentTool
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.bash import (
    BashExecOptions,
    BashOperations,
    BashSpawnContext,
    BashSpawnHook,
    BashToolDetails,
    BashToolInput,
    BashToolOptions,
    create_bash_tool,
    create_bash_tool_definition,
    create_local_bash_operations,
    createBashTool,
    createBashToolDefinition,
    createLocalBashOperations,
)
from misaka.core.tools.edit import (
    EditOperations,
    EditToolDetails,
    EditToolInput,
    EditToolOptions,
    ReplaceEditInput,
    create_edit_tool,
    create_edit_tool_definition,
    createEditTool,
    createEditToolDefinition,
    prepare_edit_arguments,
    prepareEditArguments,
)
from misaka.core.tools.edit_diff import (
    AppliedEditsResult,
    Edit,
    EditDiffError,
    EditDiffResult,
    FuzzyMatchResult,
    apply_edits_to_normalized_content,
    applyEditsToNormalizedContent,
    compute_edit_diff,
    compute_edits_diff,
    computeEditDiff,
    computeEditsDiff,
    detect_line_ending,
    detectLineEnding,
    fuzzy_find_text,
    fuzzyFindText,
    generate_diff_string,
    generate_unified_patch,
    generateDiffString,
    generateUnifiedPatch,
    normalize_for_fuzzy_match,
    normalize_to_lf,
    normalizeForFuzzyMatch,
    normalizeToLF,
    restore_line_endings,
    restoreLineEndings,
    strip_bom,
    stripBom,
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
from misaka.core.tools.output_accumulator import (
    OutputAccumulator,
    OutputAccumulatorOptions,
    OutputSnapshot,
    byte_length,
    byteLength,
    default_temp_file_path,
    defaultTempFilePath,
)
from misaka.core.tools.path_utils import (
    expand_path,
    expandPath,
    resolve_read_path,
    resolve_to_cwd,
    resolveReadPath,
    resolveToCwd,
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
from misaka.core.tools.render_utils import (
    ToolRenderResultLike,
    get_text_output,
    getTextOutput,
    invalid_arg_text,
    invalidArgText,
    normalize_display_text,
    normalizeDisplayText,
    replace_tabs,
    replaceTabs,
    shorten_path,
    shortenPath,
    str_value,
)
from misaka.core.tools.tool_definition_wrapper import (
    create_tool_definition_from_agent_tool,
    createToolDefinitionFromAgentTool,
    wrap_tool_definition,
    wrap_tool_definitions,
    wrapToolDefinition,
    wrapToolDefinitions,
)
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    GREP_MAX_LINE_LENGTH,
    TruncationOptions,
    TruncationResult,
    format_size,
    formatSize,
    truncate_head,
    truncate_line,
    truncate_tail,
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

all_tool_names: set[ToolName] = {"read", "bash", "edit", "write", "grep", "find", "ls"}


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
        case "edit":
            return create_edit_tool_definition(cwd, _get_tool_options(options, "edit"))
        case "write":
            return create_write_tool_definition(cwd, _get_tool_options(options, "write"))
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
        "edit": create_edit_tool(cwd, _get_tool_options(options, "edit")),
        "write": create_write_tool(cwd, _get_tool_options(options, "write")),
        "grep": create_grep_tool(cwd, _get_tool_options(options, "grep")),
        "find": create_find_tool(cwd, _get_tool_options(options, "find")),
        "ls": create_ls_tool(cwd, _get_tool_options(options, "ls")),
    }


createCodingTools = create_coding_tools
createReadOnlyTools = create_read_only_tools

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
    "createCodingTools",
    "createEditTool",
    "createEditToolDefinition",
    "createFindTool",
    "createFindToolDefinition",
    "createGrepTool",
    "createGrepToolDefinition",
    "createLocalBashOperations",
    "createLsTool",
    "createLsToolDefinition",
    "createReadOnlyTools",
    "createReadTool",
    "createReadToolDefinition",
    "createWriteTool",
    "createWriteToolDefinition",
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "withFileMutationQueue",
    ]
