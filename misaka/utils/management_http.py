"""Bounded retry for idempotent management HTTP requests.

Translated from pi's ``coding-agent/src/utils/management-http.ts``. ``fetch`` is the WHATWG ``fetch`` port
(``misaka.utils.whatwg_fetch``), read from this module at call time so tests replace it the way upstream's tests spy
on ``globalThis.fetch``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from misaka.utils.js import AbortSignal, DOMException, isAborted, throwIfAborted
from misaka.utils.whatwg_fetch import RequestInit, Response, fetch
from misaka.utils.whatwg_url import URL

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class FetchRetryOptions:
    # Number of additional attempts after the initial request. Defaults to two.
    maxRetries: float | None = None
    # Retry transient HTTP responses as well as transport failures. Defaults to true.
    retryOnStatus: bool | None = None
    # Overall time budget shared by all attempts.
    timeoutMs: float | None = None
    # Per-attempt timeout. A new timeout is created for every attempt.
    attemptTimeoutMs: float | None = None


async def fetchWithRetry(
    input: str | URL,
    init: RequestInit | None = None,
    options: FetchRetryOptions | None = None,
) -> Response:
    """Fetch a management HTTP resource with a bounded immediate retry.

    This is intentionally a transport-level helper for idempotent management
    requests (version checks, catalogs, and downloads). It must not be used for
    agent/model operations: those can fail after the HTTP request starts and are
    retried by their semantic caller instead.

    Caller cancellation and timeoutMs are terminal. attemptTimeoutMs aborts
    only the current attempt so a hung connection can be retried.
    """
    options = options if options is not None else FetchRetryOptions()
    maxRetries = (
        2
        if options.maxRetries is None or not math.isfinite(options.maxRetries)
        else max(0, math.floor(options.maxRetries))
    )
    retryOnStatus = options.retryOnStatus if options.retryOnStatus is not None else True
    parentSignal: Any = init.get("signal") if init is not None else None
    timeoutSignal = (
        AbortSignal.timeout(options.timeoutMs) if options.timeoutMs is not None and options.timeoutMs > 0 else None
    )
    attemptTimeoutMs = (
        options.attemptTimeoutMs if options.attemptTimeoutMs is not None and options.attemptTimeoutMs > 0 else None
    )

    attempt = 0
    while True:
        if parentSignal is not None:
            throwIfAborted(parentSignal)
        if timeoutSignal is not None:
            timeoutSignal.throwIfAborted()
        attemptTimeoutSignal = AbortSignal.timeout(attemptTimeoutMs) if attemptTimeoutMs else None
        signals = [signal for signal in (parentSignal, timeoutSignal, attemptTimeoutSignal) if signal is not None]
        signal = AbortSignal.any(signals) if len(signals) > 1 else (signals[0] if signals else None)

        try:
            response = await fetch(input, {**(init or {}), "signal": signal} if signal else init)
            shouldRetry = retryOnStatus and response.status in RETRYABLE_STATUS_CODES and attempt < maxRetries
            if not shouldRetry:
                return response
            try:
                if response.body is not None:
                    await response.body.cancel()
            except Exception:  # noqa: BLE001, S110
                # The response is being discarded before a retry. There is nothing useful to
                # do if cancelling its body also fails.
                pass
        except Exception as error:
            attemptTimedOut = (
                attemptTimeoutSignal is not None
                and attemptTimeoutSignal.aborted
                and not isAborted(parentSignal)
                and not isAborted(timeoutSignal)
            )
            if (
                isAborted(parentSignal)
                or isAborted(timeoutSignal)
                or (
                    isinstance(error, DOMException)
                    and error.name == "AbortError"
                    and not attemptTimedOut
                    and timeoutSignal is None
                )
                or attempt >= maxRetries
            ):
                raise
        attempt += 1


__all__ = ["RETRYABLE_STATUS_CODES", "FetchRetryOptions", "RequestInit", "fetch", "fetchWithRetry"]
