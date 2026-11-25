"""Translated from pi's ``packages/ai/src/utils/provider-retry.ts`` (v1.1.0).

MISAKA fork: Python SDK errors are not pi's ``ProviderError`` (``status`` plus ``headers``): openai and anthropic keep
``status_code`` and the headers on ``response``, transport failures carry only the request. ``_isProviderError`` and
``_header`` read those shapes through ``misaka.ai.utils._sdk_error``. ``Number.parseFloat`` and ``Date.parse`` are
stood in for by ``_parse_float`` and ``email.utils``.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict

import httpx

from misaka.ai.runtime_observation import observe
from misaka.ai.utils._sdk_error import provider_error_headers, provider_error_status
from misaka.ai.utils.retry import (
    _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN,
    _RETRYABLE_PROVIDER_ERROR_PATTERN,
    fullJitterMs,
    retryDelayLimitMessage,
)
from misaka.utils.js import DOMException, addAbortListener
from misaka.utils.values import signal_aborted

_DEFAULT_MAX_RETRY_DELAY_MS = 60_000
_LOCAL_REQUEST_ERRORS = (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)

try:
    import httpx2
except ImportError:  # optional transport of the OpenAI SDK
    pass
else:
    _LOCAL_REQUEST_ERRORS += (httpx2.InvalidURL, httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)


class _ProviderRetryOptions(TypedDict, total=False):
    maxRetries: int | None
    maxRetryDelayMs: float | None
    signal: Any
    # HTTP statuses that fail at once although the default policy would retry them.
    noRetryStatuses: list[int] | tuple[int, ...] | None


class ProviderResponseError(RuntimeError):
    """Failure from a provider adapter that reads HTTP responses or stream events directly."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
        code: str | None = None,
        providerMessage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers
        self.code = code
        self.providerMessage = providerMessage if providerMessage is not None else message


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


_JS_FLOAT_PREFIX = re.compile(r"\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))")


def _parse_float(text: str) -> float:
    """MISAKA fork: ``Number.parseFloat``: the leading number, ``nan`` when there is none."""
    match = _JS_FLOAT_PREFIX.match(text)
    return float(match.group(1).replace("Infinity", "inf")) if match else float("nan")


def _now_ms() -> float:
    """MISAKA fork: ``Date.now()``; tests patch it to pin "now" for the HTTP-date form of ``retry-after``."""
    return time.time() * 1000


class RetryDelayLimitError(RuntimeError):
    def __init__(self, delayMs: float, maxDelayMs: float, providerMessage: str) -> None:
        super().__init__(retryDelayLimitMessage(delayMs, maxDelayMs, providerMessage))
        self.delayMs = delayMs
        self.maxDelayMs = maxDelayMs


def providerRetryDelayCap(providerCap: float | None, sessionCap: float | None = None) -> float:
    providerCap = _DEFAULT_MAX_RETRY_DELAY_MS if providerCap is None else providerCap
    if sessionCap is not None and sessionCap > 0:
        return min(providerCap, sessionCap) if providerCap > 0 else sessionCap
    return max(0, providerCap)


def _validateServerRetryDelayMs(delayMs: float, maxDelayMs: float, error: Exception) -> None:
    if 0 < maxDelayMs < delayMs:
        observe("provider_retry_stopped", reason="retry_delay_limit", retry_source="server", delay_ms=delayMs,
                status_code=provider_error_status(error))
        raise RetryDelayLimitError(delayMs, maxDelayMs, str(error)) from error


def providerRetryAfterMs(error: Exception) -> float | None:
    retryAfterMs = _header(error, "retry-after-ms")
    if retryAfterMs:
        value = _parse_float(retryAfterMs)
        if math.isfinite(value) and value >= 0:
            return value

    retryAfter = _header(error, "retry-after")
    if retryAfter:
        seconds = _parse_float(retryAfter)
        if math.isnan(seconds):
            try:
                when = parsedate_to_datetime(retryAfter)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0, when.timestamp() * 1000 - _now_ms())
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        else:
            delayMs = seconds * 1000
        if math.isfinite(delayMs) and delayMs >= 0:
            return delayMs
    return None


def classifyProviderFailure(error: BaseException | None) -> tuple[str | None, bool]:
    """Return the retry disposition and independently whether the failure counts toward the circuit."""
    retryStop = None
    if isinstance(error, RetryDelayLimitError):
        retryStop = "retry_delay_limit"
        error = error.__cause__

    if isinstance(error, _LOCAL_REQUEST_ERRORS) or isinstance(getattr(error, "__cause__", None), _LOCAL_REQUEST_ERRORS):
        return retryStop or "provider_terminal", False
    if not _isProviderError(error):
        return retryStop, False

    status = provider_error_status(error)
    code = getattr(error, "code", None)
    code = code if isinstance(code, str) else ""
    description = error.providerMessage if isinstance(error, ProviderResponseError) else str(error)
    if status in (400, 413) or _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(code):
        return retryStop or "provider_terminal", False
    if status in (None, 429) and _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(description):
        return retryStop or "provider_terminal", False

    if isinstance(error, ProviderResponseError) and status is None:
        transient = bool(
            _RETRYABLE_PROVIDER_ERROR_PATTERN.search(code) or _RETRYABLE_PROVIDER_ERROR_PATTERN.search(description)
        )
    else:
        transient = status is None or status in (408, 409, 429) or 500 <= status <= 599
    hint = _header(error, "x-should-retry")
    retryable = hint == "true" or (hint != "false" and transient)
    return retryStop or ("transient" if retryable else "provider_terminal"), transient


def providerRetryDisposition(error: BaseException | None) -> str | None:
    return classifyProviderFailure(error)[0]


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
    maxDelayMs = providerRetryDelayCap(options.get("maxRetryDelayMs"))

    while True:
        if signal_aborted(signal):
            raise _createAbortError()
        try:
            # Each retry is a fresh SDK request, so X-Stainless-Retry-Count remains zero.
            return await request()
        except Exception as error:
            if signal_aborted(signal):
                raise _createAbortError() from error
            if providerRetryDisposition(error) != "transient":
                raise
            status = provider_error_status(error)
            if status is not None and status in (options.get("noRetryStatuses") or ()):
                raise

            delay = providerRetryAfterMs(error)
            if delay is not None:
                _validateServerRetryDelayMs(delay, maxDelayMs, error)
            if retriesRemaining <= 0:
                raise
            retryIndex = maxRetries - retriesRemaining
            retriesRemaining -= 1
            source = "server" if delay is not None else "jitter"
            if delay is None:
                ceiling = 500 * 2 ** min(retryIndex, 4)
                delay = fullJitterMs(min(ceiling, maxDelayMs) if maxDelayMs > 0 else ceiling)
            observe("provider_retry", retry_source=source, delay_ms=delay, retry_number=retryIndex + 1, status_code=status)
            await _abortableSleep(delay, signal)


__all__ = ["retryProviderRequest"]
