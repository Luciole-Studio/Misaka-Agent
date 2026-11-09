"""Shared HTTP proxy and idle-timeout policy.

Python has no safe equivalent of Undici's process-wide dispatcher. Keep the
portable parsing/environment policy here; the core SDK passes the timeout to
its provider clients, while third-party clients discover proxies through their
normal environment handling.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import MutableMapping
from typing import Any

import httpx

from misaka.utils.js import jsTrim

DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300_000

HTTP_IDLE_TIMEOUT_CHOICES = [
    {"label": "30 sec", "timeoutMs": 30_000},
    {"label": "1 min", "timeoutMs": 60_000},
    {"label": "2 min", "timeoutMs": 120_000},
    {"label": "5 min", "timeoutMs": 300_000},
    {"label": "disabled", "timeoutMs": 0},
]

_DECIMAL_NUMBER = re.compile(
    r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?$"
)
_RADIX_NUMBER = re.compile(r"^0(?P<prefix>[bBoOxX])(?P<digits>[0-9A-Fa-f]+)$")


def parseHttpIdleTimeoutMs(value: Any) -> int | None:
    if isinstance(value, str):
        trimmed = jsTrim(value)
        if trimmed.lower() == "disabled":
            return 0
        if not trimmed:
            return None
        try:
            radix_match = _RADIX_NUMBER.fullmatch(trimmed)
            if radix_match is not None:
                prefix = radix_match.group("prefix").lower()
                base = {"b": 2, "o": 8, "x": 16}[prefix]
                numeric = float(int(radix_match.group("digits"), base))
            elif _DECIMAL_NUMBER.fullmatch(trimmed) is not None:
                numeric = float(trimmed)
            else:
                return None
            return parseHttpIdleTimeoutMs(numeric)
        except (OverflowError, ValueError):
            return None

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return math.floor(numeric)


def formatHttpIdleTimeoutMs(timeout_ms: int) -> str:
    for choice in HTTP_IDLE_TIMEOUT_CHOICES:
        if choice["timeoutMs"] == timeout_ms:
            return str(choice["label"])
    seconds = timeout_ms / 1000
    return f"{int(seconds) if seconds.is_integer() and abs(seconds) < 1e21 else seconds} sec"


def applyHttpProxySettings(
    http_proxy: str | None,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    if http_proxy is None:
        return
    if not isinstance(http_proxy, str):
        raise TypeError(f"Invalid httpProxy setting: {http_proxy}")
    proxy = jsTrim(http_proxy)
    if not proxy:
        return
    target = os.environ if environ is None else environ
    target.setdefault("HTTP_PROXY", proxy)
    target.setdefault("HTTPS_PROXY", proxy)


def createHttpxIdleTimeout(
    timeout_ms: Any = None,
) -> httpx.Timeout:
    """Translate header/body idle policy without imposing a total request limit.

    Contract note (audit 2026-09-02, core-config-02): ``0`` here means *zero-second read
    idle*, i.e. an immediate ``ReadTimeout`` -- not undici's "0 disables the timeout", which
    is what `HTTP_IDLE_TIMEOUT_CHOICES`' ``disabled`` entry and `parseHttpIdleTimeoutMs`
    ("disabled" -> 0) speak. Callers that accept the settings value must map the disabled
    choice themselves before calling in; `sdk.py` does (`effective_timeout_ms`).
    """
    if timeout_ms is None:
        timeout_ms = DEFAULT_HTTP_IDLE_TIMEOUT_MS
    normalized_timeout_ms = parseHttpIdleTimeoutMs(timeout_ms)
    if normalized_timeout_ms is None:
        raise ValueError(f"Invalid HTTP idle timeout: {timeout_ms}")
    return httpx.Timeout(None, read=normalized_timeout_ms / 1000)


# MISAKA fork: undici's global dispatcher is this module's idle timeout, which every `fetch` reads when it
# opens its connection (`whatwg_fetch.createClient`, the OAuth flows' `fetch`).
_dispatcherIdleTimeoutMs: int = DEFAULT_HTTP_IDLE_TIMEOUT_MS


def configureHttpDispatcher(timeoutMs: Any = DEFAULT_HTTP_IDLE_TIMEOUT_MS) -> None:
    global _dispatcherIdleTimeoutMs
    normalizedTimeoutMs = parseHttpIdleTimeoutMs(timeoutMs)
    if normalizedTimeoutMs is None:
        raise ValueError(f"Invalid HTTP idle timeout: {timeoutMs}")
    _dispatcherIdleTimeoutMs = normalizedTimeoutMs


def dispatcherIdleTimeout() -> float | None:
    """Seconds the configured dispatcher waits for headers or the next body chunk; None when disabled (undici's
    ``0``)."""
    return None if _dispatcherIdleTimeoutMs == 0 else _dispatcherIdleTimeoutMs / 1000


__all__ = [
    "DEFAULT_HTTP_IDLE_TIMEOUT_MS",
    "HTTP_IDLE_TIMEOUT_CHOICES",
    "applyHttpProxySettings",
    "configureHttpDispatcher",
    "createHttpxIdleTimeout",
    "dispatcherIdleTimeout",
    "formatHttpIdleTimeoutMs",
    "parseHttpIdleTimeoutMs",
]
