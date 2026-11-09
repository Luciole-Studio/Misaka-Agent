"""MISAKA fork: the Python OpenAI SDK glue behind the OpenAI-shaped adapters (no upstream file).

pi builds ``new OpenAI({apiKey, baseURL, defaultHeaders})`` (``new AzureOpenAI`` for Azure), calls
``client.<resource>.create(params, requestOptions).withResponse()`` and walks the SDK stream with ``for await``. The
Python SDK differs in ways handled here, so the adapters themselves read like the upstream files:

- A ``None`` header is pi's "do not send this header"; the SDK takes ``omit`` for that, and only per request.
- ``create()`` accepts only the fields it declares, so the rest travel in ``extra_body``.
- ``timeout`` is in seconds, and httpx has no ``AbortSignal``: the request and every stream read race the signal.
- The stream yields SDK models; the adapters read the parsed JSON objects pi's events are.

The SDK is an optional extra: it is imported here, and only an adapter call loads this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import operator
from collections.abc import AsyncIterator, Mapping
from typing import Any, NamedTuple

try:
    from openai import AsyncAzureOpenAI, AsyncOpenAI, omit
except ImportError:  # optional extra: misaka[openai]
    AsyncAzureOpenAI = AsyncOpenAI = omit = None

from misaka.ai.api._common import (
    _await_maybe_with_signal,
    _close_stream,
    _iterate_async_iterable,
    split_sdk_headers,
    split_sdk_params,
)
from misaka.ai.api._sdk import require
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError


class _WithResponse(NamedTuple):
    """What ``withResponse()`` resolves to: the parsed stream and the HTTP response it arrives on."""

    data: Any
    response: Any


def _sdk_client(
    *, apiKey: str, baseURL: str, defaultHeaders: Mapping[str, Any], apiVersion: str | None = None
) -> tuple[Any, dict[str, Any]]:
    """``new OpenAI({apiKey, baseURL, defaultHeaders})``, or ``new AzureOpenAI`` when an ``apiVersion`` is given, and
    the per-request headers that carry every suppression (``split_sdk_headers``)."""
    default_headers, request_headers = split_sdk_headers(defaultHeaders, omit)
    if apiVersion is None:
        client = require(AsyncOpenAI, "openai")(api_key=apiKey, base_url=baseURL, default_headers=default_headers)
    else:
        client = require(AsyncAzureOpenAI, "openai")(
            api_key=apiKey, api_version=apiVersion, default_headers=default_headers, base_url=baseURL
        )
    return client, request_headers


async def _create_with_response(
    client: Any,
    resource: str,
    params: Mapping[str, Any],
    requestOptions: Mapping[str, Any],
    requestHeaders: Mapping[str, Any],
) -> _WithResponse:
    """``client.<resource>.create(params, requestOptions).withResponse()``, ``resource`` being ``"responses"`` or
    ``"chat.completions"``.

    ``requestOptions`` is upstream's ``{signal?, timeout?, maxRetries: 0}``, with ``timeout`` in milliseconds.
    """
    signal = requestOptions.get("signal")
    sdk_options: dict[str, Any] = {"max_retries": requestOptions.get("maxRetries", 0)}
    if requestOptions.get("timeout") is not None:
        sdk_options["timeout"] = requestOptions["timeout"] / 1000
    endpoint = operator.attrgetter(resource)(client.with_options(**sdk_options))
    call_options: dict[str, Any] = {"extra_headers": dict(requestHeaders)} if requestHeaders else {}
    raw = await _await_maybe_with_signal(
        endpoint.with_raw_response.create(**split_sdk_params(endpoint.create, params), **call_options), signal
    )
    return _WithResponse(await _await_maybe_with_signal(raw.parse(), signal), raw.http_response)


async def _iterate_stream(stream_obj: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    """``for await (const event of openaiStream)``: each event as the parsed JSON object.

    One abort task for the whole stream (``_iterate_async_iterable``), not one per SSE item: a task per item cost
    several loop turns per event, which a busy TUI turned into a stream that fell minutes behind the provider (GitHub
    issue #6). Closed with this generator, so its abort task goes with it (a turn that ended in an error left a pending
    ``signal.wait()`` behind until the cyclic GC destroyed it). After each event the loop turns once, as a JavaScript
    ``for await`` hands control to the consumer before the next item is read.
    """
    async with contextlib.aclosing(
        _iterate_async_iterable(stream_obj, signal, on_abort=lambda: _close_stream(stream_obj))
    ) as items:
        async for event in items:
            if hasattr(event, "model_dump"):
                # `exclude_unset`: the keys the provider sent, as pi's parsed event has them, not
                # every optional field the SDK model declares as None.
                dumped = event.model_dump(exclude_unset=True)
                if not isinstance(dumped, dict):
                    continue
                yield dumped
            elif isinstance(event, dict):
                yield event
            else:
                yield json.loads(json.dumps(event, default=lambda value: value.__dict__))
            await asyncio.sleep(0)


async def _close_client(client: Any) -> None:
    """Close the pool the request's client opened while its event loop still runs. Left to the garbage collector,
    httpx closes it from its finaliser after a one-shot loop is gone, and asyncio prints "Event loop is closed"."""
    if client is None:
        return
    try:
        await client.close()
    except Exception:  # noqa: BLE001, S110 - closing is best-effort; the stream has already ended
        pass


def _format_completion_error(error: Any) -> str:
    """``formatProviderError(normalizeProviderError(error))``, naming the transport failure behind a connection error.

    The SDK's "Connection error." says nothing of what failed; the transport exception it was raised from does (a read
    timeout, a reset, a peer that closed mid-body). The innermost one is named so a report can tell them apart (GitHub
    issue #6). The upstream text stays first, which is what the retry classifier matches on. An h11 refusal quotes the
    request it refused, an API key with a trailing space or line break included, so it is only described (0.18.9).
    """
    message = formatProviderError(normalizeProviderError(error))
    if not isinstance(error, BaseException):
        return message
    cause = error.__cause__
    for _ in range(8):  # a chain is a handful deep; the bound only guards `raise e from e`
        if cause is None or cause.__cause__ is None:
            break
        cause = cause.__cause__
    if cause is None:
        return message
    name = type(cause).__name__
    if name == "LocalProtocolError":
        return f"{message} ({name}: the request could not be sent -- does the API key end in a space or a line break?)"
    from misaka.utils.redact import redact

    detail = redact(str(cause)).strip()
    said = f"({name}: {detail})" if detail and detail != message.strip() else f"({name})"
    return f"{message} {said}" if message.strip() else said


__all__: list[str] = []
