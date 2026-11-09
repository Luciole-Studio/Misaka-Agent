"""Translated from pi's ``packages/ai/test/azure-openai-completions.test.ts`` (v1.1.0). See ``tests/openai_fake.py`` for
the ``vi.mock("openai")`` stand-in."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.compat import getModel
from misaka.ai.providers.azure import azureProvider
from misaka.ai.types import AssistantMessage, Context, SystemMessage, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.openai_fake import installFakeOpenAI

DEFAULT_CHUNKS = [
    {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
]

azure = azureProvider()


def _now() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def mockState(monkeypatch):
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT_NAME_MAP", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://my-resource.services.ai.azure.com")
    fake = installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)

    def create(resource: str, params: Any, _requestOptions: Any) -> Any:
        fake.dispatchedTo = resource
        if resource == "responses":
            raise RuntimeError("responses reached")
        return DEFAULT_CHUNKS

    fake.dispatchedTo = None
    fake.create = create
    return fake


context = normalizeContext(Context(systemPrompt="sys", messages=[UserMessage(content="hi", timestamp=_now())]))


def deepSeekModel():
    return getModel("azure", "deepseek-v4-pro")


# --- azure deepseek-v4-pro over Chat Completions ---------------------------------------------
# Regression for #9645: Azure Foundry rejects DeepSeek's thinking field and every prompt cache parameter here.


async def test_turns_thinking_on_with_reasoning_effort_instead_of_deepseeks_thinking_field(mockState):
    await azure.streamSimple(deepSeekModel(), context, {"apiKey": "test-key", "reasoning": "high"}).result()

    assert mockState.lastParams.get("reasoning_effort") == "high"
    assert mockState.lastParams.get("thinking") is None


async def test_clamps_thinking_levels_the_deployment_does_not_accept(mockState):
    await azure.streamSimple(deepSeekModel(), context, {"apiKey": "test-key", "reasoning": "max"}).result()

    assert mockState.lastParams.get("reasoning_effort") == "high"


async def test_sends_no_reasoning_effort_when_no_thinking_level_is_requested(mockState):
    await azure.streamSimple(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert mockState.lastParams.get("reasoning_effort") is None
    assert mockState.lastParams.get("thinking") is None


async def test_omits_prompt_cache_parameters_when_long_retention_comes_from_pi_cache_retention(mockState, monkeypatch):
    monkeypatch.setenv("PI_CACHE_RETENTION", "long")

    await azure.stream(deepSeekModel(), context, {"apiKey": "test-key", "sessionId": "session-env"}).result()

    assert mockState.lastParams.get("prompt_cache_key") is None
    assert mockState.lastParams.get("prompt_cache_retention") is None


# The deployment discards a `developer` system message once reasoning_effort is set, without
# billing it, so the system prompt has to go out under the system role.
async def test_sends_the_system_prompt_under_the_system_role(mockState):
    await azure.stream(deepSeekModel(), context, {"apiKey": "test-key", "reasoningEffort": "low"}).result()

    first = mockState.lastParams["messages"][0]
    assert (first["role"], first["content"]) == ("system", "sys")


# The deployment honours system messages sent mid-conversation, so pi must not collapse them.
async def test_keeps_mid_conversation_system_messages_in_place(mockState):
    resumed = normalizeContext(
        Context(
            systemPrompt="first",
            messages=[
                UserMessage(content="hi", timestamp=_now()),
                SystemMessage(content="second", timestamp=_now()),
                UserMessage(content="again", timestamp=_now()),
            ],
        )
    )

    await azure.stream(deepSeekModel(), resumed, {"apiKey": "test-key"}).result()

    assert [message["role"] for message in mockState.lastParams["messages"]] == ["system", "user", "system", "user"]


async def test_omits_prompt_cache_parameters_even_when_long_retention_is_requested(mockState):
    await azure.stream(
        deepSeekModel(), context, {"apiKey": "test-key", "cacheRetention": "long", "sessionId": "session-1"}
    ).result()

    assert mockState.lastParams.get("prompt_cache_key") is None
    assert mockState.lastParams.get("prompt_cache_retention") is None


async def test_replays_reasoning_content_on_assistant_turns_so_the_cached_prefix_is_unchanged(mockState):
    assistant = AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal reasoning", "thinkingSignature": "reasoning_content"},
                {"type": "text", "text": "answer"},
            ],
            "provider": "azure",
            "api": "openai-completions",
            "model": "deepseek-v4-pro",
            "timestamp": _now(),
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
        }
    )
    resumed = Context(
        systemPrompt="sys",
        messages=[
            UserMessage(content="first", timestamp=_now()),
            assistant,
            UserMessage(content="second", timestamp=_now()),
        ],
    )

    await azure.stream(deepSeekModel(), normalizeContext(resumed), {"apiKey": "test-key"}).result()

    assistantMessage = next(message for message in mockState.lastParams["messages"] if message["role"] == "assistant")
    assert assistantMessage.get("reasoning_content") == "internal reasoning"


# --- azure Chat Completions endpoint resolution -----------------------------------------------


async def test_normalizes_the_azure_endpoint_the_completions_client_is_built_with(mockState):
    await azure.stream(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert mockState.lastClientOptions["baseURL"] == "https://my-resource.services.ai.azure.com/openai/v1"


async def test_surfaces_an_unconfigured_endpoint_as_an_error_event_rather_than_throwing_out_of_stream(
    mockState, monkeypatch
):
    monkeypatch.delenv("AZURE_OPENAI_BASE_URL")

    result = await azure.stream(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert result.stopReason == "error"
    assert "Azure OpenAI base URL is required" in result.errorMessage


# The id is persisted on the assistant message and read back by name, so it has to stay a catalog id.
async def test_sends_the_model_id_as_the_request_model(mockState):
    result = await azure.stream(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert mockState.lastParams["model"] == "deepseek-v4-pro"
    assert result.model == "deepseek-v4-pro"


async def test_sends_the_mapped_deployment_name_while_keeping_the_catalog_id_on_the_message(mockState, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME_MAP", "deepseek-v4-pro=my-deepseek")

    result = await azure.streamSimple(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert mockState.lastParams["model"] == "my-deepseek"
    assert result.model == "deepseek-v4-pro"


async def test_passes_the_deployment_name_through_a_callers_on_payload(mockState, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME_MAP", "deepseek-v4-pro=my-deepseek")
    seen: dict[str, Any] = {}

    def onPayload(payload: Any, _model: Any) -> Any:
        seen["model"] = payload["model"]
        return {**payload, "temperature": 0.1}

    await azure.stream(deepSeekModel(), context, {"apiKey": "test-key", "onPayload": onPayload}).result()

    assert seen["model"] == "my-deepseek"
    assert (mockState.lastParams["model"], mockState.lastParams["temperature"]) == ("my-deepseek", 0.1)


# --- azure api map ----------------------------------------------------------------------------


async def test_still_routes_responses_models_to_the_responses_api(mockState):
    await azure.stream(getModel("azure", "gpt-4o-mini"), context, {"apiKey": "test-key"}).result()

    assert mockState.dispatchedTo == "responses"


async def test_routes_openai_completions_models_to_chat_completions(mockState):
    await azure.stream(deepSeekModel(), context, {"apiKey": "test-key"}).result()

    assert mockState.dispatchedTo == "chat.completions"
