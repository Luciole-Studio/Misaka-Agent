"""Remote references: one shared binding per host and key, and the references (views) onto it.

Translated from pi's ``packages/chord/src/remote.ts``. MISAKA fork: the module is ``remote_ref``, because
``misaka.chord.remote`` is the contract builder (a package attribute and a submodule cannot share a name). A promise is an ``asyncio.Future``
(``misaka.chord._promise``); a reference's ``Proxy`` is ``object.RefProxy``; JavaScript ``Set``\\ s are
insertion-ordered ``dict``\\ s.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from misaka.chord._promise import (
    Deferred,
    deferred,
    exposed,
    isRejection,
    rejected,
    rejection,
    resolved,
)
from misaka.chord._typebox import Value
from misaka.chord.cell import Hydration
from misaka.chord.contract import AnyContract, takesNoArgs
from misaka.chord.delta import Decoder, applyImmutable, decoder
from misaka.chord.errors import ChordError, invalid
from misaka.chord.object import (
    REF_BASE,
    CtxImpl,
    RefOwner,
    RefProxy,
    StatusListener,
    notifyStatus,
    raceSignal,
)
from misaka.chord.wire import RootRef, fromWireError
from misaka.utils.js import UNDEFINED, AbortSignal, clearTimeout, eager

if TYPE_CHECKING:
    from misaka.chord.types import Ctx, RefStatus, Shape, StateEvent


# One state frame for a member, as the binding receives it (payload unpacked): `{ as, value?, ops?, error? }`.
type StateIn = dict[str, Any]


class RemoteChannel(Protocol):
    """What a binding needs from the link it is bound to (implemented by Connection)."""

    def hold(self, binding: RemoteBinding, again: bool) -> int:
        """Sends a hold for ``binding``; ``again``: the binding already has values (a re-hold), its unit is not
        urgent."""
        ...

    def release(self, id: int) -> None: ...

    def call(
        self, hold: int, member: str, args: Any, root: RootRef | None, signal: AbortSignal
    ) -> asyncio.Future[Any]: ...

    def wantState(self, hold: int, wanted: bool) -> None:
        """Starts (``sub``) or stops (``unsub``) the state of a hold."""
        ...


class RemoteHost(Protocol):
    """What a binding needs from its host."""

    inFactory: int

    def report(self, error: Any) -> None: ...

    def root(self, fn: Callable[[Ctx], Any], options: dict[str, Any] | None = None) -> asyncio.Future[Any]: ...

    def bindingIdle(self, binding: RemoteBinding) -> None:
        """The binding has no views left: the host lingers it or closes it."""
        ...

    def forgetBinding(self, binding: RemoteBinding) -> None:
        """The binding ended or was closed: the host forgets it."""
        ...


@dataclass
class MemberState:
    """A member's value with the decoder of its current stream."""

    value: Any
    decoder: Decoder


@dataclass(frozen=True)
class _Hold:
    channel: RemoteChannel
    id: int


@dataclass
class _Pending:
    shape: Shape
    missing: dict[str, None]
    values: dict[str, MemberState]


class RemoteBinding:
    """One hold per host and key (type, normalized input, principal key, contract fingerprint), shared by every
    reference to it: one hold over the link, one stream per state member, calls over the same hold. References are
    views onto it.

    A hold implies the state of its variant (unless no view wants state): the provider answers with a unit, the shape
    followed by a snapshot of each state member the binding lacks. A shape is applied only once all of its snapshots
    are here, so a live binding always has every state member of its variant. After the first hydration the values
    stay readable while the binding re-holds (link loss): status ``loading``, last values kept."""

    def __init__(self, host: RemoteHost, key: str, contract: AnyContract, at: str, input: Any, principal: Any) -> None:
        self.host = host
        self.key = key
        self.contract = contract
        self.at = at
        self.input = input
        # Forwarded principal (omitted for the host's own principal).
        self.principal = principal
        # `ended`: gone with `error`, or closed (no views, no linger).
        self.status: str = "loading"
        self.error: Any = None
        # The applied shape (kept while re-holding).
        self.shape: Shape | None = None
        # Every state member of the applied shape has a value (stays true while re-holding).
        self.hydrated = False
        # The current hold; None while resolving the route or after a link loss.
        self.hold: _Hold | None = None
        # What the provider was told: state for this hold (`state: false` / `sub` / `unsub`).
        self.stateOn = False
        self.views: dict[RemoteRef, None] = {}
        self.members: dict[str, MemberState] = {}
        # Lingering since (no views): set by the host.
        self.lingerTimer: Any = None
        self._listeners: dict[str, dict[ViewReadable, None]] = {}
        # A shape waiting for its snapshots.
        self._pending: _Pending | None = None
        # Resolved while a shape is applied for the current hold (calls wait for it).
        self._live: Deferred[None] = deferred()
        # A shape was applied once (a re-hold says `again`).
        self._applied = False
        # Waiting for the first hydration (`whenHydrated`).
        self._hydrationWaiters: list[Deferred[None]] = []

    @property
    def ended(self) -> bool:
        return self.status == "ended"

    @property
    def stateViews(self) -> int:
        """Views that want state."""
        return sum(1 for view in self.views if view.wantsState)

    @property
    def wantsState(self) -> bool:
        """Whether the next hold should carry state: some view wants it, or nobody is left (lingering keeps it)."""
        return self.stateOn if len(self.views) == 0 else self.stateViews > 0

    @property
    def applied(self) -> bool:
        """Whether a re-hold is ``again`` (the binding already has a shape)."""
        return self._applied

    # ─── views ───

    def addView(self, view: RemoteRef) -> None:
        self.views[view] = None
        if self.lingerTimer is not None:
            clearTimeout(self.lingerTimer)
            self.lingerTimer = None
        self._syncState()
        view.update()

    def removeView(self, view: RemoteRef) -> None:
        if self.views.pop(view, UNDEFINED) is UNDEFINED:
            return
        for readables in self._listeners.values():
            for readable in list(readables):
                if readable.view is view:
                    del readables[readable]
        if len(self.views) == 0:
            self.host.bindingIdle(self)
            return
        self._syncState()

    def _syncState(self) -> None:
        """Tells the provider to start or stop state when the views' wish changed."""
        if self.ended or len(self.views) == 0:
            return
        wanted = self.stateViews > 0
        if wanted == self.stateOn:
            return
        self.stateOn = wanted
        if not wanted:
            self.hydrated = False
            self.members.clear()
            if self._pending is not None:
                self._pending.missing.clear()
        if self.hold is not None:
            self.hold.channel.wantState(self.hold.id, wanted)
        for view in list(self.views):
            view.update()

    def listen(self, name: str, readable: ViewReadable) -> None:
        self._listeners.setdefault(name, {})[readable] = None

    def unlisten(self, name: str, readable: ViewReadable) -> None:
        readables = self._listeners.get(name)
        if readables is not None:
            readables.pop(readable, None)

    def inVariant(self, name: str) -> bool:
        """Whether ``name`` is a state member of the applied variant (as the consumer's contract describes it)."""
        variant = None if self.shape is None else self.shape.get("variant")
        if variant is None:
            return False
        members = self.contract.description["variants"].get(variant) or {}
        member = members.get(name)
        return member is not None and member["kind"] == "state"

    def read(self, name: str) -> Any:
        """The members' value as views read it: throws ``not_ready`` before the first hydration, ``not_in_variant``
        outside it."""
        if self.status == "ended":
            raise rejection(self.terminalError())
        if not self.hydrated:
            raise ChordError("not_ready", f"{self.key}.{name} is not hydrated yet")
        if not self.inVariant(name):
            variant = None if self.shape is None else self.shape.get("variant")
            raise ChordError("not_in_variant", f"{self.key}: {name} is not a state member of variant {_text(variant)}")
        member = self.members.get(name)
        return UNDEFINED if member is None else member.value

    def whenHydrated(self) -> asyncio.Future[None]:
        """Resolves once the binding has its state (rejects if it ends first)."""
        if self.hydrated:
            return resolved()
        if self.ended:
            return rejected(self.terminalError())
        waiter: Deferred[None] = deferred()
        self._hydrationWaiters.append(waiter)
        return waiter.promise

    def _settleHydration(self, error: Any = UNDEFINED) -> None:
        waiters = list(self._hydrationWaiters)
        self._hydrationWaiters.clear()
        for waiter in waiters:
            if error is UNDEFINED:
                waiter.resolve()
            else:
                waiter.reject(error)

    def readable(self, name: str) -> bool:
        return self.status != "ended" and self.hydrated and self.inVariant(name)

    def terminalError(self) -> Any:
        return self.error if self.error is not None else ChordError("released", f"reference {self.key} released")

    # ─── driven by the host and the link ───

    def bound(self, channel: RemoteChannel, id: int) -> None:
        self.hold = _Hold(channel, id)
        self.stateOn = self.wantsState

    def shaped(self, shape: Shape, listed: Sequence[str]) -> None:
        """A unit begins: ``listed`` are the members whose snapshots follow."""
        if self.ended:
            return
        previous = self._pending
        missing: dict[str, None] = dict.fromkeys(listed)
        # A newer shape before the older one's snapshots (relays may reorder them): wait for both.
        if previous is not None:
            for name in previous.missing:
                missing[name] = None
        wanted = set(self._stateMembers(shape))
        for name in list(missing):
            if name not in wanted or not self.stateOn:
                del missing[name]
        self._pending = _Pending(shape, missing, {} if previous is None else previous.values)
        if len(missing) == 0:
            self._apply()

    def state(self, name: str, frame: StateIn) -> None:
        """A state frame for member ``name`` of the current hold."""
        if self.ended:
            return
        kind = frame["as"]
        if kind == "error":
            error = fromWireError(frame["error"])
            # Before the first hydration nobody could read anything: the binding fails instead of waiting forever.
            if not self.hydrated and self.stateOn:
                self.gone(error)
                self.close()
            else:
                self.host.report(error)
            return
        pending = self._pending
        if kind == "ops":
            target = (None if pending is None else pending.values.get(name)) or self.members.get(name)
            if target is None:
                return
            ops = target.decoder.decode(frame.get("ops") or [])
            target.value = applyImmutable(target.value, ops)
            if target is self.members.get(name):
                self._deliver(name, {"type": "ops", "ops": ops, "value": target.value})
            return
        fresh = MemberState(frame.get("value", UNDEFINED), decoder())
        if pending is not None and (name in pending.missing or name in pending.values):
            pending.values[name] = fresh
            pending.missing.pop(name, None)
            if len(pending.missing) == 0:
                self._apply()
            return
        if not self.inVariant(name):
            return
        self.members[name] = fresh
        self._deliver(name, {"type": "reset", "value": fresh.value})

    def gone(self, error: Any) -> None:
        """The provider ended the object (or refused the hold): every view is gone."""
        if self.ended:
            return
        self.status = "ended"
        self.error = error
        self.hold = None
        self._pending = None
        self._live.reject(error)
        self._settleHydration(error)
        for view in list(self.views):
            view.end(error)
        self._listeners.clear()
        self.host.forgetBinding(self)

    def detached(self) -> None:
        """The link went down but may come back (upstream reconnect): keep the values, wait for the re-hold."""
        if self.ended:
            return
        self.hold = None
        self._pending = None
        if self.status == "live":
            self.status = "loading"
            self._live = deferred()
        for view in list(self.views):
            view.update()

    def close(self) -> None:
        """No views and no linger: releases the hold and forgets the binding."""
        if self.lingerTimer is not None:
            clearTimeout(self.lingerTimer)
        self.lingerTimer = None
        if self.hold is not None:
            self.hold.channel.release(self.hold.id)
        self.hold = None
        if self.status != "ended":
            self.status = "ended"
            error = ChordError("released", f"reference {self.key} released")
            self._live.reject(error)
            self._settleHydration(error)
            for view in list(self.views):
                view.end(error)
        self._pending = None
        self._listeners.clear()
        self.host.forgetBinding(self)

    @eager
    async def call(self, name: str, args: Any, ctx: Ctx) -> Any:
        """Calls ``name`` over the current hold, waiting for it while the binding (re)holds."""
        while True:
            if self.ended:
                raise rejection(self.terminalError())
            if self.status != "live" or self.hold is None:
                await raceSignal(self._live.promise, ctx.signal)
            hold = self.hold
            if hold is None or self.status != "live":
                continue
            variant = None if self.shape is None else self.shape.get("variant")
            members = None if variant is None else self.contract.description["variants"].get(variant)
            description = None if members is None else members.get(name)
            if description is None or description["kind"] != "method":
                raise ChordError("not_in_variant", f"{self.key}: {name} is not a method of variant {_text(variant)}")
            # Send only what the consumer's own schema knows (the additive rule's premise; see ObjectImpl.call).
            sent = UNDEFINED if "args" not in description else Value.Clean(description["args"], Value.Clone(args))
            return await hold.channel.call(
                hold.id,
                name,
                sent,
                {"rootId": ctx.rootId, "codeVersion": ctx.codeVersion},
                ctx.signal,
            )

    def _stateMembers(self, shape: Shape) -> list[str]:
        variant = shape.get("variant")
        members = None if variant is None else self.contract.description["variants"].get(variant)
        if members is None:
            return []
        return [name for name in shape["members"] if (members.get(name) or {}).get("kind") == "state"]

    def _apply(self) -> None:
        """Applies the pending shape with its snapshots: members outside the variant go, new values are announced."""
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        self.shape = pending.shape
        wanted = set(self._stateMembers(pending.shape))
        for name in list(self.members):
            if name not in wanted:
                del self.members[name]
        for name, state in pending.values.items():
            if name in wanted:
                self.members[name] = state
        self.hydrated = self.stateOn and all(name in self.members for name in wanted)
        if self.hydrated:
            self._settleHydration()
        self.status = "live"
        self._applied = True
        self._live.resolve()
        for view in list(self.views):
            view.update()
        for name, state in pending.values.items():
            if name in wanted:
                self._deliver(name, {"type": "reset", "value": state.value})
        for view in list(self.views):
            view.shapeChanged()

    def _deliver(self, name: str, event: StateEvent[Any]) -> None:
        readables = self._listeners.get(name)
        if readables is None:
            return
        for readable in list(readables):
            readable.deliver(event)


def _text(value: Any) -> str:
    """``String(value)`` in a template literal."""
    return "undefined" if value is None or value is UNDEFINED else str(value)


class ViewReadable:
    """A state member of a remote reference: the binding's value. Reading throws ``not_ready`` before the first
    hydration and ``not_in_variant`` outside the variant. A listener added before the hydration gets its snapshot
    then."""

    def __init__(self, view: RemoteRef, name: str) -> None:
        self.view = view
        self._name = name
        self._values: dict[Callable[[Any], None], None] = {}
        self._events: dict[Callable[[StateEvent[Any]], None], None] = {}
        # Event listeners that have not had their snapshot yet.
        self._unseen: dict[Callable[[StateEvent[Any]], None], None] = {}

    @property
    def value(self) -> Any:
        if self.view.ended:
            raise rejection(self.view.terminalError())
        return self.view.binding.read(self._name)

    @property
    def __chord_hydration__(self) -> Hydration | None:
        """Not hydrated before the binding's first snapshots (a local object forwarding this member waits for
        them)."""
        binding = self.view.binding
        if binding.hydrated:
            return None
        return Hydration(False, binding.whenHydrated)

    def _memberValue(self) -> Any:
        member = self.view.binding.members.get(self._name)
        return UNDEFINED if member is None else member.value

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self._values[listener] = None
        self._attach()
        if self._readable():
            value = self._memberValue()
            self._safe(lambda: listener(value))
        return lambda: self._remove(self._values, listener)

    def events(self, listener: Callable[[StateEvent[Any]], None]) -> Callable[[], None]:
        self._events[listener] = None
        self._attach()
        if self._readable():
            value = self._memberValue()
            self._safe(lambda: listener({"type": "snap", "value": value}))
        else:
            self._unseen[listener] = None

        def unsubscribe() -> None:
            self._unseen.pop(listener, None)
            self._remove(self._events, listener)

        return unsubscribe

    def deliver(self, event: StateEvent[Any]) -> None:
        for listener in list(self._events):
            if self._unseen.pop(listener, UNDEFINED) is UNDEFINED:
                self._safe(lambda listener=listener: listener(event))
            elif event["type"] != "ops":
                self._safe(lambda listener=listener: listener({"type": "snap", "value": event["value"]}))
        for listener in list(self._values):
            self._safe(lambda listener=listener: listener(event["value"]))

    def stop(self) -> None:
        self._values.clear()
        self._events.clear()
        self._unseen.clear()
        self.view.binding.unlisten(self._name, self)

    def _readable(self) -> bool:
        return not self.view.ended and self.view.binding.readable(self._name)

    def _attach(self) -> None:
        if not self.view.ended:
            self.view.binding.listen(self._name, self)

    def _remove(self, listeners: dict[Any, None], listener: Any) -> None:
        listeners.pop(listener, None)
        if len(self._values) + len(self._events) == 0:
            self.view.binding.unlisten(self._name, self)

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as error:  # a listener's failure is reported
            if not isRejection(error):
                raise
            self.view.binding.host.report(error)


class RemoteRef:
    """A reference to an object on another host: a view onto the host's shared binding for its key. Each view has
    its own owner, status, observers and ``release``; ending by the provider and re-holding belong to the binding."""

    object = None

    def __init__(self, binding: RemoteBinding, owner: RefOwner, wantsState: bool) -> None:
        self.binding = binding
        self.owner = owner
        # False: a method-only view (`state: false`), ready once live.
        self.wantsState = wantsState
        self.status: RefStatus = "loading"
        self.error: Any = None
        self.shapeListeners: dict[Callable[[Shape], None], None] = {}
        self._statusListeners: dict[StatusListener, None] = {}
        self._readables: dict[str, ViewReadable] = {}
        self._callers: dict[str, Callable[..., Any]] = {}
        ready: Deferred[None] = deferred()
        self._ready = ready.promise
        self._settleReady: Deferred[None] | None = ready
        self.proxy = RefProxy(self)

    @property
    def ready(self) -> asyncio.Future[None]:
        """Settles once the reference is live; a waiter that gives up cancels its own view only (``exposed``)."""
        return exposed(self._ready)

    @property
    def contract(self) -> AnyContract:
        return self.binding.contract

    @property
    def address(self) -> str:
        return self.binding.key

    @property
    def ended(self) -> bool:
        return self.status == "released" or self.status == "gone"

    @property
    def variant(self) -> str | None:
        return None if self.binding.shape is None else self.binding.shape.get("variant")

    @property
    def shape(self) -> Shape | None:
        return self.binding.shape

    def onShape(self, listener: Callable[[Shape], None]) -> Callable[[], Any]:
        self.shapeListeners[listener] = None
        return lambda: self.shapeListeners.pop(listener, UNDEFINED) is not UNDEFINED

    def onStatus(self, listener: StatusListener) -> Callable[[], Any]:
        self._statusListeners[listener] = None
        return lambda: self._statusListeners.pop(listener, UNDEFINED) is not UNDEFINED

    def terminalError(self) -> Any:
        return self.error if self.status == "gone" else ChordError("released", f"reference {self.address} released")

    def has(self, name: str) -> bool:
        """``name in ref``: upstream's proxy has no ``has`` trap, so this asks the reference itself: its public fields,
        getters and methods (upstream's ``#private`` ones, here ``_``-prefixed, are not visible to ``in``). MISAKA fork:
        ``has`` is this port's own trap, not a member of upstream's class, and Python's dunders stand for nothing of
        ``Object.prototype``."""
        return name in REF_BASE or (not name.startswith("_") and name != "has" and hasattr(self, name))

    def release(self) -> None:
        if self.ended:
            return
        self.status = "released"
        self._notify()
        if self._settleReady is not None:
            self._settleReady.reject(ChordError("released", f"reference {self.address} released"))
        self._settleReady = None
        for readable in list(self._readables.values()):
            readable.stop()
        self.binding.removeView(self)

    # ─── driven by the binding ───

    def update(self) -> None:
        """Follows the binding: live once it has a shape (and, for a state view, its state) over a current hold."""
        if self.ended:
            return
        binding = self.binding
        live = binding.status == "live" and binding.hold is not None and (not self.wantsState or binding.hydrated)
        next: RefStatus = "live" if live else "loading"
        if next == self.status:
            return
        self.status = next
        self._notify()
        if live:
            if self._settleReady is not None:
                self._settleReady.resolve()
            self._settleReady = None

    def end(self, error: Any) -> None:
        if self.ended:
            return
        released = isinstance(error, ChordError) and error.code == "released"
        self.status = "released" if released else "gone"
        if not released:
            self.error = error
        self._notify()
        if self._settleReady is not None:
            self._settleReady.reject(error)
        self._settleReady = None
        for readable in list(self._readables.values()):
            readable.stop()

    def shapeChanged(self) -> None:
        shape = self.binding.shape
        if shape is None or self.ended:
            return
        for listener in list(self.shapeListeners):
            try:
                listener(shape)
            except Exception as error:  # noqa: BLE001 - a listener's failure is reported
                self.binding.host.report(error)

    def _notify(self) -> None:
        notifyStatus(self._statusListeners, self.status, self.error, lambda error: self.binding.host.report(error))

    # ─── members ───

    def member(self, name: str) -> Any:
        description = next(
            (members[name] for members in self.contract.description["variants"].values() if members.get(name) is not None),
            None,
        )
        if description is not None and description["kind"] == "method":
            return self._caller(name)
        if description is not None and description["kind"] == "state":
            readable = self._readables.get(name)
            if readable is None:
                readable = ViewReadable(self, name)
                self._readables[name] = readable
            return readable
        return None

    def _caller(self, name: str) -> Callable[..., Any]:
        caller = self._callers.get(name)
        if caller is None:
            noArgs = takesNoArgs(self.contract, name)

            def caller(*given: Any) -> asyncio.Future[Any]:
                # A method without arguments is called as `ref.name(ctx)`.
                if noArgs:
                    args, ctx = UNDEFINED, given[0] if len(given) > 0 else None
                else:
                    args = given[0] if len(given) > 0 else UNDEFINED
                    ctx = given[1] if len(given) > 1 else None
                host = self.binding.host
                if host.inFactory > 0:
                    raise ChordError("not_ready", f"{self.address}.{name} called inside a factory")
                # The same checks as a local reference's caller (RefImpl.caller and ObjectImpl.call): a ctx from another
                # host or one not made by Chord would carry a root this host does not own over its link.
                if ctx is not None and not isinstance(ctx, CtxImpl):
                    return rejected(invalid("foreign_ctx", "ctx must come from a Chord host"))
                try:
                    if ctx is not None:
                        ctx.check(host)  # type: ignore[arg-type]
                except Exception as error:  # noqa: BLE001 - a rejected call, as upstream
                    return rejected(error)
                if ctx is None or ctx.holder:
                    return host.root(lambda root: self._call(name, args, root), {"signal": None if ctx is None else ctx.signal})
                return self._call(name, args, ctx)

            self._callers[name] = caller
        return caller

    def _call(self, name: str, args: Any, ctx: Ctx) -> asyncio.Future[Any]:
        if self.ended:
            return rejected(self.terminalError())
        return self.binding.call(name, args, ctx)


__all__ = ["MemberState", "RemoteBinding", "RemoteChannel", "RemoteHost", "RemoteRef", "StateIn", "ViewReadable"]
