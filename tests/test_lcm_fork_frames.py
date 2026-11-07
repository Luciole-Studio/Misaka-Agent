"""A summary frame is never rewritten; the tools translate the number it shows.

A frame shows the database number it was drawn with. A fork imports its parent's frontier as nodes
of its own and a rebuilt cache numbers everything anew, so the number stops being one the tools can
open. Rewriting the frames wrote the whole context into the session file each time, replaced the
summarizer's hint, and worked only once: all 27 research forks that inherited a compaction wrote a
checkpoint before their first request, and on the real `test` sessions none of 65 frames opened
after a wipe. The frames now stay as they were drawn, and the tools open what they show.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import carry, storage, tools
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host.ingest import read_field
from misaka.extensions.misaka_lcm.vendor import engine as upstream


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def say(ctx, text, tick=0):
    ctx.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": 1789460410000 + tick})


def compacted(tmp_path, monkeypatch):
    """A session with a real engine compaction on its branch, and that compaction's node record."""
    project = tmp_path / "project"
    project.mkdir()
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    ctx = storage.context(SimpleNamespace(cwd=str(project), sessionManager=manager, model=None), str(project), "test")
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count = 2
    built._config.leaf_chunk_tokens = 40
    monkeypatch.setattr(upstream, "summarize_with_escalation",
                        lambda **_: ("Summary of ALPHA evidence. [Expand for details: original]", 1))
    for i in range(12):
        say(ctx, f"ALPHA evidence {i} " + "material " * 50, tick=i)
    publish(ctx, "manual")
    return ctx, ce._checkpoint(ce.snapshot(ctx))["details"]["lcm"]["nodes"][0]


def publish(ctx, reason):
    native = ctx.sessionManager.buildSessionContext().messages
    prepared = ce.prepare({"reason": reason, "messages": native}, ctx)
    if prepared is None:
        return None
    result = ce.compact(prepared)
    if result is not None:
        ctx.sessionManager.appendCompaction(result.summary, result.firstKeptEntryId, result.tokensBefore,
                                            details=result.details, contextMessages=result.contextMessages)
    return result


def fork(ctx):
    ce.close(ctx)
    ctx.sessionManager.createBranchedSession(ctx.sessionManager.getLeafId())
    ce.sync(ctx)


def scaffold(ctx):
    return "\n".join(str(read_field(message, "content")) for message in ce._checkpoint(ce.snapshot(ctx))["contextMessages"])


def open_node(ctx, node_id):
    answer = tools._answer("lcm_expand", {"node_id": node_id}, ce.snapshot(ctx), ctx)
    return json.loads(answer.split("\n", 1)[-1]) if not answer.startswith("{") else json.loads(answer)


def test_a_fork_sends_the_frames_its_parent_sent_and_writes_no_checkpoint(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    assert publish(ctx, "threshold") is None
    built = ce.bound_engine(ctx)
    messages = ce._messages(ce.snapshot(ctx), built)
    assert any(record["frame"] in str(message.get("content")) for message in messages)
    assert ce._checkpoint(ce.snapshot(ctx))["details"]["lcm"]["nodes"][0]["id"] == record["id"]


def test_a_fork_opens_the_number_its_frame_shows(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    answer = open_node(ctx, record["id"])
    assert "error" not in answer
    built = ce.bound_engine(ctx)
    own = [node for node in built._dag.get_session_nodes(built.current_session_id, limit=-1)]
    assert own and all(node.node_id != record["id"] for node in own)
    assert answer["node_id"] == own[0].node_id


def test_the_number_also_opens_given_as_text_or_in_a_list(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    built = ce.bound_engine(ctx)
    ce._messages(ce.snapshot(ctx), built)
    local = built._dag.get_session_nodes(built.current_session_id, limit=-1)[0].node_id
    assert ce.local_nodes(built, {"node_id": str(record["id"])})["node_id"] == local
    assert ce.local_nodes(built, {"node_ids": [record["id"], 999999]})["node_ids"] == [local, 999999]
    assert ce.local_nodes(built, {"query": "x"}) == {"query": "x"}


def test_a_session_keeps_opening_its_own_nodes(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    assert open_node(ctx, record["id"])["node_id"] == record["id"]


def test_a_fork_whose_parent_nodes_are_gone_keeps_the_frame_and_opens_it(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    built = ce.bound_engine(ctx)
    built._dag._conn.execute("DELETE FROM summary_nodes WHERE node_id = ?", (record["id"],))
    built._dag._conn.commit()
    messages = ce._messages(ce.snapshot(ctx), built)
    assert any(record["frame"] in str(message.get("content")) for message in messages)
    assert "ALPHA evidence" in json.dumps(open_node(ctx, record["id"]))


def test_a_number_now_naming_another_summary_still_opens_this_sessions_copy(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    built = ce.bound_engine(ctx)
    built._dag._conn.execute("UPDATE summary_nodes SET summary = 'something else' WHERE node_id = ?", (record["id"],))
    built._dag._conn.commit()
    ce._messages(ce.snapshot(ctx), built)
    own = built._dag.get_session_nodes(built.current_session_id, limit=-1)[0]
    assert ce.local_nodes(built, {"node_id": record["id"]})["node_id"] == own.node_id
    assert own.summary != "something else"


def test_a_checkpoint_records_the_frame_it_keeps_so_a_reopened_fork_still_opens_it(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    built = ce.bound_engine(ctx)
    ce._messages(ce.snapshot(ctx), built)
    node = built._dag.get_session_nodes(built.current_session_id, limit=-1)[0]
    assert ce._frame_shown(built, node, [{"role": "user", "content": "x " + record["frame"]}]) == record["frame"]
    assert ce._frame_shown(built, node, [{"role": "user", "content": ce._node_text(node)}]) == ce._node_text(node)
    assert ce._frame_shown(built, node, [{"role": "user", "content": [{"type": "text", "text": record["frame"]}]}]) \
        == record["frame"]


def test_a_forks_own_compaction_shows_its_own_numbers_and_they_open(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count = 2
    built._config.leaf_chunk_tokens = 40
    for i in range(12):
        say(ctx, f"BETA evidence {i} " + "material " * 50, tick=100 + i)
    assert publish(ctx, "manual") is not None
    records = ce._checkpoint(ce.snapshot(ctx))["details"]["lcm"]["nodes"]
    assert all(item["id"] != record["id"] for item in records)
    assert record["frame"] not in scaffold(ctx)
    assert all(item["frame"] in scaffold(ctx) for item in records)
    assert "error" not in open_node(ctx, records[0]["id"])


def test_a_reopened_fork_keeps_the_frames_and_the_number(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    assert publish(ctx, "threshold") is None
    source = Path(ctx.sessionManager.getSessionFile())
    ce.close(ctx)
    ctx.sessionManager.rewrite_file()
    before = source.read_bytes()
    ce.close_all()
    restored = SessionManager.openInMemory(str(source))
    reopened = storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=restored, model=None), ctx.lcm_project)
    ce.sync(reopened)
    assert publish(reopened, "threshold") is None
    assert "error" not in open_node(reopened, record["id"])
    assert source.read_bytes() == before


def wipe(ctx):
    ce.close_all()
    assert storage.clear(storage.project(ctx))
    ce.sync(ctx)


def test_every_wipe_leaves_the_context_as_it_was_and_its_frames_open(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    before = scaffold(ctx)
    entries = len(ctx.sessionManager.getEntries())
    for _ in range(3):
        wipe(ctx)
        assert publish(ctx, "threshold") is None
        assert "ALPHA evidence" in json.dumps(open_node(ctx, record["id"]))
    assert scaffold(ctx) == before
    assert len(ctx.sessionManager.getEntries()) == entries


def test_a_wipe_keeps_the_hint_the_summarizer_wrote(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    assert "MISAKA session compaction" not in record["frame"]
    wipe(ctx)
    built = ce.bound_engine(ctx)
    assert any(record["frame"] in str(message.get("content")) for message in ce._messages(ce.snapshot(ctx), built))


def test_a_fork_and_its_parent_each_open_the_same_frame_after_a_wipe(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    wipe(ctx)
    built = ce.bound_engine(ctx)
    answer = open_node(ctx, record["id"])
    assert answer["node_id"] == built._dag.get_session_nodes(built.current_session_id, limit=-1)[0].node_id


def test_a_retried_turn_in_a_fork_does_not_lose_the_number_its_frame_shows(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    fork(ctx)
    say(ctx, "next question", tick=50)
    ctx.sessionManager.appendMessage({
        "role": "assistant", "content": [{"type": "text", "text": "attempt that failed"}], "api": "a", "provider": "p",
        "model": "m", "stopReason": "error", "timestamp": 1789460410051,
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}})
    ce.sync(ctx)
    live = ctx.sessionManager.buildSessionContext().messages[:-1]    # the native retry dropped the attempt
    prepared = ce.prepare({"reason": "threshold", "messages": live}, ctx)
    result = ce.compact(prepared)
    assert result is not None and result.summary == "LCM replay update"
    ctx.sessionManager.appendCompaction(result.summary, result.firstKeptEntryId, result.tokensBefore,
                                        details=result.details, contextMessages=result.contextMessages)
    assert result.details["lcm"]["nodes"][0]["frame"] == record["frame"]
    ce.close(ctx)
    ce.sync(ctx)
    assert "ALPHA evidence" in json.dumps(open_node(ctx, record["id"]))
    wipe(ctx)
    assert "ALPHA evidence" in json.dumps(open_node(ctx, record["id"]))


def test_the_fence_reads_a_kept_frame_as_the_node_it_stands_for(tmp_path, monkeypatch):
    ctx, record = compacted(tmp_path, monkeypatch)
    wipe(ctx)
    built = ce.bound_engine(ctx)
    ce._messages(ce.snapshot(ctx), built)
    assert ce._guarded_summary(built, record["frame"]) == record["frame"]


def test_a_carried_session_opens_its_frames_after_a_wipe(tmp_path, monkeypatch):
    ctx, _ = compacted(tmp_path, monkeypatch)
    ctx.sessionManager.appendCustomEntry("lcm-project", {"workspace": ctx.lcm_project})
    ctx.sessionManager.rewrite_file()
    ce.bound_engine(ctx)._config.new_session_retain_depth = -1
    target = SessionManager.create(ctx.cwd, ctx.sessionManager.getSessionDir())
    carry.rollover(target, ce.snapshot(ctx), ctx)
    carried = storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=target, model=None), ctx.lcm_project)
    target.appendCustomEntry("lcm-project", {"workspace": ctx.lcm_project})
    say(carried, "the carried session goes on", tick=90)
    target.rewrite_file()
    ce.sync(carried)
    number = ce._checkpoint(ce.snapshot(carried))["details"]["lcm"]["nodes"][0]["id"]
    wipe(carried)
    assert "ALPHA evidence" in json.dumps(open_node(carried, number))
