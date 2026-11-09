"""Background-task notifications arrive the way CCB (77a7934) delivers them (R28, 2026-10-24/25).

* A background shell's completion or stall notice goes in at the next turn boundary (CCB's
  LocalShellTask queues it at priority ``next`` with MONITOR_TOOL on, and ``query.ts`` drains
  ``next`` after each tool round). As a follow-up it waited for the whole run to end: a card
  working for hours got its shells' results five to eight hours late.
* Queued notifications go in together, in one turn (CCB's ``processQueueIfReady`` drains every
  queued command of the head's mode at once). One at a time, each opened a turn of its own, and
  a card that had finished was woken over and over after it submitted.
* A sub-agent's completion still waits for the run to end, as CCB's ``later`` does.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from misaka.agent.agent import Agent
from misaka.ai.types import TextContent, ToolCall, UserMessage
from tests.test_notification_delivery_e2e import _open, _settle
from tests.test_tool_search import _assistant, _scripted, session_with


def notice(task_id, delivery):
    return ({"customType": "task-notification", "content": f"<task-notification>{task_id}</task-notification>",
             "display": True, "details": {"task_id": task_id}},
            {"deliverAs": "followUp", "triggerTurn": True, "_deliveryId": delivery})


def notices_in(context):
    """The notifications the model is sent: custom messages reach it as user text."""
    found = []
    for message in context.messages:
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else "".join(getattr(part, "text", "") for part in content)
        found += re.findall(r"<task-notification>\s*(?:<task-id>)?([\w-]+)", text)
    return found


def test_queued_notifications_leave_the_queue_together_and_other_messages_one_at_a_time():
    agent = Agent({"streamFn": lambda *args, **kwargs: None})
    agent.deliverTogether = lambda m: getattr(m, "role", None) == "custom"
    user = [UserMessage(content=text, timestamp=1) for text in ("first", "second")]
    custom = [SimpleNamespace(role="custom", name=name) for name in "abc"]
    for message in (user[0], custom[0], user[1], custom[1], custom[2]):
        agent.followUp(message)
    queue = agent._followUpQueue
    assert queue.drain() == [user[0]]
    assert queue.drain() == custom                              # every queued notice, in queue order
    assert queue.drain() == [user[1]]
    assert queue.drain() == []


async def test_notifications_queued_during_a_run_take_one_turn_and_each_is_recorded_once(tmp_path):
    seen, acks = [], []
    manager, _box = await _open(tmp_path)                       # receipts need a file that lands
    async with session_with(tmp_path, [], sessionManager=manager) as s:
        def working(context):
            for n in range(3):
                message, options = notice(f"agent-{n}", f"subagent:p:agent-{n}:x")
                s.moments.send_message(message, {**options, "_onPersist": lambda n=n: acks.append(n)})
            return _assistant([TextContent(text="still working")])

        def reading(context):
            seen.append(notices_in(context))
            return _assistant([TextContent(text="read them")])

        s.agent.streamFunction = _scripted([working, reading, reading, reading])
        await s.prompt("go")
        await _settle(s)
        assert seen == [["agent-0", "agent-1", "agent-2"]]       # one turn, not three
        assert sorted(acks) == [0, 1, 2]
        recorded = [e["details"]["task_id"] for e in s.sessionManager.getEntries()
                    if e.get("type") == "custom_message" and e.get("customType") == "task-notification"]
        assert recorded == ["agent-0", "agent-1", "agent-2"]


async def test_a_background_shell_notice_arrives_at_the_next_turn_boundary(tmp_path):
    from misaka.core.subagent.shell import ShellTask

    manager, _box = await _open(tmp_path)
    (tmp_path / "workspace" / "note.txt").write_text("a note\n", encoding="utf-8")
    seen = []
    async with session_with(tmp_path, [], sessionManager=manager) as s:
        shell = ShellTask(manager=SimpleNamespace(session=s, _closed=False), command="make test",
                          cwd=str(tmp_path), spawn_context=None, operations=None, status="completed", exit_code=0)

        def first_round(context):
            shell.notify()                                      # finishes while this round runs
            return _assistant([ToolCall(id="call-1", name="read", arguments={"path": "note.txt"})], "toolUse")

        def second_round(context):
            seen.append(notices_in(context))                    # the run has not ended: still mid-work
            return _assistant([TextContent(text="done")])

        s.agent.streamFunction = _scripted([first_round, second_round, second_round])
        await s.prompt("go")
        await _settle(s)
        assert seen[0] == [shell.id]
        assert shell.notified
