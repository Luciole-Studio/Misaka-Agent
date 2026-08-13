"""HTTP dispatcher settings helpers."""

from __future__ import annotations

from typing import Any

DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300_000

HTTP_IDLE_TIMEOUT_CHOICES = [
    {"label": "30 sec", "timeoutMs": 30_000},
    {"label": "1 min", "timeoutMs": 60_000},
    {"label": "2 min", "timeoutMs": 120_000},
    {"label": "5 min", "timeoutMs": 300_000},
    {"label": "disabled", "timeoutMs": 0},
]

_configured_http_idle_timeout_ms = DEFAULT_HTTP_IDLE_TIMEOUT_MS


def parseHttpIdleTimeoutMs(value: Any) -> int | None:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.lower() == "disabled":
            return 0
        if not trimmed:
            return None
        try:
            return parseHttpIdleTimeoutMs(float(trimmed))
        except ValueError:
            return None

    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        return None
    return int(value)


def formatHttpIdleTimeoutMs(timeout_ms: int) -> str:
    for choice in HTTP_IDLE_TIMEOUT_CHOICES:
        if choice["timeoutMs"] == timeout_ms:
            return str(choice["label"])
    return f"{timeout_ms / 1000} sec"


def configureHttpDispatcher(timeout_ms: int = DEFAULT_HTTP_IDLE_TIMEOUT_MS) -> None:
    normalized_timeout_ms = parseHttpIdleTimeoutMs(timeout_ms)
    if normalized_timeout_ms is None:
        raise ValueError(f"Invalid HTTP idle timeout: {timeout_ms}")

    global _configured_http_idle_timeout_ms
    _configured_http_idle_timeout_ms = normalized_timeout_ms


def _get_configured_http_idle_timeout_ms() -> int:
    return _configured_http_idle_timeout_ms

__all__ = [
    "DEFAULT_HTTP_IDLE_TIMEOUT_MS",
    "HTTP_IDLE_TIMEOUT_CHOICES",
    "configureHttpDispatcher",
    "formatHttpIdleTimeoutMs",
    "parseHttpIdleTimeoutMs",
]
