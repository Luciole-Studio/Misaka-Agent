"""Helpers for producing JSON-schema fragments that mirror the TS TypeBox helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


TSchema = dict[str, object]
Static = Any


class _TypeBoxCompat:
    @staticmethod
    def Unsafe(schema: TSchema) -> TSchema:
        return dict(schema)


Type = _TypeBoxCompat()


def string_enum(
    values: Sequence[str],
    options: dict[str, Any] | None = None,
) -> TSchema:
    schema: TSchema = {
        "type": "string",
        "enum": list(values),
    }
    if options:
        description = options.get("description")
        default = options.get("default")
        if description:
            schema["description"] = description
        if default:
            schema["default"] = default
    return schema


StringEnum = string_enum
