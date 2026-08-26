"""Helpers for producing JSON-schema fragments that mirror the TS TypeBox helpers."""

from __future__ import annotations

from typing import Any

TSchema = dict[str, object]
Static = Any


class _TypeBoxCompat:
    @staticmethod
    def Unsafe(schema: TSchema) -> TSchema:
        return dict(schema)


Type = _TypeBoxCompat()


