"""psutil's create_time() on macOS is corrected by the drift of `kern.boottime` since the
process imported psutil, so two processes disagree about a third one's start time by exactly
that drift once it reaches a second (2026-09-18 B20/B33; pinned 2026-09-24: eight fork nodes
refused their own claims, row `…154.054516` against the child's `…153.054516`). Identities are
now compared within a tolerance; a reused PID starts hours later, so it is still told apart."""
import os
from contextlib import closing

import pytest

from misaka.core.platform import processes, tasks
from misaka.core.research import runs


def _shifted(identity, seconds):
    host, pid, started = identity.split(":")
    return f"{host}:{pid}:{float(started) + seconds:.6f}"


def test_a_start_time_a_second_off_is_the_same_process_but_a_reused_pid_is_not():
    mine = processes.identity(os.getpid())
    assert processes.same_identity(mine, mine)
    assert processes.same_identity(mine, _shifted(mine, 1.0))
    assert processes.same_identity(mine, _shifted(mine, -2.0))
    assert not processes.same_identity(mine, _shifted(mine, 3600.0))       # a PID reused an hour later
    host, pid, started = mine.split(":")
    assert not processes.same_identity(mine, f"other-host:{pid}:{started}")
    assert not processes.same_identity(mine, f"{host}:{int(pid) + 1}:{started}")
    assert not processes.same_identity(None, None) and not processes.same_identity(mine, None)
    assert processes.same_identity("h:1:Wed Sep 24 02:00:00 2026", "h:1:Wed Sep 24 02:00:00 2026")
    assert not processes.same_identity("h:1:Wed Sep 24 02:00:00 2026", "h:1:Wed Sep 24 02:00:01 2026")


def test_liveness_accepts_a_recorded_identity_from_a_drifted_reader():
    mine = processes.identity(os.getpid())
    alive, reason = processes.explain_liveness(os.getpid(), _shifted(mine, 1.0))
    assert alive and reason == "alive"
    alive, reason = processes.explain_liveness(os.getpid(), _shifted(mine, 3600.0))
    assert not alive and reason.startswith("identity mismatch")


def test_a_node_claims_its_row_although_the_parent_recorded_a_drifted_identity(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Fixture question")
        node = runs.nodes(con, run["id"])[0]
        key = runs.prepare_runner(con, "research_execution", node["id"])
        mine = processes.identity(os.getpid())
        con.execute("UPDATE research_execution SET runner_pid=?, runner_identity=? WHERE id=?",
                    (os.getpid(), _shifted(mine, 1.0), node["id"]))
        assert runs.claim_runner(con, "research_execution", node["id"], key) is True
        row = runs.node(con, node["id"])
        assert row["runner_identity"] == mine                                # the child's own reading stands

        con.execute("UPDATE research_execution SET runner_identity=? WHERE id=?",
                    (_shifted(mine, 3600.0), node["id"]))
        assert runs.claim_runner(con, "research_execution", node["id"], key) is False
        assert "identity mismatch" in runs.note_claim_failure(con, "research_execution", node["id"], key)


# Only a POSIX worker publishes a process-group identity (dispatch._worker_identity), so only
# there does the reconciler fence a group.
posix_groups = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _group_leader():
    """A child leading its own process group, reaped in the background so a killed leader
    does not linger as a zombie that still answers ``killpg(pgid, 0)``."""
    import subprocess
    import threading

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


@posix_groups
def test_a_drifted_leader_identity_still_gets_its_group_stopped():
    proc = _group_leader()
    try:
        recorded = _shifted(processes.identity(proc.pid), 1.0)      # the recorder read one second off
        assert processes.terminate_orphaned_group(proc.pid, recorded) is True
        assert proc.wait(timeout=5) is not None                         # stopped, not just forgotten
    finally:
        proc.kill()


@posix_groups
def test_a_reused_pid_is_never_signalled():
    proc = _group_leader()
    try:
        recorded = _shifted(processes.identity(proc.pid), 3600.0)   # a different, older process
        assert processes.terminate_orphaned_group(proc.pid, recorded) is True
        assert proc.poll() is None                                   # the new owner of the PID lives
    finally:
        proc.kill()
