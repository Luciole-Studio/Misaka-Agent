"""Regression contracts from the 2026-09-16 sweep; temporary files and fixture IPC only."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.platform import cards, repo, tasks
from misaka.core.research import bundle, runs, workflow


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    monkeypatch.setattr(
        tasks, "task_state_dir", lambda tid: str(tmp_path / "task-state" / tid)
    )
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="fixture question")
    try:
        yield con, run, runs.nodes(con, run["id"])[0]
    finally:
        con.close()


@pytest.mark.parametrize("takeover", ["none", "driver", "node"])
async def test_phase_projection_keeps_original_owner(state, monkeypatch, takeover):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "old-driver")
    runs.prepare_runner(con, "research_execution", root["id"])
    runs.set_node(con, root["id"], status="planning")
    run, root = runs.get(con, run["id"]), runs.node(con, root["id"])
    plan = {
        "status": "clarify",
        "plan_markdown": "original plan",
        "clarifying_questions": ["which period?"],
        "tasks": [],
        "red_team": None,
    }
    runs.record_action(
        con,
        run,
        root,
        "plan",
        plan,
        session_file="/tmp/fixture-session",
        tool_call_id="plan",
    )

    async def yield_owner(*a):
        if takeover == "driver":
            con.execute(
                "UPDATE graphs SET lease_token='new-driver' WHERE id=?",
                (run["id"],),
            )
        if takeover == "node":
            con.execute(
                "UPDATE research_execution SET runner_key='new-node' WHERE id=?",
                (root["id"],),
            )

    monkeypatch.setattr(workflow, "_wait_unpaused", yield_owner)
    before = [dict(row) for row in runs.artifacts(con, run["id"])]
    error = None
    try:
        await workflow._expand(
            con,
            {},
            None,
            None,
            run,
            root,
            context=None,
            tool_call_id="fixture",
            poll_seconds=0.001,
            progress=None,
        )
    except (RuntimeError, ValueError) as exc:
        error = exc
    if takeover == "none":
        assert error is None
        assert runs.node(con, root["id"])["status"] == "waiting_input"
    else:
        assert runs.node(con, root["id"])["status"] == "planning", (
            "stale owner changed successor phase"
        )
        assert [dict(row) for row in runs.artifacts(con, run["id"])] == before, (
            "stale owner published artifacts"
        )
        assert error is not None


@pytest.mark.parametrize("locked", [False, True])
def test_artifact_publish_database_failure_preserves_last_checkpoint(state, locked):
    con, run, _root = state
    name = runs.run_path(run, "draft.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    peer = tasks.connect(str(Path(run["workspace"]) / "board.db"))
    peer.execute("PRAGMA busy_timeout=1")
    try:
        if locked:
            con.execute("BEGIN IMMEDIATE")
        error = None
        try:
            runs.write_text(peer, run["id"], "draft", "Draft", name, "after")
        except sqlite3.OperationalError as exc:
            error = exc
        if locked:
            assert error is not None
            con.execute("ROLLBACK")
        else:
            assert error is None
        row = runs.artifact(con, aid)
        expected = "before" if locked else "after"
        assert Path(path).read_text(encoding="utf-8") == expected, (
            "failed DB publication overwrote the previous checkpoint file"
        )
        assert runs.artifact_text(row) == expected
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        peer.close()


@pytest.mark.parametrize("mutated", [False, True])
def test_research_registration_does_not_relabel_post_submission_bytes(state, mutated):
    from misaka.core.network import dispatch

    con, run, root = state
    tid = cards.create(
        con, run["workspace"], "fixture output", "fixture body", "fixture"
    )
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="source")
    task = tasks.get(con, tid)
    path = Path(task["output_dir"]) / "evidence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("accepted bytes", encoding="utf-8")
    rel = str(path.relative_to(run["workspace"]))
    original_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    # Match the real submit_task ownership fence, without spawning a worker.
    con.execute(
        "UPDATE tasks SET status='running',claim_lock='fixture',claim_expires=? WHERE id=?",
        (10**12, tid),
    )
    assert dispatch.accept_state(
        con,
        tasks.get(con, tid),
        {"summary": "fixture", "artifacts": [rel]},
        generation=task["generation"],
        claim_lock="fixture",
    )
    if mutated:
        path.write_text("changed after acceptance", encoding="utf-8")
    if mutated:
        # 2026-09-24: changed bytes send the card back to its Sister to declare again instead
        # of failing the run; nothing is registered under the stale declaration.
        workflow.settle_done_tasks(con, run_id=run["id"])
        assert (
            tasks.latest_payload(
                con, tid, "research_v2_settled", generation=task["generation"]
            )
            is None
        )
        assert tasks.latest_payload(con, tid, "research_artifact_drift", generation=task["generation"])
        assert int(tasks.get(con, tid)["generation"]) == task["generation"] + 1
    else:
        workflow.settle_done_tasks(con, run_id=run["id"])
    artifacts = runs.artifacts(con, run["id"], task_id=tid)
    if mutated:
        assert not artifacts or artifacts[0]["sha256"] == original_sha, (
            "post-acceptance bytes relabeled as the accepted evidence"
        )
    else:
        assert len(artifacts) == 1
        assert artifacts[0]["sha256"] == original_sha


@pytest.mark.parametrize("added", [True, False])
async def test_responses_fallback_call_is_in_final_message(added):
    from misaka.ai.api.openai_responses_shared import processResponsesStream
    from misaka.ai.types import AssistantMessage, Model, Usage, UsageCost

    output = AssistantMessage(
        content=[],
        api="openai-responses",
        provider="fixture",
        model="fixture",
        stopReason="stop",
        timestamp=0,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
    )
    model = Model(
        id="fixture",
        name="fixture",
        api="openai-responses",
        provider="fixture",
        baseUrl="http://fixture.invalid",
        reasoning=False,
        input=["text"],
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        contextWindow=100,
        maxTokens=10,
    )
    item = {
        "type": "function_call",
        "id": "fc_fixture",
        "call_id": "call",
        "name": "fixture",
        "arguments": "{}",
    }

    async def events():
        if added:
            yield {
                "type": "response.output_item.added",
                "item": {**item, "arguments": ""},
            }
        yield {"type": "response.output_item.done", "item": item}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    seen = []
    await processResponsesStream(
        events(), output, SimpleNamespace(push=seen.append), model
    )
    assert any(e.type == "toolcall_end" for e in seen)
    assert len(output.content) == 1, (
        "toolcall_end was emitted but final message silently dropped the recovered call"
    )
    from misaka.agent.agent_loop import _executeToolCalls
    from misaka.agent.types import (
        AgentContext,
        AgentLoopConfig,
        AgentTool,
        AgentToolResult,
    )
    from misaka.ai.types import TextContent

    called = []

    async def execute(call_id, args, *_):
        called.append((call_id, args))
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    restored = AssistantMessage.model_validate_json(output.model_dump_json())
    end = next(event for event in seen if event.type == "toolcall_end")
    assert end.contentIndex == 0
    assert end.toolCall is output.content[0]
    assert restored.stopReason == "toolUse"
    tool = AgentTool(
        name="fixture",
        label="fixture",
        description="fixture",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    result = await _executeToolCalls(
        AgentContext(messages=[], tools=[tool]),
        restored,
        AgentLoopConfig(model=None, convertToLlm=lambda m: m),
        None,
        lambda e: None,
    )
    assert called == [("call|fc_fixture", {})]
    assert len(result.messages) == 1 and not result.messages[0].isError


@pytest.mark.parametrize("takeover", [False, True])
async def test_takeover_between_assignments_stops_old_card_creation(state, takeover):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "old-driver")
    run = runs.get(con, run["id"])
    specs = [
        {
            "local_id": name,
            "title": name,
            "instructions": "fixture",
            "assignee": "fixture",
            "dependencies": [],
        }
        for name in ["first", "second"]
    ]

    async def progress(event):
        if takeover and event["local_id"] == "first":
            con.execute(
                "UPDATE graphs SET lease_token='new-driver' WHERE id=?",
                (run["id"],),
            )

    try:
        await workflow._submit_tasks(
            con, run, root, specs, kind="research", progress=progress
        )
    except (ValueError, RuntimeError):
        pass
    assert len(runs.tasks(con, run["id"])) == (1 if takeover else 2), (
        "old driver created another Board card after takeover"
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_panel_root_shutdown_stops_owned_running_cards(
    state, monkeypatch, cancel
):
    from misaka.core.network import sister_runtime
    from misaka.core.research import window
    from misaka.ui.panel import client

    con, run, root = state
    tid = cards.create(con, run["workspace"], "fixture card", "fixture body", "fixture")
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="source")
    con.execute(
        "UPDATE tasks SET status='running',claim_lock='fixture',claim_expires=? WHERE id=?",
        (10**12, tid),
    )
    runs.set_node(con, root["id"], status="executing")
    reached, stopped = asyncio.Event(), []
    loop = asyncio.get_running_loop()

    def request(method, args=None):
        if method == "panes.status":
            loop.call_soon_threadsafe(reached.set)
            alive = tasks.get(con, tid)["status"] == "running"
            return {
                "panes": [
                    {
                        "card": tid,
                        "alive": alive,
                        "reported": {"state": "running" if alive else "idle"},
                    }
                ]
            }
        if method == "card.stop":
            stopped.append(args["task_id"])
            con.execute(
                "UPDATE tasks SET status='stopped',claim_lock=NULL WHERE id=?",
                (args["task_id"],),
            )
            return {}
        raise AssertionError(method)

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(sister_runtime, "_claimer_alive", lambda lock: True)
    monkeypatch.setenv("MISAKA_NET_PANE", "fixture-pane")
    monkeypatch.setattr(workflow, "_wait_unpaused", AsyncMock())
    monkeypatch.setattr(
        window,
        "WindowLO",
        lambda session, check, *, describe: SimpleNamespace(
            session_file=str(Path(run["workspace"]) / "session.jsonl"),
            close=AsyncMock(),
        ),
    )

    def partial(con, run, reason, **kwargs):
        runs.set_state(
            con, run["id"], status="stopped", driver_lock=kwargs.get("driver_lock")
        )
        return {"reason": "stopped"}

    monkeypatch.setattr(workflow, "_partial_result", partial)
    owner = asyncio.create_task(
        workflow.run(
            con,
            {},
            SimpleNamespace(),
            run_id=run["id"],
            session=object(),
            poll_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 1)
        if cancel:
            runs.request_stop(con, run["id"])
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
        else:
            runs.request_stop(con, run["id"])
            assert (await asyncio.wait_for(owner, 1))["reason"] == "stopped"
        assert tid in stopped, (
            "root task cancellation never sent card.stop to the still-running Sister pane"
        )
        assert tasks.get(con, tid)["status"] == "stopped"
    finally:
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize(
    "end", ["commit", "rollback", "sql_rollback", "close", "commit_error"]
)
def test_publication_follows_outer_transaction(state, monkeypatch, end):
    con, run, _ = state
    name = runs.run_path(run, "checkpoint.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    con.execute("BEGIN IMMEDIATE")
    runs.write_text(con, run["id"], "draft", "Draft", name, "first")
    runs.write_text(con, run["id"], "draft", "Draft", name, "second")
    assert Path(path).read_text(encoding="utf-8") == "second"
    if end == "commit_error":
        real = con.commit

        def fail_once():
            monkeypatch.setattr(con, "commit", real)
            raise sqlite3.OperationalError("fixture commit failure")

        monkeypatch.setattr(con, "commit", fail_once)
        with pytest.raises(sqlite3.OperationalError):
            con.commit()
        con.rollback()
    elif end == "sql_rollback":
        con.execute("ROLLBACK")
    else:
        getattr(con, end)()
    expected = "second" if end == "commit" else "before"
    assert Path(path).read_text(encoding="utf-8") == expected
    peer = tasks.connect(str(Path(run["workspace"]) / "board.db"))
    try:
        assert runs.artifact_text(runs.artifact(peer, aid)) == expected
    finally:
        peer.close()
    assert not list(Path(run["workspace"]).rglob("*.research-publish.json"))


@pytest.mark.parametrize("rollback_outer", [False, True])
def test_publication_caught_inner_failure_keeps_prior_write(
    state, monkeypatch, rollback_outer
):
    con, run, _ = state
    name = runs.run_path(run, "checkpoint.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    real = runs._atomic_write
    try:
        with tasks.write_txn(con):
            runs.write_text(con, run["id"], "draft", "Draft", name, "first")

            def broken(path, content):
                real(path, content)
                raise OSError("fixture failure after replace")

            monkeypatch.setattr(runs, "_atomic_write", broken)
            with pytest.raises(OSError, match="after replace"):
                runs.write_text(con, run["id"], "draft", "Draft", name, "second")
            assert Path(path).read_text(encoding="utf-8") == "first"
            if rollback_outer:
                raise ValueError("outer rollback")
    except ValueError:
        assert rollback_outer
    assert runs.artifact_text(runs.artifact(con, aid)) == (
        "before" if rollback_outer else "first"
    )
    assert not list(Path(run["workspace"]).rglob("*.research-publish.json"))


@pytest.mark.parametrize("committed", [False, True])
def test_publication_recovers_after_process_exit(state, committed):
    import subprocess
    import sys

    con, run, _ = state
    name = runs.run_path(run, "checkpoint.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    code = """
import os, sys
from misaka.core.platform import tasks
from misaka.core.research import runs
con = tasks.connect(sys.argv[1])
if sys.argv[4] == 'True':
    con._finish_transaction = lambda: None
else:
    original = runs._atomic_write
    def crash(path, content):
        original(path, content)
        os._exit(73)
    runs._atomic_write = crash
runs.write_text(con, sys.argv[2], 'draft', 'Draft', sys.argv[3], 'after')
os._exit(73)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(Path(run["workspace"]) / "board.db"),
            run["id"],
            name,
            str(committed),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 73, result.stderr.decode()
    assert Path(path).read_text(encoding="utf-8") == "after"
    expected = "after" if committed else "before"
    runs.recover_publications(con, run)
    assert runs.artifact_text(runs.artifact(con, aid)) == expected
    assert Path(path).read_text(encoding="utf-8") == expected
    assert not list(Path(run["workspace"]).rglob("*.research-publish.json"))


@pytest.mark.parametrize("ops_file", [False, True])
@pytest.mark.parametrize("protected", [False, True])
async def test_office_guard_checks_secondary_exports_and_freezes_ops(
    tmp_path, monkeypatch, ops_file, protected
):
    import json

    from misaka.config import home
    from misaka.core.platform.home_guard import HomeGuard
    from misaka.core.tools.office import prepare_office_input

    # A place this card may not write: MISAKA's home outside `shared`, reached through a link.
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    walled = tmp_path / "home" / "state"
    walled.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(walled, target_is_directory=True)
    project = tmp_path / "project"
    project.mkdir()
    guard = HomeGuard("card", str(project), guard_paths=True)
    destination = alias / "export.pdf" if protected else project / "ordinary.pdf"
    ops = [{"export_pdf": {"out": str(destination)}}]
    source = project / "ops.json"
    source.write_text(json.dumps(ops), encoding="utf-8")
    args = {"path": "document.docx", "ops": "@ops.json" if ops_file else ops}
    decision = await guard.tool_call({"toolName": "office", "input": args}, None)
    if protected:
        assert decision["block"]
    else:
        assert not decision.get("block")
        source.write_text(json.dumps([{"export_pdf": {"out": str(walled / "bad.pdf")}}]), encoding="utf-8")
        _, _, checked = prepare_office_input(decision["updatedInput"], str(project))
        assert checked == ops


async def test_driver_takeover_during_planner_thread_does_not_publish(
    state, monkeypatch
):
    con, run, root = state
    runs.acquire_driver(con, run["id"], "old")
    run = runs.get(con, run["id"])
    runs.set_node(con, root["id"], status="planning")
    root = runs.node(con, root["id"])
    before = [dict(row) for row in runs.artifacts(con, run["id"])]

    def plan(*a, **k):
        con.execute(
            "UPDATE graphs SET lease_token='new' WHERE id=?", (run["id"],)
        )
        return (
            {"status": "clarify", "plan_markdown": "stale", "clarifying_questions": []},
            "",
            "/fixture",
        )

    monkeypatch.setattr(workflow.planner, "plan", plan)
    with pytest.raises(ValueError, match="superseded"):
        await workflow._expand(
            con,
            {},
            None,
            None,
            run,
            root,
            context=None,
            tool_call_id="fixture",
            poll_seconds=0.001,
            progress=None,
        )
    assert [dict(row) for row in runs.artifacts(con, run["id"])] == before
    assert runs.node(con, root["id"])["status"] == "planning"


async def test_final_review_can_be_assigned_after_root_closed(state):
    con, run, root = state
    runs.set_node(con, root["id"], status="closed")
    root = runs.node(con, root["id"])
    result = await workflow._submit_tasks(
        con,
        run,
        root,
        [
            {
                "local_id": "@final-review",
                "title": "review",
                "assignee": "fixture",
                "instructions": "review",
            }
        ],
        kind="final_review",
    )
    assert "@final-review" in result


@pytest.mark.parametrize("takeover", [False, True])
async def test_repeated_cancellation_drains_only_owned_card_scope(
    state, monkeypatch, takeover
):
    con, run, root = state
    runs.acquire_driver(con, run["id"], "old")
    run = runs.get(con, run["id"])
    tid = cards.create(con, run["workspace"], "card", "body", "fixture")
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="test")
    con.execute(
        "UPDATE tasks SET status='running',claim_lock='claim' WHERE id=?", (tid,)
    )
    reached, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pending(scope):
        reached.set()
        await asyncio.Future()

    async def stop(task_id, **kwargs):
        assert kwargs["expected_generation"] == 1
        assert kwargs["expected_claim_lock"] == "claim"
        stopping.set()
        await release.wait()

    runner = SimpleNamespace(pending=pending, stop=stop)
    from misaka.core.network import dispatch

    monkeypatch.setattr(dispatch, "reconcile", lambda *a: None)
    task = asyncio.create_task(
        workflow._drive_tasks(
            con,
            {},
            runner,
            run["id"],
            scope={tid},
            check_active=lambda: runs.check_owner(con, run, root, allow_stop=True),
        )
    )
    await asyncio.wait_for(reached.wait(), 1)
    if takeover:
        con.execute(
            "UPDATE graphs SET lease_token='new' WHERE id=?", (run["id"],)
        )
    task.cancel()
    if not takeover:
        await asyncio.wait_for(stopping.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stopping.is_set() is not takeover


@pytest.mark.parametrize("generation,claim", [(1, "old"), (2, "old"), (1, "new")])
def test_panel_stop_rejects_successor_identity(state, generation, claim):
    from unittest.mock import Mock

    from misaka.ui.panel.daemon import Daemon

    con, run, _ = state
    tid = cards.create(con, run["workspace"], "card", "body", "fixture")
    con.execute(
        "UPDATE tasks SET generation=?,claim_lock=? WHERE id=?",
        (generation, claim, tid),
    )
    pane = SimpleNamespace(
        id="fixture-pane",
        card=tid,
        generation=generation,
        claim_lock=claim,
        proc=SimpleNamespace(pid=-1),
    )
    daemon = Daemon.__new__(Daemon)
    daemon.panes = {pane.id: pane}
    daemon._board = lambda: con
    daemon.close = Mock()
    result = daemon._api(
        "card.stop",
        {"task_id": tid, "expected_generation": 1, "expected_claim_lock": "old"},
    )
    assert daemon.close.called is (generation == 1 and claim == "old")
    assert result["stopped"] is daemon.close.called


async def test_cancel_after_assignment_before_drive_stops_new_card(state, monkeypatch):
    con, run, root = state
    runs.acquire_driver(con, run["id"], "driver")
    run = runs.get(con, run["id"])
    runs.set_node(con, root["id"], status="planning")
    root = runs.node(con, root["id"])
    plan = {
        "status": "ready",
        "plan_markdown": "plan",
        "clarifying_questions": [],
        "tasks": [
            {
                "local_id": "one",
                "title": "one",
                "instructions": "fixture",
                "assignee": "fixture",
                "dependencies": [],
            }
        ],
        "red_team": {"assignee": "fixture", "reason": "independent"},
    }
    runs.record_action(
        con, run, root, "plan", plan, session_file="/fixture", tool_call_id="plan"
    )
    stopped = []

    async def progress(event):
        if event["stage"] == "assigned":
            con.execute(
                "UPDATE tasks SET status='running',claim_lock='claim' WHERE id=?",
                (event["task_id"],),
            )
            raise asyncio.CancelledError

    async def stop(tid, **kwargs):
        stopped.append(tid)
        tasks.mark_stopped(con, tid, generation=kwargs["expected_generation"])

    from misaka.core.network import dispatch

    monkeypatch.setattr(dispatch, "reconcile", lambda *a: None)
    runner = SimpleNamespace(stop=stop)
    with pytest.raises(asyncio.CancelledError):
        await workflow._expand(
            con,
            {"research_plan_approval": False},
            runner,
            None,
            run,
            root,
            context=None,
            tool_call_id="fixture",
            poll_seconds=0.001,
            progress=progress,
        )
    linked = runs.tasks(con, run["id"])
    assert len(linked) == 1 and linked[0]["id"] in stopped
    assert linked[0]["status"] == "stopped"


@pytest.mark.parametrize("kind", ["card", "child", "dm", "foreground"])
async def test_office_executes_the_approved_ops_not_later_file_bytes(
    tmp_path, monkeypatch, kind
):
    import json

    from misaka.config import home
    from misaka.core.platform.home_guard import HomeGuard
    from misaka.core.tools.office import createOfficeToolDefinition

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    walled = tmp_path / "home" / "state"
    walled.mkdir(parents=True)
    protected = walled / "notes.md"
    protected.write_text("keep", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    # Every checked kind runs the ops it was shown, a person's session too (it may write the home).
    guard = HomeGuard(kind, str(project), guard_paths=True)
    ops = project / "ops.json"
    ops.write_text(json.dumps([{"create": {"content": "approved"}}]), encoding="utf-8")
    decision = await guard.tool_call(
        {"toolName": "office", "input": {"path": "output.md", "ops": "@ops.json"}}, None
    )
    ops.write_text(json.dumps([{"export_pdf": {"out": str(protected)}}]), encoding="utf-8")
    await createOfficeToolDefinition(str(project)).execute(
        "fixture", decision["updatedInput"], None, None, None
    )
    assert (project / "output.md").read_text(encoding="utf-8") == "approved"
    assert protected.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("change", ["deleted", "outside", "undigested", "forged"])
def test_attachment_manifest_boundary(state, change):
    import json

    from misaka.core.network import dispatch

    con, run, root = state
    tid = cards.create(con, run["workspace"], "card", "body", "fixture")
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="one")
    row = tasks.get(con, tid)
    path = Path(row["output_dir"]) / "evidence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("accepted", encoding="utf-8")
    rel = str(path.relative_to(run["workspace"]))
    con.execute(
        "UPDATE tasks SET status='running',claim_lock='claim',claim_expires=? WHERE id=?",
        (10**12, tid),
    )
    submission = {
        "summary": "result",
        "artifacts": [rel],
        "artifact_digests": {rel: "forged"},
    }
    assert dispatch.accept_state(
        con, tasks.get(con, tid), submission, generation=1, claim_lock="claim"
    )
    payload = json.loads(tasks.latest_payload(con, tid, "submitted", generation=1))
    assert payload["artifact_digests"][rel] == hashlib.sha256(b"accepted").hexdigest()
    if change == "undigested":                 # a submission another build wrote: refused, not trusted
        payload.pop("artifact_digests")
        con.execute(
            "UPDATE events SET payload=? WHERE task_id=? AND kind='submitted'",
            (json.dumps(payload), tid),
        )
    elif change == "deleted":
        path.unlink()
    elif change == "outside":
        path.unlink()
        # The target need not exist: the path escape must be rejected before any read.
        path.symlink_to(Path(run["workspace"]).parent / "outside-fixture.md")
    if change == "deleted":
        # 2026-09-24: a missing deliverable is drift, and drift reopens the card for its Sister.
        workflow.settle_done_tasks(con, run_id=run["id"])
        assert not runs.artifacts(con, run["id"], task_id=tid)
        assert int(tasks.get(con, tid)["generation"]) == 2
    elif change in {"outside", "undigested"}:
        with pytest.raises(ValueError, match="outside the workspace|no artifact digests"):
            workflow.settle_done_tasks(con, run_id=run["id"])
        assert not runs.artifacts(con, run["id"], task_id=tid)
    else:
        workflow.settle_done_tasks(con, run_id=run["id"])
        artifact = runs.artifacts(con, run["id"], task_id=tid)[0]
        assert "submission_digest_verified" not in json.loads(artifact["metadata_json"])


def test_read_only_artifact_lookup_never_recovers_files(state):
    from misaka.core.research import publication

    con, run, _ = state
    name = runs.run_path(run, "checkpoint.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    with tasks.write_txn(con):
        publication.prepare(con, Path(path), aid, hashlib.sha256(b"after").hexdigest())
        Path(path).write_text("after", encoding="utf-8")
    viewer = sqlite3.connect(
        f"file:{Path(run['workspace']) / 'board.db'}?mode=ro", uri=True
    )
    viewer.row_factory = sqlite3.Row
    viewer.execute("PRAGMA query_only=ON")
    try:
        assert (
            runs.artifact(viewer, aid)["sha256"]
            == hashlib.sha256(b"before").hexdigest()
        )
        assert runs.artifacts(viewer, run["id"])
        assert Path(path).read_text(encoding="utf-8") == "after"
    finally:
        viewer.close()
    runs.recover_publications(con, run)
    assert Path(path).read_text(encoding="utf-8") == "before"


def test_panel_stop_checks_driver_at_receipt_not_just_sender(state):
    from unittest.mock import Mock

    from misaka.ui.panel.daemon import Daemon

    con, run, root = state
    tid = cards.create(con, run["workspace"], "card", "body", "fixture")
    con.execute("UPDATE tasks SET claim_lock='claim' WHERE id=?", (tid,))
    pane = SimpleNamespace(id="fixture", card=tid, generation=1, claim_lock="claim")
    daemon = Daemon.__new__(Daemon)
    daemon.panes = {pane.id: pane}
    daemon._board = lambda: con
    daemon.close = Mock()
    con.execute(
        "UPDATE graphs SET lease_token='successor' WHERE id=?", (run["id"],)
    )
    result = daemon._api(
        "card.stop",
        {
            "task_id": tid,
            "expected_generation": 1,
            "expected_claim_lock": "claim",
            "research_owner": {
                "run_id": run["id"],
                "driver_lock": run["driver_lock"],
                "node_id": root["id"],
                "runner_key": root["runner_key"],
            },
        },
    )
    assert result["stale"] and not daemon.close.called


def test_driver_takeover_atomically_fences_delayed_node_spawn(state):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "old")
    key = runs.prepare_runner(con, "research_execution", root["id"])
    assert runs.acquire_driver(con, run["id"], "old")
    assert runs.node(con, root["id"])["runner_key"] == key
    con.execute("UPDATE graphs SET lease_expires=0 WHERE id=?", (run["id"],))
    assert runs.acquire_driver(con, run["id"], "new")
    assert runs.node(con, root["id"])["runner_key"] is None
    assert not runs.claim_runner(con, "research_execution", root["id"], key)


@pytest.mark.parametrize("rollback", [False, True])
def test_native_connection_context_finishes_publication(state, rollback):
    con, run, _ = state
    name = runs.run_path(run, "checkpoint.md")
    aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            runs.write_text(con, run["id"], "draft", "Draft", name, "after")
            if rollback:
                raise ValueError("fixture rollback")
    except ValueError:
        assert rollback
    assert Path(path).read_text(encoding="utf-8") == ("before" if rollback else "after")
    assert not list(Path(run["workspace"]).rglob("*.research-publish.json"))
    assert runs.artifact_text(runs.artifact(con, aid)) == (
        "before" if rollback else "after"
    )


@pytest.mark.parametrize("rollback", [False, True])
def test_caller_owned_sqlite_connection_uses_shared_transaction_callback(
    tmp_path, monkeypatch, rollback
):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    # The shared Board schema initializes the same supported caller-owned connection.
    con.executescript(tasks.SCHEMA)
    try:
        run = runs.create(con, workspace=str(tmp_path), question="fixture")
        name = runs.run_path(run, "checkpoint.md")
        aid, path = runs.write_text(con, run["id"], "draft", "Draft", name, "before")
        try:
            with tasks.write_txn(con):
                runs.write_text(con, run["id"], "draft", "Draft", name, "after")
                if rollback:
                    raise ValueError("fixture rollback")
        except ValueError:
            assert rollback
        assert Path(path).read_text(encoding="utf-8") == ("before" if rollback else "after")
        assert runs.artifact_text(runs.artifact(con, aid)) == (
            "before" if rollback else "after"
        )
        assert not list(tmp_path.rglob("*.research-publish.json"))
    finally:
        con.close()
