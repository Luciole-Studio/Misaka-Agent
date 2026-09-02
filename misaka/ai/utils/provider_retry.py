"""Provider retry policy, translated from pi's ``utils/provider-retry.ts``.

Upstream's note is the reason this exists at all (``provider-retry.ts:97-104``): the
built-in retry timers of the SDKs it wraps ignore the request's ``AbortSignal``, so its
callers pass ``maxRetries: 0`` and wrap the request with this helper instead, which
sleeps interruptibly.

The policy below: ``x-should-retry: true``/``false`` short-circuits, a missing status
retries, and 408/409/429/5xx retry. That 408/409/429/5xx ladder is the one
``_should_retry`` walks in both Python clients' ``_base_client`` (checked against openai
3.3.1 and anthropic 0.125.0; re-read it whenever either is upgraded). A server-requested
delay above ``maxRetryDelayMs`` fails immediately rather than parking the request for
minutes -- sixty seconds by default, zero to allow any delay.

Callers: ``anthropic.py``, ``openai_completions.py``, ``openai_responses.py``,
``azure_openai_responses.py``, ``google_shared.py`` and ``images/openrouter.py`` all set the
SDK's own ``maxRetries`` to zero and wrap the request with ``retry_provider_request``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from misaka.ai.utils.abort import sleep, throw_if_aborted
from misaka.ai.utils.error_body import provider_error_headers, provider_error_status
from misaka.utils.values import signal_aborted

DEFAULT_MAX_RETRY_DELAY_MS = 60_000


def _header(error: Any, name: str) -> str | None:
    headers = provider_error_headers(error)
    getter = getattr(headers, "get", None) if headers is not None else None
    if getter is None:
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


def _is_provider_error(error: Any) -> bool:
    """Whether this came back from a provider at all, as opposed to a local failure.

    Upstream tests for ``"status" in error && "headers" in error`` (``provider-retry.ts:15``)
    -- the shape its TypeScript clients raise. Neither name is set on what openai 3.3.1
    and anthropic 0.125.0 raise: their ``APIStatusError`` carries ``status_code`` and keeps
    the headers on ``response``. The probe is shared with ``utils/error_body`` so both
    modules recognise the same objects.
    """
    if not isinstance(error, Exception):
        return False
    if provider_error_status(error) is not None:
        return True
    if provider_error_headers(error) is not None:
        return True
    # Both probes above miss a transport failure: on openai 3.3.1's `APIConnectionError`
    # and on `httpx.ConnectError` / `httpx.ReadTimeout`, none of `status`, `status_code`,
    # `headers` or `response` is set. What those three do carry is the request they
    # failed on.
    if isinstance(error, httpx.TransportError):
        return True
    return getattr(error, "request", None) is not None


def is_retryable_provider_error(error: Any) -> bool:
    """Mirrors upstream's ``isRetryableProviderError`` (``provider-retry.ts:23-35``)."""
    should_retry = _header(error, "x-should-retry")
    if should_retry == "true":
        return True
    if should_retry == "false":
        return False

    status = provider_error_status(error)
    # A missing status retries, as upstream does (`provider-retry.ts:28`).
    if status is None:
        return True
    return status in (408, 409, 429) or status >= 500


def _validate_server_retry_delay_ms(
    delay_ms: float, max_retry_delay_ms: float | None, provider_error_message: str
) -> float:
    max_delay_ms = DEFAULT_MAX_RETRY_DELAY_MS if max_retry_delay_ms is None else max_retry_delay_ms
    if max_delay_ms > 0 and delay_ms > max_delay_ms:
        raise RuntimeError(
            f"Server requested {math.ceil(delay_ms / 1000)}s retry delay "
            f"(max: {math.ceil(max_delay_ms / 1000)}s). {provider_error_message}"
        )
    return delay_ms


def get_retry_delay_ms(
    error: Any, retry_index: int, max_retry_delay_ms: float | None, now_ms: float | None = None
) -> float:
    """What the server asked for, or exponential backoff with jitter.

    ``now_ms`` is injectable because the HTTP-date form of ``retry-after`` is relative to
    the current time, and a test that cannot pin "now" cannot check that branch.
    """
    message = str(error)

    retry_after_ms = _header(error, "retry-after-ms")
    if retry_after_ms:
        try:
            return _validate_server_retry_delay_ms(float(retry_after_ms), max_retry_delay_ms, message)
        except ValueError:
            pass

    retry_after = _header(error, "retry-after")
    if retry_after:
        try:
            delay_ms = float(retry_after) * 1000
        except ValueError:
            try:
                when = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                when = None
            if when is None:
                delay_ms = float("nan")
            else:
                current = now_ms if now_ms is not None else _now_ms()
                delay_ms = when.timestamp() * 1000 - current
        return _validate_server_retry_delay_ms(delay_ms, max_retry_delay_ms, message)

    # 0.5s doubling, capped at 8s, then shaved by up to a quarter -- upstream's formula
    # (`provider-retry.ts:65-66`).
    exponential_delay = min(0.5 * 2**retry_index, 8) * 1000
    # random() here is jitter, not a secret; ruff's crypto rule is not enabled in this repo.
    return exponential_delay * (1 - random.random() * 0.25)


def _now_ms() -> float:
    import time

    return time.time() * 1000


async def retry_provider_request[T](
    request: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 0,
    max_retry_delay_ms: float | None = None,
    signal: Any = None,
) -> T:
    """Run ``request``, retrying what the policy says is worth retrying."""
    retries_remaining = max_retries

    while True:
        try:
            # Upstream's note here: each retry is a fresh SDK request, so
            # `X-Stainless-Retry-Count` remains zero (`provider-retry.ts:114`).
            return await request()
        except Exception as error:  # the policy below decides what is retryable
            if signal_aborted(signal):
                throw_if_aborted(signal)
            if (
                retries_remaining <= 0
                or not _is_provider_error(error)
                or not is_retryable_provider_error(error)
            ):
                raise
            retry_index = max_retries - retries_remaining
            retries_remaining -= 1
            await sleep(get_retry_delay_ms(error, retry_index, max_retry_delay_ms), signal)


__all__ = [
    "DEFAULT_MAX_RETRY_DELAY_MS",
    "get_retry_delay_ms",
    "is_retryable_provider_error",
    "retry_provider_request",
]
