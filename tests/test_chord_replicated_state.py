"""pi chord's ``test/replicated-state.test.ts`` (``test/state.test.ts`` until 1.2.0, whose name now holds the tests of
state cells)."""

from __future__ import annotations

import sys

import pytest

from misaka.chord import replicatedState
from misaka.chord.context import BACKGROUND_CONTEXT

# -- transactional replicated state --------------------------------------------------------------


def test_publishes_one_immutable_structurally_shared_revision():
    initial = {"changed": {"value": 1}, "retained": {"value": 2}}
    state = replicatedState(initial)
    previous = state.value
    deliveries = []
    state.subscribe(lambda _value, _context, delivery: deliveries.append(delivery["sequence"]))

    def mutate(draft):
        draft["changed"]["value"] = 3
        draft["changed"]["value"] = 4

    state.change(BACKGROUND_CONTEXT, mutate)
    assert state.value == {"changed": {"value": 4}, "retained": {"value": 2}}
    assert state.value is not previous
    assert state.value["changed"] is not previous["changed"]
    assert state.value["retained"] is previous["retained"]
    assert deliveries == [0, 1]


def test_rolls_back_callback_failures_and_revokes_escaped_drafts():
    state = replicatedState({"nested": {"value": 1}})
    previous = state.value
    escaped = []

    def mutate(draft):
        escaped.append(draft["nested"])
        draft["nested"]["value"] = 2
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        state.change(BACKGROUND_CONTEXT, mutate)
    assert state.value is previous
    with pytest.raises(TypeError):
        escaped[0]["value"]


def test_rejects_awaitable_callbacks_and_aborts_their_draft():
    state = replicatedState({"value": 0})
    previous = state.value

    async def finished():
        return None

    def mutate(draft):
        # The draft is written before the awaitable comes back, so the rollback is what keeps the value.
        draft["value"] = 1
        return finished()

    with pytest.raises(TypeError, match="synchronous"):
        state.change(BACKGROUND_CONTEXT, mutate)
    assert state.value is previous


def test_rejects_nested_changes_and_replacements_without_losing_the_outer_rollback():
    state = replicatedState({"left": 0, "right": 0})

    def nestedChange(draft):
        draft["left"] = 1
        state.change(BACKGROUND_CONTEXT, lambda nested: nested.__setitem__("right", 2))

    with pytest.raises(RuntimeError, match="reentrantly"):
        state.change(BACKGROUND_CONTEXT, nestedChange)
    assert state.value == {"left": 0, "right": 0}
    with pytest.raises(RuntimeError, match="change callback"):
        state.change(BACKGROUND_CONTEXT, lambda _draft: state.replace(BACKGROUND_CONTEXT, {"left": 1, "right": 2}))
    assert state.value == {"left": 0, "right": 0}


def test_queues_listener_triggered_changes_in_sequence_order():
    state = replicatedState({"value": 0})
    deliveries = []
    lateDeliveries = []
    nested = [False]

    def onFirst(_value, _context, delivery):
        if delivery["kind"] != "update" or nested[0]:
            return
        nested[0] = True
        state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("value", 2))
        state.subscribe(lambda _value, _context, delivery: lateDeliveries.append(dict(delivery)))

    state.subscribe(onFirst)
    state.subscribe(
        lambda value, _context, delivery: delivery["kind"] == "update"
        and deliveries.append({"value": value["value"], "sequence": delivery["sequence"]})
    )
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("value", 1))
    assert deliveries == [{"value": 1, "sequence": 1}, {"value": 2, "sequence": 2}]
    assert lateDeliveries == [{"kind": "hydrate", "sequence": 2}]


def test_isolates_listener_failures_after_committing_the_revision(monkeypatch):
    # Upstream spies on `queueMicrotask`, which reports the failure later; with no running loop the report here goes to
    # `sys.excepthook`, which this test collects instead.
    reports = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, traceback: reports.append(error))
    state = replicatedState({"value": 0})
    received = []

    def failing(_value, _context, delivery):
        if delivery["kind"] == "update":
            raise RuntimeError("listener failed")

    state.subscribe(failing)
    state.subscribe(lambda _value, _context, delivery: received.append(delivery["sequence"]))
    state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("value", 1))
    assert state.value == {"value": 1}
    assert received == [0, 1]
    assert len(reports) == 1
    assert str(reports[0]) == "listener failed"


def test_copies_assigned_values_by_value():
    external = {"value": 1}
    state = replicatedState({"left": None, "right": None})

    def mutate(draft):
        draft["left"] = external
        draft["right"] = external
        draft["left"]["value"] = 2

    state.change(BACKGROUND_CONTEXT, mutate)
    assert external["value"] == 1
    assert state.value == {"left": {"value": 2}, "right": {"value": 1}}
    assert state.value["left"] is not state.value["right"]


def test_takes_immutable_ownership_of_alias_free_replacements():
    state = replicatedState({"left": {"value": 1}, "right": {"value": 2}})
    replacement = {"left": dict(state.value["left"]), "right": dict(state.value["left"])}
    state.replace(BACKGROUND_CONTEXT, replacement)
    assert state.value is replacement
    assert state.value["left"] is not state.value["right"]
    state.change(BACKGROUND_CONTEXT, lambda draft: draft["left"].__setitem__("value", 9))
    assert state.value == {"left": {"value": 9}, "right": {"value": 1}}


def test_replaces_atomically_and_ignores_deeply_equal_replacements():
    state = replicatedState({"value": {"nested": 1}, "retained": {"nested": 2}})
    previous = state.value
    state.replace(BACKGROUND_CONTEXT, {"value": {"nested": 1}, "retained": {"nested": 2}})
    assert state.value is previous
    state.replace(BACKGROUND_CONTEXT, {**state.value, "value": {"nested": 2}})
    assert state.value == {"value": {"nested": 2}, "retained": {"nested": 2}}
    assert state.value["retained"] is previous["retained"]


# -- authoritative replicated state sources ------------------------------------------------------


class TestSourceAttachment:
    __test__ = False

    def __init__(self, snapshot, onDispose):
        self.snapshot = snapshot
        self._onDispose = onDispose
        self._buffer = []
        self._listener = None
        self._activated = False
        self._disposed = False

    def activate(self, listener):
        if self._activated:
            raise RuntimeError("attachment is already active")
        self._activated = True
        self._listener = listener
        buffered, self._buffer = self._buffer, []
        for frame in buffered:
            listener(frame)

    def publish(self, frame):
        if self._disposed:
            return
        if self._listener is None:
            self._buffer.append(frame)
        else:
            self._listener(frame)

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._buffer.clear()
        self._onDispose()


class TestSource:
    __test__ = False

    def __init__(self, value, cursor=0):
        self.attachments = []
        self.onAttach = None
        self._value = value
        self._cursor = cursor

    def attach(self):
        attachment = TestSourceAttachment(
            {"value": self._value, "cursor": self._cursor}, lambda: self.attachments.remove(attachment)
        )
        self.attachments.append(attachment)
        if self.onAttach is not None:
            self.onAttach()
        return attachment

    def commit(self, value, ops, context=BACKGROUND_CONTEXT, cursor=None):
        cursor = self._cursor + 1 if cursor is None else cursor
        self._value = value
        self._cursor = cursor
        frame = {"cursor": cursor, "value": value, "ops": ops, "context": context}
        for attachment in list(self.attachments):
            attachment.publish(frame)


def test_captures_before_activation_and_drains_queued_commits_in_order():
    initial, first, second = {"value": 0}, {"value": 1}, {"value": 2}
    firstOps, secondOps = [["s", ["value"], 1]], [["s", ["value"], 2]]
    source = TestSource(initial, 10)

    def onAttach():
        source.commit(first, firstOps)
        source.commit(second, secondOps)

    source.onAttach = onAttach

    state = replicatedState(source)
    deliveries = []
    state.subscribe(lambda value, _context, delivery: deliveries.append({"value": value, "sequence": delivery["sequence"]}))
    assert state.value is second
    assert deliveries == [{"value": second, "sequence": 2}]


def test_hydrates_at_sequence_zero_when_attaching_after_existing_commits():
    source = TestSource({"value": 0}, 40)
    current = {"value": 1}
    source.commit(current, [["s", ["value"], 1]])
    state = replicatedState(source)
    deliveries = []
    state.subscribe(lambda _value, _context, delivery: deliveries.append(delivery["sequence"]))
    assert state.value is current
    assert deliveries == [0]


def test_publishes_exact_source_values_without_applying_or_re_diffing():
    source = TestSource({"value": 0})
    state = replicatedState(source)
    next_ = {"value": 1}
    # Deliberately not the operation that produces `next`: Chord must publish the source value as is.
    ops = [["s", ["value"], 7]]
    publishedValue = []
    state.subscribe(
        lambda value, _context, delivery: delivery["kind"] == "update" and publishedValue.append(value)
    )

    source.commit(next_, ops)
    assert state.value is next_
    assert publishedValue[0] is next_


def test_buffers_reentrant_frames_and_skips_updates_covered_by_a_late_hydration():
    source = TestSource({"value": 0})
    state = replicatedState(source)
    received = []
    late = []
    nested = [False]

    def onFirst(_value, _context, delivery):
        if delivery["kind"] != "update" or delivery["sequence"] != 1 or nested[0]:
            return
        nested[0] = True
        source.commit({"value": 2}, [["s", ["value"], 2]])
        state.subscribe(lambda value, _context, delivery: late.append({**delivery, "value": value["value"]}))

    state.subscribe(onFirst)
    state.subscribe(lambda value, _context, delivery: delivery["kind"] == "update" and received.append(value["value"]))

    source.commit({"value": 1}, [["s", ["value"], 1]])
    assert received == [1, 2]
    assert late == [{"kind": "hydrate", "sequence": 2, "value": 2}]


def test_reports_listener_failures_without_throwing_them_into_the_source():
    source = TestSource({"value": 0})
    errors = []
    state = replicatedState(source, {"onError": errors.append})
    received = []

    def failing(_value, _context, delivery):
        if delivery["kind"] == "update":
            raise RuntimeError("listener failed")

    state.subscribe(failing)
    state.subscribe(lambda value, _context, delivery: delivery["kind"] == "update" and received.append(value["value"]))

    source.commit({"value": 1}, [["s", ["value"], 1]])
    source.commit({"value": 2}, [["s", ["value"], 2]])
    assert received == [1, 2]
    assert [str(error) for error in errors] == ["listener failed", "listener failed"]


def test_reports_cursor_gaps_disposes_the_broken_attachment_and_ignores_later_frames():
    source = TestSource({"value": 0}, 5)
    errors = []
    state = replicatedState(source, {"onError": errors.append})
    source.commit({"value": 2}, [["s", ["value"], 2]], BACKGROUND_CONTEXT, 7)
    assert "expected 6, received 7" in str(errors[0])
    assert len(source.attachments) == 0
    assert state.value == {"value": 0}
    source.commit({"value": 3}, [["s", ["value"], 3]], BACKGROUND_CONTEXT, 8)
    assert state.value == {"value": 0}


def test_keeps_attachments_independent_and_disposes_each_idempotently():
    source = TestSource({"value": 0})
    first = replicatedState(source)
    second = replicatedState(source)
    assert len(source.attachments) == 2
    source.commit({"value": 1}, [["s", ["value"], 1]])
    assert first.value["value"] == 1
    assert second.value["value"] == 1

    first.dispose()
    first.dispose()
    assert len(source.attachments) == 1
    source.commit({"value": 2}, [["s", ["value"], 2]])
    assert first.value["value"] == 1
    assert second.value["value"] == 2
    second.dispose()
    assert len(source.attachments) == 0
