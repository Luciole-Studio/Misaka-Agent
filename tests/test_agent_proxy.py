"""Translated from pi's ``packages/agent/test/proxy.test.ts`` (v1.1.0).

MISAKA fork: ``vi.stubGlobal("fetch", ...)`` replaces the proxy module's ``fetch``.
"""

from __future__ import annotations

from typing import Any

from misaka.agent import proxy
from misaka.agent.proxy import streamProxy
from misaka.ai.types import Context, Model
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import jsonStringify
from misaka.utils.whatwg_fetch import Response
from tests.oauth_fetch import stubFetch

model = Model.model_validate(
    {
        "id": "gpt-5.4",
        "name": "GPT-5.4",
        "api": "openai-responses",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "reasoning": True,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 400000,
        "maxTokens": 128000,
    }
)

usage: dict[str, Any] = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def _context():
    return normalizeContext(Context(systemPrompt="", messages=[]))


async def _collect(stream):
    events = [event async for event in stream]
    return events, await stream.result()


async def test_preserves_tool_call_metadata_received_only_on_toolcall_end(monkeypatch):
    proxyEvents = [
        {"type": "start"},
        {"type": "toolcall_start", "contentIndex": 0, "id": "call_test|fc_test", "toolName": "lookup"},
        {"type": "toolcall_delta", "contentIndex": 0, "delta": '{"value":"hello"}'},
        {
            "type": "toolcall_end",
            "contentIndex": 0,
            "toolCall": {
                "type": "toolCall",
                "id": "call_test|fc_test",
                "name": "lookup",
                "arguments": {"value": "hello"},
                "namespace": "dynamic_tools",
            },
        },
        {"type": "done", "reason": "toolUse", "usage": usage},
    ]
    body = "".join(f"data: {jsonStringify(event)}\n\n" for event in proxyEvents)
    stubFetch(monkeypatch, proxy, lambda *_args: Response(body, status=200))

    stream = streamProxy(model, _context(), {"authToken": "test-token", "proxyUrl": "https://proxy.example.com"})
    events, result = await _collect(stream)
    endEvent = next(event for event in events if event.type == "toolcall_end")

    assert endEvent.type == "toolcall_end"
    assert endEvent.toolCall.namespace == "dynamic_tools"
    content = result.content[0]
    assert (content.type, content.arguments, content.namespace) == ("toolCall", {"value": "hello"}, "dynamic_tools")


# Regression tests for https://github.com/earendil-works/pi/issues/8996
async def test_processes_terminal_metadata_when_the_event_is_not_newline_terminated(monkeypatch):
    start = f"data: {jsonStringify({'type': 'start'})}\n\n"
    done = f"data: {jsonStringify({'type': 'done', 'reason': 'stop', 'usage': usage, 'providerThinkingLevel': 'high'})}"
    stubFetch(monkeypatch, proxy, lambda *_args: Response(start + done, status=200))

    stream = streamProxy(model, _context(), {"authToken": "test-token", "proxyUrl": "https://proxy.example.com"})
    events, result = await _collect(stream)

    assert [event.type for event in events] == ["start", "done"]
    assert result.stopReason == "stop"
    assert result.providerThinkingLevel == "high"


async def test_emits_an_error_instead_of_hanging_when_the_stream_ends_without_a_terminal_event(monkeypatch):
    body = f"data: {jsonStringify({'type': 'start'})}\n\n"
    stubFetch(monkeypatch, proxy, lambda *_args: Response(body, status=200))

    stream = streamProxy(model, _context(), {"authToken": "test-token", "proxyUrl": "https://proxy.example.com"})
    events, result = await _collect(stream)

    assert [event.type for event in events] == ["start", "error"]
    assert result.stopReason == "error"
    assert "Connection closed by proxy server" in result.errorMessage
