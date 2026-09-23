"""A research card finishing under a paused run wakes Last Order.

2026-09-18 (B26): T4 finished at 07:35 while its run was already failed; the notification was
recorded with triggerTurn off because "the phase consumes research results" -- but no phase
was running, so nobody read it until the user asked. The exemption now holds only while the
run is being driven."""
import pytest

from misaka.core.network import sister_runtime
from misaka.core.platform import tasks
from misaka.core.research import runs


@pytest.fixture
def board(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    runs.init(con)
    return con


def test_a_driven_run_reads_its_own_results(board, tmp_path):
    run = runs.create(board, workspace=str(tmp_path), question="why is the sky blue")
    assert sister_runtime._research_run_active(board, {"run_id": run["id"]})


@pytest.mark.parametrize("status", ["failed", "stopped", "done"])
def test_a_run_nobody_drives_needs_last_order_woken(board, tmp_path, status):
    run = runs.create(board, workspace=str(tmp_path), question="why is the sky blue")
    runs.set_state(board, run["id"], status=status)
    assert not sister_runtime._research_run_active(board, {"run_id": run["id"]})


def test_missing_context_or_board_wakes_last_order(board):
    assert not sister_runtime._research_run_active(board, None)
    assert not sister_runtime._research_run_active(board, {"run_id": "r_missing"})
    assert not sister_runtime._research_run_active(None, {"run_id": "r_x"})
