"""Read-only view of local runtime trace segments."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing

from misaka.config import home
from misaka.core.runtime_trace import (
    BUDGET_TOKEN_FIELDS,
    read_trace,
    trace_children,
    trace_folders,
    trace_row,
    within_trace_dir,
)

_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")


def parser_options(parser):
    commands = parser.add_subparsers(dest="op", required=True)
    show = commands.add_parser("show", help="Show one recorded run or session")
    show.add_argument("run_id")
    show.add_argument("--json", action="store_true")
    commands.add_parser("last", help="Show the most recently updated trace")


def _folder_matches_trace(folder, run_id):
    names = trace_folders(run_id)
    if folder.name in names:
        return True
    if os.name == "nt":
        for name in names:
            if folder.name.lower() == name.lower():
                try:
                    return folder.samefile(folder.with_name(name))
                except OSError:
                    return False
    return False


def _last_id():
    root = home.path("runtime_traces")
    folders, incomplete = trace_children(root)
    candidates = []
    for folder in folders:
        if not within_trace_dir(folder, root):
            incomplete = True
            continue
        try:
            if not folder.is_dir():
                continue
        except OSError:
            incomplete = True
            continue
        children, scan_incomplete = trace_children(folder)
        incomplete |= scan_incomplete
        for segment in children:
            if not segment.match("*.jsonl"):
                continue
            if not within_trace_dir(segment, folder):
                incomplete = True
                continue
            try:
                candidates.append((segment.stat().st_mtime_ns, segment))
            except OSError:
                incomplete = True
    for _, segment in sorted(candidates, reverse=True):
        last = None
        matches = {}
        try:
            with segment.open("rb") as stream:
                for line in stream:
                    row = trace_row(line)
                    if row is None:
                        incomplete = True
                        continue
                    run_id = row["run_id"]
                    if run_id not in matches:
                        matches[run_id] = _folder_matches_trace(segment.parent, run_id)
                    if matches[run_id]:
                        last = run_id
                    else:
                        incomplete = True
        except OSError:
            incomplete = True
        if last is not None:
            return last, incomplete
        incomplete = True
    return None, incomplete


def _operations(events):
    calls = {}
    for row in events:
        key = row.get("execution_id"), row.get("request_id"), row.get("call_id")
        if key[2] is None:
            continue
        match row["event"]:
            case "tool_entered" | "tool_rejected":
                # Old segments could print tool_rejected a second time after a nested result.
                calls.setdefault(key, {"execution_id": key[0], "call_id": key[2], "tool": row.get("tool"),
                                       "state": "unknown" if row["event"] == "tool_entered" else "not_executed",
                                       "request_id": key[1], "nested": bool(row.get("nested"))})
            case "tool_returned" | "tool_failed":
                item = calls.setdefault(key, {"execution_id": key[0], "call_id": key[2], "tool": row.get("tool"),
                                              "request_id": key[1], "nested": bool(row.get("nested"))})
                item.update(state="returned", status=row.get("status"))
            case "tool_receipt_saved":
                if key in calls:
                    if calls[key]["state"] != "not_executed":
                        calls[key]["state"] = "receipt_saved"
                    calls[key]["entry_id"] = row.get("entry_id")
                    calls[key]["receipt_status"] = row.get("status")
    return list(calls.values())


def _usage(events):
    requests = {}
    for row in events:
        request_id = row.get("request_id")
        if not request_id:
            continue
        if row["event"] == "model_started":
            requests.setdefault(request_id, {})
        elif row["event"] in {"model_finished", "provider_usage", "budget_refused", "circuit_rejected"}:
            requests.setdefault(request_id, {})[row["event"]] = row
        elif row["event"] == "http_observable":
            requests.setdefault(request_id, {}).setdefault("observed_http", 0)
        elif row["event"] == "http_attempt":
            facts = requests.setdefault(request_id, {})
            facts["observed_http"] = facts.get("observed_http", 0) + 1
    totals = {name: 0 for name in ("input", "output", "cache_read", "cache_write", "total")}
    reported = missing = partial = conflicting = refused = circuit_refused = 0
    http_attempts = observed_http = without_raw = unfinished_http = 0
    for facts in requests.values():
        end = facts.get("model_finished", {})
        if isinstance(end.get("http_attempts"), int):
            observed_http += 1
            http_attempts += end["http_attempts"]
        elif "observed_http" in facts:
            observed_http += 1
            unfinished_http += 1
            http_attempts += facts["observed_http"]
        if facts.get("budget_refused"):
            refused += 1
            continue
        if facts.get("circuit_rejected"):
            circuit_refused += 1
            continue
        source = facts.get("provider_usage")
        if source is None:
            without_raw += 1
            usage = end.get("usage")
        elif source.get("status") == "reported":
            if source.get("issues"):  # Version 1 initially labeled conflicts as reported.
                conflicting += 1
                continue
            usage = source.get("normalized")
        elif source.get("status") == "conflicting":
            conflicting += 1
            continue
        else:
            usage = None
            if source.get("status") == "partial":
                partial += 1
                continue
        if not isinstance(usage, dict):
            missing += 1
            continue
        values = [usage.get(name) for name in totals]
        if any(type(value) is not int or value < 0 for value in values) or sum(values[:-1]) != values[-1]:
            conflicting += 1
            continue
        reported += 1
        for name in totals:
            totals[name] += usage.get(name, 0)
    return {"requests": len(requests), "http_attempts": http_attempts if observed_http else None,
            "requests_with_http_counts": observed_http, "reported": reported, "partial": partial, "missing": missing,
            "conflicting": conflicting, "circuit_refused": circuit_refused,
            "unfinished_http_counts": unfinished_http,
            "without_provider_usage_details": without_raw, "budget_refused": refused,
            "reported_tokens": totals if reported else None}


def _board_evidence(con, run_id):
    from misaka.core.platform.tasks import is_retry_stop

    submissions = {}
    current_links = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_cards'",
    ).fetchone()
    link_table = "research_cards" if current_links else "research_run_tasks"
    rows = con.execute(
        "SELECT e.id,e.kind,e.task_id,e.generation,e.created_at,e.payload "
        f"FROM {link_table} AS card JOIN events AS e ON e.task_id=card.task_id "
        "WHERE card.run_id=? AND e.kind IN ('submitted','research_v2_settled','gave_up') ORDER BY e.id",
        (run_id,),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            yield None
            payload = {}
        event = {"event": row["kind"], "board_event_id": row["id"], "card_id": row["task_id"],
                 "generation": row["generation"], "created_at": row["created_at"]}
        if row["kind"] == "submitted":
            digests = payload.get("artifact_digests")
            event["artifact_hashes"] = sorted(
                value.lower() for value in digests.values() if isinstance(value, str) and _SHA256.fullmatch(value)
            ) if isinstance(digests, dict) else []
            submissions[row["id"]] = event
        elif row["kind"] == "research_v2_settled":
            submitted_id = payload.get("submitted_event")
            source = submissions.get(submitted_id) if type(submitted_id) is int else None
            if source and (source["card_id"], source["generation"]) != (event["card_id"], event["generation"]):
                source = None
            event["submission_event_id"] = source["board_event_id"] if source else None
            event["artifact_hashes"] = source["artifact_hashes"] if source else []
        else:
            reason = payload.get("reason")
            kind = payload.get("failure_kind")
            if reason == "consecutive_rate_limits":
                event["reason"] = reason
            elif is_retry_stop(kind):
                event["reason"] = kind
            else:
                continue
            for name in ("failures", "limit"):
                value = payload.get(name)
                if type(value) is int and value >= 0:
                    event[name] = value
        yield event


def _read_board(run_id):
    if not run_id.startswith("r_"):
        return None, [], False
    path = home.path("db")
    try:
        path.stat()
    except FileNotFoundError:
        return None, [], False
    except OSError:
        return None, [], True
    from misaka.core.research.usage import research_budget_summary

    evidence = []
    incomplete = False
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN")
            if con.execute("SELECT 1 FROM research_runs WHERE id=?", (run_id,)).fetchone() is None:
                return None, [], False
            for event in _board_evidence(con, run_id):
                if event is None:
                    incomplete = True
                else:
                    evidence.append(event)
            return research_budget_summary(con, run_id), evidence, incomplete
    except (OSError, sqlite3.Error):
        # Older Boards may still have submissions when budget tables cannot be read.
        return None, evidence, True


def report(run_id):
    events, incomplete = read_trace(run_id)
    budget, board_events, board_incomplete = _read_board(run_id)
    return {"run_id": run_id, "recording": "incomplete" if incomplete else "present" if events else "unavailable",
            "events": events, "operations": _operations(events), "usage": _usage(events), "budget": budget,
            "board_events": board_events, "board_incomplete": board_incomplete}


def run(argv):
    parser = argparse.ArgumentParser(prog="misaka trace", description=__doc__)
    parser_options(parser)
    args = parser.parse_args(argv)
    if args.op == "last":
        run_id, incomplete = _last_id()
        if incomplete:
            print("Runtime trace inventory incomplete.")
    else:
        run_id, incomplete = args.run_id, False
    if run_id is None:
        print("No readable runtime trace records found." if incomplete else "No runtime trace records found.")
        return 0
    try:
        data = report(run_id)
    except ValueError as error:
        parser.error(str(error))
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=True))
        return 0
    print(f"Runtime trace {run_id} ({data['recording']})")
    if data["recording"] == "unavailable":
        print("No runtime-event records for this ID. Older sessions may have only conversation history or Board records.")
    else:
        print(json.dumps(data["usage"], ensure_ascii=True))
    if data["board_incomplete"]:
        print("Research Board evidence incomplete.")
    if data["budget"] is not None:
        print("Research budget", json.dumps(data["budget"], ensure_ascii=True))
    if data["board_events"]:
        print("Research Board evidence")
        for event in data["board_events"]:
            print(" ", json.dumps(event, ensure_ascii=True))
    for event in data["events"]:
        fields = ("tool", "status", "reason", "status_code", "attempt", "retry_number", "duration_ms", "delay_ms",
                  "retry_source", "failure_count", "issues", "call_id", "request_id", "card_id", "entry_id",
                  "budget_scope", *BUDGET_TOKEN_FIELDS, "budget_leased")
        details = " ".join(f"{key}={event[key]}" for key in fields if event.get(key) is not None)
        print(f"  {event['at_ns']} {event['event']} {details}".rstrip())
    for operation in data["operations"]:
        if operation["state"] == "unknown" or (operation["state"] == "returned" and not operation.get("nested")):
            print(f"  {operation['state']}: {operation['tool']} {operation['call_id']}")
    return 0
