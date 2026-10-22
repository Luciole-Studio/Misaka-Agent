"""pi durable's ``test/harness-structured-output.test.ts``."""

from __future__ import annotations

import json
import shutil
import tempfile
from types import SimpleNamespace
from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    MemoryStorage,
    NestedCallDoc,
    ToolResultEntry,
    ToolTask,
    defineTool,
    watchEvents,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import createBashTool
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context, toMatchObject
from tests.harness_support import addHooks, addTool

Count = {"type": "object", "properties": {"count": {"type": "number"}}, "required": ["count"]}
EMPTY = {"type": "object", "properties": {}}


def returning(name: str, result: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A tool that returns ``result``, with ``extra`` such as a schema."""

    async def execute(*_: Any) -> dict[str, Any]:
        return result

    return defineTool({"name": name, "description": name, "parameters": EMPTY, "execute": execute, **(extra or {})})


def call(name: str) -> Any:
    return fauxAssistantMessage([fauxToolCall(name, {}, {"id": "c1"})], {"stopReason": "toolUse"})


DONE = fauxAssistantMessage([fauxText("done")])


def probeTool(execute: Any) -> dict[str, Any]:
    return defineTool({"name": "probe", "description": "probe", "parameters": EMPTY, "execute": execute})


async def nested(setup: SimpleNamespace, name: str) -> dict[str, Any]:
    """Run ``name`` as a nested call of a ``probe`` tool and return what the probe received."""
    received: dict[str, Any] = {}

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        received["result"] = await api.executeTool(name, {}, callContext)
        return {}

    addTool(setup.registry, probeTool(execute))
    setup.faux.setResponses([call("probe"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await opened.harness.close(context)
    return received["result"]  # type: ignore[no-any-return]


async def direct(setup: SimpleNamespace, name: str) -> dict[str, Any]:
    """Run ``name`` as a model-issued call and return its transcript message."""
    setup.faux.setResponses([call(name), DONE])
    opened = await openChat(MemoryStorage(), setup)
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entry = next(candidate for candidate in await allEntries(opened.root) if ToolResultEntry.is_(candidate))
    await opened.harness.close(context)
    return entry["model"][0]  # type: ignore[no-any-return]


def codes(result: dict[str, Any]) -> list[str | None]:
    return [diagnostic.get("code") for diagnostic in result.get("diagnostics") or []]


async def test_gives_callers_a_schema_less_tools_output_one_text_as_a_string_one_image_as_itself_else_the_list():
    text = {"type": "text", "text": "one"}
    image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    cases: list[tuple[list[dict[str, Any]], Any]] = [
        ([text], "one"),
        ([image], image),
        ([], ""),
        ([text, image], [text, image]),
        ([text, text], [text, text]),
    ]
    for output, value in cases:
        setup = chatSetup()
        addTool(setup.registry, returning("plain", {"output": output}))
        received = await nested(setup, "plain")
        assert received == {
            "taskId": received["taskId"],
            "structuredOutput": value,
            "isError": False,
            "diagnostics": [],
            "durationMs": received["durationMs"],
        }


async def test_gives_callers_the_bounded_output_as_the_model_sees_it_without_the_rendered_diagnostics():
    setup = chatSetup()
    addTool(
        setup.registry,
        returning(
            "long",
            {"output": [{"type": "text", "text": "a\nb\nc"}], "diagnostics": [{"severity": "info", "message": "note"}]},
            {"outputLimits": {"maxLines": 1}},
        ),
    )
    received = await nested(setup, "long")
    assert received["structuredOutput"] == "a\n"
    assert codes(received) == [None, "truncated"]


async def test_stores_exactly_what_callers_and_events_get_without_the_models_output_and_keeps_a_stored_null():
    setup = chatSetup()
    addTool(setup.registry, returning("plain", {"output": [{"type": "text", "text": "a"}]}))
    addTool(setup.registry, returning("nothing", {"structuredOutput": None}, {"structuredOutputSchema": {"type": "null"}}))
    received: list[dict[str, Any]] = []

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        received.append(await api.executeTool("plain", {}, callContext))
        received.append(await api.executeTool("nothing", {}, callContext))
        return {}

    addTool(setup.registry, probeTool(execute))
    setup.faux.setResponses([call("probe"), DONE])
    storage = MemoryStorage()
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    # The result each nested call's document got, as committed.
    stored: list[Any] = []

    def onCommit(publication: Any, *_: Any) -> None:
        for change in publication["changes"]:
            if change["type"] != "document" or change["record"]["kind"] != NestedCallDoc["definition"]["kind"]:
                continue
            value = change["value"]
            result = None if value is None else value.get("result")
            if result is not None and len(change["ops"]) > 0:
                stored.append(result)

    harness.subscribeCommits(onCommit)
    stream = await watchEvents(harness, root.id, context)
    ends: list[Any] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        for event in batch:
            if event["type"] == "tool_execution_end" and event.get("parentToolCallId") is not None:
                ends.append(event.get("result"))

    stream.start(onBatch)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await harness.waitForIdle(context)
    await stream.stop()
    await harness.close(context)

    toMatchObject(received[0], {"structuredOutput": "a", "isError": False})
    toMatchObject(received[1], {"structuredOutput": None, "isError": False})
    assert "output" not in received[0]
    assert stored == received
    assert ends == received


async def test_gives_callers_a_schema_tools_validated_structured_output_which_the_transcript_does_not_store():
    setup = chatSetup()
    result = {"output": [{"type": "text", "text": "3 things"}], "structuredOutput": {"count": 3}}
    addTool(setup.registry, returning("counter", result, {"structuredOutputSchema": Count}))
    toMatchObject(await nested(setup, "counter"), {"structuredOutput": {"count": 3}, "isError": False})

    modelSetup = chatSetup()
    addTool(modelSetup.registry, returning("counter", result, {"structuredOutputSchema": Count}))
    stored = await direct(modelSetup, "counter")
    toMatchObject(stored, {"isError": False, "content": [{"type": "text", "text": "3 things"}]})
    assert "structuredOutput" not in stored


async def test_turns_structured_output_that_breaks_the_schema_into_an_error_result_for_callers_and_a_report_for_the_models_calls():
    cases: list[tuple[str, dict[str, Any], bool, str]] = [
        ("wrong", {"structuredOutput": {"count": "three"}}, True, "does not match its schema: count:"),
        ("missing", {"output": []}, True, "returned no structuredOutput"),
        ("undeclared", {"structuredOutput": {"count": 3}}, False, "declares no structuredOutputSchema"),
    ]
    for name, result, schema, message in cases:
        setup = chatSetup()
        addTool(setup.registry, returning(name, result, {"structuredOutputSchema": Count} if schema else {}))
        received = await nested(setup, name)
        assert received["isError"] is True
        # The broken value is dropped; a schema-less tool's error result still carries its output, here none.
        if schema:
            assert "structuredOutput" not in received
        else:
            assert received["structuredOutput"] == ""
        assert codes(received) == ["invalid_structured_output"]
        assert message in received["diagnostics"][0]["message"]

        modelSetup = chatSetup()
        addTool(modelSetup.registry, returning(name, result, {"structuredOutputSchema": Count} if schema else {}))
        # The model never sees structured output: its call stands, and the host gets a report.
        stored = await direct(modelSetup, name)
        assert stored["isError"] is False
        assert message not in json.dumps(stored["content"])
        assert message in "\n".join(str(error) for error in modelSetup.reports)


async def test_gives_callers_the_output_of_an_error_result_of_a_tool_without_a_schema():
    setup = chatSetup()
    addTool(setup.registry, returning("bad", {"output": [{"type": "text", "text": "bad"}], "isError": True}))
    toMatchObject(await nested(setup, "bad"), {"isError": True, "structuredOutput": "bad"})


async def test_gives_callers_no_structured_output_for_an_error_result_the_harness_wrote_only_diagnostics():
    setup = chatSetup()
    received = await nested(setup, "missing")
    toMatchObject(received, {"isError": True, "diagnostics": [{"code": "tool_unavailable"}]})
    assert "structuredOutput" not in received


async def test_gives_callers_a_schema_tools_error_text_as_a_diagnostic_never_as_structured_output():
    setup = chatSetup()
    addTool(
        setup.registry,
        returning(
            "fails", {"output": [{"type": "text", "text": "nope"}], "isError": True}, {"structuredOutputSchema": Count}
        ),
    )
    # Its text reaches the caller as an error diagnostic: structuredOutput is only ever the tool's own value.
    received = await nested(setup, "fails")
    toMatchObject(
        received, {"isError": True, "diagnostics": [{"severity": "error", "code": "tool_error", "message": "nope"}]}
    )
    assert "structuredOutput" not in received

    # The model's call keeps the text in its output, without the extra diagnostic.
    modelSetup = chatSetup()
    addTool(
        modelSetup.registry,
        returning(
            "fails", {"output": [{"type": "text", "text": "nope"}], "isError": True}, {"structuredOutputSchema": Count}
        ),
    )
    stored = await direct(modelSetup, "fails")
    assert stored["content"] == [{"type": "text", "text": "nope"}]

    # A thrown error's message is in its tool_error diagnostic, not in the output.
    thrown = chatSetup()

    async def throws(*_: Any) -> Any:
        raise RuntimeError("permission denied")

    addTool(
        thrown.registry,
        defineTool(
            {
                "name": "throws",
                "description": "throws",
                "parameters": EMPTY,
                "structuredOutputSchema": Count,
                "execute": throws,
            }
        ),
    )
    thrownResult = await nested(thrown, "throws")
    # One diagnostic: the thrown message, not a second one from its (empty) output.
    toMatchObject(thrownResult, {"isError": True, "diagnostics": [{"code": "tool_error", "message": "permission denied"}]})
    assert len(thrownResult["diagnostics"]) == 1
    assert "structuredOutput" not in thrownResult

    # An error result whose own diagnostics say what went wrong gets no second one from its output.
    explained = chatSetup()
    addTool(
        explained.registry,
        returning(
            "fails",
            {
                "output": [{"type": "text", "text": "partial log"}],
                "isError": True,
                "diagnostics": [{"severity": "error", "code": "quota", "message": "quota exceeded"}],
            },
            {"structuredOutputSchema": Count},
        ),
    )
    assert (await nested(explained, "fails"))["diagnostics"] == [
        {"severity": "error", "code": "quota", "message": "quota exceeded"}
    ]

    # An error result that has structured output keeps it.
    kept = chatSetup()
    withValue = {"output": [{"type": "text", "text": "nope"}], "structuredOutput": {"count": 0}, "isError": True}
    addTool(kept.registry, returning("fails", withValue, {"structuredOutputSchema": Count}))
    toMatchObject(await nested(kept, "fails"), {"isError": True, "structuredOutput": {"count": 0}})


async def test_keeps_structured_output_when_aftertool_replaces_the_output_and_validates_one_aftertool_replaces():
    setup = chatSetup()
    addTool(setup.registry, returning("plain", {"output": [{"type": "text", "text": "secret"}]}))
    addTool(setup.registry, returning("counter", {"structuredOutput": {"count": 1}}, {"structuredOutputSchema": Count}))
    addTool(setup.registry, returning("other", {"structuredOutput": {"count": 1}}, {"structuredOutputSchema": Count}))

    def afterTool(hookCall: Any, result: Any, *_: Any) -> Any:
        if hookCall["name"] == "other":
            return {**result, "structuredOutput": {"count": "broken"}}
        if hookCall["name"] == "probe":
            return result
        return {**result, "output": [{"type": "text", "text": "rewritten"}]}

    addHooks(setup.registry, ToolTask, {"afterTool": afterTool})
    holder: dict[str, Any] = {}

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["plain"] = await api.executeTool("plain", {}, callContext)
        holder["counter"] = await api.executeTool("counter", {}, callContext)
        holder["other"] = await api.executeTool("other", {}, callContext)
        return {}

    addTool(setup.registry, probeTool(execute))
    setup.faux.setResponses([call("probe"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await opened.harness.close(context)
    # A schema-less tool's value is its output, so redacting the output redacts what programs get.
    assert holder["plain"]["structuredOutput"] == "rewritten"
    toMatchObject(holder["counter"], {"structuredOutput": {"count": 1}})
    assert holder["other"]["isError"] is True
    assert codes(holder["other"]) == ["invalid_structured_output"]


async def test_gives_callers_of_bash_its_retained_output_and_exit_code_also_for_a_nonzero_exit():
    directory = tempfile.mkdtemp(prefix="pi-durable-structured-")
    try:
        setup = chatSetup()
        addTool(setup.registry, createBashTool())
        received: list[dict[str, Any]] = []

        async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
            received.append(await api.executeTool("bash", {"command": "printf ok"}, callContext))
            received.append(await api.executeTool("bash", {"command": "printf bad; exit 4"}, callContext))
            return {}

        addTool(setup.registry, probeTool(execute))
        setup.faux.setResponses([call("probe"), DONE])
        env = NodeExecutionEnv({"cwd": directory})
        opened = await openChat(MemoryStorage(), setup, {"env": env})
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        await opened.harness.close(context)
        toMatchObject(
            received[0], {"isError": False, "structuredOutput": {"output": "ok", "truncated": False, "exitCode": 0}}
        )
        toMatchObject(
            received[1], {"isError": True, "structuredOutput": {"output": "bad", "truncated": False, "exitCode": 4}}
        )
        assert codes(received[1]) == ["exit_code"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)
