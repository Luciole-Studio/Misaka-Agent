"""pi codemode's ``deterministic.test.ts``: ``start()`` runs a script deterministically with the caller answering its
calls, so a durable host can replay it after a restart.

Left out: the remote backend and "deterministic runs (remote transport)" (``RemoteCodemodeSandbox`` is not ported).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from datetime import UTC, datetime

import pytest

from misaka.codemode import CodemodeSandbox
from misaka.utils.js import AbortController

STARTED_AT = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC).timestamp() * 1000

# The first two `Math.random()` values with seed 0.
PINNED_SEED_ZERO = [0.6711372530266764, 0.7258461452833667]
# The first `Math.random()` with seed 1.
PINNED_FIRST_RANDOM = 0.34227450605335275


def matches(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
    return actual == expected


class Recorded:
    def __init__(self) -> None:
        self.run = None
        self.calls: list[dict] = []
        self.idles: list[list[int]] = []
        self.events: list[str] = []
        self.fingerprintValue: str | None = None

    def fingerprint(self) -> str | None:
        return self.fingerprintValue


def start(sandbox, code, answer=None, **options) -> Recorded:
    """Start ``code`` with tools ``echo`` and ``wait``. Every call but ``wait`` is answered at once, by default with
    its argument, as result number 1, 2, ... with the clock at ``STARTED_AT + 1000 * seq``; ``wait`` is left for the
    test to answer."""
    recorded = Recorded()
    seq = 0
    answer = answer or (lambda call: {"ok": True, "value": call["args"]})
    loop = asyncio.get_running_loop()

    def onStarted(value):
        recorded.fingerprintValue = value
        recorded.events.append("started")

    def onCall(call):
        recorded.calls.append(call)
        recorded.events.append(f"call {call['id']}")
        if call["name"] == "wait":
            return

        def reply():
            nonlocal seq
            seq += 1
            recorded.run.deliver(seq, STARTED_AT + 1000 * seq, call["id"], answer(call))

        # `queueMicrotask`
        loop.call_soon(reply)

    def onIdle(seq, issued):
        recorded.idles.append([seq, issued])
        recorded.events.append(f"idle {seq}")

    recorded.run = sandbox.start(code, {
        "startedAt": STARTED_AT,
        "seed": 1,
        "tools": [{"name": "echo"}, {"name": "wait"}],
        "timeoutMs": 20_000,
        **options,
        "onStarted": onStarted,
        "onCall": onCall,
        "onIdle": onIdle,
    })
    return recorded


@pytest.fixture
async def sandboxes():
    created: list[CodemodeSandbox] = []

    def create(options=None) -> CodemodeSandbox:
        sandbox = CodemodeSandbox(options or {})
        created.append(sandbox)
        return sandbox

    yield create
    await asyncio.gather(*(sandbox.close() for sandbox in created))


async def waitFor(check) -> None:
    deadline = time.monotonic() + 5
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("Condition was not reached")
        await asyncio.sleep(0.005)


@pytest.fixture
def timezone():
    saved = os.environ.get("TZ")

    def set_(value: str) -> None:
        os.environ["TZ"] = value
        # MISAKA fork: Python reads TZ when tzset() runs; Node reads it when it is assigned.
        time.tzset()

    yield set_
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


async def test_makes_the_same_calls_and_output_for_the_same_start_seed_and_results(sandboxes):
    code = """
        const values = [Date.now(), performance.now(), Math.random(), Math.random(), new Date().toISOString()];
        const first = await tools.echo(values);
        text(JSON.stringify([Date.now(), performance.now(), Math.random()]));
        return await tools.echo(first.concat(Math.random()));
    """
    sandbox = sandboxes()
    one = start(sandbox, code)
    two = start(sandbox, code)
    a, b = await asyncio.gather(one.run.result, two.run.result)

    # Call durations are wall-clock time; everything else is the same.
    def timeless(result):
        return {**result, "calls": [{"name": call["name"], "status": call["status"]} for call in result["calls"]]}

    assert timeless(a) == timeless(b)
    assert one.calls == two.calls
    # The clock: startedAt, then the time of the result being processed; performance.now() counts from startedAt.
    now, elapsed, random = one.calls[0]["args"][:3]
    assert [now, elapsed] == [STARTED_AT, 0]
    assert one.calls[0]["args"][4] == "2026-01-15T10:00:00.000Z"
    printed = json.loads(a["output"][0]["text"]) if a["ok"] else []
    assert printed[:2] == [STARTED_AT + 1000, 1000]
    # Pinned: a change here changes what replayed scripts see, so it must change the fingerprint too.
    assert random == PINNED_FIRST_RANDOM

    # The seed decides the random values, not the start time.
    later = start(sandbox, code, startedAt=STARTED_AT + 5000)
    reseeded = start(sandbox, code, seed=2)
    await asyncio.gather(later.run.result, reseeded.run.result)
    assert later.calls[0]["args"][2] == random
    assert reseeded.calls[0]["args"][2] != random
    # Seed 0 has its own values: QuickJS turns a zero state into 1, which must not make it seed 1's.
    zero = start(sandbox, "return [Math.random(), Math.random()]", seed=0)
    zeroResult = await zero.run.result
    assert matches(zeroResult, {"ok": True, "value": PINNED_SEED_ZERO})
    assert random not in zeroResult["value"]


async def test_runs_in_utc_whatever_the_hosts_timezone_and_without_weak_ref_or_finalization_registry(sandboxes, timezone):
    timezone("Asia/Kolkata")
    code = "return [new Date(0).getHours(), new Date(0).getTimezoneOffset(), typeof WeakRef, typeof FinalizationRegistry]"
    sandbox = sandboxes()
    recorded = start(sandbox, code)
    assert matches(await recorded.run.result, {"ok": True, "value": [0, 0, "undefined", "undefined"]})
    # execute() is unchanged: the host's timezone and every intrinsic.
    assert matches(await sandbox.execute(code), {"ok": True, "value": [5, -330, "function", "function"]})


async def test_applies_results_in_seq_order_whatever_order_they_are_delivered_in(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, """const order = [];
        await Promise.all([tools.wait("a").then(() => order.push("a")), tools.wait("b").then(() => order.push("b"))]);
        return order;""")
    await waitFor(lambda: len(recorded.calls) == 2 and len(recorded.idles) == 1)
    a, b = recorded.calls
    # b is result 1 and a result 2, but a arrives first.
    recorded.run.deliver(2, STARTED_AT + 2000, a["id"], {"ok": True, "value": None})
    recorded.run.deliver(1, STARTED_AT + 1000, b["id"], {"ok": True, "value": None})
    assert matches(await recorded.run.result, {"ok": True, "value": ["b", "a"]})
    # No idle after the last result: the script finished in that drain, and the host stopped the VM.
    assert recorded.idles == [[0, 2], [1, 2]]


async def test_reports_the_fingerprint_before_any_call_and_the_calls_issued_after_each_result(sandboxes):
    sandbox = sandboxes({"memoryLimitBytes": 64 * 1024 * 1024})
    recorded = start(sandbox, "await tools.echo(1); await Promise.all([tools.echo(2), tools.echo(3)]);")
    assert matches(await recorded.run.result, {"ok": True})
    assert recorded.events[:3] == ["started", "call 1", "idle 0"]
    assert recorded.idles == [[0, 1], [1, 3], [2, 3]]
    assert [[call["id"], call["target"], call["name"], call["args"]] for call in recorded.calls] == [
        [1, "tool", "echo", 1],
        [2, "tool", "echo", 2],
        [3, "tool", "echo", 3],
    ]
    fingerprint = recorded.fingerprint()
    assert re.fullmatch(
        r"quickjs-wasi \S+, quickjs \S+, prelude [0-9a-f]{8}, deterministic \d+, stack \d+, heap 67108864, budget \S+",
        fingerprint,
    )
    assert "budget none" in fingerprint

    same = start(sandbox, "return 1")
    other = start(sandboxes({"memoryLimitBytes": 32 * 1024 * 1024}), "return 1")
    await asyncio.gather(same.run.result, other.run.result)
    assert same.fingerprint() == fingerprint
    assert other.fingerprint() != fingerprint


async def test_numbers_calls_to_globals_with_tools_and_answers_errors_as_rejections(sandboxes):
    sandbox = sandboxes()
    recorded = start(
        sandbox,
        """const found = await search("x", 2);
        try { await tools.echo("fail"); } catch (error) { text(error.message); }
        return found;""",
        answer=lambda call: (
            {"ok": False, "message": "no"}
            if call["args"] == "fail"
            else {"ok": True, "value": ["found"] if call["name"] == "search" else call["args"]}
        ),
        globals=[{"name": "search", "spread": True}],
    )
    result = await recorded.run.result
    assert recorded.calls[0] == {"id": 1, "target": "global", "name": "search", "args": ["x", 2]}
    assert matches(result, {"ok": True, "value": ["found"], "output": [{"type": "text", "text": "no"}]})
    assert matches(recorded.calls[1], {"id": 2, "target": "tool", "name": "echo"})
    # Calls to globals count as issued calls too.
    assert recorded.idles[:2] == [[0, 1], [1, 2]]
    assert [{"name": call["name"], "status": call["status"]} for call in result["calls"]] == [{"name": "echo", "status": "error"}]


async def test_keeps_the_clock_monotonic_and_rejects_a_clock_value_that_is_not_finite(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, "const before = Date.now(); await tools.wait(1); return [before, Date.now(), performance.now()]")
    await waitFor(lambda: len(recorded.calls) == 1)
    with pytest.raises(ValueError, match="not a finite number"):
        recorded.run.deliver(1, math.nan, 1, {"ok": True, "value": None})
    # A resumed host whose clock is behind the run's start.
    recorded.run.deliver(1, STARTED_AT - 2000, 1, {"ok": True, "value": None})
    assert matches(await recorded.run.result, {"ok": True, "value": [STARTED_AT, STARTED_AT, 0]})


async def test_reports_the_result_the_vm_was_processing_when_the_script_ended(sandboxes):
    sandbox = sandboxes()
    returned = start(sandbox, "await tools.echo(1); return await tools.echo(2)")
    thrown = start(sandbox, "await tools.echo(1); await Promise.all([tools.echo(2), tools.echo(3)]); throw new Error('x')")
    first = start(sandbox, "return 1")
    assert matches(await returned.run.result, {"ok": True, "value": 2, "during": {"seq": 2, "issued": 2}})
    assert matches(await thrown.run.result, {"ok": False, "during": {"seq": 3, "issued": 3}})
    assert matches(await first.run.result, {"ok": True, "during": {"seq": 0, "issued": 0}})
    assert "during" not in await sandbox.execute("return 1")


async def test_leaves_a_call_waiting_when_its_answer_cannot_be_serialized(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, "return await tools.wait(1)")
    await waitFor(lambda: len(recorded.calls) == 1)
    # pi: a BigInt; in Python, a value JSON has no form for.
    with pytest.raises(TypeError):
        recorded.run.deliver(1, STARTED_AT, 1, {"ok": True, "value": {1, 2}})
    with pytest.raises(TypeError):
        recorded.run.deliver(1, STARTED_AT, 1, {"ok": False, "message": "x", "properties": {"big": {1}}})
    recorded.run.deliver(1, STARTED_AT, 1, {"ok": True, "value": "fine"})
    assert matches(await recorded.run.result, {"ok": True, "value": "fine"})


async def test_rejects_with_an_error_carrying_the_answers_properties(sandboxes):
    sandbox = sandboxes()
    recorded = start(
        sandbox,
        "try { await tools.echo(1); } catch (e) { return [e instanceof Error, e.message, e.name, e.structuredOutput]; }",
        answer=lambda _call: {
            "ok": False,
            "message": "denied",
            "properties": {"structuredOutput": {"code": 7}, "name": "Ignored", "message": "ignored"},
        },
    )
    assert matches(await recorded.run.result, {"ok": True, "value": [True, "denied", "Error", {"code": 7}]})


async def test_rejects_rejection_properties_that_are_not_an_object_and_tools_sharing_a_script_name(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, "return await tools.wait(1)")
    await waitFor(lambda: len(recorded.calls) == 1)
    for properties in [None, [1]]:
        with pytest.raises(ValueError, match="not an object"):
            recorded.run.deliver(1, STARTED_AT, 1, {"ok": False, "message": "x", "properties": properties})
    recorded.run.deliver(1, STARTED_AT, 1, {"ok": True, "value": 1})
    assert matches(await recorded.run.result, {"ok": True, "value": 1})
    with pytest.raises(ValueError, match="script names must be unique"):
        sandbox.start("", {
            "startedAt": STARTED_AT,
            "seed": 1,
            "tools": [{"name": "a-b"}, {"name": "a_b"}],
            "onCall": lambda _call: None,
        })


async def test_uses_the_script_names_the_run_passes(sandboxes):
    sandbox = sandboxes()
    recorded = start(
        sandbox,
        "return [typeof tools.renamed, Object.keys(tools), ALL_TOOLS.map((tool) => tool.name)]",
        tools=[{"name": "my-tool", "jsName": "renamed"}],
    )
    assert matches(await recorded.run.result, {"ok": True, "value": ["function", ["renamed", "my-tool"], ["renamed"]]})


async def test_ends_with_an_error_the_script_cannot_catch_when_the_caller_aborts(sandboxes):
    sandbox = sandboxes()
    controller = AbortController()
    recorded = start(
        sandbox,
        """try { await tools.wait(1); } catch { text("caught"); }
        text("after");""",
        signal=controller.signal,
    )
    await waitFor(lambda: len(recorded.calls) == 1)
    controller.abort(RuntimeError("diverged"))
    result = await recorded.run.result
    assert matches(result, {"ok": False, "error": {"kind": "aborted", "message": "diverged"}, "output": []})
    recorded.run.deliver(1, STARTED_AT, 1, {"ok": True, "value": None})


async def test_is_finished_once_it_has_its_outcome(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, "await tools.wait(1); return 2")
    await waitFor(lambda: len(recorded.calls) == 1)
    assert recorded.run.finished is False
    recorded.run.deliver(1, STARTED_AT, 1, {"ok": True, "value": None})
    await recorded.run.result
    assert recorded.run.finished is True


async def test_rejects_a_call_that_is_not_waiting_and_a_result_number_used_twice(sandboxes):
    sandbox = sandboxes()
    recorded = start(sandbox, "await Promise.all([tools.wait(1), tools.wait(2)])")
    await waitFor(lambda: len(recorded.calls) == 2)
    run = recorded.run
    with pytest.raises(ValueError, match="Call 99 is not waiting"):
        run.deliver(1, STARTED_AT, 99, {"ok": True, "value": None})
    run.deliver(1, STARTED_AT, 1, {"ok": True, "value": None})
    with pytest.raises(ValueError, match="used once"):
        run.deliver(1, STARTED_AT, 2, {"ok": True, "value": None})
    with pytest.raises(ValueError, match="used once"):
        run.deliver(0, STARTED_AT, 2, {"ok": True, "value": None})
    run.deliver(2, STARTED_AT, 2, {"ok": True, "value": None})
    assert matches(await run.result, {"ok": True})


async def test_execute_reads_the_real_clock_and_real_randomness():
    sandbox = CodemodeSandbox()
    try:
        before = time.time() * 1000
        first = await sandbox.execute("return [Date.now(), Math.random()]")
        second = await sandbox.execute("return [Date.now(), Math.random()]")
        now, random = first["value"]
        assert now >= math.floor(before)
        assert now <= time.time() * 1000
        assert random not in second["value"]
    finally:
        await sandbox.close()


# -- misaka adaptations ------------------------------------------------------------------------------------------


async def test_rejects_start_arguments_upstream_rejects(sandboxes):
    sandbox = sandboxes()
    base = {"startedAt": STARTED_AT, "seed": 1, "onCall": lambda _call: None}
    for startedAt in [-1, math.inf, math.nan, True]:
        with pytest.raises(ValueError, match="startedAt must be a non-negative number"):
            sandbox.start("", {**base, "startedAt": startedAt})
    for seed in [-1, 2**32, 1.5, True]:
        with pytest.raises(ValueError, match="seed must be a 32-bit unsigned integer"):
            sandbox.start("", {**base, "seed": seed})
    with pytest.raises(ValueError, match="Tool names must be unique"):
        sandbox.start("", {**base, "tools": [{"name": "a"}, {"name": "a"}]})
    with pytest.raises(ValueError, match='Invalid global name "tools"'):
        sandbox.start("", {**base, "globals": [{"name": "tools"}]})
    await sandbox.close()
    with pytest.raises(RuntimeError, match="Sandbox is closed"):
        sandbox.start("", base)


def test_prelude_hash_is_fnv1a_over_utf16_code_units():
    from misaka.codemode.runtime.prelude_source import PRELUDE_SOURCE
    from misaka.codemode.runtime.vm import PRELUDE_HASH, fnv1a

    # FNV-1a test vectors, and one astral character as its two code units.
    assert fnv1a("") == "811c9dc5"
    assert fnv1a("a") == "e40c292c"
    assert fnv1a("\U0001F600") == fnv1a("😀")
    assert PRELUDE_HASH == fnv1a(PRELUDE_SOURCE)
