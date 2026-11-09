"""Gemini and Vertex retry a transient failure the way every other provider does.

The SDK makes a single attempt unless told otherwise, so the outer ``retryGoogleRequest``
owns retrying -- but the stream call was once never wrapped in it, so a 503 failed the turn
at once and ``retry.provider.maxRetries`` was ignored for Google.
"""
import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors

from misaka.ai.api import google_generative_ai, google_vertex
from misaka.ai.compat import getModel
from misaka.ai.types import Context
from misaka.utils.js import AbortController

PROVIDERS = [(google_generative_ai.stream, "google"), (google_vertex.stream, "google-vertex")]


def _chunks():
    async def stream():
        yield SimpleNamespace(response_id="r1", candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="hello", thought=None, function_call=None)]),
            finish_reason="STOP")], usage_metadata=None)
    return stream()


class _Models:
    def __init__(self, failures, hang=False):
        self.failures, self.hang, self.calls = failures, hang, 0

    async def generate_content_stream(self, **_params):
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.calls <= self.failures:
            raise errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        return _chunks()


def _useClient(monkeypatch, models) -> None:
    """The SDK client the adapters build: both modules' ``GoogleGenAI`` answers one serving ``models``."""
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    monkeypatch.setattr(google_generative_ai, "GoogleGenAI", lambda **_kwargs: client)
    monkeypatch.setattr(google_vertex, "GoogleGenAI", lambda **_kwargs: client)


@pytest.mark.parametrize("stream,provider", PROVIDERS)
async def test_a_transient_failure_is_retried_up_to_max_retries(stream, provider, monkeypatch):
    models = _Models(failures=2)
    _useClient(monkeypatch, models)
    options = {"apiKey": "test-key", "maxRetries": 2, "maxRetryDelayMs": 1}
    result = await stream(getModel(provider, "gemini-2.5-flash"), Context(messages=[]), options).result()
    assert result.stopReason == "stop", result.errorMessage
    assert models.calls == 3


@pytest.mark.parametrize("stream,provider", PROVIDERS)
async def test_without_max_retries_the_first_failure_ends_the_turn(stream, provider, monkeypatch):
    models = _Models(failures=1)
    _useClient(monkeypatch, models)
    result = await stream(getModel(provider, "gemini-2.5-flash"), Context(messages=[]),
                          {"apiKey": "test-key"}).result()
    assert result.stopReason == "error"
    assert models.calls == 1


@pytest.mark.parametrize("stream,provider", PROVIDERS)
async def test_abort_while_connecting_ends_the_turn_as_aborted(stream, provider, monkeypatch):
    controller = AbortController()
    _useClient(monkeypatch, _Models(failures=0, hang=True))
    options = {"apiKey": "test-key", "signal": controller.signal}
    pending = stream(getModel(provider, "gemini-2.5-flash"), Context(messages=[]), options).result()
    await asyncio.sleep(0.05)
    controller.abort()
    result = await asyncio.wait_for(pending, 5)
    assert result.stopReason == "aborted"
