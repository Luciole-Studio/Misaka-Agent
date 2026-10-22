"""pi chord's ``test/flow.test.ts``: link flow control. State waits while the peer's acknowledgements lag a window
behind, coalesced per subscription; everything else is sent at once.

MISAKA adaptations: ``AckGate`` is a class with upstream's members (``paused``, ``release``) over the inner link; the
counter's ``wait`` method resolves when the module-level ``release`` is called, as upstream's chained resolver does;
a mutating ``change`` callback returns ``None``; the ad-hoc link a route returns is a small class (upstream's object
literal with a getter).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from misaka.chord import (
    PROTOCOL_VERSION,
    createHost,
    hostNamed,
    isChordError,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError
from misaka.utils.js import UNDEFINED


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Callable[[], bool], attempts: int = 200) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


class AckGate:
    """A client's end of a link that can hold back its acknowledgements, and records what it receives."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.received: list[Any] = []
        self.paused = False
        self._held: Any = None

    @property
    def closed(self) -> bool:
        return self.inner.closed

    def send(self, frame: Any) -> int | None:
        if frame["t"] == "ack" and self.paused:
            self._held = frame
            return 0
        return self.inner.send(frame)

    def release(self) -> None:
        self.paused = False
        if self._held is not None:
            self.inner.send(self._held)
        self._held = None

    def receive(self, handler: Callable[[Any], None]) -> None:
        def received(frame: Any) -> None:
            self.received.append(frame)
            handler(frame)

        self.inner.receive(received)

    def onClose(self, handler: Callable[[str], None]) -> None:
        self.inner.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self.inner.close(reason)


def counterOn(name: str, host: str) -> Any:
    """A counter on the server, or (``t.flow-worker-counter``) on the worker host ``w``."""
    return remote(
        name,
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed(host),
            "variants": {
                "main": {
                    "state": {"count": Type.Object({"n": Type.Number()})},
                    "methods": {"add": method(Type.Number(), Type.Number()), "wait": method()},
                }
            },
        },
    )


Counter = counterOn("t.flow-counter", "server")
WorkerCounter = counterOn("t.flow-worker-counter", "w")

release: Callable[[], None] = lambda: None


def waiting(_ctx: Any) -> asyncio.Future[None]:
    global release
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    previous = release

    def chained() -> None:
        previous()
        if not future.done():
            future.set_result(None)

    release = chained
    return future


def serveCounter(host: Any, type: Any = Counter) -> None:
    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 0})

        def add(delta: float, ctx: Any) -> float:
            def mutate(draft: Any) -> None:
                draft["n"] = draft["n"] + delta

            count.change(ctx, mutate)
            return count.value["n"]

        return {"variant": "main", "count": count, "add": add, "wait": waiting}

    host.serve(type, factory)


def client(server: Any, window: float) -> tuple[Any, AckGate]:
    """A client of ``server`` (accepted with ``window``) whose acknowledgements can be held back."""
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}, "window": window})
    gate = AckGate(up)
    host = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": gate, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    return host, gate


def stateFrames(frames: list[Any]) -> list[Any]:
    return [frame for frame in frames if frame["t"] == "state"]


def last(values: list[Any]) -> Any:
    return values[-1] if values else UNDEFINED


# ─── link flow control ───


async def test_holds_state_back_while_the_window_is_full_answers_calls_meanwhile_and_sends_one_coalesced_frame():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    host, gate = client(server, 200)
    counter = host.hold(Counter, {})
    values: list[Any] = []
    counter.count.subscribe(values.append)
    await until(lambda: last(values) is not UNDEFINED)

    gate.paused = True
    results: list[float] = []
    for _ in range(40):
        results.append(await counter.add(1))
    # Every call was answered, though most state is still waiting for an acknowledgement.
    assert results[-1] == 40
    assert last(values)["n"] < 40, "state waits behind the window"
    before = len(stateFrames(gate.received))

    gate.release()
    await until(lambda: last(values)["n"] == 40)
    assert len(stateFrames(gate.received)) - before == 1, "the waiting ops arrive as one frame"
    await host.close()
    await server.close()


async def test_replaces_ops_that_outgrow_their_slot_with_the_current_value():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    host, gate = client(server, 64)
    counter = host.hold(Counter, {})
    values: list[Any] = []
    counter.count.subscribe(values.append)
    await until(lambda: last(values) is not UNDEFINED)

    gate.paused = True
    before = len(gate.received)
    for _ in range(100):
        await counter.add(1)
    gate.release()
    await until(lambda: last(values)["n"] == 100)
    sent = stateFrames(gate.received[before:])
    assert any(frame["as"] == "reset" for frame in sent), "a reset replaced the ops"
    assert len(sent) < 10, f"few state frames, not one per change ({len(sent)})"
    await host.close()
    await server.close()


async def test_drops_what_waits_for_a_reference_that_is_released():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    host, gate = client(server, 64)
    counter = host.hold(Counter, {})
    await counter.ready
    other = host.hold(Counter, {})
    await other.ready

    gate.paused = True
    for _ in range(10):
        await counter.add(1)
    counter.release()
    other.release()
    before = len(gate.received)
    gate.release()
    await sleep(10)
    assert stateFrames(gate.received[before:]) == []
    await host.close()
    await server.close()


class _RecordingLink:
    """The link a route returns: the worker's end, recording what the server sends to it."""

    def __init__(self, down: Any, toWorker: list[Any]) -> None:
        self._down = down
        self._toWorker = toWorker

    @property
    def closed(self) -> bool:
        return self._down.closed

    def send(self, frame: Any) -> int | None:
        self._toWorker.append(frame)
        return self._down.send(frame)

    def receive(self, handler: Callable[[Any], None]) -> None:
        self._down.receive(handler)

    def onClose(self, handler: Callable[[str], None]) -> None:
        self._down.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self._down.close(reason)


async def test_coalesces_relayed_state_for_the_origin_and_asks_downstream_for_a_reset_when_it_overflows():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    toWorker: list[Any] = []

    async def resolve(name: str) -> Any:
        down, up = memoryLinkPair()
        worker = createHost(
            {
                "self": {"kind": "host"},
                "name": name,
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        serveCounter(worker, WorkerCounter)
        return _RecordingLink(down, toWorker)

    server.route("w", {"principal": lambda name: {"host": name}, "authorize": lambda _p, _h: True, "resolve": resolve})
    host, gate = client(server, 64)
    counter = host.hold(WorkerCounter, {})
    values: list[Any] = []
    counter.count.subscribe(values.append)
    await until(lambda: last(values) is not UNDEFINED)

    def resyncs() -> int:
        return len([frame for frame in toWorker if frame["t"] == "resync"])

    assert resyncs() == 0

    gate.paused = True
    for _ in range(100):
        await counter.add(1)
    # An overflow drops the waiting ops and asks the worker for a reset (which then waits in the slot).
    assert resyncs() >= 1, "the overflowing stream was resynced downstream"
    assert not any(frame["t"] in ("sub", "unsub") for frame in toWorker)
    gate.release()
    await until(lambda: last(values)["n"] == 100)
    await host.close()
    await server.close()


async def test_neither_windows_nor_acknowledges_a_peer_whose_hello_does_not_announce_acks():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    down, raw = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}, "window": 1})
    frames: list[Any] = []
    raw.receive(frames.append)
    raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
    raw.send({"t": "hold", "id": 1, "at": "server", "token": Counter.description, "input": {}})
    for i in range(5):
        raw.send({"t": "call", "id": 3 + i, "hold": 1, "member": "add", "args": 1})
    await until(lambda: len([frame for frame in frames if frame["t"] == "result"]) == 5)
    await sleep(5)
    assert len(stateFrames(frames)) == 6, "the snapshot and one frame per change"
    assert not any(frame["t"] == "ack" for frame in frames)
    assert frames[0]["acks"] is True
    await server.close()


async def test_closes_the_link_on_an_acknowledgement_of_frames_never_sent():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    down, raw = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}})
    raw.receive(lambda _frame: None)
    raw.send({"t": "hello", "name": "raw", "kind": "client", "codeVersion": 0, "acks": True})
    raw.send({"t": "ack", "n": 99})
    await until(lambda: raw.closed)
    await server.close()


# ─── link call limit ───


async def test_answers_calls_beyond_maxcalls_at_once_with_too_many_calls_and_keeps_the_link():
    global release
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}, "maxCalls": 2})
    host = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    counter = host.hold(Counter, {})
    await counter.ready
    release = lambda: None
    first = counter.wait()
    second = counter.wait()
    with pytest.raises(ChordError) as rejected:
        await counter.add(1)
    assert isChordError(rejected.value, "unavailable") and rejected.value.reason == "too_many_calls"
    release()
    await asyncio.gather(first, second)
    assert await counter.add(1) == 1
    await host.close()
    await server.close()
