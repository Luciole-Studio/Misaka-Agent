"""Translated from pi's ``packages/ai/test/cloudflare-stream.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from misaka.ai.providers.cloudflare_stream import cloudflareStreams
from misaka.ai.types import Context, Model
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext

model = Model.model_validate(
    {
        "id": "model",
        "name": "model",
        "api": "openai-completions",
        "provider": "cloudflare-ai-gateway",
        "baseUrl": "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/openai",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    }
)

context = normalizeContext(Context(messages=[]))


def capturing(captured: list[str]) -> Any:
    def respond(requestModel: Model, *_args: Any) -> AssistantMessageEventStream:
        captured.append(requestModel.baseUrl)
        return AssistantMessageEventStream()

    return SimpleNamespace(stream=respond, streamSimple=respond)


def test_materializes_the_model_endpoint_before_dispatch():
    captured: list[str] = []
    streams = cloudflareStreams(capturing(captured))
    env = {
        "CLOUDFLARE_ACCOUNT_ID": "account",
        "CLOUDFLARE_GATEWAY_ID": "gateway",
    }

    streams.stream(model, context, {"env": env})
    streams.streamSimple(model, context, {"env": env})

    assert captured == [
        "https://gateway.ai.cloudflare.com/v1/account/gateway/openai",
        "https://gateway.ai.cloudflare.com/v1/account/gateway/openai",
    ]


def test_keeps_placeholders_when_the_provider_env_does_not_resolve_them():
    captured: list[str] = []
    streams = cloudflareStreams(capturing(captured))

    streams.streamSimple(model, context, {})

    assert captured == [model.baseUrl]
