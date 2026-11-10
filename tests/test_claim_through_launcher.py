"""A research node claims its row from inside a launcher's child.

On Windows a venv's python.exe is a launcher that starts the real interpreter as its own child.
The driver records the PID its spawn handed back, the launcher's, so every node there refused its
own claim: "claim refused: runner identity mismatch: row holds pid L ..., this process is pid C".
Where this interpreter is itself such a launcher, the node is started through it, as the driver
starts one; elsewhere a shell stands in for it: another program, which starts the interpreter as
its own child and waits, so the same processes stand in the same relation on every platform."""
import os
import subprocess
import sys
import textwrap
from contextlib import closing
from pathlib import Path

import pytest

from misaka.core.platform import processes, tasks
from misaka.core.research import runs, workflow

ROOT = Path(__file__).resolve().parents[1]

# The node's side: wait for the driver's record when told to, as a node that takes a second to
# import finds it, then claim the row and report what it is.
NODE = textwrap.dedent("""
    import os, sys, time
    from contextlib import closing
    from misaka.core.platform import processes, tasks
    from misaka.core.research import runs
    db, row_id, key, wait = sys.argv[1:5]
    with closing(tasks.connect(db)) as con:
        deadline = time.monotonic() + 20
        while wait == "wait" and con.execute(
                "SELECT runner_pid FROM research_execution WHERE id=?", (row_id,)).fetchone()[0] is None:
            if time.monotonic() > deadline:
                sys.exit("the driver never recorded a runner")
            time.sleep(0.05)
        claimed = runs.claim_runner(con, "research_execution", row_id, key)
        print(claimed, os.getpid(), processes.identity(os.getpid()), sep="|")
        if not claimed:
            print(runs.note_claim_failure(con, "research_execution", row_id, key))
""")


def _env():
    return {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))}


def _python_is_a_launcher():
    """Whether starting ``sys.executable`` runs Python in a process other than the one handed back."""
    proc = subprocess.Popen([sys.executable, "-c", "import os; print(os.getpid())"], stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=60)
    return int(out) != proc.pid


def _through_a_launcher():
    if _python_is_a_launcher():
        return []
    if os.name == "nt":
        return ["cmd", "/c"]
    return ["/bin/sh", "-c", '"$@"; exit $?', "launcher"]     # a list, so the shell forks instead of exec'ing


def _node_row(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="Fixture question")
    return con, runs.nodes(con, run["id"])[0]["id"]


def _shifted(identity, seconds):
    host, pid, started = identity.split(":")
    return f"{host}:{pid}:{float(started) + seconds:.6f}"


@pytest.fixture
def launcher_parent(monkeypatch):
    """A live process running another program, standing as this process's parent: a launcher."""
    argv = ["ping", "-n", "60", "127.0.0.1"] if os.name == "nt" else ["sleep", "60"]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert runs._runs_another_program(proc.pid)
        monkeypatch.setattr(os, "getppid", lambda: proc.pid)
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_a_node_started_through_a_launcher_claims_its_row(tmp_path):
    (tmp_path / "node.py").write_text(NODE, encoding="utf-8")
    con, node_id = _node_row(tmp_path)
    with closing(con):
        key = runs.prepare_runner(con, "research_execution", node_id)
        launcher = subprocess.Popen(
            [*_through_a_launcher(), sys.executable, str(tmp_path / "node.py"), str(tmp_path / "board.db"),
             node_id, key, "wait"], env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        workflow._record_runner(con, "research_execution", node_id, launcher, key=key)   # as the driver does
        assert runs.node(con, node_id)["runner_pid"] == launcher.pid
        out, err = launcher.communicate(timeout=60)
        assert launcher.returncode == 0, err
        claimed, pid, identity = out.splitlines()[0].split("|")
        assert claimed == "True", out
        assert int(pid) != launcher.pid                     # the node is the launcher's child, not the launcher
        row = runs.node(con, node_id)
        assert (row["runner_pid"], row["runner_identity"]) == (int(pid), identity)   # the node's own reading


def test_a_claimed_nodes_own_child_with_the_same_key_cannot_take_its_row(tmp_path):
    (tmp_path / "node.py").write_text(NODE, encoding="utf-8")
    con, node_id = _node_row(tmp_path)
    with closing(con):
        key = runs.prepare_runner(con, "research_execution", node_id)
        assert runs.claim_runner(con, "research_execution", node_id, key) is True     # this process is the node
        child = subprocess.run([sys.executable, str(tmp_path / "node.py"), str(tmp_path / "board.db"), node_id,
                                key, "now"], env=_env(), capture_output=True, text=True, timeout=60, check=False)
        assert child.stdout.startswith("False|"), child.stdout + child.stderr
        assert "runner identity mismatch" in child.stdout
        assert runs.node(con, node_id)["runner_pid"] == os.getpid()
        assert runs.claim_runner(con, "research_execution", node_id, key) is True     # still the node's


class _LandsAfterTheRead:
    """Another writer's claim landing between the claim's read and its write."""

    def __init__(self, con, row_id, pid):
        self.con, self.row_id, self.pid, self.landed = con, row_id, pid, False

    def execute(self, sql, params=()):
        cursor = self.con.execute(sql, params)
        if sql.lstrip().startswith("SELECT") and not self.landed:
            self.landed = True
            row = cursor.fetchone()
            self.con.execute("UPDATE research_execution SET runner_pid=?, runner_identity=? "
                             "WHERE id=? AND runner_pid IS NULL", (self.pid, processes.identity(self.pid), self.row_id))
            return _Fetched(row)
        return cursor


class _Fetched:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_the_launchers_record_landing_between_read_and_write_is_judged_again(tmp_path, launcher_parent):
    parent = launcher_parent
    con, node_id = _node_row(tmp_path)
    with closing(con):
        key = runs.prepare_runner(con, "research_execution", node_id)
        racing = _LandsAfterTheRead(con, node_id, parent)
        assert runs.claim_runner(racing, "research_execution", node_id, key) is True
        assert racing.landed
        row = runs.node(con, node_id)
        assert (row["runner_pid"], row["runner_identity"]) == (os.getpid(), processes.identity(os.getpid()))


def test_a_duplicates_claim_landing_between_read_and_write_is_not_overwritten(tmp_path):
    con, node_id = _node_row(tmp_path)
    other = _sleeper()
    try:
        with closing(con):
            key = runs.prepare_runner(con, "research_execution", node_id)
            racing = _LandsAfterTheRead(con, node_id, other.pid)
            assert runs.claim_runner(racing, "research_execution", node_id, key) is False
            assert racing.landed
            assert runs.node(con, node_id)["runner_pid"] == other.pid
    finally:
        other.kill()
        other.wait()


def test_a_live_process_that_did_not_start_this_one_is_still_refused(tmp_path):
    con, node_id = _node_row(tmp_path)
    other = _sleeper()
    try:
        with closing(con):
            key = runs.prepare_runner(con, "research_execution", node_id)
            con.execute("UPDATE research_execution SET runner_pid=?, runner_identity=? WHERE id=?",
                        (other.pid, processes.identity(other.pid), node_id))
            assert runs.claim_runner(con, "research_execution", node_id, key) is False
            text = runs.note_claim_failure(con, "research_execution", node_id, key)
            assert "runner identity mismatch" in text and f"started by pid {os.getppid()}" in text
            assert runs.node(con, node_id)["runner_pid"] == other.pid
    finally:
        other.kill()
        other.wait()


def test_the_launchers_pid_with_another_start_time_is_a_reused_pid(tmp_path, launcher_parent):
    parent = launcher_parent
    con, node_id = _node_row(tmp_path)
    with closing(con):
        key = runs.prepare_runner(con, "research_execution", node_id)
        con.execute("UPDATE research_execution SET runner_pid=?, runner_identity=? WHERE id=?",
                    (parent, _shifted(processes.identity(parent), 3600.0), node_id))
        assert runs.claim_runner(con, "research_execution", node_id, key) is False
        assert runs.node(con, node_id)["runner_pid"] == parent
