"""What a factory declares through ``self`` besides state cells: the shapes of resources and listeners.

Translated from pi's ``packages/chord/src/declare.ts``. ``ValueOf`` and ``EventOf`` are TypeScript conditional types
and have no runtime counterpart; ``ResourceDeclaration`` is the mapping a factory passes to ``self.resource``
(``version``, ``deps``, ``exclusive``, ``open``, ``close``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired, Protocol, TypedDict

if TYPE_CHECKING:
    from misaka.chord.types import Ctx
    from misaka.utils.js import AbortSignal


class Subscribable[E](Protocol):
    def subscribe(self, listener: Callable[[E], None]) -> Callable[[], None]: ...


class OpenOptions(TypedDict):
    signal: AbortSignal


class ResourceDeclaration[T](TypedDict):
    """``self.resource(name, declaration)``: something the object opens and later closes (a database, a watch, a
    socket)."""

    # Bump to replace the resource at the next reload (a native value has no schema to detect a change).
    version: int
    # References or resources that must be live/open first. A change replaces the resource.
    deps: NotRequired[Sequence[Any]]
    # Exclusive resources are replaced at the gate: old closed before new opened.
    exclusive: NotRequired[bool]
    open: Callable[[OpenOptions], T | Awaitable[T]]
    close: NotRequired[Callable[[T], Awaitable[None] | None]]


class Resource[T](Protocol):
    @property
    def key(self) -> str: ...

    @property
    def value(self) -> T:
        """Throws not_ready until opened."""
        ...


@dataclass(frozen=True, slots=True)
class ListenerDecl:
    """A listener as ``self.on`` declares it."""

    source: Any
    select: Callable[[Any], Subscribable[Any]] | None
    fn: Callable[[Any, Ctx], Awaitable[None] | None]


# A cell has a `key` and a `value` too: sources are matched before resources.
# What `self.on(name, source, ...)` passes to `select`: a resource's opened value, or the source itself.
type ValueOf[S] = Any
# What `self.on(name, source, handler)` delivers: a readable's values, an event source's events.
type EventOf[S] = Any

__all__ = ["EventOf", "ListenerDecl", "Resource", "ResourceDeclaration", "Subscribable", "ValueOf"]
