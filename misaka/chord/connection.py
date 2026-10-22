"""One end of a link on a host: consumer of remote references, provider for the peer's holds, and relay; flow
control by acknowledgements.

Translated from pi's ``packages/chord/src/connection.ts``. Frames are dicts (an absent key is ``undefined``); a
promise is an ``asyncio.Future`` (``misaka.chord._promise``); ``Date.now()`` is ``time.time()`` in milliseconds;
JavaScript ``Map``\\ s and ``Set``\\ s are insertion-ordered ``dict``\\ s.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict

from misaka.chord._promise import (
    Deferred,
    background,
    deferred,
    race,
    rejected,
    resolved,
)
from misaka.chord.contract import LOCAL_VARIANT, contractFromDescription, resolveHost
from misaka.chord.delta import Encoder, encoder
from misaka.chord.errors import ChordError, invalid
from misaka.chord.link import Link, Packed, unpack
from misaka.chord.object import CtxImpl, RefImpl, variantOf
from misaka.chord.schema import assertValid
from misaka.chord.wire import (
    PROTOCOL_VERSION,
    Frame,
    RootRef,
    fromWireError,
    isFrame,
    toWireError,
)
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    AbortSignal,
    clearInterval,
    clearTimeout,
    jsLength,
    jsonStringify,
    setInterval,
    setTimeout,
)

if TYPE_CHECKING:
    from misaka.chord.host import HostImpl
    from misaka.chord.remote_ref import RemoteBinding
    from misaka.chord.types import Ctx, Readable, Shape, StateEvent


class LinkOptions(TypedDict, total=False):
    # Milliseconds between pings (default 15000).
    pingInterval: float
    # Close the link when nothing arrived for this long (default 45000). Everything held through it is released.
    pingTimeout: float
    # Unacknowledged frame bytes (JSON text length) beyond which state frames wait for the peer (default 256 KiB). Only
    # applies when both ends acknowledge frames and the link reports sizes; `inf` never holds state back.
    window: float
    # Bytes of state ops that may wait for the peer on this link (default 4 windows). Beyond, the oldest waiting
    # streams are replaced by their current value (a reset), so memory stays bounded however long the peer stalls.
    pendingCap: float
    # The peer speaks another link protocol version (`peer`: None if it did not say): the link closes, and should not
    # be reconnected with the same code. A browser reloads its page, for instance.
    onProtocolMismatch: Callable[[dict[str, Any]], None]


DEFAULT_WINDOW: Final = 256 * 1024
# The default cap of waiting state ops per link, in windows.
PENDING_WINDOWS: Final = 4

# A `state` frame.
type StateFrame = Frame


@dataclass(frozen=True)
class Resend:
    """How a waiting stream recovers when its ops must be dropped: the provider resends the current value as a reset
    at flush."""

    resend: Callable[[], None]


@dataclass(frozen=True)
class Resync:
    """A relay (which has no value) asks downstream for one."""

    resync: Callable[[], None]


type Overflow = Resend | Resync


@dataclass
class UnitSlot:
    """A unit slot is lazy: the shape and snapshots are built from the current values when it is sent."""

    hold: int
    flush: Callable[[], None]
    kind: Literal["unit"] = "unit"


@dataclass
class StateSlot:
    """A state slot keeps a snapshot, reset or error (``base``) and encoded ops to apply after it, in order (as they
    arrived; the receiving decoder validates them), or, once ``lazy``, only the promise of a reset built at flush."""

    hold: int
    m: str
    base: StateFrame | None
    ops: list[Sequence[Any]]
    size: int
    lazy: bool
    overflow: Overflow
    kind: Literal["state"] = "state"


type Slot = UnitSlot | StateSlot


class AcceptOptions(LinkOptions, total=False):
    # The principal of whatever connects, from the app's authenticator. Every request on the link acts as it.
    principal: Any
    # What the peer is, decided here and never by its `hello`: `client` (default; a browser: no roots, no code
    # versions, not a reload participant, not addressable) or `host` (a child host; needs `name`).
    kind: Literal["host", "client"]
    # A child host's name: its hello must match, and it is reachable `at` this name.
    name: str
    # On `invalidatePrincipal`: a new principal keeps the link, `None` closes it (default: close).
    restamp: Callable[[Any], Any]
    # Calls the peer may have in progress on this link (default: no limit). Further ones never run: they are answered
    # at once with `unavailable` (reason `too_many_calls`), and the link stays open.
    maxCalls: int


@dataclass(frozen=True)
class Peer:
    kind: Literal["host", "client"]
    # Configured (downstream hosts) or announced by the trusted parent (upstream).
    name: str | None


@dataclass
class Feed:
    """A state member's stream to the consumer: its subscription and delta encoder."""

    unsubscribe: Callable[[], Any]
    encoder: Encoder
    # Events that arrived while a unit or reset was being built (replayed after it is sent).
    buffer: list[StateEvent[Any]] | None


@dataclass
class ServedLocal:
    """A hold the peer made on this host and serves locally."""

    id: int
    ref: RefImpl
    principal: Any
    # The client connection it comes from (see `Frame` hold `origin`); None for a host's own holds.
    origin: str | None
    # The consumer wants state (hold without `state: false`, or `sub`).
    wantsState: bool
    # The consumer had values before (re-hold): its first unit waits for the window like any other.
    again: bool
    # A unit was sent.
    unitSent: bool
    # The members whose snapshot the consumer has, with their streams.
    feeds: dict[str, Feed] = field(default_factory=dict)
    kind: Literal["local"] = "local"


@dataclass
class ServedRelay:
    relay: RelayRef
    kind: Literal["relay"] = "relay"


# A hold the peer made on this host: served locally or relayed to another link.
type Served = ServedLocal | ServedRelay


@dataclass(frozen=True)
class Pending:
    resolve: Callable[[Any], None]
    reject: Callable[[Any], None]


def unavailable(message: str) -> ChordError:
    return ChordError("unavailable", message)


# ─── relay ───────────────────────────────────────────────────────────────────


class RelayRef:
    """A hold relayed from an origin link to a downstream link. It owns everything done through it: state and calls
    queued while the route resolves, and the downstream twins once forwarded. ``close`` undoes all of it exactly once
    (cancels calls downstream, drops what waits for the origin, releases the downstream hold, rejects waiters)."""

    def __init__(self, origin: Connection, frame: Frame, principal: Any, originTag: str | None) -> None:
        self.origin = origin
        self.frame = frame
        self.principal = principal
        # The client connection the hold comes from (forwarded downstream); None for a host's own holds.
        self.originTag = originTag
        # The origin's authorization generation when the hold arrived; a revocation bumps it.
        self.generation = origin.generation
        self.down: Connection | None = None
        self.downId = 0
        self.closed = False
        # The origin wants state (forwarded as `state: false`, `sub`, `unsub`).
        self.stateWanted = frame.get("state") is not False
        # The members of the first unit, still to forward past the origin's window.
        self.urgent: set[str] | None = None
        self._shaped = False
        # Members whose ops this relay dropped: downstream ops are ignored until their reset arrives.
        self.awaiting: set[str] = set()
        self.calls: dict[int, RelayCall] = {}
        self._waiters: list[Deferred[Connection]] = []

    @property
    def originId(self) -> int:
        return self.frame["id"]

    @property
    def client(self) -> bool:
        return self.originTag is not None

    @property
    def current(self) -> bool:
        """Still authorized as it was when it arrived."""
        return not self.closed and self.origin.open and self.origin.generation == self.generation

    def ready(self) -> asyncio.Future[Connection]:
        """The downstream connection once the route resolved; rejects if the relay closes first."""
        if self.closed:
            return rejected(ChordError("gone", "relayed hold closed"))
        if self.down is not None:
            return resolved(self.down)
        waiter: Deferred[Connection] = deferred()
        self._waiters.append(waiter)
        return waiter.promise

    def open(self, down: Connection) -> None:
        """Forwards the hold to ``down``."""
        if self.closed:
            return
        self.down = down
        self.downId = down.nextId()
        down.relayedRefs[self.downId] = self
        frame: Frame = {
            "t": "hold",
            "id": self.downId,
            "at": self.frame["at"],
            "token": self.frame["token"],
            "input": self.frame.get("input", UNDEFINED),
            "principal": self.principal,
        }
        if self.originTag is not None:
            frame["origin"] = self.originTag
        if not self.stateWanted:
            frame["state"] = False
        if self.frame.get("again") is True:
            frame["again"] = True
        down.send(frame)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.resolve(down)

    def wantState(self, wanted: bool) -> None:
        """The origin starts or stops the hold's state."""
        if self.closed or wanted == self.stateWanted:
            return
        self.stateWanted = wanted
        if not wanted:
            self.origin.dropHoldState(self.originId)
            self.awaiting.clear()
        if self.down is not None:
            self.down.send({"t": "sub" if wanted else "unsub", "hold": self.downId})

    def resync(self, m: str) -> None:
        """The origin could not keep up and dropped what waited for member ``m`` (or asked for it again): ask
        downstream for a reset, and ignore its ops until the reset arrives."""
        if self.closed:
            return
        self.awaiting.add(m)
        if self.down is not None:
            self.down.send({"t": "resync", "hold": self.downId, "m": m})

    def shape(self, frame: Frame) -> None:
        """A shape from downstream: forwarded at once; the members of the first unit then skip the origin's window."""
        if self.closed:
            return
        if not self._shaped:
            self._shaped = True
            if self.frame.get("again") is not True:
                self.urgent = set(frame["state"])
        self.origin.send({**frame, "id": self.originId})

    def state(self, frame: StateFrame) -> None:
        """A state frame from downstream."""
        if self.closed:
            return
        m = frame["m"]
        if m in self.awaiting:
            if frame["as"] == "ops":
                return
            self.awaiting.discard(m)
        out: StateFrame = {**frame, "hold": self.originId}
        if self.urgent is not None and m in self.urgent:
            self.urgent.discard(m)
            self.origin.send(out)
            return
        self.origin.sendState(out, Resync(lambda: self.resync(m)))

    def close(self, error: Any, releaseDown: bool, notify: bool) -> None:
        """Closes the relay. ``releaseDown``: the downstream hold is still there (origin released, disconnected, or
        was revoked): cancel its calls and release it. ``notify``: tell the origin the hold is gone."""
        if self.closed:
            return
        self.closed = True
        for call in list(self.calls.values()):
            call.cancel(error, releaseDown)
        self.origin.dropHoldState(self.originId)
        served = self.origin.served.get(self.originId)
        if served is not None and served.kind == "relay":
            del self.origin.served[self.originId]
        if self.down is not None:
            self.down.relayedRefs.pop(self.downId, None)
            if releaseDown:
                self.down.send({"t": "release", "id": self.downId})
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.reject(error)
        if notify:
            self.origin.send({"t": "gone", "id": self.originId, "error": toWireError(error)})


class RelayCall:
    """One relayed call: settles once, by result, cancellation or the relay closing; removes both mappings."""

    def __init__(self, relay: RelayRef, originId: int, downId: int, pending: Pending) -> None:
        self.relay = relay
        self.originId = originId
        self.downId = downId
        self._pending = pending
        self._settled = False
        relay.calls[originId] = self
        if relay.down is not None:
            relay.down.relayedCalls[downId] = self

    def finish(self, error: Any, value: Any = UNDEFINED) -> None:
        if self._settled:
            return
        self._settled = True
        self.relay.calls.pop(self.originId, None)
        if self.relay.down is not None:
            self.relay.down.relayedCalls.pop(self.downId, None)
        if error is None:
            self._pending.resolve(value)
        else:
            self._pending.reject(error)

    def cancel(self, error: Any, forward: bool) -> None:
        """Cancels downstream (when the downstream hold still exists) and settles with ``error``."""
        if self._settled:
            return
        if forward and self.relay.down is not None:
            self.relay.down.send({"t": "cancel", "id": self.downId})
        self.finish(error)


# ─── connection ──────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time() * 1000


class Connection:
    """One end of a link on a host: consumer of remote references, provider for the peer's holds, and relay.

    State
    -----

    A hold implies the state of its variant (unless it says ``state: false``). The provider answers with a unit: the
    shape, then a snapshot of each state member the consumer does not have yet (``shape.state`` lists them), then the
    members' ops and resets. A reload or a variant change sends another unit with the members that are new; members
    that left the variant stop. The consumer applies a shape once its snapshots are here (``RemoteBinding``).

    Flow control
    ------------

    Problem: state is pushed as fast as it changes, but a peer may read slowly (a phone behind a relay), and a relay
    may hide that: Cloudflare forwards to browsers without backpressure. Everything sent then queues in front of the
    next call result. Example: a browser opens three long transcripts (500 KiB of snapshots each) over a 5 Mbit/s
    path; a result sent next waits 2.4 s, and the snapshot of a transcript the user already left is still sent in full.

    Solution: the peer acknowledges what it processed (``ack``, a cumulative frame count), and the sender counts the
    sizes of the frames not yet acknowledged. While they exceed the window, state waits here in slots, flushed in
    creation order as acknowledgements come:
    - a member's stream has one slot: ops are appended (encoded batches of one stream may be concatenated, see
      ``encoder``), a snapshot, reset or error replaces it;
    - a hold's unit has one slot, built lazily from the current values when it is sent, so a newer unit merges into
      it; while it waits, the members it introduces have no stream yet.

    The first unit of a hold that is not a re-hold (``again``) is sent at once, past the window: a ``get`` waits for
    it, and must not wait behind other holds' state. Results, shapes, gone, reload control and pings are sent at once
    too: they are bounded by the requests the peer makes. Hence the one ordering exception of a link: while state
    waits, a result can arrive before state changes made before it. Frames of one stream stay in order.

    Waiting ops are capped per link (``pendingCap``): beyond, the oldest streams are replaced by their current value (a
    reset built at flush), or, on a relay, by a reset asked of the provider (``resync``), so memory stays bounded
    however long the peer stalls. The window bounds what is unacknowledged when a frame is sent, so one frame (a large
    snapshot) may exceed it. Both hellos must announce ``acks``; with an older peer nothing is acknowledged and nothing
    waits. Links that do not report sizes (IPC) never fill the window.
    """

    def __init__(self, host: HostImpl, link: Link, config: Mapping[str, Any]) -> None:
        """``config``: ``role``, ``principal``, ``peer``, and optionally ``originId``, ``restamp``, ``maxCalls`` and
        the ``LinkOptions``."""
        options = config
        self.host = host
        self.link = link
        # `upstream`: the link to this host's parent. `downstream`: a link this host accepted.
        self.role: Literal["upstream", "downstream"] = config["role"]
        self.peer: Peer = config["peer"]
        # A client connection's opaque id: holds that come over it (directly or relayed) carry it as their `origin`.
        self.originId: str | None = config.get("originId")
        self.principal = config["principal"]
        self.restamp: Callable[[Any], Any] | None = config.get("restamp")
        maxCalls = config.get("maxCalls")
        # Bumped when the stamped principal is revoked or re-stamped: relays from before are refused.
        self.generation = 0
        # The code version a downstream host peer runs (from its hello, then from commits).
        self.peerCodeVersion: int | None = None
        # A downstream host peer is usable once it runs this host's current code version (see `HostImpl.peerHello`).
        self.synced = not (self.role == "downstream" and self.peer.kind == "host")
        # Code versions this child announced while behind this host (fenced): this host never ran them.
        self.fencedCodeVersions: set[int] = set()
        # Roots of fenced code versions, by the code version of this host they were admitted in.
        self._admitted: dict[str, int] = {}
        self._parentName: str | None = None
        self._syncWaiters: list[Deferred[None]] = []
        self._nextId = 1
        self._lastSeen = _now()
        self._pinger: Any = None
        self._maxCalls = float("inf") if maxCalls is None else maxCalls
        # flow control (see the class comment)
        self._window = options.get("window", DEFAULT_WINDOW)
        self._pendingCap = options.get("pendingCap", self._window * PENDING_WINDOWS)
        # Whether the peer acknowledges frames; until its hello is here, frames are counted in case it does.
        self._peerAcks: bool | None = None
        self._sent = 0
        self._acked = 0
        # Sizes of the frames sent after the last acknowledged one.
        self._unacked: list[int] = []
        self._unackedBytes = 0
        self._pending: dict[str, Slot] = {}
        # Bytes of ops waiting in `_pending`.
        self._pendingBytes = 0
        self._received = 0
        self._ackTimer: Any = None
        self._onProtocolMismatch = options.get("onProtocolMismatch")
        # The peer speaks another protocol version: nothing it sends is processed any more.
        self._mismatched = False
        # consumer side: what this host asked of the peer
        self.remotes: dict[int, RemoteBinding] = {}
        self.calls: dict[int, Pending] = {}
        # provider side: what the peer asked of this host
        self.served: dict[int, Served] = {}
        self.incoming: dict[int, AbortController] = {}
        # downstream side of relays: ids this host used on this link
        self.relayedRefs: dict[int, RelayRef] = {}
        self.relayedCalls: dict[int, RelayCall] = {}
        # Reload votes awaited from this (child) peer.
        self.votes: dict[int, Pending] = {}
        link.onClose(self._closed)
        link.receive(self._receive)
        self.send(
            {
                "t": "hello",
                "protocol": PROTOCOL_VERSION,
                "name": host.name,
                "kind": host.kind,
                "codeVersion": host.current.id,
                "acks": True,
            }
        )
        interval = options.get("pingInterval", 15_000)
        timeout = options.get("pingTimeout", 45_000)
        # A link that was already closed closed this connection above; it needs no keepalive.
        if link.closed:
            return
        n = [0]

        def ping() -> None:
            if _now() - self._lastSeen > timeout:
                link.close("ping timeout")
            else:
                n[0] += 1
                self.send({"t": "ping", "n": n[0]})

        self._pinger = setInterval(ping, interval)

    @property
    def open(self) -> bool:
        return not self.link.closed

    @property
    def name(self) -> str | None:
        """The peer's name: configured for downstream hosts, announced by the parent upstream."""
        return self._parentName if self.role == "upstream" else self.peer.name

    def send(self, frame: Frame) -> None:
        size = self.link.send(frame) or 0
        if self._peerAcks is False:
            return
        self._sent += 1
        self._unacked.append(size)
        self._unackedBytes += size

    def sendState(self, frame: StateFrame, overflow: Overflow) -> None:
        """Sends a state frame, or keeps it waiting (see the class comment)."""
        key = stateKey(frame["hold"], frame["m"])
        slot = self._pending.get(key)
        if slot is None and not self._full:
            self.send(frame)
            return
        if slot is not None and slot.kind == "unit":
            return
        if slot is None:
            slot = StateSlot(frame["hold"], frame["m"], None, [], 0, False, overflow)
            self._pending[key] = slot
        assert isinstance(slot, StateSlot)
        if frame["as"] != "ops":
            self._pendingBytes -= slot.size
            slot.base = frame
            slot.ops = []
            slot.size = 0
            slot.lazy = False
            return
        # A reset built at flush already covers these ops.
        if slot.lazy:
            return
        ops = frame.get("ops") or []
        size = jsLength(jsonStringify(ops))
        if self._pendingBytes + size > self._pendingCap:
            self._makeRoom(size, slot)
        if slot.lazy or self._pending.get(key) is not slot:
            return
        slot.ops.extend(ops)
        slot.size += size
        self._pendingBytes += size

    def _makeRoom(self, size: int, current: StateSlot) -> None:
        """Replaces the oldest waiting streams by resets until ``size`` more bytes fit; ``current`` last, if needed."""
        for slot in list(self._pending.values()):
            if self._pendingBytes + size <= self._pendingCap:
                return
            if slot.kind == "state" and slot is not current and slot.size > 0:
                self._drop(slot)
        if self._pendingBytes + size > self._pendingCap:
            self._drop(current)

    def _drop(self, slot: StateSlot) -> None:
        """Drops what waits for a stream: a lazy reset replaces it (provider), or the relay asks downstream for one."""
        self._pendingBytes -= slot.size
        slot.base = None
        slot.ops = []
        slot.size = 0
        if isinstance(slot.overflow, Resend):
            slot.lazy = True
            return
        self._pending.pop(stateKey(slot.hold, slot.m), None)
        slot.overflow.resync()

    def _resetLater(self, hold: int, m: str, resend: Callable[[], None]) -> None:
        """Sends member ``m`` of a served hold again as a reset, through the window (``resync``)."""
        key = stateKey(hold, m)
        slot = self._pending.get(key)
        if slot is not None and slot.kind == "unit":
            return
        if slot is None:
            self._pending[key] = StateSlot(hold, m, None, [], 0, True, Resend(resend))
        else:
            assert isinstance(slot, StateSlot)
            self._pendingBytes -= slot.size
            slot.base = None
            slot.ops = []
            slot.size = 0
            slot.lazy = True
        self._flush()

    def _queueUnit(self, hold: int, flush: Callable[[], None]) -> None:
        """Queues a hold's lazy unit (a waiting one already covers this one), and sends what the window allows."""
        key = unitKey(hold)
        if key not in self._pending:
            self._pending[key] = UnitSlot(hold, flush)
        self._flush()

    def dropState(self, hold: int, m: str) -> None:
        """Forgets what waits for a stream that ended."""
        slot = self._pending.get(stateKey(hold, m))
        if slot is None or slot.kind != "state":
            return
        self._pendingBytes -= slot.size
        del self._pending[stateKey(hold, m)]

    def dropHoldState(self, hold: int) -> None:
        """Forgets everything that waits for a hold (released, closed, or its state stopped)."""
        for key, slot in list(self._pending.items()):
            if slot.hold != hold:
                continue
            if slot.kind == "state":
                self._pendingBytes -= slot.size
            del self._pending[key]

    @property
    def waiting(self) -> dict[str, int]:
        """What waits for the peer: slots and bytes of ops (for tests and diagnostics)."""
        return {"slots": len(self._pending), "bytes": self._pendingBytes}

    @property
    def _full(self) -> bool:
        return self._peerAcks is True and self._unackedBytes >= self._window

    def _onAck(self, n: int) -> None:
        if self._peerAcks is not True or n < self._acked or n > self._sent:
            self._violation(f"ack {n} outside {self._acked}..{self._sent}")
            return
        count = n - self._acked
        for size in self._unacked[:count]:
            self._unackedBytes -= size
        del self._unacked[:count]
        self._acked = n
        self._flush()

    def _flush(self) -> None:
        """Sends waiting slots in creation order while the window allows."""
        while self._pending:
            if self._full:
                return
            key, slot = next(iter(self._pending.items()))
            del self._pending[key]
            if slot.kind == "unit":
                slot.flush()
                continue
            self._pendingBytes -= slot.size
            if slot.lazy:
                if isinstance(slot.overflow, Resend):
                    slot.overflow.resend()
                continue
            if slot.base is not None:
                self.send(slot.base)
            if len(slot.ops) > 0:
                self.send({"t": "state", "hold": slot.hold, "m": slot.m, "as": "ops", "ops": slot.ops})

    def _scheduleAck(self) -> None:
        """Acknowledges once the frames that arrived together are processed: one ack per burst, not per frame."""
        if self._peerAcks is not True or self._ackTimer is not None or not self.open:
            return

        def ack() -> None:
            self._ackTimer = None
            self.link.send({"t": "ack", "n": self._received})

        self._ackTimer = setTimeout(ack, 0)

    def nextId(self) -> int:
        id = self._nextId
        self._nextId += 1
        return id

    def whenSynced(self) -> asyncio.Future[None]:
        """Resolves once the peer is usable (a host peer runs this host's code version); rejects if the link closes
        first."""
        if not self.open:
            return rejected(unavailable(f"link to {self.name or 'peer'} closed"))
        if self.synced:
            return resolved()
        waiter: Deferred[None] = deferred()
        self._syncWaiters.append(waiter)
        return waiter.promise

    def markSynced(self, codeVersion: int) -> None:
        self.peerCodeVersion = codeVersion
        self.synced = True
        waiters, self._syncWaiters = self._syncWaiters, []
        for waiter in waiters:
            waiter.resolve()

    def revoke(self, next: Any) -> None:
        """Revokes the stamped principal: relays made under it close at once (also those still resolving a route)."""
        self.generation += 1
        error = ChordError("gone", "principal invalidated")
        for served in list(self.served.values()):
            if served.kind == "relay":
                served.relay.close(error, releaseDown=True, notify=True)
        if next is None:
            self.link.close("principal invalidated")
        else:
            self.principal = next

    def _protocolMismatch(self, peer: int | None, close: bool = True) -> None:
        """Stops talking to a peer of another protocol version: reports it and closes the link. The close reason tells
        the peer this side's version (``protocol-version <n>``); a peer that noticed first told us the same way
        (``_closed``)."""
        if self._mismatched:
            return
        self._mismatched = True
        try:
            if self._onProtocolMismatch is not None:
                self._onProtocolMismatch({"local": PROTOCOL_VERSION, "peer": peer})
        except Exception as error:  # noqa: BLE001 - the app's callback must not break the close
            self.host.report(error)
        if close:
            self.link.close(f"protocol-version {PROTOCOL_VERSION}")

    def _violation(self, reason: str) -> None:
        """A protocol violation: the peer cannot be trusted with anything on this link any more."""
        self.host.report(ChordError("invalid", f"link to {self.name or 'peer'}: {reason}", {"reason": "protocol"}))
        self.link.close(f"protocol error: {reason}")

    # ─── consumer side (RemoteChannel) ───

    def hold(self, binding: RemoteBinding, again: bool) -> int:
        id = self.nextId()
        self.remotes[id] = binding
        binding.bound(self, id)
        principal = binding.principal
        frame: Frame = {
            "t": "hold",
            "id": id,
            "at": binding.at,
            "token": binding.contract.description,
            "input": binding.input,
        }
        if principal is not None:
            frame["principal"] = principal
        if not binding.stateOn:
            frame["state"] = False
        if again:
            frame["again"] = True
        self.send(frame)
        return id

    def release(self, id: int) -> None:
        self.remotes.pop(id, None)
        self.send({"t": "release", "id": id})

    def wantState(self, hold: int, wanted: bool) -> None:
        if hold in self.remotes:
            self.send({"t": "sub" if wanted else "unsub", "hold": hold})

    def call(self, hold: int, member: str, args: Any, root: RootRef | None, signal: AbortSignal) -> asyncio.Future[Any]:
        id = self.nextId()
        result: Deferred[Any] = deferred()
        if signal.aborted:
            result.reject(ChordError("aborted", "call aborted"))
            return result.promise

        def onAbort() -> None:
            self.calls.pop(id, None)
            self.send({"t": "cancel", "id": id})
            result.reject(ChordError("aborted", "call aborted", {"cause": signal.reason}))

        signal.addEventListener("abort", onAbort, once=True)

        def done() -> None:
            signal.removeEventListener("abort", onAbort)

        def resolve(value: Any) -> None:
            done()
            result.resolve(value)

        def reject(error: Any) -> None:
            done()
            result.reject(error)

        self.calls[id] = Pending(resolve, reject)
        # Clients never carry roots or code versions: the server starts roots for them.
        carried = root is not None and self.host.kind == "host"
        if carried:
            assert root is not None
            self.host.rootSent(root["rootId"], self)
        frame: Frame = {"t": "call", "id": id, "hold": hold, "member": member}
        if args is not UNDEFINED:
            frame["args"] = args
        if carried:
            frame["root"] = root
        self.send(frame)
        return result.promise

    # ─── receiving ───

    def _receive(self, frame: Any) -> None:
        if self._mismatched:
            return
        self._lastSeen = _now()
        if not isFrame(frame):
            self._violation("malformed frame")
            return
        if frame["t"] == "ack":
            self._onAck(frame["n"])
            return
        self._received += 1
        try:
            self._dispatch(frame)
        except Exception as error:  # noqa: BLE001 - a failing frame is reported; the link goes on
            self.host.report(error)
        self._scheduleAck()

    def _dispatch(self, frame: Frame) -> None:
        t = frame["t"]
        if t == "hello":
            self._onHello(frame)
        elif t == "ping":
            self.send({"t": "pong", "n": frame["n"]})
        elif t in ("pong", "bye", "ack"):
            return
        elif t == "hold":
            self._onHold(frame)
        elif t == "release":
            self._dropServed(frame["id"], True)
        elif t == "call":
            self._onCall(frame)
        elif t == "cancel":
            controller = self.incoming.get(frame["id"])
            if controller is not None:
                controller.abort(ChordError("aborted", "cancelled by caller"))
        elif t in ("sub", "unsub"):
            self._onWantState(frame["hold"], t == "sub")
        elif t == "resync":
            self._onResync(frame["hold"], frame["m"])
        elif t in ("shape", "gone"):
            self._onHoldReply(frame)
        elif t == "result":
            self._onResult(frame)
        elif t == "state":
            self._onState(frame)
        elif t == "root_end":
            # Honored only from the link the root came over (see HostImpl.foreignRootEnded).
            self._admitted.pop(frame["rootId"], None)
            self.host.foreignRootEnded(frame["rootId"], self)
        elif t in ("invalidate", "prepare", "commit", "abort", "retire"):
            # Control frames only from the parent.
            if self.role != "upstream":
                self._violation(f"{t} from a child")
                return
            if t == "invalidate":
                background(self.host.invalidatePrincipal(frame["pattern"]), self.host.report)
            elif t == "retire":
                self.host.allowRetire(frame["codeVersion"])
            else:
                self.host.onReloadFrame(self, frame)
        elif t == "vote":
            if self.role != "downstream" or self.peer.kind != "host":
                self._violation("vote from a non-participant")
                return
            vote = self.votes.pop(frame["reload"], None)
            if "error" not in frame:
                if vote is not None:
                    vote.resolve(frame.get("report"))
            elif vote is not None:
                vote.reject(fromWireError(frame["error"]))

    def _onHello(self, frame: Frame) -> None:
        """The peer's hello must agree with what this side decided about it. A downstream host announces its code
        version: the host either finds it in step or fences and reconciles it (never renumbers)."""
        protocol = frame.get("protocol", 1)
        if protocol != PROTOCOL_VERSION:
            self._protocolMismatch(protocol)
            return
        if self._peerAcks is None:
            self._peerAcks = frame.get("acks") is True
            if not self._peerAcks:
                self._unacked = []
                self._unackedBytes = 0
        if self.role == "upstream":
            if frame["kind"] != "host":
                self._violation("parent is not a host")
                return
            if self._parentName is None:
                self._parentName = frame["name"]
            return
        if frame["kind"] != self.peer.kind or (self.peer.name is not None and frame["name"] != self.peer.name):
            self._violation(
                f"hello {frame['kind']} {frame['name']} does not match the accepted {self.peer.kind} {self.peer.name or ''}"
            )
            return
        if self.peer.kind == "host":
            self.host.peerHello(self, frame["codeVersion"])

    def _principalOf(self, frame: Frame) -> Any:
        """Principal stamping: a downstream link acts as the principal it was accepted with, whatever the frame says.
        Only the parent may forward a principal (relayed requests); its own requests act as the principal given to
        ``connect``."""
        if self.role == "downstream":
            return self.principal
        principal = frame.get("principal")
        return self.principal if principal is None else principal

    def _originOf(self, frame: Frame) -> str | None:
        """The client connection a hold comes from: this link's own id for a client link (whatever the frame says), the
        id the parent forwarded for a relayed hold, none for a host's own holds."""
        return self.originId if self.role == "downstream" else frame.get("origin")

    def _onHold(self, frame: Frame) -> None:
        if frame["id"] in self.served:
            self._violation(f"duplicate hold id {frame['id']}")
            return
        principal = self._principalOf(frame)
        origin = self._originOf(frame)
        client = origin is not None
        if frame["at"] != self.host.name:
            if self.role == "upstream":
                self.send({"t": "gone", "id": frame["id"], "error": toWireError(unavailable(f"{self.host.name} does not relay"))})
                return
            relay = RelayRef(self, frame, principal, origin)
            self.served[frame["id"]] = ServedRelay(relay)
            background(
                self.host.relay(relay),
                lambda error: relay.close(error, releaseDown=True, notify=True),
            )
            return
        try:
            token = contractFromDescription(frame["token"])
            if token.scope != "remote":
                raise invalid("token", f"{token.name} is local; only remote types cross links")
            # Addressed here, so the input must place it here (also by this host's contract, checked when acquiring).
            if resolveHost(token.name, token.description["host"], frame.get("input", UNDEFINED)) != self.host.name:
                raise invalid("placement", f"{token.name} with this input does not live on {self.host.name}")
            acquired = self.host.acquire(
                token, frame.get("input", UNDEFINED), {"principal": principal}, "client" if client else "connection"
            )
            if not isinstance(acquired, RefImpl):
                raise unavailable("not a local reference")
            ref = acquired
        except Exception as error:  # noqa: BLE001 - every refusal is answered with `gone`
            self.send({"t": "gone", "id": frame["id"], "error": toWireError(clientError(error) if client else error)})
            return
        served = ServedLocal(
            id=frame["id"],
            ref=ref,
            principal=principal,
            origin=origin,
            wantsState=frame.get("state") is not False,
            again=frame.get("again") is True,
            unitSent=False,
        )
        self.served[frame["id"]] = served
        ref.onShape(lambda _shape: self._unit(served))

        def onGone(error: Any) -> None:
            if self.served.get(frame["id"]) is not served:
                return
            self._dropServed(frame["id"], False)
            self.send({"t": "gone", "id": frame["id"], "error": toWireError(error)})

        ref.goneListeners[onGone] = None
        if ref.status == "live":
            self._unit(served)

    def _stateMembers(self, ref: RefImpl, shape: Shape) -> list[str]:
        """The state members of ``shape`` the consumer's contract knows."""
        result = []
        for name in shape["members"]:
            description = ref.describe(shape.get("variant"), name)
            if description is not None and description["kind"] == "state":
                result.append(name)
        return result

    def _unit(self, served: ServedLocal) -> None:
        """The shape changed (live, reload, variant) or state was asked for: members that left lose their stream, and a
        unit goes out with the shape and the members the consumer lacks. The first unit of a hold (not a re-hold) is
        urgent."""
        if self.served.get(served.id) is not served:
            return
        shape = served.ref.shape
        if shape is None:
            return
        members = set(self._stateMembers(served.ref, shape) if served.wantsState else [])
        for name, feed in list(served.feeds.items()):
            if name in members:
                continue
            feed.unsubscribe()
            del served.feeds[name]
            self.dropState(served.id, name)
        urgent = len(served.feeds) == 0 and not (served.again and not served.unitSent)
        if urgent:
            self._sendUnit(served)
        else:
            self._queueUnit(served.id, lambda: self._sendUnit(served))

    def _sendUnit(self, served: ServedLocal) -> None:
        """Builds and sends a unit from the current shape and values."""
        self._pending.pop(unitKey(served.id), None)
        if self.served.get(served.id) is not served:
            return
        shape = served.ref.shape
        if shape is None:
            return
        served.unitSent = True
        members = self._stateMembers(served.ref, shape) if served.wantsState else []
        listed = [name for name in members if name not in served.feeds]
        snaps: list[StateFrame] = []
        attached: list[tuple[str, Feed]] = []
        for name in listed:
            attachment = self._attach(served, name)
            if attachment is None:
                continue
            snaps.append(attachment[0])
            attached.append((name, attachment[1]))
        frame: Frame = {"t": "shape", "id": served.id}
        if shape.get("variant") is not None:
            frame["variant"] = shape["variant"]
        frame["members"] = shape["members"]
        frame["contract"] = shape["contract"]
        frame["state"] = [snap["m"] for snap in snaps]
        self.send(frame)
        for snap in snaps:
            self.send(snap)
        for name, feed in attached:
            self._replay(served, name, feed)

    def _attach(
        self, served: ServedLocal, name: str, kind: Literal["snap", "reset"] = "snap"
    ) -> tuple[StateFrame, Feed] | None:
        """Subscribes member ``name`` for the consumer: its snapshot (as a frame of type ``kind``), and a feed whose
        later events wait in its buffer until the snapshot is sent (``_replay``). None if the member is not readable
        state."""
        readable: Readable[Any] | None = served.ref.member(name)
        if readable is None or not callable(getattr(readable, "events", None)):
            return None
        feed = Feed(lambda: None, encoder(), [])
        first: list[StateEvent[Any]] = []

        def onEvent(event: StateEvent[Any]) -> None:
            if not first:
                first.append(event)
            elif feed.buffer is not None:
                feed.buffer.append(event)
            else:
                self._feedEvent(served, name, feed, event)

        feed.unsubscribe = readable.events(onEvent)
        served.feeds[name] = feed
        return self._snapshot(served, name, kind, first[0]["value"] if first else UNDEFINED), feed

    def _replay(self, served: ServedLocal, name: str, feed: Feed) -> None:
        """Sends the events that arrived while the snapshot was built, now that it is sent."""
        buffered = feed.buffer or []
        feed.buffer = None
        for event in buffered:
            self._feedEvent(served, name, feed, event)

    def _snapshot(self, served: ServedLocal, name: str, kind: Literal["snap", "reset"], value: Any) -> StateFrame:
        """A member's state frame for a snapshot or reset: validated against the schema the consumer is promised."""
        try:
            validateSnapshot(served.ref, name, value)
        except Exception as error:  # noqa: BLE001 - sent as the member's error
            return {"t": "state", "hold": served.id, "m": name, "as": "error", "error": toWireError(error)}
        frame: StateFrame = {"t": "state", "hold": served.id, "m": name, "as": kind}
        if value is not UNDEFINED:
            frame["value"] = value
        return frame

    def _feedEvent(self, served: ServedLocal, name: str, feed: Feed, event: StateEvent[Any]) -> None:
        """A later event of a member's stream: ops are encoded, a reset starts a new stream."""
        if served.feeds.get(name) is not feed:
            return
        overflow = Resend(lambda: self._resend(served, name))
        if event["type"] == "ops":
            self.sendState(
                {"t": "state", "hold": served.id, "m": name, "as": "ops", "ops": feed.encoder.encode(event["ops"])},
                overflow,
            )
            return
        feed.encoder = encoder()
        self.sendState(self._snapshot(served, name, "reset", event["value"]), overflow)

    def _resend(self, served: ServedLocal, name: str) -> None:
        """Sends member ``name`` again, now, as a reset from its current value (a lazy slot is flushed, or
        ``resync``)."""
        feed = served.feeds.get(name)
        if feed is None or self.served.get(served.id) is not served:
            return
        feed.unsubscribe()
        del served.feeds[name]
        attachment = self._attach(served, name, "reset")
        if attachment is None:
            return
        self.send(attachment[0])
        self._replay(served, name, attachment[1])

    def _onWantState(self, hold: int, wanted: bool) -> None:
        """The consumer starts (``sub``) or stops (``unsub``) the state of a hold."""
        served = self.served.get(hold)
        if served is None:
            return
        if served.kind == "relay":
            served.relay.wantState(wanted)
            return
        if served.wantsState == wanted:
            return
        served.wantsState = wanted
        if wanted:
            self._unit(served)
            return
        for feed in list(served.feeds.values()):
            feed.unsubscribe()
        served.feeds.clear()
        self.dropHoldState(hold)

    def _onResync(self, hold: int, m: str) -> None:
        """The consumer (a relay that dropped ops) asks for member ``m`` again as a reset."""
        served = self.served.get(hold)
        if served is None:
            return
        if served.kind == "relay":
            served.relay.resync(m)
            return
        if m not in served.feeds:
            return
        self._resetLater(hold, m, lambda: self._resend(served, m))

    def _dropServed(self, id: int, release: bool) -> None:
        """Forgets a served hold: unsubscribes its feeds and releases it (locally or downstream)."""
        served = self.served.pop(id, None)
        if served is None:
            return
        if served.kind == "relay":
            served.relay.close(ChordError("released", "relayed hold released"), releaseDown=release, notify=False)
            return
        for feed in list(served.feeds.values()):
            feed.unsubscribe()
        served.feeds.clear()
        self.dropHoldState(id)
        if release:
            served.ref.release()

    def _onCall(self, frame: Frame) -> None:
        if frame["id"] in self.incoming:
            self._violation(f"duplicate call id {frame['id']}")
            return
        if len(self.incoming) >= self._maxCalls:
            error = ChordError(
                "unavailable",
                f"Too many calls in progress on this connection ({_count(self._maxCalls)}); wait for earlier ones to finish",
                {"reason": "too_many_calls"},
            )
            self.send({"t": "result", "id": frame["id"], "error": toWireError(error)})
            return
        served = self.served.get(frame["hold"])
        if served is None:
            self.send({"t": "result", "id": frame["id"], "error": toWireError(ChordError("gone", "unknown hold"))})
            return
        controller = AbortController()
        self.incoming[frame["id"]] = controller
        # Roots are honored from hosts only; a request without one (or from a client) starts one here.
        root = self._admitRoot(frame.get("root")) if self.peer.kind == "host" else None
        principal = served.principal if served.kind == "local" else served.relay.principal
        options = {"principal": principal, "signal": controller.signal}
        if served.kind == "local":
            local = served

            def run(ctx: Ctx) -> Any:
                assert isinstance(ctx, CtxImpl)
                return local.ref.object.call(local.ref, frame["member"], unpack(frame.get("args", UNDEFINED)), ctx)

        else:
            relay = served.relay

            def run(ctx: Ctx) -> Any:
                # The root's signal: a cancel from the origin and the root's code version retiring both reach it.
                return relayCall(
                    relay, frame, {"rootId": ctx.rootId, "codeVersion": ctx.codeVersion}, ctx.signal, self.host
                )

        result = (
            self.host.root(run, options)
            if root is None
            else self.host.joinRoot(root["rootId"], root["codeVersion"], run, {**options, "from": self})
        )

        def settled(done: asyncio.Future[Any]) -> None:
            try:
                if done.cancelled():
                    self.send({"t": "result", "id": frame["id"], "error": toWireError(ChordError("aborted", "call cancelled"))})
                elif (error := done.exception()) is not None:
                    self.send({"t": "result", "id": frame["id"], "error": toWireError(error)})
                else:
                    value = done.result()
                    reply: Frame = {"t": "result", "id": frame["id"]}
                    if value is not UNDEFINED:
                        reply["value"] = value
                    self.send(reply)
            finally:
                self.incoming.pop(frame["id"], None)

        result.add_done_callback(settled)

    def _admitRoot(self, root: RootRef | None) -> RootRef | None:
        """A child that connected behind this host runs roots of a code version this host never had (it was fenced and
        is being, or was, reconciled). Such a root is admitted in this host's current code version at its first call
        here, and keeps that code version for its later calls (``superseded`` once this host retired it). Other unknown
        code versions stay ``superseded``."""
        if root is None or self.role != "downstream" or root["codeVersion"] not in self.fencedCodeVersions:
            return root
        if self.host.hasCodeVersion(root["codeVersion"]):
            return root
        codeVersion = self._admitted.get(root["rootId"])
        if codeVersion is None:
            codeVersion = self.host.current.id
            self._admitted[root["rootId"]] = codeVersion
        return {"rootId": root["rootId"], "codeVersion": codeVersion}

    def _onHoldReply(self, frame: Frame) -> None:
        relay = self.relayedRefs.get(frame["id"])
        if relay is not None:
            if frame["t"] == "gone":
                relay.close(fromWireError(frame["error"]), releaseDown=False, notify=True)
            else:
                relay.shape(frame)
            return
        binding = self.remotes.get(frame["id"])
        if binding is None:
            return
        if frame["t"] == "gone":
            del self.remotes[frame["id"]]
            binding.gone(fromWireError(frame["error"]))
            return
        binding.shaped(
            {"variant": frame.get("variant"), "members": frame["members"], "contract": frame["contract"]}, frame["state"]
        )

    def _onResult(self, frame: Frame) -> None:
        relayed = self.relayedCalls.get(frame["id"])
        if relayed is not None:
            relayed.finish(None if "error" not in frame else fromWireError(frame["error"]), frame.get("value", UNDEFINED))
            return
        pending = self.calls.pop(frame["id"], None)
        if "error" in frame:
            if pending is not None:
                pending.reject(fromWireError(frame["error"]))
            return
        try:
            value = unpack(frame.get("value", UNDEFINED))
        except Exception as error:  # noqa: BLE001 - an undecodable result rejects its call
            if pending is not None:
                pending.reject(error)
            return
        if pending is not None:
            pending.resolve(value)

    def _onState(self, frame: StateFrame) -> None:
        relay = self.relayedRefs.get(frame["hold"])
        if relay is not None:
            relay.state(frame)
            return
        binding = self.remotes.get(frame["hold"])
        if binding is None:
            return
        value = frame.get("value", UNDEFINED)
        if not isinstance(value, Packed):
            binding.state(frame["m"], frame)
            return
        try:
            unpacked = unpack(value)
        except Exception as error:  # noqa: BLE001 - an undecodable snapshot is the member's error
            binding.state(frame["m"], {"as": "error", "error": toWireError(error)})
            return
        binding.state(frame["m"], {**frame, "value": unpacked})

    # ─── lifetime ───

    def _closed(self, reason: str) -> None:
        told = _PROTOCOL_REASON.match(reason)
        if told is not None and int(told.group(1)) != PROTOCOL_VERSION:
            self._protocolMismatch(int(told.group(1)), False)
        if self._pinger is not None:
            clearInterval(self._pinger)
        if self._ackTimer is not None:
            clearTimeout(self._ackTimer)
        self._pending.clear()
        self._pendingBytes = 0
        error = unavailable(f"link to {self.name or 'peer'} closed: {reason}")
        # As origin: everything the peer held here, relayed or not, is released (downstream calls cancelled).
        for id in list(self.served):
            self._dropServed(id, True)
        for controller in list(self.incoming.values()):
            controller.abort(error)
        self.incoming.clear()
        for pending in list(self.calls.values()):
            pending.reject(error)
        self.calls.clear()
        for vote in list(self.votes.values()):
            vote.reject(error)
        self.votes.clear()
        waiters, self._syncWaiters = self._syncWaiters, []
        for waiter in waiters:
            waiter.reject(error)
        # As downstream of relays: their origins lose the hold.
        for relay in list(self.relayedRefs.values()):
            relay.close(error, releaseDown=False, notify=True)
        self.relayedRefs.clear()
        self.relayedCalls.clear()
        bindings = list(self.remotes.values())
        self.remotes.clear()
        for binding in bindings:
            # An upstream link may be replaced (reconnect); a downstream host that went away is gone.
            if self.role == "upstream":
                binding.detached()
            else:
                binding.gone(error)
        self.host.connectionClosed(self)


_PROTOCOL_REASON: Final = re.compile(r"^protocol-version (\d+)$")


def _count(value: float) -> str:
    return "Infinity" if value == float("inf") else str(int(value))


def stateKey(hold: int, m: str) -> str:
    return f"s{hold}\u0000{m}"


def unitKey(hold: int) -> str:
    return f"u{hold}"


async def relayCall(relay: RelayRef, frame: Frame, root: RootRef, signal: AbortSignal, host: HostImpl) -> Any:
    """A call through a relay: waits for the route, then forwards with the root it runs in. It settles exactly once; a
    cancel (or the origin going away) is forwarded downstream and removes both mappings."""

    def abortError() -> ChordError:
        return ChordError("aborted", "call cancelled", {"cause": signal.reason})

    if signal.aborted:
        raise abortError()
    aborted = deferred()

    def onAbort() -> None:
        aborted.reject(abortError())

    signal.addEventListener("abort", onAbort, once=True)
    onCancel: list[Callable[[], None]] = []
    try:
        down = await race([relay.ready(), aborted.promise])
        if relay.closed:
            raise ChordError("gone", "relayed hold closed")
        result: Deferred[Any] = deferred()
        call = RelayCall(relay, frame["id"], down.nextId(), Pending(result.resolve, result.reject))
        onCancel.append(lambda: call.cancel(abortError(), True))
        signal.addEventListener("abort", onCancel[0], once=True)
        host.rootSent(root["rootId"], down)
        forwarded: Frame = {"t": "call", "id": call.downId, "hold": relay.downId, "member": frame["member"]}
        if frame.get("args", UNDEFINED) is not UNDEFINED:
            forwarded["args"] = frame["args"]
        forwarded["root"] = root
        down.send(forwarded)
        return await result.promise
    finally:
        signal.removeEventListener("abort", onAbort)
        for listener in onCancel:
            signal.removeEventListener("abort", listener)


def validateSnapshot(ref: RefImpl, member: str, value: Any) -> None:
    """Validates a snapshot against the schema the consumer is promised: the provider's own schema for a direct
    binding, the consumer's contract for one through an ``accepts`` adapter (the adapter's view is what the consumer
    gets)."""
    if value is UNDEFINED:
        return
    installed = ref.object.currentInstalled()
    if installed is None:
        return
    view = ref.currentView()
    variant = variantOf(installed.implementation) if view is None else variantOf(view)
    if view is not None and view is not installed.implementation:
        members = ref.contract.description["variants"].get(variant if variant is not None else LOCAL_VARIANT) or {}
    else:
        own = variantOf(installed.implementation)
        members = installed.service.contract.description["variants"].get(own if own is not None else LOCAL_VARIANT) or {}
    description = members.get(member)
    if description is not None and description["kind"] == "state":
        assertValid("state", f"{ref.address}.{member}", description["schema"], value)


def clientError(error: Any) -> Any:
    """Clients (browsers) holding an outdated contract are told to reload their code."""
    if isinstance(error, ChordError) and error.code == "contract_mismatch":
        return ChordError("upgrade_required", error.message, {"cause": error})
    return error


__all__ = [
    "AcceptOptions",
    "Connection",
    "LinkOptions",
    "Peer",
    "RelayCall",
    "RelayRef",
    "clientError",
    "relayCall",
    "validateSnapshot",
]
