"""Context compaction helpers for coding-agent session trees."""

from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.types import AgentMessage, StreamFn, ThinkingLevel
from misaka.ai.stream import complete_simple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    TranscriptContext,
    Usage,
    UserMessage,
)
from misaka.ai.utils.retry import RetryCallbacks, RetryPolicy, retry_assistant_call
from misaka.ai.utils.transcript import get_current_system_message, normalize_context
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
from misaka.core.messages import convertToLlm
from misaka.core.session_manager import (
    SessionEntry,
    SessionProjection,
    SessionProjectionEntry,
    build_session_projection,
    session_entry_to_context_messages,
)
from misaka.utils.values import read_field


class CompactionDetails(TypedDict):
    readFiles: list[str]
    modifiedFiles: list[str]


@dataclass(slots=True)
class CompactionResult:
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: Any | None = None
    # Keep details as the fourth positional field for existing Python extensions.
    estimatedTokensAfter: int | None = None
    usage: Usage | dict[str, Any] | None = None
    # None keeps Pi's prefix/tail form; even [] is a complete engine-owned view.
    contextMessages: list[AgentMessage] | None = None


@dataclass(slots=True)
class SummaryWithUsage:
    text: str
    usage: Usage


@dataclass(slots=True)
class CompactionSettings:
    enabled: bool
    reserveTokens: int
    keepRecentTokens: int


DEFAULT_COMPACTION_SETTINGS = CompactionSettings(
    enabled=True,
    reserveTokens=16384,
    keepRecentTokens=20000,
)


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

# Clearly separates the conversation and uses continuation-oriented instructions: the
# earlier framing was refused by Claude Fable 5.1 (pi #9908).
_TURN_PREFIX_SUMMARIZATION_PROMPT = """The messages above are earlier context from an ongoing conversation. Later messages are stored separately and do not need to be reconstructed.

Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed before the later messages so the conversation can continue with the necessary context.

## Original Request
[What did the user ask for?]

## Progress So Far
- [Key decisions and work completed in these messages]

## Context Needed to Continue
- [Information from these messages needed to understand the later work]

Only summarize information explicitly present above. Do not infer or recreate later messages."""


def get_summarization_failure(response: AssistantMessage, label: str) -> str | None:
    """Return why a summarization response is unsafe to persist."""
    if response.stopReason == "error":
        return f"{label} failed: {response.errorMessage or 'Unknown error'}"
    if response.stopReason == "length":
        return f"{label} failed: generation hit the token cap and the summary is incomplete"
    return None


def combine_usage(first: Usage, second: Usage) -> Usage:
    return Usage(
        input=first.input + second.input,
        output=first.output + second.output,
        cacheRead=first.cacheRead + second.cacheRead,
        cacheWrite=first.cacheWrite + second.cacheWrite,
        cacheWrite1h=(
            (first.cacheWrite1h or 0) + (second.cacheWrite1h or 0)
            if first.cacheWrite1h is not None or second.cacheWrite1h is not None
            else None
        ),
        reasoning=(
            (first.reasoning or 0) + (second.reasoning or 0)
            if first.reasoning is not None or second.reasoning is not None
            else None
        ),
        totalTokens=first.totalTokens + second.totalTokens,
        cost={
            "input": first.cost.input + second.cost.input,
            "output": first.cost.output + second.cost.output,
            "cacheRead": first.cost.cacheRead + second.cost.cacheRead,
            "cacheWrite": first.cost.cacheWrite + second.cost.cacheWrite,
            "total": first.cost.total + second.cost.total,
        },
    )


def calculate_context_tokens(usage: Usage | dict[str, Any]) -> int:
    total_tokens = _usage_field(usage, "totalTokens")
    if isinstance(total_tokens, int) and total_tokens:
        return total_tokens
    return sum(int(_usage_field(usage, name) or 0) for name in ("input", "output", "cacheRead", "cacheWrite"))


def get_last_assistant_usage(entries: list[SessionEntry]) -> Usage | dict[str, Any] | None:
    for entry in reversed(entries):
        if _entry_field(entry, "type") != "message":
            continue
        usage = _assistant_usage(_entry_field(entry, "message"))
        if usage is not None:
            return usage
    return None


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


def estimate_projected_context_tokens(
    projection: SessionProjection, branch_entries: list[SessionEntry]
) -> ContextUsageEstimate:
    """Estimate projected context without trusting usage captured before a later edit or compaction."""
    estimate = estimate_context_tokens(projection.messages)
    if estimate.lastUsageIndex is not None:
        projected_message_index = 0
        usage_entry_id: str | None = None
        for entry in projection.entries:
            next_message_index = projected_message_index + len(entry.messages)
            if estimate.lastUsageIndex < next_message_index:
                usage_entry_id = _entry_field(entry.sourceEntry, "id")
                break
            projected_message_index = next_message_index
        usage_entry_index = (
            next((i for i, entry in enumerate(branch_entries) if _entry_field(entry, "id") == usage_entry_id), -1)
            if usage_entry_id
            else -1
        )
        latest_invalidating_entry_index = -1
        for i in range(len(branch_entries) - 1, -1, -1):
            if _entry_field(branch_entries[i], "type") in {"context_edit", "compaction"}:
                latest_invalidating_entry_index = i
                break
        if usage_entry_index > latest_invalidating_entry_index:
            return estimate
    current_system = get_current_system_message(projection.messages)
    tokens = estimate_tokens(current_system) if current_system else 0
    for message in projection.messages:
        if read_field(message, "role") != "system":
            tokens += estimate_tokens(message)
    return ContextUsageEstimate(tokens=tokens, usageTokens=0, trailingTokens=tokens, lastUsageIndex=None)


def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    return settings.enabled and context_tokens > context_window - settings.reserveTokens


def _utf16_length(text: str) -> int:
    """Return JavaScript ``String.length`` for Pi's chars/4 heuristic."""
    return len(text) + sum(ord(char) > 0xFFFF for char in text)


def estimate_tokens(message: AgentMessage) -> int:
    role = read_field(message, "role")
    chars = 0

    if role == "system":
        content = read_field(message, "content")
        if isinstance(content, str):
            chars = _utf16_length(content)
        elif isinstance(content, list):
            for block in content:
                text = read_field(block, "text")
                if read_field(block, "type") == "text" and isinstance(text, str):
                    chars += _utf16_length(text)
        for section in (read_field(message, "sections") or {}).values():
            if section:
                chars += _utf16_length(section)
        tools_added = read_field(message, "toolsAdded")
        if tools_added:
            chars += _utf16_length(_safe_json_stringify([_tool_payload(tool) for tool in tools_added]))
        return max(0, math.ceil(chars / 4))

    if role == "user":
        content = read_field(message, "content")
        if isinstance(content, str):
            chars = _utf16_length(content)
        elif isinstance(content, list):
            for block in content:
                block_type = read_field(block, "type")
                if block_type == "text":
                    text = read_field(block, "text")
                    if isinstance(text, str):
                        chars += _utf16_length(text)
                elif block_type == "image":
                    # Same flat allowance the toolResult branch below applies (and
                    # upstream's estimateTextAndImageContentChars, compaction.ts:246-260).
                    # Counting user images as zero made an image-heavy prefix look small,
                    # so compaction fired later than the window could actually afford.
                    chars += 4800
        return max(0, math.ceil(chars / 4))

    if role == "assistant":
        for block in read_field(message, "content") or []:
            block_type = read_field(block, "type")
            if block_type == "text":
                text = read_field(block, "text")
                if isinstance(text, str):
                    chars += _utf16_length(text)
            elif block_type == "thinking":
                thinking = read_field(block, "thinking")
                if isinstance(thinking, str):
                    chars += _utf16_length(thinking)
            elif block_type == "toolCall":
                name = read_field(block, "name")
                chars += _utf16_length(name) if isinstance(name, str) else 0
                chars += _utf16_length(_safe_json_stringify(read_field(block, "arguments")))
        return max(0, math.ceil(chars / 4))

    if role in {"custom", "toolResult"}:
        content = read_field(message, "content")
        if isinstance(content, str):
            chars = _utf16_length(content)
        elif isinstance(content, list):
            for block in content:
                block_type = read_field(block, "type")
                if block_type == "text":
                    text = read_field(block, "text")
                    if isinstance(text, str):
                        chars += _utf16_length(text)
                elif block_type == "image":
                    chars += 4800
        return max(0, math.ceil(chars / 4))

    if role == "bashExecution":
        command = read_field(message, "command")
        output = read_field(message, "output")
        chars = _utf16_length(command) if isinstance(command, str) else 0
        chars += _utf16_length(output) if isinstance(output, str) else 0
        return max(0, math.ceil(chars / 4))

    if role in {"branchSummary", "compactionSummary"}:
        summary = read_field(message, "summary")
        return max(0, math.ceil((_utf16_length(summary) if isinstance(summary, str) else 0) / 4))

    return 0


# Roles a cut may land on. toolResult is excluded: it must stay with its tool call.
_CUT_POINT_ROLES = frozenset({"user", "assistant", "bashExecution", "custom", "branchSummary", "compactionSummary"})
# Roles that open a turn. assistant/toolResult continue the turn they are in.
_TURN_START_ROLES = frozenset({"user", "bashExecution", "custom", "branchSummary", "compactionSummary"})


def _projected_messages(
    entries: list[SessionEntry],
    index: int,
    projections: dict[int, list[AgentMessage]] | None,
) -> list[AgentMessage]:
    """The context projection of one entry, reusing ``projections`` when it has it.

    ``session_entry_to_context_messages`` builds a fresh message for every
    custom_message / branch_summary / compaction entry, and find_cut_point walks the same
    range up to four times (cut points, keep window, metadata absorption, turn start), so
    the projection is computed once per call and shared. An empty list is a real answer
    (an entry the context cannot see) and is cached as such; ``None`` means "not cached".
    """
    if projections is not None:
        cached = projections.get(index)
        if cached is not None:
            return cached
    return session_entry_to_context_messages(entries[index])


def _is_turn_start(entry: SessionEntry, messages: list[AgentMessage]) -> bool:
    if _entry_field(entry, "type") == "compaction":
        return False
    return any(read_field(message, "role") in _TURN_START_ROLES for message in messages)


def find_turn_start_index(
    entries: list[SessionEntry],
    entry_index: int,
    start_index: int,
    projections: dict[int, list[AgentMessage]] | None = None,
) -> int:
    for index in range(entry_index, start_index - 1, -1):
        if _is_turn_start(entries[index], _projected_messages(entries, index, projections)):
            return index
    return -1


def find_cut_point(
    entries: list[SessionEntry],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    projections = {
        index: session_entry_to_context_messages(entries[index]) for index in range(start_index, end_index)
    }
    cut_points = _find_valid_cut_points(entries, start_index, end_index, projections)
    if not cut_points:
        return CutPointResult(firstKeptEntryIndex=start_index, turnStartIndex=-1, isSplitTurn=False)

    accumulated_tokens = 0
    # Fall back to the LAST valid cut point, not the first: toolResult entries are not
    # valid cut points, so a tail made of them (exactly the shape a truncate-then-
    # compact-and-retry leaves behind) fills the keepRecentTokens window without any
    # candidate inside it. Keeping everything there summarizes nothing, pays for an
    # empty summarization, appends a summary that only grows the context, and repeats
    # every turn. "Cannot keep a full keepRecentTokens window" must mean keep less.
    cut_index = cut_points[-1]

    # The keep window is measured over everything the context actually carries, so
    # custom_message / branch_summary entries count here just like message entries -- and
    # so does the previous compaction's own summary, which every second compaction walks
    # over and which the context pays for exactly like any other message.
    for index in range(end_index - 1, start_index - 1, -1):
        message_tokens = sum(estimate_tokens(message) for message in projections[index])
        if message_tokens == 0:
            continue
        accumulated_tokens += message_tokens
        if accumulated_tokens >= keep_recent_tokens:
            for candidate in cut_points:
                if candidate >= index:
                    cut_index = candidate
                    break
            break

    # Absorb adjacent entries that are invisible to the context; stop at a compaction
    # boundary or at anything the context can see.
    while cut_index > start_index:
        previous = entries[cut_index - 1]
        if _entry_field(previous, "type") == "compaction" or projections[cut_index - 1]:
            break
        cut_index -= 1

    starts_turn = _is_turn_start(entries[cut_index], projections[cut_index])
    turn_start_index = -1 if starts_turn else find_turn_start_index(entries, cut_index, start_index, projections)
    return CutPointResult(
        firstKeptEntryIndex=cut_index,
        turnStartIndex=turn_start_index,
        isSplitTurn=(not starts_turn and turn_start_index != -1),
    )


def _create_summarization_options(
    model: Model[Any],
    max_tokens: int,
    api_key: str | None,
    headers: dict[str, str] | None,
    signal: Any | None,
    thinking_level: ThinkingLevel | None,
    env: dict[str, str] | None = None,
    session_id: str | None = None,
) -> SimpleStreamOptions:
    options = SimpleStreamOptions(
        maxTokens=max_tokens,
        signal=signal,
        apiKey=api_key,
        headers=headers,
        env=env,
        sessionId=session_id,
    )
    if model.reasoning and thinking_level and thinking_level != "off":
        options.reasoning = thinking_level
    return options


def _build_summarization_context(prompt_text: str) -> TranscriptContext:
    """Build the provider context for a standalone summary request."""
    return normalize_context(
        Context(
            systemPrompt=_SUMMARIZATION_SYSTEM_PROMPT,
            messages=[UserMessage(content=[{"type": "text", "text": prompt_text}], timestamp=_timestamp_ms())],
        )
    )


async def complete_summarization(
    model: Model[Any],
    context: TranscriptContext | dict[str, Any],
    options: SimpleStreamOptions,
    stream_fn: StreamFn | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
) -> AssistantMessage:
    """The one choke point every summarization call goes through (pi #6647).

    Compaction used to make a single un-retried call, so one transient mid-stream socket
    death ("terminated") failed the whole compaction and the session kept its full
    context. Wrapping the call in the shared retry policy makes transient drops cost a
    backoff instead; deterministic errors and aborts still return on the first attempt.
    """

    # Build the isolated request once, outside the retry closure. Retries keep one
    # routing key, while independent summaries receive distinct provider sessions.
    request_options = options.model_copy(
        update={
            "cacheRetention": "none",
            "sessionId": options.sessionId if options.sessionId is not None else uuidv7(),
        }
    )

    async def produce() -> AssistantMessage:
        if stream_fn is None:
            return await complete_simple(model, context, request_options)
        stream = stream_fn(model, context, request_options)
        if inspect.isawaitable(stream):
            stream = await stream
        return await stream.result()

    return await retry_assistant_call(produce, retry, request_options.signal, callbacks)


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
    retry: RetryPolicy | None = None,
    env: dict[str, str] | None = None,
    callbacks: RetryCallbacks | None = None,
    session_id: str | None = None,
) -> str:
    return (
        await generate_summary_with_usage(
            current_messages,
            model,
            reserve_tokens,
            api_key,
            headers,
            signal,
            custom_instructions,
            previous_summary,
            thinking_level,
            stream_fn,
            retry,
            env,
            callbacks,
            session_id,
        )
    ).text


async def generate_summary_with_usage(
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
    retry: RetryPolicy | None = None,
    env: dict[str, str] | None = None,
    callbacks: RetryCallbacks | None = None,
    session_id: str | None = None,
) -> SummaryWithUsage:
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

    response = await complete_summarization(
        model,
        _build_summarization_context(prompt_text),
        _create_summarization_options(
            model,
            int(max_tokens),
            api_key,
            headers,
            signal,
            thinking_level,
            env,
            session_id,
        ),
        stream_fn,
        retry,
        callbacks,
    )
    failure = get_summarization_failure(response, "Summarization")
    if failure:
        raise RuntimeError(failure)
    if any(read_field(block, "type") == "toolCall" for block in response.content):
        raise RuntimeError("Summarization attempted to call a tool")
    return SummaryWithUsage(text=_assistant_text(response), usage=response.usage)


async def generateSummary(
    currentMessages: list[AgentMessage],
    model: Model[Any],
    reserveTokens: int,
    apiKey: str | None,
    headers: dict[str, str] | None = None,
    signal: Any | None = None,
    customInstructions: str | None = None,
    previousSummary: str | None = None,
    thinkingLevel: ThinkingLevel | None = None,
    streamFn: StreamFn | None = None,
    env: dict[str, str] | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
    sessionId: str | None = None,
) -> str:
    return await generate_summary(
        currentMessages,
        model,
        reserveTokens,
        apiKey,
        headers=headers,
        signal=signal,
        custom_instructions=customInstructions,
        previous_summary=previousSummary,
        thinking_level=thinkingLevel,
        stream_fn=streamFn,
        retry=retry,
        env=env,
        callbacks=callbacks,
        session_id=sessionId,
    )


async def generateSummaryWithUsage(
    currentMessages: list[AgentMessage],
    model: Model[Any],
    reserveTokens: int,
    apiKey: str | None,
    headers: dict[str, str] | None = None,
    signal: Any | None = None,
    customInstructions: str | None = None,
    previousSummary: str | None = None,
    thinkingLevel: ThinkingLevel | None = None,
    streamFn: StreamFn | None = None,
    env: dict[str, str] | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
    sessionId: str | None = None,
) -> SummaryWithUsage:
    return await generate_summary_with_usage(
        currentMessages,
        model,
        reserveTokens,
        apiKey,
        headers=headers,
        signal=signal,
        custom_instructions=customInstructions,
        previous_summary=previousSummary,
        thinking_level=thinkingLevel,
        stream_fn=streamFn,
        retry=retry,
        env=env,
        callbacks=callbacks,
        session_id=sessionId,
    )


def _is_projected_turn_start(entry: SessionProjectionEntry) -> bool:
    if _entry_field(entry.sourceEntry, "type") == "compaction":
        return False
    return any(read_field(message, "role") in _TURN_START_ROLES for message in entry.messages)


def _find_projected_turn_start_index(entries: list[SessionProjectionEntry], entry_index: int, start_index: int) -> int:
    for i in range(entry_index, start_index - 1, -1):
        if _is_projected_turn_start(entries[i]):
            return i
    return -1


def _find_projected_cut_point(
    entries: list[SessionProjectionEntry], start_index: int, end_index: int, keep_recent_tokens: int
) -> CutPointResult:
    cut_points: list[int] = []
    for i in range(start_index, end_index):
        entry = entries[i]
        if _entry_field(entry.sourceEntry, "type") != "compaction" and any(
            read_field(message, "role") in _CUT_POINT_ROLES for message in entry.messages
        ):
            cut_points.append(i)
    if not cut_points:
        return CutPointResult(firstKeptEntryIndex=start_index, turnStartIndex=-1, isSplitTurn=False)
    accumulated_tokens = 0
    exceeded_budget = False
    cut_index = cut_points[0]
    for i in range(end_index - 1, start_index - 1, -1):
        message_tokens = sum(estimate_tokens(message) for message in entries[i].messages)
        if message_tokens == 0:
            continue
        accumulated_tokens += message_tokens
        if accumulated_tokens >= keep_recent_tokens:
            exceeded_budget = True
            # Prefer the closest valid cut point at or after this entry. If trailing
            # tool results exceed the budget by themselves, keep their preceding
            # assistant tool call instead of falling back to the first message.
            cut_index = next((candidate for candidate in cut_points if candidate >= i), cut_points[-1])
            break
    # A recovery attempt and its omission edits are context-invisible after the last
    # visible input. Advance only for a closed suffix containing an omitted assistant
    # attempt; arbitrary metadata must not move the cut past unsent input.
    suffix = entries[cut_index + 1 : end_index]

    def is_intrinsically_visible(entry: SessionProjectionEntry) -> bool:
        return (
            _entry_field(entry.sourceEntry, "type") != "context_edit"
            and len(session_entry_to_context_messages(entry.sourceEntry)) > 0
        )

    def is_omitted(entry: SessionProjectionEntry) -> bool:
        return is_intrinsically_visible(entry) and len(entry.messages) == 0

    omitted_suffix_ids = {_entry_field(entry.sourceEntry, "id") for entry in suffix if is_omitted(entry)}
    has_external_replacement = any(
        _entry_field(entry.sourceEntry, "type") == "context_edit"
        and entry.sourceEntry.get("replacement") is not None
        and _entry_field(entry.sourceEntry, "targetId") not in omitted_suffix_ids
        for entry in suffix
    )
    is_recovery_omission_suffix = (
        exceeded_budget
        and not has_external_replacement
        and any(
            _entry_field(entry.sourceEntry, "type") == "message"
            and read_field(entry.sourceEntry.get("message"), "role") == "assistant"
            and is_omitted(entry)
            for entry in suffix
        )
        and all(
            _entry_field(entry.sourceEntry, "type") != "compaction"
            and (not is_intrinsically_visible(entry) or is_omitted(entry))
            for entry in suffix
        )
    )
    if is_recovery_omission_suffix:
        cut_index += 1
    while cut_index > start_index:
        previous = entries[cut_index - 1]
        if _entry_field(previous.sourceEntry, "type") == "compaction" or len(previous.messages) > 0:
            break
        cut_index -= 1
    starts_turn = _is_projected_turn_start(entries[cut_index])
    turn_start_index = -1 if starts_turn else _find_projected_turn_start_index(entries, cut_index, start_index)
    return CutPointResult(
        firstKeptEntryIndex=cut_index,
        turnStartIndex=turn_start_index,
        isSplitTurn=(not starts_turn and turn_start_index != -1),
    )


def prepare_compaction(
    path_entries: list[SessionEntry],
    settings: CompactionSettings,
) -> CompactionPreparation | None:
    if not path_entries:
        return None
    if path_entries[-1].get("type") == "compaction":
        return None
    projection = build_session_projection(path_entries)
    projected_entries = projection.entries
    source_entries = [entry.sourceEntry for entry in projected_entries]
    # The newest compaction is projected first. Older compaction entries can still
    # occur in its retained raw range, but their projected contribution is empty.
    prev_compaction_index = next(
        (
            index
            for index, entry in enumerate(projected_entries)
            if _entry_field(entry.sourceEntry, "type") == "compaction" and len(entry.messages) > 0
        ),
        -1,
    )
    previous_summary: str | None = None
    checkpoint_messages: list[AgentMessage] = []
    boundary_start = 0
    if prev_compaction_index >= 0:
        previous_compaction = projected_entries[prev_compaction_index].sourceEntry
        previous_summary = _entry_field(previous_compaction, "summary")
        # The canonical projection has already selected the previous compaction's retained tail.
        boundary_start = prev_compaction_index + 1
        if previous_compaction.get("contextMessages") is not None:
            # MISAKA fork: a complete checkpoint (the LCM plugin's whole-context replacement)
            # may still contain raw backlog and a changed tail. On a native compaction,
            # summarize that view, never its display label or the pre-checkpoint archive
            # that it already replaced.
            checkpoint_messages = [
                message
                for message in projected_entries[prev_compaction_index].messages
                if read_field(message, "role") != "system"
            ]
            previous_summary = None

    boundary_end = len(projected_entries)
    tokens_before = estimate_projected_context_tokens(projection, path_entries).tokens
    cut_point = _find_projected_cut_point(projected_entries, boundary_start, boundary_end, settings.keepRecentTokens)
    first_kept_entry = (
        projected_entries[cut_point.firstKeptEntryIndex].sourceEntry
        if cut_point.firstKeptEntryIndex < len(projected_entries)
        else None
    )
    first_kept_entry_id = first_kept_entry.get("id") if first_kept_entry else None
    if not isinstance(first_kept_entry_id, str) or not first_kept_entry_id:
        return None

    history_end = cut_point.turnStartIndex if cut_point.isSplitTurn else cut_point.firstKeptEntryIndex
    messages_to_summarize: list[AgentMessage] = [
        *checkpoint_messages,
        *(
            message
            for entry in projected_entries[boundary_start:history_end]
            for message in _get_messages_from_projected_entry_for_compaction(entry)
        ),
    ]
    turn_prefix_messages: list[AgentMessage] = (
        [
            message
            for entry in projected_entries[cut_point.turnStartIndex : cut_point.firstKeptEntryIndex]
            for message in _get_messages_from_projected_entry_for_compaction(entry)
        ]
        if cut_point.isSplitTurn
        else []
    )
    if not messages_to_summarize and not turn_prefix_messages:
        return None

    # Extract file operations from edited model-visible messages and the previous compaction.
    file_ops = _extract_file_operations(messages_to_summarize, source_entries, prev_compaction_index)
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
    env: dict[str, str] | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
    session_id: str | None = None,
) -> CompactionResult:
    if preparation.isSplitTurn and preparation.turnPrefixMessages:
        history_result = None
        if preparation.messagesToSummarize:
            history_result = await generate_summary_with_usage(
                preparation.messagesToSummarize,
                model,
                preparation.settings.reserveTokens,
                api_key,
                headers,
                signal,
                custom_instructions,
                preparation.previousSummary,
                thinking_level,
                stream_fn=stream_fn,
                retry=retry,
                env=env,
                callbacks=callbacks,
                session_id=session_id,
            )

        turn_prefix_result = await _generate_turn_prefix_summary(
            preparation.turnPrefixMessages,
            model,
            preparation.settings.reserveTokens,
            api_key,
            headers,
            signal,
            thinking_level,
            stream_fn=stream_fn,
            retry=retry,
            env=env,
            callbacks=callbacks,
            session_id=session_id,
        )
        history_text = (
            history_result.text
            if history_result is not None
            else (preparation.previousSummary if preparation.previousSummary is not None else "No prior history.")
        )
        summary = f"{history_text}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_result.text}"
        summary_usage = (
            combine_usage(history_result.usage, turn_prefix_result.usage)
            if history_result is not None
            else turn_prefix_result.usage
        )
    else:
        summary_result = await generate_summary_with_usage(
            preparation.messagesToSummarize,
            model,
            preparation.settings.reserveTokens,
            api_key,
            headers,
            signal,
            custom_instructions,
            preparation.previousSummary,
            thinking_level,
            stream_fn=stream_fn,
            retry=retry,
            env=env,
            callbacks=callbacks,
            session_id=session_id,
        )
        summary = summary_result.text
        summary_usage = summary_result.usage

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
        usage=summary_usage,
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


def _get_messages_from_projected_entry_for_compaction(entry: SessionProjectionEntry) -> list[AgentMessage]:
    """Extract the AgentMessages an entry contributes to compaction. Empty for entries that
    don't contribute to LLM context."""
    if _entry_field(entry.sourceEntry, "type") == "compaction":
        return []
    # System messages are prompt state, not conversation; the compaction entry carries their replay.
    return [message for message in entry.messages if read_field(message, "role") != "system"]


def _tool_payload(tool: Any) -> Any:
    schema = getattr(tool, "parameters_json_schema", None)
    if callable(schema) and hasattr(tool, "model_dump"):
        payload = tool.model_dump(exclude={"parameters"})
        payload["parameters"] = schema()
        return payload
    return tool.model_dump() if hasattr(tool, "model_dump") else tool


def _find_valid_cut_points(
    entries: list[SessionEntry],
    start_index: int,
    end_index: int,
    projections: dict[int, list[AgentMessage]] | None = None,
) -> list[int]:
    cut_points: list[int] = []
    for index in range(start_index, end_index):
        if _entry_field(entries[index], "type") == "compaction":
            continue
        messages = _projected_messages(entries, index, projections)
        if any(read_field(message, "role") in _CUT_POINT_ROLES for message in messages):
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
    retry: RetryPolicy | None = None,
    env: dict[str, str] | None = None,
    callbacks: RetryCallbacks | None = None,
    session_id: str | None = None,
) -> SummaryWithUsage:
    max_tokens = min(
        math.floor(0.5 * reserve_tokens),
        model.maxTokens if model.maxTokens > 0 else math.inf,
    )
    conversation_text = _serialize_conversation(convertToLlm(messages))
    prompt_text = f"# Conversation\n{conversation_text}\n\n# Instructions\n{_TURN_PREFIX_SUMMARIZATION_PROMPT}"
    response = await complete_summarization(
        model,
        _build_summarization_context(prompt_text),
        _create_summarization_options(
            model,
            int(max_tokens),
            api_key,
            headers,
            signal,
            thinking_level,
            env,
            session_id,
        ),
        stream_fn,
        retry,
        callbacks,
    )
    failure = get_summarization_failure(response, "Turn prefix summarization")
    if failure:
        raise RuntimeError(failure)
    if any(read_field(block, "type") == "toolCall" for block in response.content):
        raise RuntimeError("Turn prefix summarization attempted to call a tool")
    return SummaryWithUsage(text=_assistant_text(response), usage=response.usage)


def _assistant_usage(message: Any) -> Usage | dict[str, Any] | None:
    if read_field(message, "role") != "assistant":
        return None
    if read_field(message, "stopReason") in {"aborted", "error"}:
        return None
    usage = read_field(message, "usage")
    return usage if usage is not None and calculate_context_tokens(usage) > 0 else None


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


calculateContextTokens = calculate_context_tokens
completeSummarization = complete_summarization
estimateContextTokens = estimate_context_tokens
estimateTokens = estimate_tokens
estimateProjectedContextTokens = estimate_projected_context_tokens
findCutPoint = find_cut_point
findTurnStartIndex = find_turn_start_index
getLastAssistantUsage = get_last_assistant_usage
getSummarizationFailure = get_summarization_failure
prepareCompaction = prepare_compaction
shouldCompact = should_compact


__all__ = [
    "DEFAULT_COMPACTION_SETTINGS",
    "CompactionDetails",
    "CompactionPreparation",
    "CompactionResult",
    "CompactionSettings",
    "ContextUsageEstimate",
    "CutPointResult",
    "SummaryWithUsage",
    "calculateContextTokens",
    "calculate_context_tokens",
    "compact",
    "completeSummarization",
    "complete_summarization",
    "estimateContextTokens",
    "estimateProjectedContextTokens",
    "estimateTokens",
    "estimate_context_tokens",
    "estimate_projected_context_tokens",
    "estimate_tokens",
    "findCutPoint",
    "findTurnStartIndex",
    "find_cut_point",
    "find_turn_start_index",
    "generateSummary",
    "generateSummaryWithUsage",
    "generate_summary",
    "generate_summary_with_usage",
    "getLastAssistantUsage",
    "getSummarizationFailure",
    "get_last_assistant_usage",
    "get_summarization_failure",
    "prepareCompaction",
    "prepare_compaction",
    "shouldCompact",
    "should_compact",
]
