"""Notifications that reach Last Order while a run is starting, end to end: the real board outbox (sqlite
``platform/notifications``), ``SisterRuntime.deliver_pending``/``notify_row`` -> ``moments.send_message`` -> an
``AgentSession`` with a file-backed ``SessionManager``.

pi 1.2 queues a message sent while a run starts into that run. MISAKA fork (``AgentSession._handBackQueuedReceipts``):
when that start then does not run (a ``before_agent_start`` hook blocks it, or preparing it fails), a message whose
sender redelivers it (a receipt with ``_onError``: the Sister outbox) is failed back to it and redelivered into a run
of its own; every other message (no receipt, or a sub-agent's or background shell's ``_onPersist``-only receipt)
stays queued for the next run, as upstream. Exactly once is checked on three sides: the session file
(``custom_message`` entries per card), the outbox (cursor past the event, no lease held) and the receipt table.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import TextContent
from misaka.core.network.sister_runtime import SisterRuntime
from misaka.core.platform import notifications
from misaka.core.platform import tasks as db
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field
from tests.test_tool_search import _assistant, _scripted, session_with


def _replies(n: int = 12):
    return _scripted([_assistant([TextContent(text=f"r{i}")]) for i in range(n)])


class Board:
    def __init__(self, tmp_path, session):
        self.con = db.connect(str(tmp_path / "board.db"))
        self.ws = {}
        for name in ("W1", "W2"):
            path = tmp_path / name
            path.mkdir()
            self.ws[name] = db.canonical_workspace(str(path))
        self.runtime = SisterRuntime(session, lambda: self.con, dict)

    def finish(self, ws: str, title: str) -> str:
        tid = db.create_task(self.con, title, assignee="misaka-10032", workspace=self.ws[ws])
        self.con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        return tid

    def poll(self, ws: str) -> int:
        return self.runtime.deliver_pending(self.ws[ws])

    def sub(self, ws: str) -> dict[str, Any]:
        sid = notifications.subscribe(self.con, "last-order", f"board-harness:{self.ws[ws]}", "task", "*", "terminal")
        row = self.con.execute(
            "SELECT cursor,lease_token,lease_expires,failure_count FROM notification_subscriptions WHERE id=?", (sid,)
        ).fetchone()
        return dict(row)

    def last_event(self, ws: str) -> int:
        return self.con.execute("SELECT MAX(id) FROM notification_events").fetchone()[0]

    def expire_lease(self, ws: str) -> None:
        sid = notifications.subscribe(self.con, "last-order", f"board-harness:{self.ws[ws]}", "task", "*", "terminal")
        self.con.execute("UPDATE notification_subscriptions SET lease_expires=? WHERE id=?", (int(time.time()) - 5, sid))


def recorded(manager: SessionManager) -> dict[str, int]:
    """custom_message entries in the session file, by card id (or customType for non-cards)."""
    out: dict[str, int] = {}
    for entry in manager.getEntries():
        if entry.get("type") != "custom_message":
            continue
        details = entry.get("details") or {}
        key = details.get("task_id") or entry.get("customType")
        out[key] = out.get(key, 0) + 1
    return out


def in_context(session) -> list[str]:
    out = []
    for m in session.messages:
        if read_field(m, "role") == "custom":
            d = read_field(m, "details") or {}
            out.append(d.get("task_id") or read_field(m, "customType"))
    return out


def queued(session) -> list[Any]:
    return [((read_field(m, "details") or {}).get("task_id") or read_field(m, "customType"))
            for m in [*session.agent._steeringQueue._messages, *session.agent._followUpQueue._messages]]


def gate_extension(box: dict[str, Any]):
    """before_agent_start: for a custom run of box['hold'] wait for box['release'], then block if box['block']."""

    def extension(pi: Any) -> None:
        async def hook(event: Any, _ctx: Any) -> Any:
            if event["messageType"] == "custom":
                tid = (read_field(event["message"], "details") or {}).get("task_id")
                if tid is not None and tid == box.get("hold"):
                    box["started"].set()
                    await box["release"].wait()
                    if box.get("block"):
                        return {"block": True, "reason": "not now"}
            elif event["messageType"] == "user" and box.get("hold_user"):
                box["started"].set()
                await box["release"].wait()
                if box.get("block_user"):
                    return {"block": True, "reason": "user turn blocked"}
            return None

        async def settled(_event: Any, _ctx: Any) -> None:
            action = box.pop("on_settled", None)
            if action is not None:
                action()

        pi.on("before_agent_start", hook)
        pi.on("agent_settled", settled)

    return extension


async def _settle(session):
    for _ in range(5):
        await asyncio.sleep(0.02)
        await session.waitForIdle()


async def _open(tmp_path):
    (tmp_path / "workspace").mkdir()
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))
    box: dict[str, Any] = {"started": asyncio.Event(), "release": asyncio.Event()}
    return manager, box


def _assert_exactly_once(board, manager, session, cards: list[str], ws_list: list[str]):
    rec = recorded(manager)
    for tid in cards:
        assert rec.get(tid) == 1, (tid, rec)
    for ws in ws_list:
        sub = board.sub(ws)
        assert sub["lease_token"] is None, (ws, sub)
    assert session._customMessageReceipts == {}
    assert queued(session) == []


# --- 1. queued into a start that is BLOCKED --------------------------------------------------------------------


async def test_blocked_start(tmp_path):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a, block=True)
        board.poll("W1")
        await box["started"].wait()
        b = board.finish("W2", "B")
        board.poll("W2")
        assert queued(s) == [b]
        box["release"].set()
        await _settle(s)
        assert recorded(manager) == {a: 1}
        b_event = board.last_event("W2")
        assert board.sub("W2")["cursor"] < b_event and board.sub("W2")["failure_count"] == 1  # NACKed, not acked
        # the next poll redelivers B into its own run
        board.poll("W2")
        await _settle(s)
        board.poll("W1"), board.poll("W2")
        await _settle(s)
        _assert_exactly_once(board, manager, s, [a, b], ["W1", "W2"])
        assert board.sub("W2")["cursor"] == board.last_event("W2")


# --- 2. queued into a start that FAILS --------------------------------------------------------------------------


async def test_failed_start(tmp_path):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        prepare = s._preparePromptAndToolLoadout
        fail = ["loadout failed"]

        def fail_once(*args, **kwargs):
            if fail:
                raise RuntimeError(fail.pop())
            return prepare(*args, **kwargs)

        s._preparePromptAndToolLoadout = fail_once
        a = board.finish("W1", "A")
        box.update(hold=a)
        board.poll("W1")
        await box["started"].wait()
        b = board.finish("W2", "B")
        board.poll("W2")
        assert queued(s) == [b]
        box["release"].set()
        await _settle(s)
        assert recorded(manager) == {}
        assert board.sub("W1")["failure_count"] == 1 and board.sub("W2")["failure_count"] == 1
        box["hold"] = None
        board.poll("W1")
        board.poll("W2")
        await _settle(s)
        board.poll("W1"), board.poll("W2")
        await _settle(s)
        _assert_exactly_once(board, manager, s, [a, b], ["W1", "W2"])


# --- 3. queued into a start that SUCCEEDS, plus a lease-expiry redelivery racing it ------------------------------


async def test_successful_start_with_lease_expiry_redelivery(tmp_path):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a)
        board.poll("W1")
        await box["started"].wait()
        b = board.finish("W2", "B")
        board.poll("W2")
        old_token = board.sub("W2")["lease_token"]
        board.expire_lease("W2")
        board.expire_lease("W1")
        board.poll("W2")  # outbox redelivers B (new lease token) while A is starting
        board.poll("W1")  # and A itself
        new_token = board.sub("W2")["lease_token"]
        assert new_token != old_token
        assert queued(s) == [b]
        box["release"].set()
        await _settle(s)
        board.poll("W1"), board.poll("W2")
        await _settle(s)
        _assert_exactly_once(board, manager, s, [a, b], ["W1", "W2"])
        assert board.sub("W1")["cursor"] > 0 and board.sub("W2")["cursor"] == board.last_event("W2")


# --- 4. deferred during agent_settled (one of them blocked, one failing) ------------------------------------------


@pytest.mark.parametrize("first", ["runs", "blocked", "fails"])
async def test_deferred_during_agent_settled(tmp_path, first):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        cards: dict[str, str] = {}

        def during_settled():
            cards["C"] = board.finish("W1", "C")
            cards["D"] = board.finish("W2", "D")
            board.poll("W1")
            board.poll("W2")
            box["deferred"] = len(s._deferredSettledActions)

        box["on_settled"] = during_settled
        if first == "blocked":
            box["release"].set()
            box["block"] = True
            box["hold_lazy"] = True
        prepare = s._prepareRun

        async def prepare_run(trigger, admit=None):
            tid = (read_field(trigger.get("message"), "details") or {}).get("task_id") if trigger["messageType"] == "custom" else None
            if tid is None:
                return await prepare(trigger, admit)
            if first == "blocked" and tid == cards.get("C"):
                box["hold"] = tid
            if first == "fails" and tid == cards.get("C") and not box.get("failed"):
                box["failed"] = True
                raise RuntimeError("prepare failed for C")
            return await prepare(trigger, admit)

        s._prepareRun = prepare_run
        raised = None
        try:
            await s.prompt("again")
        except RuntimeError as error:
            raised = str(error)
        await _settle(s)
        # The first failing deferred run is still reported (upstream raises it); the other route ran anyway.
        assert raised == ("prepare failed for C" if first == "fails" else None)
        assert box.get("deferred") == 2
        for _ in range(2):
            board.poll("W1"), board.poll("W2")
            await _settle(s)
        _assert_exactly_once(board, manager, s, [cards["C"], cards["D"]], ["W1", "W2"])


# --- 5. a handed-back notification redelivered into a new USER run (which runs, or is blocked) ------------------


@pytest.mark.parametrize("user_run", ["runs", "blocked"])
async def test_redelivery_racing_a_new_user_run(tmp_path, user_run):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a, block=True)
        board.poll("W1")
        await box["started"].wait()
        b = board.finish("W2", "B")
        board.poll("W2")
        box["release"].set()
        await _settle(s)
        assert board.sub("W2")["failure_count"] == 1
        # a user turn starts; while its hooks run the outbox redelivers B into it
        box.update(hold=None, block=False, started=asyncio.Event(), release=asyncio.Event(), hold_user=True,
                   block_user=(user_run == "blocked"))
        turn = asyncio.ensure_future(s.prompt("user turn"))
        await box["started"].wait()
        board.poll("W2")
        assert queued(s) == [b]
        box["release"].set()
        with contextlib.suppress(RuntimeError):  # a blocked user prompt raises, as before
            await turn
        await _settle(s)
        box["hold_user"] = False
        for _ in range(2):
            board.poll("W1"), board.poll("W2")
            await _settle(s)
        _assert_exactly_once(board, manager, s, [a, b], ["W1", "W2"])


# --- 6. messages WITHOUT a receipt stay queued, as upstream ------------------------------------------------------


async def test_receiptless_message_stays_queued_as_upstream(tmp_path):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a, block=True)
        board.poll("W1")
        await box["started"].wait()
        s.moments.send_message({"customType": "plain-note", "content": "no receipt", "display": True},
                               {"deliverAs": "followUp", "triggerTurn": True})
        assert queued(s) == ["plain-note"]
        box["release"].set()
        await _settle(s)
        assert queued(s) == ["plain-note"]  # upstream: stays in the Agent queue
        await s.prompt("next")
        await _settle(s)
        assert recorded(manager).get("plain-note") == 1


# --- 7. a receipt WITHOUT _onError (background shell / sub-agent notifications) stays queued ----------------------


async def test_a_background_shell_notice_queued_into_a_blocked_start_arrives_with_the_next_run(tmp_path):
    from misaka.core.subagent.shell import ShellTask

    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a, block=True)
        board.poll("W1")
        await box["started"].wait()
        shell = ShellTask(manager=SimpleNamespace(session=s, _closed=False), command="make test", cwd=str(tmp_path),
                          spawn_context=None, operations=None, status="completed", exit_code=0)
        shell.notify()  # the real product call: `_deliveryId` "shell:<id>" + `_onPersist`, no `_onError`
        assert queued(s) == [shell.id]
        box["release"].set()
        await _settle(s)
        await s.prompt("next user turn")
        await _settle(s)
        await s.prompt("and another")
        await _settle(s)
        assert recorded(manager).get(shell.id) == 1, "background-shell notification lost"
        assert shell.notified


# --- 8. the same for a sub-agent notification's options (runtime.py `_notify_task`), and a FAILED user start ------


@pytest.mark.parametrize("how", ["custom-blocked", "user-blocked", "user-start-fails"])
async def test_a_sub_agent_completion_queued_into_a_start_that_does_not_run_arrives_with_the_next_run(tmp_path, how):
    manager, box = await _open(tmp_path)
    acks: list[str] = []
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        if how == "custom-blocked":
            a = board.finish("W1", "A")
            box.update(hold=a, block=True)
            board.poll("W1")
            starting = None
        else:
            box.update(hold_user=True, block_user=(how == "user-blocked"))
            if how == "user-start-fails":
                prepare = s._preparePromptAndToolLoadout
                fail = ["loadout failed"]

                def fail_once(*args, **kwargs):
                    if fail:
                        raise RuntimeError(fail.pop())
                    return prepare(*args, **kwargs)

                s._preparePromptAndToolLoadout = fail_once
            starting = asyncio.ensure_future(s.prompt("user turn"))
        await box["started"].wait()
        # exactly the options SubagentManager._notify_task sends (misaka/core/subagent/runtime.py `_notify_task`)
        s.moments.send_message(
            {"customType": "task-notification", "content": "<task-notification>done</task-notification>",
             "display": True, "details": {"task_id": "agent-1"}},
            {"deliverAs": "followUp", "triggerTurn": True, "_deliveryId": "subagent:parent:agent-1:n1",
             "_onPersist": lambda: acks.append("persisted")})
        assert queued(s) == ["agent-1"]
        box["release"].set()
        if starting is not None:
            with contextlib.suppress(RuntimeError):  # the failing user start raises to its caller
                await starting
        await _settle(s)
        box["hold_user"] = False
        await s.prompt("next")
        await _settle(s)
        assert recorded(manager).get("agent-1") == 1, "sub-agent completion notification lost"


# --- 9. abort requested while A starts, with B queued into A's start ------------------------------------------------


async def test_abort_during_start_with_a_queued_notification(tmp_path):
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies()
        await s.prompt("hello")
        board = Board(tmp_path, s)
        a = board.finish("W1", "A")
        box.update(hold=a)
        board.poll("W1")
        await box["started"].wait()
        b = board.finish("W2", "B")
        board.poll("W2")
        aborted = asyncio.ensure_future(s.abort())
        await asyncio.sleep(0)
        box["release"].set()
        await aborted
        await _settle(s)
        # once its lease expires the outbox redelivers B
        board.expire_lease("W2")
        board.poll("W2")
        await _settle(s)
        board.expire_lease("W2")
        board.poll("W2")
        await _settle(s)
        assert recorded(manager).get(b) == 1, "B never delivered while LO idles"


# --- 10. a CancelledError in a deferred route, and a cancellation of the task running them -------------------------


def _shell(session, tmp_path):
    from misaka.core.subagent.shell import ShellTask

    return ShellTask(manager=SimpleNamespace(session=session, _closed=False), command="make test", cwd=str(tmp_path),
                     spawn_context=None, operations=None, status="completed", exit_code=0)


async def _redeliver_and_keep_working(board, session, rounds: int = 3):
    """The outbox redelivers once leases expire, and Last Order keeps taking user turns meanwhile."""
    for index in range(rounds):
        board.expire_lease("W1"), board.expire_lease("W2")
        board.poll("W1"), board.poll("W2")
        await _settle(session)
        await session.prompt(f"user {index}")
        await _settle(session)


async def test_a_cancelled_error_inside_a_deferred_route_does_not_hold_the_routes_after_it(tmp_path):
    # MISAKA fork (`_emitAgentSettled`): C's route raises CancelledError itself (no cancellation of this task); D (a
    # Sister card) and a background shell's completion, deferred after it, still run, and nothing stays held.
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies(20)
        await s.prompt("hello")
        board = Board(tmp_path, s)
        cards: dict[str, str] = {}

        def during_settled():
            cards["C"], cards["D"] = board.finish("W1", "C"), board.finish("W2", "D")
            board.poll("W1"), board.poll("W2")
            box["shell"] = _shell(s, tmp_path)
            box["shell"].notify()

        box["on_settled"] = during_settled
        prepare = s._prepareRun

        async def prepare_run(trigger, admit=None):
            details = (read_field(trigger.get("message"), "details") or {}) if trigger["messageType"] == "custom" else {}
            if "C" in cards and details.get("task_id") == cards["C"] and not box.get("cancelled"):
                box["cancelled"] = True
                raise asyncio.CancelledError
            return await prepare(trigger, admit)

        s._prepareRun = prepare_run
        with contextlib.suppress(asyncio.CancelledError):
            await s.prompt("again")
        await _settle(s)
        assert box.get("cancelled")
        await _redeliver_and_keep_working(board, s)

        rec = recorded(manager)
        assert (rec.get(cards["C"]), rec.get(cards["D"]), rec.get(box["shell"].id)) == (1, 1, 1)
        assert box["shell"].notified
        _assert_exactly_once(board, manager, s, [cards["C"], cards["D"]], ["W1", "W2"])


async def test_a_cancellation_during_deferred_routes_hands_the_rest_back(tmp_path):
    # The task running the deferred routes is cancelled while C's run starts (a research window cancelling its
    # phase, say): the routes not run yet are not run now, but none keeps its receipt held. The Sister card is NACKed
    # and redelivered; the shell notice waits in the follow-up queue and arrives with the next run, once.
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies(20)
        await s.prompt("hello")
        board = Board(tmp_path, s)
        cards: dict[str, str] = {}

        def during_settled():
            cards["C"], cards["D"] = board.finish("W1", "C"), board.finish("W2", "D")
            board.poll("W1"), board.poll("W2")
            box["shell"] = _shell(s, tmp_path)
            box["shell"].notify()
            box["hold"] = cards["C"]  # C's start waits in before_agent_start until the cancellation

        box["on_settled"] = during_settled
        turn = asyncio.ensure_future(s.prompt("again"))
        await box["started"].wait()
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        await _settle(s)
        box["hold"] = None

        # D handed back to the outbox; the shell notice kept queued with its receipt.
        assert list(s._customMessageReceipts) == [f"shell:{box['shell'].notification_id}"]
        assert queued(s) == [box["shell"].id]
        assert board.sub("W2")["failure_count"] == 1
        await _redeliver_and_keep_working(board, s)

        rec = recorded(manager)
        assert (rec.get(cards["C"]), rec.get(cards["D"]), rec.get(box["shell"].id)) == (1, 1, 1)
        assert box["shell"].notified
        _assert_exactly_once(board, manager, s, [cards["C"], cards["D"]], ["W1", "W2"])


# --- 11. a notice nobody redelivers that is itself the trigger of a start that fails --------------------------------


@pytest.mark.parametrize("kind", ["shell", "sub-agent", "sister"])
async def test_a_notice_whose_own_start_fails_is_delivered_by_the_next_run(tmp_path, kind):
    manager, box = await _open(tmp_path)
    acks: list[str] = []
    async with session_with(tmp_path, [gate_extension(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies(20)
        await s.prompt("hello")
        board = Board(tmp_path, s)
        prepare = s._preparePromptAndToolLoadout
        fail = ["loadout failed"]

        def fail_once(*args, **kwargs):
            if fail:
                raise RuntimeError(fail.pop())
            return prepare(*args, **kwargs)

        s._preparePromptAndToolLoadout = fail_once
        if kind == "shell":
            shell = _shell(s, tmp_path)
            shell.notify()
            key = shell.id
        elif kind == "sub-agent":
            key = "agent-1"
            with contextlib.suppress(RuntimeError):  # the failing start raises to its sender, as upstream
                await s.sendCustomMessage(
                    {"customType": "task-notification", "content": "<task-notification>done</task-notification>",
                     "display": True, "details": {"task_id": key}},
                    {"deliverAs": "followUp", "triggerTurn": True, "_deliveryId": "subagent:parent:agent-1:n1",
                     "_onPersist": lambda: acks.append("persisted")})
        else:
            key = board.finish("W1", "A")
            board.poll("W1")
        await _settle(s)
        await _redeliver_and_keep_working(board, s)

        assert recorded(manager).get(key) == 1
        assert s._customMessageReceipts == {} and queued(s) == []
        if kind == "shell":
            assert shell.notified
        if kind == "sub-agent":
            assert acks == ["persisted"]


# --- 12. a cancellation hands mail back to the inbox, which keeps delivering ----------------------------------------


def _mail_gate(box: dict[str, Any]):
    """before_agent_start holds the start of box['hold'] (a card id, or "mail" for the next mail batch)."""

    def extension(pi: Any) -> None:
        async def hook(event: Any, _ctx: Any) -> None:
            if event["messageType"] != "custom":
                return
            message = event["message"]
            card = (read_field(message, "details") or {}).get("task_id")
            mail = read_field(message, "customType") == "agent-messages"
            if box.get("hold") is not None and (card == box["hold"] or (mail and box["hold"] == "mail")):
                box["hold"] = None
                box["started"].set()
                await box["release"].wait()

        async def settled(_event: Any, _ctx: Any) -> None:
            action = box.pop("on_settled", None)
            if action is not None:
                await action()

        pi.on("before_agent_start", hook)
        pi.on("agent_settled", settled)

    return extension


async def _mail_received(session, inbox_con, wanted: int) -> dict[int, Any]:
    for index in range(4):
        await asyncio.sleep(0.2)
        await _settle(session)
        await session.prompt(f"user {index}")
        await _settle(session)
        rows = {row["id"]: row["delivered_at"] for row in inbox_con.execute("SELECT id, delivered_at FROM messages")}
        if len(rows) >= wanted and all(rows.values()):
            break
    return rows


@pytest.mark.parametrize("cancelled", ["a card's start before the mail", "the mail's own start"])
async def test_a_cancellation_never_ends_the_mail_inbox(tmp_path, monkeypatch, cancelled):
    # The real inbox pump (session_start -> _pump -> _deliver_once -> sendCustomMessage). A cancellation of the task
    # running the deferred routes hands the mail back as an ordinary failure, never as the CancelledError itself:
    # with it, the pump task ended and the session read no more mail.
    from misaka.core.network import messages

    monkeypatch.setattr(messages, "POLL_SECONDS", 0.02)
    manager, box = await _open(tmp_path)
    async with session_with(tmp_path, [_mail_gate(box)], sessionManager=manager) as s:
        s.agent.streamFunction = _replies(30)
        await s.prompt("hello")
        board = Board(tmp_path, s)
        inbox = messages.MessagesPart(sender="last-order", receive=True, contact=True)
        inbox.attach(s)
        await inbox.session_start(None, None)
        inbox_con = messages.connect()
        cards: dict[str, str] = {}

        async def during_settled():
            if cancelled == "a card's start before the mail":
                cards["C"] = board.finish("W1", "C")
                board.poll("W1")
            messages.send(inbox_con, "last-order", "MAIL-ONE", sender="10032")
            for _ in range(500):  # until the pump's delivery is deferred past this agent_settled
                if any(key.startswith("mail:") for key in s._customMessageReceipts):
                    break
                await asyncio.sleep(0.01)
            box["hold"] = cards.get("C", "mail")

        box["on_settled"] = during_settled
        turn = asyncio.ensure_future(s.prompt("again"))
        await box["started"].wait()
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        box["release"].set()
        await _settle(s)
        await asyncio.sleep(0.3)
        assert not inbox._job.done(), "the inbox pump ended"

        messages.send(inbox_con, "last-order", "MAIL-TWO", sender="10032")
        rows = await _mail_received(s, inbox_con, 2)
        try:
            assert len(rows) == 2 and all(rows.values()), rows
            assert not inbox._job.done()
            texts = [str(read_field(m, "content")) for m in s.messages if read_field(m, "customType") == "agent-messages"]
            assert sum("MAIL-ONE" in text for text in texts) == 1 and sum("MAIL-TWO" in text for text in texts) == 1
        finally:
            await inbox.session_shutdown(None, None)


# --- 13. a cancellation while a deferred run streams starts no new run from that run's cleanup -----------------------


async def test_a_cancellation_during_a_deferred_runs_stream_starts_no_new_run(tmp_path):
    from misaka.ai.types import DoneEvent
    from misaka.ai.utils.event_stream import AssistantMessageEventStream

    manager, box = await _open(tmp_path)
    calls: list[int] = []
    pending: list[asyncio.Future] = []

    def extension(pi: Any) -> None:
        async def settled(_event: Any, _ctx: Any) -> None:
            action = box.pop("on_settled", None)
            if action is not None:
                action()

        pi.on("agent_settled", settled)

    async with session_with(tmp_path, [extension], sessionManager=manager) as s:
        def stream(model, _context, _options=None):
            calls.append(len(calls))
            out = AssistantMessageEventStream()
            message = _assistant([TextContent(text=f"r{len(calls)}")]).model_copy(
                update={"api": model.api, "provider": model.provider, "model": model.id})

            async def finish():
                if box.pop("block_next", False):
                    box["streaming"].set()
                    await asyncio.Event().wait()  # only the cancellation ends this stream
                out.push(DoneEvent(reason="stop", message=message))
                out.end()

            pending.append(asyncio.ensure_future(finish()))
            return out

        s.agent.streamFunction = stream
        await s.prompt("hello")
        board = Board(tmp_path, s)
        cards: dict[str, str] = {}
        box["streaming"] = asyncio.Event()

        def first():
            # C's run is deferred past this settle and blocks while streaming; E arrives at C's own settle.
            cards["C"] = board.finish("W1", "C")
            board.poll("W1")
            box["block_next"] = True

            def second():
                cards["E"] = board.finish("W2", "E")
                board.poll("W2")

            box["on_settled"] = second

        box["on_settled"] = first
        turn = asyncio.ensure_future(s.prompt("again"))
        await asyncio.wait_for(box["streaming"].wait(), 5)
        before = len(calls)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(turn, 5)
        try:
            assert len(calls) == before, "a new LLM run started inside the cancellation"
            assert recorded(manager).get(cards["E"]) is None
            assert s._customMessageReceipts == {}  # E handed back to the outbox, not held
            assert board.sub("W2")["failure_count"] == 1
            # The outbox redelivers E afterwards, once.
            for _ in range(2):
                board.expire_lease("W2"), board.poll("W2")
                await _settle(s)
            assert recorded(manager).get(cards["E"]) == 1
        finally:
            for future in pending:
                future.cancel()
