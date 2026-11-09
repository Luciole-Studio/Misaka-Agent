"""Translated from pi's ``packages/ai/src/utils/error-body.ts`` (v1.1.0).

Shared normalization for provider HTTP error objects.

Endpoints behind a proxy / gateway may return a non-2xx response whose body
the provider SDK cannot fold into `error.message`. The SDK error object still
carries the HTTP status and the raw/parsed body, but under SDK-specific field
names. Provider catch blocks that read only `error.message` therefore drop
the body and surface opaque messages like `"403 status code (no body)"` or
collapse to `"Unknown: UnknownError"`.

`normalizeProviderError` probes the known SDK field shapes and returns a struct each provider
composes into its display string. The `messageCarriesBody` flag captures the
happy path where the SDK already folded the body into the message, so providers can preserve it without
double-printing.

MISAKA fork: the field shapes are the Python SDKs' (see ``_extractStatus`` and ``_pickBodyText``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from misaka.ai.utils._sdk_error import provider_error_status
from misaka.utils.js import (
    UNDEFINED,
    jsLength,
    jsonStringify,
    jsSlice,
    jsString,
    jsTrim,
)

MAX_PROVIDER_ERROR_BODY_CHARS = 4000


@dataclass(slots=True, kw_only=True)
class NormalizedProviderError:
    # HTTP status code, when one could be extracted from the SDK error object.
    status: int | None = None
    # Raw HTTP body reason, already trimmed and truncated to the cap.
    body: str | None = None
    # `error.message`, or `safeJsonStringify(error)` for a non-`Error` throw.
    message: str
    # True when `message` already contains the body (no separate body to add).
    messageCarriesBody: bool


def normalizeProviderError(error: Any) -> NormalizedProviderError:
    if not isinstance(error, BaseException):
        return NormalizedProviderError(message=safeJsonStringify(error), messageCarriesBody=False)

    status = _extractStatus(error)
    body = _extractBody(error)
    message = str(error)
    messageCarriesBody = body is None or body in message

    return NormalizedProviderError(status=status, body=body, message=message, messageCarriesBody=messageCarriesBody)


def _extractStatus(error: Any) -> int | None:
    """Probe the HTTP status, first numeric hit wins.

    MISAKA fork: in the Python SDKs' field order (``misaka.ai.utils._sdk_error.provider_error_status``), not
    `statusCode` (Mistral) → `status` (`openai`, `@google/genai`) → `$metadata.httpStatusCode` (Bedrock) →
    `$response.statusCode` (Bedrock).
    """
    return provider_error_status(error)


def _extractBody(error: Any) -> str | None:
    """Probe the raw body reason, first usable hit wins. Empty objects and unread response
    streams are treated as no body so they do not surface as `"{}"` or serialized
    stream internals. The chosen body is truncated to the cap."""
    bodyText = _pickBodyText(error)
    if bodyText is None:
        return None
    trimmed = jsTrim(bodyText)
    if len(trimmed) == 0:
        return None
    return truncateErrorText(trimmed, MAX_PROVIDER_ERROR_BODY_CHARS)


def _pickBodyText(error: Any) -> str | None:
    """MISAKA fork: the Python SDKs' fields: `body` (a string or a parsed object), `error` (a parsed object), botocore's
    `response["Error"]`, then the text of an `httpx.Response` that was read; upstream reads `body`, `error` and
    `$response.body`."""
    body = getattr(error, "body", None)
    if isinstance(body, str):
        return body
    if _isPlainNonEmptyObject(body):
        return safeJsonStringify(body)

    parsed = getattr(error, "error", None)
    if _isPlainNonEmptyObject(parsed):
        return safeJsonStringify(parsed)

    response = getattr(error, "response", None)
    if response is not None:
        if isinstance(response, dict):
            failure = response.get("Error")
            if _isPlainNonEmptyObject(failure):
                return safeJsonStringify(failure)
            return None
        if _isReadableStreamLike(response):
            return None
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - an unread streaming response simply has no text
            return None
        if isinstance(text, str):
            return text
    return None


def _isReadableStreamLike(value: Any) -> bool:
    """MISAKA fork: an ``httpx.Response`` is excluded by type rather than by probing its ``text``, which raises
    ``ResponseNotRead`` on an unread body; anything else with a ``read`` or ``pipe`` method is a stream."""
    if isinstance(value, httpx.Response):
        return False
    return callable(getattr(value, "read", None)) or callable(getattr(value, "pipe", None))


def _isPlainNonEmptyObject(value: Any) -> bool:
    """Only a PLAIN object counts as an HTTP body. SDK error fields can hold class
    instances instead of parsed bodies, and stringifying one produced garbage
    as the "body", which then REPLACED `error.message` in the composed display string.
    `error.message` is where the SDK puts the real deserialized exception text, so the one useful string was
    discarded for noise. A class instance yields no body, `messageCarriesBody` stays true, and the real
    message survives. MISAKA fork: a plain object is a ``dict`` exactly."""
    return type(value) is dict and len(value) > 0


def formatProviderError(norm: NormalizedProviderError, prefix: str | None = None) -> str:
    """Compose a display string from a normalized error. When the message already
    carries the body or no body/status was extracted, the message is returned unchanged. Otherwise the status and
    body are surfaced, with an optional provider prefix.

    - no prefix: `"<status>: <body>"`
    - prefix:    `"<prefix> (<status>): <body>"`
    """
    if norm.messageCarriesBody or norm.status is None or norm.body is None:
        return f"{prefix} ({norm.status}): {norm.message}" if prefix is not None and norm.status is not None else norm.message
    return f"{prefix} ({norm.status}): {norm.body}" if prefix is not None else f"{norm.status}: {norm.body}"


def truncateErrorText(text: str, maxChars: int) -> str:
    if jsLength(text) <= maxChars:
        return text
    return f"{jsSlice(text, 0, maxChars)}... [truncated {jsLength(text) - maxChars} chars]"


def safeJsonStringify(value: Any) -> str:
    try:
        serialized = UNDEFINED if value is UNDEFINED or callable(value) else jsonStringify(value)
        return jsString(value) if serialized is UNDEFINED else serialized  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - JSON.stringify throws on cycles and BigInt; String(value) still works
        return jsString(value)


__all__ = [
    "MAX_PROVIDER_ERROR_BODY_CHARS",
    "NormalizedProviderError",
    "formatProviderError",
    "normalizeProviderError",
    "safeJsonStringify",
    "truncateErrorText",
]
