"""Translated from pi's ``packages/ai/src/utils/headers.ts`` (v1.1.0).

MISAKA fork: ``headersToRecord`` takes a mapping (``httpx.Headers`` yields lower-cased names like fetch ``Headers``) or
an iterable of pairs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def headersToRecord(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    if isinstance(headers, Mapping):
        return {str(key): str(value) for key, value in headers.items()}
    return {str(key): str(value) for key, value in headers}


def providerHeadersToRecord(*headerSources: Mapping[str, str | None] | None) -> dict[str, str] | None:
    merged: dict[str, tuple[str, str]] = {}
    for source in headerSources:
        for name, value in (source or {}).items():
            normalizedName = name.lower()
            merged.pop(normalizedName, None)
            if value is not None:
                merged[normalizedName] = (name, value)
    return dict(merged.values()) if len(merged) > 0 else None


__all__ = [
    "headersToRecord",
    "providerHeadersToRecord",
]
