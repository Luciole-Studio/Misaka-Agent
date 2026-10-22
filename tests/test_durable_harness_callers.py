"""pi durable's ``test/harness-callers.test.ts``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import AgentDoc, MemoryStorage, defineTool
from tests.chat_support import chatSetup, openChat
from tests.durable_support import context, toMatchObject
from tests.harness_support import addTool


def tool(name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    async def execute(*_: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": f"{name} ran"}]}

    return defineTool(
        {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
            "execute": execute,
            **(extra or {}),
        }
    )


DONE = fauxAssistantMessage([fauxText("done")])


def call(name: str, id: str = "c1") -> Any:
    return fauxAssistantMessage([fauxToolCall(name, {}, {"id": id})], {"stopReason": "toolUse"})


def prober(names: list[str]) -> SimpleNamespace:
    """A tool that calls each of ``names`` as a nested call and records what it got back."""
    received: dict[str, dict[str, Any]] = {}

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        for name in names:
            received[name] = await api.executeTool(name, {}, callContext)
        return {}

    registration = defineTool(
        {"name": "probe", "description": "probe", "parameters": {"type": "object", "properties": {}}, "execute": execute}
    )
    return SimpleNamespace(registration=registration, received=received)


async def offered(root: Any) -> list[str]:
    return [each["name"] for each in (await root.agent(context))["tools"]]


async def callable_(root: Any) -> list[str]:
    return [each["name"] for each in (await root.agent(context))["callable"]]


async def setupWith(tools: list[dict[str, Any]]) -> SimpleNamespace:
    setup = chatSetup()
    for each in tools:
        addTool(setup.registry, each)
    return setup


async def test_offers_the_model_only_tools_it_may_call_and_lets_tools_call_only_tools_they_may_call():
    script = tool("script", {"callers": ["model"]})
    hidden = tool("hidden", {"callers": ["tools"]})
    both = tool("both")
    probe = prober(["script", "hidden", "both"])
    setup = await setupWith([script, hidden, both, probe.registration])
    setup.faux.setResponses([call("probe"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    assert await offered(root) == ["script", "both", "probe"]
    assert await callable_(root) == ["hidden", "both", "probe"]
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert probe.received["script"]["diagnostics"][0]["code"] == "tool_unavailable"
    toMatchObject(probe.received["hidden"], {"isError": False, "structuredOutput": "hidden ran"})
    toMatchObject(probe.received["both"], {"isError": False, "structuredOutput": "both ran"})
    await harness.close(context)


async def test_narrows_what_the_model_is_offered_with_modeltools_leaving_the_rest_callable_by_tools():
    read = tool("read")
    bash = tool("bash")
    probe = prober(["read", "bash"])
    setup = await setupWith([read, bash, probe.registration])
    setup.faux.setResponses([call("probe"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.configure({"modelTools": [probe.registration]}, context)
    assert await offered(root) == ["probe"]
    assert await callable_(root) == ["read", "bash", "probe"]
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert probe.received["read"]["isError"] is False
    assert probe.received["bash"]["isError"] is False

    await root.configure({"modelTools": {"remove": [bash]}}, context)
    assert await offered(root) == ["read", "probe"]
    assert (await harness.snapshot(AgentDoc, root.id, context))["modelTools"] == {"remove": ["bash"]}
    await root.configure({"modelTools": None}, context)
    assert await offered(root) == ["read", "bash", "probe"]
    await harness.close(context)


async def test_never_offers_a_tool_that_is_not_enabled_nor_lets_tools_call_it():
    read = tool("read")
    bash = tool("bash")
    probe = prober(["bash"])
    setup = await setupWith([read, bash, probe.registration])
    setup.faux.setResponses([call("probe"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    # modelTools cannot bring back what tools disabled.
    await root.configure({"tools": {"remove": [bash]}, "modelTools": [bash, probe.registration]}, context)
    assert await offered(root) == ["probe"]
    assert await callable_(root) == ["read", "probe"]
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert probe.received["bash"]["diagnostics"][0]["code"] == "tool_unavailable"
    await harness.close(context)


async def test_answers_a_model_issued_call_of_a_tool_it_was_not_offered_as_unavailable():
    read = tool("read")
    bash = tool("bash")
    setup = await setupWith([read, bash])
    setup.faux.setResponses([call("bash"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.configure({"modelTools": [read]}, context)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entries = await root.entries({}, 100, None, context)
    result = next((entry for entry in entries["items"] if entry["kind"] == "pi.tool-result"), None)
    assert "Tool bash is not available" in json.dumps(None if result is None else result.get("model"))
    await harness.close(context)


async def test_applies_addtools_to_modeltools_too_so_the_model_is_offered_the_added_tools():
    extra = tool("extra")

    async def growExecute(*_: Any) -> dict[str, Any]:
        return {"output": [], "control": {"addTools": ["extra"]}}

    grow = tool("grow", {"execute": growExecute})
    setup = await setupWith([extra, grow])
    setup.faux.setResponses([call("grow"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.configure({"modelTools": [grow]}, context)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert (await harness.snapshot(AgentDoc, root.id, context))["modelTools"] == ["grow", "extra"]
    assert await offered(root) == ["grow", "extra"]
    await harness.close(context)
