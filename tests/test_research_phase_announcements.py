"""A note announcing work the next phase hands a Last Order stays out of what her model sees (R60).

2026-10-27, node b_44a31afcf8: the red-team Sister mailed her Last Order as the review finished,
which started a turn of its own; "the red-team review is back with its Last Order: 6 material
issue(s) to answer" landed in that turn, and she called ``misaka_research_dispose`` -- a tool the
dispose phase's prompt brings with it, not yet given -- and got "not found". The phase's prompt
carries the same review and the tool. The note still shows; a node's window had no filter at all,
and the root window keeps another node's notes, which are news there."""
from types import SimpleNamespace

import pytest

from misaka.core.platform import tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import node, research


@pytest.fixture
def board(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    runs.init(con)
    yield con
    con.close()


def note(node_id, *, announces=True, stage="review_returned"):
    details = {"stage": stage, "node": node_id, "run_id": "r"}
    if announces:
        details[workflow.ANNOUNCES_PHASE] = True
    return {"role": "custom", "customType": "research-progress", "display": True, "details": details,
            "content": f"note about {node_id}", "timestamp": 2}


USER = {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1}


def test_only_a_marked_progress_note_about_one_of_the_nodes_announces_a_phase():
    assert workflow.announces_phase_of(note("b_1"), {"b_1"})
    assert not workflow.announces_phase_of(note("b_2"), {"b_1"})
    assert not workflow.announces_phase_of(note("b_1", announces=False), {"b_1"})
    assert not workflow.announces_phase_of({**note("b_1"), "customType": "sister-notification"}, {"b_1"})
    assert not workflow.announces_phase_of(USER, {"b_1"})


async def test_a_node_window_hides_its_own_announcements_and_nothing_else():
    part = node.NodePart("r", "b_1", "key")
    own, other, plain = note("b_1"), note("b_2"), note("b_1", announces=False, stage="disposed")
    result = await part.context({"messages": [USER, own, other, plain]}, None)
    assert result["messages"] == [USER, other, plain]
    assert await part.context({"messages": [USER, plain]}, None) is None


async def test_the_root_window_hides_the_root_nodes_announcements_and_keeps_the_others(board, tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_con", lambda: board)
    run = runs.create(board, workspace=str(tmp_path), question="why", origin_session="s1")
    root = runs.root(board, run["id"])
    child = runs.create_node(board, run["id"], question="child", parents=[root["id"]])
    part = research.ResearchPart.__new__(research.ResearchPart)
    ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionId="s1"))
    own, other = note(root["id"]), note(child["id"])
    no_issues = note(root["id"], announces=False)
    result = await part.context({"messages": [USER, own, other, no_issues]}, ctx)
    kept = result["messages"]
    assert own not in kept and other in kept and no_issues in kept and USER in kept
    runs.set_state(board, run["id"], status="done")             # a finished run announces nothing
    result = await part.context({"messages": [USER, own]}, ctx)
    assert result is None or own in result["messages"]
