"""A record of researchers' declarations, not a verdict on their material.

Sources and quotations are optional locators. Artifact digests identify saved files;
they do not certify a claim. Corrections remain alongside earlier declarations so
Last Order and the red team can inspect the history themselves.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from misaka.core.research import runs


class Finding(BaseModel):
    text: str = Field(min_length=1)
    claim_type: Literal["fact", "inference", "interpretation", "normative"] = "fact"
    quote: str = ""
    source_file: str | None = Field(None, description="Source path or locator, if applicable.")
    doc_id: str | None = None
    page: int | None = Field(None, ge=1, strict=True)


def findings(con, run_id, *, branch_id=None, task_id=None, limit=None, offset=0):
    q, args = "SELECT * FROM research_findings WHERE run_id=?", [run_id]
    for column, value in (("branch_id", branch_id), ("task_id", task_id)):
        if value is not None:
            q += f" AND {column}=?"
            args.append(value)
    return con.execute(q + " ORDER BY created_at,rowid LIMIT ? OFFSET ?",
                       [*args, -1 if limit is None else int(limit), int(offset)]).fetchall()


def claims(con, finding_id):
    return con.execute(
        "SELECT * FROM research_claims WHERE finding_id=? ORDER BY created_at,id", (finding_id,),
    ).fetchall()


def ingest_report(con, run, task, report):
    """Record structurally valid declarations; never inspect or grade source content."""
    # Validate the envelope before writing anything. No silent truncation or partial rejection.
    declared = [Finding.model_validate(item) for item in report.get("findings", [])]
    artifacts = {}
    for row in runs.artifacts(con, run["id"], task_id=task["id"]):
        source = json.loads(row["metadata_json"] or "{}").get("source_file")
        if source:
            artifacts[source] = row
    link = con.execute(
        "SELECT branch_id FROM research_run_tasks WHERE run_id=? AND task_id=?",
        (run["id"], task["id"]),
    ).fetchone()
    made, made_claims = 0, 0
    for item in declared:
        record = json.dumps([run["id"], task["id"], item.model_dump()], ensure_ascii=False, sort_keys=True)
        fid = "f_" + hashlib.sha256(record.encode()).hexdigest()[:24]
        before = con.total_changes
        con.execute(
            "INSERT OR IGNORE INTO research_findings "
            "(id,run_id,branch_id,task_id,text,claim_type,created_at) VALUES (?,?,?,?,?,?,strftime('%s','now'))",
            (fid, run["id"], link["branch_id"] if link else None, task["id"], item.text, item.claim_type),
        )
        made += int(con.total_changes > before)
        sources = [item.source_file] if item.source_file else []
        if item.doc_id:
            sources.append(f"doc:{item.doc_id}" + (f"#p{item.page}" if item.page is not None else ""))
        if item.quote and not sources:
            sources.append("")
        for source in dict.fromkeys(sources):
            artifact = artifacts.get(source)
            aid, sha = (artifact["id"], artifact["sha256"]) if artifact is not None else (None, "")
            before = con.total_changes
            con.execute(
                "INSERT OR IGNORE INTO research_claims "
                "(finding_id,artifact_id,source_file,quote,evidence_sha,created_at) "
                "SELECT ?,?,?,?,?,strftime('%s','now') WHERE NOT EXISTS ("
                "SELECT 1 FROM research_claims WHERE finding_id=? AND artifact_id IS ? "
                "AND source_file=? AND quote=?)",
                (fid, aid, source, item.quote, sha, fid, aid, source, item.quote),
            )
            made_claims += int(con.total_changes > before)
    return {"findings": made, "claims": made_claims}
