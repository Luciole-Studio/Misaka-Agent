"""A blocked event loop leaves a stack trace, and an unattended process exits (issue #10).

Each case runs in its own process: the watchdog is per process, and an exit is its whole point."""
import os
import subprocess
import sys
import textwrap

import pytest

from misaka.utils import loop_watchdog

SCRIPT = textwrap.dedent('''
    import asyncio, os, re, signal, sys, time
    from misaka.utils import loop_watchdog

    def spin_forever():
        while True:
            pass

    def backtrack_forever():
        re.match(r"(a+)+$", "a" * 40 + "!")

    async def body(kind):
        if kind == "early":
            spin_forever()              # before the coroutine first yields
        await asyncio.sleep(0.2)
        if kind == "suspended":
            loop_watchdog.suspend()     # as the TUI's Ctrl+Z does
            os.kill(os.getpid(), signal.SIGSTOP)
            await asyncio.sleep(0.5)
        elif kind == "python":
            spin_forever()
        elif kind == "regex":
            backtrack_forever()
        elif kind == "blocked":
            time.sleep(2.5)
        else:
            await asyncio.sleep(3)

    kind, exit_on_stall, configured = sys.argv[1], sys.argv[2] == "1", sys.argv[3] == "1"
    if configured:
        loop_watchdog.configure("card-t_test", exit_on_stall=exit_on_stall)
    asyncio.run(loop_watchdog.watched(body(kind)))
''')


def _run(tmp_path, kind, *, exit_on_stall=True, configured=True):
    env = {**os.environ, "MISAKA_HOME": str(tmp_path), "MISAKA_LOOP_STALL_SECONDS": "1"}
    done = subprocess.run([sys.executable, "-c", SCRIPT, kind, "1" if exit_on_stall else "0", "1" if configured else "0"],
                          env=env, capture_output=True, text=True, timeout=30, check=False)
    logs = list((tmp_path / "logs" / "stalls").glob("card-t_test-*.log")) if (tmp_path / "logs").exists() else []
    return done.returncode, (logs[0].read_text(encoding="utf-8") if logs else ""), logs


@pytest.mark.parametrize("kind, frame", [("python", "spin_forever"), ("regex", "backtrack_forever")])
def test_an_unattended_process_writes_where_it_is_stuck_and_exits(tmp_path, kind, frame, monkeypatch):
    code, log, logs = _run(tmp_path, kind)
    assert code == 1 and "Timeout (" in log and frame in log
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path))
    pid = int(logs[0].stem.rsplit("-", 1)[1])
    assert f"in {frame}" in loop_watchdog.report("card-t_test", pid)
    assert f"in {frame}" in loop_watchdog.report("card-t_test"), "found without the pid too"


def test_an_attended_process_writes_the_stack_and_goes_on(tmp_path):
    code, log, _logs = _run(tmp_path, "blocked", exit_on_stall=False)
    assert code == 0 and "Timeout (" in log


def test_a_healthy_loop_writes_no_stack_and_leaves_no_log(tmp_path):
    code, _log, logs = _run(tmp_path, "healthy")
    assert code == 0 and logs == []


def test_a_coroutine_stuck_before_it_first_yields_is_caught(tmp_path):
    code, log, _logs = _run(tmp_path, "early")
    assert code == 1 and "spin_forever" in log


@pytest.mark.skipif(os.name == "nt", reason="SIGSTOP is POSIX")
def test_a_process_that_suspends_itself_is_not_taken_for_stalled(tmp_path):
    import signal
    import time
    env = {**os.environ, "MISAKA_HOME": str(tmp_path), "MISAKA_LOOP_STALL_SECONDS": "1"}
    child = subprocess.Popen([sys.executable, "-c", SCRIPT, "suspended", "1", "1"], env=env)
    time.sleep(3)
    child.send_signal(signal.SIGCONT)
    assert child.wait(timeout=30) == 0


def test_a_report_reads_only_the_last_process_of_a_reused_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path))
    path = loop_watchdog.log_path("card-t_test", 4242)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("--- 2026-10-01 10:00:00 card-t_test pid 4242 stall 1 s exit True argv []\n"
                "Timeout (0:00:01)!\nThread 0x1 (most recent call first):\n"
                '  File "/x/body.py", line 3 in spin_forever\n'
                "--- 2026-10-08 10:00:00 card-t_test pid 4242 stall 300 s exit True argv []\n")
    assert loop_watchdog.report("card-t_test", 4242) is None, "the earlier process's stall is not this one's"
    with open(path, "a", encoding="utf-8") as f:
        f.write('Timeout (0:05:00)!\nCurrent thread 0x2 (most recent call first):\n  File "/x/a.py", line 9 in hang\n')
    assert "over 300 s" in loop_watchdog.report("card-t_test", 4242)


def test_old_logs_are_pruned_when_a_process_starts(tmp_path):
    folder = tmp_path / "logs" / "stalls"
    folder.mkdir(parents=True)
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True,
                          text=True, check=True).stdout.strip()
    old, recent = folder / f"card-old-{gone}.log", folder / "card-recent-2.log"
    running = folder / f"panel-daemon-{os.getpid()}.log"
    for path in (old, recent, running):
        path.write_text("--- header\nTimeout (\n", encoding="utf-8")
    month_ago = os.path.getmtime(old) - 30 * 86400
    for path in (old, running):
        os.utime(path, (month_ago, month_ago))
    _run(tmp_path, "healthy")
    assert not old.exists() and recent.exists() and running.exists()


def test_an_unconfigured_process_is_not_watched(tmp_path):
    code, _log, logs = _run(tmp_path, "blocked", configured=False)
    assert code == 0 and logs == []



@pytest.mark.parametrize("pane, tty, exits", [("p1", True, False), ("", False, True)])
def test_a_node_window_a_person_reads_is_left_open_on_a_stall(monkeypatch, pane, tty, exits):
    """An interactive node window stays open after its routine for the user; a stall killed it."""
    import sys

    from misaka.core.research import node
    monkeypatch.setenv("MISAKA_NET_PANE", pane)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(node, "run_interactive", lambda *a, **k: "window")
    monkeypatch.setattr(node, "run_headless", lambda *a, **k: "headless")
    node.main("r_1", "b_1", runner_key="k")
    assert loop_watchdog._settings == {"label": "node-b_1", "exit": exits}


TUI_SHUTDOWN = textwrap.dedent('''
    import asyncio, os, signal
    from misaka.utils import loop_watchdog

    async def interactive():          # InteractiveMode: its own SIGTERM handler shuts it down in order
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        async def shutdown():
            signal.signal(signal.SIGTERM, previous)     # unregisterSignalHandlers(), first thing
            await asyncio.sleep(0.2)                    # drain input, stop the TUI, dispose
            print("shut down in order", flush=True)
            done.set_result(0)
        previous = signal.signal(signal.SIGTERM, lambda *_: loop.create_task(shutdown()))
        loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        return await done

    loop_watchdog.configure("chat", exit_on_stall=False)
    raise SystemExit(asyncio.run(loop_watchdog.watched(interactive())))
''')


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM is POSIX")
def test_a_window_that_takes_sigterm_itself_shuts_down_in_its_own_order(tmp_path):
    """The watchdog's SIGTERM cancelled the window's task mid-shutdown: the terminal stayed raw."""
    env = {**os.environ, "MISAKA_HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", TUI_SHUTDOWN], text=True, encoding="utf-8",
                          env=env, capture_output=True, timeout=30, check=False)
    assert done.returncode == 0 and "shut down in order" in done.stdout
