"""A stand-in for the ``openai`` package, as pi's tests ``vi.mock("openai")``.

``_openai_sdk._sdk_client`` (``new OpenAI(...)`` / ``new AzureOpenAI(...)``) records the options it was built with, and
``_openai_sdk._create_with_response`` (``client.<resource>.create(params, requestOptions).withResponse()``) records the
request and answers with ``chunks`` (``defaultChunks`` when unset). ``create`` replaces that answer: it is called with
``(resource, params, requestOptions)`` and returns the chunks, or raises.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from misaka.ai.api import _openai_sdk


class FakeOpenAIClient:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    async def close(self) -> None:
        return None


@dataclass
class FakeOpenAI:
    defaultChunks: list[Any] = field(default_factory=list)
    constructorCalls: list[dict[str, Any]] = field(default_factory=list)
    lastClientOptions: dict[str, Any] | None = None
    lastParams: Any = None
    lastResource: str | None = None
    lastRequestOptions: Any = None
    requests: list[Any] = field(default_factory=list)
    chunks: list[Any] | None = None
    create: Callable[..., Any] | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def installFakeOpenAI(monkeypatch: Any, defaultChunks: list[Any] | None = None) -> FakeOpenAI:
    fake = FakeOpenAI(defaultChunks=list(defaultChunks or []))

    def sdkClient(**options: Any) -> tuple[Any, dict[str, Any]]:
        fake.constructorCalls.append(options)
        fake.lastClientOptions = options
        return FakeOpenAIClient(options), {}

    async def createWithResponse(
        _client: Any, resource: str, params: Any, requestOptions: Any, _requestHeaders: Any
    ) -> _openai_sdk._WithResponse:
        fake.lastParams = params
        fake.lastResource = resource
        fake.lastRequestOptions = requestOptions
        fake.requests.append(params)
        if fake.create is not None:
            chunks = fake.create(resource, params, requestOptions)
            if inspect.isawaitable(chunks):
                chunks = await chunks
        else:
            chunks = fake.chunks if fake.chunks is not None else fake.defaultChunks

        async def data():
            for chunk in chunks:
                yield chunk

        return _openai_sdk._WithResponse(data(), httpx.Response(fake.status, headers=fake.headers))

    monkeypatch.setattr(_openai_sdk, "_sdk_client", sdkClient)
    monkeypatch.setattr(_openai_sdk, "_create_with_response", createWithResponse)
    return fake
