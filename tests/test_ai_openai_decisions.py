"""Translated from pi's ``packages/ai/test/openai-decisions.test.ts`` (v1.1.0).

MISAKA fork: ``options.fetch`` is not ported; ``httpx.AsyncClient.post`` is patched to the test's handler instead.
"""

from __future__ import annotations

import json

import httpx
import pytest

from misaka.ai.api.openai_decisions import classify
from misaka.ai.types import ClassifierContext, ClassifierModel, ImageContent
from misaka.utils.js import jsonStringify

model = ClassifierModel.model_validate(
    {
        "type": "classifier",
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "api": "openai-decisions",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "input": ["text", "image"],
        "cost": {
            "input": 0.1,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "tiers": [{"inputTokensAbove": 272000, "input": 0.2, "output": 0, "cacheRead": 0, "cacheWrite": 0}],
        },
        "contextWindow": 922000,
    }
)

context = ClassifierContext.model_validate(
    {
        "state": {"text": "The deployment succeeded, thank you."},
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "Classify the message",
                "criteria": {"success": "Successful", "failure": ""},
            },
            "satisfaction": {
                "type": "score",
                "instructions": "Score satisfaction",
                "criteria": ["low", "neutral", "high"],
            },
            "approved": {
                "type": "bool",
                "instructions": "Does the user approve?",
                "criteria": {"true": "Approval", "false": "No approval"},
            },
        },
    }
)

# Response shape from the API reference and live `gpt-6-luna` requests.
wireAnswers = [
    {
        "type": "choice",
        "name": "category",
        "choice": "success",
        "probabilities": [
            {"value": "success", "probability": 0.9},
            {"value": "failure", "probability": 0.1},
        ],
        "confidence": 0.8,
    },
    {
        "type": "score",
        "name": "satisfaction",
        "score": 1.8,
        "probabilities": [
            {"value": 0, "label": "low", "probability": 0.05},
            {"value": 1, "label": "neutral", "probability": 0.1},
            {"value": 2, "label": "high", "probability": 0.85},
        ],
        "confidence": 0.7,
    },
    {"type": "predicate", "name": "approved", "probability": 0.95},
]

wireUsage = {
    "input_tokens": 164,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 0,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 164,
}

image = ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")


def _fetch(monkeypatch, handler) -> list[dict]:
    """``options.fetch``: every POST goes to ``handler()``; the calls are recorded."""
    calls: list[dict] = []

    async def post(self, url, *, headers=None, content=None, **_kwargs):
        calls.append({"url": str(url), "headers": httpx.Headers(headers or {}), "body": content})
        response = handler()
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


async def test_maps_questions_to_decisions_types_and_answers_back_by_name(monkeypatch):
    # Answers out of question order: they are matched by name.
    calls = _fetch(
        monkeypatch,
        lambda: httpx.Response(200, json={"model": "gpt-6-luna", "answers": list(reversed(wireAnswers)), "usage": wireUsage}),
    )

    result = await classify(model, context, {"apiKey": "secret", "temperature": 1.5})

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.openai.com/v1/decisions"
    assert calls[0]["headers"].get("authorization") == "Bearer secret"
    assert json.loads(calls[0]["body"]) == {
        "model": "gpt-6-luna",
        "input": jsonStringify(context.state),
        "questions": [
            {
                "type": "choice",
                "name": "category",
                "instructions": "Classify the message",
                # Empty descriptions are omitted.
                "choices": [{"value": "success", "description": "Successful"}, {"value": "failure"}],
            },
            {
                "type": "score",
                "name": "satisfaction",
                "instructions": "Score satisfaction",
                "levels": [{"label": "low"}, {"label": "neutral"}, {"label": "high"}],
            },
            {
                "type": "predicate",
                "name": "approved",
                "instructions": "Does the user approve?\n\nTrue means: Approval\nFalse means: No approval",
            },
        ],
    }
    assert result.stopReason == "stop"
    assert {id: answer.model_dump() for id, answer in result.answers.items()} == {
        "category": {
            "type": "choice",
            "choice": "success",
            "probabilities": {"success": 0.9, "failure": 0.1},
            "confidence": 0.8,
        },
        "satisfaction": {"type": "score", "score": 1.8, "confidence": 0.7},
        "approved": {"type": "bool", "probability": 0.95},
    }
    assert (result.usage.input, result.usage.output, result.usage.cacheRead, result.usage.totalTokens) == (164, 0, 0, 164)
    assert result.usage.cost.total == pytest.approx(0.0000164, abs=1e-12)


async def test_prices_long_context_requests_at_the_long_context_input_rate(monkeypatch):
    _fetch(
        monkeypatch,
        lambda: httpx.Response(200, json={"answers": wireAnswers, "usage": {"input_tokens": 300000, "output_tokens": 0}}),
    )

    result = await classify(model, context, {"apiKey": "secret"})

    assert result.usage.cost.total == pytest.approx(0.06, abs=1e-12)


async def test_sends_images_after_the_state_in_one_user_message(monkeypatch):
    calls = _fetch(monkeypatch, lambda: httpx.Response(200, json={"answers": wireAnswers}))

    result = await classify(
        model,
        context.model_copy(update={"images": [image, image.model_copy(update={"mimeType": "image/jpeg"})]}),
        {
            "apiKey": "secret",
        },
    )

    assert result.stopReason == "stop"
    assert json.loads(calls[0]["body"])["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": jsonStringify(context.state)},
                {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,aW1hZ2U="},
            ],
        },
    ]


async def test_rejects_more_than_128_images_before_sending(monkeypatch):
    calls = _fetch(monkeypatch, lambda: httpx.Response(200, json={"answers": wireAnswers}))

    result = await classify(
        model,
        context.model_copy(update={"images": [image for _ in range(129)]}),
        {
            "apiKey": "secret",
        },
    )

    assert calls == []
    assert result.stopReason == "error"
    assert "at most 128 images, got 129" in result.errorMessage


async def test_fails_the_result_when_a_question_is_refused_and_keeps_the_billed_usage(monkeypatch):
    _fetch(
        monkeypatch,
        lambda: httpx.Response(
            200,
            json={
                "answers": [wireAnswers[0], wireAnswers[1], {"type": "refusal", "name": "approved"}],
                "usage": wireUsage,
            },
        ),
    )

    result = await classify(model, context, {"apiKey": "secret"})

    assert result.stopReason == "error"
    assert result.answers == {}
    assert result.errorMessage == "OpenAI Decisions refused to answer approved"
    assert result.usage.input == 164


async def test_returns_missing_and_mistyped_answers_as_classifier_errors(monkeypatch):
    _fetch(monkeypatch, lambda: httpx.Response(200, json={"answers": wireAnswers[0:2]}))
    missing = await classify(model, context, {"apiKey": "secret"})
    _fetch(
        monkeypatch,
        lambda: httpx.Response(200, json={"answers": [wireAnswers[0], wireAnswers[1], {"type": "score", "name": "approved"}]}),
    )
    mistyped = await classify(model, context, {"apiKey": "secret"})

    assert missing.stopReason == "error"
    assert "did not return an answer for approved" in missing.errorMessage
    assert mistyped.stopReason == "error"
    assert "did not return a predicate answer for approved" in mistyped.errorMessage


async def test_preserves_prototype_sensitive_question_ids_in_answers(monkeypatch):
    prototypeContext = ClassifierContext.model_validate(
        {
            "state": {},
            "questions": json.loads(
                '{"__proto__":{"type":"bool","instructions":"Is this true?","criteria":{"true":"Yes","false":"No"}}}'
            ),
        }
    )
    _fetch(
        monkeypatch,
        lambda: httpx.Response(200, json={"answers": [{"type": "predicate", "name": "__proto__", "probability": 0.75}]}),
    )

    result = await classify(model, prototypeContext, {"apiKey": "secret"})

    assert result.stopReason == "stop"
    assert "__proto__" in result.answers
    assert result.answers["__proto__"].model_dump() == {"type": "bool", "probability": 0.75}


async def test_does_not_retry_gateway_timeouts_and_explains_them_instead_of_returning_the_html_page(monkeypatch):
    calls = _fetch(
        monkeypatch,
        lambda: httpx.Response(504, text="<!DOCTYPE html><html>Gateway time-out</html>", headers={"retry-after-ms": "0"}),
    )
    # Default retries: the same input would time out again.
    result = await classify(model, context, {"apiKey": "secret"})

    assert len(calls) == 1
    assert result.stopReason == "error"
    assert "OpenAI Decisions error (504): the request timed out at the gateway" in result.errorMessage
    assert "<html>" not in result.errorMessage


async def test_still_retries_other_server_errors(monkeypatch):
    attempt = 0

    def reply() -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(503, text="busy", headers={"retry-after-ms": "0"})
        return httpx.Response(200, json={"answers": wireAnswers})

    _fetch(monkeypatch, reply)
    result = await classify(model, context, {"apiKey": "secret"})

    assert attempt == 2
    assert result.stopReason == "stop"


async def test_includes_the_api_error_body_for_other_http_failures(monkeypatch):
    _fetch(
        monkeypatch,
        lambda: httpx.Response(
            400,
            json={"error": {"message": "Decision input exceeds the token limit.", "type": "invalid_request_error"}},
        ),
    )

    result = await classify(model, context, {"apiKey": "secret", "maxRetries": 0})

    assert result.stopReason == "error"
    assert "OpenAI Decisions error (400)" in result.errorMessage
    assert "Decision input exceeds the token limit." in result.errorMessage


async def test_rejects_models_for_other_classifier_apis_and_missing_api_keys(monkeypatch):
    calls = _fetch(monkeypatch, lambda: httpx.Response(200, json={"answers": wireAnswers}))
    otherApi = await classify(model.model_copy(update={"api": "typesafe-system-one"}), context, {"apiKey": "secret"})
    noKey = await classify(model, context, {})

    assert calls == []
    assert "Unsupported classifier API: typesafe-system-one" in otherApi.errorMessage
    assert "No API key for provider: openai" in noKey.errorMessage
