"""Every card has an output folder. Before this an ordinary card had none, so a `## deliverable` could never be met."""
import json
import os
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.network import worker
from misaka.core.platform import cards, repo, tasks
from misaka.core.research import runs

BODY = "## question\nq\n## acceptance criteria\nc\n## deliverable\n`report.md`\n"
PLAIN = "## question\nq\n## acceptance criteria\nc\n"

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    ws = tmp_path / "project"
    ws.mkdir()
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield SimpleNamespace(con=con, ws=os.path.realpath(ws))

def card(board, body=BODY):
    tid = cards.create(board.con, board.ws, "t", body, "10032")
    return tid, dict(tasks.get(board.con, tid))

def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def test_a_card_has_a_folder_of_its_own_and_nothing_is_made_early(board):
    tid, row = card(board)
    assert row["output_dir"] == os.path.join(board.ws, "outputs", tid)
    assert not os.path.exists(row["output_dir"])

def test_a_file_already_in_the_project_does_not_meet_the_contract(board):
    _tid, row = card(board)
    write(os.path.join(board.ws, "report.md"), "last month's")
    assert worker.missing_deliverable(row) == "report.md"

def test_the_card_s_own_file_meets_it(board):
    _tid, row = card(board)
    write(os.path.join(row["output_dir"], "report.md"))
    assert worker.missing_deliverable(row) is None

def test_an_attachment_of_the_same_name_does_not_meet_it(board, tmp_path):
    tid, row = card(board)
    source = tmp_path / "report.md"
    source.write_text("an input")
    cards.attach(board.ws, tid, str(source))
    assert worker.missing_deliverable(row) == "report.md"

def test_an_output_is_not_listed_as_an_attachment(board):
    tid, row = card(board)
    write(os.path.join(row["output_dir"], "report.md"))
    assert cards.attachment_list(board.ws, tid) == []

def test_deleting_a_card_leaves_what_it_produced_and_says_so(board):
    tid, row = card(board)
    write(os.path.join(row["output_dir"], "report.md"))
    ok, message = cards.remove(board.con, board.ws, tid)
    assert ok and os.path.isfile(os.path.join(row["output_dir"], "report.md"))
    assert f"outputs/{tid}" in message

def test_an_attachment_cannot_take_the_folder_s_place(board, tmp_path):
    tid, row = card(board)
    source = tmp_path / "output"
    source.write_text("an input")
    cards.attach(board.ws, tid, str(source))
    write(os.path.join(row["output_dir"], "report.md"))
    assert worker.missing_deliverable(row) is None

def test_a_card_from_before_every_card_had_a_folder_gets_one_when_idle(board):
    tid, row = card(board)
    board.con.execute("UPDATE tasks SET output_dir=NULL WHERE id=?", (tid,))
    cards.reconcile_one(board.con, board.ws, tid)
    assert tasks.get(board.con, tid)["output_dir"] == row["output_dir"]

def test_a_running_card_is_left_as_it_is(board):
    tid, _row = card(board)
    board.con.execute("UPDATE tasks SET output_dir=NULL, status='running' WHERE id=?", (tid,))
    cards.reconcile_one(board.con, board.ws, tid)
    assert tasks.get(board.con, tid)["output_dir"] is None

def test_a_near_name_does_not_meet_the_contract(board):
    # 结论.md is not 草稿.md, and 2024.csv is not 2025.csv.
    _tid, row = card(board)
    write(os.path.join(row["output_dir"], "repert.md"))
    assert worker.missing_deliverable(row) == "report.md"

def test_creating_a_card_keeps_the_board_lock(board):
    assert hasattr(tasks.create_task, "__wrapped__") and not hasattr(tasks.card_output_dir, "__wrapped__")

def test_the_outline_lists_a_folder_only_when_there_is_one(board):
    from misaka.workspace import outline
    _tid, row = card(board)
    assert "Deliverable directory" not in json.dumps(outline(board.con, workspace=board.ws), ensure_ascii=False)
    write(os.path.join(row["output_dir"], "report.md"))
    assert "Deliverable directory" in json.dumps(outline(board.con, workspace=board.ws), ensure_ascii=False)

def test_a_card_restored_from_its_file_has_the_folder(board):
    tid, row = card(board)
    board.con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    cards._PARSED.clear()
    cards.rebuild(board.con, board.ws)
    assert tasks.get(board.con, tid)["output_dir"] == row["output_dir"]

def test_a_deliverable_added_to_the_card_file_later_can_be_met(board):
    tid, _row = card(board, PLAIN)
    path = Path(cards.card_path(board.ws, tid))
    path.write_text(path.read_text(encoding="utf-8").replace("## log", "## deliverable\n`report.md`\n\n## log"), encoding="utf-8")
    cards.reconcile_one(board.con, board.ws, tid)
    row = dict(tasks.get(board.con, tid))
    assert worker.missing_deliverable(row) == "report.md"
    write(os.path.join(row["output_dir"], "report.md"))
    assert worker.missing_deliverable(row) is None

def test_a_research_card_keeps_its_folder_and_leaves_no_other(board):
    run = runs.create(board.con, workspace=board.ws, question="How was the salt monopoly run?")
    tid = cards.create(board.con, board.ws, "t", BODY, "10032", after_row=lambda t: runs.link_task(
        board.con, run["id"], t, kind="research", node=runs.root(board.con, run["id"]), local_id="a"))
    row = tasks.get(board.con, tid)
    assert os.sep + "nodes" + os.sep in row["output_dir"]
    assert not os.path.exists(os.path.join(board.ws, "outputs", tid))

def test_the_prompt_names_the_folder_only_to_a_card_that_delivers_there(board):
    _tid, row = card(board)
    assert row["output_dir"] in worker.card_prompt(row)
    _tid, plain = card(board, PLAIN)
    assert "Deliverable location" not in worker.card_prompt(plain)

def test_a_workflow_card_without_a_contract_file_is_still_told(board):
    run = runs.create(board.con, workspace=board.ws, question="How was the salt monopoly run?")
    tid = cards.create(board.con, board.ws, "t", PLAIN, "10032", after_row=lambda t: runs.link_task(
        board.con, run["id"], t, kind="red_team", node=runs.root(board.con, run["id"]), local_id="a"))
    assert "Deliverable location" in worker.card_prompt(dict(tasks.get(board.con, tid)))

def test_a_malformed_contract_is_still_told_where_to_write(board):
    _tid, row = card(board, BODY + "\n## deliverable\n`other.md`\n")
    assert "Deliverable location" in worker.card_prompt(row)

def test_the_deliverable_reaches_the_submission(board):
    tid, row = card(board)
    tasks.claim(board.con, tid, "lock", generation=1, pid=os.getpid(), worker_identity="w", host_cap=9, assignee_cap=9)
    row = dict(tasks.get(board.con, tid))
    worker.record_output_baseline(board.con, row)
    write(os.path.join(row["output_dir"], "report.md"))
    submission = worker.build_submission(board.con, row, "done")
    assert f"outputs/{tid}/report.md" in json.dumps(submission)

def test_a_running_card_may_write_only_to_its_folder(board, monkeypatch):
    from misaka.core.network.todo import TodoPart
    tid, _row = card(board)
    tasks.claim(board.con, tid, "lock", generation=1, pid=os.getpid(), worker_identity="w", host_cap=9, assignee_cap=9)
    part = TodoPart(tid, "10032")
    part.con = lambda: board.con
    part._ownership = lambda: (1, "lock")
    part.session = SimpleNamespace(getActiveToolNames=lambda: ["read", "write", "edit"])
    grants = next(fn for name, fn in vars(TodoPart).items() if "writeDirectories" in str(getattr(getattr(fn, "__code__", None), "co_consts", "")))
    assert grants(part)["writeDirectories"]["write"] == [os.path.join(board.ws, "outputs", tid)]
