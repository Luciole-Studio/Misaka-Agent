"""A blocked event loop becomes a stack trace -- and, in a process nobody is watching, an exit.

Every MISAKA process does its work on one asyncio loop. Synchronous code that never returns on it
(GitHub issue #10: the live-skill guard looping forever before a bash call) freezes everything
that process does -- heartbeats, leases, the TUI -- and nothing inside the process can notice,
because noticing needs the loop. ``faulthandler``'s watchdog is a C thread that needs neither the
loop nor the GIL: the running loop re-arms it every ``STALL_SECONDS / 4``, and if no loop re-arms
it, it writes every thread's stack to ``logs/stalls/<label>-<pid>.log`` -- and, in a process that
exits on a stall (an unattended card, node or sub-agent), ends it with status 1, which every
supervisor already treats as a crashed attempt and retries. A log with no stall in it is removed
when its process ends, and logs older than ``KEEP_DAYS`` when the next process starts.

Nothing happens until a process entry calls :func:`configure`; library code and tests are
untouched. Whichever loop is running re-arms the one timer, so ``run_coro``'s nested loop (whose
caller's loop is waiting on it) keeps the watch going. Known limit: with two loops alive in two
threads, the live one keeps re-arming while the other is frozen. A process stopped from outside
(SIGSTOP, a debugger) for longer than the limit is taken for stalled when it continues: the C timer
counts the stopped time and fires before any Python code runs. The TUI's own Ctrl+Z disarms first
(:func:`suspend`).
"""
from __future__ import annotations

import asyncio
import atexit
import faulthandler
import os
import re
import signal
import sys
import sysconfig
import time

STALL_SECONDS = 300.0
KEEP_DAYS = 14
_settings: dict | None = None
_file = None
_depth = 0


def _seconds() -> float:
    try:
        return float(os.environ.get("MISAKA_LOOP_STALL_SECONDS") or STALL_SECONDS)
    except ValueError:
        return STALL_SECONDS


def log_path(label: str, pid: int) -> str:
    from misaka.config import home
    return os.path.join(str(home.path("stall_logs")), f"{label}-{int(pid)}.log")


def configure(label: str, *, exit_on_stall: bool) -> None:
    """Watch this process's loops from now on (once per process, at its entry)."""
    global _settings
    _settings = {"label": re.sub(r"[^A-Za-z0-9_.-]", "_", label) or "process", "exit": exit_on_stall}


def _open():
    global _file
    if _file is None:
        path = log_path(_settings["label"], os.getpid())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _prune(os.path.dirname(path))
        _file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - faulthandler needs it open for the process's life
        before = _file.tell()
        _file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {_settings['label']} pid {os.getpid()} "
                    f"stall {_seconds():g} s exit {_settings['exit']} argv {sys.argv!r}\n")
        _file.flush()
        atexit.register(_drop_if_clean, _file, path, before, _file.tell())
    return _file


def _drop_if_clean(file, path: str, before: int, after: int) -> None:
    """At a normal exit: take this process's header back out when nothing followed it (a stall
    exits through faulthandler, never through here, so its stack stays)."""
    try:
        if os.path.getsize(path) != after:
            return
        file.close()
        if before == 0:
            os.remove(path)
        else:
            os.truncate(path, before)
    except OSError:
        pass


def _prune(folder: str) -> None:
    """Remove logs older than ``KEEP_DAYS`` -- not one whose process still runs: a panel open for
    weeks without a stall would dump its next stack into a file no one can find."""
    import psutil
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        pid = entry.name[:-len(".log")].rpartition("-")[2]
        try:
            if (entry.name.endswith(".log") and entry.stat().st_mtime < cutoff
                    and not (pid.isdigit() and psutil.pid_exists(int(pid)))):
                os.remove(entry.path)
        except OSError:
            continue


def _arm(seconds: float) -> None:
    faulthandler.dump_traceback_later(seconds, repeat=not _settings["exit"], file=_open(), exit=_settings["exit"])


async def _rearm(seconds: float) -> None:
    while True:
        _arm(seconds)
        await asyncio.sleep(seconds / 4)


def suspend() -> None:
    """Disarm before this process stops itself (the TUI's Ctrl+Z): the C timer would count the
    stopped time and fire the moment the process continued -- an exit, in an unattended window.
    The loop re-arms itself on its first turn after it continues."""
    if _settings is not None:
        faulthandler.cancel_dump_traceback_later()


def _end_on_sigterm():
    """On POSIX, a SIGTERM -- how a halted run stops its cards and nodes -- cancels this coroutine
    instead of killing the process outright, so what it holds is let go on the way out: a model
    request's token lease is settled, not left to expire and be charged in full (0.18.9 sweep).
    A Python-level handler, as a window's TUI installs its own: the TUI's, installed later, takes
    the signal over whole for its orderly shutdown (an asyncio handler fired beside it and cut
    that shutdown short). Returns ``(was it asked, undo)``; Windows ends a process outright."""
    asked = []
    if os.name != "posix":
        return asked, lambda: None
    loop, task = asyncio.get_running_loop(), asyncio.current_task()

    def ended(_signum, _frame):
        asked.append(True)
        loop.call_soon_threadsafe(task.cancel)

    original = signal.getsignal(signal.SIGTERM)
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: None)  # windows-footgun: ok - POSIX only, returned early above
        signal.signal(signal.SIGTERM, ended)
    except (NotImplementedError, RuntimeError, ValueError):
        return asked, lambda: None

    def undo():
        if signal.getsignal(signal.SIGTERM) is ended:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, original)
    return asked, undo


async def watched(coro):
    """``await coro`` with this loop re-arming the watchdog, and ended gracefully by a SIGTERM;
    a pass-through when not configured."""
    global _depth
    if _settings is None:
        return await coro
    _arm(_seconds())
    rearm = asyncio.ensure_future(_rearm(_seconds()))
    _depth += 1
    terminated, undo = _end_on_sigterm() if _depth == 1 else ([], lambda: None)
    try:
        return await coro
    except asyncio.CancelledError:
        if terminated:
            raise SystemExit(128 + signal.SIGTERM) from None
        raise
    finally:
        _depth -= 1
        rearm.cancel()
        undo()
        if _depth == 0:
            faulthandler.cancel_dump_traceback_later()


_FRAME = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')
_HEADER = re.compile(r"^--- .* stall ([0-9.]+) s exit ", re.MULTILINE)


def report(label: str, pid: int | None = None, *, since: float | None = None) -> str | None:
    """One line for a failure reason: where the loop of ``label``'s process was stuck, and the log
    with the whole stack; None when it left no stall. ``pid`` names the process; without it (a
    pane's process may be a launcher) the newest log of ``label`` written after ``since`` is read."""
    import glob

    label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
    if pid:
        path = log_path(label, pid)
    else:
        logs = [p for p in glob.glob(log_path(label, 0)[:-len("0.log")] + "*.log")
                if since is None or os.path.getmtime(p) >= since]
        if not logs:
            return None
        path = max(logs, key=os.path.getmtime)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    headers = list(_HEADER.finditer(text))
    if headers:
        text = text[headers[-1].start():]
    if "Timeout (" not in text:
        return None
    seconds = float(headers[-1].group(1)) if headers else _seconds()
    last = text[text.rindex("Timeout ("):]
    for block in re.split(r"\n(?=(?:Current thread|Thread) 0x)", last):
        frames = _FRAME.findall(block)
        if frames and any("asyncio" in file and func in ("run_forever", "_run_once") for file, _line, func in frames):
            stdlib = sysconfig.get_paths()["stdlib"]
            shown = [frames[0]]
            own = next((frame for frame in frames if not os.path.realpath(frame[0]).startswith(os.path.realpath(stdlib))
                        and "asyncio" not in frame[0]), None)
            if own is not None and own != frames[0]:
                shown.append(own)
            where = " <- ".join(f"{os.path.basename(file)}:{line} in {func}" for file, line, func in shown)
            return f"the event loop was blocked for over {seconds:g} s at {where}; full stack in {path}"
    return f"the event loop was blocked for over {seconds:g} s; stack in {path}"
