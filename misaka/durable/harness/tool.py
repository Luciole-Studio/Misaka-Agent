"""The built-in tool task: runs one tool call, model-issued or nested.

Translated from pi's ``packages/durable/src/harness/tool.ts`` (v1.3.0). A tool is a ``dict`` (``name``, ``description``,
``parameters`` as a JSON schema, ``execute(args, api, context)``, optional ``replay``, ``executionMode``,
``prepareArguments(args)``, ``structuredOutputSchema``, ``callers``, ``deferred`` and ``outputLimits``), a result is a
``dict`` (``output``, ``structuredOutput``, ``isError``, ``details``, ``diagnostics``, ``usage``, ``control``) and the
``api`` a tool receives is a ``types.SimpleNamespace`` (spread it with ``SimpleNamespace(**{**vars(api), "name":
value})``) with ``taskId``, ``conversationId``, ``callId``, ``registry``, ``env``, ``models``, ``agent``, ``output``,
``outputWindow``, ``retainedOutput``, ``diagnostic``, ``details``, ``commit``, ``memo``, ``createTask``, ``getTask``,
``waitForTask``, ``conversation``, ``snapshot``, ``snapshotAsOf``, ``watchDoc`` and ``executeTool``. ``details`` that is
absent is ``UNDEFINED``. ``outputWindow`` is the tail this call's output keeps, for ``ShellExecOptions.window``, or
``None`` when the tool keeps the head; a wrapper that replaces ``output`` and transforms text must also set it to
``None``, so skipped text cannot bypass its transform.

A task input is ``{"kind": "model", "assistant", "callId"}`` (a model-issued call, read from its assistant entry) or
``{"kind": "nested", "parent", "parentCallId", "key", "call", "progress"?: False}`` (a nested call a running tool made
through ``executeTool()``, whose call the input carries because no entry holds it). A task result is ``{"kind": "model",
"entryId", "control"?}`` or ``{"kind": "nested"}``; a version 1 result has no ``kind`` and is a model-issued call's.
"""

from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import Tool as AiTool
from misaka.ai.types import ToolCall as AiToolCall
from misaka.ai.utils.validation import validateToolArguments
from misaka.chord._promise import isRejection

# MISAKA fork: upstream compiles `structuredOutputSchema` with the typebox package's `Compile`; chord's TypeBox stand-in
# checks a JSON schema with TypeBox's semantics and error wording, so durable adds no dependency for it.
from misaka.chord._typebox import Value as TypeBoxValue
from misaka.chord.context import Context, awaitWithContext
from misaka.chord.delta import overlap
from misaka.chord.delta.diff import strictEquals
from misaka.chord.json import copyJson
from misaka.chord.output import (
    PROGRESS_BYTES_PER_SECOND,
    OutputBuffer,
    OutputLimits,
    Progress,
    boundOutput,
)
from misaka.utils.js import (
    UNDEFINED,
    awaitValue,
    eager,
    jsLength,
    jsonStringify,
    jsRound,
    jsSlice,
    startTask,
)

from ..documents import defineDocFamily
from ..entries import AssistantEntry, ToolResultEntry
from ..errors import errorMessage
from ..tasks import defineTask
from ..truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, utf8ByteLength
from ..types import ConversationId, Tx
from .json import assignJson
from .live import LiveDoc, clearProgress, finishSlot, removeNestedSlots, toolSlot
from .usage import recordUsage
from .util import asThrown, thrownBy

MADE_NESTED_CALLS = "pi.tool.madeNestedCalls"
"""Memo of a tool task that made a nested call, in any attempt, committed before the first admission of each attempt:
only then can ``pi.live`` hold slots below it, which settling removes. A crash between the two only costs a scan."""

NestedCallDoc = defineDocFamily(
    {
        "kind": "pi.tool.nested-call",
        "version": 1,
        "scope": "task",
        "family": True,
        "initial": lambda *_: {},
    }
)
"""The nested calls of a tool call, a task-scoped family of the calling task keyed by the nested call's key: so a rerun
finds the calls it already made, and the caller reads each result. Created with the nested task; the nested call's
terminal commit adds its result. One document per call, so no commit rewrites the others. Retires with the caller. A
member (``NestedCallState``) is ``{"taskId"?, "result"?}``: ``taskId`` is absent only inside the commit that creates the
call (a creation that fails leaves no member); ``result`` is exactly what ``executeTool()`` returns, absent while running
and for a call the scheduler faulted or orphaned."""


async def resolveTool(runtime: Any, input: dict[str, Any], name: str, context: Context) -> dict[str, Any] | None:
    """The called tool: among the tools the model is offered for a model-issued call, else among those tools may
    call."""
    agent = await runtime.agent(context)
    tools = agent["tools"] if input["kind"] == "model" else agent["callable"]
    return next((tool for tool in tools if tool["name"] == name), None)


async def readCall(runtime: Any, input: dict[str, Any], context: Context) -> dict[str, Any]:
    """The call: from the input for a nested call, which hooks see with its parent (a ``ToolHookCall``), else from the
    assistant entry."""
    if input["kind"] == "nested":
        return {**input["call"], "parent": {"taskId": input["parent"], "callId": input["parentCallId"]}}
    entry = await runtime.entry(AssistantEntry, input["assistant"], context)
    model = None if entry is None else entry.get("model")
    message = model[0] if model else None
    call = None
    if message is not None and message["role"] == "assistant":
        call = next(
            (content for content in message["content"] if content["type"] == "toolCall" and content["id"] == input["callId"]),
            None,
        )
    if call is None:
        raise RuntimeError(f"Entry {input['assistant']} has no tool call {input['callId']}")
    return call  # type: ignore[no-any-return]


def prepare(tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """The call's arguments as repaired by the tool; a throwing repair makes them invalid. ``{"args": ...}`` or
    ``{"error": ...}``."""
    if tool.get("prepareArguments") is None:
        return {"args": args}
    try:
        return {"args": tool["prepareArguments"](args)}
    except Exception as error:  # noqa: BLE001 - a throwing repair makes the arguments invalid
        return {"error": errorMessage(error)}


def validate(tool: dict[str, Any], call: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Arguments validated and coerced against the implementation's schema."""
    try:
        declared = AiTool(name=tool["name"], description=tool["description"], parameters=tool["parameters"])
        plain = {key: value for key, value in call.items() if key != "parent"}
        # MISAKA fork: TypeBox lets an optional property hold `undefined` (a repair may set one); misaka.ai's jsonschema
        # validator would check `UNDEFINED` as a value, so it validates the arguments without such properties.
        defined = definedProperties(args)
        return {"args": validateToolArguments(declared, AiToolCall.model_validate({**plain, "arguments": defined}))}
    except Exception as error:  # noqa: BLE001 - invalid arguments are reported to the model
        return {"error": errorMessage(error)}


def definedProperties(value: Any) -> Any:
    """``value`` without object properties set to ``UNDEFINED``, as JSON sees it."""
    if isinstance(value, dict):
        return {key: definedProperties(item) for key, item in value.items() if item is not UNDEFINED}
    if isinstance(value, list):
        return [definedProperties(item) for item in value]
    return value


def invalid(message: str) -> dict[str, Any]:
    return harnessError("invalid_arguments", message)


async def run(
    runtime: Any, input: dict[str, Any], call: dict[str, Any], tool: dict[str, Any], args: dict[str, Any], context: Context
) -> None:
    """Execute with the resolved implementation, then settle its result."""
    outputLimits = tool.get("outputLimits") or {}
    limits: OutputLimits = {
        "maxBytes": outputLimits["maxBytes"] if outputLimits.get("maxBytes") is not None else DEFAULT_MAX_BYTES,
        "maxLines": outputLimits["maxLines"] if outputLimits.get("maxLines") is not None else DEFAULT_MAX_LINES,
        "retain": outputLimits["retain"] if outputLimits.get("retain") is not None else "head",
    }
    # What a running tool reported through its api: output, the last details, and diagnostics.
    reported: dict[str, Any] = {"output": OutputBuffer(limits), "limits": limits, "diagnostics": [], "details": UNDEFINED}
    streams = input["kind"] == "model" or input.get("progress") is not False
    progress = publishProgress(runtime, reported, streams, context)
    # Nested call admissions this invocation started; cleanup waits for them, so it sees every nested call.
    admissions: list[asyncio.Future[Any]] = []
    # A caller that can rerun reattaches to its nested calls and child tasks after a restart; any other caller never
    # resumes, so they are abandoned (`TaskOptions.abandonOnRestart`).
    resumes = tool.get("replay") == "safe"
    # Default keys: the order of this invocation's calls, so a rerun that calls in the same order reattaches.
    sequence = 0
    # This attempt's commit of the `MADE_NESTED_CALLS` memo.
    madeNestedMemo: asyncio.Future[Any] | None = None
    ended = False

    def assertLive() -> None:
        if ended:
            raise RuntimeError(f"Tool call {call['id']} has settled")

    def output(chunk: Any, skipped: dict[str, Any] | None = None) -> None:
        assertLive()
        if reported["output"].push(chunk, skipped):
            progress.mark()

    def retainedOutput() -> dict[str, Any]:
        assertLive()
        retained = reported["output"].snapshot()
        return {"text": retained["text"], "truncated": retained["droppedBytes"] > 0}

    def diagnostic(item: dict[str, Any]) -> None:
        assertLive()
        reported["diagnostics"].append(copyJson(item, {"omitUndefinedProperties": True}))
        progress.mark()

    @eager
    async def details(value: Any, detailsContext: Context) -> None:
        assertLive()
        signal = detailsContext.abortSignal
        if signal is not None:
            signal.throwIfAborted()
        reported["details"] = copyJson(value, {"omitUndefinedProperties": True})
        committed = progress.markAndWait()
        # Cancelling the wait leaves the update in place; the commit's own outcome stays observed.
        committed.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await awaitWithContext(committed, detailsContext)  # type: ignore[no-any-return]

    @eager
    async def commit(change: Any, commitContext: Context) -> Any:
        result: Any = UNDEFINED

        async def apply(tx: Tx, current: dict[str, Any]) -> None:
            nonlocal result
            result = await awaitValue(change(tx))

        await runtime.commit(apply, commitContext)
        return result

    @eager
    async def createTask(task: dict[str, Any], taskInput: Any, options: dict[str, Any], taskContext: Context) -> Any:
        """Create a task in one commit. A child task of a tool that is not replay-safe defaults to ``abandonOnRestart``:
        the tool never resumes after a restart, so nothing awaits the child then."""
        child = {"abandonOnRestart": True, **options} if options["ownership"]["kind"] == "task" and not resumes else options
        id: Any = None

        async def apply(tx: Tx, current: dict[str, Any]) -> None:
            nonlocal id
            id = await tx.createTask(task, taskInput, child)

        await runtime.commit(apply, taskContext)
        return id

    @eager
    async def executeTool(
        name: str, toolArgs: dict[str, Any], callContext: Context, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run tool ``name`` as a nested call of this one and wait for its ``NestedToolExecutionResult``; see the
        upstream ``ToolExecutionApi.executeTool``."""
        nonlocal sequence, madeNestedMemo
        options = options or {}
        assertLive()
        if options.get("key") is not None:
            key = options["key"]
            checkKey(key)
        else:
            sequence += 1
            key = str(sequence)
        # The memo first, once per attempt; a failed one is tried again by the next call.
        if madeNestedMemo is None:
            memo = runtime.memo(MADE_NESTED_CALLS, True, callContext)

            def forget(done: asyncio.Future[Any]) -> None:
                nonlocal madeNestedMemo
                if (done.cancelled() or done.exception() is not None) and madeNestedMemo is memo:
                    madeNestedMemo = None

            memo.add_done_callback(forget)
            madeNestedMemo = memo
        made = madeNestedMemo

        async def admit() -> Any:
            # Shielded: one call that stops waiting must not cancel the memo the others share.
            await asyncio.shield(made)
            return await startNestedCall(
                runtime,
                call["id"],
                name,
                toolArgs,
                {"key": key, "progress": options.get("progress"), "abandonOnRestart": not resumes},
                callContext,
            )

        # Registered now, so cleanup waits for it.
        admission = startTask(admit())
        admissions.append(admission)
        id = await admission
        settled = await runtime.waitForTask(id, callContext)
        state = await runtime.snapshot(NestedCallDoc, runtime.taskId, key, callContext)
        stored = None if state is None else state.get("result")
        # A copy the tool may change; a nested call the scheduler faulted or orphaned stored none.
        return fallbackResult(id, name, settled["state"]["outcome"]) if stored is None else copyJson(stored)  # type: ignore[no-any-return]

    api = SimpleNamespace(
        taskId=runtime.taskId,
        conversationId=runtime.conversationId,
        callId=call["id"],
        registry=runtime.registry,
        agent=runtime.agent,
        models=runtime.models,
        output=output,
        outputWindow=(
            {
                "maxBytes": limits["maxBytes"],
                "maxLines": limits["maxLines"],
                "minIntervalMs": runtime.settings["progress"]["outputIntervalMs"],
                "bytesPerSecond": PROGRESS_BYTES_PER_SECOND,
            }
            if limits["retain"] == "tail"
            else None
        ),
        retainedOutput=retainedOutput,
        diagnostic=diagnostic,
        details=details,
        commit=commit,
        memo=runtime.memo,
        createTask=createTask,
        getTask=runtime.getTask,
        waitForTask=runtime.waitForTask,
        conversation=runtime.conversation,
        snapshot=runtime.snapshot,
        snapshotAsOf=runtime.snapshotAsOf,
        watchDoc=runtime.watchDoc,
        executeTool=executeTool,
    )

    result: dict[str, Any]
    ending = COMPLETED
    # Execution time of this attempt; a rerun after recovery measures only itself.
    durationMs: int | None = None
    try:
        # Built for this call, so a rerun after recovery gets the conversation's environment at that time.
        env = await runtime.env(context)
        startedAt = time.perf_counter()
        try:
            result = await awaitValue(tool["execute"](args, SimpleNamespace(**{**vars(api), "env": env}), context))
        finally:
            durationMs = jsRound((time.perf_counter() - startedAt) * 1000)
    except GeneratorExit:
        # The coroutine is being closed (a loop closed without cancelling it): nothing may be awaited now.
        raise
    except BaseException as caught:
        # A CancelledError from something the tool awaited is a throw like any other (`isRejection`); the task's own
        # cancellation ends the call as an abort does: the waiters are rejected, then it propagates.
        if runtime.signal.aborted or not isRejection(caught):
            ended = True
            for waiter in await progress.stop():
                waiter.reject(asThrown(caught))
            raise
        error = thrownBy(caught)
        result = {"isError": True, "diagnostics": [toolDiagnostic("tool_error", errorMessage(error))]}
        # A throw, from ``execute()`` or from building the environment, ends the task ``failed``, which cancels what
        # the call owned; it no longer supervises it. The error text is already in the result.
        ending = {"status": "failed", "message": f"Tool {call['name']} threw"}
    ended = True
    # No admission starts after `ended`; let those underway commit, so settlement aborts and lists their calls.
    if len(admissions) > 0:
        await asyncio.wait([asyncio.shield(admission) for admission in admissions])
    reported["output"].end()
    # Details still waiting for a progress commit settle with the terminal commit, the final flush.
    pending = await progress.stop()
    try:
        settled = await finalResult(runtime, input, call, tool, result, reported, context)
        await settle(runtime, input, call, ending, lambda slot: settled, context, durationMs)
    except BaseException as error:
        for waiter in pending:
            waiter.reject(asThrown(error))
        raise
    for waiter in pending:
        waiter.resolve()


def publishProgress(runtime: Any, reported: dict[str, Any], enabled: bool, context: Context) -> Progress:
    """Throttled commits of what the tool reported into its slot, in ``pi.live.tools`` or ``pi.live.nestedTools``, each
    writing only what changed since the last one; none when ``enabled`` is false."""
    written: dict[str, Any] = {"text": "", "details": UNDEFINED, "diagnostics": 0}

    async def write() -> int:
        nonlocal written
        if not enabled:
            return 0
        # Capture everything synchronously: the tool keeps reporting while the commit is in flight.
        snapshot = reported["output"].snapshot()
        current = {"text": snapshot["text"], "details": reported["details"], "diagnostics": len(reported["diagnostics"])}
        added = reported["diagnostics"][written["diagnostics"] : current["diagnostics"]]
        detailsChanged = not strictEquals(current["details"], written["details"])
        # What the commit writes, as Chord diffs the string: an append, a trim plus an append of what follows the shared
        # part, or the whole window when its bounded overlap search finds nothing.
        size = 0
        if snapshot["text"] != written["text"]:
            shared = (
                jsLength(written["text"])
                if snapshot["text"].startswith(written["text"])
                else overlap(written["text"], snapshot["text"], 65_536)
            )
            size += utf8ByteLength(jsSlice(snapshot["text"], shared))
        if detailsChanged:
            size += utf8ByteLength(jsonStringify(None if current["details"] is UNDEFINED else current["details"]))
        if len(added) > 0:
            size += utf8ByteLength(jsonStringify(added))

        async def apply(tx: Tx, task: dict[str, Any]) -> None:
            slot = toolSlot(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
            if slot is None:
                return
            # REMINDER: assign ``output`` as one string field. Chord then diffs it into an append, or a trim plus an
            # append for a sliding tail; replacing the slot object would record the whole window on every commit.
            existing = slot.get("output", "")
            if existing != snapshot["text"]:
                slot["output"] = snapshot["text"]
            if snapshot["droppedBytes"] > 0:
                slot["droppedBytes"] = snapshot["droppedBytes"]
            if snapshot["droppedLines"] > 0:
                slot["droppedLines"] = snapshot["droppedLines"]
            # Diff details leaf by leaf and append new diagnostics, so each commit writes only what changed.
            if detailsChanged and current["details"] is not UNDEFINED:
                assignJson(slot, "details", current["details"])
            if len(added) > 0:
                if "diagnostics" not in slot:
                    slot["diagnostics"] = []
                for item in added:
                    slot["diagnostics"].push(item)
            return

        await runtime.commit(apply, context)
        written = current
        return size

    def onError(error: BaseException) -> None:
        # Rejections after an abort mark or close are expected; the committed state stays consistent.
        if not runtime.signal.aborted:
            runtime.report(error)

    return Progress(write, onError, runtime.settings["progress"]["outputIntervalMs"])


async def finalResult(
    runtime: Any,
    input: dict[str, Any],
    call: dict[str, Any],
    tool: dict[str, Any],
    result: dict[str, Any],
    reported: dict[str, Any],
    context: Context,
) -> dict[str, Any]:
    """The settled result: the tool's result with the retained output and last details as fallbacks, its diagnostics
    after those reported through the api, ``afterTool`` applied, explicit text bounded, and ``structuredOutput`` checked
    against the tool's schema, with the Harness's diagnostics last. A nested call whose ``structuredOutput`` breaks the
    contract gets an error result without it; a model-issued call only loses it, and the break is reported to the
    host."""
    harness: list[dict[str, Any]] = []
    retained = reported["output"].snapshot() if result.get("output") is None else None
    output = (
        result["output"]
        if retained is None
        else ([] if retained["text"] == "" else [{"type": "text", "text": retained["text"]}])
    )
    details = result.get("details", UNDEFINED)
    if details is UNDEFINED:
        details = reported["details"]
    final: dict[str, Any] = {
        **result,
        "output": output,
        **({} if details is UNDEFINED else {"details": details}),
        "diagnostics": [*reported["diagnostics"], *(result.get("diagnostics") or [])],
    }
    if details is UNDEFINED:
        final.pop("details", None)

    async def afterTool(hook: Any) -> None:
        nonlocal final
        replaced = await awaitValue(hook(call, final, runtime, context))
        final = replaced if replaced is not None else final

    await runtime.hooks.each("afterTool", afterTool)
    # The retained output's truncation applies only while afterTool kept that output.
    if retained is not None and retained["droppedBytes"] > 0 and final.get("output") is output:
        harness.append(truncated(retained, reported["limits"]["retain"]))
    bounded = boundContent(final.get("output") or [], reported["limits"])
    if bounded["droppedBytes"] > 0:
        harness.append(truncated(bounded, reported["limits"]["retain"]))
    broken = structuredOutputError(tool, final)
    if broken is not None:
        rest = {key: value for key, value in final.items() if key != "structuredOutput"}
        if input["kind"] == "nested":
            final = {**rest, "isError": True}
            harness.append(toolDiagnostic("invalid_structured_output", broken))
        else:
            # The model never sees structured output, so its call stands; the host learns of the broken tool.
            final = rest
            runtime.report(RuntimeError(broken))
    # Programs get the tool's own value: without a schema, its output. The model reads only `output`, so model-issued
    # calls skip both.
    if input["kind"] == "nested" and tool.get("structuredOutputSchema") is None:
        final = {**final, "structuredOutput": outputValue(bounded["content"])}
    # A schema tool's error result without structured output has its text only in the output, which programs never get:
    # it becomes an error diagnostic, as a thrown error's message does, unless one says what went wrong already.
    if (
        input["kind"] == "nested"
        and tool.get("structuredOutputSchema") is not None
        and final.get("isError") is True
        and final.get("structuredOutput", UNDEFINED) is UNDEFINED
        and not any(item["severity"] == "error" for item in [*(final.get("diagnostics") or []), *harness])
    ):
        text = "\n".join(item["text"] for item in bounded["content"] if item["type"] == "text")
        if text != "":
            harness.append(toolDiagnostic("tool_error", text))
    return {**final, "output": bounded["content"], "diagnostics": [*(final.get("diagnostics") or []), *harness]}


def structuredOutputError(tool: dict[str, Any], result: dict[str, Any]) -> str | None:
    """Why a result's ``structuredOutput`` breaks the tool's contract, or ``None`` when it keeps it. (Upstream caches
    compiled validators per schema in a ``WeakMap``; the TypeBox stand-in checks the schema directly.)"""
    schema = tool.get("structuredOutputSchema")
    value = result.get("structuredOutput", UNDEFINED)
    if schema is None:
        return (
            None
            if value is UNDEFINED
            else f"Tool {tool['name']} returned structuredOutput but declares no structuredOutputSchema"
        )
    if value is UNDEFINED:
        return None if result.get("isError") is True else f"Tool {tool['name']} returned no structuredOutput"
    if TypeBoxValue.Check(schema, value):
        return None
    first = next(iter(TypeBoxValue.Errors(schema, value)), None)
    path = re.sub(r"^/", "", "" if first is None else first["instancePath"]).replace("/", ".") or "root"
    message = "invalid" if first is None else first["message"]
    return f"Tool {tool['name']} returned structuredOutput that does not match its schema: {path}: {message}"


async def settle(
    runtime: Any,
    input: dict[str, Any],
    call: dict[str, Any],
    ending: dict[str, Any],
    build: Any,
    context: Context,
    durationMs: int | None = None,
) -> None:
    """Commit the tool's terminal state and mark its slot done. A model-issued call appends its result entry and ends
    with the entry ID; a nested call writes its result to the caller's ``NestedCallDoc``, records its usage, and ends
    with a small receipt. Nested calls this call left running are aborted first, so they report into their slots before
    the slots below the call leave ``pi.live``. ``build`` receives the slot so interruption and abort can report the
    durable partial output."""
    # A crash in between leaves the call in `execute`: a safe rerun reattaches to the calls, an interruption ends here.
    # Every live nested call the task owns, also those an earlier attempt made and this one never called again.
    nestedIds = [
        record["id"]
        for record in await runtime.ownedTasks(context)
        if record["kind"] == ToolTask["definition"]["name"] and record["input"].get("kind") == "nested"
    ]
    # Read before the settling commit: no admission starts after `ended`, and those underway were awaited.
    madeNested = (await runtime.memo(MADE_NESTED_CALLS, context)) is True
    # Mark them all before waiting for any: one may run until a sibling is cancelled.
    if len(nestedIds) > 0:
        await asyncio.gather(*[runtime.abortOwned(id, context) for id in nestedIds])

    async def apply(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        conversationId = runtime.conversationId
        live = await tx.doc(LiveDoc, conversationId)
        slot = toolSlot(live, runtime.taskId)
        result = build(slot)
        settled: dict[str, Any]
        if input["kind"] == "nested":
            nested = nestedResult(runtime.taskId, result, durationMs)
            if nested.get("usage") is not None:
                await recordUsage(tx, conversationId, "tools", call["name"], nested["usage"])
            # The caller is never terminal before its owned nested calls, so its document is open; another task in it is
            # a bug, not a race.
            state = await tx.doc(NestedCallDoc, input["parent"], input["key"], None)
            if state.get("taskId") != runtime.taskId:
                raise RuntimeError(f"Nested call {call['id']} is not in its caller's index")
            state["result"] = nested
            if slot is not None and "parentTaskId" in slot:
                finishSlot(slot)
                slot["summary"] = summaryOf(nested, result.get("output") or [])
            settled = {"kind": "nested"}
        else:
            timing = {"timestamp": runtime.now(), **({} if durationMs is None else {"durationMs": durationMs})}
            entry = await appendToolResult(tx, conversationId, call, result, timing)
            if slot is not None and "parentTaskId" not in slot:
                finishSlot(slot)
                slot["entry"] = entry["id"]
            # Tools build control objects freely; drop keys set to undefined so the task result is strict JSON.
            control = (
                {}
                if result.get("control") is None or ending["status"] != "completed"
                else {"control": copyJson(result["control"], {"omitUndefinedProperties": True})}
            )
            settled = {"kind": "model", "entryId": entry["id"], **control}
        # A call that made no nested calls, in any attempt, has no slots below it; skipping the scan keeps many leaves
        # linear. Committed before the read: no nested call is admitted after `ended`.
        if madeNested:
            removeNestedSlots(live, runtime.taskId)
        if ending["status"] == "aborted":
            return {"status": "terminal", "outcome": {"status": "aborted", "result": settled}}
        if ending["status"] == "failed":
            error = {"message": ending["message"]}
            return {"status": "terminal", "outcome": {"status": "failed", "error": error, "result": settled}}
        return {"status": "terminal", "outcome": {"status": "completed", "result": settled}}

    await runtime.commit(apply, context)


def nestedResult(taskId: int, result: dict[str, Any], durationMs: int | None) -> dict[str, Any]:
    """A nested call's result, as stored and returned: strict JSON, without ``output``, which only the model reads, and
    without ``control``, which only model-issued calls apply."""
    nested = {
        "taskId": taskId,
        "structuredOutput": result.get("structuredOutput", UNDEFINED),
        "isError": result["isError"] if result.get("isError") is not None else False,
        "details": result.get("details", UNDEFINED),
        "diagnostics": result.get("diagnostics") or [],
        "usage": result.get("usage", UNDEFINED),
        "durationMs": UNDEFINED if durationMs is None else durationMs,
    }
    return copyJson(nested, {"omitUndefinedProperties": True})  # type: ignore[return-value]


async def startNestedCall(
    runtime: Any, parentCallId: str, name: str, args: dict[str, Any], options: dict[str, Any], context: Context
) -> Any:
    """Create nested call ``key`` of the call ``parentCallId`` in one commit: its tool task, owned by the calling task,
    its slot, and its ``NestedCallDoc``. A key already made returns that call's task when it names the same tool and
    arguments."""
    key = options["key"]
    call = {
        "type": "toolCall",
        "id": f"{parentCallId}/{key}",
        "name": name,
        "arguments": copyJson(args, {"omitUndefinedProperties": True}),
    }
    id: Any = None

    async def apply(tx: Tx, current: dict[str, Any]) -> None:
        nonlocal id
        # Opened, or created empty for a key not made yet.
        state = await tx.doc(NestedCallDoc, runtime.taskId, key, None)
        existing = state.get("taskId", None)  # A draft answers `UNDEFINED` for a missing key.
        if existing is not None:
            found = await tx.task(existing)
            existingInput = None if found is None else found["input"]
            same = (
                existingInput is not None
                and existingInput.get("kind") == "nested"
                and existingInput["call"]["name"] == name
                and jsonEqual(existingInput["call"]["arguments"], call["arguments"])
            )
            if not same:
                raise RuntimeError(f"Nested call {call['id']} was already made with another tool or other arguments")
            id = existing
            return
        nestedInput = {
            "kind": "nested",
            "parent": runtime.taskId,
            "parentCallId": parentCallId,
            "key": key,
            "call": call,
            **({"progress": False} if options.get("progress") is False else {}),
        }
        created = await tx.createTask(
            ToolTask,
            nestedInput,
            {"ownership": {"kind": "task", "taskId": runtime.taskId}, "abandonOnRestart": options["abandonOnRestart"]},
        )
        state["taskId"] = created
        live = await tx.doc(LiveDoc, runtime.conversationId)
        if live.get("nestedTools", UNDEFINED) is UNDEFINED:
            live["nestedTools"] = []
        slot = {
            "callId": call["id"],
            "parentCallId": parentCallId,
            "parentTaskId": runtime.taskId,
            "name": name,
            "taskId": created,
            "arguments": call["arguments"],
            "status": "pending",
        }
        live["nestedTools"].push(slot)
        id = created

    await runtime.commit(apply, context)
    return id


def outputValue(output: list[dict[str, Any]]) -> Any:
    """What programs get of a tool's output when it declares no schema: one text item as its string, one image as
    itself, nothing as ``""``, and anything else as the content list."""
    if len(output) == 0:
        return ""
    if len(output) > 1:
        return output
    only = output[0]
    return only["text"] if only["type"] == "text" else only


def fallbackResult(taskId: int, name: str, outcome: dict[str, Any]) -> dict[str, Any]:
    """What the caller gets for a nested call without a stored result: one the scheduler faulted or orphaned."""
    status = outcome["status"]
    if status == "faulted":
        message = f"Tool {name} failed: {outcome['error']['message']}"
    elif status == "orphaned":
        message = f"Tool {name} could not resume: {outcome['reason']}"
    else:
        message = f"Tool {name} ended without a result"
    error = harnessError(status, message)
    return {"taskId": taskId, "isError": True, "diagnostics": error.get("diagnostics") or []}


COMPLETED: dict[str, Any] = {"status": "completed"}
"""How a tool task ends; the result is settled either way. ``failed`` (execution threw or was interrupted) records
cancellation intent for the conversations the call owns; a result with ``isError`` still completes."""


async def callPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    input = task["input"]
    call = await readCall(runtime, input, context)
    tool = await resolveTool(runtime, input, call["name"], context)
    if tool is None:
        error = harnessError("tool_unavailable", f"Tool {call['name']} is not available")
        await settle(runtime, input, call, COMPLETED, lambda slot: error, context)
        return
    prepared = prepare(tool, call["arguments"])
    checked = prepared if "error" in prepared else validate(tool, call, prepared["args"])
    if "error" in checked:
        await settle(runtime, input, call, COMPLETED, lambda slot: invalid(checked["error"]), context)
        return
    args = checked["args"]
    block: str | None = None

    async def beforeTool(hook: Any) -> None:
        nonlocal block, args
        if block is not None:
            return
        try:
            decision = await awaitValue(hook({**call, "arguments": args}, runtime, context))
            if decision is not None and decision.get("block") is not None:
                block = decision["block"]
            elif decision is not None and decision.get("arguments") is not None:
                args = decision["arguments"]
        except BaseException as error:
            if runtime.signal.aborted or not isRejection(error):
                raise
            block = errorMessage(thrownBy(error))

    await runtime.hooks.each("beforeTool", beforeTool)
    if block is not None:
        blocked = harnessError("blocked", f"Tool call blocked: {block}")
        await settle(runtime, input, call, COMPLETED, lambda slot: blocked, context)
        return
    validated = validate(tool, call, args)
    if "error" in validated:
        await settle(runtime, input, call, COMPLETED, lambda slot: invalid(validated["error"]), context)
        return
    # Strict JSON: repair may set optional properties to undefined, which the slot and checkpoint cannot hold.
    final = copyJson(validated["args"], {"omitUndefinedProperties": True})

    async def recordIntent(tx: Tx, current: dict[str, Any]) -> dict[str, Any]:
        slot = toolSlot(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
        if slot is not None:
            slot["status"] = "running"
        # A nested slot shows the arguments the call runs with, which repair, hooks, and coercion may have changed.
        # Compared with the input, which the slot got at admission: reading the draft would track every leaf.
        changed = input["kind"] == "nested" and not jsonEqual(input["call"]["arguments"], final)
        if changed and slot is not None and "parentTaskId" in slot:
            slot["arguments"] = final
        intent = {"phase": "execute", "arguments": final, "replay": tool["replay"] if tool.get("replay") is not None else "unsafe"}
        return {"status": "running", "checkpoint": intent}

    await runtime.commit(recordIntent, context)
    await run(runtime, input, call, tool, final, context)


async def executePhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    """Recovery after intent: rerun only when the stored and the current policy both say ``safe``."""
    input = task["input"]
    args = task["state"]["checkpoint"]["arguments"]
    replay = task["state"]["checkpoint"]["replay"]
    call = await readCall(runtime, input, context)
    tool = await resolveTool(runtime, input, call["name"], context)
    if replay == "safe" and tool is not None and tool.get("replay") == "safe":
        # The rerun reports from scratch; clear what the interrupted attempt published.
        async def clear(tx: Tx, current: dict[str, Any]) -> None:
            slot = toolSlot(await tx.doc(LiveDoc, runtime.conversationId), runtime.taskId)
            if slot is not None:
                clearProgress(slot)

        await runtime.commit(clear, context)
        await run(runtime, input, call, tool, args, context)
        return
    message = f"Tool {call['name']} was interrupted and may have partially run"
    # ``failed`` records cancellation intent, so the call's owned conversations, left unsupervised, are aborted.
    ending = {"status": "failed", "message": message}
    await settle(runtime, input, call, ending, lambda slot: fromSlot(slot, "interrupted", message), context)


async def abortPhase(task: dict[str, Any], runtime: Any, context: Context) -> None:
    input = task["input"]
    call = await readCall(runtime, input, context)
    # Abandoned after a restart (`TaskOptions.abandonOnRestart`): say whether the call may have run.
    if task.get("abortReason") != "restart":
        code, message = "aborted", f"Tool {call['name']} was aborted"
    elif task["state"]["checkpoint"]["phase"] == "execute":
        code, message = "interrupted", f"Tool {call['name']} was interrupted by a restart and may have partially run"
    else:
        code, message = "abandoned", f"Tool {call['name']} was not started: its caller ended with a restart"
    await settle(runtime, input, call, {"status": "aborted"}, lambda slot: fromSlot(slot, code, message), context)


def migrateToolTask(input: Any, checkpoint: Any, _version: int) -> dict[str, Any]:
    """Version 1 had only model-issued calls."""
    return {"input": {"kind": "model", **input}, "checkpoint": checkpoint}


ToolTask = defineTask(
    {
        "name": "pi.tool",
        "version": 2,
        "initial": lambda input: {"phase": "call"},
        "migrate": migrateToolTask,
        "phases": {"call": callPhase, "execute": executePhase},
        "abort": abortPhase,
    }
)
"""Built-in tool task: resolves the called tool among its phase agent's tools, validates, runs ``beforeTool``, records
intent, executes, runs ``afterTool``, and settles the result, all in one ``call`` handler so nothing separates resolution
from settlement. ``execute`` is reached only by recovery and applies the replay rule. A model-issued call settles by
appending its result entry; a nested call by writing its result to its caller's ``NestedCallDoc``."""


def fromSlot(slot: Any, code: str, message: str) -> dict[str, Any]:
    """An error result from the slot's durable partial output, details, and diagnostics."""
    diagnostics = list(slot["diagnostics"]) if slot is not None and "diagnostics" in slot else []
    droppedBytes = slot["droppedBytes"] if slot is not None and "droppedBytes" in slot else 0
    if droppedBytes > 0:
        droppedLines = slot.get("droppedLines", 0)
        diagnostics.append(truncated({"droppedBytes": droppedBytes, "droppedLines": droppedLines}))
    diagnostics.append(toolDiagnostic(code, message))
    output = slot["output"] if slot is not None and "output" in slot else None
    return {
        "output": [] if output is None or output == "" else [{"type": "text", "text": output}],
        "isError": True,
        **({} if slot is None or "details" not in slot else {"details": slot["details"]}),
        "diagnostics": diagnostics,
    }


def harnessError(code: str, message: str) -> dict[str, Any]:
    """An error result the Harness writes itself: no content and one ``error`` diagnostic with ``code``."""
    return {"output": [], "isError": True, "diagnostics": [toolDiagnostic(code, message)]}


def toolDiagnostic(code: str, message: str) -> dict[str, Any]:
    return {"severity": "error", "code": code, "message": message}


def truncated(dropped: dict[str, Any], retain: str | None = None) -> dict[str, Any]:
    """The Harness's truncation diagnostic; ``retain`` is unknown when rebuilt from a slot after recovery."""
    kept = "" if retain is None else f" to its {'beginning' if retain == 'head' else 'end'}"
    return {
        "severity": "warn",
        "code": "truncated",
        "message": f"Output truncated{kept}: {dropped['droppedLines']} lines, {dropped['droppedBytes']} bytes dropped",
    }


MAX_SUMMARY_ERROR_CHARS = 500
"""Error text a nested call's summary keeps."""


def summaryOf(result: dict[str, Any], output: list[dict[str, Any]]) -> dict[str, Any]:
    """How a nested call ended, for its slot: error text, from its diagnostics or else its ``output``, bounded."""
    error = jsSlice(errorTextOf(result, output), 0, MAX_SUMMARY_ERROR_CHARS) if result["isError"] else ""
    return {
        "isError": result["isError"],
        **({} if result.get("durationMs") is None else {"durationMs": result["durationMs"]}),
        **({} if result.get("usage") is None else {"usage": result["usage"]}),
        **({} if error == "" else {"error": error}),
    }


def errorTextOf(result: dict[str, Any], output: list[dict[str, Any]]) -> str:
    """The error messages of a nested result, else the text items of its output, joined with newlines."""
    errors = [diagnostic for diagnostic in result["diagnostics"] if diagnostic["severity"] == "error"]
    if len(errors) > 0:
        return "\n".join(diagnostic["message"] for diagnostic in errors)
    return "\n".join(item["text"] for item in output if item["type"] == "text")


async def appendToolResult(
    tx: Tx, conversationId: ConversationId, call: dict[str, Any], result: dict[str, Any], meta: dict[str, Any]
) -> dict[str, Any]:
    """Append a ``pi.tool-result`` entry. The content ends with the rendered diagnostics, so the stored message is exactly
    what the model sees; ``data`` keeps the structured list. A result's usage is added to ``pi.usage`` in the same
    commit. ``meta`` is ``{"timestamp", "durationMs"?}``."""
    timestamp = meta["timestamp"]
    durationMs = meta.get("durationMs")
    diagnostics = list(result.get("diagnostics") or [])
    content = list(result.get("output") or [])
    if len(diagnostics) > 0:
        content.append({"type": "text", "text": renderDiagnostics(diagnostics)})
    details = result.get("details", UNDEFINED)
    message = {
        "role": "toolResult",
        "toolCallId": call["id"],
        "toolName": call["name"],
        "content": content,
        **({} if details is UNDEFINED else {"details": details}),
        **({} if result.get("usage") is None else {"usage": result["usage"]}),
        "isError": result["isError"] if result.get("isError") is not None else False,
        **({} if durationMs is None else {"durationMs": durationMs}),
        "timestamp": timestamp,
    }
    if result.get("usage") is not None:
        await recordUsage(tx, conversationId, "tools", call["name"], result["usage"])
    return await tx.appendEntry(ToolResultEntry, conversationId, {"model": [message], "data": {"diagnostics": diagnostics}})  # type: ignore[no-any-return]


def renderDiagnostics(diagnostics: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"[{diagnostic['severity']}] {diagnostic['message']}" for diagnostic in diagnostics)
    return f"<harness>\n{lines}\n</harness>"


def boundContent(content: list[dict[str, Any]], limits: OutputLimits) -> dict[str, Any]:
    """Bound the text of result content. When the joined text exceeds the limits, the text items are replaced by one
    bounded item at the position of the first (head) or last (tail) text item; other content is kept."""
    texts = [item for item in content if item["type"] == "text"]
    bounded = boundOutput("".join(item["text"] for item in texts), limits)
    if bounded["droppedBytes"] == 0:
        return {"content": content, "droppedBytes": 0, "droppedLines": 0}
    keep = texts[0] if limits["retain"] == "head" else texts[-1]
    result: list[dict[str, Any]] = []
    for item in content:
        if item["type"] != "text":
            result.append(item)
        elif item is keep:
            result.append({**item, "text": bounded["text"]})
    return {"content": result, "droppedBytes": bounded["droppedBytes"], "droppedLines": bounded["droppedLines"]}


_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


def checkKey(key: str) -> None:
    """Explicit nested call keys are path segments of the call ID, so IDs of different nested calls never collide, and
    are never plain positive integers, which default keys use. ``__proto__`` stays reserved, as it was when keys indexed
    a JSON object."""
    if key == "" or "/" in key or key == "__proto__" or _POSITIVE_INTEGER.fullmatch(key) is not None:
        raise RuntimeError(
            f'Nested call key {jsonStringify(key)} must be non-empty, without "/", not "__proto__", and not a positive integer'
        )


def jsonEqual(a: Any, b: Any) -> bool:
    """Structural JSON equality; object key order does not matter."""
    if a is b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if not isinstance(a, list | dict) or not isinstance(b, list | dict):
        return not isinstance(a, list | dict) and not isinstance(b, list | dict) and a == b
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
            jsonEqual(item, b[index]) for index, item in enumerate(a)
        )
    return len(a) == len(b) and all(key in b and jsonEqual(value, b[key]) for key, value in a.items())


__all__ = [
    "NestedCallDoc",
    "ToolTask",
    "appendToolResult",
    "harnessError",
]
