"""The research graph: queries over its nodes and edges, the structural check of a level's
reconciliation, and the views written into the project.

SQLite (``runs``) is the only authority. ``final/<run>-graph.md``, ``nodes/<node>/NODE.md`` and
``final/<run>-graph.json`` are rewritten from it after state changes and never read back, like the
source bundles. Python checks structure here -- references, depth, the node budget, that every
pending option gets an outcome -- and never judges what a node found or whether two nodes ask the
same question: that is Last Order's call.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from contextlib import contextmanager
from itertools import pairwise

from misaka.config import home
from misaka.core.research import runs
from misaka.utils import atomic, file_lock
from misaka.utils.paths import posix_relpath

_LOG = logging.getLogger(__name__)

GRAPH_VIEW = "graph.md"
GRAPH_SNAPSHOT = "graph.json"
PATHS_VIEW = "paths.md"
NODE_VIEW = "NODE.md"


def kind(con, node):
    """``root`` (depth 0), ``join`` (several parents) or ``alt`` (one parent): read off the edges."""
    if runs.is_root(node):
        return "root"
    return "join" if len(runs.parents(con, node["id"])) > 1 else "alt"


def ancestors(con, node_id):
    """Every node the given one descends from, through any parent."""
    seen, frontier = set(), [node_id]
    while frontier:
        for parent in runs.parents(con, frontier.pop()):
            if parent["id"] not in seen:
                seen.add(parent["id"])
                frontier.append(parent["id"])
    return seen


def fork_source(con, node):
    """The node whose Last Order conversation a new node forks: the lowest common ancestor of its
    parents, which for a single parent is that parent. Parents can have several lowest common
    ancestors, none descending from another; each carries its own line only, so the search goes on
    to the lowest node they all descend from. A join thus inherits what its lines share and neither
    line's own deliberation, which reaches it through its context packet. The root ends every search."""
    ids = [parent["id"] for parent in runs.parents(con, node["id"])]
    while len(ids) > 1:
        common = set.intersection(*({i} | ancestors(con, i) for i in ids))
        above = {c: ancestors(con, c) for c in common}
        ids = sorted(c for c in common if not any(c in above[other] for other in common if other != c))
    return runs.node(con, ids[0])


def lineage(con, node):
    """The nodes whose conversations this node's own continues, nearest first: itself, the node it
    forked (``fork_source``), and so on up to the root. A single-parent line is its ancestors; a
    join's parents are its ancestors but not its lineage -- their deliberation is in their own
    sessions, not in the conversation the join inherited."""
    chain, current = [node["id"]], node
    while not runs.is_root(current):
        current = fork_source(con, current)
        chain.append(current["id"])
    return chain


# --- where each possibility went ---------------------------------------------------------
# Every possibility raised in a run -- an option of a decision, a divergence proposal, a red-team
# issue taken to a decision -- has at most one pointer to what answers it, set by the Last Order who
# judged it: an issue ``becomes`` an option or is ``covered_by`` a node; an option ``opens`` a node
# or, left unopened, is ``covered_by`` the node or the option that answers it. Following them ends at
# a node, or at a record left unopened (declined, not pursued, or still waiting). Nothing here judges
# whether two possibilities are the same; it reads what was recorded. The chain is never shortened:
# each step is someone's judgement, and the record keeps them all.

def possibility_issues(con, run, issues=None):
    """The issues that are possibilities, the ones the paths list carries: every divergence proposal,
    and every red-team issue taken to a node's decision -- disposed ``branch``, or named in a decision
    that then covered or declined it. A gap is not one, nor a red-team issue answered in its node."""
    decided = set()
    for node in runs.nodes(con, run["id"]):
        recorded = runs.action(con, run["id"], node["id"], runs.DECIDE_KEY)
        if recorded:
            payload = recorded["payload"]
            decided.update(source for decision in payload.get("decisions", ()) for option in decision["options"]
                           for source in option.get("sources", ()))
            decided.update(item["issue_id"] for item in payload.get("declined", ()))
    rows = runs.issues(con, run["id"]) if issues is None else issues
    return [row for row in rows if row["origin"] == "divergence" or row["disposition"] == "branch" or row["id"] in decided]


def proposal_premise(issue):
    """What a proposal rests on: the divergence reviewer's stated premise, else its rationale's first line
    (a red-team issue's). Premises, not questions, tell two possibilities apart."""
    text = (issue.get("rationale") or "").strip()
    for line in text.splitlines():
        if line.startswith("Premise: "):
            return line[len("Premise: "):].strip()
    return text.splitlines()[0].strip() if text else ""


def _pointer(element):
    """Where one possibility points, or None: an issue's option or node, an option's node or cover."""
    if "decision_id" in element:                          # an option
        return element["node_id"] or element["covered_by"]
    return element["option_id"] or element["covered_by"]


def where(elements, element_id):
    """The chain from ``element_id`` along the recorded pointers, ending at a node id or at a record
    with no pointer (``elements`` maps the run's options and issues by id). Never written back."""
    chain, seen = [element_id], {element_id}
    while chain[-1] in elements:
        target = _pointer(elements[chain[-1]])
        if target is None or target in seen:
            break
        chain.append(target)
        seen.add(target)
    return chain


def reopenable(con, run):
    """The records a new option may bring back by answering the reason recorded for them: possibilities
    declined, covered or left unopened -- every one that is not a node's own and no longer waits."""
    found = {row["id"] for row in possibility_issues(con, run) if row["disposition"] in ("decline", "covered")}
    found.update(row["id"] for row in runs.options(con, run["id"]) if row["node_id"] is None and row["reason"])
    return found


def coverage_claims(con, run):
    """Possibilities a node closed as ``covered`` by a node -- or a waiting option -- of another line,
    whose coverage no reconciliation has reviewed yet. A node knows its own line from the inside: the
    nodes it descends from decided in the conversation it continues, or, for a join, in the parents'
    sessions its packet names. Another line it knows only by what that node wrote, so whether that node
    really answers the possibility is judged where both lines are in view: the reconciliation. Each
    claim carries what it needs: the proposal, the claimant's reason, and the covering node or option
    as it stood when the claim was made and as it stands now."""
    workspace = run["workspace"]
    options = {row["id"]: row for row in runs.options(con, run["id"])}
    decided_at = {row["id"]: row["node_id"] for row in runs.decisions(con, run["id"])}
    claims = []
    for issue in possibility_issues(con, run):
        if issue["disposition"] != "covered" or issue.get("coverage_review"):
            continue
        target = issue["covered_by"] or issue["option_id"]
        if target is None:
            continue
        claimant = issue["branch_id"]
        owner = target if issue["covered_by"] else decided_at.get(options[target]["decision_id"])
        if owner == claimant or owner in ancestors(con, claimant):
            continue
        decided = runs.action(con, run["id"], claimant, runs.DECIDE_KEY)
        claimed_at = decided["created_at"] if decided else issue["created_at"]
        claim = {"proposal": issue["id"], "raised_at": claimant, "question": issue["question"],
                 "premise": proposal_premise(issue), "rationale": issue["rationale"], "claimant_reason": issue["reason"],
                 "claimed_at": claimed_at}
        if issue["covered_by"]:
            node = runs.node(con, target)
            versions = syntheses(con, run, node)
            then = [row for row in versions if row["created_at"] <= claimed_at]
            claim["covered_by"] = {
                "node": target, "question": node["question"], "depth": node["depth"], "status": node["status"],
                "conclusion": posix_relpath(versions[-1]["path"], workspace) if versions else None,
                "conclusion_when_claimed": posix_relpath(then[-1]["path"], workspace) if then else None,
                "revised_since_claim": bool(versions) and len(then) < len(versions)}
        else:
            option = options[target]
            claim["covered_by"] = {"option": target, "label": option["label"], "premise": option["premise"],
                                   "decided_at": owner,
                                   "outcome": _outcome(con, option, owner)}
        claims.append(claim)
    return claims


def syntheses(con, run, node):
    """The node's conclusions, first version first."""
    return list(runs.artifacts(con, run["id"], branch_id=node["id"], kind="synthesis"))


def dissolution(con, run, node):
    """What the node's latest conclusion dissolves, when it does: ``{"question", "grounds"}`` with
    ``question`` either ``this_node`` or ``research_question``. A revised conclusion that no longer
    dissolves anything leaves the earlier version's record behind."""
    versions = len(syntheses(con, run, node))
    recorded = runs.action(con, run["id"], node["id"], runs.dissolve_key(versions)) if versions else None
    return recorded["payload"] if recorded else None


def dissolving_nodes(con, run):
    """Nodes whose latest conclusion dissolves the research question itself."""
    return [node for node in runs.nodes(con, run["id"])
            if (dissolution(con, run, node) or {}).get("question") == "research_question"]


def graph_path(run):
    return runs.run_path(run, GRAPH_VIEW)


def paths_path(run):
    return runs.run_path(run, PATHS_VIEW)


def node_view_path(node_id):
    return os.path.join(runs.node_dir(node_id), NODE_VIEW)


# --- reconciliation ---------------------------------------------------------------------

def reconcilable_options(con, run):
    """Pending options whose deciding node has finished its own work: what a reconciliation must
    give an outcome to."""
    return [row for row in runs.pending_options(con, run["id"])
            if runs.node(con, row["decided_at"])["status"] == "closed"]


def reconcile_state(con, run):
    """``(needed, considered)``: whether this level barrier needs the root Last Order, and the
    finished nodes a reconciliation now would cover. A barrier: never while a node is still running
    or failed and waiting for its retry -- not even on a resume, which restarts the driver's loop at
    this check with a level half done. Then it is needed while an option waits for an outcome, or
    when nodes finished since the last one and at least two nodes hold a conclusion (below that no
    join or relation can exist), or while a coverage claim waits for its review. Structure only;
    nothing here reads what a node found."""
    nodes = runs.nodes(con, run["id"])
    finished = [n for n in nodes if n["status"] == "closed"]
    covered = set()
    for item in runs.reconcile_rounds(con, run["id"]):
        if item["applied_at"] is not None:
            covered.update(item["receipt"]["considered"])
    fresh = [n["id"] for n in finished if n["id"] not in covered]
    concluded = sum(1 for n in finished if syntheses(con, run, n))
    settled = all(n["status"] in ("closed", "parked") for n in nodes)
    needed = settled and (bool(reconcilable_options(con, run)) or bool(coverage_claims(con, run))
                          or (bool(fresh) and concluded >= 2))
    return needed, [n["id"] for n in finished]


def resolve_reconcile(con, run, payload):
    """Check a reconciliation's structure against the graph as it stands and return what applying
    it creates: each new node with its parents (the nodes whose options it pursues, or the nodes
    it joins). Raises ValueError with what to change; judges nothing about content."""
    chosen = runs.current_limits(con, run)
    pending = {row["id"]: row for row in reconcilable_options(con, run)}
    problems = []
    claims = {claim["proposal"]: claim for claim in coverage_claims(con, run)}
    verdicts = payload.get("coverage") or []
    reviewed = Counter(item["proposal"] for item in verdicts)
    stray = sorted(set(reviewed) - set(claims))
    if stray:
        problems.append(f"not coverage claims waiting for review: {', '.join(stray)}")
    twice = sorted(pid for pid, count in reviewed.items() if count > 1)
    if twice:
        problems.append(f"coverage claims given more than one verdict: {', '.join(twice)}")
    unreviewed = sorted(set(claims) - set(reviewed))
    if unreviewed:
        problems.append(f"every coverage claim needs a verdict (holds or reopen); missing: {', '.join(unreviewed)}")
    # A reopened proposal is a possibility of its own: it is given an outcome like a pending option, and a
    # node opened for it is a child of the node that raised it.
    reopened = {item["proposal"]: claims[item["proposal"]] for item in verdicts
                if item["verdict"] == "reopen" and item["proposal"] in claims}
    origin = {**{oid: row["decided_at"] for oid, row in pending.items()},
              **{pid: claim["raised_at"] for pid, claim in reopened.items()}}
    named = Counter([oid for item in payload["nodes"] for oid in item["options"]]
                    + [item["option"] for item in payload["not_pursued"]])
    unknown = sorted(set(named) - set(origin))
    if unknown:
        problems.append(f"not pending options of this run or proposals whose coverage this reconciliation "
                        f"reopens: {', '.join(unknown)}")
    twice = sorted(oid for oid, count in named.items() if count > 1)
    if twice:
        problems.append(f"options given more than one outcome: {', '.join(twice)}")
    missing = sorted(set(origin) - set(named))
    if missing:
        problems.append("every pending option, and every proposal whose coverage is reopened, needs an outcome "
                        f"(a node, or not_pursued with its reason); missing: {', '.join(missing)}")
    resolved = {"nodes": [], "joins": [], "not_pursued": [], "relations": [], "coverage": []}
    total = len(runs.nodes(con, run["id"]))
    for position, item in enumerate(payload["nodes"], 1):
        if not item["options"]:
            problems.append(f"nodes[{position}] pursues no option; a node opened without one is a join")
            continue
        parent_ids = list(dict.fromkeys(origin[oid] for oid in item["options"] if oid in origin))
        depth = max((runs.node(con, pid)["depth"] for pid in parent_ids), default=-1) + 1
        if parent_ids and depth > chosen["max_depth"]:
            problems.append(f"nodes[{position}] would open at depth {depth}, beyond max_depth {chosen['max_depth']}: "
                            "record its options under not_pursued with the depth limit as the reason")
        resolved["nodes"].append({"question": item["question"], "parents": parent_ids,
                                  "options": list(item["options"]), "rationale": item["rationale"]})
    for position, item in enumerate(payload["joins"], 1):
        parent_ids = list(dict.fromkeys(item["parents"]))
        rows = [runs.node(con, pid) for pid in parent_ids]
        if len(parent_ids) < 2:
            problems.append(f"joins[{position}] names fewer than two distinct nodes")
        if any(row is None or row["run_id"] != run["id"] for row in rows):
            problems.append(f"joins[{position}] names a node that is not in this run")
            continue
        unfinished = [row["id"] for row in rows if row["status"] != "closed"]
        if unfinished:
            problems.append(f"joins[{position}]: {', '.join(unfinished)} has not finished its own work "
                            "(or was skipped or failed) and cannot carry a join")
        related = [f"{a} descends from {b}" for a in parent_ids for b in parent_ids
                   if a != b and b in ancestors(con, a)]
        if related:
            problems.append(f"joins[{position}]: {'; '.join(related)} -- a node already carries its ancestors "
                            "forward, so join nodes that are not each other's ancestors")
        depth = max((row["depth"] for row in rows), default=-1) + 1
        if depth > chosen["max_depth"]:
            problems.append(f"joins[{position}] would open at depth {depth}, beyond max_depth {chosen['max_depth']}")
        resolved["joins"].append({"question": item["question"], "parents": parent_ids, "rationale": item["rationale"]})
    opened = len(resolved["nodes"]) + len(resolved["joins"])
    if total + opened > chosen["max_nodes"]:
        problems.append(f"this would bring the graph to {total + opened} nodes, over max_nodes {chosen['max_nodes']}: "
                        "merge options that ask the same question into one node, or record options under "
                        "not_pursued with the node limit as the reason")
    opening = {oid for item in payload["nodes"] for oid in item["options"]}
    for item in payload["not_pursued"]:
        cover = item.get("covered_by") or None
        found = runs.node(con, cover) if cover else None
        if cover and cover == item["option"]:
            problems.append(f"not_pursued {item['option']} is covered by itself")
        elif cover and not (found is not None and found["run_id"] == run["id"]) and cover not in opening:
            problems.append(f"not_pursued {item['option']}: covered_by {cover} is neither a node of this run nor an "
                            "option this reconciliation opens")
        resolved["not_pursued"].append({"option": item["option"], "reason": item["reason"], "covered_by": cover})
    for item in verdicts:
        claim = claims.get(item["proposal"])
        if claim is not None:
            cover = claim["covered_by"]
            resolved["coverage"].append({"proposal": item["proposal"], "verdict": item["verdict"], "note": item["note"],
                                         "node": claim["raised_at"], "premise": claim["premise"],
                                         "covered_by": cover.get("node") or cover.get("option")})
    resolved["relations"] = _relations(con, run, payload["relations"], problems)
    if problems:
        raise ValueError("Reconciliation not accepted: " + "; ".join(problems) + ".")
    return resolved


def _relations(con, run, items, problems):
    out = []
    for position, item in enumerate(items, 1):
        members = list(dict.fromkeys(item["nodes"]))
        rows = [runs.node(con, nid) for nid in members]
        if len(members) < 2 or any(row is None or row["run_id"] != run["id"] for row in rows):
            problems.append(f"relations[{position}] must name at least two distinct nodes of this run")
        out.append({"kind": item["kind"], "nodes": members, "note": item["note"]})
    return out


def resolve_relations(con, run, payload):
    """Check relations recorded outside a reconciliation: every one names two or more nodes of the run."""
    problems = []
    relations = _relations(con, run, payload["relations"], problems)
    if problems:
        raise ValueError("Relations not accepted: " + "; ".join(problems) + ".")
    return {"relations": relations}


# --- views --------------------------------------------------------------------------------

def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cell(value):
    return " ".join(str(value or "").splitlines()).strip().replace("|", r"\|")


def _mermaid_label(text):
    return _clip(text, 60).replace('"', "'").replace("[", "(").replace("]", ")")


def _outcome(con, option, deciding_node_id):
    if option["node_id"] == deciding_node_id:
        return "this node's own line"
    if option["node_id"]:
        return f"node `{option['node_id']}`"
    if option["reason"]:
        cover = option.get("covered_by")
        what = "node" if cover and runs.node(con, cover) is not None else "option"
        return "not pursued" + (f", answered by {what} `{cover}`" if cover else "") + f": {option['reason']}"
    return "pending"


def _dispositions(con, run, node):
    counts = Counter(row["disposition"] or "undisposed" for row in runs.issues(con, run["id"], node_id=node["id"]))
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "-"


def snapshot(con, run):
    """The whole graph as plain data, read in one pass: the source of every view."""
    workspace = run["workspace"]
    rel = lambda path: posix_relpath(path, workspace) if path else None
    out = {"run": run["id"], "question": run["question"], "status": run["status"], "phase": run["phase"],
           "limits": runs.limits(run), "nodes": [], "decisions": [], "relations": runs.relations(con, run["id"]),
           "reconciles": []}
    for node in runs.nodes(con, run["id"]):
        files = {}
        for row in runs.artifacts(con, run["id"], branch_id=node["id"]):
            if row["task_id"] is None:
                files.setdefault(row["kind"], []).append(rel(row["path"]))
        out["nodes"].append({
            "id": node["id"], "question": node["question"], "depth": node["depth"], "status": node["status"],
            "kind": kind(con, node), "parents": [p["id"] for p in runs.parents(con, node["id"])],
            "children": [c["id"] for c in runs.children(con, node["id"])],
            "reached_by": [{"option": o["id"], "label": o["label"], "premise": o["premise"],
                            "decision": o["decision_question"], "decided_at": o["decided_at"]}
                           for o in runs.origin_options(con, node["id"])],
            "files": files, "issues": _dispositions(con, run, node), "dissolves": dissolution(con, run, node),
            "red_team": runs.red_team(con, run["id"], node["id"]),
            "cards": [{"id": t["id"], "title": t["title"], "kind": t["research_kind"], "round": t["round"],
                       "attempt": int(t["generation"]), "status": t["status"], "folder": rel(t["output_dir"])}
                      for t in runs.tasks(con, run["id"], node_id=node["id"])],
        })
    for decision in runs.decisions(con, run["id"]):
        out["decisions"].append({
            "id": decision["id"], "node": decision["node_id"], "origin": decision["origin"],
            "question": decision["question"], "stakes": decision["stakes"],
            "options": [{"id": o["id"], "label": o["label"], "premise": o["premise"], "node": o["node_id"],
                         "reason": o["reason"], "covered_by": o["covered_by"], "answers": o["answers"],
                         "outcome": _outcome(con, o, decision["node_id"])}
                        for o in runs.options(con, run["id"], decision_id=decision["id"])]})
    for item in runs.reconcile_rounds(con, run["id"]):
        out["reconciles"].append({"round": item["round"], "applied": item["applied_at"] is not None,
                                  "summary": item["payload"].get("summary", ""), "receipt": item["receipt"]})
    issues = possibility_issues(con, run)
    claims = {claim["proposal"] for claim in coverage_claims(con, run)}
    out["paths"] = _paths(con, run, out, issues, claims)
    out["destinations"] = _destinations(con, run, out, issues, claims)
    out["errata"] = runs.errata(con, run["id"])
    return out


def _paths(con, run, data, issues, claims):
    """Every possibility raised in the run, under the node that raised it, with where it went: each
    option of every decision (with the proposals it takes up), and each divergence proposal or branch
    issue no option took up. Red-team corrections are not possibilities and stay out."""
    options = {option["id"] for decision in data["decisions"] for option in decision["options"]}
    taken = {}
    for issue in issues:
        if issue["option_id"] in options:
            taken.setdefault(issue["option_id"], []).append({"issue": issue["id"], "question": issue["question"]})
    out = []
    for node in data["nodes"]:
        entries = [{"id": option["id"], "what": "option", "text": option["label"], "premise": option["premise"],
                    "decision": decision["question"], "outcome": option["outcome"], "takes_up": taken.get(option["id"], []),
                    "answers": option["answers"]}
                   for decision in data["decisions"] if decision["node"] == node["id"] for option in decision["options"]]
        entries += [{"id": issue["id"], "what": "proposal", "text": issue["question"], "premise": proposal_premise(issue),
                     "outcome": _proposal_outcome(con, run, issue, claims)}
                    for issue in issues if issue["branch_id"] == node["id"]
                    and not (issue["disposition"] != "covered" and issue["option_id"] in options)]
        if entries:
            out.append({"node": node["id"], "kind": node["kind"], "depth": node["depth"], "question": node["question"],
                        "entries": entries})
    return out


def _destinations(con, run, data, issues, claims):
    """The same possibilities as ``_paths``, grouped the other way: under what answers each now -- the
    node that pursues it, or the record that leaves it unopened -- through every option it went by.
    A path brought back by a later option stays under its own record, which names that option."""
    decided_at = {decision["id"]: decision["node"] for decision in data["decisions"]}
    options = {row["id"]: row for row in runs.options(con, run["id"])}
    elements = {**options, **{row["id"]: row for row in issues}}
    order = {node["id"]: position for position, node in enumerate(data["nodes"])}
    answered_by = {}
    for option in options.values():
        for record in option["answers"]:
            answered_by.setdefault(record, []).append(option["id"])

    def describe(element_id, chain):
        element = elements[element_id]
        if "decision_id" in element:
            entry = {"id": element_id, "what": "option", "text": element["label"], "premise": element["premise"],
                     "at": decided_at.get(element["decision_id"]), "reason": element["reason"]}
        else:
            entry = {"id": element_id, "what": "proposal", "text": element["question"], "at": element["branch_id"],
                     "premise": proposal_premise(element), "disposition": element["disposition"], "reason": element["reason"],
                     "review": _review_note(element, claims) or None}
        entry["via"] = chain[1:-1]
        entry["brought_back_by"] = []
        for oid in answered_by.get(element_id, []):
            end = where(elements, oid)[-1]
            goes = (f"node `{end}`" if end not in elements else "waiting for the reconciliation" if end == oid
                    and not options[oid]["reason"] else f"`{end}`")
            entry["brought_back_by"].append({"option": oid, "at": decided_at.get(options[oid]["decision_id"]),
                                             "goes_to": goes})
        return entry

    groups = {}
    for element_id in [*options, *(row["id"] for row in issues)]:
        chain = where(elements, element_id)
        end = chain[-1]
        groups.setdefault(end, []).append(describe(element_id, chain))
    nodes = {node["id"]: node for node in data["nodes"]}
    out = {"nodes": [], "not_opened": [], "waiting": [], "undecided": []}
    for end in sorted(groups, key=lambda key: (order.get(key, len(order)), key)):
        members = groups[end]
        if end in nodes:
            node = nodes[end]
            out["nodes"].append({"node": end, "kind": node["kind"], "depth": node["depth"], "status": node["status"],
                                 "question": node["question"], "members": members})
            continue
        root = elements.get(end)
        if root is None:
            continue
        if "decision_id" in root:
            out["not_opened" if root["reason"] else "waiting"].append({"record": end, "members": members})
        else:
            out["undecided" if root["disposition"] is None else "not_opened"].append({"record": end, "members": members})
    return out


def _proposal_outcome(con, run, issue, claims=()):
    if issue["disposition"] == "covered":
        target = f"node `{issue['covered_by']}`" if issue["covered_by"] else f"option `{issue['option_id']}`"
        return (f"covered by {target}" + (f" -- {issue['reason']}" if issue["reason"] else "")
                + _review_text(issue, claims))
    if issue["disposition"] == "decline":
        return f"declined: {issue['reason']}"
    return "awaiting this node's decision"


def _review_note(issue, claims):
    """What became of a coverage claim: its review, or that it waits for one; empty for any other issue."""
    review = issue.get("coverage_review")
    if review:
        word = "held" if review["verdict"] == "holds" else "was reopened"
        return f"the coverage claim {word} at reconciliation {review['round']}: {review['note']}"
    return "a claim across lines: the next reconciliation reviews it" if issue["id"] in claims else ""


def _review_text(issue, claims):
    note = _review_note(issue, claims)
    return f" ({note})" if note else ""


def render_graph(data):
    """``final/<run>-graph.md``: the map every Last Order and review card reads."""
    lines = [f"# Research graph — run {data['run']}", "",
             "Written by misaka from the run's records after every change; edit nothing here, it is rewritten.",
             "", f"Question: {data['question']}", "",
             f"Every possibility raised so far, and where it went: `{runs.run_path({'id': data['run']}, PATHS_VIEW)}`.", "",
             (f"Status: {data['status']}/{data['phase']} · limits: depth {data['limits']['max_depth']}, "
              f"nodes {len(data['nodes'])}/{data['limits']['max_nodes']}, revisions {data['limits']['max_revisions']}, "
              f"follow-ups {data['limits']['max_followups']}"), "", "```mermaid", "flowchart TD"]
    by_id = {n["id"]: n for n in data["nodes"]}
    for node in data["nodes"]:
        lines.append(f'  {node["id"]}["{node["id"]} · {node["kind"]} · {node["status"]}<br/>{_mermaid_label(node["question"])}"]')
    for node in data["nodes"]:
        labels = {}
        for item in node["reached_by"]:
            labels.setdefault(item["decided_at"], []).append(item["label"])
        for parent in node["parents"]:
            label = "; ".join(labels.get(parent, []))
            arrow = "==>" if node["kind"] == "join" else "-->"
            lines.append(f"  {parent} {arrow}" + (f'|"{_mermaid_label(label)}"|' if label else "") + f" {node['id']}")
    for relation in data["relations"]:
        members = [m for m in relation["nodes"] if m in by_id]
        for a, b in pairwise(members):
            lines.append(f'  {a} -.-|"{relation["kind"]}"| {b}')
    lines += ["```", "", "## Nodes", "",
              "| Node | Kind | Depth | Status | Question | Parents | Reached by | Conclusion (latest) | Issues |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for node in data["nodes"]:
        reached = "; ".join(f"{r['decided_at']}: {r['label']}" for r in node["reached_by"]) or "-"
        conclusion = (node["files"].get("synthesis") or ["-"])[-1]
        if node["dissolves"]:
            conclusion += f" (dissolves {'the research question' if node['dissolves']['question'] == 'research_question' else 'its question'})"
        lines.append(f"| `{node['id']}` | {node['kind']} | {node['depth']} | {node['status']} | {_cell(node['question'])} "
                     f"| {', '.join(node['parents']) or '-'} | {_cell(reached)} | `{conclusion}` | {node['issues']} |")
    lines += ["", "## Decisions and options", ""]
    if not data["decisions"]:
        lines.append("No decision recorded yet.")
    for decision in data["decisions"]:
        lines += [f"### `{decision['node']}` · {_cell(decision['question'])} ({decision['origin']})", "",
                  f"Stakes: {_cell(decision['stakes'])}", "", "| Option | Premise | Outcome |", "| --- | --- | --- |"]
        lines += [f"| {_cell(o['label'])} | {_cell(o['premise'])} | {_cell(o['outcome'])} |" for o in decision["options"]]
        lines.append("")
    lines += ["## Relations", ""]
    lines += [f"- {r['kind']}: {', '.join(f'`{n}`' for n in r['nodes'])} — {_cell(r['note'])}" for r in data["relations"]] \
        or ["No relation recorded."]
    lines += ["", "## Reconciliations", ""]
    lines += [f"- round {r['round']}{'' if r['applied'] else ' (not applied yet)'}: {_cell(r['summary']) or '-'}"
              for r in data["reconciles"]] or ["None yet."]
    lines += ["", "## Corrections (errata)", ""]
    lines += [_erratum_line(e) for e in data["errata"]] or ["None recorded."]
    return "\n".join(lines) + "\n"


def _erratum_line(erratum):
    return (f"- `{erratum['node']}` says \"{_cell(erratum['claim'])}\" → {_cell(erratum['correction'])} "
            f"(recorded by `{erratum['raised_by']}`: {_cell(erratum['grounds'])})")


def render_paths(data):
    """``final/<run>-paths.md``: every possibility raised so far and where it went, under the node
    that raised it -- what a review reads before proposing, so nothing on it is raised again."""
    flat = lambda text: " ".join(str(text or "").split())
    lines = [f"# Paths — run {data['run']}", "",
             "Written by misaka from the run's records after every change; edit nothing here, it is rewritten.", "",
             ("Every possibility raised in this run -- each option of every decision, each divergence proposal -- "
              "under the node that raised it, with where it went. What is on this list is not raised again: a node "
              "named here pursues it, or it was declined or left unopened for the reason given."), ""]
    if not data["paths"]:
        lines.append("None raised yet.")
    for group in data["paths"]:
        lines += [f"## `{group['node']}` ({group['kind']}, depth {group['depth']}) — {_clip(group['question'], 120)}", ""]
        for entry in group["entries"]:
            if entry["what"] == "option":
                lines.append(f"- `{entry['id']}` option \"{flat(entry['text'])}\" (decision: {flat(entry['decision'])}) — "
                             f"premise: {flat(entry['premise'])} → {flat(entry['outcome'])}")
                lines += [f"  - takes up `{t['issue']}`: {flat(t['question'])}" for t in entry["takes_up"]]
            else:
                lines.append(f"- `{entry['id']}` proposal: {flat(entry['text'])}{_premise_text(entry, flat)} → "
                             f"{flat(entry['outcome'])}")
        lines.append("")
    lines += _render_destinations(data.get("destinations"), flat)
    return "\n".join(lines).rstrip("\n") + "\n"


def _premise_text(entry, flat):
    return f" — premise: {flat(entry['premise'])}" if entry.get("premise") else ""


def _render_destinations(found, flat):
    """The paths list's second part: every possibility under what answers it now."""
    if not found or not any(found.values()):
        return []

    def member(entry, root, indent=""):
        where = f" at `{entry['at']}`" if entry["at"] else ""
        if entry["what"] == "option":
            text = f"{indent}- `{entry['id']}` option \"{flat(entry['text'])}\"{where} — premise: {flat(entry['premise'])}"
        else:
            text = f"{indent}- `{entry['id']}` proposal{where}: {flat(entry['text'])}" + _premise_text(entry, flat)
        if entry["via"]:
            text += " — via " + " → ".join(f"`{step}`" for step in entry["via"])
        if entry["id"] == root:
            if entry["what"] == "option" and entry["reason"]:
                text += f" — not pursued: {flat(entry['reason'])}"
            elif entry["what"] == "proposal" and entry["disposition"] == "decline":
                text += f" — declined: {flat(entry['reason'])}"
        elif entry["what"] == "proposal" and entry["disposition"] == "covered":
            text += f" — covered: {flat(entry['reason'])}"
        elif entry["what"] == "option" and entry["reason"]:
            text += f" — not pursued: {flat(entry['reason'])}"
        if entry.get("review"):
            text += f" ({flat(entry['review'])})"
        lines = [text]
        lines += [f"{indent}  - brought back by option `{b['option']}`" + (f" at `{b['at']}`" if b["at"] else "")
                  + f", which answers the reason recorded here → {b['goes_to']}" for b in entry["brought_back_by"]]
        return lines

    lines = ["## Where each possibility went", "",
             ("The same possibilities, grouped under what answers each now: the node that pursues it -- opened for it, "
              "or named as covering it -- or the record that leaves it unopened, through every option it went by "
              "(`via`). A possibility reached from another frame shows here beside the node that took it up. Grouped "
              "by premise only as the run's Last Orders judged it: read the premises, not the labels."), ""]
    for group in found["nodes"]:
        lines += [(f"### Node `{group['node']}` ({group['kind']}, depth {group['depth']}, {group['status']}) — "
                   f"{_clip(group['question'], 120)}"), ""]
        for entry in group["members"]:
            lines += member(entry, None)
        lines.append("")
    for title, key in (("Not opened", "not_opened"), ("Waiting for the reconciliation", "waiting"),
                       ("Waiting for its node's decision", "undecided")):
        if found[key]:
            lines += [f"### {title}", ""]
            for group in found[key]:
                # The record first; what went to it under it.
                ordered = sorted(group["members"], key=lambda entry: entry["id"] != group["record"])
                for entry in ordered:
                    lines += member(entry, group["record"], "" if entry["id"] == group["record"] else "  ")
            lines.append("")
    return lines


def render_node(data, node_id):
    """``nodes/<node>/NODE.md``: what this folder is, in the graph's words."""
    node = next(n for n in data["nodes"] if n["id"] == node_id)
    here = runs.node_dir(node_id)
    link = lambda path: f"`{posix_relpath(path, here)}`"
    lines = [f"# Node {node_id} — {_clip(node['question'], 120)}", "",
             "Written by misaka from the run's records; edit nothing here, it is rewritten.", "",
             f"- Run: `{data['run']}` · kind: {node['kind']} · depth {node['depth']} · status {node['status']}",
             f"- Question: {node['question']}",
             "- Parents: " + (", ".join(f"[`{p}`](../{p}/{NODE_VIEW})" for p in node["parents"]) or "none (the root)"),
             "- Children: " + (", ".join(f"[`{c}`](../{c}/{NODE_VIEW})" for c in node["children"]) or "none yet"),
             f"- Graph: `{posix_relpath(runs.run_path({'id': data['run']}, GRAPH_VIEW), here)}`"]
    if node["red_team"]:
        lines.append(f"- Red team and divergence review: Sister {node['red_team']['assignee']}"
                     + (f" — {node['red_team']['reason']}" if node["red_team"].get("reason") else ""))
    if node["dissolves"]:
        which = "the research question" if node["dissolves"]["question"] == "research_question" else "its own question"
        lines += ["", f"## Outcome: its conclusion dissolves {which}", "", node["dissolves"]["grounds"]]
    if node["reached_by"]:
        lines += ["", "## How this node was reached", ""]
        lines += [f"- at `{r['decided_at']}`, deciding \"{_clip(r['decision'], 100)}\": option \"{r['label']}\" — premise: {r['premise']}"
                  for r in node["reached_by"]]
    lines += ["", "## Files", ""]
    for name, paths in sorted(node["files"].items()):
        lines.append(f"- {name}: " + ", ".join(link(p) for p in paths))
    if not node["files"]:
        lines.append("- none yet")
    if node["cards"]:
        lines += ["", "## Cards", ""]
        lines += [f"- [{c['id']}] {_clip(c['title'], 80)} ({c['kind']}, round {c['round']}"
                  + (f", attempt {c['attempt']}" if c["attempt"] > 1 else "") + f", {c['status']})"
                  + (f" — {link(c['folder'])}" if c["folder"] else "") for c in node["cards"]]
    lines += ["", f"## Issues: {node['issues']}", "",
              f"The issues and their dispositions: misaka_research_view(view=\"issues\", node=\"{node_id}\").", ""]
    against = [e for e in data["errata"] if e["node"] == node_id]
    if against:
        lines += ["## Corrections recorded against this node", "", *map(_erratum_line, against), ""]
    made = [d for d in data["decisions"] if d["node"] == node_id]
    if made:
        lines += ["## Decisions made here", ""]
        for decision in made:
            lines.append(f"- {decision['question']} ({decision['origin']}) — stakes: {decision['stakes']}")
            lines += [f"  - {o['label']} — {o['premise']} → {o['outcome']}" for o in decision["options"]]
    return "\n".join(lines).rstrip() + "\n"


@contextmanager
def _writer(run_id):
    """One writer of a run's views at a time, across processes. The records are read inside the
    lock, so a later write never carries an older picture than an earlier one."""
    path = home.path("locks") / f"research-graph-{run_id}.lock"
    os.makedirs(path.parent, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        file_lock.lock_fd(fd)
        yield
    finally:
        os.close(fd)


def write_views(con, run, *, final=False):
    """Rewrite the graph view, the paths list and every node's NODE.md from the records; with ``final`` also the
    ``graph.json`` snapshot a finished (or halted) run leaves in the project. Derived output: a
    failure is logged and the workflow carries on. Never call this inside a database transaction."""
    try:
        with _writer(run["id"]):
            current = runs.get(con, run["id"])
            data = snapshot(con, current)
            workspace = current["workspace"]
            atomic.write_text(os.path.join(workspace, graph_path(current)), render_graph(data))
            atomic.write_text(os.path.join(workspace, paths_path(current)), render_paths(data))
            for node in data["nodes"]:
                folder = os.path.join(workspace, runs.node_dir(node["id"]))
                os.makedirs(folder, exist_ok=True)
                atomic.write_text(os.path.join(folder, NODE_VIEW), render_node(data, node["id"]))
            if final:
                atomic.write_text(os.path.join(workspace, runs.run_path(current, GRAPH_SNAPSHOT)),
                                  json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:  # noqa: BLE001 - a view must never undo or stop the research it shows
        _LOG.warning("research %s: graph views could not be written: %s", run["id"], error)
