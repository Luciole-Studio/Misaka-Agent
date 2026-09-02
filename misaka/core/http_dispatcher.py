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

DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300_000

HTTP_IDLE_TIMEOUT_CHOICES = [
    {"label": "30 sec", "timeoutMs": 30_000},
    {"label": "1 min", "timeoutMs": 60_000},
    {"label": "2 min", "timeoutMs": 120_000},
    {"label": "5 min", "timeoutMs": 300_000},
    {"label": "disabled", "timeoutMs": 0},
]

_ECMASCRIPT_WHITESPACE = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_DECIMAL_NUMBER = re.compile(
    r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?$"
)
_RADIX_NUMBER = re.compile(r"^0(?P<prefix>[bBoOxX])(?P<digits>[0-9A-Fa-f]+)$")


def parseHttpIdleTimeoutMs(value: Any) -> int | None:
    if isinstance(value, str):
        trimmed = value.strip(_ECMASCRIPT_WHITESPACE)
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
    proxy = http_proxy.strip(_ECMASCRIPT_WHITESPACE)
    if not proxy:
        return
    target = os.environ if environ is None else environ
    target.setdefault("HTTP_PROXY", proxy)
    target.setdefault("HTTPS_PROXY", proxy)


def createHttpxIdleTimeout(
    timeout_ms: Any = None,
) -> httpx.Timeout:
    """Translate header/body idle policy without imposing a total request limit."""
    if timeout_ms is None:
        timeout_ms = DEFAULT_HTTP_IDLE_TIMEOUT_MS
    normalized_timeout_ms = parseHttpIdleTimeoutMs(timeout_ms)
    if normalized_timeout_ms is None:
        raise ValueError(f"Invalid HTTP idle timeout: {timeout_ms}")
    return httpx.Timeout(None, read=normalized_timeout_ms / 1000)


__all__ = [
    "DEFAULT_HTTP_IDLE_TIMEOUT_MS",
    "HTTP_IDLE_TIMEOUT_CHOICES",
    "applyHttpProxySettings",
    "createHttpxIdleTimeout",
    "formatHttpIdleTimeoutMs",
    "parseHttpIdleTimeoutMs",
]
