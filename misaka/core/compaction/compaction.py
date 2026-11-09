"""Translated from pi's ``packages/coding-agent/src/core/compaction/compaction.ts`` (v1.1.0).

Context compaction for long sessions.

Pure functions for compaction logic. The session manager handles I/O,
and after compaction the session is reloaded.

MISAKA forks:
- A context engine (the LCM plugin) may replace the whole context: ``CompactionResult.contextMessages`` and
  ``contextSummaries``, and a later native compaction summarizes that view (``prepareCompaction``).
- ``findCutPoint`` computes each entry's context projection once and shares it across its passes.
"""

from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.agent.types import AgentMessage, StreamFn, ThinkingLevel
from misaka.ai.compat import completeSimple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    TranscriptContext,
    Usage,
    UserMessage,
)
from misaka.ai.utils.retry import RetryCallbacks, RetryPolicy, retryAssistantCall
from misaka.ai.utils.text import contentText
from misaka.ai.utils.transcript import getCurrentSystemMessage, normalizeContext
from misaka.ai.utils.uuid import uuidv7
from misaka.core.compaction.utils import (
    SUMMARIZATION_SYSTEM_PROMPT,
    FileOperations,
    computeFileLists,
    createFileOps,
    extractFileOpsFromMessage,
    formatFileOperations,
    serializeConversation,
)
from misaka.core.messages import convertToLlm
from misaka.core.session_manager import (
    SessionEntry,
    SessionProjection,
    SessionProjectionEntry,
    buildSessionProjection,
    sessionEntryToContextMessages,
)
from misaka.core.usage_totals import combineUsage
from misaka.utils.js import jsLength, jsonStringify
from misaka.utils.values import read_field

# ============================================================================
# File Operation Tracking
# ============================================================================


class CompactionDetails(TypedDict):
    """Details stored in CompactionEntry.details for file tracking"""

    readFiles: list[str]
    modifiedFiles: list[str]


def extractFileOperations(
    messages: list[AgentMessage],
    entries: list[SessionEntry],
    prevCompactionIndex: int,
) -> FileOperations:
    """Extract file operations from messages and previous compaction entries."""
    fileOps = createFileOps()

    # Collect from previous compaction's details (if pi-generated)
    if prevCompactionIndex >= 0:
        prevCompaction = entries[prevCompactionIndex]
        if not _entryField(prevCompaction, "fromHook") and _entryField(prevCompaction, "details"):
            # fromHook field kept for session file compatibility
            details = _entryField(prevCompaction, "details")
            readFiles = read_field(details, "readFiles")
            modifiedFiles = read_field(details, "modifiedFiles")
            if isinstance(readFiles, list):
                for f in readFiles:
                    fileOps.read.add(f)
            if isinstance(modifiedFiles, list):
                for f in modifiedFiles:
                    fileOps.edited.add(f)

    # Extract from tool calls in messages
    for msg in messages:
        extractFileOpsFromMessage(msg, fileOps)

    return fileOps


# ============================================================================
# Message Extraction
# ============================================================================


def getMessagesFromProjectedEntryForCompaction(entry: SessionProjectionEntry) -> list[AgentMessage]:
    """Extract AgentMessage from an entry if it produces one.
    Returns undefined for entries that don't contribute to LLM context.
    """
    if _entryField(entry.sourceEntry, "type") == "compaction":
        return []
    # System messages are prompt state, not conversation; the compaction entry carries their replay.
    return [message for message in entry.messages if read_field(message, "role") != "system"]


@dataclass(slots=True)
class CompactionResult:
    """Result from compact() - SessionManager adds uuid/parentUuid when saving"""

    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    estimatedTokensAfter: int | None = None
    # Usage from the LLM call(s) that generated this summary, if available
    usage: Usage | dict[str, Any] | None = None
    # Extension-specific data (e.g., ArtifactIndex, version markers for structured compaction)
    details: Any | None = None
    # MISAKA fork: a context engine's whole-context replacement. None keeps pi's prefix/tail form; even [] is a
    # complete engine-owned view.
    contextMessages: list[AgentMessage] | None = None
    # MISAKA fork: indexes into contextMessages of what the engine wrote itself (its summaries) rather
    # than kept from the transcript -- shown as compaction summaries, not as turns someone took.
    contextSummaries: list[int] | None = None


# ============================================================================
# Types
# ============================================================================


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

# ============================================================================
# Token calculation
# ============================================================================


def calculateContextTokens(usage: Usage | dict[str, Any]) -> int:
    """Calculate total context tokens from usage.
    Uses the native totalTokens field when available, falls back to computing from components.
    """
    return read_field(usage, "totalTokens") or (
        (read_field(usage, "input") or 0)
        + (read_field(usage, "output") or 0)
        + (read_field(usage, "cacheRead") or 0)
        + (read_field(usage, "cacheWrite") or 0)
    )


def getAssistantUsage(msg: Any) -> Usage | dict[str, Any] | None:
    """Get usage from an assistant message if available.
    Skips aborted, error, and all-zero usage messages as they don't have valid usage data.
    """
    if read_field(msg, "role") == "assistant":
        usage = read_field(msg, "usage")
        if (
            read_field(msg, "stopReason") != "aborted"
            and read_field(msg, "stopReason") != "error"
            and usage
            and calculateContextTokens(usage) > 0
        ):
            return usage
    return None


def getLastAssistantUsage(entries: list[SessionEntry]) -> Usage | dict[str, Any] | None:
    """Find the last valid assistant message usage from session entries."""
    for entry in reversed(entries):
        if _entryField(entry, "type") == "message":
            usage = getAssistantUsage(_entryField(entry, "message"))
            if usage:
                return usage
    return None


@dataclass(slots=True)
class ContextUsageEstimate:
    tokens: int
    usageTokens: int
    trailingTokens: int
    lastUsageIndex: int | None


def getLastAssistantUsageInfo(messages: list[AgentMessage]) -> dict[str, Any] | None:
    for i in range(len(messages) - 1, -1, -1):
        usage = getAssistantUsage(messages[i])
        if usage:
            return {"usage": usage, "index": i}
    return None


def estimateContextTokens(messages: list[AgentMessage]) -> ContextUsageEstimate:
    """Estimate context tokens from messages, using the last assistant usage when available.
    If there are messages after the last usage, estimate their tokens with estimateTokens.
    """
    usageInfo = getLastAssistantUsageInfo(messages)

    if not usageInfo:
        estimated = 0
        for message in messages:
            estimated += estimateTokens(message)
        return ContextUsageEstimate(tokens=estimated, usageTokens=0, trailingTokens=estimated, lastUsageIndex=None)

    usageTokens = calculateContextTokens(usageInfo["usage"])
    trailingTokens = 0
    for i in range(usageInfo["index"] + 1, len(messages)):
        trailingTokens += estimateTokens(messages[i])

    return ContextUsageEstimate(
        tokens=usageTokens + trailingTokens,
        usageTokens=usageTokens,
        trailingTokens=trailingTokens,
        lastUsageIndex=usageInfo["index"],
    )


def estimateProjectedContextTokens(projection: SessionProjection, branchEntries: list[SessionEntry]) -> ContextUsageEstimate:
    """Estimate projected context without trusting usage captured before a later edit or compaction."""
    estimate = estimateContextTokens(projection.messages)
    if estimate.lastUsageIndex is not None:
        projectedMessageIndex = 0
        usageEntryId: str | None = None
        for entry in projection.entries:
            nextMessageIndex = projectedMessageIndex + len(entry.messages)
            if estimate.lastUsageIndex < nextMessageIndex:
                usageEntryId = _entryField(entry.sourceEntry, "id")
                break
            projectedMessageIndex = nextMessageIndex

        usageEntryIndex = (
            next((i for i, entry in enumerate(branchEntries) if _entryField(entry, "id") == usageEntryId), -1)
            if usageEntryId
            else -1
        )
        latestInvalidatingEntryIndex = -1
        for i in range(len(branchEntries) - 1, -1, -1):
            entry = branchEntries[i]
            if _entryField(entry, "type") == "context_edit" or _entryField(entry, "type") == "compaction":
                latestInvalidatingEntryIndex = i
                break
        if usageEntryIndex > latestInvalidatingEntryIndex:
            return estimate

    currentSystem = getCurrentSystemMessage(projection.messages)
    tokens = estimateTokens(currentSystem) if currentSystem else 0
    for message in projection.messages:
        if read_field(message, "role") != "system":
            tokens += estimateTokens(message)
    return ContextUsageEstimate(tokens=tokens, usageTokens=0, trailingTokens=tokens, lastUsageIndex=None)


def shouldCompact(contextTokens: int, contextWindow: int, settings: CompactionSettings) -> bool:
    """Check if compaction should trigger based on context usage."""
    if not settings.enabled:
        return False
    return contextTokens > contextWindow - settings.reserveTokens


# ============================================================================
# Cut point detection
# ============================================================================

ESTIMATED_IMAGE_CHARS = 4800


def estimateTextAndImageContentChars(content: Any) -> int:
    if isinstance(content, str):
        return jsLength(content)

    chars = 0
    for block in content or []:
        text = read_field(block, "text")
        if read_field(block, "type") == "text" and text:
            chars += jsLength(text)
        elif read_field(block, "type") == "image":
            chars += ESTIMATED_IMAGE_CHARS
    return chars


def _toolPayload(tool: Any) -> Any:
    """A tool as ``JSON.stringify`` writes it: its fields, with the parameters as their JSON schema."""
    schema = getattr(tool, "parameters_json_schema", None)
    if callable(schema) and hasattr(tool, "model_dump"):
        payload = tool.model_dump(exclude={"parameters"})
        payload["parameters"] = schema()
        return payload
    return tool.model_dump() if hasattr(tool, "model_dump") else tool


def estimateTokens(message: AgentMessage) -> int:
    """Estimate token count for a message using chars/4 heuristic.
    This is conservative (overestimates tokens).
    """
    chars = 0

    match read_field(message, "role"):
        case "system":
            chars = estimateTextAndImageContentChars(read_field(message, "content"))
            sections = read_field(message, "sections")
            if sections:
                for section in sections.values():
                    if section:
                        chars += jsLength(section)
            toolsAdded = read_field(message, "toolsAdded")
            if toolsAdded:
                chars += jsLength(jsonStringify([_toolPayload(tool) for tool in toolsAdded]))
            return math.ceil(chars / 4)
        case "user":
            chars = estimateTextAndImageContentChars(read_field(message, "content"))
            return math.ceil(chars / 4)
        case "assistant":
            for block in read_field(message, "content") or []:
                match read_field(block, "type"):
                    case "text":
                        chars += jsLength(read_field(block, "text") or "")
                    case "thinking":
                        chars += jsLength(read_field(block, "thinking") or "")
                    case "toolCall":
                        chars += jsLength(read_field(block, "name") or "") + jsLength(
                            jsonStringify(read_field(block, "arguments"))
                        )
            return math.ceil(chars / 4)
        case "custom" | "toolResult":
            chars = estimateTextAndImageContentChars(read_field(message, "content"))
            return math.ceil(chars / 4)
        case "bashExecution":
            chars = jsLength(read_field(message, "command") or "") + jsLength(read_field(message, "output") or "")
            return math.ceil(chars / 4)
        case "branchSummary" | "compactionSummary":
            chars = jsLength(read_field(message, "summary") or "")
            return math.ceil(chars / 4)

    return 0


def isCutPointMessage(message: AgentMessage) -> bool:
    match read_field(message, "role"):
        case "user" | "assistant" | "bashExecution" | "custom" | "branchSummary" | "compactionSummary":
            return True
        case "toolResult":
            return False
    return False


def isTurnStartMessage(message: AgentMessage) -> bool:
    match read_field(message, "role"):
        case "user" | "bashExecution" | "custom" | "branchSummary" | "compactionSummary":
            return True
        case "assistant" | "toolResult":
            return False
    return False


def isTurnStartEntry(entry: SessionEntry) -> bool:
    if _entryField(entry, "type") == "compaction":
        return False
    return any(isTurnStartMessage(message) for message in sessionEntryToContextMessages(entry))


def findValidCutPoints(entries: list[SessionEntry], startIndex: int, endIndex: int) -> list[int]:
    """Find valid cut points: indices of context-visible user-like or assistant messages.
    Never cut at tool results (they must follow their tool call).
    When we cut at an assistant message with tool calls, its tool results follow it
    and will be kept.
    """
    cutPoints: list[int] = []
    for i in range(startIndex, endIndex):
        entry = entries[i]
        if _entryField(entry, "type") == "compaction":
            continue
        if any(isCutPointMessage(message) for message in sessionEntryToContextMessages(entry)):
            cutPoints.append(i)
    return cutPoints


def findTurnStartIndex(entries: list[SessionEntry], entryIndex: int, startIndex: int) -> int:
    """Find the context-visible user-role message that starts the turn containing the given entry index.
    Returns -1 if no turn start found before the index.
    """
    for i in range(entryIndex, startIndex - 1, -1):
        if isTurnStartEntry(entries[i]):
            return i
    return -1


@dataclass(slots=True)
class CutPointResult:
    # Index of first entry to keep
    firstKeptEntryIndex: int
    # Index of user message that starts the turn being split, or -1 if not splitting
    turnStartIndex: int
    # Whether this cut splits a turn (cut point is not a user message)
    isSplitTurn: bool


def findCutPoint(
    entries: list[SessionEntry],
    startIndex: int,
    endIndex: int,
    keepRecentTokens: int,
) -> CutPointResult:
    """Find the cut point in session entries that keeps approximately `keepRecentTokens`.

    Algorithm: Walk backwards from newest, accumulating estimated message sizes.
    Stop when we've accumulated >= keepRecentTokens. Cut at that point.

    Can cut at user OR assistant messages (never tool results). When cutting at an
    assistant message with tool calls, its tool results come after and will be kept.

    Returns CutPointResult with:
    - firstKeptEntryIndex: the entry index to start keeping from
    - turnStartIndex: if cutting mid-turn, the user message that started that turn
    - isSplitTurn: whether we're cutting in the middle of a turn

    Only considers entries between `startIndex` and `endIndex` (exclusive).
    """
    cutPoints = findValidCutPoints(entries, startIndex, endIndex)

    if len(cutPoints) == 0:
        return CutPointResult(firstKeptEntryIndex=startIndex, turnStartIndex=-1, isSplitTurn=False)

    # Walk backwards from newest, accumulating estimated message sizes
    accumulatedTokens = 0
    cutIndex = cutPoints[0]  # Default: keep from first message (not header)

    for i in range(endIndex - 1, startIndex - 1, -1):
        entry = entries[i]
        messageTokens = sum(estimateTokens(message) for message in sessionEntryToContextMessages(entry))
        if messageTokens == 0:
            continue
        accumulatedTokens += messageTokens

        # Check if we've exceeded the budget
        if accumulatedTokens >= keepRecentTokens:
            # Prefer the closest valid cut point at or after this entry. If trailing
            # tool results exceed the budget by themselves, keep their preceding
            # assistant tool call instead of falling back to the first message.
            cutIndex = next((candidate for candidate in cutPoints if candidate >= i), cutPoints[-1])
            break

    # Scan backwards from cutIndex to include adjacent metadata entries that do not affect context.
    while cutIndex > startIndex:
        prevEntry = entries[cutIndex - 1]
        # Stop at compaction boundaries or context-visible entries.
        if _entryField(prevEntry, "type") == "compaction" or len(sessionEntryToContextMessages(prevEntry)) > 0:
            break
        cutIndex -= 1

    # Determine if this is a split turn
    cutEntry = entries[cutIndex]
    startsTurn = isTurnStartEntry(cutEntry)
    turnStartIndex = -1 if startsTurn else findTurnStartIndex(entries, cutIndex, startIndex)

    return CutPointResult(
        firstKeptEntryIndex=cutIndex,
        turnStartIndex=turnStartIndex,
        isSplitTurn=not startsTurn and turnStartIndex != -1,
    )


# ============================================================================
# Summarization
# ============================================================================

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

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

UPDATE_SUMMARIZATION_INSTRUCTIONS = """Update the existing structured summary with new information. RULES:
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

UPDATE_SUMMARIZATION_PROMPT = f"""The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

{UPDATE_SUMMARIZATION_INSTRUCTIONS}"""


def getSummarizationFailure(response: AssistantMessage, label: str) -> str | None:
    """Returns an error message when a summarization response cannot safely be persisted.
    A length stop contains partial text and must not become a session checkpoint.
    """
    if response.stopReason == "error":
        return f"{label} failed: {response.errorMessage or 'Unknown error'}"
    if response.stopReason == "length":
        return f"{label} failed: generation hit the token cap and the summary is incomplete"
    return None


def createSummarizationOptions(
    model: Model[Any],
    maxTokens: int,
    apiKey: str | None,
    headers: dict[str, str] | None,
    env: dict[str, str] | None,
    signal: Any | None,
    thinkingLevel: ThinkingLevel | None,
    sessionId: str | None,
) -> SimpleStreamOptions:
    options = SimpleStreamOptions(maxTokens=maxTokens, signal=signal, apiKey=apiKey, headers=headers, env=env, sessionId=sessionId)
    if model.reasoning and thinkingLevel and thinkingLevel != "off":
        options.reasoning = thinkingLevel
    return options


async def completeSummarization(
    model: Model[Any],
    context: TranscriptContext | dict[str, Any],
    options: SimpleStreamOptions,
    streamFn: StreamFn | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
) -> AssistantMessage:
    """Shared choke point for every compaction/branch-summary summarization call. Wraps the
    single LLM call in retryAssistantCall so transient stream drops (e.g.
    `terminated`, socket close) honor the configured retry policy instead of failing
    the whole compaction on the first attempt. Deterministic errors and aborts return
    immediately (see retryAssistantCall).
    """
    # Avoid cache writes for one-off summaries. Reuse caller-supplied routing when available;
    # callers without a session ID, including branch summaries, receive a fresh routing ID.
    requestOptions = options.model_copy(
        update={"cacheRetention": "none", "sessionId": options.sessionId if options.sessionId is not None else uuidv7()}
    )

    async def produce() -> AssistantMessage:
        if streamFn is None:
            return await completeSimple(model, context, requestOptions)
        stream = streamFn(model, context, requestOptions)
        if inspect.isawaitable(stream):
            stream = await stream
        return await stream.result()

    return await retryAssistantCall(produce, retry, requestOptions.signal, callbacks)


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
    """Generate a summary of the conversation using the LLM.
    If previousSummary is provided, uses the update prompt to merge.
    """
    return (
        await generateSummaryWithUsage(
            currentMessages,
            model,
            reserveTokens,
            apiKey,
            headers,
            signal,
            customInstructions,
            previousSummary,
            thinkingLevel,
            streamFn,
            env,
            retry,
            callbacks,
            sessionId,
        )
    ).text


def buildSummarizationContext(promptText: str) -> TranscriptContext:
    """Build the provider context for a standalone summary request."""
    return normalizeContext(
        Context(
            systemPrompt=SUMMARIZATION_SYSTEM_PROMPT,
            messages=[UserMessage(content=[{"type": "text", "text": promptText}], timestamp=int(time.time() * 1000))],
        )
    )


@dataclass(slots=True)
class SummaryWithUsage:
    """``{ text: string; usage: Usage }``."""

    text: str
    usage: Usage


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
    """Generate or update a conversation summary and return its provider usage."""
    maxTokens = min(math.floor(0.8 * reserveTokens), model.maxTokens if model.maxTokens > 0 else math.inf)

    # Use update prompt if we have a previous summary, otherwise initial prompt
    basePrompt = UPDATE_SUMMARIZATION_PROMPT if previousSummary else SUMMARIZATION_PROMPT
    if customInstructions:
        basePrompt = f"{basePrompt}\n\nAdditional focus: {customInstructions}"

    # Serialize conversation to text so model doesn't try to continue it
    # Convert to LLM messages first (handles custom types like bashExecution, custom, etc.)
    llmMessages = convertToLlm(currentMessages)
    conversationText = serializeConversation(llmMessages)

    # Build the prompt with conversation wrapped in tags
    promptText = f"<conversation>\n{conversationText}\n</conversation>\n\n"
    if previousSummary:
        promptText += f"<previous-summary>\n{previousSummary}\n</previous-summary>\n\n"
    promptText += basePrompt

    completionOptions = createSummarizationOptions(
        model, int(maxTokens), apiKey, headers, env, signal, thinkingLevel, sessionId
    )

    response = await completeSummarization(
        model, buildSummarizationContext(promptText), completionOptions, streamFn, retry, callbacks
    )

    failure = getSummarizationFailure(response, "Summarization")
    if failure:
        raise RuntimeError(failure)
    if any(read_field(block, "type") == "toolCall" for block in response.content):
        raise RuntimeError("Summarization attempted to call a tool")

    textContent = contentText(response.content)

    return SummaryWithUsage(text=textContent, usage=response.usage)


# ============================================================================
# Compaction Preparation (for extensions)
# ============================================================================


@dataclass(slots=True)
class CompactionPreparation:
    # UUID of first entry to keep
    firstKeptEntryId: str
    # Messages that will be summarized and discarded
    messagesToSummarize: list[AgentMessage]
    # Messages that will be turned into turn prefix summary (if splitting)
    turnPrefixMessages: list[AgentMessage]
    # Whether this is a split turn (cut point in middle of turn)
    isSplitTurn: bool
    tokensBefore: int
    # Summary from previous compaction, for iterative update
    previousSummary: str | None
    # File operations extracted from messagesToSummarize
    fileOps: FileOperations
    # Compaction settions from settings.jsonl
    settings: CompactionSettings


def isProjectedTurnStart(entry: SessionProjectionEntry) -> bool:
    if _entryField(entry.sourceEntry, "type") == "compaction":
        return False
    return any(isTurnStartMessage(message) for message in entry.messages)


def findProjectedTurnStartIndex(entries: list[SessionProjectionEntry], entryIndex: int, startIndex: int) -> int:
    for i in range(entryIndex, startIndex - 1, -1):
        if isProjectedTurnStart(entries[i]):
            return i
    return -1


def findProjectedCutPoint(
    entries: list[SessionProjectionEntry],
    startIndex: int,
    endIndex: int,
    keepRecentTokens: int,
) -> CutPointResult:
    cutPoints: list[int] = []
    for i in range(startIndex, endIndex):
        entry = entries[i]
        if _entryField(entry.sourceEntry, "type") != "compaction" and any(isCutPointMessage(message) for message in entry.messages):
            cutPoints.append(i)
    if len(cutPoints) == 0:
        return CutPointResult(firstKeptEntryIndex=startIndex, turnStartIndex=-1, isSplitTurn=False)

    accumulatedTokens = 0
    exceededBudget = False
    cutIndex = cutPoints[0]
    for i in range(endIndex - 1, startIndex - 1, -1):
        messageTokens = sum(estimateTokens(message) for message in entries[i].messages)
        if messageTokens == 0:
            continue
        accumulatedTokens += messageTokens
        if accumulatedTokens >= keepRecentTokens:
            exceededBudget = True
            cutIndex = next((candidate for candidate in cutPoints if candidate >= i), cutPoints[-1])
            break

    # A recovery attempt and its omission edits are context-invisible after the last
    # visible input. Advance only for a closed suffix containing an omitted assistant
    # attempt; arbitrary metadata must not move the cut past unsent input.
    suffix = entries[cutIndex + 1 : endIndex]

    def isIntrinsicallyVisible(entry: SessionProjectionEntry) -> bool:
        return _entryField(entry.sourceEntry, "type") != "context_edit" and len(sessionEntryToContextMessages(entry.sourceEntry)) > 0

    def isOmitted(entry: SessionProjectionEntry) -> bool:
        return isIntrinsicallyVisible(entry) and len(entry.messages) == 0

    omittedSuffixIds = {_entryField(entry.sourceEntry, "id") for entry in suffix if isOmitted(entry)}
    hasExternalReplacement = any(
        _entryField(entry.sourceEntry, "type") == "context_edit"
        and _entryField(entry.sourceEntry, "replacement") is not None
        and _entryField(entry.sourceEntry, "targetId") not in omittedSuffixIds
        for entry in suffix
    )
    isRecoveryOmissionSuffix = (
        exceededBudget
        and not hasExternalReplacement
        and any(
            _entryField(entry.sourceEntry, "type") == "message"
            and read_field(_entryField(entry.sourceEntry, "message"), "role") == "assistant"
            and isOmitted(entry)
            for entry in suffix
        )
        and all(
            _entryField(entry.sourceEntry, "type") != "compaction" and (not isIntrinsicallyVisible(entry) or isOmitted(entry))
            for entry in suffix
        )
    )
    if isRecoveryOmissionSuffix:
        cutIndex += 1

    while cutIndex > startIndex:
        previous = entries[cutIndex - 1]
        if _entryField(previous.sourceEntry, "type") == "compaction" or len(previous.messages) > 0:
            break
        cutIndex -= 1
    startsTurn = isProjectedTurnStart(entries[cutIndex])
    turnStartIndex = -1 if startsTurn else findProjectedTurnStartIndex(entries, cutIndex, startIndex)
    return CutPointResult(
        firstKeptEntryIndex=cutIndex,
        turnStartIndex=turnStartIndex,
        isSplitTurn=not startsTurn and turnStartIndex != -1,
    )


def prepareCompaction(
    pathEntries: list[SessionEntry],
    settings: CompactionSettings,
) -> CompactionPreparation | None:
    if len(pathEntries) > 0 and _entryField(pathEntries[-1], "type") == "compaction":
        return None

    projection = buildSessionProjection(pathEntries)
    projectedEntries = projection.entries
    sourceEntries = [entry.sourceEntry for entry in projectedEntries]
    # The newest compaction is projected first. Older compaction entries can still
    # occur in its retained raw range, but their projected contribution is empty.
    prevCompactionIndex = next(
        (
            i
            for i, entry in enumerate(projectedEntries)
            if _entryField(entry.sourceEntry, "type") == "compaction" and len(entry.messages) > 0
        ),
        -1,
    )

    previousSummary: str | None = None
    # MISAKA fork: the messages of a context engine's checkpoint, summarized ahead of the rest.
    checkpointMessages: list[AgentMessage] = []
    boundaryStart = 0
    if prevCompactionIndex >= 0:
        previousCompaction = projectedEntries[prevCompactionIndex].sourceEntry
        previousSummary = _entryField(previousCompaction, "summary")
        # The canonical projection has already selected the previous compaction's retained tail.
        boundaryStart = prevCompactionIndex + 1
        if _entryField(previousCompaction, "contextMessages") is not None:
            # MISAKA fork: a complete checkpoint (the LCM plugin's whole-context replacement)
            # may still contain raw backlog and a changed tail. On a native compaction,
            # summarize that view, never its display label or the pre-checkpoint archive
            # that it already replaced.
            checkpointMessages = [
                message
                for message in projectedEntries[prevCompactionIndex].messages
                if read_field(message, "role") != "system"
            ]
            previousSummary = None
    boundaryEnd = len(projectedEntries)
    tokensBefore = estimateProjectedContextTokens(projection, pathEntries).tokens
    cutPoint = findProjectedCutPoint(projectedEntries, boundaryStart, boundaryEnd, settings.keepRecentTokens)

    firstKeptEntry = (
        projectedEntries[cutPoint.firstKeptEntryIndex].sourceEntry
        if cutPoint.firstKeptEntryIndex < len(projectedEntries)
        else None
    )
    if not firstKeptEntry or not _entryField(firstKeptEntry, "id"):
        return None
    firstKeptEntryId = _entryField(firstKeptEntry, "id")
    historyEnd = cutPoint.turnStartIndex if cutPoint.isSplitTurn else cutPoint.firstKeptEntryIndex

    messagesToSummarize = [
        *checkpointMessages,
        *(
            message
            for entry in projectedEntries[boundaryStart:historyEnd]
            for message in getMessagesFromProjectedEntryForCompaction(entry)
        ),
    ]
    turnPrefixMessages = (
        [
            message
            for entry in projectedEntries[cutPoint.turnStartIndex : cutPoint.firstKeptEntryIndex]
            for message in getMessagesFromProjectedEntryForCompaction(entry)
        ]
        if cutPoint.isSplitTurn
        else []
    )

    if len(messagesToSummarize) == 0 and len(turnPrefixMessages) == 0:
        return None

    # Extract file operations from edited model-visible messages and the previous compaction.
    fileOps = extractFileOperations(messagesToSummarize, sourceEntries, prevCompactionIndex)

    # Also extract file ops from turn prefix if splitting
    if cutPoint.isSplitTurn:
        for msg in turnPrefixMessages:
            extractFileOpsFromMessage(msg, fileOps)

    return CompactionPreparation(
        firstKeptEntryId=firstKeptEntryId,
        messagesToSummarize=messagesToSummarize,
        turnPrefixMessages=turnPrefixMessages,
        isSplitTurn=cutPoint.isSplitTurn,
        tokensBefore=tokensBefore,
        previousSummary=previousSummary,
        fileOps=fileOps,
        settings=settings,
    )


# ============================================================================
# Main compaction function
# ============================================================================

TURN_PREFIX_SUMMARIZATION_PROMPT = """The messages above are earlier context from an ongoing conversation. Later messages are stored separately and do not need to be reconstructed.

Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed before the later messages so the conversation can continue with the necessary context.

## Original Request
[What did the user ask for?]

## Progress So Far
- [Key decisions and work completed in these messages]

## Context Needed to Continue
- [Information from these messages needed to understand the later work]

Only summarize information explicitly present above. Do not infer or recreate later messages."""


async def compact(
    preparation: CompactionPreparation,
    model: Model[Any],
    apiKey: str | None,
    headers: dict[str, str] | None = None,
    customInstructions: str | None = None,
    signal: Any | None = None,
    thinkingLevel: ThinkingLevel | None = None,
    streamFn: StreamFn | None = None,
    env: dict[str, str] | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
    sessionId: str | None = None,
) -> CompactionResult:
    """Generate summaries for compaction using prepared data.
    Returns CompactionResult - SessionManager adds uuid/parentUuid when saving.

    preparation: Pre-calculated preparation from prepareCompaction()
    customInstructions: Optional custom focus for the summary
    sessionId: Optional routing session ID forwarded without enabling prompt caching
    """
    firstKeptEntryId = preparation.firstKeptEntryId
    messagesToSummarize = preparation.messagesToSummarize
    turnPrefixMessages = preparation.turnPrefixMessages
    isSplitTurn = preparation.isSplitTurn
    tokensBefore = preparation.tokensBefore
    previousSummary = preparation.previousSummary
    fileOps = preparation.fileOps
    settings = preparation.settings

    # Generate summaries and merge into one
    if isSplitTurn and len(turnPrefixMessages) > 0:
        historyText = previousSummary if previousSummary is not None else "No prior history."
        historyUsage: Usage | None = None
        if len(messagesToSummarize) > 0:
            historyResult = await generateSummaryWithUsage(
                messagesToSummarize,
                model,
                settings.reserveTokens,
                apiKey,
                headers,
                signal,
                customInstructions,
                previousSummary,
                thinkingLevel,
                streamFn,
                env,
                retry,
                callbacks,
                sessionId,
            )
            historyText = historyResult.text
            historyUsage = historyResult.usage
        turnPrefixResult = await generateTurnPrefixSummary(
            turnPrefixMessages,
            model,
            settings.reserveTokens,
            apiKey,
            headers,
            env,
            signal,
            thinkingLevel,
            streamFn,
            retry,
            callbacks,
            sessionId,
        )
        # Merge into single summary
        summary = f"{historyText}\n\n---\n\n**Turn Context (split turn):**\n\n{turnPrefixResult.text}"
        summaryUsage = combineUsage(historyUsage, turnPrefixResult.usage) if historyUsage else turnPrefixResult.usage
    else:
        # Just generate history summary
        result = await generateSummaryWithUsage(
            messagesToSummarize,
            model,
            settings.reserveTokens,
            apiKey,
            headers,
            signal,
            customInstructions,
            previousSummary,
            thinkingLevel,
            streamFn,
            env,
            retry,
            callbacks,
            sessionId,
        )
        summary = result.text
        summaryUsage = result.usage

    # Compute file lists and append to summary
    fileLists = computeFileLists(fileOps)
    readFiles, modifiedFiles = fileLists["readFiles"], fileLists["modifiedFiles"]
    summary += formatFileOperations(readFiles, modifiedFiles)

    if not firstKeptEntryId:
        raise RuntimeError("First kept entry has no UUID - session may need migration")

    return CompactionResult(
        summary=summary,
        firstKeptEntryId=firstKeptEntryId,
        tokensBefore=tokensBefore,
        usage=summaryUsage,
        details={"readFiles": readFiles, "modifiedFiles": modifiedFiles},
    )


async def generateTurnPrefixSummary(
    messages: list[AgentMessage],
    model: Model[Any],
    reserveTokens: int,
    apiKey: str | None,
    headers: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    signal: Any | None = None,
    thinkingLevel: ThinkingLevel | None = None,
    streamFn: StreamFn | None = None,
    retry: RetryPolicy | None = None,
    callbacks: RetryCallbacks | None = None,
    sessionId: str | None = None,
) -> SummaryWithUsage:
    """Generate a summary for a turn prefix (when splitting a turn)."""
    maxTokens = min(
        math.floor(0.5 * reserveTokens), model.maxTokens if model.maxTokens > 0 else math.inf
    )  # Smaller budget for turn prefix
    llmMessages = convertToLlm(messages)
    conversationText = serializeConversation(llmMessages)
    promptText = f"# Conversation\n{conversationText}\n\n# Instructions\n{TURN_PREFIX_SUMMARIZATION_PROMPT}"

    response = await completeSummarization(
        model,
        buildSummarizationContext(promptText),
        createSummarizationOptions(model, int(maxTokens), apiKey, headers, env, signal, thinkingLevel, sessionId),
        streamFn,
        retry,
        callbacks,
    )

    failure = getSummarizationFailure(response, "Turn prefix summarization")
    if failure:
        raise RuntimeError(failure)
    if any(read_field(block, "type") == "toolCall" for block in response.content):
        raise RuntimeError("Turn prefix summarization attempted to call a tool")

    return SummaryWithUsage(text=contentText(response.content), usage=response.usage)


def _entryField(entry: Any, name: str) -> Any:
    """``entry.name`` for a session entry, a mapping or an object."""
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


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
    "compact",
    "completeSummarization",
    "estimateContextTokens",
    "estimateProjectedContextTokens",
    "estimateTokens",
    "findCutPoint",
    "findTurnStartIndex",
    "generateSummary",
    "generateSummaryWithUsage",
    "getLastAssistantUsage",
    "getSummarizationFailure",
    "prepareCompaction",
    "shouldCompact",
]
