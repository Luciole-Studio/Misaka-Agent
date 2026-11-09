"""On Windows a Sister root's card is reclaimed once its process is gone (issue #10 plan, O1).

The root records a process-group identity on every platform, and terminate_orphaned_group refused
outright off POSIX: the card's lease was never reclaimed, and the panel's takeover kept saying its
previous process was still running."""
import subprocess
import sys

from misaka.core.platform import processes


def test_a_gone_or_reused_pid_is_absent_and_a_live_one_is_ended():
    reclaim = processes._terminate_recorded_tree        # what terminate_orphaned_group runs off POSIX
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        recorded = processes.identity(child.pid)
        assert reclaim(child.pid, "someone-else@0") is True, "a reused PID is not ours"
        assert child.poll() is None
        assert reclaim(child.pid, recorded) is True
        assert child.wait(5) is not None
        assert reclaim(child.pid, recorded) is True, "gone is absent"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)


def test_a_dead_leaders_children_are_ended_too(monkeypatch):
    """Windows keeps the dead leader's PID as its children's parent and has no group to signal:
    the old attempt's tool processes went on writing while the card ran again."""
    import os
    import time

    import psutil

    leader = subprocess.Popen([sys.executable, "-c", "pass"])
    recorded = processes.identity(leader.pid)
    leader.wait(5)
    time.sleep(0.05)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        def windows_like(attrs=None):
            for process in (psutil.Process(child.pid), psutil.Process(stranger.pid)):
                process.info = {"ppid": leader.pid if process.pid == child.pid else os.getpid(),
                                "create_time": process.create_time()}
                yield process
        monkeypatch.setattr(psutil, "process_iter", windows_like)
        assert processes._terminate_recorded_tree(leader.pid, recorded) is True
        assert child.wait(5) is not None
        assert stranger.poll() is None, "a process with another parent is left alone"
    finally:
        for process in (child, stranger):
            if process.poll() is None:
                process.kill()
            process.wait(5)


def test_this_process_holding_a_dead_roots_pid_reclaims_it():
    import os
    assert processes._terminate_recorded_tree(os.getpid(), "elsewhere:1:0.0") is True
    assert processes._terminate_recorded_tree(os.getpid(), processes.identity(os.getpid())) is False
