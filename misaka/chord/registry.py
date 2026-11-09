"""Registries: tables that objects of lifetime ``"host"`` fill (``self.register``) and code reads per code version.

Translated from pi's ``packages/chord/src/registry.ts``. ``Registry.value`` is a TypeScript type carrier only and
has no counterpart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from misaka.chord.types import Ctx


@dataclass(frozen=True, eq=False, slots=True)
class Registry[V]:
    name: str
    conflict: Literal["error", "last"]


def defineRegistry[V](name: str, options: Mapping[str, Any] | None = None) -> Registry[V]:
    """``conflict: "error"`` rejects two objects registering one key; ``"last"`` keeps the later registration."""
    return Registry(name, (options or {}).get("conflict") or "error")


type RegistryTable[V] = Mapping[str, V]


class RegistryView[V](Protocol):
    def table(self, ctx: Ctx | None = None) -> RegistryTable[V]:
        """Table of ``ctx.codeVersion``, or of the current code version without ctx."""
        ...

    def get(self, key: str, ctx: Ctx | None = None) -> V | None: ...

    def subscribe(self, listener: Callable[[RegistryTable[V], int], None]) -> Callable[[], None]:
        """Called with the current table immediately and whenever a new table is installed."""
        ...


@dataclass(frozen=True, slots=True)
class Registration:
    registry: Registry[Any]
    key: str
    value: Any


__all__ = ["Registration", "Registry", "RegistryTable", "RegistryView", "defineRegistry"]
