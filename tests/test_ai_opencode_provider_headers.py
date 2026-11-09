"""Translated from pi's ``packages/ai/test/opencode-provider-headers.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.providers.opencode_headers import withOpenCodeSessionHeader
from misaka.ai.types import Context, DoneEvent, Model, StartEvent, UserMessage
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.values import read_field

model = Model.model_validate(
    {
        "id": "test-model",
        "name": "Test model",
        "api": "test-api",
        "provider": "opencode",
        "baseUrl": "https://opencode.ai/zen/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    }
)
context = normalizeContext(Context(messages=[UserMessage(content="hi", timestamp=0)]))


def completedStream() -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    message = fauxAssistantMessage("ok")
    stream.push(StartEvent(type="start", partial=message))
    stream.push(DoneEvent(type="done", reason="stop", message=message))
    stream.end(message)
    return stream


def recordingStreams(capture: Any) -> Any:
    def stream(_model: Model, _context: Any, options: Any = None) -> AssistantMessageEventStream:
        capture(options)
        return completedStream()

    return SimpleNamespace(stream=stream, streamSimple=stream)


# Regression test for https://github.com/earendil-works/pi/issues/9326
@pytest.mark.parametrize("method", ["stream", "streamSimple"])
def test_maps_session_id_for_requests_even_without_cache_retention(method):
    captured: list[Any] = []
    streams = withOpenCodeSessionHeader(recordingStreams(captured.append))

    getattr(streams, method)(model, context, {"sessionId": "conversation-1", "cacheRetention": "none"})

    assert read_field(captured[0], "headers") == {"x-opencode-session": "conversation-1"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-OpenCode-Session": "caller-value"}, {"X-OpenCode-Session": "caller-value"}),
        ({"X-OpenCode-Session": None}, {"X-OpenCode-Session": None}),
    ],
)
def test_preserves_a_case_insensitive_caller_override(headers, expected):
    captured: list[Any] = []
    streams = withOpenCodeSessionHeader(recordingStreams(captured.append))

    streams.streamSimple(model, context, {"sessionId": "generated-value", "headers": headers})

    assert captured[0]["headers"] == expected


def test_does_not_fabricate_a_session_header_when_session_id_is_absent():
    captured: list[Any] = []
    streams = withOpenCodeSessionHeader(recordingStreams(captured.append))

    streams.streamSimple(model, context, {"headers": {"x-custom": "value"}})

    assert read_field(captured[0], "headers") == {"x-custom": "value"}
