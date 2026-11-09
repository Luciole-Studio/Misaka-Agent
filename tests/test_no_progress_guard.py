"""NoProgressGuard as FrontierAgent wrote it: it breaks a coordinator's create/assign spin.

2026-10-24: 10032, reviewing a 36-card plan, read six files in 31 seconds and was told -- as
the user -- that she had produced nothing and should wrap up; twelve such turns took her tools
away. MISAKA's port had counted read/grep/doc_read as idling, where FrontierAgent counts only
its four sub-agent management tools and calls data-gathering progress."""
import asyncio

from misaka.agent.guards import NoProgressGuard, _SessionGuards
from misaka.core.platform.vocabulary import NO_PROGRESS_VOCABULARY

SUBSTANTIVE = "Read the 1914 gazette and list every clause of the ordinance with its source."


def _call(name, call_id, **arguments):
    return {"type": "toolCall", "name": name, "id": call_id, "arguments": arguments}


def _result(name, call_id, **details):
    return {"toolName": name, "toolCallId": call_id, "details": details, "isError": False}


def _spawn(turn, prompt=SUBSTANTIVE, *, foreground=False):
    """One turn that creates a sub-agent: launched in the background, or run to its report."""
    call_id, agent = f"c{turn}", f"a{turn}"
    status = "completed" if foreground else "async_launched"
    return [_call("Agent", call_id, description="d", prompt=prompt)], [_result("Agent", call_id, status=status, agentId=agent)]


def _actions(guard, turns):
    return [guard.observe(calls, results).action for calls, results in turns]


def test_reading_and_searching_are_progress():
    guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
    reads = [([_call(name, f"r{i}", path="x")], []) for i in range(30) for name in ("read", "doc_read", "grep")]
    assert set(_actions(guard, reads)) == {"none"}


def test_waiting_on_running_sub_agents_is_not_a_spin():
    """Without repeated creation a management streak is fan-out or waiting: no nudge, however long."""
    guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
    polls = [([_call("TaskOutput", f"p{i}", task_id="a1")],
              [_result("TaskOutput", f"p{i}", retrieval_status="not_ready", task={"task_id": "a1"})])
             for i in range(20)]
    assert set(_actions(guard, [_spawn(0), *polls])) == {"none"}


def test_a_create_assign_spin_is_nudged_with_a_cooldown_then_stopped():
    guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
    actions = _actions(guard, [_spawn(turn) for turn in range(1, 13)])
    assert actions == ["none"] * 5 + ["hint", "none", "none", "hint", "none", "none", "stop"]


def test_a_fresh_substantive_report_resets_the_spin():
    guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
    turns = [_spawn(turn) for turn in range(1, 6)] + [_spawn(6, foreground=True)]
    turns += [_spawn(turn) for turn in range(7, 12)]
    assert set(_actions(guard, turns)) == {"none"}


def test_reports_from_wind_down_assignments_do_not_count_and_hasten_the_stop():
    guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
    turns = [_spawn(turn) for turn in range(1, 4)]
    turns += [_spawn(turn, prompt="Done.", foreground=True) for turn in range(4, 9)]
    actions = _actions(guard, turns)
    assert actions[5] == "hint" and actions[7] == "stop"     # degenerate: soft_streak + 2


def test_any_other_tool_or_a_tool_free_turn_resets():
    for reset in (([_call("read", "r", path="x")], []), ([], [])):
        guard = NoProgressGuard(NO_PROGRESS_VOCABULARY)
        turns = [_spawn(turn) for turn in range(1, 6)] + [reset] + [_spawn(turn) for turn in range(7, 12)]
        assert set(_actions(guard, turns)) == {"none"}


def test_without_a_vocabulary_the_guard_has_no_opinion():
    assert set(_actions(NoProgressGuard(), [_spawn(turn) for turn in range(1, 30)])) == {"none"}


def test_the_session_hands_the_guard_its_tool_results():
    """`_SessionGuards.after_turn` reads the turn's results: a spin is nudged through `steer`."""
    steered = []

    class Session:
        async def steer(self, text):
            steered.append(text)

    guards = _SessionGuards(Session(), no_progress_vocabulary=NO_PROGRESS_VOCABULARY)

    async def run():
        for turn in range(1, 7):
            calls, results = _spawn(turn, prompt=f"{SUBSTANTIVE} Part {turn}.")
            await guards.after_turn({"message": {"content": calls}, "toolResults": results})

    asyncio.run(run())
    assert len(steered) == 1 and "Agent" in steered[0] and "SendMessage" in steered[0]
