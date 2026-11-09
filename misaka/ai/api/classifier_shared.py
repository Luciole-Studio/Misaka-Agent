"""Translated from pi's ``packages/ai/src/api/classifier-shared.ts`` (v1.1.0).

MISAKA fork, kept to what the platform forces (each is marked where it happens): httpx stands in for ``fetch`` (no
timeout of its own; a failed connection is fetch's status-less "fetch failed"), ``TimeoutAbortSignal``/
``AnyAbortSignal`` for ``AbortSignal.timeout``/``AbortSignal.any``, and the error interface is an exception class.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import httpx

from misaka.ai.api._common import _option
from misaka.ai.models import calculateCost
from misaka.ai.types import ClassifierModel, ProviderHeaders, Usage, UsageCost
from misaka.ai.utils.headers import headersToRecord, providerHeadersToRecord
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.utils._abort_signal import (
    AnyAbortSignal,
    TimeoutAbortSignal,
    cancel_on_abort,
)
from misaka.utils.js import (
    isNumber,
    jsonParse,
    jsonStringify,
    jsString,
    nullishCoalesce,
)
from misaka.utils.values import maybe_await, signal_aborted


class ClassifierHttpError(Exception):
    """An HTTP failure in the shape `retryProviderRequest` and `normalizeProviderError` understand."""

    # MISAKA fork: upstream's interface over `Error`; `_httpError` and `_timeoutError` set the fields.
    name: str = "Error"
    status: int | None
    headers: Any
    body: str


def _httpError(label: str, response: httpx.Response, body: str) -> ClassifierHttpError:
    error = ClassifierHttpError(f"{label} returned {response.status_code}")
    error.status = response.status_code
    error.headers = response.headers
    error.body = body
    return error


def _timeoutError(timeoutMs: float) -> ClassifierHttpError:
    error = ClassifierHttpError(f"Request timed out after {jsString(timeoutMs)}ms")
    error.name = "TimeoutError"
    error.status = None
    error.headers = None
    error.body = ""
    return error


def isRecord(value: Any) -> bool:
    return isinstance(value, dict)


def requiredNumber(label: str, value: Any, field: str) -> float:
    if not isNumber(value) or not math.isfinite(value):
        raise RuntimeError(f"{label} returned an invalid {field}")
    return value


def _requestHeaders(
    model: ClassifierModel,
    apiKey: str,
    optionsHeaders: ProviderHeaders | None = None,
) -> dict[str, str]:
    return (
        providerHeadersToRecord(
            {"authorization": f"Bearer {apiKey}", "content-type": "application/json"},
            model.headers,
            optionsHeaders,
        )
        or {}
    )


async def postClassifierRequest(
    label: str,
    url: str,
    model: ClassifierModel,
    body: Any,
    options: Any,
    noRetryStatuses: Sequence[int] | None = None,
) -> Any:
    """Posts one JSON classifier request with bearer auth, `onPayload`/`onResponse` hooks, a fresh
    timeout per attempt, and provider retries. Returns the parsed response body; throws on failure.
    `noRetryStatuses` lists HTTP statuses that fail at once although they are normally retried.
    """
    if not _option(options, "apiKey"):
        raise RuntimeError(f"No API key for provider: {model.provider}")
    apiKey = _option(options, "apiKey")
    payload = body
    onPayload = _option(options, "onPayload")
    transformed = await maybe_await(onPayload(payload, model)) if onPayload else None
    if transformed is not None:
        payload = transformed

    async def attempt() -> tuple[httpx.Response, Any]:
        timeoutSignal = (
            TimeoutAbortSignal(_option(options, "timeoutMs")) if _option(options, "timeoutMs") is not None else None
        )
        signal = (
            AnyAbortSignal(_option(options, "signal"), timeoutSignal)
            if _option(options, "signal") and timeoutSignal
            else nullishCoalesce(_option(options, "signal"), timeoutSignal)
        )
        try:
            # MISAKA fork: `options.fetch` is not ported; httpx sends the request, following redirects and with no
            # timeout of its own, as fetch does -- the signals above are the bound.
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(None)) as client:
                try:
                    next = await cancel_on_abort(
                        client.post(
                            url,
                            headers=_requestHeaders(model, apiKey, _option(options, "headers")),
                            content=jsonStringify(payload),
                        ),
                        signal,
                    )
                except httpx.TransportError as error:
                    # MISAKA fork: fetch rejects a failed connection with a status-less "fetch failed", which
                    # retryProviderRequest does not retry.
                    raise RuntimeError("fetch failed") from error
                if not next.is_success:
                    raise _httpError(label, next, next.text)
                return next, jsonParse(next.text)
        except Exception:
            if signal_aborted(timeoutSignal) and not signal_aborted(_option(options, "signal")):
                raise _timeoutError(_option(options, "timeoutMs")) from None
            raise

    # MISAKA fork: the attempt returns `(response, json)` for upstream's `{ response, json }`.
    response, json = await retryProviderRequest(
        attempt,
        {
            "maxRetries": nullishCoalesce(_option(options, "maxRetries"), 2),
            "maxRetryDelayMs": _option(options, "maxRetryDelayMs"),
            "signal": _option(options, "signal"),
            "noRetryStatuses": noRetryStatuses,
        },
    )
    onResponse = _option(options, "onResponse")
    if onResponse:
        await maybe_await(onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model))
    return json


def _tokenCount(value: Any) -> float:
    return value if isNumber(value) and math.isfinite(value) and value > 0 else 0


def parseClassifierUsage(value: Any, model: ClassifierModel) -> Usage | None:
    """Usage from a `{ input_tokens, output_tokens }` object, priced from the model catalog like chat
    usage. A missing or malformed usage object leaves the result without usage instead of failing it.
    """
    if not isRecord(value) or ("input_tokens" not in value and "output_tokens" not in value):
        return None
    input = _tokenCount(value.get("input_tokens"))
    output = _tokenCount(value.get("output_tokens"))
    usage = Usage(
        input=input,
        output=output,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=input + output,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )
    calculateCost(model, usage)
    return usage


__all__ = ["ClassifierHttpError", "isRecord", "parseClassifierUsage", "postClassifierRequest", "requiredNumber"]
