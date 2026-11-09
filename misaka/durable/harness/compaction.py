"""The built-in compaction task: summarize an old prefix of the model context and place the summary as an entry.

Translated from pi's ``packages/durable/src/harness/compaction.ts`` (v1.3.0, spec section 8.7). A compaction input is
``{"reason", "instructions"?}``; the pinned summarization request is ``{"attempt", "model", "thinkingLevel",
"streamOptions", "maxTokens", "tail", "firstKept"}``; a checkpoint is ``{"phase": "select"}``, ``{"phase": "summarize",
**request}`` or ``{"phase": "retry", "until", **request}``.
"""

from __future__ import annotations

import math
from typing import Any

from misaka.ai.types import SimpleStreamOptions
from misaka.chord.context import Context
from misaka.utils.js import (
    awaitValue,
    jsLength,
    jsonStringify,
    jsSlice,
    jsTrim,
    nullishCoalesce,
)

from ..entries import CompactionEntry
from ..tasks import defineTask
from ..types import ConversationId, TaskId, Tx
from .ai_bridge import (
    calculateContextTokens,
    estimateMessageTokens,
    isRetryableAssistantError,
    retryDelayMs,
    toContext,
    toMessageDict,
)
from .context import orderToolResults
from .live import (
    LiveDoc,
    addCompactionStatus,
    compactionStatus,
    failCompactionStatus,
    removeCompactionStatus,
)
from .provider import ensureProviderSessionId
from .usage import recordUsage

TOOL_RESULT_MAX_CHARS = 2000
"""Longest tool result text a serialized summary source keeps."""

SUMMARY_PREFIX = "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
SUMMARY_SUFFIX = "\n</summary>"

SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work. If the conversation starts with an earlier summary, preserve its information and fold the newer messages into it.

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


async def selectPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    conversationId = runtime.conversationId
    agent = await runtime.agent(context)
    settings = runtime.settings
    ref = agent.get("model")
    model = None if ref is None else runtime.models.getModel(ref["provider"], ref["modelId"])
    if ref is None or model is None:
        return await failNoModel(runtime, ref, context)  # type: ignore[no-any-return]
    policy = settings["compaction"]
    view = await runtime.context(conversationId, context)
    cut = selectCut(view, policy["keepRecentTokens"])
    if cut is None:
        return await complete(runtime, context)  # type: ignore[no-any-return]
    firstKept = view["entries"][cut]["id"]
    reason = task["input"]["reason"]
    instructions = task["input"].get("instructions")
    compaction = {
        "reason": reason,
        "entries": view["entries"][:cut],
        "messages": summarizedMessages(view, cut),
        "firstKept": firstKept,
        **({} if instructions is None else {"instructions": instructions}),
    }
    decision: dict[str, Any] | None = None

    async def beforeCompact(hook: Any) -> None:
        nonlocal decision
        if decision is None:
            decision = await awaitValue(hook(compaction, runtime, context))

    await runtime.hooks.each("beforeCompact", beforeCompact)
    if decision is not None and "decline" in decision:
        return await complete(runtime, context)  # type: ignore[no-any-return]
    if decision is not None:
        return await place(runtime, firstKept, decision["summary"], context)  # type: ignore[no-any-return]
    request = {
        "attempt": 1,
        "model": ref,
        "thinkingLevel": agent["thinkingLevel"],
        "streamOptions": settings["stream"],
        "maxTokens": min(
            math.floor(0.8 * policy["reserveTokens"]), model.maxTokens if model.maxTokens > 0 else math.inf
        ),
        "tail": max([firstKept, *[entry["id"] for entry in view["entries"]]]),
        "firstKept": firstKept,
    }
    await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "summarize", **request}}, context)


async def summarizePhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    request = {key: value for key, value in task["state"]["checkpoint"].items() if key != "phase"}
    ref = request["model"]
    thinkingLevel = request["thinkingLevel"]
    streamOptions = request["streamOptions"]
    maxTokens = request["maxTokens"]
    tail = request["tail"]
    firstKept = request["firstKept"]
    attempt = request["attempt"]
    model = runtime.models.getModel(ref["provider"], ref["modelId"])
    if model is None:
        return await failNoModel(runtime, ref, context)  # type: ignore[no-any-return]
    # The context at ``tail`` is immutable, so this is the range ``select`` chose.
    view = await runtime.context(runtime.conversationId, context, {"at": tail})
    cut = next((index for index, entry in enumerate(view["entries"]) if entry["id"] == firstKept), -1)
    now = runtime.now()
    messages = [
        {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT, "timestamp": now},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": summaryPrompt(summarizedMessages(view, cut), task["input"].get("instructions"))}
            ],
            "timestamp": now,
        },
    ]
    forwarded = {key: value for key, value in streamOptions.items() if key != "deferred"}
    options = {
        **forwarded,
        "cacheRetention": "none",
        "maxTokens": maxTokens,
        "signal": runtime.signal,
        "sessionId": await ensureProviderSessionId(runtime, context),
        **({} if thinkingLevel == "off" else {"reasoning": thinkingLevel}),
    }
    message = toMessageDict(
        await runtime.models.completeSimple(model, toContext(messages), SimpleStreamOptions(**options))
    )
    # An abort mark or close: the abort invocation or the reopened task handles the committed state.
    runtime.signal.throwIfAborted()
    summary = summaryText(message)
    policy = runtime.settings["retry"]
    retry = (
        message["stopReason"] == "error"
        and isRetryableAssistantError(message)
        and policy["enabled"]
        and attempt <= policy["maxRetries"]
    )
    until = runtime.now() + retryDelayMs(policy, attempt) if retry else 0

    async def record(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        await recordUsage(tx, runtime.conversationId, "models", f"{message['provider']}/{message['model']}", message["usage"])
        live = await tx.doc(LiveDoc, runtime.conversationId)
        if summary is not None:
            return await placeSummary(tx, runtime, current, live, firstKept, summary)
        if retry:
            status = compactionStatus(live, runtime.taskId)
            if status is not None:
                status["retry"] = {"at": until, "error": message.get("errorMessage") or ""}
            return {"status": "running", "checkpoint": {"phase": "retry", **request, "until": until}}
        text = summaryFailure(message)
        failCompactionStatus(live, runtime.taskId, text)
        return {
            "status": "terminal",
            "outcome": {"status": "failed", "error": {"message": text, "detail": {"reason": "model_error"}}},
        }

    await runtime.commit(record, context)


async def retryPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    request = {key: value for key, value in task["state"]["checkpoint"].items() if key not in ("phase", "until")}
    until = task["state"]["checkpoint"]["until"]
    await runtime.sleep(until, context)
    attempt = request["attempt"] + 1

    async def advance(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        status = compactionStatus(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
        if status is not None:
            status["attempt"] = attempt
            if "retry" in status:
                del status["retry"]
        return {"status": "running", "checkpoint": {"phase": "summarize", **request, "attempt": attempt}}

    await runtime.commit(advance, context)


async def abortPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    async def end(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        removeCompactionStatus(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
        return {"status": "terminal", "outcome": {"status": "aborted"}}

    await runtime.commit(end, context)


CompactionTask = defineTask(
    {
        "name": "pi.compaction",
        "version": 1,
        "initial": lambda input: {"phase": "select"},
        "phases": {"select": selectPhase, "summarize": summarizePhase, "retry": retryPhase},
        "abort": abortPhase,
    }
)
"""Built-in compaction task (spec section 8.7): select an old prefix of the model context, summarize it, and place a
summary entry whose ``head`` is the first kept entry. A compaction the generation owns blocks it and appends directly; a
conversation-owned one places its summary through a write submission."""


async def createCompaction(
    tx: Tx, conversationId: ConversationId, input: dict[str, Any], owner: TaskId | None = None
) -> TaskId:
    """Create a compaction task with its status in this commit. ``owner`` is the generation that waits for it (a blocking
    compaction); without one it is conversation-owned, and ``background`` unless it is manual."""
    ownership = {"kind": "conversation"} if owner is None else {"kind": "task", "taskId": owner}
    background = owner is None and input["reason"] != "manual"
    taskId = await tx.createTask(
        CompactionTask, input, {"ownership": ownership, "conversationId": conversationId, "background": background}
    )
    status = {"taskId": taskId, "reason": input["reason"], "blocking": owner is not None, "attempt": 1}
    addCompactionStatus(await tx.doc(LiveDoc, conversationId), status)
    return taskId  # type: ignore[no-any-return]


def selectCut(view: dict[str, Any], keepRecentTokens: int) -> int | None:
    """Index in ``view["entries"]`` of the first entry a summary keeps, or ``None`` when there is nothing to compact
    (spec section 8.7). Walks back from the tail until ``keepRecentTokens`` are kept, then cuts at the first candidate at
    or after that entry: an entry whose contribution starts with a user or assistant message, never a tool result, and
    never a user entry that a result of the preceding assistant's calls still follows."""
    contributions = view["contributions"]
    start = 0 if view["head"] is None else 1
    candidates = [index for index in range(start, len(contributions)) if isCandidate(contributions, index)]
    kept = 0
    cut: int | None = None
    for index in range(len(contributions) - 1, start - 1, -1):
        for message in contributions[index]:
            kept += estimateMessageTokens(message)
        if kept < keepRecentTokens:
            continue
        cut = next((candidate for candidate in candidates if candidate >= index), None)
        if cut is None and len(candidates) > 0:
            cut = candidates[-1]
        break
    if cut is None:
        return None
    for index in range(start, cut):
        if len(contributions[index]) > 0:
            return cut
    return None


def isCandidate(contributions: list[list[dict[str, Any]]], index: int) -> bool:
    first = contributions[index][0] if len(contributions[index]) > 0 else None
    if first is not None and first["role"] == "assistant":
        return True
    if first is None or first["role"] != "user":
        return False
    # A result of the preceding assistant's calls that follows this entry, before the next assistant, belongs before it.
    calls: set[str] = set()
    for before in range(index - 1, -1, -1):
        assistant = next((message for message in reversed(contributions[before]) if message["role"] == "assistant"), None)
        if assistant is None:
            continue
        calls = {content["id"] for content in assistant["content"] if content["type"] == "toolCall"}
        break
    if len(calls) == 0:
        return True
    for after in range(index, len(contributions)):
        for position, message in enumerate(contributions[after]):
            if message["role"] == "assistant" and (after > index or position > 0):
                return True
            if message["role"] == "toolResult" and message["toolCallId"] in calls:
                return False
    return True


def summarizedMessages(view: dict[str, Any], cut: int) -> list[dict[str, Any]]:
    """Model messages of the entries before ``cut``: the head marker first, ordered like model context (spec section 2.1)."""
    return orderToolResults([message for contributed in view["contributions"][:cut] for message in contributed])


def estimateContext(view: dict[str, Any], extra: list[dict[str, Any]]) -> int:
    """Size of a request over ``view`` followed by ``extra`` (spec section 8.3): the usage of the newest assistant
    appended after the head marker, whose request included the marker, plus estimates of the messages after it; without
    one, estimates of every message."""
    measured: dict[str, Any] | None = None
    after = -math.inf if view["head"] is None else view["head"]["id"]
    index = len(view["entries"]) - 1
    while index >= 0 and measured is None:
        if view["entries"][index]["id"] > after:
            measured = next(
                (
                    message
                    for message in reversed(view["contributions"][index])
                    if message["role"] == "assistant" and calculateContextTokens(message["usage"]) > 0
                ),
                None,
            )
        index -= 1
    messages = view["messages"]
    fromIndex = 0
    if measured is not None:
        fromIndex = next((at for at in range(len(messages) - 1, -1, -1) if messages[at] is measured), -1) + 1
    tokens = 0 if measured is None else calculateContextTokens(measured["usage"])
    for message in messages[fromIndex:]:
        tokens += estimateMessageTokens(message)
    for message in extra:
        tokens += estimateMessageTokens(message)
    return tokens


def summaryText(message: dict[str, Any]) -> str | None:
    """The summary of a clean ``stop`` with text and no tool call; anything else is not a summary."""
    if message["stopReason"] != "stop" or any(content["type"] == "toolCall" for content in message["content"]):
        return None
    text = jsTrim("\n".join(content["text"] for content in message["content"] if content["type"] == "text"))
    return None if len(text) == 0 else text


def summaryFailure(message: dict[str, Any]) -> str:
    if message["stopReason"] in ("error", "aborted"):
        return f"Summarization failed: {nullishCoalesce(message.get('errorMessage'), message['stopReason'])}"
    if message["stopReason"] == "length":
        return "Summarization hit the token limit; the summary is incomplete"
    if any(content["type"] == "toolCall" for content in message["content"]):
        return "Summarization attempted to call a tool"
    return "Summarization produced no text"


def summaryPrompt(messages: list[dict[str, Any]], instructions: str | None) -> str:
    """The summarizer's user message: the serialized conversation, the prompt, and any instructions."""
    focus = "" if instructions is None else f"\n\nAdditional focus: {instructions}"
    return f"<conversation>\n{serializeConversation(messages)}\n</conversation>\n\n{SUMMARIZATION_PROMPT}{focus}"


def serializeConversation(messages: list[dict[str, Any]]) -> str:
    """Messages as plain text, so the summarizer reads a transcript instead of continuing it. System messages are
    omitted. Thinking is omitted too: providers return summaries of the real reasoning, and replayed thinking is often
    not part of the live context (a block that does not match its request is dropped), so including it can make the
    request larger than the context it summarizes. Visible text, tool calls, and tool results record what happened."""
    parts: list[str] = []
    for message in messages:
        if message["role"] == "user":
            text = contentText(message["content"])
            if len(text) > 0:
                parts.append(f"[User]: {text}")
        elif message["role"] == "assistant":
            texts = [content["text"] for content in message["content"] if content["type"] == "text"]
            calls = [
                f"{content['name']}({', '.join(f'{key}={jsonStringify(value)}' for key, value in content['arguments'].items())})"
                for content in message["content"]
                if content["type"] == "toolCall"
            ]
            if len(texts) > 0:
                parts.append(f"[Assistant]: {chr(10).join(texts)}")
            if len(calls) > 0:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif message["role"] == "toolResult":
            text = contentText(message["content"])
            if len(text) > 0:
                parts.append(f"[Tool result]: {truncate(text, TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


def contentText(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block["type"] == "text" and block.get("text") is not None)


def truncate(text: str, maxChars: int) -> str:
    length = jsLength(text)
    if length <= maxChars:
        return text
    return f"{jsSlice(text, 0, maxChars)}\n\n[... {length - maxChars} more characters truncated]"


async def place(runtime: Any, firstKept: int, summary: str, context: Context) -> None:
    """Place a summary supplied by a hook in its own commit."""

    async def apply(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        return await placeSummary(tx, runtime, current, await tx.doc(LiveDoc, runtime.conversationId), firstKept, summary)

    await runtime.commit(apply, context)


async def placeSummary(
    tx: Tx, runtime: Any, current: dict[str, Any], live: Any, firstKept: int, summary: str
) -> dict[str, Any]:
    """Place the summary entry and complete (spec section 8.7). A blocking compaction, owned by its generation, appends
    it: the generation holds the run and waits. A conversation-owned one admits it as a write submission, placed at once
    when idle, otherwise at the next boundary, or settled ``stale``.

    REMINDER: nothing else may append to a busy conversation, so every non-blocking summary goes through admission."""
    # Imported here: submissions imports the generation module, which imports this one.
    from .submissions import admitSubmission

    removeCompactionStatus(live, runtime.taskId)
    if "compactionFailure" in live:
        del live["compactionFailure"]
    text = f"{SUMMARY_PREFIX}{summary}{SUMMARY_SUFFIX}"
    entry = {
        "kind": CompactionEntry.kind,
        "head": firstKept,
        "model": [{"role": "user", "content": [{"type": "text", "text": text}], "timestamp": runtime.now()}],
        "data": {"reason": current["input"]["reason"]},
    }
    if current.get("owner") is None:
        result = {
            "submissionId": await admitSubmission(
                tx,
                runtime.conversationId,
                {"type": "write", "requestId": f"compaction:{runtime.taskId}", "entry": entry},
                runtime.now(),
                runtime.settings,
            )
        }
    else:
        result = {"entryId": (await tx.appendEntry(runtime.conversationId, entry))["id"]}
    return {"status": "terminal", "outcome": {"status": "completed", "result": result}}


async def complete(runtime: Any, context: Context) -> None:
    """Remove the status and complete without a summary."""

    async def end(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        removeCompactionStatus(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
        return {"status": "terminal", "outcome": {"status": "completed", "result": {}}}

    await runtime.commit(end, context)


async def failNoModel(runtime: Any, ref: dict[str, Any] | None, context: Context) -> None:
    message = "No model is configured" if ref is None else f"Model {ref['provider']}/{ref['modelId']} is not available"

    async def fail(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        failCompactionStatus(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId, message)
        return {
            "status": "terminal",
            "outcome": {"status": "failed", "error": {"message": message, "detail": {"reason": "no_model"}}},
        }

    await runtime.commit(fail, context)


__all__ = [
    "CompactionTask",
    "createCompaction",
    "estimateContext",
    "selectCut",
    "serializeConversation",
    "summarizedMessages",
]
