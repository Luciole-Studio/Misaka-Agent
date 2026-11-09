"""MISAKA fork: stand-ins for the parts of the ``typebox`` npm package that pi-ai re-exports (``Type``, ``Static``,
``TSchema``). A schema is a JSON-schema dict; ``Static`` has no runtime meaning.
"""

from __future__ import annotations

from typing import Any

TSchema = dict[str, object]
Static = Any


class _TypeBoxCompat:
    @staticmethod
    def Unsafe(schema: TSchema) -> TSchema:
        return dict(schema)


Type = _TypeBoxCompat()

__all__ = ["Static", "TSchema", "Type"]
