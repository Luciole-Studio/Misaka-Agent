"""Translated from pi's ``packages/ai/test/google-raw-stop-reason.test.ts`` (v1.1.0).

MISAKA fork: ``vi.mock("@google/genai")`` becomes a fake client patched over each adapter's ``GoogleGenAI``; the Python
SDK streams through ``client.aio.models.generate_content_stream`` and takes snake_case keyword arguments
(``http_options``). The chunks stay the camelCase objects upstream yields, which ``_google_sdk.coalesce_attr`` reads as
dicts. pi's User-Agent is MISAKA's (``getPiUserAgent``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.api import google_generative_ai, google_vertex
from misaka.ai.api.google_generative_ai import stream as streamGoogleGenerativeAi
from misaka.ai.api.google_vertex import stream as streamGoogleVertex
from misaka.ai.compat import getModel
from misaka.ai.types import TextContent
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext

googleGenAiMock: dict[str, Any] = {
    "constructorCalls": [],
    "finishReason": "MALFORMED_FUNCTION_CALL",
    "includeFunctionCall": False,
    "streamChunks": None,
}


class GoogleGenAI:
    def __init__(self, **config: Any) -> None:
        googleGenAiMock["constructorCalls"].append(config)
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._generateContentStream))

    async def _generateContentStream(self, **_params: Any) -> Any:
        async def chunks() -> Any:
            if googleGenAiMock["streamChunks"]:
                for chunk in googleGenAiMock["streamChunks"]:
                    yield chunk
                return
            yield {
                "responseId": "google-response-id",
                "candidates": [
                    {
                        "finishReason": googleGenAiMock["finishReason"],
                        **(
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "id": "call-1",
                                                "name": "echo",
                                                "args": {"value": "truncated"},
                                            },
                                        },
                                    ],
                                },
                            }
                            if googleGenAiMock["includeFunctionCall"]
                            else {}
                        ),
                    },
                ],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 0,
                    "totalTokenCount": 1,
                },
            }

        return chunks()


@pytest.fixture(autouse=True)
def _mockGoogleGenAI(monkeypatch):
    monkeypatch.setattr(google_generative_ai, "GoogleGenAI", GoogleGenAI)
    monkeypatch.setattr(google_vertex, "GoogleGenAI", GoogleGenAI)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    googleGenAiMock["streamChunks"] = None


PI_USER_AGENT = getPiUserAgent()

context = normalizeContext({"messages": [{"role": "user", "content": "hello", "timestamp": 0}]})


async def captureGoogleHeaders(headers: dict[str, str] | None = None) -> dict[str, str]:
    googleGenAiMock["constructorCalls"].clear()
    googleGenAiMock["finishReason"] = "STOP"
    googleGenAiMock["includeFunctionCall"] = False
    await streamGoogleGenerativeAi(
        getModel("google", "gemini-2.5-flash"),
        context,
        {
            "apiKey": "test-api-key",
            "headers": headers,
        },
    ).result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    httpOptions = googleGenAiMock["constructorCalls"][0]["http_options"]
    return httpOptions.get("headers") or {}


# --- Google raw stop reasons ---------------------------------------------------------------------


async def test_preserves_raw_gemini_finish_reasons_for_google_generative_ai_errors():
    googleGenAiMock["finishReason"] = "MALFORMED_FUNCTION_CALL"
    googleGenAiMock["includeFunctionCall"] = False

    stream = streamGoogleGenerativeAi(
        getModel("google", "gemini-2.5-flash"),
        context,
        {
            "apiKey": "test-api-key",
        },
    )

    message = await stream.result()

    assert message.stopReason == "error"
    assert message.rawStopReason == "MALFORMED_FUNCTION_CALL"
    assert message.errorMessage == "Provider stopped with: MALFORMED_FUNCTION_CALL"


async def test_preserves_raw_gemini_finish_reasons_for_google_vertex_errors():
    googleGenAiMock["finishReason"] = "SAFETY"
    googleGenAiMock["includeFunctionCall"] = False

    stream = streamGoogleVertex(
        getModel("google-vertex", "gemini-3-flash-preview"),
        context,
        {
            "project": "test-project",
            "location": "us-central1",
        },
    )

    message = await stream.result()

    assert message.stopReason == "error"
    assert message.rawStopReason == "SAFETY"
    assert message.errorMessage == "Provider stopped with: SAFETY"


adapters = [
    pytest.param(
        lambda: streamGoogleGenerativeAi(
            getModel("google", "gemini-2.5-flash"),
            context,
            {
                "apiKey": "test-api-key",
            },
        ),
        id="Google Generative AI",
    ),
    pytest.param(
        lambda: streamGoogleVertex(
            getModel("google-vertex", "gemini-3-flash-preview"),
            context,
            {
                "project": "test-project",
                "location": "us-central1",
            },
        ),
        id="Google Vertex",
    ),
]


@pytest.mark.parametrize("createStream", adapters)
async def test_preserves_max_tokens_with_a_tool_call_as_length(createStream):
    googleGenAiMock["finishReason"] = "MAX_TOKENS"
    googleGenAiMock["includeFunctionCall"] = True

    message = await createStream().result()

    assert message.stopReason == "length"
    assert message.rawStopReason == "MAX_TOKENS"
    assert any(block.type == "toolCall" for block in message.content)


@pytest.mark.parametrize("createStream", adapters)
async def test_maps_stop_with_a_tool_call_to_tool_use(createStream):
    googleGenAiMock["finishReason"] = "STOP"
    googleGenAiMock["includeFunctionCall"] = True

    message = await createStream().result()

    assert message.stopReason == "toolUse"
    assert message.rawStopReason == "STOP"
    assert any(block.type == "toolCall" for block in message.content)


# --- Google provider stream events ---------------------------------------------------------------

googleModel = getModel("google", "gemini-2.5-flash")
vertexModel = getModel("google-vertex", "gemini-3-flash-preview")
streamEventAdapters = [
    pytest.param(
        googleModel,
        lambda onProviderStreamEvent: streamGoogleGenerativeAi(
            googleModel,
            context,
            {
                "apiKey": "test-api-key",
                "onProviderStreamEvent": onProviderStreamEvent,
            },
        ),
        id="Google Generative AI",
    ),
    pytest.param(
        vertexModel,
        lambda onProviderStreamEvent: streamGoogleVertex(
            vertexModel,
            context,
            {
                "project": "test-project",
                "location": "us-central1",
                "onProviderStreamEvent": onProviderStreamEvent,
            },
        ),
        id="Google Vertex",
    ),
]


@pytest.mark.parametrize(("model", "createStream"), streamEventAdapters)
async def test_forwards_each_sdk_chunk_in_order_before_normalizing_it(model, createStream):
    googleGenAiMock["streamChunks"] = [
        {
            "responseId": "resp_google",
            "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
        },
        {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3},
        },
    ]
    received: list[Any] = []
    eventModels: list[Any] = []

    async def onProviderStreamEvent(chunk: Any, eventModel: Any) -> None:
        await asyncio.sleep(0)
        received.append(chunk)
        eventModels.append(eventModel)

    result = await createStream(onProviderStreamEvent).result()

    assert received == googleGenAiMock["streamChunks"]
    assert received[0] is googleGenAiMock["streamChunks"][0]
    assert received[1] is googleGenAiMock["streamChunks"][1]
    assert eventModels == [model, model]
    assert result.stopReason == "stop"
    assert result.responseId == "resp_google"
    assert result.content == [TextContent(type="text", text="hello")]


# --- Google Generative AI user agent -------------------------------------------------------------


async def test_uses_pis_user_agent_by_default():
    assert (await captureGoogleHeaders())["User-Agent"] == PI_USER_AGENT


async def test_lets_explicit_headers_override_the_default_user_agent():
    assert (await captureGoogleHeaders({"User-Agent": "custom-agent"}))["User-Agent"] == "custom-agent"
