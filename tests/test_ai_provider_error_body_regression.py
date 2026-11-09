"""Translated from pi's ``packages/ai/test/provider-error-body-regression.test.ts`` (v1.1.0).

Per-tier provider regression for issues/provider-error-body-passthrough.

Routes a 403-with-body error through the real provider catch path for one representative per tier (Success Criterion
7): a body-blind text provider (openai-completions), a status-only provider (openai-responses), and a body-blind Bedrock
provider. Each asserts the resulting errorMessage carries both the HTTP status and the body reason. The image-provider
tier is covered by provider-error-body-passthrough.test.ts; the already-correct happy path (no double body / no
duplicated status) is asserted via the shared helper in error-body.test.ts.

MISAKA fork: pi mocks the ``openai`` module's client and the AWS SDK. Here ``_openai_sdk._create_with_response`` raises
the error, and the boto client's ``converse_stream`` raises botocore's ``ClientError`` (what the Python SDK raises for a
gateway's error body). One more case runs the OpenRouter ``metadata.raw`` body through a real openai-python
``PermissionDeniedError``, where the parsed body sits on ``error.body``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import openai
import pytest
from botocore.exceptions import ClientError

from misaka.ai.api import _openai_sdk
from misaka.ai.api.bedrock_converse_stream import streamSimple as streamSimpleBedrock
from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.compat import getModel
from misaka.ai.types import Context, Model, TextContent, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.bedrock_fake import FakeBedrockClient, useFakeBedrock


# openai SDK APIError shape: "<status> status code (no body)" message, the
# parsed body kept on `.error`.
class FakeAPIError(Exception):
    def __init__(self, status: int, parsedBody: Any) -> None:
        super().__init__(f"{status} status code (no body)")
        self.name = "PermissionDeniedError"
        self.status = status
        self.error = parsedBody


@pytest.fixture
def openaiMock(monkeypatch):
    state = {"parsedBody": {"error": "blocked by gateway WAF"}, "error": None}

    async def throwingCreate(*_args: Any) -> Any:
        raise state["error"] if state["error"] is not None else FakeAPIError(403, state["parsedBody"])

    monkeypatch.setattr(_openai_sdk, "_create_with_response", throwingCreate)
    return state


context = normalizeContext(
    Context(systemPrompt="", messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)], tools=[])
)

completionsModel = Model.model_validate(
    {
        "id": "test-model",
        "name": "Test Model",
        "api": "openai-completions",
        "provider": "openrouter",
        "baseUrl": "https://openrouter.ai/api/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    }
)

responsesModel = Model.model_validate(
    {
        "id": "gpt-test",
        "name": "GPT Test",
        "api": "openai-responses",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    }
)


async def drainResult(stream: Any) -> Any:
    async for _event in stream:
        pass
    return await stream.result()


async def test_openai_completions_body_blind_text_surfaces_status_and_body(openaiMock):
    output = await drainResult(streamOpenAICompletions(completionsModel, context, {"apiKey": "test"}))

    assert output.stopReason == "error"
    assert "403" in output.errorMessage
    assert "blocked by gateway WAF" in output.errorMessage
    assert output.errorMessage != "403 status code (no body)"


async def test_openai_completions_does_not_double_print_the_openrouter_metadata_raw_extra(openaiMock):
    # OpenRouter returns the extra reason under error.error.metadata.raw, which
    # is part of the parsed body normalizeProviderError already surfaces. The
    # manual append must not duplicate it.
    openaiMock["parsedBody"] = {
        "message": "Provider returned error",
        "code": 403,
        "metadata": {"raw": "upstream WAF blocked policy XYZ"},
    }

    output = await drainResult(streamOpenAICompletions(completionsModel, context, {"apiKey": "test"}))

    assert "upstream WAF blocked policy XYZ" in output.errorMessage
    assert len(re.findall(r"upstream WAF blocked policy XYZ", output.errorMessage)) == 1


async def test_openai_completions_does_not_double_print_metadata_raw_from_an_openai_python_error(openaiMock):
    body = {"message": "Provider returned error", "code": 403, "metadata": {"raw": "upstream WAF blocked policy XYZ"}}
    response = httpx.Response(403, json={"error": body}, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    openaiMock["error"] = openai.PermissionDeniedError(f"Error code: 403 - {body}", response=response, body=body)

    output = await drainResult(streamOpenAICompletions(completionsModel, context, {"apiKey": "test"}))

    assert "403" in output.errorMessage
    assert len(re.findall(r"upstream WAF blocked policy XYZ", output.errorMessage)) == 1


async def test_openai_responses_status_only_keeps_the_prefix_and_surfaces_the_body(openaiMock):
    output = await drainResult(streamOpenAIResponses(responsesModel, context, {"apiKey": "test"}))

    assert output.stopReason == "error"
    assert "OpenAI API error (403)" in output.errorMessage
    assert "blocked by gateway WAF" in output.errorMessage


class _FailingBedrockClient(FakeBedrockClient):
    def converse_stream(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        raise self.fail


async def test_bedrock_body_blind_surfaces_the_gateway_body_instead_of_unknown_error(monkeypatch):
    useFakeBedrock(
        monkeypatch,
        _FailingBedrockClient(
            # What botocore's rest-json parser makes of a gateway's `403 {"message":"blocked by gateway WAF"}`.
            fail=ClientError(
                {"Error": {"Message": "blocked by gateway WAF", "Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                "ConverseStream",
            )
        ),
    )
    model = getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8")

    output = await drainResult(streamSimpleBedrock(model, normalizeContext(Context(messages=context.messages)), {}))

    assert output.stopReason == "error"
    assert "403" in output.errorMessage
    assert "blocked by gateway WAF" in output.errorMessage
    assert "Unknown: UnknownError" not in output.errorMessage


async def test_bedrock_preserves_the_sdk_validation_message_when_the_response_body_is_a_stream(monkeypatch):
    useFakeBedrock(
        monkeypatch,
        _FailingBedrockClient(
            fail=ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Invocation of model ID anthropic.claude-opus-5 with on-demand throughput isn't supported. Retry with an inference profile.",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 400},
                },
                "ConverseStream",
            )
        ),
    )
    model = getModel("amazon-bedrock", "global.anthropic.claude-opus-5")

    output = await drainResult(streamSimpleBedrock(model, normalizeContext(Context(messages=context.messages)), {}))

    assert output.stopReason == "error"
    assert "on-demand throughput isn't supported" in output.errorMessage
    assert "inference profile" in output.errorMessage
    assert "_readableState" not in output.errorMessage
