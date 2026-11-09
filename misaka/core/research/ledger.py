"""A record of researchers' declarations, not a verdict on their material.

Sources and quotations are optional locators. Artifact digests identify saved files;
they do not certify a claim. Corrections remain alongside earlier declarations so
Last Order and the red team can inspect the history themselves.

A quotation cited on a page of a corpus document is looked for there whenever the claim is
shown (``locate``), so a quotation on another page, or nowhere in the indexed text, is marked
where Last Order, the red team and the bundle read it. It is a mark, never a refusal: OCR and
typography make true quotations miss (GitHub issue #9: the page a quotation was cited on had
never been compared with anything).
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re

from misaka.core.network.card_contract import Finding
from misaka.core.research import runs


def findings(con, run_id, *, branch_id=None, task_id=None, limit=None, offset=0):
    q, args = "SELECT * FROM research_findings WHERE run_id=?", [run_id]
    for column, value in (("branch_id", branch_id), ("task_id", task_id)):
        if value is not None:
            q += f" AND {column}=?"
            args.append(value)
    return con.execute(q + " ORDER BY created_at,rowid LIMIT ? OFFSET ?",
                       [*args, -1 if limit is None else int(limit), int(offset)]).fetchall()


_DOC_LOCATOR = re.compile(r"^doc:([0-9a-f]{12})(?:#p(\d+))?$")


def _doc_stamp(doc_id, workspace):
    """When the document's index last changed (a re-read rewrites its meta.json), or None."""
    from misaka.core.documents import index as corpus
    ddir = corpus.resolve_doc(doc_id, workspace=workspace)
    try:
        return os.stat(os.path.join(ddir, "meta.json")).st_mtime_ns if ddir else None
    except OSError:
        return None


@functools.lru_cache(maxsize=4096)
def _located(doc_id, quote, cited, workspace, _stamp):
    """``corpus.locate_quote``, once per quotation and document version: every node's context and
    every bundle located every claim of the run again, and a quotation missing from its page reads
    the whole document -- minutes on the event loop for a run of a few hundred claims (0.18.10 sweep)."""
    from misaka.core.documents import index as corpus
    return corpus.locate_quote(doc_id, quote, cited, workspace=workspace)


def locate(claim, workspace):
    """Where a claim's quotation is in the corpus document it cites, as one line -- or None for a
    claim that cites no corpus document or quotes nothing."""
    match = _DOC_LOCATOR.match(str(claim["source_file"] or ""))
    if not match or not str(claim["quote"] or "").strip():
        return None
    doc_id, cited = match.group(1), int(match.group(2)) if match.group(2) else None
    found = _located(doc_id, claim["quote"], cited, workspace, _doc_stamp(doc_id, workspace))
    printed = f" (printed p. {found['printed']})" if found.get("printed") else ""
    across = f", running on to p{found['continues_on']}" if found.get("continues_on") else ""
    return {"on_page": f"located on p{found.get('page')}{printed}{across}",
            "elsewhere": f"NOT on the cited p{cited}: the quotation is on p{found.get('page')}{printed}",
            "not_found": "NOT FOUND in the document's indexed text (OCR or typography may differ; check the page)",
            "no_document": "the cited document is not in this project's corpus"}[found["status"]]


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
        "SELECT branch_id FROM research_cards WHERE run_id=? AND task_id=?",
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
