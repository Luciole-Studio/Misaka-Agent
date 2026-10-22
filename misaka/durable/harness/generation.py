"""The built-in generation task: prepare, request or poll the model, retry, classify, and run the tool round.

Translated from pi's ``packages/durable/src/harness/generation.ts`` (v1.3.0). A checkpoint is a ``dict`` with a
``phase``: ``prepare`` (``attempt``, ``compacted``?, ``overflow``?, ``threshold``?: true when the blocking threshold
started ``compacted``, whose outcome is checked once when ``prepare`` resumes), ``request`` (``attempt``, ``compacted``?, ``model``,
``thinkingLevel``, ``streamOptions``, ``cutoff``), ``retry`` (``attempt``, ``compacted``?, ``until``), ``poll``
(``attempt``, ``compacted``?, ``model``, ``cutoff``, ``handle``, ``pollAt``) or ``tools`` (``assistant``, ``tools``,
``pending``). Provider results cross the pi-ai boundary through ``misaka.durable.harness.ai_bridge``: messages are strict
JSON ``dict`` objects here.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from misaka.ai.types import (
    DeferredCancelOptions,
    DeferredFetchOptions,
    DeferredHandle,
    SimpleStreamOptions,
)
from misaka.ai.utils.transcript import getCurrentTools
from misaka.chord.context import Context
from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED, awaitValue, nullishCoalesce, startTask

from ..entries import AssistantEntry, ResetEntry, SystemEntry, UserEntry
from ..tasks import defineTask
from ..types import ConversationId, EntryId, SubmissionId, TaskId, Tx
from .agent import addTools
from .ai_bridge import (
    isContextOverflow,
    isRetryableAssistantError,
    retryDelayMs,
    toContext,
    toMessageDict,
)
from .compaction import createCompaction, estimateContext, selectCut
from .inbox import applyBoundary, prepareBoundary
from .json import assignJson
from .live import LiveDoc, endRun
from .prompt import planSystemEntries, renderSections, replaySections
from .provider import ensureProviderSessionId
from .tool import ToolTask, appendToolResult, harnessError
from .usage import recordUsage

DEFAULT_POLL_AFTER_MS = 5000


async def preparePhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    """Render the system prompt and tool loadout and append the positional ``pi.system`` entries they need, then move
    to ``request``. The agent and settings resolved here are fixed for this request. Only the Harness writes model
    context to a busy conversation, so the context read here is still current at the commit; display-only entries may
    land meanwhile."""
    conversationId = runtime.conversationId
    agent = await runtime.agent(context)
    settings = runtime.settings
    model = agent.get("model")
    thinkingLevel = agent["thinkingLevel"]
    resolved = None if model is None else runtime.models.getModel(model["provider"], model["modelId"])
    if model is None or resolved is None:
        await failNoModel(runtime, model, context)
        return
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    overflow = checkpoint.get("overflow")
    blocked = checkpoint.get("threshold")
    if compacted is not None and overflow is not None:
        outcome = (await runtime.outcomes([compacted], context))[0]
        if outcome["status"] != "completed" or outcome["result"].get("entryId") is None:
            await failModelError(runtime, overflow, context)
            return
    # A threshold compaction that found nothing to compact or was declined leaves a request that may still fit. One that
    # failed left a context it judged too large: sending it would at best leave no room for the answer.
    if compacted is not None and blocked is True:
        outcome = (await runtime.outcomes([compacted], context))[0]
        if outcome is not None and outcome["status"] != "completed":
            await failModelError(runtime, compactionFailure(outcome), context)
            return
    view = await runtime.context(conversationId, context)
    shown = replaySections(view["messages"])

    def report(error: BaseException) -> None:
        runtime.report(error)

    env: Any = None
    try:
        env = await runtime.env(context)
    except Exception as error:
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            raise
        report(error)
    promptInput = {"conversationId": conversationId, "agent": agent, "env": env, "shown": dict(shown), "read": runtime}
    desired = await renderSections(agent["sections"], promptInput, shown, report, context)
    entries = planSystemEntries(view, desired, agent["tools"], runtime.now())
    threshold = (
        thresholdCompaction(view, entries, resolved.contextWindow, settings["compaction"]) if compacted is None else None
    )
    if threshold == "blocking":
        # Compact first and prepare again; the transcript is unchanged until the compaction appends.
        async def compactFirst(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
            child = await createCompaction(tx, conversationId, {"reason": "threshold"}, runtime.taskId)
            checkpoint = {"phase": "prepare", "attempt": attempt, "compacted": child, "threshold": True}
            return {"status": "waiting", "checkpoint": checkpoint, "on": [child], "policy": "allSettled"}

        await runtime.commit(compactFirst, context)
        return

    async def prepare(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        page = await tx.scanEntries({"conversationId": conversationId}, 1)
        cutoff = page["items"][0]["id"] if len(page["items"]) > 0 else None
        for entry in entries:
            cutoff = (await tx.appendEntry(SystemEntry, conversationId, entry))["id"]
        if cutoff is None:
            raise RuntimeError(f"Conversation {conversationId} has no entries to send")
        # Checked in this commit, so a compaction admitted during preparation counts.
        if threshold == "background" and "compactions" not in (await tx.doc(LiveDoc, conversationId)):
            await createCompaction(tx, conversationId, {"reason": "threshold"})
        request = {
            "attempt": attempt,
            **({} if compacted is None else {"compacted": compacted}),
            "model": model,
            "thinkingLevel": thinkingLevel,
            "streamOptions": settings["stream"],
            "cutoff": cutoff,
        }
        return {"status": "running", "checkpoint": {"phase": "request", **request}}

    await runtime.commit(prepare, context)


async def requestPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    ref = checkpoint["model"]
    thinkingLevel = checkpoint["thinkingLevel"]
    streamOptions = checkpoint["streamOptions"]
    cutoff = checkpoint["cutoff"]
    conversationId = runtime.conversationId

    async def begin(tx: Tx, current: dict[str, Any]) -> None:
        live = await tx.doc(LiveDoc, conversationId)
        await convertPartial(tx, live, conversationId)
        live["generation"] = {"attempt": attempt}

    await runtime.commit(begin, context)
    model = runtime.models.getModel(ref["provider"], ref["modelId"])
    if model is None:
        await failNoModel(runtime, ref, context)
        return
    view = await runtime.context(conversationId, context, {"at": cutoff})
    messages = view["messages"]

    async def beforeRequest(hook: Any) -> None:
        nonlocal messages
        replaced = await awaitValue(hook({"messages": messages}, runtime, context))
        if replaced is not None:
            messages = replaced["messages"]

    await runtime.hooks.each("beforeRequest", beforeRequest)
    options = {
        **streamOptions,
        "signal": runtime.signal,
        "sessionId": await ensureProviderSessionId(runtime, context),
        **({} if thinkingLevel == "off" else {"reasoning": thinkingLevel}),
    }
    message = await streamResponse(runtime, model, messages, options, attempt, context)
    request = {
        "attempt": attempt,
        "compacted": compacted,
        "model": ref,
        "cutoff": cutoff,
        "messages": view["messages"],
    }
    await classify(runtime, request, message, context)


async def retryPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    await runtime.sleep(checkpoint["until"], context)

    async def next_(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        (await tx.doc(LiveDoc, runtime.conversationId))["generation"] = {"attempt": attempt + 1}
        following = {
            "phase": "prepare",
            "attempt": attempt + 1,
            **({} if compacted is None else {"compacted": compacted}),
        }
        return {"status": "running", "checkpoint": following}

    await runtime.commit(next_, context)


async def pollPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    checkpoint = task["state"]["checkpoint"]
    attempt = checkpoint["attempt"]
    compacted = checkpoint.get("compacted")
    ref = checkpoint["model"]
    cutoff = checkpoint["cutoff"]
    handle = checkpoint["handle"]
    pollAt = checkpoint["pollAt"]
    model = runtime.models.getModel(ref["provider"], ref["modelId"])
    if model is None:
        await failNoModel(runtime, ref, context)
        return
    await runtime.sleep(pollAt, context)
    message = toMessageDict(
        await runtime.models.fetchDeferred(
            model, DeferredHandle.model_validate(handle), DeferredFetchOptions(signal=runtime.signal)
        )
    )
    request = {"attempt": attempt, "compacted": compacted, "model": ref, "cutoff": cutoff, "pollAt": pollAt}
    await classify(runtime, request, message, context)


async def toolsPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    checkpoint = task["state"]["checkpoint"]
    assistant = checkpoint["assistant"]
    tools = checkpoint["tools"]
    pending = checkpoint["pending"]
    if len(pending) == 0:
        await finishToolRound(runtime, assistant, tools, context)
        return
    following, rest = pending[0], pending[1:]

    # Sequential round: start the next call and wait for it.
    async def start(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, runtime.conversationId)
        taskId = await createToolTask(tx, runtime, assistant, following)
        slot = next(
            (slot for slot in live.get("tools") or [] if slot["callId"] == following and "taskId" not in slot), None
        )
        if slot is not None:
            slot["taskId"] = taskId
        waiting = {"phase": "tools", "assistant": assistant, "tools": [*tools, taskId], "pending": rest}
        return {"status": "waiting", "checkpoint": waiting, "on": [taskId], "policy": "allSettled"}

    await runtime.commit(start, context)


async def abortPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    checkpoint = task["state"]["checkpoint"]
    if checkpoint["phase"] == "poll":
        model = runtime.models.getModel(checkpoint["model"]["provider"], checkpoint["model"]["modelId"])
        if model is not None:
            try:
                await runtime.models.cancelDeferred(
                    model, DeferredHandle.model_validate(checkpoint["handle"]), DeferredCancelOptions(signal=runtime.signal)
                )
            except Exception as error:  # noqa: BLE001 - a failing cancellation is reported
                runtime.report(error)
    conversationId = runtime.conversationId
    # Runs after the round's tool tasks are terminal; calls never started get ``aborted`` results (spec section 8.5).
    unstarted = (
        await readCalls(runtime, checkpoint["assistant"], checkpoint["pending"], context)
        if checkpoint["phase"] == "tools"
        else []
    )

    async def end(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, conversationId)
        await convertPartial(tx, live, conversationId)
        for call in unstarted:
            result = harnessError("aborted", f"Tool {call['name']} was aborted")
            await appendToolResult(tx, conversationId, call, result, {"timestamp": runtime.now()})
        endRun(tx, live, runtime.taskId, {"status": "unanswered", "reason": "aborted"})
        return {"status": "terminal", "outcome": {"status": "aborted"}}

    await runtime.commit(end, context)


GenerationTask = defineTask(
    {
        "name": "pi.generation",
        "version": 1,
        "initial": lambda input: {"phase": "prepare", "attempt": 1},
        "phases": {
            "prepare": preparePhase,
            "request": requestPhase,
            "retry": retryPhase,
            "poll": pollPhase,
            "tools": toolsPhase,
        },
        "abort": abortPhase,
    }
)
"""Built-in generation task: prepares the positional system prompt and tool loadout, requests or polls the model,
retries, and classifies the response. The run's inputs live in ``pi.live.run``."""


async def readCalls(runtime: Any, assistant: EntryId, callIds: list[str], context: Context) -> list[dict[str, Any]]:
    """The calls ``callIds`` of the assistant entry, in the given order."""
    entry = await runtime.entry(AssistantEntry, assistant, context)
    model = None if entry is None else entry.get("model")
    message = model[0] if model else None
    calls = (
        [content for content in message["content"] if content["type"] == "toolCall"]
        if message is not None and message["role"] == "assistant"
        else []
    )
    return [call for id in callIds for call in [next((call for call in calls if call["id"] == id), None)] if call is not None]


def createToolTask(tx: Tx, runtime: Any, assistant: EntryId, callId: str) -> Any:
    """A tool task for call ``callId``, owned by the generation."""
    return tx.createTask(
        ToolTask,
        {"kind": "model", "assistant": assistant, "callId": callId},
        {"ownership": {"kind": "task", "taskId": runtime.taskId}},
    )


def thresholdCompaction(
    view: dict[str, Any], planned: list[dict[str, Any]], contextWindow: int, policy: dict[str, Any]
) -> str | None:
    """Which threshold compaction preparation starts before its request (spec section 8.3): ``blocking`` above
    ``contextWindow - reserveTokens``, ``background`` above the background threshold, and only when range selection finds
    a cut. The caller starts a background one only while no compaction is listed."""
    if not policy["enabled"] or contextWindow <= 0:
        return None
    tokens = estimateContext(view, [message for entry in planned for message in entry.get("model") or []])
    blocking = contextWindow - policy["reserveTokens"]
    background = blocking - policy["backgroundTokens"]
    over = (
        "blocking"
        if tokens > blocking
        else "background"
        if policy["backgroundTokens"] > 0 and tokens > background
        else None
    )
    if over is None or selectCut(view, policy["keepRecentTokens"]) is None:
        return None
    return over


def compactionFailure(outcome: dict[str, Any]) -> str:
    """Error text for a generation whose blocking compaction ended without completing."""
    status = outcome["status"]
    if status in ("failed", "faulted"):
        return f"Compaction failed: {outcome['error']['message']}"
    if status == "orphaned":
        return f"Compaction failed: {outcome['reason']}"
    return "Compaction was aborted"


async def failModelError(runtime: Any, text: str, context: Context) -> None:
    """Settle the run's inputs ``unanswered`` with ``model_error`` and fail with ``text``."""

    async def fail(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, runtime.conversationId)
        endRun(tx, live, runtime.taskId, {"status": "unanswered", "reason": "model_error", "detail": text})
        return {
            "status": "terminal",
            "outcome": {"status": "failed", "error": {"message": text, "detail": {"reason": "model_error"}}},
        }

    await runtime.commit(fail, context)


async def failNoModel(runtime: Any, ref: dict[str, Any] | None, context: Context) -> None:
    """Settle the run's inputs ``unanswered`` with ``no_model`` and fail."""
    message = "No model is configured" if ref is None else f"Model {ref['provider']}/{ref['modelId']} is not available"

    async def fail(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, runtime.conversationId)
        endRun(tx, live, runtime.taskId, {"status": "unanswered", "reason": "no_model"})
        return {
            "status": "terminal",
            "outcome": {"status": "failed", "error": {"message": message, "detail": {"reason": "no_model"}}},
        }

    await runtime.commit(fail, context)


async def convertPartial(tx: Tx, live: Any, conversationId: ConversationId) -> None:
    """Append a committed partial left by an interrupted, aborted, faulted, or orphaned attempt as an aborted assistant
    entry; the caller replaces or removes ``generation``."""
    generation = live.get("generation", UNDEFINED)
    partial = UNDEFINED if generation is UNDEFINED else generation.get("message", UNDEFINED)
    if partial is UNDEFINED:
        return
    message: dict[str, Any] = copyJson(partial)  # type: ignore[assignment]
    await appendAssistant(tx, conversationId, {**message, "stopReason": "aborted"})


async def streamResponse(
    runtime: Any, model: Any, messages: list[dict[str, Any]], options: dict[str, Any], attempt: int, context: Context
) -> dict[str, Any]:
    """Stream one request and return the terminal message. Partials commit as trailing writes at most every
    ``progress.partialIntervalMs`` (default 100 ms) with one commit in flight; ``finally`` stops the throttle and awaits
    that commit, so no stale partial lands after the outcome."""
    loop = asyncio.get_running_loop()
    interval = runtime.settings["progress"]["partialIntervalMs"]
    pending: Any = None
    timer: Any = None
    inFlight: asyncio.Future[None] | None = None
    stopped = False

    def flush() -> None:
        nonlocal timer, pending, inFlight
        timer = None
        partial = pending
        pending = None
        if partial is None or stopped:
            return
        done: asyncio.Future[None] = loop.create_future()
        inFlight = done

        async def write() -> None:
            nonlocal timer, inFlight
            try:
                # Copy synchronously: the provider keeps mutating its partial.
                message = copyJson(toMessageDict(partial), {"omitUndefinedProperties": True})

                async def apply(tx: Tx, current: dict[str, Any]) -> None:
                    live = await tx.doc(LiveDoc, runtime.conversationId)
                    if "generation" not in live:
                        live["generation"] = {"attempt": attempt}
                    assignJson(live["generation"], "message", message)

                await runtime.commit(apply, context)
            except Exception as error:  # noqa: BLE001 - rejections after an abort mark or close are expected
                if not runtime.signal.aborted:
                    runtime.report(error)
            finally:
                if inFlight is done:
                    inFlight = None
                done.set_result(None)
                if pending is not None and not stopped:
                    timer = loop.call_later(interval / 1000, flush)

        startTask(write())

    try:
        events = runtime.models.streamSimple(model, toContext(messages), SimpleStreamOptions(**options))
        async for event in events:
            # A partial without content, such as pi-ai's opening ``start`` event, shows nothing; a deferred response
            # never gets past it, so it never leaves a partial.
            if event.type in ("done", "error") or len(event.partial.content) == 0:
                continue
            pending = event.partial
            if timer is None and inFlight is None:
                timer = loop.call_later(interval / 1000, flush)
        return toMessageDict(await events.result())
    finally:
        stopped = True
        if timer is not None:
            timer.cancel()
        if inFlight is not None:
            await inFlight


async def classify(runtime: Any, request: dict[str, Any], message: dict[str, Any], context: Context) -> None:
    """Classify a terminal provider message in one commit that also clears the partial."""
    # An abort mark or close: the abort invocation or the reopened run handles the committed state.
    runtime.signal.throwIfAborted()
    conversationId = runtime.conversationId
    attempt = request["attempt"]
    compacted = request.get("compacted")
    ref = request["model"]
    cutoff = request["cutoff"]
    if message["stopReason"] == "deferred" and message.get("deferred") is not None:
        handle = message["deferred"]
        pollAfter = handle.get("pollAfterMs")
        pollAt = max(
            runtime.now() + (pollAfter if pollAfter is not None else DEFAULT_POLL_AFTER_MS),
            -math.inf if request.get("pollAt") is None else request["pollAt"] + 1,
        )

        async def park(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
            (await tx.doc(LiveDoc, conversationId))["generation"] = {"attempt": attempt, "deferred": {"pollAt": pollAt}}
            checkpoint = {
                "phase": "poll",
                "attempt": attempt,
                **({} if compacted is None else {"compacted": compacted}),
                "model": ref,
                "cutoff": cutoff,
                "handle": handle,
                "pollAt": pollAt,
            }
            return {"status": "running", "checkpoint": checkpoint}

        await runtime.commit(park, context)
        return
    await runtime.hooks.each("afterResponse", lambda hook: hook(message, runtime, context))
    calls = [content for content in message["content"] if content["type"] == "toolCall"]
    if message["stopReason"] == "toolUse" and len(calls) > 0:
        await startToolRound(runtime, request, message, calls, context)
        return
    if message["stopReason"] in ("stop", "length", "toolUse"):
        await answer(runtime, message, context)
        return
    # The retry and compaction policies govern the next attempt, so they are read now rather than pinned at preparation.
    settings = runtime.settings
    overflow = message["stopReason"] == "error" and isContextOverflow(message)
    if overflow and compacted is None and settings["compaction"]["enabled"]:
        policy = settings["compaction"]
        view = await runtime.context(conversationId, context, {"at": cutoff})
        if selectCut(view, policy["keepRecentTokens"]) is not None:
            text = nullishCoalesce(message.get("errorMessage"), "Context overflow")

            async def compactOverflow(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
                live = await tx.doc(LiveDoc, conversationId)
                await appendAssistant(tx, conversationId, message)
                del live["generation"]
                child = await createCompaction(tx, conversationId, {"reason": "overflow"}, runtime.taskId)
                checkpoint = {"phase": "prepare", "attempt": attempt, "compacted": child, "overflow": text}
                return {"status": "waiting", "checkpoint": checkpoint, "on": [child], "policy": "allSettled"}

            await runtime.commit(compactOverflow, context)
            return
    policy = settings["retry"]
    # An overflow is never retried: only a compaction can make the next request fit.
    retry = (
        message["stopReason"] == "error"
        and not overflow
        and isRetryableAssistantError(message)
        and policy["enabled"]
        and attempt <= policy["maxRetries"]
    )
    until = runtime.now() + retryDelayMs(policy, attempt) if retry else 0

    async def settle(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, conversationId)
        await appendAssistant(tx, conversationId, message)
        if retry:
            live["generation"] = {"attempt": attempt, "retry": {"at": until, "error": message.get("errorMessage") or ""}}
            checkpoint = {
                "phase": "retry",
                "attempt": attempt,
                **({} if compacted is None else {"compacted": compacted}),
                "until": until,
            }
            return {"status": "running", "checkpoint": checkpoint}
        text = nullishCoalesce(
            message.get("errorMessage"), f"Model response ended with stop reason {message['stopReason']}"
        )
        endRun(tx, live, runtime.taskId, {"status": "unanswered", "reason": "model_error", "detail": text})
        return {
            "status": "terminal",
            "outcome": {"status": "failed", "error": {"message": text, "detail": {"reason": "model_error"}}},
        }

    await runtime.commit(settle, context)


async def answer(runtime: Any, message: dict[str, Any], context: Context) -> None:
    """A final answer; the final boundary places queued items (spec section 6). The first ``onYield`` continuation appends
    a user message and hands the run to a successor generation, but only when the boundary selected no user item and no
    reset. Otherwise the run's inputs settle ``done``, and selected user items start the next run."""
    continuation: Any = None

    async def onYield(hook: Any) -> None:
        nonlocal continuation
        if continuation is not None:
            return
        decision = await awaitValue(hook(message, runtime, context))
        continuation = None if decision is None else decision.get("continue")

    await runtime.hooks.each("onYield", onYield)
    conversationId = runtime.conversationId

    async def place(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        # Queue modes are read on the Session line, when the boundary is decided.
        boundary = await prepareBoundary(tx, conversationId, runtime.settings)
        live = await tx.doc(LiveDoc, conversationId)
        entry = await appendAssistant(tx, conversationId, message)
        result = {"status": "terminal", "outcome": {"status": "completed", "result": {"entryId": entry["id"]}}}
        boundaryResult = await applyBoundary(tx, boundary, "final", runtime.now())
        users = boundaryResult["users"]
        if continuation is not None and len(users) == 0 and not boundaryResult["reset"]:
            user = {"role": "user", "content": continuation, "timestamp": runtime.now()}
            await tx.appendEntry(UserEntry, conversationId, {"model": [user]})
            handOver(live, runtime.taskId, await createGeneration(tx, conversationId))
            if "generation" in live:
                del live["generation"]
            return result
        endRun(tx, live, runtime.taskId, {"status": "done", "answer": entry["id"]})
        if len(users) > 0:
            await startRun(tx, conversationId, live, users)
        return result

    await runtime.commit(place, context)


async def startToolRound(
    runtime: Any, request: dict[str, Any], message: dict[str, Any], calls: list[dict[str, Any]], context: Context
) -> None:
    """Append the tool-calling answer and start its tool round in one commit (spec section 8.3). A call to a tool the
    request did not offer gets its ``tool_unavailable`` result here; every other call gets a tool task owned by the
    generation, only the first one now when the round is sequential. The generation then waits for them in its ``tools``
    phase, keeping the run."""
    conversationId = runtime.conversationId
    messages = request.get("messages")
    if messages is None:
        messages = (await runtime.context(conversationId, context, {"at": request["cutoff"]}))["messages"]
    offered = {tool["name"] for tool in getCurrentTools(messages)}
    # Read as the round starts; a tool is resolved as its tool task resolves it.
    tools = (await runtime.agent(context))["tools"]
    sequential = runtime.settings["toolExecution"] == "sequential" or any(
        call["name"] in offered
        and (next((tool for tool in tools if tool["name"] == call["name"]), None) or {}).get("executionMode") == "sequential"
        for call in calls
    )

    async def start(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        live = await tx.doc(LiveDoc, conversationId)
        entry = await appendAssistant(tx, conversationId, message)
        slots: list[dict[str, Any]] = []
        started: list[TaskId] = []
        pending: list[str] = []
        for call in calls:
            if call["name"] not in offered:
                unavailable = harnessError("tool_unavailable", f"Tool {call['name']} is not available")
                result = await appendToolResult(tx, conversationId, call, unavailable, {"timestamp": runtime.now()})
                slots.append({"callId": call["id"], "name": call["name"], "status": "done", "entry": result["id"]})
                continue
            if sequential and len(started) > 0:
                pending.append(call["id"])
                slots.append({"callId": call["id"], "name": call["name"], "status": "pending"})
                continue
            taskId = await createToolTask(tx, runtime, entry["id"], call["id"])
            started.append(taskId)
            slots.append({"callId": call["id"], "name": call["name"], "taskId": taskId, "status": "pending"})
        if "generation" in live:
            del live["generation"]
        live["tools"] = slots
        checkpoint = {"phase": "tools", "assistant": entry["id"], "tools": started, "pending": pending}
        return {"status": "waiting", "checkpoint": checkpoint, "on": started, "policy": "allSettled"}

    await runtime.commit(start, context)


async def finishToolRound(runtime: Any, assistant: EntryId, tools: list[TaskId], context: Context) -> None:
    """The round's tools are terminal: apply their controls and either end the run at the final boundary
    (``terminate``, ``handoff``, or a queued reset) or hand it to the next generation at the ``postTools`` boundary
    (spec section 8.5)."""
    conversationId = runtime.conversationId
    controls: dict[TaskId, dict[str, Any] | None] = {}
    outcomes = await runtime.outcomes(tools, context)
    for index, id in enumerate(tools):
        outcome = outcomes[index]
        # Version 1 results have no kind; every result a generation's round reads is a model-issued call's.
        controls[id] = (
            outcome["result"].get("control")
            if outcome["status"] == "completed" and outcome["result"].get("kind") != "nested"
            else None
        )
    snapshot = await runtime.snapshot(LiveDoc, conversationId, context)
    slots = (snapshot or {}).get("tools") or []
    results = [slot["entry"] for slot in slots if slot.get("entry") is not None]
    await runtime.hooks.each("afterTools", lambda hook: hook(assistant, results, runtime, context))
    # Every call of the round, including those answered without a task, must ask to terminate.
    terminate = len(slots) > 0 and all(
        slot.get("taskId") is not None and (controls.get(slot["taskId"]) or {}).get("terminate") is True for slot in slots
    )
    added = [name for control in controls.values() for name in (control or {}).get("addTools") or []]
    # The last handoff in call order wins.
    handoff = next(
        (control["handoff"] for control in reversed(list(controls.values())) if control is not None and control.get("handoff") is not None),
        None,
    )

    async def finish(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        boundary = await prepareBoundary(tx, conversationId, runtime.settings)
        if len(added) > 0:
            await addTools(tx, conversationId, added)
        live = await tx.doc(LiveDoc, conversationId)
        now = runtime.now()
        if terminate or handoff is not None:
            if handoff is not None:
                message = {"role": "user", "content": handoff, "timestamp": now}
                entry = await tx.appendEntry(ResetEntry, conversationId, {"head": "self", "model": [message]})
                boundary["head"] = entry["id"]
            users = (await applyBoundary(tx, boundary, "final", now))["users"]
            endRun(tx, live, runtime.taskId, {"status": "done", "answer": assistant})
            if len(users) > 0:
                await startRun(tx, conversationId, live, users)
        else:
            boundaryResult = await applyBoundary(tx, boundary, "postTools", now)
            users = boundaryResult["users"]
            if boundaryResult["reset"]:
                # The queued reset cut the run's context before an answer.
                endRun(tx, live, runtime.taskId, {"status": "unanswered", "reason": "reset"})
                if len(users) > 0:
                    await startRun(tx, conversationId, live, users)
            else:
                if "tools" in live:
                    del live["tools"]
                if "nestedTools" in live:
                    del live["nestedTools"]
                run = live.get("run", UNDEFINED)
                if run is not UNDEFINED and run["taskId"] == runtime.taskId:
                    run["inputs"].push(*users)
                handOver(live, runtime.taskId, await createGeneration(tx, conversationId))
        return {"status": "terminal", "outcome": {"status": "completed", "result": {"entryId": assistant}}}

    await runtime.commit(finish, context)


async def appendAssistant(tx: Tx, conversationId: ConversationId, message: dict[str, Any]) -> dict[str, Any]:
    """Append a provider result and add its usage to ``pi.usage`` in the same commit.

    REMINDER: every built-in writer of assistant entries goes through here, so the usage ledger stays complete."""
    await recordUsage(tx, conversationId, "models", f"{message['provider']}/{message['model']}", message["usage"])
    return await tx.appendEntry(AssistantEntry, conversationId, {"model": [message]})  # type: ignore[no-any-return]


async def startRun(tx: Tx, conversationId: ConversationId, live: Any, inputs: list[SubmissionId]) -> None:
    """Start a run for ``inputs``, placed input submissions: a new generation takes ``pi.live.run``."""
    live["run"] = {"taskId": await createGeneration(tx, conversationId), "inputs": inputs}


def createGeneration(tx: Tx, conversationId: ConversationId) -> Any:
    """A generation owned by its conversation."""
    return tx.createTask(GenerationTask, {}, {"ownership": {"kind": "conversation"}, "conversationId": conversationId})


def handOver(live: Any, from_: TaskId, to: TaskId) -> None:
    """Hand run control from ``from_`` to ``to``; the run's inputs move with it."""
    run = live.get("run", UNDEFINED)
    if run is not UNDEFINED and run["taskId"] == from_:
        run["taskId"] = to


__all__ = ["GenerationTask", "convertPartial", "handOver", "startRun"]
