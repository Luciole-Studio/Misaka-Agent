"""What a research run spent: tokens and money, by the conversations that spent them.

2026-10-07 (GitHub issue #9): a run's spending could not be seen anywhere. The board's ledger
counts tokens against the cap (``budget_usage`` rows, a total each); what a call cost is kept
where Pi keeps it, on each assistant message's ``usage.cost`` in the session file (and on the
tool results, summaries and ``usage`` entries that carry one, as Pi's footer counts them). A run's
conversations are its root Last Order's (her window, from the run's start), every other folder
the run opened in the session store (each node's Last Order, a command-line root), and each of
its cards' session folders, every attempt and subagent included.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FIELDS = ("calls", "input", "output", "cacheRead", "cacheWrite", "tokens", "cost", "unpriced")


def _blank():
    return dict.fromkeys(FIELDS, 0)


def _add(into, usage):
    into["calls"] += 1
    for key in ("input", "output", "cacheRead", "cacheWrite"):
        into[key] += int(usage.get(key) or 0)
    into["tokens"] += int(usage.get("totalTokens") or 0)
    cost = (usage.get("cost") or {}).get("total") if isinstance(usage.get("cost"), dict) else None
    if cost:
        into["cost"] += float(cost)
    elif usage.get("totalTokens"):
        into["unpriced"] += 1          # a model with no price in the catalogue costs nothing here


def _entry_ms(entry, message):
    """When an entry was written, in ms: a message's own timestamp, else the entry's ISO one."""
    stamp = message.get("timestamp") if isinstance(message, dict) else None
    if isinstance(stamp, (int, float)):
        return stamp
    written = entry.get("timestamp")
    if isinstance(written, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(written).timestamp() * 1000
        except ValueError:
            return None
    return None


def _spent(entry):
    """``(usage, provider/model label)`` of one session entry, the entry kinds pi's footer counts
    (core/usage_totals.getUsageCostBreakdown): assistant messages, tool results and summaries that
    carry usage, and ``usage`` entries (a cache warm-up). ``(None, None)`` for the rest."""
    kind = entry.get("type")
    message = entry.get("message") if kind == "message" else None
    if isinstance(message, dict):
        usage = message.get("usage")
        if message.get("role") == "assistant":
            return usage, f"{message.get('provider') or '?'}/{message.get('model') or '?'}"
        if message.get("role") == "toolResult":
            return usage, "Tools/summaries"
        return None, None
    if kind == "usage":
        return entry.get("usage"), f"{entry.get('provider') or '?'}/{entry.get('model') or '?'}"
    if kind in ("branch_summary", "compaction"):
        return entry.get("usage"), "Tools/summaries"
    return None, None


def session_usage(paths, *, since_ms=None, until_ms=None, by_model=None):
    """The usage these session files record, within the time window given."""
    total = _blank()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                lines = list(f)
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            usage, label = _spent(entry)
            if not isinstance(usage, dict):
                continue
            stamp = _entry_ms(entry, entry.get("message"))
            if isinstance(stamp, (int, float)) and (since_ms and stamp < since_ms or until_ms and stamp > until_ms):
                continue
            _add(total, usage)
            if by_model is not None:
                _add(by_model.setdefault(label, _blank()), usage)
    return total


def _sessions(folder):
    return sorted(str(path) for path in Path(folder).rglob("*.jsonl")) if folder and os.path.isdir(folder) else []


def run_usage(con, run):
    """``{"parts": [(label, usage)], "total": usage, "models": {provider/model: usage}}`` for one run."""
    from misaka.config import sessions
    from misaka.core.research import runs

    models, parts = {}, []
    since = int(run["created_at"]) * 1000
    root = run["root_session"]
    # The window goes on after its run: once the run has ended (done, stopped, failed) count it up
    # to the run's last change, and never past the next run started from the same window. 0.18.7
    # bounded a done run only, so a stopped run was charged for every later run in its window. A
    # run still going (waiting for the user, stopping, or resumed) is counted to now; one resumed
    # in a window that started another run meanwhile counts that run's turns there too.
    until = None
    if run["status"] not in runs.ACTIVE:
        until = int(run["updated_at"]) * 1000 + 60_000
        later = root and con.execute("SELECT MIN(created_at) FROM research_runs WHERE root_session=? AND created_at>?",
                                     (root, run["created_at"])).fetchone()[0]
        if later:
            until = min(until, int(later) * 1000)
    research = Path(sessions.sessions_root()) / "research"
    folders = sorted(research.glob(f"{run['id']}--*")) if research.is_dir() else []
    window = root and not any(Path(root).is_relative_to(folder) for folder in folders)
    if window:
        # The root's Last Order is the user's window: only what she said once the run had begun.
        parts.append(("root Last Order", session_usage([root], since_ms=since, until_ms=until, by_model=models)))
    for folder in folders:
        scope = folder.name.split("--", 1)[1]
        label = "root Last Order" if scope == "root-lo" else scope.replace("-", " ", 1)
        parts.append((label, session_usage(_sessions(folder), by_model=models)))
    for task in runs.tasks(con, run["id"]):
        files = _sessions(task["session_dir"]) or [path for path in [task["session_file"]] if path]
        parts.append((f"card {task['id']} (Sister {task['assignee']})", session_usage(files, by_model=models)))
    total = _blank()
    for _label, usage in parts:
        for key in FIELDS:
            total[key] += usage[key]
    return {"parts": [(label, usage) for label, usage in parts if usage["calls"]], "total": total, "models": models}


def money(usage):
    text = f"${usage['cost']:,.2f}"
    return text + (f" (+{usage['unpriced']} calls with no known price)" if usage["unpriced"] else "")


def report(con, run, *, ledger_tokens=None):
    """``misaka usage --run``: a run's spending, by conversation and by model."""
    spent = run_usage(con, run)
    width = max([len(label) for label, _usage in spent["parts"]] + [len("total"), 20])
    row = lambda label, usage: (f"  {label:<{width}}  {usage['calls']:>5} calls  {usage['tokens']:>12,} tokens  "
                                f"{money(usage)}")
    lines = [f"Research run {run['id']} ({run['status']}): {' '.join(run['question'].split())[:80]}"]
    lines += [row(label, usage) for label, usage in spent["parts"]]
    lines.append(row("total", spent["total"]))
    if spent["models"]:
        lines.append("By model:")
        lines += [row(model, usage) for model, usage in sorted(spent["models"].items(),
                                                                key=lambda item: -item[1]["cost"])]
    if ledger_tokens is not None:
        lines.append(f"Charged to the token cap (research.token_cap): {ledger_tokens:,} tokens.")
    return "\n".join(lines)
