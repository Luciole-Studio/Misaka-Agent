"""Offline expected-contract checks. Real database/workflow; no model, network, or live process actions."""

import asyncio
import itertools
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.platform import tasks
from misaka.core.research import report, runs, workflow


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


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_first_poll", [False, True])
async def test_resident_node_completion_is_not_lost(state, finish_before_first_poll):
    con, run, root = state
    key = runs.prepare_runner(con, "research_branches", root["id"])
    assert runs.claim_runner(con, "research_branches", root["id"], key)
    handles = {root["id"]: SimpleNamespace(pid=os.getpid())}
    spawner = SimpleNamespace(alive=lambda handle: True)

    def finish():
        runs.set_node(con, root["id"], status="closed")
        runs.release_runner(con, "research_branches", root["id"], key)

    if finish_before_first_poll:
        finish()
    else:
        asyncio.get_running_loop().call_later(0.04, finish)
    result = await asyncio.wait_for(
        workflow._wait_level(
            con, {}, spawner, run, handles, poll_seconds=0.005, progress=None
        ),
        0.15,
    )
    assert result == "done"


@pytest.mark.asyncio
async def test_parent_spawn_reply_does_not_restore_released_runner(state):
    con, _run, root = state
    key = runs.prepare_runner(con, "research_branches", root["id"])
    assert runs.claim_runner(con, "research_branches", root["id"], key)
    runs.set_node(con, root["id"], status="closed")
    runs.release_runner(con, "research_branches", root["id"], key)
    # A late pane.create reply still names the living resident window.
    workflow._record_runner(
        con, "research_branches", root["id"], SimpleNamespace(pid=os.getpid()), key=key
    )
    assert runs.node(con, root["id"])["runner_pid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("clarify_first", [False, True])
async def test_one_clarifying_node_does_not_revoke_its_sibling(
    state, monkeypatch, clarify_first
):
    con, run, root = state
    a = runs.create_node(
        con, run["id"], trigger="question A", parent_id=root["id"], depth=1
    )
    b = runs.create_node(
        con, run["id"], trigger="question B", parent_id=root["id"], depth=1
    )
    runs.record_action(
        con,
        run,
        a,
        "plan",
        {
            "status": "clarify",
            "plan_markdown": "Need input",
            "tasks": [],
            "red_team": None,
            "clarifying_questions": ["Which period?"],
        },
        session_file="/tmp/fixture-a.jsonl",
        tool_call_id="plan-a",
    )
    if clarify_first:
        result = await workflow._expand(
            con,
            {},
            None,
            None,
            run,
            a,
            context=None,
            tool_call_id="fixture",
            poll_seconds=0.001,
            progress=None,
        )
        assert result["reason"] == "waiting_input"
    # Sibling was already running before node A paused the level.
    accepted = runs.record_action(
        con,
        run,
        b,
        "plan",
        {"status": "ready"},
        session_file="/tmp/fixture-b.jsonl",
        tool_call_id="plan-b",
    )
    assert accepted["tool_call_id"] == "plan-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_index", [False, True])
async def test_derived_index_failure_does_not_reverse_completed_run(
    state, monkeypatch, fail_index
):
    con, run, root = state
    runs.record_action(
        con,
        run,
        root,
        "plan",
        {"red_team": {"assignee": "fixture"}},
        session_file="/tmp/fixture.jsonl",
        tool_call_id="plan",
    )
    runs.set_node(con, root["id"], status="closed")
    monkeypatch.setattr(
        report,
        "prepare",
        lambda *a, **k: {
            "id": "draft",
            "sha256": "fixture",
            "path": "/tmp/fixture-draft.md",
        },
    )
    monkeypatch.setattr(
        workflow,
        "_submit_tasks",
        AsyncMock(return_value={"@final-review": "fixture-review"}),
    )
    monkeypatch.setattr(workflow, "_drive_tasks", AsyncMock(return_value="done"))
    monkeypatch.setattr(workflow, "settle_done_tasks", lambda *a, **k: None)
    monkeypatch.setattr(report, "review_receipt", lambda *a, **k: {})

    def finalize(*a, **k):
        aid, path = runs.write_text(
            con,
            run["id"],
            "final",
            "Final",
            runs.run_path(run, "final.md"),
            "Final fixture",
        )
        return {"artifact": aid, "path": path}

    monkeypatch.setattr(report, "finalize", finalize)

    def index(*a, **k):
        assert runs.get(con, run["id"])["status"] == "done"
        if fail_index:
            raise OSError("fixture workspace-index write failed")

    monkeypatch.setattr(workflow, "_refresh_workspace_index", index)
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    from contextlib import asynccontextmanager

    from misaka.core.research import window

    @asynccontextmanager
    async def root_session(*args):
        yield SimpleNamespace(session=object(), session_file="/tmp/fixture.jsonl", close=AsyncMock())

    monkeypatch.setattr(window, "node_session", root_session)
    try:
        await workflow.run(con, {}, SimpleNamespace(), run_id=run["id"])
    except OSError as e:
        assert fail_index and "workspace-index" in str(e)
    saved = runs.get(con, run["id"])
    assert saved["phase"] == "done"
    assert saved["status"] == "done", dict(saved)
    assert saved["driver_lock"] is None


@pytest.mark.parametrize("value", [True, 1.8, -0.5])
def test_depth_requires_an_integer(value):
    with pytest.raises(ValueError):
        runs.normalize_limits({"max_depth": value})


@pytest.mark.parametrize("document_id", ["A", "B"])
def test_bundle_url_resolution_keeps_document_identity(state, monkeypatch, document_id):
    from pathlib import Path

    from misaka.core.research import bundle
    from misaka.core.web.evidence import _frontmatter

    con, run, _root = state
    pages = Path(run["workspace"]) / "downloads/pages"
    pages.mkdir(parents=True)
    source = pages / "saved.md"
    source.write_text(
        _frontmatter({"source_url": "https://fixture.invalid/document?id=A"})
        + "Document A\n"
    )
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    index = bundle._Index(run["workspace"])
    collector = bundle._Collector(con, run, index)
    resolved, _reason = collector._resolve(
        f"https://fixture.invalid/document?id={document_id}", [run["workspace"]]
    )
    if document_id == "A":
        assert resolved == str(source.resolve())
    else:
        assert resolved is None, f"Unsaved document B resolved to {resolved}"


@pytest.mark.parametrize("mutate_source", [False, True])
def test_bundle_manifest_digest_matches_packaged_bytes(
    state, monkeypatch, mutate_source
):
    import hashlib
    from pathlib import Path

    from misaka.core.research import bundle

    con, run, _root = state
    source = Path(run["workspace"]) / "downloads/source.md"
    source.parent.mkdir()
    source.write_text("version one\n")
    old_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    runs.register_file(con, run["id"], "source", "Source", str(source), sha256=old_sha)
    runs.write_text(
        con,
        run["id"],
        "final",
        "Final",
        runs.run_path(run, "final.md"),
        "# Final\n## Sources\n- downloads/source.md\n",
    )
    if mutate_source:
        source.write_text("version TWO\n")
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    manifest = Path(bundle.final_bundle(con, run))
    placed = Path(run["workspace"]) / "final" / f"{run['id']}-sources/source.md"
    if mutate_source:
        assert not placed.exists()
        assert "source changed since its registered sha256" in manifest.read_text()
        assert (
            next(r for r in runs.artifacts(con, run["id"]) if r["kind"] == "source")[
                "sha256"
            ]
            == old_sha
        )
    else:
        actual_sha = hashlib.sha256(placed.read_bytes()).hexdigest()
        assert f"sha256 {actual_sha}" in manifest.read_text()


@pytest.mark.parametrize("other_tokens", [0, 700])
def test_research_status_does_not_charge_another_task(state, monkeypatch, other_tokens):
    from misaka.core.platform import budget
    from misaka.core.research.wiring import research

    con, run, _root = state
    monkeypatch.setattr(research, "_cfg", lambda: {"token_cap": 0})
    budget.commit_agent_usage(con, None, run["id"], 1, 100)
    if other_tokens:
        budget.commit_agent_usage(con, None, "unrelated-card", 1, other_tokens)
    shown = research._status(con, run["id"], run["workspace"])
    assert "tokens added by this run 100" in shown, shown


@pytest.mark.asyncio
async def test_pane_spawn_does_not_block_owner_event_loop(state, monkeypatch):
    import time

    from misaka.core.research.node import PaneSpawner
    from misaka.ui.panel import client

    con, run, root = state
    ticks = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.002)

    def delayed_reply(*a, **k):
        time.sleep(0.08)
        runs.set_node(con, root["id"], status="closed")
        return {"pane_id": "fixture", "pid": os.getpid()}

    monkeypatch.setattr(client, "request", delayed_reply)
    spawner = PaneSpawner("fixture-parent")
    monkeypatch.setattr(spawner, "alive", lambda h: False)
    ticker_task = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.01)
        result = await workflow._expand_level(
            con, {}, spawner, run, [root], poll_seconds=0.005, progress=None
        )
        await asyncio.sleep(0.01)
        assert result == "done"
        worst = max(b - a for a, b in itertools.pairwise(ticks))
        assert worst < 0.04, (
            f"Owner event loop frozen for {worst:.3f}s during pane.create"
        )
    finally:
        ticker_task.cancel()
        await asyncio.gather(ticker_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sequence",
    [
        "retry_then_success",
        "success_then_followup_success",
        "success_then_followup_error",
    ],
)
async def test_queued_followup_error_does_not_invalidate_phase_answer(
    tmp_path, monkeypatch, sequence
):
    from contextlib import nullcontext

    from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
    from misaka.core.research import window

    monkeypatch.setattr(
        window.worker,
        "_reserve_usage",
        lambda *a: {"allowed": True, "tokens": 0, "token": None},
    )

    class Recorder:
        def __init__(self, *a):
            pass

        def __call__(self, *a):
            pass

        def settle(self, *a):
            pass

    monkeypatch.setattr(window.worker, "_UsageRecorder", Recorder)
    observers = []

    async def send(*a):
        events = {
            "phase": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "phase answer"}],
            },
            "followup": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "followup answer"}],
            },
            "error": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "unrelated followup failed",
                "content": [],
            },
        }
        order = {
            "retry_then_success": ["error", "phase"],
            "success_then_followup_success": ["phase", "followup"],
            "success_then_followup_error": ["phase", "error"],
        }[sequence]
        for name in order:
            for observer in observers:
                observer({"type": "message_end", "message": events[name]})

    session = SimpleNamespace(
        isIdle=True,
        sessionManager=SimpleNamespace(sessionFile=str(tmp_path / "session.jsonl")),
        agent=SimpleNamespace(streamFn=None),
        moments=SimpleNamespace(parts=[SisterCapabilitiesPart(str(tmp_path), [])]),
        subscribe=lambda fn: observers.append(fn) or (lambda: observers.remove(fn)),
        toolScope=lambda names: nullcontext(),
        registerCustomTools=lambda tools: None,
        unregisterCustomTools=lambda tools: None,
        getActiveToolNames=list,
        sendCustomMessage=send,
    )
    bridge = window.WindowLO(session, lambda: None, describe=dict)
    try:
        _obj, text, error = await bridge._execute_turn(
            "phase fixture", {"session_dir": str(tmp_path), "tools": []}
        )
        assert text == "phase answer"
        assert error is None, error
    finally:
        await bridge.close()
