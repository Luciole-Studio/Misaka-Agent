"""Translated from pi's ``packages/ai/test/transcript-tool-changes.test.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.compat import getModel, streamSimple
from misaka.ai.types import (
    Context,
    Model,
    ModelCost,
    SimpleStreamOptions,
    SystemMessage,
    Tool,
    ToolReference,
    UserMessage,
)


class PayloadCaptured(Exception):
    pass


def tool(name: str) -> Tool:
    return Tool(name=name, description=f"{name} tool", parameters={"type": "object", "properties": {}})


async def capturePayload(model: Model, context: Context) -> Any:
    captured: Any = None

    def onPayload(payload: Any, _model: Any) -> Any:
        nonlocal captured
        captured = payload
        raise PayloadCaptured()

    stream = streamSimple(model, context, SimpleStreamOptions(apiKey="test-key", onPayload=onPayload))
    await stream.result()
    if not captured:
        raise RuntimeError("Expected payload capture")
    return captured


modelBase: dict[str, Any] = {
    "baseUrl": "http://127.0.0.1:9",
    "reasoning": True,
    "input": ["text"],
    "cost": ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
    "contextWindow": 100000,
    "maxTokens": 1000,
}

baseTool = tool("base_tool")
lateTool = tool("late_tool")
context = Context(
    messages=[
        SystemMessage(
            content="base prompt",
            sections={"rules": "<rules>\nold rules\n</rules>", "docs": "<docs>\nread docs\n</docs>"},
            toolsAdded=[baseTool],
            timestamp=0,
        ),
        UserMessage(content="before", timestamp=1),
        SystemMessage(
            content="updated guidance",
            sections={"rules": "<rules>\nnew rules\n</rules>", "docs": None},
            toolsRemoved=[ToolReference(name="base_tool")],
            toolsAdded=[lateTool],
            timestamp=2,
        ),
    ]
)

additionContext = Context(
    messages=[
        SystemMessage(content="base prompt", toolsAdded=[baseTool], timestamp=0),
        UserMessage(content="before", timestamp=1),
        SystemMessage(content="updated guidance", toolsAdded=[lateTool], timestamp=2),
    ]
)

anthropicNativeModel = Model(
    **modelBase,
    id="claude-opus-5",
    name="Claude Opus 5",
    api="anthropic-messages",
    provider="anthropic",
    compat={"supportsMidConvoSystemMessages": True, "supportsMidConvoToolChanges": True},
)


async def test_sends_anthropic_updates_and_tool_changes_in_native_system_messages():
    payload = await capturePayload(anthropicNativeModel, context)

    assert "inline-tools-2026-09-15" in payload["betas"]
    assert "mid-conversation-tool-changes-2026-07-01" not in payload["betas"]
    assert [block["text"] for block in payload["system"]] == [
        "base prompt\n\n<rules>\nold rules\n</rules>\n\n<docs>\nread docs\n</docs>"
    ]
    # The top-level list holds only the initial tools (with the cache breakpoint) and the
    # deferred placeholder; later tools never appear there.
    assert len(payload["tools"]) == 2
    assert payload["tools"][0]["name"] == "base_tool"
    assert payload["tools"][0]["cache_control"]["type"] == "ephemeral"
    assert payload["tools"][1]["name"] == "__pi_deferred_placeholder__"
    assert payload["tools"][1]["defer_loading"] is True
    assert "defer_loading" not in payload["tools"][0]
    assert "cache_control" not in payload["tools"][1]
    update = payload["messages"][-1]
    assert update["role"] == "system"
    assert [block["type"] for block in update["content"]] == ["text", "tool_removal", "tool_addition"]
    assert update["content"][1]["tool"] == {"type": "tool_reference", "name": "base_tool"}
    addition = update["content"][2]
    assert addition["tool"]["type"] == "tool_definition"
    assert addition["tool"]["definition"]["name"] == "late_tool"
    assert addition["tool"]["definition"]["description"] == "late_tool tool"
    assert addition["cache_control"]["type"] == "ephemeral"
    # Cache control goes on the block, never on the inline definition; nothing is deferred.
    definition = addition["tool"]["definition"]
    assert "cache_control" not in definition
    assert "defer_loading" not in definition
    assert "updated guidance" in update["content"][0]["text"]
    assert "<rules>\nnew rules\n</rules>" in update["content"][0]["text"]
    assert 'Removed system prompt section "docs"' in update["content"][0]["text"]

    # The placeholder is declared before any change so its scaffolding is cached from request one.
    initial = await capturePayload(anthropicNativeModel, Context(messages=context.messages[:2]))
    assert [value["name"] for value in initial["tools"]] == ["base_tool", "__pi_deferred_placeholder__"]


async def test_redefines_an_anthropic_tool_by_value_under_the_same_name():
    redefinedTool = baseTool.model_copy(update={"description": "changed"})
    payload = await capturePayload(
        anthropicNativeModel,
        Context(
            messages=[
                SystemMessage(content="base prompt", toolsAdded=[baseTool], timestamp=0),
                UserMessage(content="before", timestamp=1),
                SystemMessage(
                    content="",
                    toolsRemoved=[ToolReference(name="base_tool")],
                    toolsAdded=[redefinedTool],
                    timestamp=2,
                ),
            ]
        ),
    )

    assert "inline-tools-2026-09-15" in payload["betas"]
    # The top-level declaration keeps the original definition, so the cached prefix survives.
    assert [(value["name"], value.get("description")) for value in payload["tools"]] == [
        ("base_tool", "base_tool tool"),
        ("__pi_deferred_placeholder__", "Reserved placeholder. Never available. Never call this."),
    ]
    # The new definition replaces the old one in place; no removal block is sent for it.
    last = payload["messages"][-1]
    assert last["role"] == "system"
    assert len(last["content"]) == 1
    assert last["content"][0]["type"] == "tool_addition"
    assert last["content"][0]["tool"]["type"] == "tool_definition"
    assert last["content"][0]["tool"]["definition"]["name"] == "base_tool"
    assert last["content"][0]["tool"]["definition"]["description"] == "changed"


async def test_sends_the_current_anthropic_tool_list_without_an_initial_tool():
    # Anthropic rejects a tool list where every tool (here: the placeholder) is deferred.
    payload = await capturePayload(
        anthropicNativeModel,
        Context(
            messages=[
                SystemMessage(content="base prompt", timestamp=0),
                UserMessage(content="before", timestamp=1),
                SystemMessage(content="updated guidance", toolsAdded=[lateTool], timestamp=2),
            ]
        ),
    )
    assert "inline-tools-2026-09-15" not in payload.get("betas", [])
    assert [value["name"] for value in payload["tools"]] == ["late_tool"]
    assert payload["tools"][0]["cache_control"]["type"] == "ephemeral"
    assert "defer_loading" not in payload["tools"][0]
    assert [block["type"] for block in payload["messages"][-1]["content"]] == ["text"]


async def test_folds_anthropic_updates_into_the_system_prompt_without_native_support():
    model = Model(
        **modelBase,
        id="claude-sonnet-4-5",
        name="Claude Sonnet 4.5",
        api="anthropic-messages",
        provider="anthropic",
    )
    payload = await capturePayload(model, context)

    assert "inline-tools-2026-09-15" not in payload.get("betas", [])
    assert [block["text"] for block in payload["system"]] == ["base prompt\n\nupdated guidance\n\n<rules>\nnew rules\n</rules>"]
    assert [value["name"] for value in payload["tools"]] == ["late_tool"]
    assert [message["role"] for message in payload["messages"]] == ["user"]


async def test_requires_both_anthropic_capabilities_for_native_tool_changes():
    model = Model(
        **modelBase,
        id="claude-opus-5",
        name="Claude Opus 5",
        api="anthropic-messages",
        provider="anthropic",
        compat={"supportsMidConvoToolChanges": True},
    )
    payload = await capturePayload(model, context)

    assert "inline-tools-2026-09-15" not in payload.get("betas", [])
    assert [value["name"] for value in payload["tools"]] == ["late_tool"]
    assert [message["role"] for message in payload["messages"]] == ["user"]


async def test_anchors_openai_additions_at_their_developer_message():
    model = Model(
        **modelBase,
        id="gpt-5.4",
        name="GPT-5.4",
        api="openai-responses",
        provider="openai",
        compat={"supportsMidConvoSystemMessages": True, "supportsAdditionalTools": True},
    )
    payload = await capturePayload(model, additionContext)

    assert [value["name"] for value in payload["tools"]] == ["base_tool"]
    additional = next(item for item in payload["input"] if item.get("type") == "additional_tools")
    assert [value["name"] for value in additional["tools"]] == ["late_tool"]
    assert [
        item["content"] for item in payload["input"] if item.get("role") == "developer" and item.get("type") is None
    ] == ["base prompt", "updated guidance"]


async def test_maps_system_message_additions_into_synthetic_tool_search():
    model = Model(
        **modelBase,
        id="gpt-5.4",
        name="GPT-5.4",
        api="openai-responses",
        provider="openai",
        compat={"supportsMidConvoSystemMessages": True, "supportsToolSearch": True},
    )
    payload = await capturePayload(model, additionContext)

    assert [value["name"] for value in payload["tools"]] == ["base_tool"]
    assert "tool_search_call" in [item.get("type") for item in payload["input"]]
    output = next(item for item in payload["input"] if item.get("type") == "tool_search_output")
    assert [value["name"] for value in output["tools"]] == ["late_tool"]


async def test_folds_openai_updates_into_the_leading_developer_message_without_native_support():
    model = Model(
        **modelBase,
        id="gpt-4.1",
        name="GPT-4.1",
        api="openai-responses",
        provider="openai",
        compat={"supportsAdditionalTools": True},
    )
    payload = await capturePayload(model, context)

    assert [value["name"] for value in payload["tools"]] == ["late_tool"]
    assert [item.get("type") or item.get("role") for item in payload["input"]] == ["developer", "user"]
    assert payload["input"][0]["content"] == "base prompt\n\nupdated guidance\n\n<rules>\nnew rules\n</rules>"


async def test_falls_back_to_the_complete_current_tool_state_when_removals_are_unsupported():
    model = Model(
        **modelBase,
        id="gpt-5.4",
        name="GPT-5.4",
        api="openai-responses",
        provider="openai",
        compat={"supportsMidConvoSystemMessages": True, "supportsAdditionalTools": True},
    )
    payload = await capturePayload(model, context)

    assert [value["name"] for value in payload["tools"]] == ["late_tool"]
    assert not any(item.get("type") == "additional_tools" for item in payload["input"])
    assert len([item for item in payload["input"] if item.get("role") == "developer"]) == 2


async def test_anchors_kimi_additions_in_tool_bearing_system_messages():
    model = Model(
        **modelBase,
        id="kimi-k3",
        name="Kimi K3",
        api="openai-completions",
        provider="moonshotai",
        compat={"supportsMidConvoSystemMessages": True, "supportsMidConvoToolAdditions": True},
    )
    payload = await capturePayload(model, additionContext)

    assert [value["function"]["name"] for value in payload["tools"]] == ["base_tool"]
    toolMessage = next(message for message in payload["messages"] if message.get("tools"))
    assert [value["function"]["name"] for value in toolMessage["tools"]] == ["late_tool"]
    assert [message.get("content") for message in payload["messages"] if message["role"] == "system"] == [
        "base prompt",
        None,
        "updated guidance",
    ]


# #10720: Kimi Coding uses Chat Completions so K3-style models keep added tools out of the top-level list.
@pytest.mark.parametrize("modelId", ["k3", "k3-256k", "kimi-for-coding"])
async def test_anchors_additions_for_kimi_coding(modelId):
    payload = await capturePayload(getModel("kimi-coding", modelId), additionContext)

    assert [value["function"]["name"] for value in payload["tools"]] == ["base_tool"]
    toolMessage = next(message for message in payload["messages"] if message.get("tools"))
    assert [value["function"]["name"] for value in toolMessage["tools"]] == ["late_tool"]


# #10720: Kimi Code Highspeed rejects tool-bearing system messages.
async def test_keeps_kimi_coding_highspeed_additions_in_top_level_tools():
    payload = await capturePayload(getModel("kimi-coding", "kimi-for-coding-highspeed"), additionContext)

    assert [value["function"]["name"] for value in payload["tools"]] == ["base_tool", "late_tool"]
    assert not any("tools" in message for message in payload["messages"])


async def test_keeps_kimi_k2_system_text_inline_without_dynamic_tool_messages():
    model = Model(
        **modelBase,
        id="kimi-k2.7-code",
        name="Kimi K2.7 Code",
        api="openai-completions",
        provider="moonshotai",
        compat={"supportsMidConvoSystemMessages": True},
    )
    payload = await capturePayload(model, additionContext)

    assert [value["function"]["name"] for value in payload["tools"]] == ["base_tool", "late_tool"]
    assert not any("tools" in message for message in payload["messages"])
    assert [message.get("content") for message in payload["messages"] if message["role"] == "system"] == [
        "base prompt",
        "updated guidance",
    ]


async def test_folds_openai_compatible_updates_into_the_system_prompt_without_native_support():
    model = Model(
        **{**modelBase, "reasoning": False},
        id="custom-model",
        name="Custom model",
        api="openai-completions",
        provider="custom-provider",
    )
    payload = await capturePayload(model, context)

    assert [value["function"]["name"] for value in payload["tools"]] == ["late_tool"]
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["content"] == "base prompt\n\nupdated guidance\n\n<rules>\nnew rules\n</rules>"
