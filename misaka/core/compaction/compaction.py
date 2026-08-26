"""Context compaction helpers for coding-agent session trees."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass
from typing import Any

from misaka.agent.types import AgentMessage, StreamFn, ThinkingLevel
from misaka.ai.stream import complete_simple
from misaka.ai.types import (
    AssistantMessage,
    Model,
    SimpleStreamOptions,
    Usage,
    UserMessage,
)
from misaka.core.compaction.utils import (
    SUMMARIZATION_SYSTEM_PROMPT as _SUMMARIZATION_SYSTEM_PROMPT,
)
from misaka.core.compaction.utils import (
    FileOperations,
    _assistant_text,
    _safe_json_stringify,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
)
from misaka.core.compaction.utils import (
    serialize_conversation as _serialize_conversation,
)
from misaka.core.messages import (
    convertToLlm,
    createBranchSummaryMessage,
    createCompactionSummaryMessage,
    createCustomMessage,
)
from misaka.core.session_manager import SessionEntry, build_session_context
from misaka.utils.values import read_field


@dataclass(slots=True)
class CompactionResult:
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: Any | None = None


@dataclass(slots=True)
class CompactionSettings:
    enabled: bool
    reserveTokens: int
    keepRecentTokens: int


@dataclass(slots=True)
class ContextUsageEstimate:
    tokens: int
    usageTokens: int
    trailingTokens: int
    lastUsageIndex: int | None


@dataclass(slots=True)
class CutPointResult:
    firstKeptEntryIndex: int
    turnStartIndex: int
    isSplitTurn: bool


@dataclass(slots=True)
class CompactionPreparation:
    firstKeptEntryId: str
    messagesToSummarize: list[AgentMessage]
    turnPrefixMessages: list[AgentMessage]
    isSplitTurn: bool
    tokensBefore: int
    previousSummary: str | None
    fileOps: FileOperations
    settings: CompactionSettings


_SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize.
Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

_UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate
into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

_TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep.
The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""


def calculate_context_tokens(usage: Usage | dict[str, Any]) -> int:
    total_tokens = _usage_field(usage, "totalTokens")
    if isinstance(total_tokens, int) and total_tokens:
        return total_tokens
    return sum(int(_usage_field(usage, name) or 0) for name in ("input", "output", "cacheRead", "cacheWrite"))


def estimate_context_tokens(messages: list[AgentMessage]) -> ContextUsageEstimate:
    usage_info = _last_assistant_usage_info(messages)
    if usage_info is None:
        estimated = sum(estimate_tokens(message) for message in messages)
        return ContextUsageEstimate(
            tokens=estimated,
            usageTokens=0,
            trailingTokens=estimated,
            lastUsageIndex=None,
        )

    usage_tokens = calculate_context_tokens(usage_info["usage"])
    trailing_tokens = sum(estimate_tokens(message) for message in messages[usage_info["index"] + 1 :])
    return ContextUsageEstimate(
        tokens=usage_tokens + trailing_tokens,
        usageTokens=usage_tokens,
        trailingTokens=trailing_tokens,
        lastUsageIndex=usage_info["index"],
    )


def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    return settings.enabled and context_tokens > context_window - settings.reserveTokens


def estimate_tokens(message: AgentMessage) -> int:
    role = read_field(message, "role")
    chars = 0

    if role == "user":
        content = read_field(message, "content")
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                if read_field(block, "type") == "text":
                    text = read_field(block, "text")
                    if isinstance(text, str):
                        chars += len(text)
        return max(0, math.ceil(chars / 4))

    if role == "assistant":
        for block in read_field(message, "content") or []:
            block_type = read_field(block, "type")
            if block_type == "text":
                text = read_field(block, "text")
                if isinstance(text, str):
                    chars += len(text)
            elif block_type == "thinking":
                thinking = read_field(block, "thinking")
                if isinstance(thinking, str):
                    chars += len(thinking)
            elif block_type == "toolCall":
                name = read_field(block, "name")
                chars += len(name) if isinstance(name, str) else 0
                chars += len(_safe_json_stringify(read_field(block, "arguments")))
        return max(0, math.ceil(chars / 4))

    if role in {"custom", "toolResult"}:
        content = read_field(message, "content")
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                block_type = read_field(block, "type")
                if block_type == "text":
                    text = read_field(block, "text")
                    if isinstance(text, str):
                        chars += len(text)
                elif block_type == "image":
                    chars += 4800
        return max(0, math.ceil(chars / 4))

    if role == "bashExecution":
        command = read_field(message, "command")
        output = read_field(message, "output")
        chars = len(command) if isinstance(command, str) else 0
        chars += len(output) if isinstance(output, str) else 0
        return max(0, math.ceil(chars / 4))

    if role in {"branchSummary", "compactionSummary"}:
        summary = read_field(message, "summary")
        return max(0, math.ceil((len(summary) if isinstance(summary, str) else 0) / 4))

    return 0


def find_turn_start_index(entries: list[SessionEntry], entry_index: int, start_index: int) -> int:
    for index in range(entry_index, start_index - 1, -1):
        entry = entries[index]
        entry_type = _entry_field(entry, "type")
        if entry_type in {"branch_summary", "custom_message"}:
            return index
        if entry_type == "message":
            role = read_field(_entry_field(entry, "message"), "role")
            if role in {"user", "bashExecution"}:
                return index
    return -1


def find_cut_point(
    entries: list[SessionEntry],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    cut_points = _find_valid_cut_points(entries, start_index, end_index)
    if not cut_points:
        return CutPointResult(firstKeptEntryIndex=start_index, turnStartIndex=-1, isSplitTurn=False)

    accumulated_tokens = 0
    cut_index = cut_points[0]

    for index in range(end_index - 1, start_index - 1, -1):
        entry = entries[index]
        if _entry_field(entry, "type") != "message":
            continue
        accumulated_tokens += estimate_tokens(_entry_field(entry, "message"))
        if accumulated_tokens >= keep_recent_tokens:
            for candidate in cut_points:
                if candidate >= index:
                    cut_index = candidate
                    break
            break

    while cut_index > start_index:
        previous = entries[cut_index - 1]
        previous_type = _entry_field(previous, "type")
        if previous_type == "compaction":
            break
        if previous_type == "message":
            break
        cut_index -= 1

    cut_entry = entries[cut_index]
    cut_message = _entry_field(cut_entry, "message")
    is_user_message = _entry_field(cut_entry, "type") == "message" and read_field(cut_message, "role") == "user"
    turn_start_index = -1 if is_user_message else find_turn_start_index(entries, cut_index, start_index)
    return CutPointResult(
        firstKeptEntryIndex=cut_index,
        turnStartIndex=turn_start_index,
        isSplitTurn=(not is_user_message and turn_start_index != -1),
    )


def _create_summarization_options(
    model: Model[Any],
    max_tokens: int,
    api_key: str | None,
    headers: dict[str, str] | None,
    signal: Any | None,
    thinking_level: ThinkingLevel | None,
) -> SimpleStreamOptions:
    options = SimpleStreamOptions(maxTokens=max_tokens, signal=signal, apiKey=api_key, headers=headers)
    if model.reasoning and thinking_level and thinking_level != "off":
        options.reasoning = thinking_level
    return options


async def _complete_summarization(
    model: Model[Any],
    context: dict[str, Any],
    options: SimpleStreamOptions,
    stream_fn: StreamFn | None = None,
) -> AssistantMessage:
    if stream_fn is None:
        return await complete_simple(model, context, options)
    stream = stream_fn(model, context, options)
    if inspect.isawaitable(stream):
        stream = await stream
    return await stream.result()


async def generate_summary(
    current_messages: list[AgentMessage],
    model: Model[Any],
    reserve_tokens: int,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    signal: Any | None = None,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    stream_fn: StreamFn | None = None,
) -> str:
    max_tokens = min(
        math.floor(0.8 * reserve_tokens),
        model.maxTokens if model.maxTokens > 0 else math.inf,
    )

    base_prompt = _UPDATE_SUMMARIZATION_PROMPT if previous_summary else _SUMMARIZATION_PROMPT
    if custom_instructions:
        base_prompt = f"{base_prompt}\n\nAdditional focus: {custom_instructions}"

    conversation_text = _serialize_conversation(convertToLlm(current_messages))
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n"
    if previous_summary:
        prompt_text += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt_text += base_prompt

    response = await _complete_summarization(
        model,
        {
            "systemPrompt": _SUMMARIZATION_SYSTEM_PROMPT,
            "messages": [UserMessage(content=[{"type": "text", "text": prompt_text}], timestamp=_timestamp_ms())],
        },
        _create_summarization_options(model, int(max_tokens), api_key, headers, signal, thinking_level),
        stream_fn,
    )
    if response.stopReason == "error":
        raise RuntimeError(f"Summarization failed: {response.errorMessage or 'Unknown error'}")
    return _assistant_text(response)


def prepare_compaction(
    path_entries: list[SessionEntry],
    settings: CompactionSettings,
) -> CompactionPreparation | None:
    if not path_entries:
        return None
    if path_entries[-1].get("type") == "compaction":
        return None

    previous_compaction_index = -1
    for index in range(len(path_entries) - 1, -1, -1):
        if path_entries[index].get("type") == "compaction":
            previous_compaction_index = index
            break

    previous_summary: str | None = None
    boundary_start = 0
    if previous_compaction_index >= 0:
        previous_compaction = path_entries[previous_compaction_index]
        previous_summary = _entry_field(previous_compaction, "summary")
        first_kept_entry_id = _entry_field(previous_compaction, "firstKeptEntryId")
        first_kept_entry_index = next(
            (index for index, entry in enumerate(path_entries) if _entry_field(entry, "id") == first_kept_entry_id),
            -1,
        )
        boundary_start = first_kept_entry_index if first_kept_entry_index >= 0 else previous_compaction_index + 1

    tokens_before = estimate_context_tokens(build_session_context(path_entries).messages).tokens
    cut_point = find_cut_point(path_entries, boundary_start, len(path_entries), settings.keepRecentTokens)
    first_kept_entry = path_entries[cut_point.firstKeptEntryIndex]
    first_kept_entry_id = first_kept_entry.get("id")
    if not isinstance(first_kept_entry_id, str) or not first_kept_entry_id:
        return None

    history_end = cut_point.turnStartIndex if cut_point.isSplitTurn else cut_point.firstKeptEntryIndex
    messages_to_summarize: list[AgentMessage] = []
    for entry in path_entries[boundary_start:history_end]:
        message = _get_message_from_entry_for_compaction(entry)
        if message is not None:
            messages_to_summarize.append(message)

    turn_prefix_messages: list[AgentMessage] = []
    if cut_point.isSplitTurn:
        for entry in path_entries[cut_point.turnStartIndex : cut_point.firstKeptEntryIndex]:
            message = _get_message_from_entry_for_compaction(entry)
            if message is not None:
                turn_prefix_messages.append(message)

    file_ops = _extract_file_operations(messages_to_summarize, path_entries, previous_compaction_index)
    if cut_point.isSplitTurn:
        for message in turn_prefix_messages:
            extract_file_ops_from_message(message, file_ops)

    return CompactionPreparation(
        firstKeptEntryId=first_kept_entry_id,
        messagesToSummarize=messages_to_summarize,
        turnPrefixMessages=turn_prefix_messages,
        isSplitTurn=cut_point.isSplitTurn,
        tokensBefore=tokens_before,
        previousSummary=previous_summary,
        fileOps=file_ops,
        settings=settings,
    )


async def compact(
    preparation: CompactionPreparation,
    model: Model[Any],
    api_key: str | None,
    headers: dict[str, str] | None = None,
    custom_instructions: str | None = None,
    signal: Any | None = None,
    thinking_level: ThinkingLevel | None = None,
    stream_fn: StreamFn | None = None,
) -> CompactionResult:
    if preparation.isSplitTurn and preparation.turnPrefixMessages:
        history_result, turn_prefix_result = await asyncio.gather(
            generate_summary(
                preparation.messagesToSummarize,
                model,
                preparation.settings.reserveTokens,
                api_key,
                headers,
                signal,
                custom_instructions,
                preparation.previousSummary,
                thinking_level,
                stream_fn,
            )
            if preparation.messagesToSummarize
            else _resolved("No prior history."),
            _generate_turn_prefix_summary(
                preparation.turnPrefixMessages,
                model,
                preparation.settings.reserveTokens,
                api_key,
                headers,
                signal,
                thinking_level,
                stream_fn,
            ),
        )
        summary = f"{history_result}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_result}"
    else:
        summary = await generate_summary(
            preparation.messagesToSummarize,
            model,
            preparation.settings.reserveTokens,
            api_key,
            headers,
            signal,
            custom_instructions,
            preparation.previousSummary,
            thinking_level,
            stream_fn,
        )

    file_lists = compute_file_lists(preparation.fileOps)
    summary += format_file_operations(file_lists["readFiles"], file_lists["modifiedFiles"])
    if not preparation.firstKeptEntryId:
        raise RuntimeError("First kept entry has no UUID - session may need migration")
    return CompactionResult(
        summary=summary,
        firstKeptEntryId=preparation.firstKeptEntryId,
        tokensBefore=preparation.tokensBefore,
        details={
            "readFiles": file_lists["readFiles"],
            "modifiedFiles": file_lists["modifiedFiles"],
        },
    )


def _extract_file_operations(
    messages: list[AgentMessage],
    entries: list[SessionEntry],
    previous_compaction_index: int,
) -> FileOperations:
    file_ops = create_file_ops()
    if previous_compaction_index >= 0:
        previous = entries[previous_compaction_index]
        if not previous.get("fromHook") and previous.get("details") is not None:
            details = previous.get("details")
            read_files = details.get("readFiles") if isinstance(details, dict) else getattr(details, "readFiles", None)
            modified_files = (
                details.get("modifiedFiles") if isinstance(details, dict) else getattr(details, "modifiedFiles", None)
            )
            if isinstance(read_files, list):
                for path in read_files:
                    if isinstance(path, str):
                        file_ops.read.add(path)
            if isinstance(modified_files, list):
                for path in modified_files:
                    if isinstance(path, str):
                        file_ops.edited.add(path)
    for message in messages:
        extract_file_ops_from_message(message, file_ops)
    return file_ops


def _get_message_from_entry(entry: SessionEntry) -> AgentMessage | None:
    entry_type = entry.get("type")
    if entry_type == "message":
        return entry.get("message")
    if entry_type == "custom_message":
        return createCustomMessage(
            entry.get("customType"),
            entry.get("content"),
            entry.get("display"),
            entry.get("details"),
            entry.get("timestamp"),
        )
    if entry_type == "branch_summary":
        return createBranchSummaryMessage(
            entry.get("summary"),
            entry.get("fromId"),
            entry.get("timestamp"),
        )
    if entry_type == "compaction":
        return createCompactionSummaryMessage(
            entry.get("summary"),
            entry.get("tokensBefore"),
            entry.get("timestamp"),
        )
    return None


def _get_message_from_entry_for_compaction(entry: SessionEntry) -> AgentMessage | None:
    if entry.get("type") == "compaction":
        return None
    return _get_message_from_entry(entry)


def _find_valid_cut_points(entries: list[SessionEntry], start_index: int, end_index: int) -> list[int]:
    cut_points: list[int] = []
    for index in range(start_index, end_index):
        entry = entries[index]
        entry_type = entry.get("type")
        if entry_type == "message":
            role = read_field(entry.get("message"), "role")
            if role in {"bashExecution", "custom", "branchSummary", "compactionSummary", "user", "assistant"}:
                cut_points.append(index)
        if entry_type in {"branch_summary", "custom_message"}:
            cut_points.append(index)
    return cut_points


async def _generate_turn_prefix_summary(
    messages: list[AgentMessage],
    model: Model[Any],
    reserve_tokens: int,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    signal: Any | None = None,
    thinking_level: ThinkingLevel | None = None,
    stream_fn: StreamFn | None = None,
) -> str:
    max_tokens = min(
        math.floor(0.5 * reserve_tokens),
        model.maxTokens if model.maxTokens > 0 else math.inf,
    )
    conversation_text = _serialize_conversation(convertToLlm(messages))
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n{_TURN_PREFIX_SUMMARIZATION_PROMPT}"
    response = await _complete_summarization(
        model,
        {
            "systemPrompt": _SUMMARIZATION_SYSTEM_PROMPT,
            "messages": [UserMessage(content=[{"type": "text", "text": prompt_text}], timestamp=_timestamp_ms())],
        },
        _create_summarization_options(model, int(max_tokens), api_key, headers, signal, thinking_level),
        stream_fn,
    )
    if response.stopReason == "error":
        raise RuntimeError(f"Turn prefix summarization failed: {response.errorMessage or 'Unknown error'}")
    return _assistant_text(response)


def _assistant_usage(message: Any) -> Usage | dict[str, Any] | None:
    if read_field(message, "role") != "assistant":
        return None
    if read_field(message, "stopReason") in {"aborted", "error"}:
        return None
    usage = read_field(message, "usage")
    return usage if usage is not None else None


def _last_assistant_usage_info(messages: list[AgentMessage]) -> dict[str, Any] | None:
    for index in range(len(messages) - 1, -1, -1):
        usage = _assistant_usage(messages[index])
        if usage is not None:
            return {"usage": usage, "index": index}
    return None


def _usage_field(usage: Usage | dict[str, Any], name: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


async def _resolved(value: str) -> str:
    return value


calculateContextTokens = calculate_context_tokens
estimateContextTokens = estimate_context_tokens
prepareCompaction = prepare_compaction
shouldCompact = should_compact

__all__ = [
    "CompactionPreparation",
    "CompactionResult",
    "CompactionSettings",
    "ContextUsageEstimate",
    "CutPointResult",
    "calculateContextTokens",
    "compact",
    "estimateContextTokens",
    "prepareCompaction",
    "shouldCompact",
]
