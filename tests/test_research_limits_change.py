"""A running run's limits can be changed (GitHub issue #9).

A run's limits were written once, when it was created, and nothing could change them: a run
started with four Sisters per node and a cluster that could take one more had to be stopped and
started again. They are now changed in place, read where each is used, and never set below what
the graph already holds."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.core.platform import repo, tasks
from misaka.core.research import runs
from misaka.core.research.wiring import research


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield con


def _run(board, tmp_path, **limits):
    return runs.create(board, workspace=str(tmp_path), question="How was the salt monopoly run?", limits=limits)


def test_a_change_is_saved_and_read_back_where_the_run_uses_it(board, tmp_path):
    run = _run(board, tmp_path, sister_parallel=4, context_threshold=0.6)
    before, after = runs.update_limits(board, run["id"], {"sister_parallel": "2", "max_revisions": 1})
    assert (before["sister_parallel"], after["sister_parallel"], after["max_revisions"]) == (4, 2, 1)
    assert runs.current_limits(board, run)["sister_parallel"] == 2, "the run row a driver holds is not trusted"
    assert runs.limits(run)["sister_parallel"] == 4
    assert json.loads(runs.get(board, run["id"])["limits_json"])["context_threshold"] == 0.6, "other settings kept"
    assert runs.limits_changed(before, after) == ("sister_parallel 4 → 2 (at once); "
                                                  "max_revisions 2 → 1 (at each node's next review)")


def test_a_limit_is_never_set_below_what_the_graph_holds(board, tmp_path):
    run = _run(board, tmp_path)
    root = runs.node(board, board.execute("SELECT id FROM research_execution WHERE run_id=?", (run["id"],)).fetchone()[0])
    with tasks.write_txn(board):
        child = runs.create_node(board, run["id"], question="Who bought the licences?", parents=[root["id"]])
        runs.create_node(board, run["id"], question="Who smuggled?", parents=[child["id"]])
    with pytest.raises(ValueError, match="below the 3 nodes"):
        runs.update_limits(board, run["id"], {"max_nodes": 2})
    with pytest.raises(ValueError, match="below the depth 2"):
        runs.update_limits(board, run["id"], {"max_depth": 1})
    with pytest.raises(ValueError, match="sister_parallel must be a positive integer"):
        runs.update_limits(board, run["id"], {"sister_parallel": 0})
    with pytest.raises(ValueError, match="not adjustable: plan_approval"):
        runs.update_limits(board, run["id"], {"plan_approval": False})
    assert runs.limits(runs.get(board, run["id"])) == runs.DEFAULT_LIMITS, "a refused change changes nothing"


def test_a_finished_run_is_left_alone(board, tmp_path):
    run = _run(board, tmp_path)
    board.execute("UPDATE research_runs SET status='done' WHERE id=?", (run["id"],))
    with pytest.raises(ValueError, match="is done"):
        runs.update_limits(board, run["id"], {"parallel": 2})


@pytest.mark.parametrize("line, expected", [
    ("limits --sister-parallel 2", {"action": "limits", "run_id": None, "changes": {"sister_parallel": "2"}}),
    ("limits r_0123456789 --parallel=1 --max-nodes 40",
     {"action": "limits", "run_id": "r_0123456789", "changes": {"parallel": "1", "max_nodes": "40"}}),
])
def test_the_panel_command(line, expected):
    assert research.parse_command(line) == expected


@pytest.mark.parametrize("line", ["limits", "limits r_0123456789", "limits --plan-approval 0",
                                  "limits --parallel 1 --parallel 2"])
def test_the_panel_command_says_what_is_wrong(line):
    with pytest.raises(ValueError):
        research.parse_command(line)


def test_the_cli_changes_a_running_run(board, tmp_path, monkeypatch, capsys):
    from misaka.cli import app
    run = _run(board, tmp_path)
    monkeypatch.setattr(app, "current_config", lambda: {"db": "board.db", "profiles_root": str(tmp_path)})
    monkeypatch.setattr(app.db, "connect", lambda path: board)
    args = SimpleNamespace(node=None, runner_key=None, tell=None, limits=run["id"], resume=None, goal=None, depth=None,
                           parallel=None, sister_parallel=1, followups=None, revisions=None, max_nodes=None)
    app._cmd_research(args)
    assert "sister_parallel 4 → 1 (at once)" in capsys.readouterr().out
    assert runs.limits(runs.get(board, run["id"]))["sister_parallel"] == 1


@pytest.mark.parametrize("line", ["limits of state capacity in Qing China", "status of women in Tang China",
                                  "status of women's work in Song China", "stop the war: 1914 and after",
                                  "help me compare two land reforms", "start tell me why the Song dynasty fell"])
def test_a_question_that_starts_with_a_subcommand_word_is_a_question(line):
    """0.18.7 took these for a limits change, a status with too many words, ... and refused them."""
    from misaka.core.research.wiring.research import parse_command
    parsed = parse_command(line)
    assert parsed["action"] == "activate" and parsed["question"] == line.removeprefix("start ")


@pytest.mark.parametrize("line", ["stop r_abc now", "status r_abc extra"])
def test_words_after_a_run_id_are_a_mistake_not_a_new_question(line):
    """The first fix read these as a question and would have started a new, paid run."""
    from misaka.core.research.wiring.research import parse_command
    with pytest.raises(ValueError, match="Usage"):
        parse_command(line)
