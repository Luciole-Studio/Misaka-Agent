"""Translated from pi's ``packages/ai/test/cache-retention.test.ts`` (v1.1.0): the Anthropic block (its two
credential-gated cases are not ported; the OpenAI blocks are ported with the OpenAI adapters)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.compat import getModel
from misaka.ai.types import Model, UserMessage
from misaka.ai.utils.transcript import normalizeContext


class PayloadCaptured(Exception):
    pass


@pytest.fixture(autouse=True)
def _noCacheRetentionEnv(monkeypatch):
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)


context = normalizeContext(
    {
        "systemPrompt": "You are a helpful assistant.",
        "messages": [UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)],
    }
)


async def _capture(model: Model, options: dict[str, Any]) -> Any:
    captured: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured()

    s = streamAnthropic(model, context, {"apiKey": "fake-key", **options, "onPayload": onPayload})
    async for event in s:
        if event.type == "error":
            break
    assert captured, "Expected payload capture"
    return captured[0]


def _proxy(model: Model, **update: Any) -> Model:
    return type(model).model_validate(
        {**model.model_dump(exclude_none=True), "baseUrl": "https://my-proxy.example.com/v1", **update}
    )


async def test_should_add_ttl_for_non_api_anthropic_com_base_url_by_default(monkeypatch):
    monkeypatch.setenv("PI_CACHE_RETENTION", "long")
    payload = await _capture(_proxy(getModel("anthropic", "claude-haiku-4-5")), {})

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_should_omit_ttl_when_supports_long_cache_retention_is_false():
    model = _proxy(getModel("anthropic", "claude-haiku-4-5"), compat={"supportsLongCacheRetention": False})
    payload = await _capture(model, {"cacheRetention": "long"})

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_should_omit_cache_control_when_cache_retention_is_none():
    payload = await _capture(getModel("anthropic", "claude-haiku-4-5"), {"cacheRetention": "none"})

    assert "cache_control" not in payload["system"][0]


async def test_should_add_cache_control_to_string_user_messages():
    payload = await _capture(getModel("anthropic", "claude-haiku-4-5"), {})

    lastMessage = payload["messages"][-1]
    assert isinstance(lastMessage["content"], list)
    assert lastMessage["content"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_should_set_1h_cache_ttl_when_cache_retention_is_long():
    payload = await _capture(getModel("anthropic", "claude-haiku-4-5"), {"cacheRetention": "long"})

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
