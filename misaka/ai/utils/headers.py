"""Helpers for normalizing header containers into plain dictionaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def headers_to_record(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    if isinstance(headers, Mapping):
        return {str(key): str(value) for key, value in headers.items()}
    return {str(key): str(value) for key, value in headers}


headersToRecord = headers_to_record

__all__ = ["headersToRecord"]
