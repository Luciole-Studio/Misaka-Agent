"""A session reopened after its process ended is told about the background agents whose ends it
never heard (R34). The notice waited in the old process's queue: pi's loop ends a run on an error
reply without taking follow-ups, card M2 kept failing on 503s while its agents failed too, and
when the card was stopped (2026-10-25 06:52) twelve of its fourteen agents had never been
reported -- eight failures, four completions. Their metadata said so on disk, but a new process
read it only when the session looked an agent up by name.

On session start the manager reports each background agent that ended unreported -- and each
that was still running when the process went, since its child fences itself within a second of
its parent dying -- with the notice and delivery id it would have sent. The notice joins the next
run's opening messages instead of starting a run, so a resumed card reads it with its contract.
One that did land, its receipt lost, is acknowledged and not repeated."""
from __future__ import annotations

import json
import os
from pathlib import Path

from misaka.ai.types import TextContent
from tests.test_notification_delivery_e2e import _open, _settle
from tests.test_task_notification_delivery import notices_in
from tests.test_tool_search import _assistant, _scripted, session_with


def manager_for(session, tmp_path):
    from misaka.core.subagent.runtime import RoleContext, SubagentManager
    return SubagentManager(session, RoleContext(role="sisters/10032", mcp_role="sisters/10032",
                                                workspace=str(tmp_path / "workspace"),
                                                profile_dir=str(tmp_path / "profiles" / "10032")))


def write_meta(session, agent_id, *, status, notified=False, background=True, notification="n1"):
    from misaka.config.sessions import subagent_session_dir
    from misaka.core.subagent.runtime import _safe_component

    parent = _safe_component(str(session.sessionManager.getSessionId()))
    directory = Path(subagent_session_dir(parent, session.sessionManager.getSessionFile()))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent_id}.meta.json"
    path.write_text(json.dumps({
        "schemaVersion": 1, "agentId": agent_id, "parentSessionId": parent,
        "definition": {"name": "general", "description": "d", "prompt": "p"},
        "description": f"reader {agent_id}", "prompt": "read", "cwd": session.sessionManager.getCwd(),
        "transcript": str(directory / f"agent-{agent_id}.jsonl"), "background": background,
        "status": status, "error": "Error code: 503" if status == "failed" else None,
        "result": None, "notified": notified, "notificationId": notification,
        "startTimeMs": 1, "endTimeMs": 2, "turnCount": 1}), encoding="utf-8")
    return path


def meta_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def test_unreported_ends_join_the_next_run_together_and_are_marked_told(tmp_path):
    seen = []
    manager_file, _box = await _open(tmp_path)
    async with session_with(tmp_path, [], sessionManager=manager_file) as s:
        s.sessionManager.appendMessage({"role": "user", "content": "earlier", "timestamp": 1})
        paths = {
            "failed": write_meta(s, "a" + "1" * 16, status="failed"),
            "completed": write_meta(s, "a" + "2" * 16, status="completed"),
            "orphaned": write_meta(s, "a" + "3" * 16, status="running"),
            "told": write_meta(s, "a" + "4" * 16, status="completed", notified=True),
            "foreground": write_meta(s, "a" + "5" * 16, status="failed", background=False),
        }
        manager = manager_for(s, tmp_path)
        assert await manager.recover_notices(s) == 3
        assert not s.isStreaming                                 # nothing started a run of its own

        def reading(context):
            seen.append(sorted(notices_in(context)))
            return _assistant([TextContent(text="noted")])

        s.agent.streamFunction = _scripted([reading, reading])
        await s.prompt("the contract")
        await _settle(s)
        assert seen[0] == sorted("a" + c * 16 for c in "123")    # all three, with the opening message
        for name in ("failed", "completed", "orphaned"):
            assert meta_of(paths[name])["notified"] is True, name
        assert meta_of(paths["orphaned"])["status"] == "failed"  # its child went with its parent
        assert meta_of(paths["foreground"])["notified"] is False  # its result was the tool call's
        assert await manager_for(s, tmp_path).recover_notices(s) == 0


async def test_a_notice_that_landed_but_lost_its_receipt_is_acknowledged_not_repeated(tmp_path):
    manager_file, _box = await _open(tmp_path)
    async with session_with(tmp_path, [], sessionManager=manager_file) as s:
        agent_id = "a" + "6" * 16
        path = write_meta(s, agent_id, status="completed", notification="n9")
        parent = meta_of(path)["parentSessionId"]
        s.sessionManager.appendCustomMessageEntry(          # the old process wrote it, then went
            "task-notification", f"<task-notification><task-id>{agent_id}</task-id></task-notification>", True,
            {"task_id": agent_id, "delivery_id": f"subagent:{parent}:{agent_id}:n9"})
        await manager_for(s, tmp_path).recover_notices(s)
        await _settle(s)
        assert meta_of(path)["notified"] is True
        entries = [e for e in s.sessionManager.getEntries()
                   if e.get("type") == "custom_message" and e.get("customType") == "task-notification"]
        assert len(entries) == 1


async def test_a_session_with_no_agents_creates_nothing(tmp_path):
    manager_file, _box = await _open(tmp_path)
    async with session_with(tmp_path, [], sessionManager=manager_file) as s:
        before = sorted(os.listdir(tmp_path / "sessions"))
        assert await manager_for(s, tmp_path).recover_notices(s) == 0
        assert sorted(os.listdir(tmp_path / "sessions")) == before


async def test_reopening_the_session_reports_them_without_being_asked(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from misaka.core.subagent.extension import SubagentPart
    from misaka.core.subagent.runtime import RoleContext

    seen = []
    manager_file, _box = await _open(tmp_path)
    async with session_with(tmp_path, [], sessionManager=manager_file) as s:
        s.sessionManager.appendMessage({"role": "user", "content": "earlier", "timestamp": 1})
        path = write_meta(s, "a" + "7" * 16, status="failed")
        part = SubagentPart(RoleContext(role="sisters/10032", mcp_role="sisters/10032",
                                        workspace=str(tmp_path / "workspace"),
                                        profile_dir=str(tmp_path / "profiles" / "10032")), True)
        part.attach(s)

        async def no_roster(ctx):
            return None

        monkeypatch.setattr(part, "_refresh_roster", no_roster)
        await part.session_start({}, SimpleNamespace(sessionManager=s.sessionManager))

        def reading(context):
            seen.append(notices_in(context))
            return _assistant([TextContent(text="noted")])

        s.agent.streamFunction = _scripted([reading])
        await s.prompt("the contract")
        await _settle(s)
        assert seen == [["a" + "7" * 16]]
        assert meta_of(path)["notified"] is True
