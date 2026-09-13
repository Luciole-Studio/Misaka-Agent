"""Provider HTTP error normalization, translated from pi's ``utils/error-body.ts``.

Upstream's header comment (``error-body.ts:1-14``) states the problem: an endpoint behind
a proxy or gateway answers non-2xx with a body the SDK cannot fold into ``error.message``,
so a catch block that reads only the message surfaces ``403 status code (no body)`` and
drops the actual reason. This probes the places SDK errors keep the status and the raw
body, and returns a struct the caller composes into a display string.

**The field names are not upstream's.** ``error-body.ts`` probes ``statusCode`` →
``status`` → ``$metadata.httpStatusCode`` → ``$response.statusCode`` for the status
(``error-body.ts:61-67``) and ``body`` → ``error`` → ``$response.body`` for the body
(``error-body.ts:84-92``). What is translated is the *behaviour*: probe in order, first
usable hit wins, and refuse bodies that are really stream wrappers. The chains below
probe ``status_code``/``status``/``statusCode``, then ``response.status_code`` and
botocore's ``response["ResponseMetadata"]["HTTPStatusCode"]``; for the body, ``body``,
``error``, botocore's ``response["Error"]``, then ``response.text``. ``$metadata`` and
``$response`` are dropped.

The rule about plain objects is upstream's (``error-body.ts:98-111``): only a plain dict
counts as a body, because serializing an SDK wrapper class produced garbage that then
*replaced* the real message -- which is where the SDK put the useful text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

MAX_PROVIDER_ERROR_BODY_CHARS = 4000


@dataclass(slots=True)
class NormalizedProviderError:
    # `str(error)`, or a serialization of a non-exception throw.
    message: str
    # True when `message` already contains the body, so there is nothing to add.
    messageCarriesBody: bool
    # HTTP status, when one could be extracted.
    status: int | None = None
    # Raw HTTP body reason, trimmed and truncated to the cap.
    body: str | None = None


def safe_json_stringify(value: Any) -> str:
    """Compact, like ``JSON.stringify``, so the serialized body matches upstream's shape."""
    try:
        return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def truncate_error_text(text: str, maxChars: int) -> str:
    if len(text) <= maxChars:
        return text
    return f"{text[:maxChars]}... [truncated {len(text) - maxChars} chars]"


def _is_plain_non_empty_dict(value: Any) -> bool:
    """Only a plain, non-empty dict counts as a parsed body."""
    return type(value) is dict and len(value) > 0


def _is_stream_like(value: Any) -> bool:
    """A body that has to be consumed, as opposed to a response we can quote.

    An ``httpx.Response`` is excluded by type rather than by probing its ``text``: that
    property raises ``ResponseNotRead`` when the body has not been read yet, and the
    exception escapes ``getattr``'s default. Probing it turned error normalisation into a
    second, unrelated exception -- worse than the missing body it was added to prevent.
    """
    if isinstance(value, httpx.Response):
        return False
    return callable(getattr(value, "read", None)) or callable(getattr(value, "pipe", None))


def provider_error_status(error: Any) -> int | None:
    """First numeric hit wins, in the order below.

    Public because ``utils/provider_retry`` imports it (``provider_retry.py:32``): both
    modules have to recognise the same objects as provider errors, and two copies of this
    order would drift apart.
    """
    for name in ("status_code", "status", "statusCode"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    response = getattr(error, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        # botocore keeps it in a dict rather than on an object.
        if isinstance(response, dict):
            metadata = response.get("ResponseMetadata")
            if isinstance(metadata, dict):
                value = metadata.get("HTTPStatusCode")
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    # google-genai APIError keeps the HTTP code here even when response is
    # absent. Do not mistake provider-specific codes (e.g. 1210) for HTTP.
    value = getattr(error, "code", None)
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def provider_error_headers(error: Any) -> Any:
    """The response headers an SDK error carries, wherever it keeps them.

    openai 3.3.1 and anthropic 0.125.0 keep them on ``error.response.headers``; upstream's
    ``ProviderError`` declares ``headers`` on the error itself (``provider-retry.ts:9-12``).
    Both are answered here so the retry policy can read ``retry-after`` from either.
    """
    headers = getattr(error, "headers", None)
    if headers is not None:
        return headers
    response = getattr(error, "response", None)
    return getattr(response, "headers", None) if response is not None else None


def _pick_body_text(error: Any) -> str | None:
    body = getattr(error, "body", None)
    if isinstance(body, str):
        return body
    if _is_plain_non_empty_dict(body):
        return safe_json_stringify(body)

    parsed = getattr(error, "error", None)
    if _is_plain_non_empty_dict(parsed):
        return safe_json_stringify(parsed)

    response = getattr(error, "response", None)
    if response is not None:
        if isinstance(response, dict):
            failure = response.get("Error")
            if _is_plain_non_empty_dict(failure):
                return safe_json_stringify(failure)
            return None
        if _is_stream_like(response):
            return None
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - an unread streaming response simply has no text
            return None
        if isinstance(text, str):
            return text
    return None


def _extract_body(error: Any) -> str | None:
    text = _pick_body_text(error)
    if text is None:
        return None
    trimmed = text.strip()
    if not trimmed:
        return None
    return truncate_error_text(trimmed, MAX_PROVIDER_ERROR_BODY_CHARS)


def normalize_provider_error(error: Any) -> NormalizedProviderError:
    """Status, body and message, pulled out of whatever the client raised."""
    if not isinstance(error, BaseException):
        return NormalizedProviderError(
            message=safe_json_stringify(error), messageCarriesBody=False
        )

    status = provider_error_status(error)
    body = _extract_body(error)
    message = str(error)
    return NormalizedProviderError(
        message=message,
        messageCarriesBody=body is None or body in message,
        status=status,
        body=body,
    )


def format_provider_error(norm: NormalizedProviderError, prefix: str | None = None) -> str:
    """Compose the display string, without printing the body twice.

    When the client already folded the body into its message, adding it again would show
    the same JSON twice; the message is returned as-is instead.
    """
    if norm.messageCarriesBody or norm.status is None or norm.body is None:
        if prefix is not None and norm.status is not None:
            return f"{prefix} ({norm.status}): {norm.message}"
        return norm.message
    if prefix is not None:
        return f"{prefix} ({norm.status}): {norm.body}"
    return f"{norm.status}: {norm.body}"


__all__ = [
    "MAX_PROVIDER_ERROR_BODY_CHARS",
    "NormalizedProviderError",
    "format_provider_error",
    "normalize_provider_error",
    "provider_error_headers",
    "provider_error_status",
    "safe_json_stringify",
    "truncate_error_text",
]
