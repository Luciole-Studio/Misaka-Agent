"""Helpers for normalizing header containers into plain dictionaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def headers_to_record(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    if isinstance(headers, Mapping):
        return {str(key): str(value) for key, value in headers.items()}
    return {str(key): str(value) for key, value in headers}


def provider_headers_to_record(
    headers: Mapping[str, str | None] | None,
) -> dict[str, str] | None:
    """Drop the headers whose value is ``None``, keep the rest.

    A ``None`` value in ``ProviderHeaders`` means "do not send this header" -- it is how
    resolved auth displaces a provider's own header, which the Cloudflare AI Gateway does
    with ``Authorization`` and ``x-api-key``. Passing one through to an HTTP client raises
    ``Header value must be str or bytes, not NoneType`` before the request goes out.

    Translated from pi's ``ai/src/utils/headers.ts``; ``None`` rather than an empty dict
    when nothing survives, so callers can tell "no headers" from "headers I removed".
    """
    if not headers:
        return None
    kept = {name: value for name, value in headers.items() if value is not None}
    return kept or None


def apply_provider_headers(target: dict[str, Any], overrides: Mapping[str, Any] | None) -> None:
    """Apply one override layer onto ``target``, where ``None`` means *remove this header*.

    Upstream hands ``ProviderHeaders`` -- nulls included -- straight to a provider SDK,
    because a JavaScript SDK reads ``null`` as "omit this header" and its ``Headers``
    object matches names case-insensitively. Python's SDKs do neither: ``openai`` raises
    ``Header value must be str or bytes, not NoneType``, and a dict keeps ``Authorization``
    and ``authorization`` as two entries. Doing the removal here reproduces what upstream's
    SDK does with the same input.
    """
    if not overrides:
        return
    for name, value in overrides.items():
        lowered = str(name).lower()
        for existing in [key for key in target if key.lower() == lowered]:
            del target[existing]
        if value is not None:
            target[str(name)] = value


def resolve_provider_headers(*layers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge header layers in order, honouring ``None`` as a deletion."""
    resolved: dict[str, Any] = {}
    for layer in layers:
        apply_provider_headers(resolved, layer)
    return resolved


__all__ = [
    "apply_provider_headers",
    "headers_to_record",
    "provider_headers_to_record",
    "resolve_provider_headers",
]
