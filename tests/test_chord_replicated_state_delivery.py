"""pi chord's ``test/replicated-state-delivery.test.ts`` (``test/state-delivery.test.ts`` until 1.2.0).

A listener returning an awaitable (a coroutine or a future) is awaited, as one returning a promise is. A
coroutine listener runs at once, up to its first suspension, as a JavaScript async function does.
"""

from __future__ import annotations

import asyncio

import pytest

from misaka.chord import replicatedState
from misaka.chord.context import BACKGROUND_CONTEXT, withCancel


class Deferred:
    def __init__(self):
        self.promise = asyncio.get_running_loop().create_future()

    def resolve(self):
        self.promise.set_result(None)

    def reject(self, error):
        self.promise.set_exception(error)


async def waitFor(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def fixture(kind, onError=lambda error: None):
    if kind == "mutable":
        mutable = replicatedState({"value": 0})
        return mutable, lambda value, context: mutable.replace(context, value)
    else:
        holder = {}

        class Attachment:
            def __init__(self):
                self.snapshot = {"value": {"value": 0}, "cursor": 0}

            def activate(self, listener):
                holder["receive"] = listener

            def dispose(self):
                pass

        class Source:
            def attach(self):
                return Attachment()

        state = replicatedState(Source(), {"onError": onError})
        return state, lambda value, context: holder["receive"](
            {"value": value, "cursor": value["value"], "ops": [["s", ["value"], value["value"]]], "context": context}
        )


KINDS = ["mutable", "attached"]


@pytest.mark.parametrize("kind", KINDS)
async def test_awaits_hydration_and_each_update_independently_of_other_subscribers(kind):
    state, publish = fixture(kind)
    hydration = Deferred()
    update = Deferred()
    events = []
    fast = []

    async def slow(value, _context, _delivery):
        events.append(f"start:{value['value']}")
        if value["value"] == 0:
            await hydration.promise
        if value["value"] == 1:
            await update.promise
        events.append(f"end:{value['value']}")

    state.subscribe(slow)
    state.subscribe(lambda value, _context, _delivery: fast.append(value["value"]))
    publish({"value": 1}, BACKGROUND_CONTEXT)
    publish({"value": 2}, BACKGROUND_CONTEXT)
    assert events == ["start:0"]
    assert fast == [0, 1, 2]
    assert state.value == {"value": 2}
    hydration.resolve()
    await waitFor(lambda: events == ["start:0", "end:0", "start:1"])
    update.resolve()
    await waitFor(lambda: events == ["start:0", "end:0", "start:1", "end:1", "start:2", "end:2"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("count", [100, 101, 102, 201, 202])
async def test_bounds_pending_deliveries(kind, count):
    state, publish = fixture(kind)
    hydration = Deferred()
    received = []

    def listener(value, _context, _delivery):
        received.append(value["value"])
        if value["value"] == 0:
            return hydration.promise
        return None

    state.subscribe(listener)
    for value in range(1, count + 1):
        publish({"value": value}, BACKGROUND_CONTEXT)
    assert received == [0]
    hydration.resolve()
    await hydration.promise
    await asyncio.sleep(0)
    first = (count - 1) // 100 * 100 + 1
    assert received == [0, *range(first, count + 1)]


@pytest.mark.parametrize("kind", KINDS)
async def test_excludes_a_running_update_from_overflow_and_retains_exact_value_context_delivery(kind):
    state, publish = fixture(kind)
    gate = Deferred()
    context = withCancel(BACKGROUND_CONTEXT).context
    received = []

    def listener(value, listenerContext, delivery):
        received.append({"value": value, "context": listenerContext, "delivery": delivery})
        if value["value"] == 1:
            return gate.promise
        return None

    state.subscribe(listener)
    for value in range(1, 102):
        publish({"value": value}, BACKGROUND_CONTEXT)
    newest = {"value": 102}
    publish(newest, context)
    adopted = state.value
    publish({"value": 103}, BACKGROUND_CONTEXT)
    assert [entry["value"]["value"] for entry in received] == [0, 1]
    gate.resolve()
    await gate.promise
    await asyncio.sleep(0)
    assert [entry["value"]["value"] for entry in received] == [0, 1, 102, 103]
    assert received[2]["value"] is adopted
    assert received[2]["context"] is context
    assert received[2]["delivery"] == {"kind": "update", "sequence": 102}


@pytest.mark.parametrize("kind", KINDS)
def test_serializes_reentrant_hydration_and_update_callbacks(kind):
    state, publish = fixture(kind)
    events = []

    def listener(value, _context, _delivery):
        events.append(f"start:{value['value']}")
        if value["value"] < 2:
            publish({"value": value["value"] + 1}, BACKGROUND_CONTEXT)
        events.append(f"end:{value['value']}")

    state.subscribe(listener)
    assert events == ["start:0", "end:0", "start:1", "end:1", "start:2", "end:2"]


@pytest.mark.parametrize("kind", KINDS)
async def test_treats_two_subscriptions_of_the_same_callback_independently(kind):
    state, publish = fixture(kind)
    gate = Deferred()
    received = []

    def listener(value, _context, _delivery):
        received.append(value["value"])
        if value["value"] == 0:
            return gate.promise
        return None

    stopFirst = state.subscribe(listener)
    stopSecond = state.subscribe(listener)
    publish({"value": 1}, BACKGROUND_CONTEXT)
    stopFirst()
    stopFirst()
    gate.resolve()
    await gate.promise
    await asyncio.sleep(0)
    assert received == [0, 0, 1]
    stopSecond()


@pytest.mark.parametrize("kind", KINDS)
async def test_unsubscribe_drops_queued_callbacks_without_joining_or_aborting_the_running_callback(kind):
    state, publish = fixture(kind)
    gate = Deferred()
    context = withCancel(BACKGROUND_CONTEXT).context
    received = []
    completed = [False]

    async def listener(value, _context, _delivery):
        received.append(value["value"])
        if value["value"] == 1:
            await gate.promise
            completed[0] = True

    stop = state.subscribe(listener)
    await asyncio.sleep(0)
    publish({"value": 1}, context)
    publish({"value": 2}, context)
    assert stop() is None
    publish({"value": 3}, context)
    assert context.abortSignal.aborted is False
    assert completed[0] is False
    gate.resolve()
    await waitFor(lambda: completed[0])
    assert received == [0, 1]


# -- attached listener errors --------------------------------------------------------------------


def test_isolates_a_synchronous_hydration_failure_without_removing_the_subscription():
    kind = "attached"
    errors = []
    state, publish = fixture(kind, errors.append)
    received = []
    failure = RuntimeError("sync hydration")

    def listener(value, _context, _delivery):
        received.append(value["value"])
        if value["value"] == 0:
            raise failure

    state.subscribe(listener)
    publish({"value": 1}, BACKGROUND_CONTEXT)
    assert errors == [failure]
    assert received == [0, 1]


async def test_observes_hydration_rejection_synchronous_throw_and_update_rejection_while_continuing_delivery():
    kind = "attached"
    errors = []
    state, publish = fixture(kind, errors.append)
    gate = Deferred()
    received = []
    fast = []

    def listener(value, _context, _delivery):
        received.append(value["value"])
        if value["value"] == 0:
            return gate.promise
        if value["value"] == 1:
            raise RuntimeError("sync update")
        if value["value"] == 2:
            future = asyncio.get_running_loop().create_future()
            future.set_exception(RuntimeError("async update"))
            return future
        return None

    state.subscribe(listener)
    state.subscribe(lambda value, _context, _delivery: fast.append(value["value"]))
    for value in range(1, 4):
        publish({"value": value}, BACKGROUND_CONTEXT)
    gate.reject(RuntimeError("async hydration"))
    await waitFor(lambda: received == [0, 1, 2, 3])
    assert [str(error) for error in errors] == ["async hydration", "sync update", "async update"]
    assert fast == [0, 1, 2, 3]


async def test_still_observes_a_callback_rejection_after_unsubscribe():
    kind = "attached"
    errors = []
    state, publish = fixture(kind, errors.append)
    gate = Deferred()
    received = []

    def listener(value, _context, _delivery):
        received.append(value["value"])
        return gate.promise

    stop = state.subscribe(listener)
    publish({"value": 1}, BACKGROUND_CONTEXT)
    stop()
    failure = RuntimeError("stopped callback")
    gate.reject(failure)
    await waitFor(lambda: errors == [failure])
    assert received == [0]


async def test_mutable_state_reports_rejected_callbacks_through_its_default_error_reporter():
    loop = asyncio.get_running_loop()
    reports = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        state = replicatedState({"value": 0})
        failure = RuntimeError("async mutable hydration")
        received = []

        def listener(value, _context, _delivery):
            received.append(value["value"])
            if value["value"] == 0:
                future = loop.create_future()
                future.set_exception(failure)
                return future
            return None

        state.subscribe(listener)
        state.replace(BACKGROUND_CONTEXT, {"value": 1})
        await asyncio.sleep(0.01)
        assert received == [0, 1]
        assert len(reports) == 1
        assert reports[0]["exception"] is failure
    finally:
        loop.set_exception_handler(previous)
