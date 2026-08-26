"""Evidence-backed findings produced by research tasks.

Sisters record findings while doing the work. This module only checks that each
source path is a registered artifact and that each quote appears verbatim in it,
then persists the result; it never scores credibility or merges similar claims.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from misaka.research import runs


CLAIM_TYPES = {"fact", "inference", "interpretation", "normative"}
MAX_FINDINGS = 32
MAX_TEXT = 4000
MAX_QUOTE = 2000


def _norm(value):
    return "".join(str(value or "").split())


def _id(run_id, task_id, text):
    key = f"{run_id}\0{task_id}\0{_norm(text).casefold()}".encode("utf-8")
    return "f_" + hashlib.sha256(key).hexdigest()[:16]


def findings(con, run_id, *, branch_id=None, task_id=None, limit=80):
    q, args = "SELECT * FROM research_findings WHERE run_id=?", [run_id]
    if branch_id is not None:
        q += " AND branch_id=?"
        args.append(branch_id)
    if task_id is not None:
        q += " AND task_id=?"
        args.append(task_id)
    return con.execute(q + " ORDER BY created_at,id LIMIT ?", [*args, int(limit)]).fetchall()


def claims(con, finding_id):
    return con.execute(
        "SELECT * FROM research_claims WHERE finding_id=? ORDER BY created_at,id",
        (finding_id,),
    ).fetchall()


def _artifact_map(con, run_id, task_id):
    out = {}
    for row in runs.artifacts(con, run_id):
        if row["task_id"] != task_id or row["kind"] != "task_output":
            continue
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        source_file = metadata.get("source_file")
        if isinstance(source_file, str):
            out[source_file] = row
    return out


def ingest_report(con, run, task, report):
    """Persist the valid findings from a task report and return a short summary.

    A missing ``findings`` key is accepted for old or resumed tasks. Invalid entries
    are dropped with a reason rather than retried: every check here is
    deterministic, so a retry would fail the same way.
    """
    raw = report.get("findings", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return {"findings": 0, "claims": 0, "dropped": ["findings must be an array"]}
    artifacts = _artifact_map(con, run["id"], task["id"])
    link = con.execute(
        "SELECT branch_id FROM research_run_tasks WHERE run_id=? AND task_id=?",
        (run["id"], task["id"]),
    ).fetchone()
    branch_id = link["branch_id"] if link else None
    made, made_claims, dropped, artifact_texts = 0, 0, [], {}
    for index, item in enumerate(raw[:MAX_FINDINGS]):
        if not isinstance(item, dict):
            dropped.append(f"finding[{index}] is not an object")
            continue
        text = str(item.get("text") or "").strip()
        source_file = item.get("source_file")
        quote = str(item.get("quote") or "").strip()
        claim_type = str(item.get("claim_type") or "fact").strip()
        artifact = artifacts.get(source_file) if isinstance(source_file, str) else None
        if len(text) < 8 or len(text) > MAX_TEXT:
            dropped.append(f"finding[{index}] has an invalid text length")
            continue
        if claim_type not in CLAIM_TYPES:
            dropped.append(f"finding[{index}] has an invalid claim_type")
            continue
        if artifact is None:
            dropped.append(f"finding[{index}] source_file is not a registered task artifact")
            continue
        if not quote or len(quote) > MAX_QUOTE:
            dropped.append(f"finding[{index}] has an invalid quotation")
            continue
        if artifact["id"] not in artifact_texts:
            try:
                artifact_texts[artifact["id"]] = runs.artifact_text(artifact)
            except (OSError, ValueError):
                artifact_texts[artifact["id"]] = None
        artifact_text = artifact_texts[artifact["id"]]
        if artifact_text is None:
            dropped.append(f"finding[{index}] source artifact is not readable")
            continue
        if _norm(quote) not in _norm(artifact_text):
            dropped.append(f"finding[{index}] quotation was not found in the source artifact")
            continue
        fid = _id(run["id"], task["id"], text)
        before = con.total_changes
        con.execute(
            "INSERT OR IGNORE INTO research_findings "
            "(id,run_id,branch_id,task_id,text,claim_type,created_at) VALUES (?,?,?,?,?,?,strftime('%s','now'))",
            (fid, run["id"], branch_id, task["id"], text, claim_type),
        )
        made += int(con.total_changes > before)
        before = con.total_changes
        con.execute(
            "INSERT OR IGNORE INTO research_claims "
            "(finding_id,artifact_id,source_file,quote,evidence_sha,created_at) "
            "VALUES (?,?,?,?,?,strftime('%s','now'))",
            (fid, artifact["id"], source_file, quote, artifact["sha256"]),
        )
        made_claims += int(con.total_changes > before)
    if len(raw) > MAX_FINDINGS:
        dropped.append(f"more than {MAX_FINDINGS} findings submitted; the remainder were ignored")
    return {"findings": made, "claims": made_claims, "dropped": dropped}
