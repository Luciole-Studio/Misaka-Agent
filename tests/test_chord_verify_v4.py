"""Regression tests for the independent verification of the chord port (VERIFY-V4-chord, findings V4-B*/V4-O*).

Each test is named after its finding and fails on the port as it was before the fix. The upstream behaviour each test
pins was run under chord v1.3.0 (Node) by the verifier; the expected values below are upstream's.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import weakref
from pathlib import Path
from typing import Any

import pytest

from misaka.chord import (
    ChordError,
    createHost,
    hostNamed,
    local,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import Facet
from misaka.chord.object import checkLifetime, refOf
from misaka.chord.publish import enqueue, holdPublications
from misaka.utils.js import UNDEFINED

# ─── V4-B01: one publication queue per realm (thread) ──────────────────────────


def test_b01_a_notification_enqueued_on_one_thread_runs_on_that_thread_while_another_holds():
    inside = threading.Event()
    release = threading.Event()
    log: list[tuple[str, object]] = []

    def holder() -> None:
        def commit() -> None:
            inside.set()
            release.wait(2)

        holdPublications(commit)  # thread A: a reload commit holding publications

    def other() -> None:
        inside.wait(2)
        enqueue(lambda: log.append(("ran on", threading.current_thread().name)))
        log.append(("delivered before enqueue returned", len(log) == 1))
        release.set()

    a = threading.Thread(target=holder, name="A")
    b = threading.Thread(target=other, name="B")
    a.start()
    b.start()
    a.join(5)
    b.join(5)
    assert log == [("ran on", "B"), ("delivered before enqueue returned", True)]


# ─── object.py ───────────────────────────────────────────────────────────────


async def settle(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


DocV1 = remote(
    "t.v4-doc",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("host"),
        "variants": {"main": {"methods": {"read": method(None, Type.Object({"text": Type.String()}))}}},
    },
)
DocV2 = remote(
    "t.v4-doc",
    {
        "version": 2,
        "input": Type.Object({}),
        "host": hostNamed("host"),
        "variants": {"main": {"methods": {"fetch": method(None, Type.Object({"body": Type.String()}))}}},
    },
)


def _adapter(current: dict[str, Any]) -> dict[str, Any]:
    return {"variant": "main", "read": lambda _ctx=None: {"text": current["fetch"]()["body"]}}


async def test_b03_an_adapter_view_does_not_keep_a_retired_implementation_alive():
    fetches: list[weakref.ref[Any]] = []

    def facet(body: str) -> Facet:
        def setup(api: Any) -> None:
            def factory(*_: Any) -> dict[str, Any]:
                def fetch(_ctx: Any = None) -> dict[str, Any]:
                    return {"body": body}

                fetches.append(weakref.ref(fetch))
                return {"variant": "main", "fetch": fetch}

            api.serve(DocV2, factory, {"accepts": {1: _adapter}})

        return Facet("doc", setup)

    host = createHost({"self": {"kind": "host", "name": "host"}, "onError": lambda _error: None})
    await host.install(facet("a"))
    consumer = host.hold(DocV1, {})
    assert await consumer.read() == {"text": "a"}
    await host.reload([facet("b")])
    assert await consumer.read() == {"text": "b"}
    await settle()
    gc.collect()
    # The long-lived consumer's view of the old implementation went with it (upstream's views are an ephemeron table).
    assert fetches[0]() is None
    assert fetches[-1]() is not None
    await host.close()


async def test_b04_a_cancelled_wait_inside_an_activation_unwinds_it():
    S = local("t.v4-cancelled-open", {"version": 1, "input": Type.Object({})})
    log: list[str] = []
    pending: list[asyncio.Future[Any]] = []

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def openA(_options: Any) -> int:
            log.append("open a")
            return 1

        async def openB(_options: Any) -> Any:
            waiting = asyncio.get_running_loop().create_future()
            pending.append(waiting)
            return await waiting

        self.resource("a", {"version": 1, "open": openA, "close": lambda _value: log.append("close a")})
        self.resource("b", {"version": 1, "open": openB})
        return {}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(S, factory)
    ref = host.hold(S, {})
    await settle(5)
    pending[0].cancel()  # the resource's own wait is cancelled: a rejection, as JavaScript has no cancellation
    with pytest.raises(ChordError) as caught:
        await ref.ready
    assert caught.value.code == "unavailable"
    await settle(5)
    impl = refOf(ref)
    assert (impl.object.status, impl.status, impl.object.address in host.objects, log) == (
        "failed",
        "gone",
        False,
        ["open a", "close a"],
    )
    await host.close()


async def test_b04_a_caller_that_stops_waiting_for_get_releases_its_reference():
    S = local("t.v4-get-cancelled", {"version": 1, "input": Type.Object({})})
    gate: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def open_(_options: Any) -> Any:
            return await gate

        self.resource("r", {"version": 1, "open": open_})
        return {}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(S, factory)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(host.get(S, {}), 0.05)
    await settle()
    # `awaitReady` released the reference whatever ended the wait (upstream: every rejection), so the object went.
    assert host.objects == {}
    gate.set_result(None)
    await host.close()


async def test_b05_a_task_that_ends_without_suspending_leaves_no_stale_entry_and_reload_restarts_at_once():
    S = local("t.v4-task", {"version": 1, "input": Type.Object({})})
    log: list[str] = []

    def facet(n: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                async def task(_ctx: Any) -> None:
                    log.append(f"start {n}")

                self.task(task)
                return {}

            api.serve(S, factory)

        return Facet("s", setup)

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    await host.install(facet(1))
    ref = host.hold(S, {})
    await ref.ready
    await settle(3)
    assert refOf(ref).object._tasks == {}
    await host.reload([facet(2)])
    # Upstream (node): [ 'start 1', 'start 2' ] right after the reload.
    assert log == ["start 1", "start 2"]
    await host.close()


class _Scaled:
    """A readable-like whose value is computed on each read (a fresh float object each time), like an adapter's."""

    def __init__(self, base: float) -> None:
        self.base = base

    @property
    def value(self) -> float:
        return self.base * 1.5

    def subscribe(self, fn: Any) -> Any:
        fn(self.value)
        return lambda: True

    def events(self, fn: Any) -> Any:
        fn({"type": "snap", "value": self.value})
        return lambda: True


async def test_b06_an_equal_first_snapshot_is_not_announced_again():
    S = local("t.v4-scaled", {"version": 1, "input": Type.Object({}), "members": {"x": Type.Number()}})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(S, lambda *_: {"x": _Scaled(1001)})
    ref = host.hold(S, {})
    await ref.ready
    seen: list[Any] = []
    ref.x.subscribe(seen.append)
    await settle(10)
    events: list[Any] = []
    other = host.hold(S, {})
    await other.ready
    other.x.events(events.append)
    await settle(10)
    assert seen == [1501.5]
    assert events == [{"type": "snap", "value": 1501.5}]
    await host.close()


def test_b06_strict_equality_is_javascripts():
    from misaka.chord.object import strictEqual

    nan = float("nan")
    assert strictEqual(1, 1.0) and strictEqual("a", "a") and strictEqual(None, None) and strictEqual(UNDEFINED, UNDEFINED)
    assert not strictEqual(True, 1) and not strictEqual(0, False) and not strictEqual(nan, nan)
    assert not strictEqual(None, UNDEFINED) and not strictEqual([1], [1]) and not strictEqual({}, {})
    assert strictEqual(-0.0, 0)


async def test_b34_a_null_opaque_member_is_in_the_reference():
    L = local("t.v4-null-opaque", {"version": 1, "input": Type.Object({}), "members": {"h": opaque("handle@1")}})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(L, lambda *_: {"h": None, "extra": 0})
    ref = host.hold(L, {})
    await ref.ready
    assert ("h" in ref, ref.h, "extra" in ref, "nope" in ref, ref.nope) == (True, None, True, False, None)
    await host.close()


@pytest.mark.parametrize("options", [{"version": None}, {"version": 2.0}, {"version": UNDEFINED}])
async def test_b35_a_state_version_is_a_javascript_integer_and_defaults_through_nullish(options: dict[str, Any]):
    S = local("t.v4-version", {"version": 1, "input": Type.Object({})})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        self.state("x", Type.Number(), 0, options)
        self.resource("r", {"version": 3.0, "open": lambda _options: None})
        return {}

    host.serve(S, factory)
    ref = host.hold(S, {})
    await ref.ready
    assert refOf(ref).status == "live"
    await host.close()


def test_b16_a_lifetime_is_defaulted_through_nullish_and_a_host_object_refuses_an_explicit_null_linger():
    L = local("t.v4-lifetime", {"version": 1, "input": Type.Object({})})
    with pytest.raises(TypeError, match=r"^t\.v4-lifetime: unknown lifetime $"):
        checkLifetime(L, {"lifetime": ""})
    with pytest.raises(TypeError, match="does not linger"):
        checkLifetime(L, {"lifetime": "host", "linger": None})
    checkLifetime(L, {"lifetime": None})
    checkLifetime(L, {"lifetime": "host"})


# ─── reload.py / cluster.py ──────────────────────────────────────────────────


async def test_b13_a_reload_whose_caller_stops_waiting_runs_on_to_its_commit():
    """VERIFY2 R2-N3: a promise cannot be cancelled, so a caller that gives up leaves the reload running, as upstream's
    does (node: `caller gave up`, `root: hello g`, codeVersion 2, `greet: hi z`)."""
    from tests.test_chord_reload import (
        Greeter,
        Latch,
        _probeConflict,
        greeterFacet,
        quiet,
        tick,
    )

    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def grandfathered(ctx: Any) -> Any:
        await ref.greet("g", ctx)
        await go.promise
        return await ref.greet("g", ctx)

    root = host.root(grandfathered)
    await tick()
    # The caller stops waiting for a reload whose gate waits for the grandfathered root.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(host.reload([greeterFacet("hi", state={"version": 2})], {"gateDeadline": 60_000}), 0.1)
    go.open()
    assert await root == "hello g"
    await tick()
    assert (host.reloading, host.pending, host.gating, host.codeVersion) == (False, None, None, 2)
    assert not await _probeConflict(ref)
    assert await ref.greet("z") == "hi z"
    await host.close()


async def test_b16_a_gate_deadline_of_none_is_the_default():
    from tests.test_chord_reload import Greeter, Latch, greeterFacet, quiet, tick

    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def blocking(ctx: Any) -> Any:
        await ref.greet("g", ctx)
        await go.promise
        return await ref.greet("g", ctx)

    root = host.root(blocking)
    await tick()
    reload = host.reload([greeterFacet("hi", state={"version": 2})], {"gateDeadline": None, "retireDeadline": None})
    asyncio.get_running_loop().call_later(0.1, go.open)  # well within the default 5000 ms
    report = await reload
    assert report["gated"] == [refOf(ref).address]
    await root
    await host.close()


async def test_o3_a_dry_run_reads_undefined_from_a_source_that_is_not_ready():
    from misaka.chord.errors import ChordError as Error
    from misaka.chord.reload import candidateValue

    class NotReady:
        @property
        def value(self) -> Any:
            raise Error("not_ready", "x is not ready")

    assert candidateValue(NotReady()) is UNDEFINED


# ─── host.py ─────────────────────────────────────────────────────────────────


async def test_b16_host_options_default_through_nullish():
    T = local("t.v4-opts", {"version": 1, "input": Type.Object({}), "members": {"ping": method(None, Type.String())}})
    host = createHost({"self": {"k": 1}, "readyTimeout": None, "taskStopDeadline": None, "name": "", "dev": None})
    assert (host.readyTimeout, host.taskStopDeadline, host.name, host.dev) == (60_000, 5000, "", False)
    host.serve(T, lambda *_: {"ping": lambda _ctx=None: "pong"})
    ref = await host.get(T, {})
    assert await ref.ping() == "pong"
    await host.close()


def test_b17_registry_tables_compare_values_by_javascript_equality():
    from misaka.chord.host import sameTable

    joined = "ab" + str.lower("C")  # equal to "abc" but a distinct object: the point of the test
    assert joined is not "abc"  # noqa: F632
    assert sameTable({"k": joined}, {"k": "abc"})
    assert sameTable({"k": int("100000")}, {"k": 100000})
    assert not sameTable({"k": 1}, {"k": True})
    assert not sameTable({"k": [1]}, {"k": [1]})
    assert not sameTable({"a": 1, "b": 2}, {"b": 2, "a": 1})


async def test_b18_a_pattern_matches_an_array_principal():
    from misaka.chord.host import matchesPattern

    assert matchesPattern({}, [1])
    assert matchesPattern({"0": 1, "length": 1}, [1])
    assert not matchesPattern({"0": 2}, [1])
    assert not matchesPattern({}, None) and not matchesPattern({}, "s") and not matchesPattern({}, 1)
    T = local("t.v4-array-principal", {"version": 1, "input": Type.Object({}), "members": {"ping": method(None, Type.String())}})
    host = createHost({"self": {"k": 1}, "onError": lambda _error: None})
    host.serve(T, lambda *_: {"ping": lambda _ctx=None: "pong"})
    ref = host.hold(T, {}, {"principal": [1]})
    await ref.ready
    await host.invalidatePrincipal({})
    assert ref.status == "gone"
    await host.close()


async def test_o2_a_facet_given_as_a_mapping_installs_and_loads():
    T = local("t.v4-dict-facet", {"version": 1, "input": Type.Object({}), "members": {"ping": method(None, Type.String())}})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def setup(api: Any) -> None:
        api.serve(T, lambda *_: {"ping": lambda _ctx=None: "pong"})

    await host.load([{"name": "dict-facet", "setup": setup}])
    assert list(host.facets) == ["dict-facet"]
    ref = await host.get(T, {})
    assert await ref.ping() == "pong"
    await host.close()


# ─── errors.py, state.py, remote_ref.py ──────────────────────────────────────


def test_b19_a_chord_error_copies_and_pickles():
    import copy
    import pickle

    error = ChordError("invalid", "bad", {"reason": "args", "details": [1], "cause": "why"})
    for clone in (copy.copy(error), copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
        assert type(clone) is ChordError
        assert (clone.message, clone.code, clone.reason, clone.details, clone.cause, str(clone)) == (
            "invalid/args: bad",
            "invalid",
            "args",
            [1],
            "why",
            "invalid/args: bad",
        )


async def test_b20_every_step_runs_when_one_awaits_a_cancelled_future():
    from misaka.chord._promise import CancelledRejection
    from misaka.chord.errors import runAll

    log: list[str] = []

    async def cancelled() -> None:
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await future

    with pytest.raises(CancelledRejection) as caught:
        await runAll([lambda: log.append("a"), cancelled, lambda: log.append("c")], "cleanup failed")
    # A CancelledError of an awaited future is a rejection: it is thrown after every step ran (here: the only one), as
    # an Exception (VERIFY2 R2-N1), with the CancelledError as its cause.
    assert log == ["a", "c"]
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)


async def test_b20_every_step_runs_when_the_task_itself_is_cancelled():
    from misaka.chord.errors import runAll

    log: list[str] = []
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)

    task = asyncio.ensure_future(runAll([slow, lambda: log.append("after")], "cleanup failed"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["after"]


def test_b20_a_listener_that_raises_a_base_exception_does_not_stop_later_deliveries():
    from misaka.chord.state import StateDelivery, StateSubscriber

    calls: list[Any] = []

    def listener(value: Any, *_rest: Any) -> None:
        calls.append(value)
        if value == 1:
            raise KeyboardInterrupt

    subscriber = StateSubscriber(listener, lambda _error: None)
    subscriber.push(StateDelivery(1, None, None))
    with pytest.raises(KeyboardInterrupt):
        subscriber.drain()
    subscriber.push(StateDelivery(2, None, None))
    subscriber.drain()
    assert calls == [1, 2]


async def test_b39_in_on_a_remote_reference_sees_only_its_public_members():
    from misaka.chord.remote_ref import RemoteRef

    names = {"_readables", "_callers", "_settleReady", "has", "_notify", "__class__"}
    ref = RemoteRef.__new__(RemoteRef)
    ref._readables = {}  # type: ignore[attr-defined]
    for name in names:
        assert not RemoteRef.has(ref, name), name
    assert RemoteRef.has(ref, "release") and RemoteRef.has(ref, "member") and RemoteRef.has(ref, "status")


# ─── output.py ───────────────────────────────────────────────────────────────


async def test_b11_stop_hands_back_resolvers_that_never_throw():
    from misaka.chord.output import Progress

    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def write() -> int:
        await gate
        return 0

    progress = Progress(write, lambda _error: None, 100)
    first = progress.markAndWait()
    second = progress.markAndWait()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second, 0.01)  # the awaiting side gives up: the promise is cancelled
    stopping = asyncio.ensure_future(progress.stop())
    gate.set_result(None)
    pending = await stopping
    await first
    assert len(pending) == 1
    pending[0].resolve()  # JavaScript's resolve() never throws, also on a promise nobody waits for any more
    pending[0].reject(RuntimeError("late"))


async def test_b49_a_synchronous_write_failure_leaves_mark_and_an_on_error_failure_rejects_stop():
    from misaka.chord.output import Progress

    def write() -> Any:
        raise RuntimeError("sync")

    reported: list[Any] = []
    progress = Progress(write, reported.append, 100)
    with pytest.raises(RuntimeError, match="sync"):
        progress.mark()
    assert reported == []

    async def failing() -> int:
        raise RuntimeError("commit failed")

    def onError(_error: Any) -> None:
        raise ValueError("reporter failed")

    progress = Progress(failing, onError, 100)
    waiter = progress.markAndWait()
    with pytest.raises(ValueError, match="reporter failed"):
        await progress.stop()
    with pytest.raises(RuntimeError, match="commit failed"):
        await waiter


# ─── loader.py ───────────────────────────────────────────────────────────────


async def test_b02_a_directory_above_the_facet_directories_does_not_run(tmp_path: Path):
    from misaka.chord.loader import nodeLoader

    package = tmp_path / "pkg"
    plugin = package / "plug"
    plugin.mkdir(parents=True)
    (package / "__init__.py").write_text("import builtins\nbuiltins.__chord_v4_ancestor_ran__ = True\n", encoding="utf-8")
    (package / "sibling.py").write_text("token = 1\n", encoding="utf-8")
    (plugin / "facet.py").write_text(
        "from misaka.chord import defineFacet\nfrom .. import sibling\nassert sibling.token == 1\n"
        "default = defineFacet(lambda api: None)\n",
        encoding="utf-8",
    )
    import builtins

    try:
        await nodeLoader([str(plugin / "facet.py")])
        assert not hasattr(builtins, "__chord_v4_ancestor_ran__")
    finally:
        if hasattr(builtins, "__chord_v4_ancestor_ran__"):
            del builtins.__chord_v4_ancestor_ran__  # type: ignore[attr-defined]


async def test_b22_a_facet_directorys_own_init_has_its_file(tmp_path: Path):
    from misaka.chord.loader import nodeLoader

    plugin = tmp_path / "plug"
    plugin.mkdir()
    (plugin / "__init__.py").write_text("seen = __file__\n", encoding="utf-8")
    (plugin / "facet.py").write_text(
        "from misaka.chord import defineFacet\nfrom . import seen\ndefault = defineFacet(lambda api: None)\n"
        "assert seen.endswith('__init__.py'), seen\n",
        encoding="utf-8",
    )
    await nodeLoader([str(plugin / "facet.py")])


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX path spelled into the URL, and a directory symlink")
async def test_b21_a_facet_is_named_as_path_to_file_url_names_it(tmp_path: Path):
    from misaka.chord.loader import nodeLoader

    assert all(char.isalnum() or char in "/-_." for char in str(tmp_path))
    real = tmp_path / "a+b@c,d=e!(f)~g"
    real.mkdir()
    (real / "facet.py").write_text("from misaka.chord import defineFacet\ndefault = defineFacet(lambda api: None)\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    named = await nodeLoader([str(real / "facet.py"), str(link / ".." / "link" / "facet.py")])
    # Node: `pathToFileURL` keeps `+@,=!()`, encodes `~`, and resolves `..` without following the symlink.
    assert [facet.name for facet in named] == [f"file://{tmp_path}/a+b@c,d=e!(f)%7Eg/facet.py", f"file://{tmp_path}/link/facet.py"]


async def test_o2_a_module_default_may_be_a_mapping(tmp_path: Path):
    from misaka.chord.loader import nodeLoader

    (tmp_path / "facet.py").write_text("uses = []\ndefault = {'setup': lambda api: None, 'uses': uses}\n", encoding="utf-8")
    (facet,) = await nodeLoader([str(tmp_path / "facet.py")])
    assert callable(facet.setup) and facet.uses == []
    (tmp_path / "bad.py").write_text("default = {'setup': 1}\n", encoding="utf-8")
    with pytest.raises(TypeError, match="default export must be a facet"):
        await nodeLoader([str(tmp_path / "bad.py")])


async def test_b38_a_reference_whose_contract_has_events_and_subscribe_is_readable_like():
    from misaka.chord.object import isReadableLike

    Feed = local(
        "t.v4-feed",
        {"version": 1, "input": Type.Object({}), "members": {"events": method(None), "subscribe": method(None)}},
    )
    Plain = local("t.v4-plain", {"version": 1, "input": Type.Object({}), "members": {"ping": method(None)}})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(Feed, lambda *_: {"events": lambda _ctx=None: None, "subscribe": lambda _ctx=None: None})
    host.serve(Plain, lambda *_: {"ping": lambda _ctx=None: None})
    # As upstream's proxy answers `events`/`subscribe` with callers, a reference to such a contract is readable-like.
    assert isReadableLike(host.hold(Feed, {}))
    assert not isReadableLike(host.hold(Plain, {}))
    assert not isReadableLike({"events": print, "subscribe": print})
    await host.close()
