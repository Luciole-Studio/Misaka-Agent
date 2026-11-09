"""Translated from pi's ``packages/ai/test/azure-openai-{base-url,responses-reasoning-replay,tool-choice}.test.ts``
(v1.1.0).

pi mocks the ``openai`` module's ``AzureOpenAI`` class; here the SDK glue's ``_sdk_client`` records the constructor
options and ``_create_with_response`` answers the request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from misaka.ai.api import _openai_sdk
from misaka.ai.api.azure_openai_responses import stream as streamAzureOpenAIResponses
from misaka.ai.api.azure_openai_responses import streamSimple
from misaka.ai.api.openai_responses_shared import (
    convertResponsesMessages,
    processResponsesStream,
)
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    Tool,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext


@dataclass
class _AzureMock:
    constructorCalls: list[dict[str, Any]] = field(default_factory=list)
    lastParams: dict[str, Any] | None = None
    streamEvents: list[dict[str, Any]] | None = None


class _FakeAzureOpenAI:
    async def close(self) -> None:
        return None


@pytest.fixture
def azureMock(monkeypatch) -> _AzureMock:
    mock = _AzureMock()

    def sdkClient(**config: Any) -> tuple[Any, dict[str, Any]]:
        mock.constructorCalls.append(config)
        return _FakeAzureOpenAI(), {}

    async def createWithResponse(_client: Any, resource: str, params: Any, *_args: Any) -> _openai_sdk._WithResponse:
        assert resource == "responses"
        mock.lastParams = params
        events = mock.streamEvents
        if not events:
            raise RuntimeError("mock create")

        async def data():
            for event in events:
                yield event

        return _openai_sdk._WithResponse(data(), httpx.Response(200))

    monkeypatch.setattr(_openai_sdk, "_sdk_client", sdkClient)
    monkeypatch.setattr(_openai_sdk, "_create_with_response", createWithResponse)
    for name in ("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_RESOURCE_NAME", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return mock


PI_USER_AGENT = getPiUserAgent()

context = Context(messages=[UserMessage(content="hello", timestamp=time.time_ns() // 1_000_000)])


async def captureClientBaseUrl(azureMock: _AzureMock, monkeypatch, baseUrl: str) -> str:
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", baseUrl)
    model = getModel("azure", "gpt-4o-mini")
    await streamAzureOpenAIResponses(model, normalizeContext(context), {"apiKey": "test-api-key"}).result()
    assert len(azureMock.constructorCalls) == 1
    return azureMock.constructorCalls[0]["baseURL"]


async def captureClientHeaders(azureMock: _AzureMock, headers: dict[str, str] | None = None) -> dict[str, Any]:
    model = getModel("azure", "gpt-4o-mini")
    await streamAzureOpenAIResponses(
        model,
        normalizeContext(context),
        {"apiKey": "test-api-key", "azureBaseUrl": "https://my-resource.openai.azure.com", "headers": headers},
    ).result()
    assert len(azureMock.constructorCalls) == 1
    return azureMock.constructorCalls[0].get("defaultHeaders") or {}


# --- azure base URL normalization ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("baseUrl", "expected"),
    [
        # normalizes Cognitive Services root endpoints to /openai/v1
        (
            "https://marc-quicktests-resource.cognitiveservices.azure.com",
            "https://marc-quicktests-resource.cognitiveservices.azure.com/openai/v1",
        ),
        # normalizes Microsoft Foundry root endpoints to /openai/v1
        ("https://marc-quicktests-resource.ai.azure.com", "https://marc-quicktests-resource.ai.azure.com/openai/v1"),
        # normalizes Azure OpenAI root endpoints to /openai/v1
        ("https://my-resource.openai.azure.com", "https://my-resource.openai.azure.com/openai/v1"),
        # normalizes /openai to /openai/v1
        ("https://my-resource.cognitiveservices.azure.com/openai", "https://my-resource.cognitiveservices.azure.com/openai/v1"),
        # preserves /openai/v1 endpoints
        (
            "https://my-resource.cognitiveservices.azure.com/openai/v1",
            "https://my-resource.cognitiveservices.azure.com/openai/v1",
        ),
        # normalizes /openai/v1/responses to /openai/v1
        ("https://my-resource.services.ai.azure.com/openai/v1/responses", "https://my-resource.services.ai.azure.com/openai/v1"),
        # preserves explicit non-Azure proxy paths
        ("https://my-proxy.example.com/v1", "https://my-proxy.example.com/v1"),
        # strips query params when normalizing Azure host URLs
        ("https://my-resource.openai.azure.com/openai?api-version=2024-12-01", "https://my-resource.openai.azure.com/openai/v1"),
        # preserves query params on non-Azure proxy URLs
        ("https://my-proxy.example.com/v1?custom=true", "https://my-proxy.example.com/v1?custom=true"),
    ],
)
async def test_normalizes_the_base_url(azureMock, monkeypatch, baseUrl, expected):
    assert await captureClientBaseUrl(azureMock, monkeypatch, baseUrl) == expected


async def test_throws_on_invalid_urls(azureMock, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "not-a-url")
    model = getModel("azure", "gpt-4o-mini")
    result = await streamAzureOpenAIResponses(model, normalizeContext(context), {"apiKey": "test-api-key"}).result()
    assert result.stopReason == "error"
    assert "Invalid Azure OpenAI base URL" in result.errorMessage


async def test_clamps_prompt_cache_key_to_openais_64_character_limit(azureMock):
    model = getModel("azure", "gpt-4o-mini")
    await streamAzureOpenAIResponses(
        model,
        normalizeContext(context),
        {"apiKey": "test-api-key", "azureBaseUrl": "https://my-resource.openai.azure.com", "sessionId": "x" * 67},
    ).result()

    assert azureMock.lastParams["prompt_cache_key"] == "x" * 64


async def test_disables_server_side_response_storage(azureMock):
    model = getModel("azure", "gpt-4o-mini")
    await streamAzureOpenAIResponses(
        model, normalizeContext(context), {"apiKey": "test-api-key", "azureBaseUrl": "https://my-resource.openai.azure.com"}
    ).result()

    assert azureMock.lastParams["store"] is False


async def test_honors_supports_strict_mode_false(azureMock):
    baseModel = getModel("azure", "gpt-4o-mini")
    model = Model.model_validate(
        {**baseModel.model_dump(), "compat": {**(baseModel.model_dump().get("compat") or {}), "supportsStrictMode": False}}
    )

    await streamAzureOpenAIResponses(
        model,
        normalizeContext(
            Context(
                messages=context.messages,
                tools=[
                    Tool(
                        name="preferred",
                        description="Preferred constrained tool",
                        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                        constrainedSampling={"type": "json_schema", "strict": "prefer"},
                    )
                ],
            )
        ),
        {"apiKey": "test-api-key", "azureBaseUrl": "https://my-resource.openai.azure.com"},
    ).result()

    assert "strict" not in azureMock.lastParams["tools"][0]


async def test_builds_the_default_url_from_azure_openai_resource_name(azureMock, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_RESOURCE_NAME", "my-resource")
    model = getModel("azure", "gpt-4o-mini")
    await streamAzureOpenAIResponses(model, normalizeContext(context), {"apiKey": "test-api-key"}).result()
    assert len(azureMock.constructorCalls) == 1
    assert azureMock.constructorCalls[0]["baseURL"] == "https://my-resource.openai.azure.com/openai/v1"


# --- azure provider stream events ---------------------------------------------------------------


async def test_forwards_parsed_events_in_order_before_normalizing_the_response(azureMock):
    azureMock.streamEvents = [
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_azure"}},
        {"type": "response.completed", "sequence_number": 1, "response": {"id": "resp_azure", "status": "completed"}},
    ]
    model = getModel("azure", "gpt-4o-mini")
    received: list[Any] = []
    eventModels: list[Model] = []

    async def onProviderStreamEvent(event: Any, eventModel: Model) -> None:
        received.append(event)
        eventModels.append(eventModel)

    result = await streamAzureOpenAIResponses(
        model,
        normalizeContext(context),
        {
            "apiKey": "test-api-key",
            "azureBaseUrl": "https://my-resource.openai.azure.com",
            "onProviderStreamEvent": onProviderStreamEvent,
        },
    ).result()

    assert received == azureMock.streamEvents
    assert received[0] is azureMock.streamEvents[0]
    assert received[1] is azureMock.streamEvents[1]
    assert eventModels == [model, model]
    assert result.stopReason == "stop"
    assert result.responseId == "resp_azure"


# --- azure user agent ---------------------------------------------------------------------------


async def test_uses_the_default_user_agent(azureMock):
    assert (await captureClientHeaders(azureMock))["User-Agent"] == PI_USER_AGENT


async def test_lets_explicit_headers_override_the_default_user_agent(azureMock):
    assert (await captureClientHeaders(azureMock, {"User-Agent": "custom-agent"}))["User-Agent"] == "custom-agent"


# --- azure-openai-responses-reasoning-replay.test.ts --------------------------------------------


def _replayModel() -> Model:
    return Model(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        api="azure-openai-responses",
        provider="azure",
        baseUrl="https://example.invalid",
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=400000,
        maxTokens=128000,
    )


def _replayOutput(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="pending",
        timestamp=time.time_ns() // 1_000_000,
    )


async def _replayEvents(doneItem: dict[str, Any], completedItem: dict[str, Any]):
    yield {
        "type": "response.output_item.added",
        "output_index": 0,
        "sequence_number": 0,
        "item": {"type": "reasoning", "id": doneItem["id"], "summary": []},
    }
    yield {"type": "response.output_item.done", "output_index": 0, "sequence_number": 1, "item": doneItem}
    yield {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {"id": "resp_test", "status": "completed", "output": [completedItem]},
    }


def getReplayedReasoning(model: Model, assistant: AssistantMessage) -> dict[str, Any] | None:
    now = time.time_ns() // 1_000_000
    replayContext = normalizeContext(
        Context(
            messages=[
                UserMessage(content="first", timestamp=now - 1),
                assistant,
                UserMessage(content="follow-up", timestamp=now),
            ]
        )
    )
    input = convertResponsesMessages(model, replayContext, {"azure"})
    return next((item for item in input if item.get("type") == "reasoning"), None)


async def test_preserves_existing_encrypted_content_from_output_item_done():
    model = _replayModel()
    output = _replayOutput(model)
    doneItem = {"type": "reasoning", "id": "rs_done", "summary": [], "encrypted_content": "from-output-item-done"}
    completedItem = {**doneItem, "encrypted_content": "from-response-completed"}

    await processResponsesStream(_replayEvents(doneItem, completedItem), output, AssistantMessageEventStream(), model)

    replayed = getReplayedReasoning(model, output)
    assert (replayed["type"], replayed["id"], replayed["encrypted_content"]) == (
        "reasoning",
        "rs_done",
        "from-output-item-done",
    )


async def test_fills_encrypted_content_when_output_item_done_omitted_it():
    model = _replayModel()
    output = _replayOutput(model)
    doneItem = {"type": "reasoning", "id": "rs_missing", "summary": []}
    completedItem = {**doneItem, "encrypted_content": "from-response-completed"}

    await processResponsesStream(_replayEvents(doneItem, completedItem), output, AssistantMessageEventStream(), model)

    replayed = getReplayedReasoning(model, output)
    assert (replayed["type"], replayed["id"], replayed["encrypted_content"]) == (
        "reasoning",
        "rs_missing",
        "from-response-completed",
    )


# --- azure-openai-tool-choice.test.ts -----------------------------------------------------------

toolChoiceModel = Model(
    id="test-deployment",
    name="Test Deployment",
    api="azure-openai-responses",
    provider="azure",
    baseUrl="http://127.0.0.1:9/openai/v1",
    reasoning=False,
    input=["text"],
    cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
    contextWindow=10_000,
    maxTokens=1_000,
)


def _toolChoiceContext():
    return normalizeContext(
        Context(
            messages=[UserMessage(content="Summarize this", timestamp=1)],
            tools=[
                Tool(
                    name="read",
                    description="Read a file",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                )
            ],
        )
    )


def _payloadCapture(captured: list[Any]):
    def onPayload(requestPayload: Any, _model: Any) -> Any:
        captured.append(requestPayload)
        raise RuntimeError("payload captured")

    return onPayload


async def test_forwards_provider_specific_tool_choice_while_preserving_tool_definitions():
    captured: list[Any] = []
    result = streamAzureOpenAIResponses(
        toolChoiceModel,
        _toolChoiceContext(),
        {"apiKey": "test-key", "toolChoice": "required", "onPayload": _payloadCapture(captured)},
    )

    await result.result()

    assert captured[0]["tool_choice"] == "required"
    assert len(captured[0]["tools"]) == 1


async def test_forwards_provider_neutral_tool_choice_from_simple_options():
    captured: list[Any] = []
    result = streamSimple(
        toolChoiceModel,
        _toolChoiceContext(),
        {"apiKey": "test-key", "toolChoice": "none", "onPayload": _payloadCapture(captured)},
    )

    await result.result()

    assert captured[0]["tool_choice"] == "none"
    assert len(captured[0]["tools"]) == 1
