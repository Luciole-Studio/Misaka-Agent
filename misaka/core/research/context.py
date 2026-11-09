"""Context packets for the Last Order of every node below the root.

Every artifact and session stays addressable at its recorded path; this compact map locates
them. A node with a single parent is a fork of that parent's conversation and already holds it;
a join is a fork of the lowest node its parents share, so for its parents' own work the packet is
its brief: each parent listed on its own -- question, conclusions, critiques, what its Last Order
did with each issue, the decisions it made -- never merged into one account.
"""
from __future__ import annotations

import json
import os

from misaka.core.platform import prompt_guard
from misaka.core.research import ledger, runs, views
from misaka.utils.paths import posix_relpath


def _parent(con, run, node, parent):
    workspace = run["workspace"]
    rel = lambda path: posix_relpath(path, workspace)
    files = {}
    for row in runs.artifacts(con, run["id"], branch_id=parent["id"]):
        if row["task_id"] is None or row["kind"] in ("critique", "divergence"):
            files.setdefault(row["kind"], []).append(rel(row["path"]))
    return {
        "id": parent["id"], "question": parent["question"], "depth": parent["depth"], "status": parent["status"],
        "kind": views.kind(con, parent), "session_file": parent["session_file"],
        "conclusions": files.get("synthesis", []), "critiques": files.get("critique", []),
        "divergence_review": files.get("divergence", []), "plans": files.get("plan", []),
        "issues": [{"id": row["id"], "origin": row["origin"], "round": row["round"], "question": row["question"],
                    "disposition": row["disposition"], "to": runs.destination(row), "reason": row["reason"]}
                   for row in runs.issues(con, run["id"], node_id=parent["id"])],
        "decisions": [{"question": d["question"], "stakes": d["stakes"],
                       "options": [{"label": o["label"], "premise": o["premise"], "node": o["node_id"],
                                    "reason": o["reason"], "covered_by": o["covered_by"],
                                    "leads_here": o["node_id"] == node["id"]}
                                   for o in runs.options(con, run["id"], decision_id=d["id"])]}
                      for d in runs.decisions(con, run["id"], node_id=parent["id"])],
    }


def _opened_because(con, run, node):
    """What the reconciliation that opened this node wrote about it."""
    for item in runs.reconcile_rounds(con, run["id"]):
        receipt = item["receipt"] or {}
        for key in ("nodes", "joins"):
            if node["id"] in (receipt.get(key) or []):
                entry = item["payload"][key][receipt[key].index(node["id"])]
                return {"reconciliation": item["round"], "question": entry["question"], "rationale": entry["rationale"]}
    return None


def build(con, run, *, node):
    artifacts = [{"id": a["id"], "kind": a["kind"], "title": a["title"], "path": a["path"]}
                 for a in runs.artifacts(con, run["id"])]
    findings = []
    for finding in ledger.findings(con, run["id"]):
        evidence = [{"source_file": claim["source_file"], "quote": claim["quote"],
                     "evidence_sha": claim["evidence_sha"],
                     **({"located": where} if (where := ledger.locate(claim, run["workspace"])) else {})}
                    for claim in ledger.claims(con, finding["id"])]
        findings.append({"id": finding["id"], "text": finding["text"],
                         "task_id": finding["task_id"],
                         "claim_type": finding["claim_type"], "claims": evidence})
    parents = runs.parents(con, node["id"])
    ancestors = views.ancestors(con, node["id"])
    return {
        "run_id": run["id"], "workspace": run["workspace"], "root_question": run["question"],
        "root_session": run["root_session"], "graph": os.path.join(run["workspace"], views.graph_path(run)),
        "paths": os.path.join(run["workspace"], views.paths_path(run)),
        "node": {"id": node["id"], "question": node["question"], "depth": node["depth"], "kind": views.kind(con, node),
                 "forked_from": views.fork_source(con, node)["id"] if parents else None},
        "reached_by": [{"option": o["id"], "label": o["label"], "premise": o["premise"],
                        "decision": o["decision_question"], "stakes": o["decision_stakes"], "decided_at": o["decided_at"],
                        "rivals": [{"label": r["label"], "premise": r["premise"], "node": r["node_id"], "reason": r["reason"]}
                                   for r in runs.options(con, run["id"], decision_id=o["decision_id"]) if r["id"] != o["id"]]}
                       for o in runs.origin_options(con, node["id"])],
        "opened_because": _opened_because(con, run, node),
        "parents": [_parent(con, run, node, parent) for parent in parents],
        "ancestors": sorted(ancestors),
        "errata": [e for e in runs.errata(con, run["id"]) if e["node"] in ancestors],
        "artifact_map": artifacts, "declared_findings": findings,
        "notice": (
            "Every artifact and session remains available at its recorded path. "
            f"This packet is a snapshot; use misaka_research_view(view='workspace', run_id='{run['id']}') "
            "for current state and misaka_research_view(view='graph') for the graph as it stands. Use recorded paths "
            "verbatim, not filenames guessed from IDs or titles. The ledger records declarations, including corrections "
            "and disagreements, not verified truths. Read full sources and assess their context. A `doc:<doc_id>#p<page>` "
            "locator names a corpus document readable with doc_read; the locator and any quotation are the researcher's "
            "declarations."
        ),
    }


def render(packet):
    node = packet["node"]
    lines = [
        "# Research Context Packet", "",
        f"- Run: `{packet['run_id']}`",
        f"- Workspace: `{packet['workspace']}`",
        f"- Root Last Order session: `{packet['root_session'] or 'not recorded'}`",
        f"- Research graph: `{packet['graph']}`",
        f"- Paths list (every possibility raised so far, and where it went): `{packet['paths']}`", "",
        '## Root question', packet["root_question"], "",
        f"## This node: `{node['id']}` ({node['kind']}, depth {node['depth']})", node["question"], "",
    ]
    if node["forked_from"] and len(packet["parents"]) > 1:
        lines += [(f"This conversation is a fork of node `{node['forked_from']}`'s, the lowest node your parents "
                   "share: its turns above are that node's, and none of them is a parent's own work. Each parent below "
                   "keeps its own premises; do not merge them into one world. Say what the shared continuation rests "
                   "on, and keep their differences visible."), ""]
    elif node["forked_from"]:
        lines += [f"This conversation is a fork of node `{node['forked_from']}`'s: its turns above are that node's.", ""]
    if packet["reached_by"]:
        lines += ["## How this node was reached"]
        for reached in packet["reached_by"]:
            lines.append(f"- at `{reached['decided_at']}`, deciding \"{reached['decision']}\" (stakes: {reached['stakes']}): "
                         f"option \"{reached['label']}\" -- premise: {reached['premise']}")
            lines += [f"  - rival \"{rival['label']}\" -- premise: {rival['premise']}"
                      + (f" (node `{rival['node']}`)" if rival["node"] else f" (not pursued: {rival['reason']})"
                         if rival["reason"] else " (not decided yet)") for rival in reached["rivals"]]
        lines.append("")
    if packet["errata"]:
        lines += ["## Corrections recorded against the nodes this one descends from",
                  "Each supersedes the claim it corrects; the corrected conclusion still reads as it was written."]
        lines += [f"- `{e['node']}` says \"{e['claim']}\" -> {e['correction']} (recorded by `{e['raised_by']}`: "
                  f"{e['grounds']})" for e in packet["errata"]]
        lines.append("")
    if packet["opened_because"]:
        opened = packet["opened_because"]
        lines += [f"## Why reconciliation {opened['reconciliation']} opened it", opened["rationale"], ""]
    lines.append("## Parents")
    for parent in packet["parents"]:
        lines += ["", f"### `{parent['id']}` ({parent['kind']}, depth {parent['depth']}, {parent['status']})",
                  parent["question"],
                  "- Conclusions (oldest first): " + (", ".join(f"`{p}`" for p in parent["conclusions"]) or "none"),
                  "- Red-team critiques: " + (", ".join(f"`{p}`" for p in parent["critiques"]) or "none"),
                  "- Divergence review: " + (", ".join(f"`{p}`" for p in parent["divergence_review"]) or "none"),
                  f"- Session: `{parent['session_file'] or 'not recorded'}`"]
        for issue in parent["issues"]:
            lines.append(f"- issue `{issue['id']}` ({issue['origin']}, round {issue['round']}): {issue['question']} "
                         f"→ {issue['disposition'] or 'undisposed'}"
                         + (f" [{issue['to']}]" if issue["to"] else "")
                         + (f": {issue['reason']}" if issue["reason"] else ""))
        for decision in parent["decisions"]:
            lines.append(f"- decision: {decision['question']} (stakes: {decision['stakes']})")
            for option in decision["options"]:
                outcome = ("this node" if option["leads_here"] else f"node {option['node']}" if option["node"]
                       else "not pursued" + (f", answered by {option['covered_by']}" if option.get("covered_by") else "")
                       + f": {option['reason']}" if option["reason"] else "pending")
                lines.append(f"  - {option['label']} -- {option['premise']} → {outcome}")
    # Findings are Sisters' words and their sources' quotations: data, fenced as planner's
    # evidence block fences the same ledger (issue #10 audit, M3).
    declared = []
    for finding in packet["declared_findings"]:
        declared.append(f"- `{finding['id']}` {finding['text']}")
        for claim in finding["claims"]:
            declared.append(f"  - {claim['source_file']}: {claim['quote']}")
    lines += ["", '## Declared findings (not machine-reviewed)']
    if declared:
        lines.append(prompt_guard.untrusted("context-findings", "\n".join(declared)))
    lines += ["", '## Artifact map']
    for artifact in packet["artifact_map"]:
        lines.append(f"- `{artifact['id']}` [{artifact['kind']}] {artifact['title']} — `{artifact['path']}`")
    lines += ["", '## Inheritance discipline', packet["notice"], "", '## Machine-readable packet',
              prompt_guard.untrusted("context-packet", json.dumps(packet, ensure_ascii=False, indent=2)), ""]
    return "\n".join(lines)


def create(con, run, *, node):
    packet = build(con, run, node=node)
    with runs.owned_txn(con, run, node):
        aid, path = runs.write_text(
            con, run["id"], "context", f"Node {node['id']} context",
            runs.generated_path("context.md", node_id=node["id"]), render(packet), branch_id=node["id"],
            metadata={"parents": [parent["id"] for parent in packet["parents"]]},
        )
        runs.set_node(con, node["id"], context_artifact=aid)
    return aid, path, packet
