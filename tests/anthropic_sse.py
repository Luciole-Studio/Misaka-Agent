"""Feed the Anthropic adapter a canned stream the way the API sends it: SSE bytes on an HTTP response."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterable
from typing import Any

import httpx

from misaka.ai.api import _anthropic_sdk


def sseResponse(events: Iterable[dict[str, Any]], status: int = 200) -> httpx.Response:
    """An ``event:``/``data:`` frame per event, named by its ``type``."""
    body = "".join(f"event: {event.get('type')}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(status, content=body.encode(), headers={"content-type": "text/event-stream"})


def patchRawResponse(monkeypatch: Any, events: Iterable[dict[str, Any]] | AsyncIterable[dict[str, Any]]) -> None:
    """Every request the adapter makes answers with ``events``."""

    async def create(*_args: Any, **_kwargs: Any) -> httpx.Response:
        collected = [event async for event in events] if isinstance(events, AsyncIterable) else list(events)
        return sseResponse(collected)

    monkeypatch.setattr(_anthropic_sdk, "_create_raw_response", create)


def createSseResponse(events: Iterable[dict[str, str]]) -> httpx.Response:
    """pi's test helper: ``{event, data}`` frames joined into one SSE body."""
    body = "\n".join(f"event: {event['event']}\ndata: {event['data']}\n" for event in events)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def createFakeAnthropicClient(response: httpx.Response, onCreate: Any = None) -> Any:
    """An injected client (``options.client``) whose request answers with ``response``. MISAKA fork: the adapter
    calls the Python SDK's ``messages.with_raw_response.create(**params)``; undeclared fields arrive in
    ``extra_body`` and are merged back so ``onCreate`` sees the request body."""
    from types import SimpleNamespace

    async def create(**kwargs: Any) -> Any:
        params = {**kwargs.pop("extra_body", {}), **kwargs}
        params.pop("extra_headers", None)
        if onCreate is not None:
            onCreate(params)
        return SimpleNamespace(http_response=response)

    messages = SimpleNamespace(create=create, with_raw_response=SimpleNamespace(create=create))
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))
