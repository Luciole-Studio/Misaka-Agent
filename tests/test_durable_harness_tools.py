"""pi durable's ``test/harness-tools.test.ts``."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    AgentDoc,
    GenerationTask,
    LiveDoc,
    MemoryStorage,
    ToolResultEntry,
    ToolTask,
    configure,
    defineExtension,
    defineTask,
    defineTool,
    hook,
    section,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import createBashTool, createEditTool, createReadTool
from misaka.utils.js import UNDEFINED
from tests.chat_support import allEntries, chatSetup, openChat, requestText, textOf
from tests.durable_support import AnyOf, ArrayContaining, context, toMatchObject
from tests.harness_support import addHooks, addTask, addTool
from tests.task_support import aborted, deferred

EchoParameters = {"type": "object", "properties": {"text": {"type": "string"}}}


def tool(name: str, execute: Callable[..., Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return defineTool(
        {
            "name": name,
            "description": f"The {name} tool",
            "parameters": EchoParameters,
            "execute": execute,
            **(extra or {}),
        }
    )


def calls(*items: tuple[str, dict[str, Any], str]) -> Any:
    """A tool-calling answer with one call per ``(name, args, id)``."""
    return fauxAssistantMessage([fauxToolCall(name, args, {"id": id}) for name, args, id in items], {"stopReason": "toolUse"})


DONE = fauxAssistantMessage([fauxText("done")])


async def run(
    setup: Any,
    responses: list[Any],
    prepare: Callable[[Any, Any], Any] | None = None,
    options: dict[str, Any] | None = None,
) -> SimpleNamespace:
    setup.faux.setResponses(responses)
    opened = await openChat(MemoryStorage(), setup, options or {})
    harness, root = opened.harness, opened.root
    if prepare is not None:
        await prepare(harness, root)
    settled = await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    return SimpleNamespace(harness=harness, root=root, entries=await allEntries(root), status=settled["status"])


def results(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["model"][0] for entry in entries if ToolResultEntry.is_(entry)]


def resultText(message: dict[str, Any]) -> str:
    return "|".join(item["text"] if item["type"] == "text" else f"[{item['type']}]" for item in message["content"])


def consume(future: Any) -> None:
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def test_tool_round_runs_input_tool_call_tool_result_and_answer_and_settles_the_input():
    setup = chatSetup()

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": f"echo {args.get('text')}"}]}

    addTool(setup.registry, tool("echo", echo))
    result = await run(setup, [calls(("echo", {"text": "hi"}, "c1")), DONE])
    harness, root, entries = result.harness, result.root, result.entries
    assert result.status == "done"
    assert [entry["kind"] for entry in entries] == [
        "pi.user",
        "pi.system",
        "pi.assistant",
        "pi.tool-result",
        "pi.assistant",
    ]
    system = entries[1]["model"][0]
    assert system["toolsAdded"] == [{"name": "echo", "description": "The echo tool", "parameters": AnyOf(dict)}]
    [first] = results(entries)
    toMatchObject(first, {"toolCallId": "c1", "toolName": "echo", "isError": False})
    assert resultText(first) == "echo hi"
    assert ToolResultEntry.is_(entries[3])
    assert entries[3]["data"] == {"diagnostics": []}
    assert entries[3].get("byTaskId") is not None
    # The second request sees the tool result right after its call.
    assert setup.faux.state.callCount == 2
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


# #10395
async def test_tool_round_gives_tools_and_hooks_the_harnesss_models():
    setup = chatSetup()
    seen: list[Any] = []

    async def echo(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        seen.append(api.models)
        return {"output": []}

    def beforeTool(_call: Any, api: Any, *_: Any) -> None:
        seen.append(api.models)

    addTool(setup.registry, tool("echo", echo))
    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    result = await run(setup, [calls(("echo", {}, "c1")), DONE])
    assert result.status == "done"
    assert seen == [setup.models, setup.models]
    assert all(models is setup.models for models in seen)
    await result.harness.close(context)


async def test_tool_round_answers_calls_to_tools_the_request_did_not_offer_without_a_task():
    setup = chatSetup()

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(setup.registry, tool("echo", echo))
    result = await run(setup, [calls(("ghost", {}, "c1"), ("echo", {}, "c2")), DONE])
    harness, root, entries = result.harness, result.root, result.entries
    assert result.status == "done"
    ghost, echoResult = results(entries)
    toMatchObject(ghost, {"toolCallId": "c1", "isError": True})
    assert resultText(ghost) == "<harness>\n[error] Tool ghost is not available\n</harness>"
    toMatchObject(echoResult, {"toolCallId": "c2", "isError": False})
    ghostEntry = next(
        entry for entry in entries if ToolResultEntry.is_(entry) and entry["model"][0]["role"] == "toolResult"
    )
    assert ghostEntry["data"] == {
        "diagnostics": [{"severity": "error", "code": "tool_unavailable", "message": "Tool ghost is not available"}]
    }
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 20), context)
    assert len([task for task in tasks["items"] if task["kind"] == "pi.tool"]) == 1
    await harness.close(context)


async def test_tool_round_answers_a_call_to_a_tool_deactivated_after_preparation_with_tool_unavailable():
    setup = chatSetup()
    seen: list[str] = []

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        seen.append("ran")
        return {"output": []}

    addTool(setup.registry, tool("echo", echo))
    holder: dict[str, Any] = {}

    async def deactivate(*_: Any) -> Any:
        await holder["root"].configure({"tools": []}, context)
        return calls(("echo", {}, "c1"))

    async def prepare(_harness: Any, conversation: Any) -> None:
        holder["root"] = conversation

    result = await run(setup, [deactivate, DONE], prepare)
    assert seen == []
    assert resultText(results(result.entries)[0]) == "<harness>\n[error] Tool echo is not available\n</harness>"
    # The next preparation removes it.
    systems = [entry for entry in result.entries if entry["kind"] == "pi.system"]
    assert systems[-1]["model"][0]["toolsRemoved"] == [{"name": "echo"}]
    await result.harness.close(context)


async def test_tool_round_removes_unregistered_active_tools_from_the_offer_and_adds_them_back_after_re_registration():
    setup = chatSetup()

    async def echoRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    echo = tool("echo", echoRun)
    registration = addTool(setup.registry, echo)
    first = await run(setup, [DONE])
    registration.dispose()
    setup.faux.setResponses([calls(("echo", {}, "c1")), DONE])
    second = await (await first.root.submit({"type": "input", "content": "again"}, context)).wait(context)
    assert second["status"] == "done"
    entries = await allEntries(first.root)
    removal = [entry for entry in entries if entry["kind"] == "pi.system"][-1]["model"][0]
    assert removal["toolsRemoved"] == [{"name": "echo"}]
    toMatchObject(results(entries)[0], {"isError": True})
    # The stored agent is not rewritten; the tool is only not resolved.
    assert (await first.root.agent(context))["tools"] == []

    addTool(setup.registry, echo)
    setup.faux.setResponses([DONE])
    await (await first.root.submit({"type": "input", "content": "back"}, context)).wait(context)
    entries = await allEntries(first.root)
    addition = [entry for entry in entries if entry["kind"] == "pi.system"][-1]["model"][0]
    assert [declared["name"] for declared in addition["toolsAdded"]] == ["echo"]
    await first.harness.close(context)


async def test_tool_round_produces_tool_unavailable_when_the_implementation_is_unregistered_before_its_task_runs():
    setup = chatSetup()
    installed: dict[str, Any] = {}

    async def firstRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        installed["second"].dispose()
        return {"output": []}

    addTool(setup.registry, tool("first", firstRun, {"executionMode": "sequential"}))

    async def secondRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "ran"}]}

    installed["second"] = addTool(setup.registry, tool("second", secondRun))
    result = await run(setup, [calls(("first", {}, "c1"), ("second", {}, "c2")), DONE])
    assert result.status == "done"
    late = results(result.entries)[1]
    assert resultText(late) == "<harness>\n[error] Tool second is not available\n</harness>"
    await result.harness.close(context)


async def test_tool_round_reads_the_execution_mode_when_a_round_starts_and_keeps_it_for_the_round():
    setup = chatSetup()
    events: list[str] = []

    def slow(name: str) -> Callable[..., Any]:
        async def execute(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
            events.append(f"start {name}")
            # Changed after the round started: not seen by this round.
            setup.settings["toolExecution"] = "parallel"
            await asyncio.sleep(0.02)
            events.append(f"end {name}")
            return {"output": []}

        return execute

    addTool(setup.registry, tool("a", slow("a")))
    addTool(setup.registry, tool("b", slow("b")))

    # Changed while the model request runs: the round that follows uses it.
    def request(*_: Any) -> Any:
        setup.settings["toolExecution"] = "sequential"
        return calls(("a", {}, "c1"), ("b", {}, "c2"))

    result = await run(setup, [request, DONE])
    assert events == ["start a", "end a", "start b", "end b"]
    await result.harness.close(context)


async def test_tool_round_runs_a_round_in_parallel_by_default_and_sequentially_when_configured_or_required_by_a_tool():
    async def trace(setup: Any) -> list[str]:
        events: list[str] = []

        def slow(name: str) -> Callable[..., Any]:
            async def execute(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
                events.append(f"start {name}")
                await asyncio.sleep(0.02)
                events.append(f"end {name}")
                return {"output": []}

            return execute

        if setup.registry.snapshot().extension("tool:a") is None:
            addTool(setup.registry, tool("a", slow("a")))
        if setup.registry.snapshot().extension("tool:b") is None:
            addTool(setup.registry, tool("b", slow("b")))
        result = await run(setup, [calls(("a", {}, "c1"), ("b", {}, "c2")), DONE])
        await result.harness.close(context)
        return events

    assert (await trace(chatSetup()))[:2] == ["start a", "start b"]
    configured = chatSetup()
    configured.settings["toolExecution"] = "sequential"
    sequential = await trace(configured)
    assert sequential == ["start a", "end a", "start b", "end b"]
    perTool = chatSetup()
    events: list[str] = []

    async def aRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        events.append("a")
        return {"output": []}

    async def bRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        events.append("b")
        return {"output": []}

    addTool(perTool.registry, tool("a", aRun, {"executionMode": "sequential"}))
    addTool(perTool.registry, tool("b", bRun))
    result = await run(perTool, [calls(("a", {}, "c1"), ("b", {}, "c2")), DONE])
    tasks = await result.harness.commit(lambda tx: tx.scanTasks({"conversationId": result.root.id}, 20), context)
    tools = sorted((task for task in tasks["items"] if task["kind"] == "pi.tool"), key=lambda task: task["id"])
    # The generation owns both tools and creates the second only after the first ended.
    generation = next(task for task in tasks["items"] if task["kind"] == "pi.generation")
    assert [task.get("owner") for task in tools] == [generation["id"], generation["id"]]
    assert events == ["a", "b"]
    await result.harness.close(context)


async def test_tool_results_use_retained_output_and_the_last_details_when_the_result_omits_them_with_diagnostics_in_order():
    setup = chatSetup()

    async def log(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("line 1\n")
        api.output(b"line 2\nline 3\n")
        api.diagnostic({"severity": "info", "message": "from api"})
        await api.details({"step": 1}, context)
        await api.details({"step": 2}, context)
        return {"diagnostics": [{"severity": "warn", "message": "from result"}]}

    addTool(setup.registry, tool("log", log, {"outputLimits": {"maxLines": 2}}))
    result = await run(setup, [calls(("log", {}, "c1")), DONE])
    [first] = results(result.entries)
    assert first["details"] == {"step": 2}
    assert resultText(first) == (
        "line 1\nline 2\n|<harness>\n[info] from api\n[warn] from result\n"
        "[warn] Output truncated to its beginning: 1 lines, 7 bytes dropped\n</harness>"
    )
    entry = next(candidate for candidate in result.entries if ToolResultEntry.is_(candidate))
    assert [diagnostic.get("code") for diagnostic in entry["data"]["diagnostics"]] == [None, None, "truncated"]
    await result.harness.close(context)


async def test_tool_results_offer_the_tail_window_with_the_configured_pace_not_a_head_window_and_accept_skipped_output():
    setup = chatSetup()
    setup.settings = {"progress": {"outputIntervalMs": 250}}
    windows: list[Any] = []

    async def tailed(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        windows.append(api.outputWindow)
        api.output("dropped\n")
        api.output("x\ny\n", {"bytes": 8, "newlines": 1, "endsWithNewline": True})
        return {}

    async def headed(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        windows.append(api.outputWindow)
        return {}

    addTool(setup.registry, tool("tailed", tailed, {"outputLimits": {"maxLines": 1, "retain": "tail"}}))
    addTool(setup.registry, tool("headed", headed, {"outputLimits": {"maxLines": 1}}))
    result = await run(setup, [calls(("tailed", {}, "c1"), ("headed", {}, "c2")), DONE])
    assert {"maxBytes": 50 * 1024, "maxLines": 1, "minIntervalMs": 250, "bytesPerSecond": 100 * 1024} in windows
    assert None in windows
    # 8 bytes written, 8 skipped, then "x\n" dropped by the window: 3 lines, 18 bytes in all.
    tailedResult = next(each for each in results(result.entries) if each["toolCallId"] == "c1")
    assert resultText(tailedResult) == (
        "y\n|<harness>\n[warn] Output truncated to its end: 3 lines, 18 bytes dropped\n</harness>"
    )
    await result.harness.close(context)


async def test_tool_results_bound_explicit_text_content_and_keep_other_content():
    setup = chatSetup()
    image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}

    async def big(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "a\nb\n"}, image, {"type": "text", "text": "c\nd\n"}]}

    addTool(setup.registry, tool("big", big, {"outputLimits": {"maxLines": 2, "retain": "tail"}}))
    result = await run(setup, [calls(("big", {}, "c1")), DONE])
    assert resultText(results(result.entries)[0]) == (
        "[image]|c\nd\n|<harness>\n[warn] Output truncated to its end: 2 lines, 4 bytes dropped\n</harness>"
    )
    await result.harness.close(context)


async def test_tool_results_turn_a_throw_into_a_tool_error_result_with_the_partial_output():
    setup = chatSetup()

    async def fail(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("partial\n")
        raise RuntimeError("boom")

    addTool(setup.registry, tool("fail", fail))
    result = await run(setup, [calls(("fail", {}, "c1")), DONE])
    assert result.status == "done"
    [first] = results(result.entries)
    assert first["isError"] is True
    assert resultText(first) == "partial\n|<harness>\n[error] boom\n</harness>"
    await result.harness.close(context)


# #10549
async def test_tool_results_record_how_long_execute_took_excluding_hooks_and_nothing_for_calls_that_did_not_run():
    setup = chatSetup()

    async def slow(*_: Any) -> dict[str, Any]:
        await asyncio.sleep(0.03)
        return {"output": []}

    async def thrower(*_: Any) -> dict[str, Any]:
        await asyncio.sleep(0.03)
        raise RuntimeError("boom")

    async def beforeTool(call: Any, *_: Any) -> Any:
        await asyncio.sleep(0.1)
        return {"block": "no"} if call["id"] == "blocked" else None

    addTool(setup.registry, tool("slow", slow))
    addTool(setup.registry, tool("thrower", thrower))
    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    result = await run(
        setup, [calls(("slow", {}, "slow"), ("thrower", {}, "thrower"), ("slow", {}, "blocked")), DONE]
    )
    byId = {each["toolCallId"]: each for each in results(result.entries)}
    for id in ["slow", "thrower"]:
        assert byId[id]["durationMs"] >= 25
        assert byId[id]["durationMs"] < 100
    assert byId["thrower"]["isError"] is True
    assert "durationMs" not in byId["blocked"]
    await result.harness.close(context)


async def test_tool_results_validate_arguments_before_and_after_before_tool_and_apply_blocks_and_replacements():
    setup = chatSetup()
    seen: list[dict[str, Any]] = []

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        seen.append(args)
        return {"output": []}

    addTool(setup.registry, tool("echo", echo))

    def beforeTool(call: dict[str, Any], *_: Any) -> Any:
        if call["id"] == "block":
            return {"block": "not today"}
        if call["id"] == "throw":
            raise RuntimeError("hook failed")
        if call["id"] == "bad":
            return {"arguments": {"text": {"not": "a string"}}}
        return {"arguments": {"text": f"{call['arguments'].get('text')}!"}}

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    result = await run(
        setup,
        [
            calls(
                ("echo", {"text": 1}, "coerced"),
                ("echo", {"text": {"not": "a string"}}, "invalid"),
                ("echo", {}, "block"),
                ("echo", {}, "throw"),
                ("echo", {"text": "x"}, "bad"),
                ("echo", {"text": "x"}, "ok"),
            ),
            DONE,
        ],
    )
    # Parallel tools append their results in completion order.
    byId = {item["toolCallId"]: [item["isError"], resultText(item)] for item in results(result.entries)}
    assert byId["block"] == [True, "<harness>\n[error] Tool call blocked: not today\n</harness>"]
    assert byId["throw"] == [True, "<harness>\n[error] Tool call blocked: hook failed\n</harness>"]
    assert byId["bad"][0] is True
    assert "Validation failed" in byId["bad"][1]
    assert byId["ok"] == [False, ""]
    assert byId["invalid"][0] is True
    assert "Validation failed" in byId["invalid"][1]
    # pi-ai coerces a number to a string before the first validation.
    assert byId["coerced"] == [False, ""]
    assert seen == ArrayContaining([{"text": "1!"}, {"text": "x!"}])
    assert len(seen) == 2
    await result.harness.close(context)


async def test_tool_results_repair_arguments_with_prepare_arguments_before_validation_and_a_throwing_repair_is_invalid():
    setup = chatSetup()
    seen: list[dict[str, Any]] = []

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        seen.append(args)
        return {"output": []}

    def prepareArguments(args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text")
        if text == "throw":
            raise RuntimeError("cannot repair")
        return {"text": f"#{text}"} if isinstance(text, int) and not isinstance(text, bool) else args

    addTool(setup.registry, tool("echo", echo, {"prepareArguments": prepareArguments}))
    result = await run(setup, [calls(("echo", {"text": 7}, "fixed"), ("echo", {"text": "throw"}, "broken")), DONE])
    byId = {item["toolCallId"]: resultText(item) for item in results(result.entries)}
    assert byId["fixed"] == ""
    assert byId["broken"] == "<harness>\n[error] cannot repair\n</harness>"
    assert seen == [{"text": "#7"}]
    # The stored call keeps what the model sent.
    call = next(item for item in result.entries[2]["model"][0]["content"] if item["type"] == "toolCall")
    assert call["arguments"] == {"text": 7}
    await result.harness.close(context)


async def test_tool_results_let_the_first_before_tool_block_win_and_skip_later_handlers():
    setup = chatSetup()

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(setup.registry, tool("echo", echo))
    asked: list[str] = []

    def first(*_: Any) -> Any:
        asked.append("first")
        return {"block": "first says no"}

    def second(*_: Any) -> Any:
        asked.append("second")
        return {"block": "second says no"}

    addHooks(setup.registry, ToolTask, {"beforeTool": first})
    addHooks(setup.registry, ToolTask, {"beforeTool": second})
    result = await run(setup, [calls(("echo", {}, "c1")), DONE])
    assert asked == ["first"]
    assert resultText(results(result.entries)[0]) == "<harness>\n[error] Tool call blocked: first says no\n</harness>"
    await result.harness.close(context)


async def test_tool_results_chain_after_tool_replacements_and_observe_the_round_with_after_tools():
    setup = chatSetup()

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "raw"}]}

    addTool(setup.registry, tool("echo", echo))
    addHooks(
        setup.registry,
        ToolTask,
        {"afterTool": lambda _call, result, *_: {**result, "output": [{"type": "text", "text": "first"}]}},
    )

    def replaced(_call: Any, result: Any, *_: Any) -> dict[str, Any]:
        texts = [item["text"] if item["type"] == "text" else "" for item in result.get("output") or []]
        return {**result, "details": {"replaced": "|".join(texts)}}

    addHooks(setup.registry, ToolTask, {"afterTool": replaced})
    observed: list[Any] = []

    def afterTools(assistant: Any, entries: Any, *_: Any) -> None:
        observed.extend([assistant, entries])

    addHooks(setup.registry, GenerationTask, {"afterTools": afterTools})
    result = await run(setup, [calls(("echo", {}, "c1")), DONE])
    [first] = results(result.entries)
    assert resultText(first) == "first"
    assert first["details"] == {"replaced": "first"}
    resultEntry = next(entry for entry in result.entries if ToolResultEntry.is_(entry))
    assert observed == [result.entries[2]["id"], [resultEntry["id"]]]
    await result.harness.close(context)


async def test_tool_results_run_the_hooks_of_the_selected_extensions_a_task_owned_child_copies_its_owners_selection():
    setup = chatSetup()
    calledIn: list[int] = []

    async def echoRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    Echo = defineExtension({"name": "echo", "tools": [tool("echo", echoRun)]})

    def audit(_call: Any, api: Any, *_: Any) -> None:
        calledIn.append(api.conversationId)

    Audit = defineExtension({"name": "audit", "hooks": [hook(ToolTask, {"beforeTool": audit})]})
    setup.registry.install(Echo)
    setup.registry.install(Audit)
    # Audit is installed but not in the default selection.
    setup.settings["extensions"] = [Echo]
    first = await run(setup, [DONE])
    await first.root.configure({"extensions": {"add": [Audit]}}, context)

    async def never(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    # An owner task no registered definition takes stays live and pending.
    owner = defineTask(
        {
            "name": "test.owner",
            "version": 1,
            "initial": lambda input: {"phase": "never"},
            "phases": {"never": never},
            "abort": never,
        }
    )

    async def createChild(tx: Any) -> int:
        taskId = await tx.createTask(
            owner, {}, {"ownership": {"kind": "conversation"}, "conversationId": first.root.id}
        )
        return (await tx.createConversation({"ownership": {"kind": "task", "taskId": taskId}}))["id"]  # type: ignore[no-any-return]

    childId = await first.harness.commit(createChild, context)
    child = await first.harness.conversation(childId, context)
    other = await first.harness.createConversation(
        {"ownership": {"kind": "ownerless"}, "agent": {"model": {"provider": "faux", "modelId": "faux-1"}}}, context
    )
    for conversation in (first.root, child, other):
        setup.faux.setResponses([calls(("echo", {}, "c1")), DONE])
        await (await conversation.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert calledIn == [first.root.id, child.id]
    await first.harness.close(context)


async def test_tool_results_apply_add_tools_and_terminate_only_when_every_result_of_the_round_asks_to():
    setup = chatSetup()

    async def stopRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [], "control": {"terminate": True}}

    async def growRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [], "control": {"addTools": ["extra", "stop"]}}

    async def extraRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    stop = tool("stop", stopRun)
    grow = tool("grow", growRun)
    addTool(setup.registry, stop)
    addTool(setup.registry, grow)
    addTool(setup.registry, tool("extra", extraRun))

    async def prepare(_harness: Any, root: Any) -> None:
        await root.configure({"tools": [stop, grow]}, context)

    first = await run(setup, [calls(("stop", {}, "c1"))], prepare)
    assert first.status == "done"
    assert first.entries[-1]["kind"] == "pi.tool-result"
    settled = await first.harness.commit(lambda tx: tx.scanTasks({"conversationId": first.root.id}, 20), context)
    assert all(task["state"]["status"] == "terminal" for task in settled["items"])

    setup.faux.setResponses([calls(("stop", {}, "c1"), ("grow", {}, "c2")), DONE])
    second = await (await first.root.submit({"type": "input", "content": "again"}, context)).wait(context)
    assert second["status"] == "done"
    assert (await allEntries(first.root))[-1]["kind"] == "pi.assistant"
    # addTools appends to the stored tool array, skipping names it already holds.
    assert (await first.harness.snapshot(AgentDoc, first.root.id, context))["tools"] == ["stop", "grow", "extra"]
    await first.harness.close(context)


async def test_generation_hooks_replace_request_messages_observe_responses_and_continue_on_yield():
    setup = chatSetup()
    requests: list[list[str]] = []

    def record(request: Any, *_: Any) -> Any:
        requests.append([f"{message.role}:{requestText(message) or ''}" for message in request.messages])
        return fauxAssistantMessage([fauxText(f"answer {len(requests)}")])

    addHooks(
        setup.registry,
        GenerationTask,
        {
            "beforeRequest": lambda arg, *_: {
                "messages": [*arg["messages"], {"role": "user", "content": "injected", "timestamp": 0}]
            }
        },
    )
    responses: list[str] = []

    def afterResponse(message: Any, *_: Any) -> None:
        responses.append(textOf(message) or "")

    addHooks(setup.registry, GenerationTask, {"afterResponse": afterResponse})
    yields = 0

    def onYield(*_: Any) -> Any:
        nonlocal yields
        yields += 1
        return {"continue": [{"type": "text", "text": "keep going"}]} if yields == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": onYield})
    result = await run(setup, [record, record])
    assert result.status == "done"
    assert requests[0][-2:] == ["user:go", "user:injected"]
    assert responses == ["answer 1", "answer 2"]
    assert [entry["kind"] for entry in result.entries] == ["pi.user", "pi.assistant", "pi.user", "pi.assistant"]
    # The injected message was used for the request only.
    assert not any(textOf((entry.get("model") or [None])[0]) == "injected" for entry in result.entries)
    await result.harness.close(context)


async def test_generation_hooks_keep_the_runs_input_open_across_an_on_yield_continuation_and_answer_it_with_the_final_answer():
    setup = chatSetup()
    yields = 0
    holder: dict[str, Any] = {}

    def onYield(*_: Any) -> Any:
        nonlocal yields
        yields += 1
        return {"continue": "again"} if yields == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": onYield})

    async def second(*_: Any) -> Any:
        harness = holder["harness"]
        holder["input"] = (await harness.snapshot(LiveDoc, 1, context))["run"]["inputs"][0]
        holder["statusAtSecondRequest"] = (await (await harness.submission(holder["input"], context)).status(context))["status"]
        return fauxAssistantMessage([fauxText("second")])

    async def prepare(opened: Any, _root: Any) -> None:
        holder["harness"] = opened

    result = await run(setup, [fauxAssistantMessage([fauxText("first")]), second], prepare)
    harness = holder["harness"]
    assert holder["statusAtSecondRequest"] == "placed"
    answers = [entry for entry in result.entries if entry["kind"] == "pi.assistant"]
    toMatchObject(
        await (await harness.submission(holder["input"], context)).status(context),
        {"status": "done", "answer": answers[1]["id"]},
    )
    await harness.close(context)


async def test_generation_hooks_observe_responses_that_arrive_by_polling_a_deferred_request():
    setup = chatSetup({"deferred": {"pendingFetches": 1, "pollAfterMs": 1}})
    observed: list[str] = []

    def afterResponse(message: Any, *_: Any) -> None:
        observed.append(f"{message['stopReason']}:{textOf(message) or ''}")

    addHooks(setup.registry, GenerationTask, {"afterResponse": afterResponse})
    setup.settings["stream"] = {"deferred": True}
    result = await run(setup, [fauxAssistantMessage([fauxText("late")])])
    assert result.status == "done"
    # The still-deferred results are not terminal.
    assert observed == ["stop:late"]
    await result.harness.close(context)


async def test_generation_hooks_let_the_first_on_yield_continuation_win_and_report_throws_without_stopping_later_handlers():
    setup = chatSetup()
    called: list[str] = []

    def throwing(*_: Any) -> None:
        called.append("throwing observer")
        raise RuntimeError("observer failed")

    addHooks(setup.registry, GenerationTask, {"afterResponse": throwing})
    addHooks(setup.registry, GenerationTask, {"afterResponse": lambda *_: called.append("next observer")})
    yields = 0

    def firstYield(*_: Any) -> Any:
        nonlocal yields
        yields += 1
        return {"continue": "first"} if yields == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": firstYield})

    def secondYield(*_: Any) -> Any:
        called.append("second onYield")
        return {"continue": "second"} if len([name for name in called if name == "second onYield"]) == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": secondYield})
    result = await run(
        setup,
        [fauxAssistantMessage([fauxText("a")]), fauxAssistantMessage([fauxText("b")]), fauxAssistantMessage([fauxText("c")])],
    )
    # The first continuation skips the second handler; on the next answer the second handler's continuation wins.
    users = [textOf(entry["model"][0]) for entry in result.entries if entry["kind"] == "pi.user"]
    assert users == ["go", "first", "second"]
    assert len([name for name in called if name == "second onYield"]) == 2
    assert len([name for name in called if name == "next observer"]) == 3
    assert "observer failed" in [str(error) for error in setup.reports]
    await result.harness.close(context)


async def test_generation_hooks_keep_durable_hook_decisions_in_task_memos():
    setup = chatSetup()
    asked = 0

    async def echo(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(setup.registry, tool("echo", echo))

    async def beforeTool(_call: Any, api: Any, *_: Any) -> None:
        nonlocal asked
        asked += 1
        decision = await api.memo("approval:decision", "approved", context)
        assert await api.memo("approval:decision", "denied", context) == decision

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    result = await run(setup, [calls(("echo", {}, "c1")), DONE])
    assert asked == 1
    await result.harness.close(context)


async def test_tool_execution_api_builds_the_environment_per_call_from_the_conversations_cwd_and_runs_commits_memos_and_child_tasks():
    setup = chatSetup()

    async def childRun(task: Any, runtime: Any, callContext: Any) -> None:
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": task["input"]["n"] * 2}},
            callContext,
        )

    async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    child = defineTask(
        {
            "name": "test.child",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": childRun},
            "abort": noAbort,
        }
    )
    addTask(setup.registry, child)
    seen: list[Any] = []

    async def probeRun(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        seen.append(None if api.env is None else api.env.cwd)
        seen.append([each["name"] for each in (await api.agent(context))["tools"]])
        seen.append(api.registry.extension("tool:probe") is not None)

        async def change(tx: Any) -> dict[str, Any]:
            # The next call runs in the new directory: its environment is built when it executes.
            await configure(tx, api.conversationId, {"cwd": "/"})
            return await tx.appendEntry(api.conversationId, {"kind": "test.note", "data": api.callId})  # type: ignore[no-any-return]

        entry = await api.commit(change, context)
        seen.append(entry["byTaskId"] == api.taskId)
        seen.append(await api.memo("m", 1, context))
        seen.append(await api.memo("m", 2, context))
        id = await api.createTask(child, {"n": 21}, {"ownership": {"kind": "conversation"}}, context)
        done = await api.waitForTask(id, context)
        seen.append(done["state"]["outcome"])
        return {"output": []}

    addTool(setup.registry, tool("probe", probeRun))
    setup.faux.setResponses([calls(("probe", {}, "c1"), ("probe", {}, "c2")), DONE])
    setup.settings["toolExecution"] = "sequential"
    targets: list[Any] = []

    def env(target: dict[str, Any], callContext: Any) -> Any:
        targets.append([target["conversationId"], target.get("cwd")])
        return NodeExecutionEnv({"cwd": target.get("cwd") or "/tmp"})

    opened = await openChat(MemoryStorage(), setup, {"env": env})
    harness, root = opened.harness, opened.root
    await root.configure({"cwd": "/tmp"}, context)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    outcome = {"status": "completed", "result": 42}

    def forCall(cwd: str) -> list[Any]:
        return [cwd, ["probe"], True, True, 1, 1, outcome]

    assert seen == [*forCall("/tmp"), *forCall("/")]
    assert [root.id, "/tmp"] in targets
    assert [root.id, "/"] in targets
    await harness.close(context)


async def test_tool_execution_api_answers_a_throwing_environment_with_a_tool_error_result_and_reports_it_once_while_preparing():
    setup = chatSetup()

    async def probeRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "ran"}]}

    addTool(setup.registry, tool("probe", probeRun))

    def env(*_: Any) -> Any:
        raise RuntimeError("no sandbox")

    result = await run(setup, [calls(("probe", {}, "c1")), DONE], None, {"env": env})
    [first] = results(result.entries)
    toMatchObject(first, {"isError": True})
    assert resultText(first) == "<harness>\n[error] no sandbox\n</harness>"
    # Each preparation reports the failure and renders without an environment.
    assert len([error for error in setup.reports if str(error) == "no sandbox"]) == 2
    await result.harness.close(context)


async def test_coding_tools_answer_a_failing_command_with_its_retained_tail_and_diagnostics_in_order():
    directory = tempfile.mkdtemp(prefix="pi-durable-coding-")
    try:
        setup = chatSetup()
        addTool(setup.registry, createBashTool())
        command = "i=1; while [ $i -le 3000 ]; do echo line-$i; i=$((i + 1)); done; exit 7"
        setup.faux.setResponses([calls(("bash", {"command": command}, "b")), DONE])
        env = NodeExecutionEnv({"cwd": directory})
        opened = await openChat(MemoryStorage(), setup, {"env": env})
        harness, root = opened.harness, opened.root
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        entry = next(candidate for candidate in await allEntries(root) if ToolResultEntry.is_(candidate))
        [first] = results([entry])
        assert first["isError"] is True
        text = resultText(first)
        assert text.startswith("line-1001\n")
        assert [diagnostic.get("code") for diagnostic in entry["data"]["diagnostics"]] == [
            "full_output",
            "exit_code",
            "truncated",
        ]
        assert "line-3000\n|<harness>\n[info] Full output: " in text
        assert "\n[error] Command exited with code 7\n[warn] Output truncated to its end: 1000 lines, " in text
        await harness.close(context)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def test_coding_tools_read_edit_and_run_a_command_in_one_run_then_answer():
    directory = tempfile.mkdtemp(prefix="pi-durable-coding-")
    try:
        Path(directory, "notes.txt").write_bytes(b"hello world\n")
        setup = chatSetup()
        setup.registry.install(
            defineExtension({"name": "coding", "tools": [createReadTool(), createEditTool(), createBashTool()]})
        )
        setup.faux.setResponses(
            [
                calls(("read", {"path": "notes.txt"}, "r")),
                calls(("edit", {"path": "notes.txt", "edits": [{"oldText": "world", "newText": "durable"}]}, "e")),
                calls(("bash", {"command": "cat notes.txt"}, "b")),
                DONE,
            ]
        )
        env = NodeExecutionEnv({"cwd": directory})
        opened = await openChat(MemoryStorage(), setup, {"env": env})
        harness, root = opened.harness, opened.root
        settled = await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert settled["status"] == "done"
        entries = await allEntries(root)
        assert [[item["toolName"], item["isError"], resultText(item)] for item in results(entries)] == [
            ["read", False, "hello world\n"],
            ["edit", False, "Successfully replaced 1 block(s) in notes.txt."],
            ["bash", False, "hello durable\n"],
        ]
        assert entries[-1]["kind"] == "pi.assistant"
        assert Path(directory, "notes.txt").read_bytes() == b"hello durable\n"
        await harness.close(context)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def test_tool_progress_applies_the_default_output_limits():
    setup = chatSetup()

    async def lines(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        for index in range(1, 2501):
            api.output(f"{index}\n")
        return {}

    addTool(setup.registry, tool("lines", lines))
    result = await run(setup, [calls(("lines", {}, "c1")), DONE])
    text = resultText(results(result.entries)[0])
    assert text.startswith("1\n2\n")
    assert text.endswith(
        "\n2000\n|<harness>\n[warn] Output truncated to its beginning: 500 lines, 2500 bytes dropped\n</harness>"
    )
    await result.harness.close(context)


async def test_tool_progress_sanitizes_running_output_but_keeps_explicit_result_content_as_the_tool_returned_it():
    setup = chatSetup()
    holder: dict[str, Any] = {}

    async def noisy(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("a\x07b\r\n")
        await api.details({"ready": True}, context)
        snapshot = await api.snapshot(LiveDoc, api.conversationId, context)
        holder["slotOutput"] = None if snapshot is None else (snapshot.get("tools") or [{}])[0].get("output")
        return {}

    async def explicit(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "c\x1bd"}]}

    addTool(setup.registry, tool("noisy", noisy))
    addTool(setup.registry, tool("explicit", explicit))
    result = await run(setup, [calls(("noisy", {}, "c1"), ("explicit", {}, "c2")), DONE])
    assert holder["slotOutput"] == "ab\n"
    byId = {item["toolCallId"]: resultText(item) for item in results(result.entries)}
    assert byId["c1"] == "ab\n"
    assert byId["c2"] == "c\x1bd"
    await result.harness.close(context)


async def test_tool_progress_drops_control_keys_set_to_undefined_instead_of_faulting():
    setup = chatSetup()

    async def grow(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [], "control": {"addTools": ["extra"], "terminate": UNDEFINED}}

    async def extraRun(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(setup.registry, tool("grow", grow))
    extra = tool("extra", extraRun)
    addTool(setup.registry, extra)

    async def prepare(_harness: Any, conversation: Any) -> None:
        await conversation.configure({"tools": {"remove": [extra]}}, context)

    result = await run(setup, [calls(("grow", {}, "c1")), DONE], prepare)
    assert result.status == "done"
    # addTools deletes the name from a stored ``{ remove }`` filter.
    assert (await result.harness.snapshot(AgentDoc, result.root.id, context))["tools"] == {"remove": []}
    await result.harness.close(context)


async def test_tool_progress_uses_explicit_null_details_instead_of_the_last_reported_value():
    setup = chatSetup()

    async def nullDetails(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.details({"old": 1}, context)
        return {"output": [], "details": None}

    addTool(setup.registry, tool("null", nullDetails))
    result = await run(setup, [calls(("null", {}, "c1")), DONE])
    assert results(result.entries)[0]["details"] is None
    await result.harness.close(context)


async def test_tool_progress_settles_details_promises_with_coalesced_progress_commits_and_the_terminal_commit():
    setup = chatSetup()
    settled: list[str] = []

    async def mark(label: str, awaitable: Any) -> None:
        await awaitable
        settled.append(label)

    async def detailsRun(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        # Three updates in one throttle window coalesce; the last is still pending when execute() returns.
        first = asyncio.ensure_future(mark("first", api.details({"n": 1}, context)))
        second = asyncio.ensure_future(mark("second", api.details({"n": 2}, context)))
        await first
        consume(second)
        consume(asyncio.ensure_future(mark("third", api.details({"n": 3}, context))))
        return {"output": []}

    addTool(setup.registry, tool("details", detailsRun))
    result = await run(setup, [calls(("details", {}, "c1")), DONE])
    assert settled == ["first", "second", "third"]
    assert results(result.entries)[0]["details"] == {"n": 3}
    await result.harness.close(context)


async def test_tool_progress_finishes_a_call_under_the_implementation_it_resolved_when_the_tool_is_replaced_mid_call():
    setup = chatSetup()
    started = deferred()
    finished = deferred()

    async def v1Run(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        started.resolve()
        await finished.promise
        return {"output": [{"type": "text", "text": "v1"}]}

    addTool(setup.registry, tool("work", v1Run))
    setup.faux.setResponses([calls(("work", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await started.promise

    async def v2Run(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "v2"}]}

    # The same extension name replaces the old one in place.
    addTool(setup.registry, tool("work", v2Run))
    finished.resolve()
    await submission.wait(context)
    assert resultText(results(await allEntries(root))[0]) == "v1"
    await harness.close(context)


async def test_tool_progress_uses_a_section_and_hook_extension_reloaded_mid_run_from_the_runs_next_request():
    setup = chatSetup()
    requests: list[str] = []

    def prompt(version: str) -> dict[str, Any]:
        return defineExtension(
            {
                "name": "prompt",
                "sections": [section("mode", lambda *_: version)],
                "hooks": [hook(GenerationTask, {"beforeRequest": lambda *_: requests.append(version)})],
            }
        )

    setup.registry.install(prompt("v1"))
    running = deferred()
    reloaded = deferred()

    async def work(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        running.resolve()
        await reloaded.promise
        return {"output": []}

    addTool(setup.registry, tool("work", work))
    setup.faux.setResponses([calls(("work", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await running.promise
    setup.registry.install(prompt("v2"))
    reloaded.resolve()
    await submission.wait(context)
    sections = []
    for entry in await allEntries(root):
        model = entry.get("model")
        message = model[0] if model else None
        if message is not None and message["role"] == "system" and message.get("sections") is not None:
            sections.append(message["sections"])
    assert sections == [{"mode": "<mode>\nv1\n</mode>"}, {"mode": "<mode>\nv2\n</mode>"}]
    assert requests == ["v1", "v2"]
    await harness.close(context)


async def test_tool_progress_rejects_invocation_bound_waits_and_stops_watches_when_the_tools_invocation_ends():
    setup = chatSetup()

    async def nothing(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    never = defineTask(
        {
            "name": "test.never",
            "version": 1,
            "initial": lambda input: {"phase": "never"},
            "phases": {"never": nothing},
            "abort": nothing,
        }
    )
    holder: dict[str, Any] = {}

    async def detach(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        # The child's definition is not registered, so it stays pending.
        child = await api.createTask(never, {}, {"ownership": {"kind": "conversation"}}, context)
        holder["wait"] = api.waitForTask(child, context)
        consume(holder["wait"])
        watch = await api.watchDoc(LiveDoc, api.conversationId, context)
        holder["watchClosed"] = watch.closed
        return {"output": []}

    addTool(setup.registry, tool("detach", detach))
    result = await run(setup, [calls(("detach", {}, "c1")), DONE])
    with pytest.raises(Exception, match="invocation has ended"):
        await holder["wait"]
    await holder["watchClosed"]
    await result.harness.close(context)


async def test_tool_progress_rejects_details_still_waiting_when_the_call_is_aborted_during_after_tool():
    setup = chatSetup()
    holder: dict[str, Any] = {}
    inAfterTool = deferred()

    async def slow(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("first\n")
        # The output commit is in flight, so these details wait for the next throttle window.
        holder["pendingDetails"] = api.details({"step": 1}, context)
        consume(holder["pendingDetails"])
        return {}

    addTool(setup.registry, tool("slow", slow))

    async def afterTool(_call: Any, _result: Any, _api: Any, callContext: Any) -> Any:
        inAfterTool.resolve()
        await aborted(callContext.abortSignal)
        return None

    addHooks(setup.registry, ToolTask, {"afterTool": afterTool})
    setup.faux.setResponses([calls(("slow", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await inAfterTool.promise
    taskId = (await harness.snapshot(LiveDoc, root.id, context))["tools"][0]["taskId"]
    await harness.abortTask(taskId, context)
    with pytest.raises(Exception):  # noqa: B017
        await holder["pendingDetails"]
    await submission.wait(context)
    await harness.close(context)


async def test_tool_progress_answers_an_aborted_tool_with_only_its_durable_output_discarding_buffered_output():
    setup = chatSetup()
    buffered = deferred()

    async def slow(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("durable\n")
        # The first output commits at once; this one waits for the next throttle window.
        await asyncio.sleep(0.02)
        api.output("buffered\n")
        buffered.resolve()
        await aborted(callContext.abortSignal)
        return {}

    addTool(setup.registry, tool("slow", slow))
    setup.faux.setResponses([calls(("slow", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await buffered.promise
    taskId = (await harness.snapshot(LiveDoc, root.id, context))["tools"][0]["taskId"]
    await harness.abortTask(taskId, context)
    await submission.wait(context)
    assert resultText(results(await allEntries(root))[0]) == "durable\n|<harness>\n[error] Tool slow was aborted\n</harness>"
    await harness.close(context)
