"""Public types of chord's references: call contexts, readables, statuses and shapes.

Translated from pi's ``packages/chord/src/types.ts``. Since 1.2.0 the context types live in
``misaka.chord.context``, the JSON types in ``misaka.chord.json`` and the replicated state types in
``misaka.chord.state``; the facet and service types are gone with the facet and service runtime.

Interfaces are ``Protocol`` classes; option bags and events are ``TypedDict``\\ s with upstream's keys. ``Exposed`` is
a TypeScript brand; its runtime form is ``object.ExposedImpl``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from misaka.chord.contract import AnyContract
    from misaka.chord.delta import Op
    from misaka.utils.js import AbortSignal


class GetOptions(TypedDict, total=False):
    """Options of ``get``. ``principal`` is the app's: a JSON-serializable identity of a caller, mapped to an object key
    by ``createHost({"principalKey": ...})``."""

    # Principal the object is created for. Defaults to the host's own principal (`createHost({ self })`).
    principal: Any
    # False: a method-only reference. It receives no state (unless another reference to the same object wants it),
    # and is ready once its object is live.
    state: bool


class Ctx(Protocol):
    """The explicit call context of one root (a short Chord call tree). Calls made with it carry its root id and code
    version; ``get`` references are released when the call that received this ctx settles."""

    @property
    def signal(self) -> AbortSignal: ...

    @property
    def rootId(self) -> str: ...

    @property
    def codeVersion(self) -> int: ...

    @property
    def principal(self) -> Any:
        """Who the root acts as: the caller at ingress, the host's own principal inside objects."""
        ...

    def get(self, type: AnyContract, input: Any, options: GetOptions | None = None) -> Awaitable[Any]:
        """A reference for this call, released when the call settles. Resolves once it is ready: live, with every
        state member of its variant (unless ``state: False``). Rejects with the activation error, ``aborted`` when the
        ctx is aborted or the call settles first, or ``unavailable/ready_timeout`` after the host's ``readyTimeout``;
        the reference is released then."""
        ...


class SnapEvent(TypedDict):
    type: Literal["snap"]
    value: Any


class OpsEvent(TypedDict):
    type: Literal["ops"]
    ops: Sequence[Op]
    value: Any


class ResetEvent(TypedDict):
    type: Literal["reset"]
    value: Any


type StateEvent[T] = SnapEvent | OpsEvent | ResetEvent


class Readable[T](Protocol):
    """A readable state source: a cell, a derivation, or a reference's state member. Values are immutable by
    contract."""

    @property
    def value(self) -> T: ...

    def subscribe(self, listener: Callable[[T], None]) -> Callable[[], Any]:
        """Called with the current value immediately, then with every new value."""
        ...

    def events(self, listener: Callable[[StateEvent[T]], None]) -> Callable[[], Any]:
        """Delta stream: a snapshot immediately, then ops, or a reset after a migration or rebind."""
        ...


# What an implementation may expose as a state member: a cell, a derivation, or a held object's state member (an
# object goes live only once the members it exposes have their values).
type StateSource[T] = Readable[T]

# A local member backed by a resource (`self.expose(res.x)`): references read the resource's value once live.
type Exposed[T] = Any

type RefStatus = Literal["loading", "live", "released", "gone"]


class Shape(TypedDict):
    variant: str | None
    members: Sequence[str]
    # Fingerprint of the provider contract.
    contract: str


class RefBase(Protocol):
    """What every reference has besides its members."""

    @property
    def contract(self) -> AnyContract:
        """The consumer contract token this reference was bound with."""
        ...

    @property
    def status(self) -> RefStatus: ...

    @property
    def ready(self) -> Awaitable[None]: ...

    @property
    def variant(self) -> str | None: ...

    @property
    def shape(self) -> Shape | None: ...

    @property
    def error(self) -> Any:
        """Why the reference is gone, if it is."""
        ...

    def onShape(self, listener: Callable[[Shape], None]) -> Callable[[], Any]: ...

    def onStatus(self, listener: Callable[[RefStatus, Any], None]) -> Callable[[], Any]:
        """Called after every status change (``loading`` → ``live``, ``live`` → ``loading`` while a link reconnects,
        ``released``, ``gone`` with ``error``). A UI watches for ``gone`` to ``get`` again."""
        ...

    def release(self) -> None: ...


__all__ = [
    "Ctx",
    "Exposed",
    "GetOptions",
    "Readable",
    "RefBase",
    "RefStatus",
    "Shape",
    "StateEvent",
    "StateSource",
]
