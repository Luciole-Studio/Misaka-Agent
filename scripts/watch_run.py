#!/usr/bin/env python3
"""Watch a live MISAKA run and report the failure signatures the audit identified.

The 2026-09-02 audit found 222 defects and four waves of fixes closed most of them, but
every one of those conclusions came from static reading and targeted repro -- nothing was
ever verified against the system actually running. This watches the traces a real run
leaves on disk and names, in the audit's own vocabulary, anything that looks like one of
them coming true.

It sees only what reaches disk: the board, the session transcripts, the logs, the process
table, and files the tools wrote. It cannot see the TUI screen or in-process state, so a
clean report means "nothing observable went wrong", not "nothing went wrong".

    python scripts/watch_run.py                 # poll every 5s until Ctrl-C
    python scripts/watch_run.py --once          # one pass, exit 1 if anything fires
    python scripts/watch_run.py --since-now     # ignore whatever is already broken

`--since-now` matters: this board already carries zombies from earlier testing, and
without a baseline they drown out whatever the current run does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~/.misaka"))
BOARD = HOME / "board.db"
SESSIONS = HOME / "sessions"
CRASH_LOG = HOME / "panel-crash.log"

FATAL, BAD, SUSPECT = "fatal", "bad", "suspicious"
_ORDER = {FATAL: 0, BAD: 1, SUSPECT: 2}


class Finding:
    __slots__ = ("audit", "detail", "name", "severity")

    def __init__(self, severity: str, name: str, detail: str, audit: str) -> None:
        self.severity, self.name, self.detail, self.audit = severity, name, detail, audit

    def key(self) -> tuple:
        return (self.name, self.detail)

    def __str__(self) -> str:
        tag = {FATAL: "FATAL", BAD: "BAD  ", SUSPECT: "?    "}[self.severity]
        return f"  {tag} {self.name}: {self.detail}   [{self.audit}]"


def _rows(query: str, params: tuple = ()) -> list[tuple]:
    if not BOARD.exists():
        return []
    con = sqlite3.connect(f"file:{BOARD}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# ── the board ──────────────────────────────────────────────────────────────────────

def check_board() -> list[Finding]:
    out: list[Finding] = []
    now = int(time.time())

    # A lease is the only thing that says a card is owned. Past its expiry the worker can
    # no longer write its own result, and only a reconciler can settle the row -- so an
    # expired lease means the card's work is unrecoverable and its admission slot is held.
    for tid, expires, pid in _rows(
        "SELECT id, claim_expires, worker_pid FROM tasks WHERE status='running' "
        "AND (claim_expires IS NULL OR claim_expires < ?)", (now - 120,)):
        age = (now - (expires or now)) / 3600
        alive = _pid_alive(pid)
        out.append(Finding(
            FATAL if not alive else BAD, "zombie_running_lease",
            f"{tid} lease expired {age:.1f}h ago, worker pid {pid} "
            f"{'alive but not renewing' if alive else 'gone'}",
            "network-platform-01"))

    # claim() is fenced on `status='ready' AND claim_lock IS NULL`; reclaim only matches
    # 'running'. A ready row still carrying a lock therefore satisfies neither and is
    # dispatched never -- the exact fingerprint the stale mirror UPDATE used to produce.
    for tid, lock in _rows(
        "SELECT id, claim_lock FROM tasks WHERE status IN ('ready','todo') "
        "AND claim_lock IS NOT NULL"):
        out.append(Finding(FATAL, "unclaimable_ready_card",
                           f"{tid} is {('ready')} but holds claim_lock={lock!r}; nothing can claim or release it",
                           "network-platform-01"))

    # Nothing reconciles status='review', so a reviewer that died between claim and
    # decision leaves the card waiting for a human forever.
    for tid, expires in _rows(
        "SELECT id, review_expires FROM tasks WHERE status='review' "
        "AND review_lock IS NOT NULL AND review_expires < ?", (now - 300,)):
        out.append(Finding(BAD, "review_lease_abandoned",
                           f"{tid} review lease expired {(now - expires) / 60:.0f}m ago; no reconciler covers 'review'",
                           "review lock leakage"))

    # Every path out of 'review' clears all four review_* columns in the same UPDATE.
    for tid, status in _rows(
        "SELECT id, status FROM tasks WHERE status <> 'review' AND review_lock IS NOT NULL"):
        out.append(Finding(BAD, "review_lock_on_non_review_row",
                           f"{tid} is {status!r} but still carries review_lock",
                           "review lock leakage"))
    return out


# ── session transcripts ────────────────────────────────────────────────────────────

def _entries(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"type": "<unparseable>"})
    return out


def check_sessions(since: float) -> list[Finding]:
    out: list[Finding] = []
    if not SESSIONS.is_dir():
        return out
    for path in SESSIONS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        entries = _entries(path)
        name = path.name[:38]

        if any(e.get("type") == "<unparseable>" for e in entries):
            out.append(Finding(FATAL, "session_file_unparseable",
                               f"{name} has a line that is not JSON; the append was torn",
                               "core-session / session_manager"))

        # Every toolCall must be answered, including on abort. An unanswered one means the
        # next request carries a dangling call, which providers reject outright.
        #
        # A call is a `toolCall` block inside an assistant message; its answer is a separate
        # message whose role is `toolResult`, linked by a message-level `toolCallId`. They do
        # not live at the same level, which is the shape to check against before trusting any
        # count here.
        calls, results, aborted = set(), set(), False
        for e in entries:
            msg = e.get("message") or {}
            if msg.get("role") == "toolResult":
                results.add(msg.get("toolCallId"))
                continue
            if msg.get("stopReason") in {"aborted", "error"}:
                aborted = True
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        calls.add(block.get("id"))
        unanswered = sorted(calls - results - {None})
        if unanswered:
            # A run the user interrupted legitimately leaves its last calls unanswered; the
            # loop only owes an answer for a turn it actually finished. Only the calls before
            # the final assistant message are a contract breach.
            severity = BAD if aborted else FATAL
            note = " (session ended aborted; may be a clean interrupt)" if aborted else ""
            out.append(Finding(severity, "tool_call_without_result",
                               f"{name}: {len(unanswered)} toolCall(s) never answered{note}",
                               "core-session (loop contract)"))
        for orphan in sorted(results - calls - {None}):
            out.append(Finding(FATAL, "orphan_tool_result",
                               f"{name} toolResult {orphan} answers no call "
                               f"(transcript corrupt, or a cut point split a turn)",
                               "core-runtime-01 / compaction"))

        # parentId chains build the context window; a break silently truncates history.
        by_id = {e.get("id") for e in entries if e.get("id")}
        for e in entries:
            parent = e.get("parentId")
            if parent and parent not in by_id:
                out.append(Finding(FATAL, "broken_parent_chain",
                                   f"{name} entry {e.get('id')} points at missing parent {parent}; "
                                   f"context before it is silently dropped",
                                   "core-session / session_manager"))
                break
    return out


# ── logs and processes ─────────────────────────────────────────────────────────────

_KNOWN_BUGS = [
    (r"Separator is not found, and chunk exceed the limit", FATAL, "stream_limit_hit",
     "cross-cutting-01"),
    (r"attached to a different loop", FATAL, "cross_loop_task", "wave2 residual"),
    (r"no such trigger: task_terminal_notification", FATAL, "drop_trigger_race",
     "network-platform-03"),
    (r"Invalid stored credential for provider", BAD, "credential_validation", "auth_storage"),
    (r"Agent process is no longer attached", BAD, "subagent_stub_adopted",
     "ext-sisters-lastorder-03"),
    (r"is ambiguous across providers", SUSPECT, "model_ambiguity", "MOD-02 (working as fixed)"),
]


def check_logs(since: float) -> list[Finding]:
    out: list[Finding] = []
    if not CRASH_LOG.exists() or CRASH_LOG.stat().st_mtime < since:
        return out
    text = CRASH_LOG.read_text(errors="replace")
    tail = text[-200_000:]
    for pattern, severity, name, audit in _KNOWN_BUGS:
        if re.search(pattern, tail):
            out.append(Finding(severity, name, f"panel-crash.log matches {pattern!r}", audit))
    # The panel's own loop dying takes every pane with it.
    recent = [m for m in re.finditer(r"^=== (\S+ \S+) (.*)$", tail, re.MULTILINE)]
    deaths = [m for m in recent if "pane exited" in m.group(2) and "exit code 0" not in m.group(2)]
    if len(deaths) >= 3:
        out.append(Finding(FATAL, "mass_pane_death",
                           f"{len(deaths)} panes exited non-zero; last: {deaths[-1].group(2)[:60]}",
                           "ui-panel-01 / daemon"))
    elif deaths:
        out.append(Finding(BAD, "pane_exited_nonzero",
                           deaths[-1].group(0)[:90], "ui-panel"))
    return out


def check_processes() -> list[Finding]:
    out: list[Finding] = []
    try:
        ps = subprocess.run(["ps", "-eo", "pid,ppid,etime,command"],
                            capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    orphans = [l for l in ps.splitlines()
               if "misaka" in l and re.search(r"^\s*\d+\s+1\s", l) and "watch_run" not in l]
    for line in orphans:
        out.append(Finding(BAD, "orphaned_misaka_process",
                           f"reparented to init: {line.strip()[:90]}",
                           "ext-sisters (start_new_session) / ui-panel"))
    return out


# ── driver ─────────────────────────────────────────────────────────────────────────

def sweep(since: float) -> list[Finding]:
    found = check_board() + check_sessions(since) + check_logs(since) + check_processes()
    return sorted(found, key=lambda f: (_ORDER[f.severity], f.name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one pass, exit 1 if anything fired")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--since-now", action="store_true",
                        help="ignore findings that are already present when the watch starts")
    args = parser.parse_args()

    since = time.time() if args.since_now else 0.0
    baseline: set[tuple] = set()
    if args.since_now:
        baseline = {f.key() for f in sweep(0.0)}
        print(f"baseline: {len(baseline)} pre-existing finding(s) suppressed")

    if args.once:
        fresh = [f for f in sweep(since) if f.key() not in baseline]
        for f in fresh:
            print(f)
        print(f"\n{len(fresh)} finding(s)" if fresh else "\nnothing observable went wrong")
        return 1 if fresh else 0

    print(f"watching {HOME} every {args.interval}s -- Ctrl-C to stop")
    seen = set(baseline)
    try:
        while True:
            for f in sweep(since):
                if f.key() not in seen:
                    seen.add(f.key())
                    print(f"{time.strftime('%H:%M:%S')}\n{f}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped; {len(seen) - len(baseline)} new finding(s) this session")
    return 0


if __name__ == "__main__":
    sys.exit(main())
