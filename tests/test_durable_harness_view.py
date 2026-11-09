"""pi durable's ``test/harness-view.test.ts``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText
from misaka.chord.context import withAbortSignal
from misaka.chord.delta import applyImmutable
from misaka.durable import (
    AgentDoc,
    InboxDoc,
    LiveDoc,
    MemoryStorage,
    ProviderDoc,
    UsageDoc,
    defineDoc,
)
from misaka.utils.js import AbortController
from tests.chat_support import allEntries, chatSetup, openChat, waitFor
from tests.durable_support import context, flush
from tests.task_support import aborted, deferred, race

MOUNTED = ["pi.agent", "pi.inbox", "pi.live", "pi.provider", "pi.usage"]


async def record(conversation: Any) -> SimpleNamespace:
    """Start a watch of ``conversation`` that records its acquisition revision and every delivered frame."""
    watch = await conversation.watch(context)
    initial = watch.value
    frames: list[dict[str, Any]] = []

    async def onFrame(value: Any, ops: Any, callContext: Any) -> None:
        frames.append({"value": value, "ops": ops})

    watch.start(onFrame)
    return SimpleNamespace(initial=initial, frames=frames, stop=lambda: watch.stop())


async def fresh(conversation: Any) -> Any:
    """A freshly built view of ``conversation``, for comparing with an advanced one."""
    state = await conversation.viewState(context)
    value = state.value
    state.dispose()
    return value


async def committed(harness: Any, conversation: Any, conversationRecord: Any) -> dict[str, Any]:
    """The view as committed state defines it, read without any mount: the active entries and the built-in documents."""
    docs: dict[str, Any] = {}
    for token in (AgentDoc, LiveDoc, InboxDoc, ProviderDoc, UsageDoc):
        value = await harness.snapshot(token, conversation.id, context)
        if value is not None:
            docs[token["definition"]["kind"]] = value
    return {"conversation": conversationRecord, "entries": (await conversation.context(context))["entries"], "docs": docs}


def replay(initial: Any, frames: list[dict[str, Any]]) -> Any:
    """Replay every frame's operations from ``initial``, checking each delivered revision on the way."""
    value = initial
    for frame in frames:
        value = applyImmutable(value, frame["ops"])
        assert value == frame["value"]
    return value


def answer(text: str) -> Any:
    return fauxAssistantMessage([fauxText(text)])


async def drained() -> None:
    """Let watch callbacks, which run after the commit, catch up."""
    await flush()


def touches(harness: Any, conversation: Any) -> SimpleNamespace:
    counter = SimpleNamespace(count=0)

    def onCommit(publication: Any, callContext: Any) -> None:
        touched = any(
            (change["type"] == "entry" and change["value"]["conversationId"] == conversation.id)
            or (
                change["type"] == "document"
                and change.get("conversationId") == conversation.id
                and change["record"]["kind"] in MOUNTED
                and len(change["ops"]) > 0
            )
            for change in publication["changes"]
        )
        if touched:
            counter.count += 1

    harness.subscribeCommits(onCommit)
    return counter


def note(root: Any, kind: str) -> Any:
    return root.commit(lambda tx: tx.appendEntry(root.id, {"kind": kind}), context)


async def test_hydrates_the_active_entries_and_the_built_in_documents():
    setup = chatSetup()
    setup.faux.setResponses([answer("hello")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    view = await fresh(root)
    assert view["conversation"] == {"id": root.id}
    assert view["entries"] == await allEntries(root)
    assert sorted(view["docs"].keys()) == MOUNTED
    assert view["docs"]["pi.live"] == {}
    assert view["docs"]["pi.inbox"] == {"items": []}
    await harness.close(context)


async def test_publishes_one_frame_per_touching_commit_whose_operations_rebuild_every_revision():
    setup = chatSetup()
    release = deferred()

    async def step(_request: Any, options: Any, *_: Any) -> Any:
        await race(release.promise, aborted(options.signal))
        return answer("a longer answer")

    setup.faux.setResponses([step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    recorded = await record(root)
    touching = touches(harness, root)
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    await waitFor(lambda: any(frame["value"]["docs"]["pi.live"].get("generation") for frame in recorded.frames))
    release.resolve()
    await submission.wait(context)
    await harness.waitForIdle(context)
    await drained()
    frames = recorded.frames
    assert len(frames) == touching.count
    assert replay(recorded.initial, frames) == await committed(harness, root, recorded.initial["conversation"])
    assert any(
        op[0] == "p" and op[1] == ["entries"] and op[2] == 0 and op[3] == 0 and [entry["kind"] for entry in op[4]] == ["pi.user"]
        for op in frames[0]["ops"]
    )
    # Document operations keep their exact shape under the mount path.
    assert ["s", ["docs", "pi.live", "generation"], {"attempt": 1}] in [op for frame in frames for op in frame["ops"]]
    await recorded.stop()
    await harness.close(context)


async def test_shares_unchanged_parts_between_revisions_and_skips_commits_that_touch_nothing_mounted():
    setup = chatSetup()
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    recorded = await record(root)
    await other.commit(lambda tx: tx.appendEntry(other.id, {"kind": "note"}), context)
    await root.configure({"thinkingLevel": "high"}, context)
    await note(root, "note")
    await drained()
    frames = recorded.frames
    assert len(frames) == 2
    assert frames[0]["ops"] == [["s", ["docs", "pi.agent", "thinkingLevel"], "high"]]
    assert frames[0]["value"]["entries"] is recorded.initial["entries"]
    assert frames[0]["value"]["docs"]["pi.live"] is recorded.initial["docs"]["pi.live"]
    assert frames[1]["value"]["docs"] is frames[0]["value"]["docs"]
    await recorded.stop()
    await harness.close(context)


async def test_cuts_the_entries_at_a_head_marker_keeping_the_entries_from_its_head():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    await note(root, "a")
    b = await note(root, "b")
    await note(root, "c")
    recorded = await record(root)
    summary = await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "summary", "head": b["id"]}), context)
    await note(root, "d")
    await root.reset(None, context)
    await drained()
    frames = recorded.frames
    kinds = [[entry["kind"] for entry in frame["value"]["entries"]] for frame in frames]
    assert kinds == [["summary", "b", "c"], ["summary", "b", "c", "d"], ["pi.reset"]]
    assert frames[0]["ops"] == [["p", ["entries"], 0, 1, [summary]]]
    assert replay(recorded.initial, frames) == await committed(harness, root, recorded.initial["conversation"])
    await recorded.stop()
    await harness.close(context)


async def test_keeps_only_mounted_entries_for_a_raw_head_write_that_targets_before_the_active_range():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    old = await note(root, "old")
    await root.reset(None, context)
    recorded = await record(root)
    await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "summary", "head": old["id"]}), context)
    await drained()
    # Model context now starts at `old` again, but the mount never held it (spec 12); a rebuilt mount shows it.
    assert [entry["kind"] for entry in recorded.frames[0]["value"]["entries"]] == ["summary"]
    await recorded.stop()
    assert [entry["kind"] for entry in (await fresh(root))["entries"]] == ["summary", "old"]
    await harness.close(context)


async def test_cuts_a_forks_view_into_its_inherited_entries():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    a = await note(root, "a")
    b = await note(root, "b")
    fork = await root.fork(b["id"], {"ownership": {"kind": "ownerless"}}, context)
    recorded = await record(fork)
    await fork.commit(lambda tx: tx.appendEntry(fork.id, {"kind": "summary", "head": b["id"]}), context)
    await drained()
    assert [entry["id"] for entry in recorded.initial["entries"]] == [a["id"], b["id"]]
    assert [entry["kind"] for entry in recorded.frames[0]["value"]["entries"]] == ["summary", "b"]
    assert recorded.frames[0]["value"] == await committed(harness, fork, recorded.initial["conversation"])
    await recorded.stop()
    await harness.close(context)


async def test_shows_a_forks_inherited_entries_and_follows_only_the_forks_own_commits():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    first = await note(root, "first")
    fork = await root.fork(first["id"], {"ownership": {"kind": "ownerless"}}, context)
    recorded = await record(fork)
    assert [entry["kind"] for entry in recorded.initial["entries"]] == ["first"]
    assert recorded.initial["conversation"]["parent"] == {"conversationId": root.id, "at": first["id"]}
    await note(root, "parent")
    await fork.commit(lambda tx: tx.appendEntry(fork.id, {"kind": "child"}), context)
    await drained()
    assert [[entry["kind"] for entry in frame["value"]["entries"]] for frame in recorded.frames] == [["first", "child"]]
    await recorded.stop()
    await harness.close(context)


async def test_unmounts_a_retired_document_and_mounts_its_recreation_whole():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    recorded = await record(root)
    await root.commit(lambda tx: tx.retireDoc(LiveDoc, root.id), context)

    async def recreate(tx: Any) -> None:
        (await tx.doc(LiveDoc, root.id))["tools"] = []

    await root.commit(recreate, context)
    await drained()
    frames = recorded.frames
    assert [frame["ops"] for frame in frames] == [
        [["d", ["docs", "pi.live"]]],
        [["s", ["docs", "pi.live"], {"tools": []}]],
    ]
    assert "pi.live" not in frames[0]["value"]["docs"]
    await recorded.stop()
    await harness.close(context)


async def test_replaces_undelivered_frames_with_the_newest_view_after_100_pending_frames():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    watch = await root.watch(context)
    for _ in range(101):
        await note(root, "note")
    frames: list[dict[str, Any]] = []

    async def onFrame(value: Any, ops: Any, callContext: Any) -> None:
        frames.append({"value": value, "ops": ops})

    watch.start(onFrame)
    await drained()
    assert len(frames) == 1
    assert frames[0]["ops"] == [["r", frames[0]["value"]]]
    assert len(frames[0]["value"]["entries"]) == 101
    await watch.stop()
    await harness.close(context)


async def test_keeps_states_and_watches_of_one_conversation_independent_and_remounts_after_the_last_one_detaches():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    state = await root.viewState(context)
    recorded = await record(root)
    await note(root, "one")
    await drained()
    assert [entry["kind"] for entry in state.value["entries"]] == ["one"]
    await recorded.stop()
    assert len(recorded.frames) == 1
    await note(root, "two")
    await drained()
    assert [entry["kind"] for entry in state.value["entries"]] == ["one", "two"]
    last = state.value
    state.dispose()
    # No observer is left, so the mount was dropped: a new observer builds a new revision from committed state.
    rebuilt = await fresh(root)
    assert rebuilt == last
    assert rebuilt is not last
    await harness.close(context)


async def test_ends_states_and_watches_at_close_and_rejects_later_acquisition():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    watch = await root.watch(context)
    state = await root.viewState(context)
    await harness.close(context)
    assert await watch.closed == {"reason": "session_closed"}
    assert state.value["entries"] == []
    with pytest.raises(Exception):  # noqa: B017
        await root.watch(context)
    with pytest.raises(Exception):  # noqa: B017
        await root.viewState(context)


async def test_rejects_an_acquisition_cancelled_or_closed_while_it_waits_for_the_session_line():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root

    def hold() -> SimpleNamespace:
        release = deferred()

        async def block(tx: Any) -> None:
            await release.promise

        blocking = root.commit(block, context)
        return SimpleNamespace(release=release.resolve, blocking=blocking)

    held = hold()
    controller = AbortController()
    cancelled = root.watch(withAbortSignal(controller.signal, context))
    controller.abort(RuntimeError("cancelled"))
    held.release()
    await held.blocking
    with pytest.raises(Exception, match="cancelled"):
        await cancelled

    held = hold()
    closedWhileQueued = root.watch(context)
    closing = harness.close(context)
    held.release()
    await held.blocking
    with pytest.raises(Exception, match="Harness is closed"):
        await closedWhileQueued
    await closing


async def test_shares_one_mount_between_concurrent_observers_and_isolates_a_failing_listener():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    failing = await root.watch(context)
    recorded = await record(root)
    shared = await root.viewState(context)
    assert failing.value is shared.value
    shared.dispose()

    async def fail(value: Any, ops: Any, callContext: Any) -> None:
        raise RuntimeError("listener failed")

    failing.start(fail)
    await note(root, "one")
    await note(root, "two")
    await drained()
    assert (await failing.closed)["reason"] == "listener_error"
    assert len(recorded.frames) == 2
    await recorded.stop()
    await harness.close(context)


async def test_publishes_one_frame_for_a_commit_that_appends_several_entries_and_edits_a_document():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    Other = defineDoc(
        {
            "kind": "app.other",
            "version": 1,
            "scope": "conversation",
            "history": "latest",
            "fork": "initial",
            "initial": lambda: {"n": 0},
        }
    )
    recorded = await record(root)

    async def several(tx: Any) -> None:
        await tx.appendEntry(root.id, {"kind": "a"})
        (await tx.doc(LiveDoc, root.id))["tools"] = []
        await tx.appendEntry(root.id, {"kind": "b"})

    await root.commit(several, context)

    # A document that is not mounted publishes nothing.
    async def unmounted(tx: Any) -> None:
        (await tx.doc(Other, root.id))["n"] = 1

    await root.commit(unmounted, context)
    await drained()
    frames = recorded.frames
    assert len(frames) == 1
    assert [entry["kind"] for entry in frames[0]["value"]["entries"]] == ["a", "b"]
    assert replay(recorded.initial, frames) == await committed(harness, root, recorded.initial["conversation"])
    await recorded.stop()
    await harness.close(context)
