"""A driver that died hard (a crashed pane, SIGKILL) leaves its lease behind, and until the
300 s TTL ran out `/research resume` was refused as "already being driven" (2026-09-23 23:41,
three refusals, cleared by hand in the database). A lease whose lock names this host and a
pid that no longer exists is taken over at once; a live pid, a foreign host and a lock that
does not name a process still wait for the TTL."""
import os
import socket
import time
from contextlib import closing

import psutil
import pytest

from misaka.core.platform import tasks
from misaka.core.research import runs


@pytest.fixture
def run(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        yield con, runs.create(con, workspace=str(tmp_path), question="Fixture question")


def _dead_pid():
    pid = 99990
    while psutil.pid_exists(pid):
        pid -= 1
    return pid


def _hold(con, run_id, lock):
    con.execute("UPDATE graphs SET lease_token=?, lease_expires=? WHERE id=?",
                (lock, int(time.time()) + 300, run_id))


def test_a_dead_driver_on_this_host_is_taken_over_at_once(run):
    con, row = run
    _hold(con, row["id"], f"driver:{socket.gethostname()}:{_dead_pid()}:abc123")
    assert runs.acquire_driver(con, row["id"], "driver:me:1:new") is True
    assert runs.get(con, row["id"])["driver_lock"] == "driver:me:1:new"


@pytest.mark.parametrize("lock", [
    f"driver:{socket.gethostname()}:{os.getpid()}:live",   # this very process
    "driver:some-other-host:1:foreign",                      # never judged from here
    "driver:junk",                                           # not a process at all
])
def test_a_live_foreign_or_unreadable_lease_still_waits_for_the_ttl(run, lock):
    con, row = run
    _hold(con, row["id"], lock)
    assert runs.acquire_driver(con, row["id"], "driver:me:1:new") is False
    assert runs.get(con, row["id"])["driver_lock"] == lock
