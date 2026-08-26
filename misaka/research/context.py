"""Context packets for child Last Order sessions.

Every ancestor artifact and session stays addressable at its recorded path; only
this compact map is injected by default, so descendants inherit the history
without a copy of it.
"""
from __future__ import annotations

import json
import os

from misaka.research import ledger, runs


def _branch_chain(con, branch):
    chain, current = [], branch
    while current:
        chain.append({
            "id": current["id"], "depth": current["depth"],
            "trigger": current["trigger_text"], "session_file": current["session_file"],
            "context_artifact": current["context_artifact"],
        })
        parent = current["parent_id"]
        current = (con.execute("SELECT * FROM research_branches WHERE id=?", (parent,)).fetchone()
                   if parent else None)
    return list(reversed(chain))


def _where(run, node, row, lineage):
    """Where the child reads an artifact. One from its own lineage (an ancestor node's line, merged
    into the child's branch) is read from the child's own copy; one from any other branch is read
    where it was recorded -- never from a same-named file that happens to sit on this line."""
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        meta = {}
    own = os.path.join(runs.node_root(run, node), meta["source_file"]) \
        if meta.get("source_file") and row["branch_id"] in lineage else None
    for candidate in (own, meta.get("source_workspace"), row["path"]):
        if candidate and os.path.exists(candidate):
            return candidate
    return row["path"]


def build(con, run, *, issue, node, parent=None, max_findings=80):
    lineage = {entry["id"] for entry in _branch_chain(con, node)}
    artifacts = [{"id": a["id"], "kind": a["kind"], "title": a["title"], "path": _where(run, node, a, lineage)}
                 for a in runs.artifacts(con, run["id"])]
    findings = []
    for finding in ledger.findings(con, run["id"], limit=max_findings):
        evidence = [{"source_file": claim["source_file"], "quote": claim["quote"],
                     "evidence_sha": claim["evidence_sha"]}
                    for claim in ledger.claims(con, finding["id"])]
        findings.append({"id": finding["id"], "text": finding["text"],
                         "task_id": finding["task_id"],
                         "claim_type": finding["claim_type"], "claims": evidence})
    payload = {
        "run_id": run["id"], "workspace": run["workspace"], "root_question": run["question"],
        "root_session": run["root_session"],
        "target_issue": {"id": issue["id"], "kind": issue["kind"],
                         "question": issue["question"], "rationale": issue["rationale"]},
        "ancestor_branches": _branch_chain(con, parent) if parent else [],
        "artifact_map": artifacts, "current_evidence_backed_findings": findings,
        "notice": (
            "Ancestor sessions and artifacts remain available at their recorded paths. "
            "Use the index to identify relevant material, then read the original source. "
            "Summaries are navigation aids, not evidence; verify every quotation against its source."
        ),
    }
    return payload


def render(packet):
    lines = [
        "# Research Context Packet",
        "",
        f"- Run: `{packet['run_id']}`",
        f"- Workspace: `{packet['workspace']}`",
        f"- Root Last Order session: `{packet['root_session'] or 'not recorded'}`",
        "",
        '## Root question',
        packet["root_question"],
        "",
        '## Current research issue',
        f"**{packet['target_issue']['kind']}**: {packet['target_issue']['question']}",
        packet["target_issue"]["rationale"],
        "",
        '## Ancestor branches',
    ]
    for branch in packet["ancestor_branches"]:
        lines.append(f"- `{branch['id']}`: {branch['trigger']}; session={branch['session_file']}")
    lines += ["", '## Current evidence-backed findings']
    for finding in packet["current_evidence_backed_findings"]:
        lines.append(f"- `{finding['id']}` {finding['text']}")
        for claim in finding["claims"]:
            lines.append(f"  - {claim['source_file']}: {claim['quote']}")
    lines += ["", '## Artifact map']
    for artifact in packet["artifact_map"]:
        lines.append(f"- `{artifact['id']}` [{artifact['kind']}] {artifact['title']} — `{artifact['path']}`")
    lines += ["", '## Inheritance discipline', packet["notice"], "", '## Machine-readable packet', "```json",
              json.dumps(packet, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def create(con, run, *, issue, node, parent=None):
    packet = build(con, run, issue=issue, node=node, parent=parent)
    aid, path = runs.write_text(
        con, run["id"], "context", f"Node {node['id']} context",
        f"branches/{node['id']}/context.md", render(packet), branch_id=node["id"],
        metadata={"issue_id": issue["id"], "parent": node["parent_id"]},
    )
    runs.set_node(con, node["id"], context_artifact=aid)
    return aid, path, packet
