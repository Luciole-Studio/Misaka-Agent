"""Translated from pi's ``packages/ai/test/google-vertex-api-key-resolution.test.ts`` (v1.1.0).

MISAKA fork: ``vi.mock("@google/genai")`` becomes a fake client patched over the adapter's ``GoogleGenAI``. The Python
SDK takes snake_case keyword arguments (``api_key``, ``http_options``) and reads the API version from the HTTP options
alone, so the ``apiVersion`` upstream expects beside ``httpOptions`` is asserted inside them; a custom base URL without
a version gets ``/v1`` (``google_vertex._buildHttpOptions``). pi's User-Agent is MISAKA's (``getPiUserAgent``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.api import google_vertex
from misaka.ai.api.google_vertex import stream as streamGoogleVertex
from misaka.ai.compat import getModel
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext

googleGenAiMock: dict[str, Any] = {"constructorCalls": []}


class GoogleGenAI:
    def __init__(self, **config: Any) -> None:
        googleGenAiMock["constructorCalls"].append(config)
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._generateContentStream))

    async def _generateContentStream(self, **_params: Any) -> Any:
        async def chunks() -> Any:
            yield {
                "responseId": "vertex-response-id",
                "candidates": [
                    {
                        "content": {"parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    },
                ],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 2,
                },
            }

        return chunks()


PI_USER_AGENT = getPiUserAgent()
model = getModel("google-vertex", "gemini-3-flash-preview")
context = normalizeContext({"messages": [{"role": "user", "content": "hello", "timestamp": 0}]})


@pytest.fixture(autouse=True)
def _mockGoogleGenAI(monkeypatch):
    monkeypatch.setattr(google_vertex, "GoogleGenAI", GoogleGenAI)
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    googleGenAiMock["constructorCalls"].clear()


def _matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """``toMatchObject``: every expected key is present with a matching value; nested objects match the same way."""
    for key, value in expected.items():
        if key not in actual:
            return False
        if isinstance(value, dict):
            if not isinstance(actual[key], dict) or not _matches(actual[key], value):
                return False
        elif actual[key] != value:
            return False
    return True


async def test_falls_back_to_adc_when_options_api_key_is_a_placeholder_marker():
    stream = streamGoogleVertex(
        model,
        context,
        {
            "apiKey": "<authenticated>",
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
            "http_options": {"apiVersion": "v1"},
        },
    )
    assert "api_key" not in googleGenAiMock["constructorCalls"][0]


async def test_falls_back_to_adc_when_options_api_key_is_the_gcp_vertex_credentials_marker():
    stream = streamGoogleVertex(
        model,
        context,
        {
            "apiKey": "gcp-vertex-credentials",
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
            "http_options": {"apiVersion": "v1"},
        },
    )
    assert "api_key" not in googleGenAiMock["constructorCalls"][0]


async def test_falls_back_to_adc_when_google_cloud_api_key_is_a_placeholder_marker(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", "<authenticated>")

    stream = streamGoogleVertex(
        model,
        context,
        {
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
            "http_options": {"apiVersion": "v1"},
        },
    )
    assert "api_key" not in googleGenAiMock["constructorCalls"][0]


async def test_still_uses_the_api_key_client_for_real_api_keys():
    stream = streamGoogleVertex(
        model,
        context,
        {
            "apiKey": "AIzaSyExampleRealisticLookingApiKey123456",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "api_key": "AIzaSyExampleRealisticLookingApiKey123456",
            "http_options": {"apiVersion": "v1"},
        },
    )
    assert "project" not in googleGenAiMock["constructorCalls"][0]
    assert "location" not in googleGenAiMock["constructorCalls"][0]


async def test_does_not_forward_generated_vertex_base_url_placeholders():
    stream = streamGoogleVertex(
        model,
        context,
        {
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert googleGenAiMock["constructorCalls"][0]["http_options"] == {
        "apiVersion": "v1",
        "headers": {"User-Agent": PI_USER_AGENT},
    }


async def test_lets_explicit_headers_override_the_default_user_agent():
    stream = streamGoogleVertex(
        model,
        context,
        {
            "project": "test-project",
            "location": "us-central1",
            "headers": {"User-Agent": "custom-agent"},
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert googleGenAiMock["constructorCalls"][0]["http_options"] == {
        "apiVersion": "v1",
        "headers": {"User-Agent": "custom-agent"},
    }


async def test_forwards_custom_base_url_to_the_adc_client():
    customModel = model.model_copy(update={"baseUrl": "https://proxy.example.com"})
    stream = streamGoogleVertex(
        customModel,
        context,
        {
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
            "http_options": {
                "baseUrl": "https://proxy.example.com/v1",
                "baseUrlResourceScope": "COLLECTION",
                "apiVersion": "",
            },
        },
    )


async def test_forwards_custom_base_url_to_the_api_key_client():
    customModel = model.model_copy(update={"baseUrl": "https://proxy.example.com"})
    stream = streamGoogleVertex(
        customModel,
        context,
        {
            "apiKey": "AIzaSyExampleRealisticLookingApiKey123456",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "vertexai": True,
            "api_key": "AIzaSyExampleRealisticLookingApiKey123456",
            "http_options": {
                "baseUrl": "https://proxy.example.com/v1",
                "baseUrlResourceScope": "COLLECTION",
                "apiVersion": "",
            },
        },
    )


async def test_does_not_append_api_version_when_custom_base_url_already_includes_one():
    customModel = model.model_copy(
        update={"baseUrl": "https://proxy.example.com/v1/projects/test-project/locations/global"}
    )
    stream = streamGoogleVertex(
        customModel,
        context,
        {
            "project": "test-project",
            "location": "us-central1",
        },
    )

    await stream.result()

    assert len(googleGenAiMock["constructorCalls"]) == 1
    assert _matches(
        googleGenAiMock["constructorCalls"][0],
        {
            "http_options": {
                "baseUrl": "https://proxy.example.com/v1/projects/test-project/locations/global",
                "baseUrlResourceScope": "COLLECTION",
                "apiVersion": "",
            },
        },
    )
