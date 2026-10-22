"""pi 1.1.0 -> 1.3.0 kernel follow-ups (partition C-core of the 1.3 port).

Upstream cases, translated: default-tools-setting (``resume``, #10785). MISAKA's own adaptations of the port: a
research window's tool scope does not become the resumed chat's loadout (K1), a run deferred past ``agent_settled``
keeps its delivery receipt, a blocked custom run records its message without a turn, and the parts that act on a
prompt only (the board collection, SubagentStart hooks) skip the runs a notification wakes (K2).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from misaka.ai.types import TextContent
from misaka.ai.utils.transcript import getCurrentTools
from misaka.core.moments import TURN, opens_turn
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import DEFAULT_TOOL_NAMES
from misaka.utils.values import read_field
from tests.test_pi_110_port import _inactive_and_active_tools
from tests.test_tool_search import _assistant, _scripted, session_with

# --- default-tools-setting: resume (#10785) ----------------------------------------------------------------------


def _recorded_session(tmp_path, tool_names: list[str]) -> SessionManager:
    """A session whose transcript records the loadout ``tool_names``, as a run records it."""
    (tmp_path / "workspace").mkdir(exist_ok=True)
    manager = SessionManager.inMemory(str(tmp_path / "workspace"))
    manager.appendMessage({
        "role": "system",
        "content": "",
        "toolsAdded": [
            {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}} for name in tool_names
        ],
        "timestamp": int(time.time() * 1000),
    })
    return manager


async def test_resume_restores_the_loadout_the_transcript_records(tmp_path):
    # active_tool was deactivated during the session, inactive_tool activated.
    manager = _recorded_session(tmp_path, ["read", "grep", "inactive_tool"])
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        assert sorted(session.getActiveToolNames()) == ["grep", "inactive_tool", "read"]


async def test_resume_does_not_restore_excluded_tools(tmp_path):
    manager = _recorded_session(tmp_path, ["read", "grep", "inactive_tool"])
    async with session_with(
        tmp_path, [_inactive_and_active_tools], sessionManager=manager, excludeTools=["inactive_tool"]
    ) as session:
        assert sorted(session.getActiveToolNames()) == ["grep", "read"]


async def test_resume_uses_explicit_tool_options_instead_of_the_recorded_loadout(tmp_path):
    recorded = ["read", "grep", "inactive_tool"]
    async with session_with(
        tmp_path, [_inactive_and_active_tools], sessionManager=_recorded_session(tmp_path, recorded), tools=["read", "write"]
    ) as allowlisted:
        assert allowlisted.getActiveToolNames() == ["read", "write"]

    async with session_with(
        tmp_path, [_inactive_and_active_tools], sessionManager=_recorded_session(tmp_path, recorded), noTools="builtin"
    ) as builtinless:
        assert builtinless.getActiveToolNames() == ["active_tool"]


async def test_resume_applies_name_tool_options_to_the_recorded_loadout(tmp_path):
    manager = _recorded_session(tmp_path, ["read", "grep", "inactive_tool"])
    async with session_with(
        tmp_path, [_inactive_and_active_tools], sessionManager=manager, tools=["-grep", "+write"]
    ) as session:
        assert sorted(session.getActiveToolNames()) == ["inactive_tool", "read", "write"]


async def test_resume_uses_the_default_tools_when_the_transcript_records_no_loadout(tmp_path):
    (tmp_path / "workspace").mkdir(exist_ok=True)
    manager = SessionManager.inMemory(str(tmp_path / "workspace"))
    manager.appendMessage({"role": "user", "content": "hi", "timestamp": int(time.time() * 1000)})
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        # MISAKA fork: the default selection is misaka's eight tools (`DEFAULT_TOOL_NAMES`), where pi has four.
        assert sorted(session.getActiveToolNames()) == sorted([*DEFAULT_TOOL_NAMES, "active_tool"])


# --- K1: a research window's tool scope is not the resumed chat's loadout ----------------------------------------


def _phase(content: str = "phase") -> dict[str, Any]:
    """The custom message a research window starts a phase with (research/window.py)."""
    return {"customType": "research-phase", "display": True, "content": content, "details": {TURN: True}}


def _declared(manager: SessionManager) -> list[str]:
    return sorted(read_field(tool, "name") for tool in getCurrentTools(manager.buildSessionContext().messages))


async def test_a_research_window_scope_does_not_become_the_resumed_chats_loadout(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as chat:
        full = sorted(chat.getActiveToolNames())
        chat.agent.streamFunction = _scripted([_assistant([TextContent(text="chat")]), _assistant([TextContent(text="phase")])])
        await chat.prompt("hello")
        # A research phase runs in Last Order's own conversation, inside the window's tool scope.
        with chat.toolScope(["read"]):
            await chat.sendCustomMessage(_phase(), {"triggerTurn": True})
            assert chat.getActiveToolNames() == ["read"]
        assert sorted(chat.getActiveToolNames()) == full
        assert _declared(manager) == full
        # `/tree` restores from the transcript as resume does.
        chat._restoreToolsFromTranscript()
        assert sorted(chat.getActiveToolNames()) == full
        session_file = manager.getSessionFile()

    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=SessionManager.open(session_file)) as resumed:
        assert sorted(resumed.getActiveToolNames()) == full


async def test_a_scope_that_ends_while_a_run_works_declares_the_loadout_once_the_run_settles(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as chat:
        full = sorted(chat.getActiveToolNames())
        chat.agent.streamFunction = _scripted([_assistant([TextContent(text="chat")])])
        await chat.prompt("hello")
        requested, release = asyncio.Event(), asyncio.Event()
        scripted = _scripted([_assistant([TextContent(text="phase")])])

        async def held(*args: Any, **kwargs: Any) -> Any:
            requested.set()
            await release.wait()
            return scripted(*args, **kwargs)

        chat.agent.streamFunction = held
        with chat.toolScope(["read"]):
            run = asyncio.ensure_future(chat.sendCustomMessage(_phase(), {"triggerTurn": True}))
            await requested.wait()
        # The run's only request declared the scope's tools; nothing declares the user's loadout before it settles.
        release.set()
        await run
        assert _declared(manager) == full


async def test_a_scope_without_a_recorded_loadout_writes_nothing(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        with session.toolScope(["read"]):
            pass
        assert [read_field(m, "role") for m in session.agent.state.messages] == []


# --- a run deferred past agent_settled keeps its delivery receipt ------------------------------------------------


async def test_a_triggered_notification_sent_from_agent_settled_still_starts_its_run(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    sent: dict[str, Any] = {}
    persisted: list[str] = []

    def extension(pi: Any) -> None:
        async def settled(_event: Any, _ctx: Any) -> None:
            if sent.get("done"):
                return
            sent["done"] = True
            # Delivered from inside the agent_settled dispatch, as a Sister's result can be: the run is deferred
            # until every settled handler finished, and must not meet its own receipt then.
            await sent["session"].sendCustomMessage(
                {"customType": "sister-notification", "content": "card done", "display": True},
                {"deliverAs": "followUp", "triggerTurn": True, "_deliveryId": "card:1",
                 "_onPersist": lambda: persisted.append("card:1"), "_onError": lambda error: persisted.append(repr(error))},
            )

        pi.on("agent_settled", settled)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        sent["session"] = session
        session.agent.streamFunction = _scripted([_assistant([TextContent(text="one")]), _assistant([TextContent(text="two")])])
        await session.prompt("hello")
        await session.waitForIdle()
        assert [read_field(m, "role") for m in session.messages if read_field(m, "role") != "system"] == [
            "user", "assistant", "custom", "assistant",
        ]
        assert persisted == ["card:1"]


# --- before_agent_start block on a custom-message run ------------------------------------------------------------


async def test_a_blocked_custom_run_records_and_acknowledges_its_message_without_a_turn(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    receipts: list[str] = []

    def extension(pi: Any) -> None:
        def block(event: Any, _ctx: Any) -> Any:
            if event["messageType"] == "custom":
                return {"block": True, "reason": "not now"}
            return None

        pi.on("before_agent_start", block)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text="one")])])
        await session.prompt("hello")
        await session.sendCustomMessage(
            {"customType": "task-notification", "content": "task done", "display": True},
            {"triggerTurn": True, "_deliveryId": "task:1",
             "_onPersist": lambda: receipts.append("ack"), "_onError": lambda _error: receipts.append("nack")},
        )
        assert receipts == ["ack"]
        assert session.isIdle
        assert [read_field(m, "role") for m in session.messages if read_field(m, "role") != "system"] == [
            "user", "assistant", "custom",
        ]
        # A blocked prompt still fails, as before.
        blocked_prompt = SimpleNamespace(raised=None)
        try:
            await session.sendUserMessage("x")
        except RuntimeError as error:  # pragma: no cover - the extension blocks only custom runs
            blocked_prompt.raised = error
        assert blocked_prompt.raised is None


# --- a notification queued into a run that then does not start ---------------------------------------------------


def _notification(text: str) -> dict[str, Any]:
    return {"customType": "sister-notification", "content": text, "display": True}


def _delivery(delivery_id: str, receipts: list[str]) -> dict[str, Any]:
    """A durable outbox's options: ACK on persistence, NACK (redeliver later) on failure."""
    return {"deliverAs": "followUp", "triggerTurn": True, "_deliveryId": delivery_id,
            "_onPersist": lambda: receipts.append(f"ack:{delivery_id}"),
            "_onError": lambda _error: receipts.append(f"nack:{delivery_id}")}


def _custom_contents(session: Any) -> list[Any]:
    return [read_field(m, "content") for m in session.messages if read_field(m, "role") == "custom"]


async def _queue_into_a_starting_run(session: Any, receipts: list[str], started: asyncio.Event, release: asyncio.Event):
    """A's run is starting (its before_agent_start handler waits); B arrives meanwhile and joins A's run."""
    first = asyncio.ensure_future(session.sendCustomMessage(_notification("A"), _delivery("A", receipts)))
    await started.wait()
    await session.sendCustomMessage(_notification("B"), _delivery("B", receipts))
    assert session.agent.hasQueuedMessages()
    release.set()
    return first


async def test_a_notification_queued_into_a_blocked_run_is_handed_back_for_redelivery(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    started, release = asyncio.Event(), asyncio.Event()
    receipts: list[str] = []

    def extension(pi: Any) -> None:
        async def block_a(event: Any, _ctx: Any) -> Any:
            if event["messageType"] == "custom" and read_field(event["message"], "content") == "A":
                started.set()
                await release.wait()
                return {"block": True, "reason": "not now"}
            return None

        pi.on("before_agent_start", block_a)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=t)]) for t in ("one", "two")])
        await session.prompt("hello")
        await (await _queue_into_a_starting_run(session, receipts, started, release))

        # A is recorded and acknowledged without a turn; B is not left in the queue of a run that never came.
        assert session.isIdle and not session.agent.hasQueuedMessages()
        assert receipts == ["nack:B", "ack:A"]
        # The outbox redelivers B: it starts its own run, once.
        await session.sendCustomMessage(_notification("B"), _delivery("B", receipts))
        await session.waitForIdle()
        assert _custom_contents(session) == ["A", "B"]
        assert receipts == ["nack:B", "ack:A", "ack:B"]


@pytest.mark.parametrize(
    "receipt",
    [{"_deliveryId": "shell:1:stall"}, {"_deliveryId": "subagent:agent-1", "_onPersist": lambda: None}],
    ids=["delivery-id-only", "on-persist-only"],
)
async def test_a_notice_nobody_redelivers_stays_queued_for_the_next_run(tmp_path, receipt):
    # A background shell's stall notice (`_deliveryId` only) or a sub-agent's completion (`_onPersist` only) has no
    # sender that redelivers it: failing it back would lose it. It stays queued, as upstream, and the next run has it.
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    started, release = asyncio.Event(), asyncio.Event()
    receipts: list[str] = []

    def extension(pi: Any) -> None:
        async def block_a(event: Any, _ctx: Any) -> Any:
            if event["messageType"] == "custom" and read_field(event["message"], "content") == "A":
                started.set()
                await release.wait()
                return {"block": True, "reason": "not now"}
            return None

        pi.on("before_agent_start", block_a)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=t)]) for t in ("one", "two", "three")])
        await session.prompt("hello")
        first = asyncio.ensure_future(session.sendCustomMessage(_notification("A"), _delivery("A", receipts)))
        await started.wait()
        await session.sendCustomMessage(_notification("notice"), {"deliverAs": "followUp", "triggerTurn": True, **receipt})
        release.set()
        await first

        assert session.agent.hasQueuedMessages()
        assert receipt["_deliveryId"] in session._customMessageReceipts
        await session.prompt("next")
        await session.waitForIdle()
        assert _custom_contents(session) == ["A", "notice"]
        assert session._customMessageReceipts == {}


async def test_a_notification_queued_into_a_run_that_fails_to_start_is_handed_back_for_redelivery(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    started, release = asyncio.Event(), asyncio.Event()
    receipts: list[str] = []

    def extension(pi: Any) -> None:
        async def hold_a(event: Any, _ctx: Any) -> None:
            if event["messageType"] == "custom" and read_field(event["message"], "content") == "A":
                started.set()
                await release.wait()

        pi.on("before_agent_start", hold_a)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=t)]) for t in ("one", "two")])
        await session.prompt("hello")
        prepare = session._preparePromptAndToolLoadout
        failures = ["loadout failed"]

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            if failures:
                raise RuntimeError(failures.pop())
            return prepare(*args, **kwargs)

        session._preparePromptAndToolLoadout = fail_once
        first = await _queue_into_a_starting_run(session, receipts, started, release)
        try:
            await first
        except RuntimeError as error:
            assert str(error) == "loadout failed"

        assert session.isIdle and not session.agent.hasQueuedMessages()
        assert sorted(receipts) == ["nack:A", "nack:B"]
        assert session._customMessageReceipts == {}
        await session.sendCustomMessage(_notification("B"), _delivery("B", receipts))
        await session.waitForIdle()
        assert _custom_contents(session) == ["B"]
        assert receipts.count("ack:B") == 1


async def test_a_failing_deferred_run_does_not_drop_the_runs_deferred_after_it(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    receipts: list[str] = []
    box: dict[str, Any] = {}

    def extension(pi: Any) -> None:
        async def settled(_event: Any, _ctx: Any) -> None:
            if box.get("done"):
                return
            box["done"] = True
            for delivery_id in ("C", "D"):  # both deferred past this agent_settled dispatch
                await box["session"].sendCustomMessage(_notification(delivery_id), _delivery(delivery_id, receipts))

        pi.on("agent_settled", settled)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        box["session"] = session
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=t)]) for t in ("one", "two")])
        prepare = session._prepareRun

        async def fail_for_c(trigger: Any, admit: Any = None) -> Any:
            if trigger["messageType"] == "custom" and read_field(trigger["message"], "content") == "C":
                raise RuntimeError("prepare failed for C")
            return await prepare(trigger, admit)

        session._prepareRun = fail_for_c
        raised = None
        try:
            await session.prompt("hello")
        except RuntimeError as error:
            raised = str(error)
        await session.waitForIdle()

        assert raised == "prepare failed for C"  # still reported, as upstream does
        assert _custom_contents(session) == ["D"]
        assert receipts == ["nack:C", "ack:D"]
        assert session._customMessageReceipts == {}


async def test_every_failing_deferred_run_is_logged_and_the_first_is_raised(tmp_path, caplog):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    receipts: list[str] = []
    box: dict[str, Any] = {}

    def extension(pi: Any) -> None:
        async def settled(_event: Any, _ctx: Any) -> None:
            if box.get("done"):
                return
            box["done"] = True
            for delivery_id in ("C", "D", "E"):
                await box["session"].sendCustomMessage(_notification(delivery_id), _delivery(delivery_id, receipts))

        pi.on("agent_settled", settled)

    async with session_with(tmp_path, [extension], sessionManager=manager) as session:
        box["session"] = session
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=t)]) for t in ("one", "two")])
        prepare = session._prepareRun

        async def fail_for_c_and_d(trigger: Any, admit: Any = None) -> Any:
            content = read_field(trigger.get("message"), "content") if trigger["messageType"] == "custom" else None
            if content in ("C", "D"):
                raise RuntimeError(f"prepare failed for {content}")
            return await prepare(trigger, admit)

        session._prepareRun = fail_for_c_and_d
        with caplog.at_level("WARNING", logger="misaka.core.agent_session"):
            raised = None
            try:
                await session.prompt("hello")
            except RuntimeError as error:
                raised = str(error)
            await session.waitForIdle()

        assert raised == "prepare failed for C"
        logged = [record for record in caplog.records if "deferred past agent_settled" in record.getMessage()]
        assert [str(record.exc_info[1]) for record in logged] == ["prepare failed for C", "prepare failed for D"]
        assert _custom_contents(session) == ["E"]
        assert receipts == ["nack:C", "nack:D", "ack:E"]


# --- K2: per-prompt work skips notification-woken runs -----------------------------------------------------------


def test_opens_turn_is_true_for_prompts_and_turn_marked_custom_messages():
    assert opens_turn({"messageType": "user", "prompt": "hi"})
    assert opens_turn({})  # an event without messageType is a prompt
    assert opens_turn({"messageType": "custom", "message": {"details": {TURN: True}}})
    assert not opens_turn({"messageType": "custom", "message": {"details": {"run_id": "r"}}})
    assert not opens_turn({"messageType": "custom", "message": {"details": None}})


async def test_the_board_collection_runs_for_a_users_turn_only():
    from misaka.core.network.wiring.network import NetworkPart

    part = NetworkPart.__new__(NetworkPart)
    part.receive_notifications = True
    part._collect_pending = AsyncMock()
    await part.before_agent_start({"messageType": "custom", "message": {"details": {TURN: True}}}, None)
    await asyncio.sleep(0)
    part._collect_pending.assert_not_called()
    await part.before_agent_start({"messageType": "user", "prompt": "hi"}, None)
    await asyncio.sleep(0)
    part._collect_pending.assert_called_once()


async def test_subagent_start_hooks_run_for_turn_opening_runs_with_the_messages_text():
    from misaka.core.subagent.extension import SubagentPart
    from misaka.core.subagent.policy import AgentPolicy

    payloads: list[dict[str, Any]] = []
    policy = AgentPolicy.__new__(AgentPolicy)
    policy._payload = lambda _name, **fields: payloads.append(fields) or fields
    policy.context = SimpleNamespace(role="sisters")
    policy._execute_hooks = AsyncMock(return_value=[])
    part = SubagentPart.__new__(SubagentPart)
    part.manager = None
    part.session = None
    part.policy = policy

    await part.before_agent_start({"messageType": "custom", "message": {"content": "task done", "details": {}}}, None)
    assert payloads == []
    await part.before_agent_start(
        {"messageType": "custom", "message": {"content": [{"type": "text", "text": "phase one"}], "details": {TURN: True}}},
        None,
    )
    await part.before_agent_start({"messageType": "user", "prompt": "hello"}, None)
    assert payloads == [{"prompt": "phase one"}, {"prompt": "hello"}]


# --- reachesTools (pi 1.2) ----------------------------------------------------------------------------------------


async def test_get_all_tools_reports_reaches_tools(tmp_path):
    from tests.test_pi_110_port import _tool

    def reach(_input: Any) -> str:
        return "all"

    def tools(pi: Any) -> None:
        pi.registerTool(_tool("orchestrator", reachesTools=reach, namespace={"name": "mcp__docs", "aliases": ["docs"]}))
        pi.registerTool(_tool("plain"))

    async with session_with(tmp_path, [tools]) as session:
        infos = {info.name: info for info in session.getAllTools()}
        assert infos["orchestrator"].reachesTools is reach
        assert infos["orchestrator"].namespace == {"name": "mcp__docs", "aliases": ["docs"]}
        assert infos["plain"].reachesTools is None
