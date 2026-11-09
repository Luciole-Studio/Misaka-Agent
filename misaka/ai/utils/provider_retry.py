"""Translated from pi's ``packages/ai/src/utils/provider-retry.ts`` (v1.1.0).

MISAKA fork: Python SDK errors are not pi's ``ProviderError`` (``status`` plus ``headers``): openai and anthropic keep
``status_code`` and the headers on ``response``, transport failures carry only the request. ``_isProviderError`` and
``_header`` read those shapes through ``misaka.ai.utils._sdk_error``. ``Number.parseFloat`` and ``Date.parse`` are
stood in for by ``_parse_float`` and ``email.utils``.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict

import httpx

from misaka.ai.utils._sdk_error import provider_error_headers, provider_error_status
from misaka.utils.js import DOMException, addAbortListener
from misaka.utils.values import signal_aborted

_DEFAULT_MAX_RETRY_DELAY_MS = 60_000


class _ProviderRetryOptions(TypedDict, total=False):
    maxRetries: int | None
    maxRetryDelayMs: float | None
    signal: Any
    # HTTP statuses that fail at once although the default policy would retry them.
    noRetryStatuses: list[int] | tuple[int, ...] | None


def _header(error: Any, name: str) -> str | None:
    """MISAKA fork: ``error.headers?.get(name)`` on the headers a Python SDK error carries."""
    headers = provider_error_headers(error)
    getter = getattr(headers, "get", None) if headers is not None else None
    if getter is None:
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


def _isProviderError(error: Any) -> bool:
    """MISAKA fork: upstream tests for ``"status" in error && "headers" in error``, the shape its TypeScript clients
    raise. openai and anthropic set neither: their ``APIStatusError`` carries ``status_code`` and keeps the headers on
    ``response``; a transport failure (``APIConnectionError``, ``httpx.ConnectError``) carries only the request."""
    if not isinstance(error, Exception):
        return False
    if hasattr(error, "status") and hasattr(error, "headers"):
        return True
    if provider_error_status(error) is not None:
        return True
    if provider_error_headers(error) is not None:
        return True
    if isinstance(error, httpx.TransportError):
        return True
    return getattr(error, "request", None) is not None


def _isRetryableProviderError(error: Any) -> bool:
    """Mirrors the pinned OpenAI/Anthropic SDK retry policy; review when either SDK is upgraded."""
    shouldRetry = _header(error, "x-should-retry")
    if shouldRetry == "true":
        return True
    if shouldRetry == "false":
        return False

    status = provider_error_status(error)
    if status is None:
        return True
    return status == 408 or status == 409 or status == 429 or status >= 500


_JS_FLOAT_PREFIX = re.compile(r"\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))")


def _parse_float(text: str) -> float:
    """MISAKA fork: ``Number.parseFloat``: the leading number, ``nan`` when there is none."""
    match = _JS_FLOAT_PREFIX.match(text)
    return float(match.group(1).replace("Infinity", "inf")) if match else float("nan")


def _now_ms() -> float:
    """MISAKA fork: ``Date.now()``; tests patch it to pin "now" for the HTTP-date form of ``retry-after``."""
    return time.time() * 1000


def _validateServerRetryDelayMs(delayMs: float, maxRetryDelayMs: float | None, providerErrorMessage: str) -> float:
    maxDelayMs = _DEFAULT_MAX_RETRY_DELAY_MS if maxRetryDelayMs is None else maxRetryDelayMs
    if maxDelayMs > 0 and delayMs > maxDelayMs:
        raise RuntimeError(
            f"Server requested {math.ceil(delayMs / 1000)}s retry delay (max: {math.ceil(maxDelayMs / 1000)}s). {providerErrorMessage}"
        )
    return delayMs


def _getRetryDelayMs(error: Any, retryIndex: int, maxRetryDelayMs: float | None) -> float:
    retryAfterMs = _header(error, "retry-after-ms")
    if retryAfterMs:
        value = _parse_float(retryAfterMs)
        if math.isfinite(value):
            return _validateServerRetryDelayMs(value, maxRetryDelayMs, str(error))

    retryAfter = _header(error, "retry-after")
    if retryAfter:
        seconds = _parse_float(retryAfter)
        if math.isnan(seconds):
            try:
                when = parsedate_to_datetime(retryAfter)
            except (TypeError, ValueError):
                when = None
            delayMs = float("nan") if when is None else when.timestamp() * 1000 - _now_ms()
        else:
            delayMs = seconds * 1000
        if math.isfinite(delayMs):
            return _validateServerRetryDelayMs(delayMs, maxRetryDelayMs, str(error))

    exponentialDelay = min(0.5 * 2**retryIndex, 8) * 1000
    return exponentialDelay * (1 - random.random() * 0.25)


def _createAbortError() -> Exception:
    error = DOMException("Request aborted", "AbortError")
    return error


async def _abortableSleep(ms: float, signal: Any = None) -> None:
    if signal_aborted(signal):
        raise _createAbortError()
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def onAbort() -> None:
        timeout.cancel()
        if not done.done():
            done.set_exception(_createAbortError())

    def finish() -> None:
        remove()
        if not done.done():
            done.set_result(None)

    timeout = loop.call_later(max(0.0, ms) / 1000, finish)
    remove = addAbortListener(signal, onAbort) if signal is not None else (lambda: None)
    try:
        await done
    finally:
        timeout.cancel()
        remove()


async def retryProviderRequest[T](
    request: Callable[[], Awaitable[T]], options: _ProviderRetryOptions | None = None
) -> T:
    """Reproduce the retry behavior used by the OpenAI and Anthropic SDKs while making
    their backoff sleep interruptible. Their built-in retry timers ignore the
    request AbortSignal, so callers must invoke the SDK with `maxRetries: 0` and
    wrap the request with this helper. Provider-requested delays above
    `maxRetryDelayMs` fail immediately (60 seconds by default); set it to zero to
    disable the limit.
    """
    options = options if options is not None else {}
    maxRetries = options.get("maxRetries") or 0
    retriesRemaining = maxRetries
    signal = options.get("signal")

    while True:
        try:
            # Each retry is a fresh SDK request, so X-Stainless-Retry-Count remains zero.
            return await request()
        except Exception as error:
            if signal_aborted(signal):
                raise _createAbortError() from error
            if retriesRemaining <= 0 or not _isProviderError(error) or not _isRetryableProviderError(error):
                raise
            status = provider_error_status(error)
            if status is not None and status in (options.get("noRetryStatuses") or ()):
                raise

            retryIndex = maxRetries - retriesRemaining
            retriesRemaining -= 1
            await _abortableSleep(_getRetryDelayMs(error, retryIndex, options.get("maxRetryDelayMs")), signal)


__all__ = ["retryProviderRequest"]
