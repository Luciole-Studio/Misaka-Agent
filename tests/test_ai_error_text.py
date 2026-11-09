"""A request that fails with an exception carrying no message still says what failed (R8).

pi reads ``error.message``; in Python many timeouts have none -- ``asyncio.wait_for``'s
``TimeoutError``, httpx's ``ReadTimeout`` -- and ``str(error)`` is empty. A card's turn ended
after six and a half minutes with ``errorMessage == ""`` (2026-10-25): the HTTP idle timeout.
"""
import httpx
import pytest

from misaka.ai.api import anthropic_messages
from tests.test_ai_anthropic_strict_tool_fallback import Harness, context, tool


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("")])
async def test_a_message_less_failure_is_named(monkeypatch, error):
    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(anthropic_messages, "_createClient", failing)
    with Harness(lambda _payload, _attempt: None) as h:
        result = await anthropic_messages.stream(h.model, context([tool("lookup")]), h.options).result()
    assert result.stopReason == "error"
    assert result.errorMessage == type(error).__name__


async def test_a_failure_that_says_something_is_unchanged(monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(anthropic_messages, "_createClient", failing)
    with Harness(lambda _payload, _attempt: None) as h:
        result = await anthropic_messages.stream(h.model, context([tool("lookup")]), h.options).result()
    assert result.errorMessage == "socket closed"
