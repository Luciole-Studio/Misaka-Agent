"""What a research run spent: tokens and money, by the conversations that spent them.

2026-10-07 (GitHub issue #9): a run's spending could not be seen anywhere. The board's ledger
counts tokens against the cap (``budget_usage`` rows, a total each); what a call cost is kept
where Pi keeps it, on each assistant message's ``usage.cost`` in the session file (and on the
tool results, summaries and ``usage`` entries that carry one, as Pi's footer counts them). A run's
conversations are its root Last Order's windows, every other folder
the run opened in the session store (each node's Last Order, a command-line root), and each of
its cards' session folders, every attempt and subagent included.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from misaka.core.subagent.fork import FORK_SEED_END, in_fork_child

_TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite")
FIELDS = ("calls", *_TOKEN_FIELDS, "tokens", "cost", "unpriced")
_UNTAGGED = object()
_REQUEST_EVENTS = frozenset({"model_started", "model_finished", "model_receipt_saved"})


def _blank():
    return dict.fromkeys(FIELDS, 0)


def _recorded_usage(usage):
    tokens = usage.get("totalTokens")
    cost = usage.get("cost")
    price = cost.get("total") if isinstance(cost, dict) else None
    amount = float(price or 0)
    if not math.isfinite(amount):
        raise ValueError("Invalid recorded cost")
    return (1, *(int(usage.get(key) or 0) for key in _TOKEN_FIELDS), int(tokens or 0), amount,
            int(bool(tokens) and not price))


def _add(into, usage):
    for key, amount in zip(FIELDS, usage):
        into[key] += amount


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


def _path_key(path):
    return os.path.normcase(os.path.realpath(path))


@dataclass
class _UsageFile:
    complete: bool = False
    parent: str | None = None
    session_id: str | None = None
    entries: list[tuple] = field(default_factory=list)
    entry_ids: set[str] = field(default_factory=set)
    tool_calls: dict[str, list[int]] = field(default_factory=dict)
    seed_end: int | None = None
    first_fork_prompt: int | None = None


def _read_usage_file(path):
    source = _UsageFile()
    complete, saw_entry = True, False
    try:
        with open(path, "rb") as file:
            for position, line in enumerate(file):
                try:
                    entry = json.loads(line.decode("utf-8"))
                except (ValueError, RecursionError):
                    if not line.strip():
                        continue
                    entry = None
                if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
                    complete = False
                    continue
                saw_entry = True
                if entry.get("type") == "session":
                    if isinstance(entry.get("parentSession"), str):
                        source.parent = _path_key(entry["parentSession"])
                    if isinstance(entry.get("id"), str):
                        source.session_id = entry["id"]
                    continue
                if (source.seed_end is None and entry.get("type") == "custom"
                        and entry.get("customType") == FORK_SEED_END):
                    source.seed_end = position
                message = entry.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if message.get("role") == "assistant" and isinstance(content, list):
                        for block in content:
                            if (isinstance(block, dict) and block.get("type") == "toolCall"
                                    and isinstance(block.get("id"), str)):
                                source.tool_calls.setdefault(block["id"], []).append(position)
                    elif (source.first_fork_prompt is None and message.get("role") == "user"
                          and isinstance(content, (str, list)) and in_fork_child([message])):
                        source.first_fork_prompt = position
                usage, label = _spent(entry)
                if not isinstance(usage, dict):
                    continue
                try:
                    usage = _recorded_usage(usage)
                except (ValueError, TypeError, OverflowError):
                    complete = False
                    continue
                request_id = message.get("requestId") if isinstance(message, dict) else None
                entry_id = entry.get("id")
                if isinstance(entry_id, str):
                    source.entry_ids.add(entry_id)
                source.entries.append((position, entry_id, request_id, _entry_ms(entry, message), usage, label,
                                       entry.get("usageRunId", _UNTAGGED)))
    except OSError:
        complete = False
    source.complete = complete and saw_entry
    return source


def _seed_boundary(path, source, parents):
    """Return the first non-seed position, and whether its provenance is confirmed."""
    if source.seed_end is not None:
        return source.seed_end, True
    transcript = Path(path)
    if not transcript.stem.startswith("agent-"):
        return 0, True
    try:
        metadata = json.loads(transcript.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = None
    if not (isinstance(metadata, dict) and metadata.get("forked") is True
            and metadata.get("agentId") == transcript.stem.removeprefix("agent-")):
        # A native fork may have written its snapshot before its main sidecar.
        if not transcript.with_suffix(".meta.fork.json").is_file():
            return 0, True
        return source.first_fork_prompt, False
    parent_id = metadata.get("parentSessionId")
    tool_id = metadata.get("toolUseId")
    parent = parents.get((str(transcript.parent), parent_id)) if isinstance(parent_id, str) else None
    calls = source.tool_calls.get(tool_id, []) if isinstance(tool_id, str) else []
    if (len(calls) == 1 and parent is not None and len(parent.tool_calls.get(tool_id, [])) == 1
            and (source.first_fork_prompt is None or calls[0] < source.first_fork_prompt)):
        # The old writer copied through that assistant call and then added placeholders without usage.
        return calls[0] + 1, True
    return source.first_fork_prompt, False


def _legacy_panel_owners(trace_ids, request_ids, trace_rows):
    """Join only older rows with request IDs to traces for this panel's runs and ordinary session."""
    from misaka.core.runtime_trace import read_trace, valid_id

    owners = {}
    if request_ids:
        for trace_id in dict.fromkeys(trace_ids):
            if not valid_id(trace_id):
                continue
            rows = trace_rows.get(trace_id)
            if rows is None:
                rows, _incomplete = read_trace(trace_id, kinds=_REQUEST_EVENTS)
            for row in rows:
                request_id = row.get("request_id")
                if request_id in request_ids and owners.setdefault(request_id, trace_id) != trace_id:
                    owners[request_id] = _UNTAGGED
    return owners


def _panel_attribution(owner, request_id, run_id, trace_owners, stamp, *, can_estimate, uncertain_periods):
    if owner is _UNTAGGED and isinstance(request_id, str):
        owner = trace_owners.get(request_id, _UNTAGGED)
    if owner is not _UNTAGGED:
        return "exact" if owner == run_id else "elsewhere"
    if not can_estimate or request_id or stamp is None:
        return "unassigned"
    second = stamp // 1000
    if any(start <= second and (end is None or second <= end) for start, end in uncertain_periods):
        return "unassigned"
    return "estimated"


def _outside_usage_window(stamp, since_ms, until_ms, before_ms):
    return stamp is not None and (since_ms is not None and stamp < since_ms
                                  or until_ms is not None and stamp > until_ms
                                  or before_ms is not None and stamp >= before_ms)


def _summarize_sessions(sources, groups, by_model, *, run_id=None, panel_paths=(), panel_owners=None,
                        ambiguous_panel=False, uncertain_periods=()):
    from misaka.config import sessions

    parents = {}
    for path, source in sources.items():
        if source.session_id:
            try:
                namespace = _path_key(sessions.subagent_session_dir(source.session_id, path))
            except ValueError:
                continue
            key = (namespace, source.session_id)
            parents[key] = source if key not in parents else None
    boundaries = {path: _seed_boundary(path, source, parents) for path, source in sources.items()}
    panel_owners = panel_owners or {}

    seen_requests = set()
    pending_usage = []
    unverified_fork_usage, estimated_panel_usage, unassigned_panel_usage = _blank(), _blank(), _blank()
    parts = []
    seen_paths = set()
    # Groups and sources share the same normalized path keys.
    for label, paths, since_ms, until_ms, before_ms in groups:
        total = _blank()
        for path in paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            source = sources[path]
            seen_entries = set()
            in_panel = path in panel_paths
            # Only consult files already included in the report. A transcript may
            # name an arbitrary path, and an uncollected parent proves no duplicate.
            parent = sources.get(source.parent) if source.parent != path else None
            seed_end, confirmed = boundaries[path]
            for position, entry_id, request_id, stamp, usage, model_label, owner in source.entries:
                in_seed = seed_end is None or position < seed_end
                if in_seed and confirmed:
                    continue
                if isinstance(entry_id, str) and parent is not None and entry_id in parent.entry_ids:
                    continue
                if run_id is not None and owner is not _UNTAGGED and owner != run_id:
                    continue
                attribution = None
                if in_panel:
                    attribution = _panel_attribution(owner, request_id, run_id, panel_owners, stamp,
                                                      can_estimate=not ambiguous_panel,
                                                      uncertain_periods=uncertain_periods)
                if attribution == "elsewhere":
                    continue
                if attribution != "exact" and _outside_usage_window(stamp, since_ms, until_ms, before_ms):
                    continue
                if isinstance(entry_id, str):
                    if entry_id in seen_entries:
                        continue
                    seen_entries.add(entry_id)
                if in_seed or attribution == "unassigned":
                    subtotal = unverified_fork_usage if in_seed else unassigned_panel_usage
                    pending_usage.append((request_id, usage, subtotal))
                    continue
                if isinstance(request_id, str) and request_id:
                    if request_id in seen_requests:
                        continue
                    seen_requests.add(request_id)
                _add(total, usage)
                if attribution == "estimated":
                    _add(estimated_panel_usage, usage)
                if by_model is not None:
                    _add(by_model.setdefault(model_label, _blank()), usage)
        parts.append((label, total))
    for request_id, usage, subtotal in pending_usage:
        if isinstance(request_id, str) and request_id:
            if request_id in seen_requests:
                continue
            seen_requests.add(request_id)
        _add(subtotal, usage)
    return parts, {"unverified_fork_usage": unverified_fork_usage, "estimated_panel_usage": estimated_panel_usage,
                   "unassigned_panel_usage": unassigned_panel_usage}


def session_usage(paths, *, since_ms=None, until_ms=None, by_model=None):
    """The usage these session files record, within the time window given."""
    sources = {path: _read_usage_file(path) for path in dict.fromkeys(map(_path_key, paths))}
    parts, _unverified = _summarize_sessions(sources, [("", sources, since_ms, until_ms, None)], by_model)
    return parts[0][1]


def _sessions(folder):
    return sorted(str(path) for path in Path(folder).rglob("*.jsonl")) if folder and os.path.isdir(folder) else []


def _window_sessions(root):
    from misaka.config import sessions
    from misaka.core.session_manager import assertValidSessionId, readSessionHeader

    session_id = readSessionHeader(root).get("id")
    if session_id is None:
        return None, [root]
    try:
        assertValidSessionId(session_id)
    except ValueError:
        return None, [root]
    return session_id, [root, *_sessions(sessions.subagent_session_dir(session_id, root))]


def _journal_windows(con, run_id):
    from misaka.config import home
    from misaka.core import graph
    from misaka.core.research import runs

    root = runs.root(con, run_id)
    entries = graph.entries(con, run_id, scope="", op="reconcile")
    if root:
        entries += graph.entries(con, run_id, scope=root["id"], op="action")
    paths = set()
    for entry in entries:
        if not isinstance(entry.proposer, Mapping):
            continue
        path = entry.proposer.get("session_file")
        if isinstance(path, str) and path and "\0" not in path:
            paths.add(_path_key(os.path.realpath(home.from_stored(path))))
    return paths


def _trace_windows(rows, known_paths):
    """Locate persisted receipts outside the Board's known window and card transcripts."""
    from misaka.core import session_catalog
    from misaka.core.session_manager import readSessionHeader

    receipts = {row["session_id"] for row in rows if row["event"] == "model_receipt_saved" and row.get("session_id")}
    if not receipts:
        return set(), False
    counts = Counter(readSessionHeader(path).get("id") for path in known_paths)
    missing = receipts.difference(counts)
    try:
        found = session_catalog.saved_session_paths(missing)
    except OSError:
        found = {}
    return set(found.values()), bool(missing.difference(found) or any(counts[sid] > 1 for sid in receipts))


_PANEL_STOP_GRACE_SECONDS = 60


def _panel_neighbors(con, run, roots, complete):
    from misaka.config import home
    from misaka.core.research import runs

    # Legacy Board rows can store either spelling and may predate Windows path normalization.
    stored = {home.stored(path) for path in roots}
    aliases = sorted(stored | {str(home.from_stored(path)) for path in stored} |
                     ({run["root_session"]} if run["root_session"] else set()))
    trace_ids, competing, legacy_cutoffs = set(), [], []
    for other, created, updated, status, periods, recorded in runs.prior_window_runs(con, run["id"], aliases):
        trace_ids.add(other)
        if recorded is None and complete is None and type(created) is int:
            # Preserve creation-order estimates for two legacy runs with no binding events.
            if created > run["created_at"]:
                legacy_cutoffs.append(created)
            elif created == run["created_at"]:
                competing.append((created, None))
            continue
        start = created if type(created) is int else 0
        end = (updated + _PANEL_STOP_GRACE_SECONDS
               if status not in runs.ACTIVE and type(updated) is int and updated >= start else None)
        # A switch can occur anywhere within its recorded second. Count that second
        # as a possible overlap for either window.
        if recorded:
            competing.extend((start, left if left is not None else end) for start, left in periods)
        else:
            competing.append((start, end))
    return trace_ids, competing, min(legacy_cutoffs) * 1000 if legacy_cutoffs else None


def run_usage(con, run):
    """Return one run's session usage with ambiguous fork history kept out of its totals."""
    from misaka.config import sessions
    from misaka.core.research import runs

    run_id = run["id"]
    since = int(run["created_at"]) * 1000
    until = (int(run["updated_at"]) + _PANEL_STOP_GRACE_SECONDS) * 1000 if run["status"] not in runs.ACTIVE else None
    research = Path(sessions.sessions_root()) / "research"
    folders = sorted(research.glob(f"{run_id}--*")) if research.is_dir() else []
    bindings, complete = runs.root_session_history(con, run_id)
    roots = set(bindings)
    trace_rows = {}
    if not complete:
        from misaka.core.runtime_trace import read_trace

        roots.update(_journal_windows(con, run_id))
        trace_rows[run_id], _incomplete = read_trace(run_id, kinds=_REQUEST_EVENTS)

    groups = []
    panel_roots = set(roots)
    for folder in folders:
        scope = folder.name.split("--", 1)[1]
        label = "root Last Order" if scope == "root-lo" else scope.replace("-", " ", 1)
        folder_key = Path(_path_key(folder))
        bound = {path for path in panel_roots if Path(path).is_relative_to(folder_key)}
        panel_roots.difference_update(bound)
        files = [*map(_path_key, _sessions(folder)), *sorted(bound)]
        groups.append((label, files, None, None, None))
    for task in runs.tasks(con, run_id):
        files = _sessions(sessions.card_session_dir(task)) or [path for path in [task["session_file"]] if path]
        groups.append((f"card {task['id']} (Sister {task['assignee']})", list(map(_path_key, files)), None, None, None))
    group_paths = [path for _, paths, *_ in groups for path in paths]

    panel_paths, panel_trace_ids = set(), {run_id}

    def add_window(root):
        session_id, paths = _window_sessions(root)
        panel_paths.update(map(_path_key, paths))
        if session_id:
            panel_trace_ids.add(session_id)

    for root in panel_roots:
        add_window(root)

    unresolved = False
    if not complete:
        known_paths = panel_paths.union(group_paths)
        found, unresolved = _trace_windows(trace_rows[run_id], known_paths)
        roots.update(found)
        panel_roots.update(found)
        for root in found:
            add_window(root)

    panel_files = sorted(panel_paths)
    paths = dict.fromkeys([*panel_files, *group_paths])
    sources = {path: _read_usage_file(path) for path in paths}
    before = None
    panel_owners = {}
    ambiguous_panel = len(roots) > 1 or unresolved or complete is False
    uncertain_periods = ()
    legacy_requests = {
        request_id if isinstance(request_id, str) and request_id else None
        for path in panel_paths
        for _, _, request_id, _, _, _, owner in sources[path].entries
        if owner is _UNTAGGED
    }
    if legacy_requests:
        neighbors, uncertain_periods, cutoff = _panel_neighbors(con, run, panel_roots, complete)
        panel_trace_ids.update(neighbors)
        if not ambiguous_panel:
            before = cutoff
            if complete:
                root = next(iter(panel_roots))
                first_second, _ = bindings[root][0]
                since = first_second * 1000
                # The first binding can occur after an unrelated request in the same second.
                uncertain_periods.append((first_second, first_second))
        legacy_requests.discard(None)
        panel_owners = _legacy_panel_owners(sorted(panel_trace_ids), legacy_requests, trace_rows)
    if panel_files:
        groups.insert(0, ("root Last Order", panel_files, since, until, before))

    models = {}
    parts, provenance = _summarize_sessions(
        sources, groups, models, run_id=run_id, panel_paths=panel_paths, panel_owners=panel_owners,
        ambiguous_panel=ambiguous_panel, uncertain_periods=uncertain_periods,
    )
    provenance["window_history_incomplete"] = not complete or any(not source.complete for source in sources.values())
    merged_parts, total = {}, _blank()
    for label, usage in parts:
        # Preserve the original cost accumulation order independently of display grouping.
        for key in FIELDS:
            total[key] += usage[key]
        if not usage["calls"]:
            continue
        target = merged_parts.setdefault(label, _blank())
        for key in FIELDS:
            target[key] += usage[key]
    return {"parts": list(merged_parts.items()), "total": total, "models": models,
            **provenance}


def research_budget_summary(con, run_id):
    """Read the Research budget without settling reservations or changing board state."""
    serialized = getattr(con, "serialized", None)
    with serialized() if serialized else nullcontext():
        owns_snapshot = not con.in_transaction
        if owns_snapshot:
            con.execute("BEGIN")
        try:
            current_links = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_cards'"
            ).fetchone()
            link_table = "research_cards" if current_links else "research_run_tasks"
            task_ids = (run_id, *(row[0] for row in con.execute(
                f"SELECT task_id FROM {link_table} WHERE run_id=?", (run_id,)
            )))
            return _budget_snapshot(con, task_ids)
        finally:
            if owns_snapshot:
                con.rollback()


def _budget_snapshot(con, task_ids):
    from misaka.core.platform import budget

    slots = ",".join("?" * len(task_ids))
    reserved_tokens = con.execute(
        f"SELECT COALESCE(SUM(tokens),0) FROM budget_reservations WHERE expires_at>=? AND task_id IN ({slots})",
        (int(time.time()), *task_ids),
    ).fetchone()[0]
    expired_tokens = 0
    for (payload,) in con.execute(
        f"SELECT payload FROM events WHERE kind='budget_usage' AND task_id IN ({slots})", task_ids
    ):
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("expiredReservation") and type(event.get("totalTokens")) is int:
            expired_tokens += event["totalTokens"]
    return {
        "recorded_tokens": budget.spent(con, task_ids=task_ids),
        "reserved_tokens": reserved_tokens,
        "expired_reservation_tokens": expired_tokens,
    }


def money(usage):
    text = f"${usage['cost']:,.2f}"
    return text + (f" (+{usage['unpriced']} calls with no known price)" if usage["unpriced"] else "")


def panel_usage_note(spent):
    notes = []
    if spent["estimated_panel_usage"]["calls"]:
        notes.append("includes panel estimate")
    if spent["unassigned_panel_usage"]["calls"]:
        notes.append("excludes unassigned panel usage")
    if spent["window_history_incomplete"]:
        notes.append("window history incomplete")
    return f" ({', '.join(notes)})" if notes else ""


def report(con, run, *, ledger_tokens=None, board_budget=None):
    """``misaka usage --run``: a run's spending, by conversation and by model."""
    spent = run_usage(con, run)
    width = max([len(label) for label, _usage in spent["parts"]] + [len("total"), 20])
    row = lambda label, usage: (f"  {label:<{width}}  {usage['calls']:>5} calls  {usage['tokens']:>12,} tokens  "
                                f"{money(usage)}")
    lines = [f"Research run {run['id']} ({run['status']}): {' '.join(run['question'].split())[:80]}"]
    lines.append(f"Totals use normalized session records. For validated provider usage: misaka trace show {run['id']}.")
    lines += [row(label, usage) for label, usage in spent["parts"]]
    lines.append(row("total", spent["total"]))
    if spent["unverified_fork_usage"]["calls"]:
        lines.append(row("Unverified fork history", spent["unverified_fork_usage"]))
    if spent["estimated_panel_usage"]["calls"]:
        lines.append(row("Estimated panel usage (included)", spent["estimated_panel_usage"]))
    if spent["unassigned_panel_usage"]["calls"]:
        lines.append(row("Unassigned panel usage (outside total)", spent["unassigned_panel_usage"]))
    if spent["window_history_incomplete"]:
        lines.append("Window history incomplete: binding or transcript evidence is missing or incomplete.")
    if spent["models"]:
        lines.append("By model:")
        lines += [row(model, usage) for model, usage in sorted(spent["models"].items(),
                                                                key=lambda item: -item[1]["cost"])]
    if board_budget is not None:
        ledger_tokens = board_budget["recorded_tokens"]
    if ledger_tokens is not None:
        lines.append(f"Charged to the token cap (research.token_cap): {ledger_tokens:,} tokens.")
    if board_budget is not None:
        lines.append(f"Active reservations: {board_budget['reserved_tokens']:,} tokens.")
        lines.append(f"Expired reservations already included in the charged total: "
                     f"{board_budget['expired_reservation_tokens']:,} tokens.")
    return "\n".join(lines)
