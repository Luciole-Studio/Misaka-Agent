"""The LCM cache keeps between runs and repairs itself from the session files.

A cache that outlives its processes can disagree with the sessions it was built from. These pin
what happens then, and what the two real failures behind the change were: a session whose rows no
longer matched took upstream's reconciliation tens of minutes or failed the turn (2026-10-27, R38),
and a session written before 2026-10-26 could not be rebuilt at all, because its checkpoint named
the empty reply of a 503 that a retry had dropped (R27).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host import entry_rows, operators, storage
from misaka.extensions.misaka_lcm.vendor import engine as upstream


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def session(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    manager = SessionManager.create(str(workspace), str(workspace.parent / "sessions"))
    manager.appendCustomEntry("lcm-project", {"workspace": str(workspace.resolve())})
    return bound(manager, workspace)


def bound(manager, workspace, source="momo@t_1"):
    return storage.context(SimpleNamespace(cwd=str(workspace), sessionManager=manager, model=None,
                                           modelRegistry=None), workspace, source)


def reopen(ctx):
    path = ctx.sessionManager.getSessionFile()
    return bound(SessionManager.open(path, str(Path(path).parent)), Path(ctx.lcm_project))


def say(ctx, text, tick=0):
    return ctx.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": 1789460410000 + tick})


def reply(ctx, text, tick=0):
    return ctx.sessionManager.appendMessage({
        "role": "assistant", "content": [{"type": "text", "text": text}], "api": "a", "provider": "p", "model": "m",
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        "stopReason": "stop", "timestamp": 1789460410000 + tick})


def failed_attempt(ctx, tick):
    """A provider error's empty reply, dropped by the retry that followed it."""
    entry = ctx.sessionManager.appendMessage({
        "role": "assistant", "content": [], "api": "a", "provider": "p", "model": "m",
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        "stopReason": "error", "errorMessage": "Error code: 503", "timestamp": 1789460420000 + tick})
    ctx.sessionManager.appendContextEdit(entry, None)
    return entry


def rows(ctx):
    built = ce.bound_engine(ctx)
    return built._store._conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?",
                                      (built.current_session_id,)).fetchone()[0]


def grep(ctx, query, **options):
    return json.loads(ce.bound_engine(ctx).handle_tool_call("lcm_grep", {"query": query, **options}))


def checkpoint(ctx, monkeypatch):
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count, built._config.leaf_chunk_tokens = 2, 40
    monkeypatch.setattr(upstream, "summarize_with_escalation", lambda **_: ("Summary. [Expand for details: original]", 1))
    result = ce.compact(ce.prepare({"reason": "manual", "messages": ctx.sessionManager.buildSessionContext().messages}, ctx))
    ctx.sessionManager.appendCompaction(result.summary, result.firstKeptEntryId, result.tokensBefore,
                                        details=result.details, contextMessages=result.contextMessages)
    return result


def duplicate_rows(built):
    connection = built._store._conn
    columns = ",".join(row[1] for row in connection.execute("PRAGMA table_info(messages)") if row[1] != "store_id")
    with connection:
        connection.execute(f"INSERT INTO messages({columns}) SELECT {columns} FROM messages WHERE session_id = ?",
                           (built.current_session_id,))


def test_what_one_session_stored_is_still_found_after_every_process_exited(tmp_path):
    finished, later = session(tmp_path / "project"), session(tmp_path / "project")
    say(finished, "zebraword from a finished card")
    say(later, "another note")
    for ctx in (finished, later):
        ce.sync(ctx)
        ctx.sessionManager.rewrite_file()
    handle = grep(later, "zebraword", session_scope="all")["results"][0]["store_id"]
    ce.close_all()
    assert storage.directory(Path(later.lcm_project)).exists()
    again = reopen(later)
    ce.sync(again)
    assert grep(again, "zebraword", session_scope="all")["total_results"] == 1
    assert "error" not in json.loads(ce.bound_engine(again).handle_tool_call("lcm_expand", {"store_id": handle}))


@pytest.mark.parametrize("messages", [1, 2, 7])
def test_reopening_on_a_kept_cache_adds_no_rows(tmp_path, messages):
    ctx = session(tmp_path / "project")
    for index in range(messages):
        (say if index % 2 == 0 else reply)(ctx, f"message number {index} " + "word " * 20, index)
    ce.sync(ctx)
    ctx.sessionManager.rewrite_file()
    ce.close_all()
    for _ in range(3):
        ctx = reopen(ctx)
        ce.sync(ctx)
        assert rows(ctx) == messages
        ce.close_all()


def test_a_session_whose_rows_doubled_is_rebuilt_instead_of_reconciled(tmp_path, monkeypatch):
    ctx = session(tmp_path / "project")
    for index in range(12):
        say(ctx, f"ALPHA evidence {index} " + "material " * 50, index)
    ce.sync(ctx)
    checkpoint(ctx, monkeypatch)
    ctx.sessionManager.rewrite_file()
    built = ce.bound_engine(ctx)
    frontier = [(node.depth, node.summary) for node in built._summary_frontier_nodes()]
    duplicate_rows(built)
    assert rows(ctx) == 24

    def never(*_args, **_kwargs):
        raise AssertionError("upstream reconciled a store known not to match")

    monkeypatch.setattr(type(built), "_find_reconciled_cursor_for_store_tail", never)
    ce.start(ctx)
    ce.prepare({"reason": "manual", "messages": ctx.sessionManager.buildSessionContext().messages}, ctx)
    assert rows(ctx) == 12
    assert [(node.depth, node.summary) for node in built._summary_frontier_nodes()] == frontier
    assert grep(ctx, "ALPHA")["total_results"] > 0


def test_a_row_the_session_never_had_is_not_a_held_row(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "real row")
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    originals = [row for group in ce._session_originals(ce.snapshot(ctx)).values() for row in group]
    assert ce._rows_held(built, originals) == 1
    stranger = built._store._conn.execute("SELECT store_id FROM messages LIMIT 1").fetchone()[0]
    entry_rows._connection(built._store).execute("UPDATE rows SET entry_id = 'gone' WHERE store_id = ?", (stranger,))
    assert ce._rows_held(built, originals) is None


def test_a_session_partway_through_ingest_is_healthy(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "stored first")
    ce.sync(ctx)
    say(ctx, "not stored yet", 1)
    built = ce.bound_engine(ctx)
    originals = [row for group in ce._session_originals(ce.snapshot(ctx)).values() for row in group]
    assert len(originals) == 2 and ce._rows_held(built, originals) == 1


def test_a_failed_reconciliation_rebuilds_once_and_the_turn_goes_on(tmp_path, monkeypatch):
    ctx = session(tmp_path / "project")
    say(ctx, "kept through the failure")
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    real, calls = built._ingest_messages, []

    def flaky(messages):
        calls.append(len(messages))
        if len(calls) == 1:
            raise OSError("reconciliation failed")
        return real(messages)

    monkeypatch.setattr(built, "_ingest_messages", flaky)
    ce.start(ctx)
    ce.prepare({"reason": "manual", "messages": ctx.sessionManager.buildSessionContext().messages}, ctx)
    assert len(calls) >= 2 and rows(ctx) == 1


def test_a_checkpoint_naming_a_dropped_503_reply_still_rebuilds(tmp_path, monkeypatch):
    ctx = session(tmp_path / "project")
    for index in range(12):
        say(ctx, f"BETA evidence {index} " + "material " * 50, index)
    dropped = failed_attempt(ctx, 20)
    say(ctx, "after the retry", 30)
    ce.sync(ctx)
    result = checkpoint(ctx, monkeypatch)
    entry = ctx.sessionManager.getEntries()[-1]
    assert entry["type"] == "compaction" and result.details["lcm"]["nodes"]
    entry["details"]["lcm"]["nodes"][0]["sourceEntries"].append(dropped)  # as checkpoints before 2026-10-26 did
    ctx.sessionManager.rewrite_file()
    ce.close_all()
    assert storage.clear(Path(ctx.lcm_project))
    again = reopen(ctx)
    ce.sync(again)
    ce.prepare({"reason": "manual", "messages": again.sessionManager.buildSessionContext().messages}, again)
    assert rows(again) == 13


def test_a_deleted_session_leaves_the_cache_with_its_file(tmp_path):
    gone, stays = session(tmp_path / "project"), session(tmp_path / "project")
    say(gone, "zebraword in a deleted session")
    say(stays, "kept note")
    for ctx in (gone, stays):
        ce.sync(ctx)
        ctx.sessionManager.rewrite_file()
    ce.close_all()
    Path(gone.sessionManager.getSessionFile()).unlink()
    again = reopen(stays)
    ce.sync(again)
    assert grep(again, "zebraword", session_scope="all")["total_results"] == 0
    assert grep(again, "kept")["total_results"] == 1


def test_a_deleted_session_is_left_alone_while_others_are_at_work(tmp_path):
    """Housekeeping belongs to the first owner: a running session must not see rows vanish."""
    project = (tmp_path / "project").resolve()
    storage.acquire(project)
    assert storage.take_first_owner(project) and not storage.take_first_owner(project)
    storage.release(project)


def test_an_unreadable_database_is_replaced_by_a_fresh_cache(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "before the damage")
    ce.sync(ctx)
    ctx.sessionManager.rewrite_file()
    database = Path(ce.bound_engine(ctx)._store.db_path)
    ce.close_all()
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    database.write_bytes(b"this is not a database" * 400)
    again = reopen(ctx)
    ce.sync(again)
    assert rows(again) == 1


def test_letting_go_of_the_cache_does_not_wait_for_the_gate(tmp_path):
    """Every process of a run exits at once; they used to queue here for 30 s each to delete it."""
    project = (tmp_path / "project").resolve()
    storage.acquire(project)
    _, gate, _ = storage._paths(project)
    with gate:
        storage.release(project)
    assert project not in storage._LEASES


def test_misaka_lcm_clear_removes_the_cache_and_no_session(tmp_path, monkeypatch, capsys):
    ctx = session(tmp_path / "project")
    say(ctx, "note")
    ce.sync(ctx)
    ctx.sessionManager.rewrite_file()
    archive = Path(ctx.sessionManager.getSessionFile())
    saved = archive.read_bytes()
    monkeypatch.chdir(ctx.lcm_project)
    assert operators.main(["clear"]) == 1  # this process is using it
    assert "in use" in capsys.readouterr().out
    ce.close_all()
    assert operators.main(["clear"]) == 0
    assert not storage.directory(Path(ctx.lcm_project)).exists()
    assert archive.read_bytes() == saved


def test_clearing_where_there_is_no_cache_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert operators.main(["clear"]) == 0
    assert list(tmp_path.iterdir()) == [] or not (tmp_path / ".misaka").exists()
