"""Translated from pi's ``packages/ai/test/openai-responses-mid-convo-effort.test.ts`` (v1.3.0).

pi answers through a mocked ``fetch``; here the SDK glue's ``_create_with_response`` answers (``tests.openai_fake``)
and records the request payload.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from tests.openai_fake import installFakeOpenAI

thinkingLevelMap = {
    "off": "none",
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def openAIModel(compat: dict[str, Any] | None = None) -> Model:
    return Model.model_validate(
        {
            "id": "gpt-6-luna",
            "name": "GPT-6 Luna",
            "api": "openai-responses",
            "provider": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "reasoning": True,
            "thinkingLevelMap": thinkingLevelMap,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 272000,
            "maxTokens": 128000,
            "compat": {"supportsMidConvoEffort": True} if compat is None else compat,
        }
    )


def user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=0)


def toolResult(callId: str) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=callId, toolName="lookup", content=[TextContent(text="result")], isError=False, timestamp=0
    )


def update(effort: str) -> dict[str, Any]:
    return {"type": "configuration_update", "reasoning": {"effort": effort}}


def responseEvents(responseId: str, withToolCall: bool) -> list[dict[str, Any]]:
    item = (
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": "{}", "status": "completed"}
        if withToolCall
        else {
            "type": "message",
            "id": f"msg_{responseId}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "answer", "annotations": []}],
        }
    )
    return [
        {"type": "response.created", "response": {"id": responseId}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": responseId, "status": "completed"}},
    ]


@pytest.fixture
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch)


async def runOpenAI(
    mockState: Any,
    messages: list[Any],
    reasoningEffort: str | None,
    *,
    apiKey: str | None = None,
    model: Model | None = None,
    withToolCall: bool = False,
) -> tuple[dict[str, Any], AssistantMessage]:
    payloads: list[dict[str, Any]] = []

    def create(_resource: str, params: Any, _requestOptions: Any) -> list[dict[str, Any]]:
        payloads.append(copy.deepcopy(params))
        return responseEvents("resp_1", withToolCall)

    mockState.create = create
    message = await streamOpenAIResponses(
        model if model is not None else openAIModel(),
        normalizeContext(Context(messages=messages)),
        {"apiKey": apiKey if apiKey is not None else "sk-test", "reasoningEffort": reasoningEffort},
    ).result()
    assert payloads, "Request payload was not captured"
    assert message.stopReason != "error", message.errorMessage
    return payloads[0], message


# --- OpenAI Responses configuration_update --------------------------------------------------------


async def test_keeps_reasoning_effort_constant_and_only_appends_input_when_the_level_changes(mockState):
    history: list[Any] = [user("one")]
    first = await runOpenAI(mockState, history, "high")
    history.extend([first[1], user("two")])
    second = await runOpenAI(mockState, history, "high")
    history.extend([second[1], user("three")])
    third = await runOpenAI(mockState, history, "low")

    assert first[0]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "one"}]},
        update("high"),
    ]
    for payload, _message in (first, second, third):
        assert payload["reasoning"]["effort"] == "medium"
    assert second[0]["input"][: len(first[0]["input"])] == first[0]["input"]
    assert third[0]["input"][: len(second[0]["input"])] == second[0]["input"]
    assert [item for item in third[0]["input"] if item.get("type") == "configuration_update"] == [
        update("high"),
        update("high"),
        update("low"),
    ]
    assert third[0]["input"][-1] == update("low")
    assert [message.providerThinkingLevel for _payload, message in (first, second, third)] == ["high", "high", "low"]


async def test_applies_a_level_change_made_during_a_tool_loop_to_the_next_reply(mockState):
    history: list[Any] = [user("one")]
    toolCall = await runOpenAI(mockState, history, "high", withToolCall=True)
    history.extend([toolCall[1], toolResult("call_1|fc_1")])
    afterTool = await runOpenAI(mockState, history, "low")

    assert afterTool[0]["input"][: len(toolCall[0]["input"])] == toolCall[0]["input"]
    toolOutput, last = afterTool[0]["input"][-2:]
    assert (toolOutput["type"], toolOutput["call_id"]) == ("function_call_output", "call_1")
    assert last == update("low")
    assert afterTool[1].providerThinkingLevel == "low"


async def test_adds_items_only_before_replies_recorded_by_this_model(mockState):
    unstamped = (await runOpenAI(mockState, [user("one")], "high", model=openAIModel({})))[1]
    otherProvider = unstamped.model_copy(update={"provider": "opencode", "providerThinkingLevel": "high"})
    otherModel = unstamped.model_copy(update={"model": "gpt-6-sol", "providerThinkingLevel": "high"})
    recorded = unstamped.model_copy(update={"providerThinkingLevel": "high"})

    payload, _message = await runOpenAI(
        mockState,
        [
            user("one"),
            unstamped,
            user("two"),
            otherProvider,
            user("three"),
            otherModel,
            user("four"),
            recorded,
            user("five"),
        ],
        "low",
    )

    assert [item.get("type") or item.get("role") for item in payload["input"]] == [
        "user",
        "message",
        "user",
        "message",
        "user",
        "message",
        "user",
        "configuration_update",
        "message",
        "user",
        "configuration_update",
    ]
    assert [item for item in payload["input"] if item.get("type") == "configuration_update"] == [
        update("high"),
        update("low"),
    ]


@pytest.mark.parametrize(
    ("apiKey", "model", "level", "effort"),
    [
        pytest.param("chatgpt-access-token", openAIModel(), "low", "low", id="Sign in with ChatGPT"),
        pytest.param("sk-test", openAIModel({"supportsMidConvoEffort": False}), "low", "low", id="the compat flag turned off"),
        pytest.param("sk-test", openAIModel(), None, "none", id="thinking off"),
    ],
)
async def test_sends_the_level_as_reasoning_effort(mockState, apiKey, model, level, effort):
    first = await runOpenAI(mockState, [user("one")], "high", apiKey=apiKey, model=model)
    payload, message = await runOpenAI(mockState, [user("one"), first[1], user("two")], level, apiKey=apiKey, model=model)

    assert payload["reasoning"]["effort"] == effort
    assert not any(item.get("type") == "configuration_update" for item in payload["input"])
    assert message.providerThinkingLevel is None
