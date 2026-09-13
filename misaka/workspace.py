"""Workspace navigation across the project brief, task cards, research runs, and indexed artifacts."""
import os

from misaka.core.documents import index as corpus
from misaka.core.platform import cards


def _doc_node(m, workspace=None):
    """Build a workspace-tree node for an indexed document."""
    did = m["doc_id"]
    node = {"node_id": f"doc:{did}", "title": m["title"],
            "summary": f"{m['pages']} pages", "nodes": []}
    tree = corpus._tree(did, workspace=workspace)
    if tree:
        node["nodes"] = tree
        node["summary"] += " (PageIndex structure)"
        return node
    node["nodes"] = [{"node_id": f"doc:{did}#p{h['page']}", "title": f"p{h['page']}",
                      "summary": h["head"].replace("\n", " ")}
                     for h in corpus.page_heads(did, workspace=workspace)]
    return node


def _artifact_node(row):
    """Build a cheap heading tree for a Markdown artifact."""
    children = []
    try:
        with open(row["path"], encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    title = stripped.lstrip("#").strip()
                    if title:
                        children.append({"node_id": f"artifact:{row['id']}#L{lineno}",
                                         "title": title, "summary": f"Line {lineno}"})
    except OSError:
        pass
    return {"node_id": f"artifact:{row['id']}", "title": row["title"],
            "summary": row["kind"], "path": row["path"], "nodes": children[:120]}


def _project_file(workspace):
    """Build a heading tree for ``<workspace>/PROJECT.md``, or None when there is none."""
    if not workspace:
        return None
    path = os.path.join(workspace, "PROJECT.md")
    if not os.path.isfile(path):
        return None
    children = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    children.append({"node_id": f"project#L{lineno}",
                                     "title": stripped.lstrip("#").strip(),
                                     "summary": f"Line {lineno}"})
    except OSError:
        return None
    return {"node_id": "project", "title": "PROJECT.md", "summary": "Project brief", "path": path,
            "nodes": children}


def _session_node(node_id, title, path):
    """One conversation of a run: its LCM session id (the session file's header id) and file."""
    from misaka.core.session_manager import read_session_header
    if not path:
        return None
    sid = str(read_session_header(path).get("id") or "")
    return {"node_id": node_id, "title": title, "summary": f"session_id {sid}" if sid else "session id unknown",
            "path": path}


def _conversation_nodes(bcon, run, research_store):
    """Every conversation this run consists of, so an agent can tell which sessions in the
    context store are this work: each node's Last Order session and each card's Sister session."""
    out = []
    for branch in research_store.nodes(bcon, run["id"]):
        path = branch["session_file"] or (run["root_session"] if branch["parent_id"] is None else None)
        node = _session_node(f"session:node:{branch['id']}",
                             f"Node {branch['id']} · Last Order" + (" (root)" if branch["parent_id"] is None else ""), path)
        if node:
            out.append(node)
    for task in research_store.tasks(bcon, run["id"]):
        path = task["session_file"]
        if not path:
            row = bcon.execute("SELECT session_file FROM task_runs WHERE task_id=? AND session_file IS NOT NULL "
                               "ORDER BY started_at DESC LIMIT 1", (task["id"],)).fetchone()
            path = row[0] if row else None
        node = _session_node(f"session:card:{task['id']}",
                             f"[{task['id']}] {task['title']} · Sister {task['assignee']} ({task['research_kind']})", path)
        if node:
            out.append(node)
    return out


def outline(bcon, task_id=None, *, workspace=None, run_id=None, research_store=None):
    """Return the workspace tree, optionally limited to a task, a project folder, or a run."""
    if task_id:
        tasks = [bcon.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()]
    elif run_id and research_store is not None:
        tasks = list(research_store.tasks(bcon, run_id))
    elif workspace:
        tasks = bcon.execute("SELECT * FROM tasks WHERE workspace=? ORDER BY created_at",
                             (workspace,)).fetchall()
    else:
        tasks = bcon.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
    tasks = [t for t in tasks if t]
    by_task = {}
    all_docs = corpus.docs(workspace=workspace)
    for m in all_docs:
        task_ids = m.get("task_ids") or ([m["task_id"]] if m.get("task_id") else [])
        for linked_task in task_ids:
            by_task.setdefault(linked_task, []).append(m)

    card_nodes = []
    for t in tasks:
        kids = [{"node_id": f"task:{t['id']}#contract", "title": "Task contract (goal, boundaries, acceptance criteria)",
                 "summary": (t["body"] or "")[:80].replace("\n", " ")}]
        if t["output_dir"]:
            kids.append({"node_id": f"task:{t['id']}#output", "title": "Deliverable directory", "path": t["output_dir"]})
        for m in by_task.get(t["id"], []):
            kids.append(_doc_node(m, workspace))
        card_nodes.append({"node_id": f"task:{t['id']}", "title": f"[{t['id']}] {t['title']}",
                           "summary": t["status"], "path": cards.card_path(t["workspace"], t["id"]),
                           "nodes": kids})

    materials = [_doc_node(m, workspace) for m in all_docs
                 if not (m.get("task_ids") or m.get("task_id"))]

    run_nodes = []
    selected_runs = (
        ([research_store.get(bcon, run_id)] if run_id
         else research_store.listing(bcon, workspace=workspace))
        if research_store is not None
        else []
    )
    for run in [r for r in selected_runs if r]:
        artifact_nodes = [_artifact_node(a) for a in research_store.artifacts(
            bcon, run["id"], root_only=True) if a["kind"] not in {"workspace_index", "workspace_index_json"}]
        branch_nodes = []
        for branch in research_store.nodes(bcon, run["id"]):
            children = [_artifact_node(a) for a in research_store.artifacts(
                bcon, run["id"], branch_id=branch["id"])]
            branch_nodes.append({"node_id": f"branch:{branch['id']}",
                                 "title": f"Node {branch['id']}",
                                 "summary": f"{branch['status']} · depth {branch['depth']}",
                                 "nodes": children})
        conversation_nodes = _conversation_nodes(bcon, run, research_store)
        run_nodes.append({"node_id": f"run:{run['id']}",
                          "title": f"Research Run {run['id']}",
                          "summary": f"{run['status']}/{run['phase']}",
                          "nodes": [
                              {"node_id": f"run:{run['id']}#artifacts", "title": "Artifacts",
                               "summary": str(len(artifact_nodes)), "nodes": artifact_nodes},
                              {"node_id": f"run:{run['id']}#branches", "title": "Research nodes",
                               "summary": str(len(branch_nodes)), "nodes": branch_nodes},
                              {"node_id": f"run:{run['id']}#conversations", "title": "Conversations",
                               "summary": f"{len(conversation_nodes)} sessions of this run; sessions not listed here "
                                          "belong to other work",
                               "nodes": conversation_nodes},
                          ]})

    project_node = _project_file(workspace)
    top_nodes = ([project_node] if project_node else []) + [
        {"node_id": "runs", "title": "Research Runs", "summary": str(len(run_nodes)),
         "nodes": run_nodes},
        {"node_id": "board", "title": "Task board", "summary": f"{len(card_nodes)} cards",
         "nodes": card_nodes},
        {"node_id": "materials", "title": "Research materials", "summary": f"{len(materials)} documents",
         "nodes": materials},
        {"node_id": "ledger", "title": "Evidence ledger", "summary": "Research findings, quotations, and evaluations"},
    ]
    name = (os.path.basename(workspace.rstrip(os.sep)) or workspace) if workspace else ""
    return {"node_id": "ws", "title": f"Workspace{f' · {name}' if name else ''}",
            "nodes": top_nodes}


def render(tree, depth=0, out=None):
    """Render a workspace tree as indented text."""
    out = [] if out is None else out
    pad = "  " * depth
    s = tree.get("summary")
    out.append(f"{pad}{tree.get('title','')}  [{tree.get('node_id','')}]" + (f"  — {s}" if s else ""))
    if tree.get("path"):
        out.append(f"{pad}  Path: {tree['path']}")
    for kid in tree.get("nodes") or []:
        render(kid, depth + 1, out)
    return "\n".join(out)
