"""Offline second-pass fault injections; only temporary state and fake external boundaries."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.platform import tasks
from misaka.core.research import bundle, planner, runs, window, workflow


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_commit", lambda *a, **k: None)
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(
        con,
        workspace=str(tmp_path),
        question="fixture question",
        limits={"max_depth": 1},
    )
    root = runs.nodes(con, run["id"])[0]
    yield con, run, root
    con.close()


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize("writer", [runs.record_action, runs.replace_action])
def test_stale_driver_epoch_must_not_accept_phase_commands(state, takeover, writer):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "old", ttl_seconds=10)
    old = runs.get(con, run["id"])
    if takeover:
        con.execute(
            "UPDATE research_runs SET driver_expires=0 WHERE id=?", (run["id"],)
        )
        assert runs.acquire_driver(con, run["id"], "new")
        with pytest.raises(ValueError):
            writer(
                con,
                old,
                root,
                "plan",
                {"status": "ready"},
                session_file="/tmp/fixture-session",
                tool_call_id="late",
            )
        assert runs.action(con, run["id"], root["id"], "plan") is None
    else:
        assert writer(
            con,
            old,
            root,
            "plan",
            {"status": "ready"},
            session_file="/tmp/fixture-session",
            tool_call_id="on-time",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_approval_wait_checks_ownership_and_preserves_new_owner_state(
    state, monkeypatch, replacement
):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "old")
    run = runs.get(con, run["id"])
    runs.record_action(
        con,
        run,
        root,
        "plan",
        {"status": "ready"},
        session_file="/tmp/fixture-session",
        tool_call_id="plan",
    )
    monkeypatch.setattr(planner, "_roster", lambda cfg: [])
    waiter = asyncio.create_task(
        workflow._await_approval(
            con, {}, None, None, run, root, poll_seconds=0.005, progress=None
        )
    )
    try:
        for _ in range(20):
            if runs.node(con, root["id"])["status"] == "awaiting_approval":
                break
            await asyncio.sleep(0.002)
        if replacement:
            con.execute(
                "UPDATE research_runs SET driver_lock=? WHERE id=?", ("new", run["id"])
            )
            con.execute(
                "UPDATE research_branches SET runner_key='new-node',status='executing' WHERE id=?",
                (root["id"],),
            )
            await asyncio.sleep(0.04)
            assert waiter.done(), (
                "Old approval loop ignored both changed driver and changed node epoch"
            )
            assert runs.node(con, root["id"])["status"] == "executing"
        else:
            runs.request_stop(con, run["id"])
            assert await asyncio.wait_for(waiter, 0.1) == "stopped"
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_old_approval_wait_cannot_overwrite_successor(
    state, monkeypatch
):
    con, run, root = state
    runs.record_action(
        con,
        run,
        root,
        "plan",
        {"status": "ready"},
        session_file="/tmp/fixture-session",
        tool_call_id="plan",
    )
    monkeypatch.setattr(planner, "_roster", lambda cfg: [])
    waiter = asyncio.create_task(
        workflow._await_approval(
            con, {}, None, None, run, root, poll_seconds=0.005, progress=None
        )
    )
    await asyncio.sleep(0.01)
    con.execute(
        "UPDATE research_branches SET runner_key='new',status='executing' WHERE id=?",
        (root["id"],),
    )
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert runs.node(con, root["id"])["status"] == "executing", (
        "Old finally reset successor to planning"
    )


@pytest.mark.parametrize("cite_bundle", [False, True])
def test_rebuild_keeps_cited_bundle_link(state, monkeypatch, cite_bundle):
    con, run, _root = state
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    source = Path(run["workspace"]) / "downloads/source.md"
    source.parent.mkdir()
    source.write_text("fixture source")
    relative = runs.run_path(run, "final.md")
    runs.write_text(
        con,
        run["id"],
        "final",
        "Final",
        relative,
        "## Sources\n- downloads/source.md\n",
    )
    bundle.final_bundle(con, run)
    linked = Path(run["workspace"]) / "final" / f"{run['id']}-sources/source.md"
    assert linked.is_file()
    if cite_bundle:
        locator = linked.relative_to(run["workspace"])
        runs.write_text(
            con, run["id"], "final", "Final", relative, f"## Sources\n- {locator}\n"
        )
    manifest = Path(bundle.final_bundle(con, run)).read_text()
    if cite_bundle:
        assert not linked.exists()
        assert "disposable bundle source" in manifest
        assert "## Cited, already in this folder" not in manifest
        assert source.read_text() == "fixture source"
    else:
        assert linked.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_disposal", [False, True])
async def test_node_session_restores_environment_on_disposal_cancellation(
    state, monkeypatch, cancel_disposal
):
    from misaka.core.platform import session as platform_session

    con, run, root = state
    keys = [
        "MISAKA_AUDIT_MARKER",
        "MISAKA_USAGE_DB",
        "MISAKA_USAGE_TASK_ID",
        "MISAKA_USAGE_GENERATION",
        "MISAKA_USAGE_TOKEN_CAP",
    ]
    for key in keys:
        monkeypatch.setenv(key, "before")
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispose():
        entered.set()
        await release.wait()

    control = SimpleNamespace(accepting=True, paused=False, inputs=set())
    session = SimpleNamespace(
        sessionManager=SimpleNamespace(
            sessionFile=str(Path(run["workspace"]) / "session.jsonl")
        ),
        moments=SimpleNamespace(parts=[SimpleNamespace(control=control)]),
        waitForIdle=AsyncMock(),
    )
    monkeypatch.setattr(planner, "_lo_session", lambda *a: run["workspace"])
    monkeypatch.setattr(
        window.worker,
        "bare_session_setup",
        lambda *a, **k: ([], None, {"MISAKA_AUDIT_MARKER": "inside"}),
    )
    monkeypatch.setattr(
        platform_session,
        "open_session",
        AsyncMock(return_value=(SimpleNamespace(dispose=dispose), session, None)),
    )
    cfg = {
        "roles_root": run["workspace"],
        "provider": "fixture",
        "default_model": "fixture",
        "db": str(Path(run["workspace"]) / "board.db"),
    }

    async def invoke():
        async with window.node_session(con, cfg, run, root):
            pass

    owner = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel_disposal:
            owner.cancel()
            await asyncio.sleep(0.01)
            assert (
                not owner.done()
            )  # Cleanup still owns the environment until disposal ends.
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        assert {k: os.environ.get(k) for k in keys} == {k: "before" for k in keys}
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("git_enabled", [False, True])
@pytest.mark.parametrize("separate_connection", [False, True])
def test_accept_and_reopen_do_not_invert_database_and_card_locks(
    state, monkeypatch, git_enabled, separate_connection
):
    import threading

    import filelock

    from misaka.core.network import dispatch
    from misaka.core.platform import cards, repo

    con, run, _root = state
    peer = (
        tasks.connect(str(Path(run["workspace"]) / "board.db"))
        if separate_connection
        else con
    )
    tid = cards.create(con, run["workspace"], "fixture card", "fixture body", "fixture")
    con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    cards.set_fields(run["workspace"], tid, status="done")
    row = tasks.get(con, tid)
    card_held, db_held = threading.Event(), threading.Event()
    errors = []
    real_mirror = tasks._mirror_status

    def mirror(*args, **kwargs):
        if threading.current_thread().name == "reopen":
            db_held.set()
        return real_mirror(*args, **kwargs)

    def enabled(workspace):
        if threading.current_thread().name == "accept":
            card_held.set()
            assert db_held.wait(1)
            return git_enabled
        return False

    real_lock = filelock.FileLock
    # A finite diagnostic timeout breaks the deadlock; production defaults to no timeout.
    monkeypatch.setattr(
        filelock,
        "FileLock",
        lambda path, *a, **k: real_lock(path, *a, timeout=0.2, **k),
    )
    monkeypatch.setattr(tasks, "_mirror_status", mirror)
    monkeypatch.setattr(repo, "enabled", enabled)
    monkeypatch.setattr(repo, "commit_card", lambda *a, **k: True)
    monkeypatch.setattr(dispatch, "index_artifacts", lambda *a, **k: None)

    def accept():
        try:
            dispatch.accept_side_effects(
                con, row, {"artifacts": []}, generation=1, workspace=run["workspace"]
            )
        except BaseException as e:  # noqa: BLE001 - forward every test-thread failure to the assertion
            errors.append(("accept", type(e).__name__, str(e)))

    def reopen():
        try:
            assert card_held.wait(1)
            tasks.reopen_task(peer, tid, target_status="ready", expected_generation=1)
        except BaseException as e:  # noqa: BLE001 - forward every test-thread failure to the assertion
            errors.append(("reopen", type(e).__name__, str(e)))

    threads = [
        threading.Thread(target=accept, name="accept", daemon=True),
        threading.Thread(target=reopen, name="reopen", daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        if git_enabled:
            import json
            import sys
            import time
            import traceback

            assert db_held.wait(1)
            time.sleep(0.05)
            frames = sys._current_frames()
            stacks = {
                t.name: [
                    {"file": f.filename, "line": f.lineno, "function": f.name}
                    for f in traceback.extract_stack(frames[t.ident])
                ]
                for t in threads
                if t.ident in frames
            }
            (Path(run["workspace"]) / "lock-order-stacks.json").write_text(
                json.dumps(stacks, indent=2)
            )
        for thread in threads:
            thread.join(2)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors, errors
    finally:
        card_held.set()
        db_held.set()
        for thread in threads:
            thread.join(1)
        if separate_connection:
            peer.close()


@pytest.mark.parametrize("collision", [False, True])
def test_bundle_name_collision_never_changes_original_source_bytes(
    state, monkeypatch, collision
):
    import hashlib

    con, run, _root = state
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    relative = "nodes/x/base.md"
    suffix = hashlib.sha256(relative.encode()).hexdigest()[:8]
    sources = {
        "downloads/x/base.md": b"SOURCE ONE",
        relative: b"SOURCE TWO",
        f"downloads/x/base-{suffix if collision else 'different'}.md": b"SOURCE THREE",
    }
    for name, content in sources.items():
        p = Path(run["workspace"]) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    runs.write_text(
        con,
        run["id"],
        "final",
        "Final",
        runs.run_path(run, "final.md"),
        "## Sources\n" + "\n".join("- " + name for name in sources) + "\n",
    )
    bundle.final_bundle(con, run)
    actual = {name: (Path(run["workspace"]) / name).read_bytes() for name in sources}
    assert actual == sources, (
        "Bundling overwrote an ORIGINAL source through a colliding hard-link destination"
    )


@pytest.mark.parametrize("restricted", [False, True])
def test_bundle_honors_shared_private_material_boundary(state, monkeypatch, restricted):
    from misaka.core.tools._web.evidence import check_material_read

    con, run, _root = state
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    name = (
        "downloads/web-evidence/originals/page.md"
        if restricted
        else "downloads/public/page.md"
    )
    source = Path(run["workspace"]) / name
    source.parent.mkdir(parents=True)
    source.write_text("PRIVATE_FIXTURE_ONLY" if restricted else "PUBLIC_FIXTURE")
    if restricted:
        with pytest.raises(ValueError):
            check_material_read(str(source))
    else:
        check_material_read(str(source))
    runs.write_text(
        con,
        run["id"],
        "final",
        "Final",
        runs.run_path(run, "final.md"),
        "## Sources\n- " + name + "\n",
    )
    bundle.final_bundle(con, run)
    packaged = list(
        (Path(run["workspace"]) / "final" / f"{run['id']}-sources").rglob("*.md")
    )
    if restricted:
        assert not packaged, (
            f"Restricted material copied into public bundle: {packaged}"
        )
    else:
        assert len(packaged) == 1
