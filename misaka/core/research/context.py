"""Context packets for child Last Order sessions.

Every ancestor artifact and session stays addressable at its recorded path; only
this compact map locates artifacts for descendants whose session was forked from the parent.
"""
from __future__ import annotations

import json

from misaka.core.research import ledger, runs


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


def build(con, run, *, issue, node, parent=None):
    artifacts = [{"id": a["id"], "kind": a["kind"], "title": a["title"], "path": a["path"]}
                 for a in runs.artifacts(con, run["id"])]
    findings = []
    for finding in ledger.findings(con, run["id"]):
        evidence = [{"source_file": claim["source_file"], "quote": claim["quote"],
                     "evidence_sha": claim["evidence_sha"]}
                    for claim in ledger.claims(con, finding["id"])]
        findings.append({"id": finding["id"], "text": finding["text"],
                         "task_id": finding["task_id"],
                         "claim_type": finding["claim_type"], "claims": evidence})
    command = runs.action(con, run["id"], issue["branch_id"], "investigate")
    assignment = next((item["assignment"] for item in command["payload"]["assignments"]
                       if item["issue_id"] == issue["id"]), "") if command else ""
    payload = {
        "run_id": run["id"], "workspace": run["workspace"], "root_question": run["question"],
        "root_session": run["root_session"],
        "target_issue": {"id": issue["id"], "kind": issue["kind"],
                         "question": issue["question"], "rationale": issue["rationale"], "assignment": assignment},
        "ancestor_branches": _branch_chain(con, parent) if parent else [],
        "artifact_map": artifacts, "declared_findings": findings,
        "notice": (
            "Ancestor sessions and artifacts remain available at their recorded paths. "
            f"This packet is a snapshot; use misaka_research_view(view='workspace', run_id='{run['id']}') "
            "for current state and newer artifacts. Use recorded paths verbatim, not filenames guessed from IDs or titles. "
            "The ledger records declarations, including corrections and disagreements, not verified truths. "
            "Read full sources and assess their context. A `doc:<doc_id>#p<page>` locator names a corpus "
            "document readable with doc_read; the locator and any quotation are the researcher's declarations."
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
        '## Parent Last Order assignment',
        packet["target_issue"]["assignment"],
        "",
        '## Ancestor branches',
    ]
    for branch in packet["ancestor_branches"]:
        lines.append(f"- `{branch['id']}`: {branch['trigger']}; session={branch['session_file']}")
    lines += ["", '## Declared findings (not machine-reviewed)']
    for finding in packet["declared_findings"]:
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
    with runs.owned_txn(con, run, node):
        aid, path = runs.write_text(
            con, run["id"], "context", f"Node {node['id']} context",
            runs.generated_path(run, "context.md", branch_id=node["id"]), render(packet), branch_id=node["id"],
            metadata={"issue_id": issue["id"], "parent": node["parent_id"]},
        )
        runs.set_node(con, node["id"], context_artifact=aid)
    return aid, path, packet
