"""Workspace navigation across the project brief, task cards, research runs, and indexed artifacts."""
import os

from misaka.documents import index as corpus


def _doc_node(m):
    """Build a workspace-tree node for an indexed document."""
    did = m["doc_id"]
    node = {"node_id": f"doc:{did}", "title": m["title"],
            "summary": f"{m['pages']} pages", "nodes": []}
    tree = corpus._tree(did)
    if tree:
        node["nodes"] = tree
        node["summary"] += " (PageIndex structure)"
        return node
    node["nodes"] = [{"node_id": f"doc:{did}#p{h['page']}", "title": f"p{h['page']}",
                      "summary": h["head"].replace("\n", " ")}
                     for h in corpus.page_heads(did)]
    return node


def _artifact_node(row):
    """Build a cheap heading tree for a Markdown artifact."""
    children = []
    try:
        for lineno, line in enumerate(open(row["path"], encoding="utf-8", errors="replace"), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                if title:
                    children.append({"node_id": f"artifact:{row['id']}#L{lineno}",
                                     "title": title, "summary": f"Line {lineno}"})
    except OSError:
        pass
    return {"node_id": f"artifact:{row['id']}", "title": row["title"],
            "summary": row["kind"], "nodes": children[:120]}


def _project_file(workspace):
    """Build a heading tree for ``<workspace>/PROJECT.md``, or None when there is none."""
    if not workspace:
        return None
    path = os.path.join(workspace, "PROJECT.md")
    if not os.path.isfile(path):
        return None
    children = []
    try:
        for lineno, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                children.append({"node_id": f"project#L{lineno}",
                                 "title": stripped.lstrip("#").strip(),
                                 "summary": f"Line {lineno}"})
    except OSError:
        return None
    return {"node_id": "project", "title": "PROJECT.md", "summary": "Project brief",
            "nodes": children}


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
        for m in by_task.get(t["id"], []):
            kids.append(_doc_node(m))
        card_nodes.append({"node_id": f"task:{t['id']}", "title": f"[{t['id']}] {t['title']}",
                           "summary": t["status"],
                           "nodes": kids})

    materials = [_doc_node(m) for m in all_docs
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
            bcon, run["id"], root_only=True)]
        branch_nodes = []
        for branch in research_store.nodes(bcon, run["id"]):
            children = [_artifact_node(a) for a in research_store.artifacts(
                bcon, run["id"], branch_id=branch["id"])]
            branch_nodes.append({"node_id": f"branch:{branch['id']}",
                                 "title": f"Node {branch['id']}",
                                 "summary": f"{branch['status']} · depth {branch['depth']}",
                                 "nodes": children})
        run_nodes.append({"node_id": f"run:{run['id']}",
                          "title": f"Research Run {run['id']}",
                          "summary": f"{run['status']}/{run['phase']}",
                          "nodes": [
                              {"node_id": f"run:{run['id']}#artifacts", "title": "Artifacts",
                               "summary": str(len(artifact_nodes)), "nodes": artifact_nodes},
                              {"node_id": f"run:{run['id']}#branches", "title": "Research nodes",
                               "summary": str(len(branch_nodes)), "nodes": branch_nodes},
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


def read(bcon, node_id, max_chars=6000, *, workspace=None, research_store=None):
    """Read one workspace node by ID."""
    if node_id.startswith("task:"):
        tid, _, part = node_id[5:].partition("#")
        t = bcon.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        if part == "contract" or not part:
            return (
                f"# [{t['id']}] {t['title']}\nStatus: {t['status']} · Assignee: {t['assignee']}\n\n{t['body'] or ''}"
            )
        return None
    if node_id.startswith("doc:"):
        did, _, page = node_id[4:].partition("#p")
        if page:
            txt = corpus.read_page(did, int(page))
            return txt[:max_chars] if txt else None
        return corpus.read_pages(did, 1, 10 ** 9, max_chars=max_chars) or None
    if node_id.startswith("artifact:"):
        if research_store is None:
            return None
        aid, _, anchor = node_id[9:].partition("#L")
        row = research_store.artifact(bcon, aid)
        if not row:
            return None
        try:
            text = open(row["path"], encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        if not anchor:
            return text[:max_chars]
        lines = text.splitlines()
        start = max(0, int(anchor) - 1)
        level = len(lines[start]) - len(lines[start].lstrip("#")) if start < len(lines) else 0
        end = len(lines)
        for i in range(start + 1, len(lines)):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                next_level = len(stripped) - len(stripped.lstrip("#"))
                if next_level <= level:
                    end = i
                    break
        return "\n".join(lines[start:end])[:max_chars]
    if node_id == "project" or node_id.startswith("project#L"):
        if not _project_file(workspace):
            return None
        _, _, anchor = node_id.partition("#L")
        text = open(os.path.join(workspace, "PROJECT.md"), encoding="utf-8", errors="replace").read()
        if not anchor:
            return text[:max_chars]
        lines = text.splitlines()
        start = max(0, int(anchor) - 1)
        return "\n".join(lines[start:])[:max_chars]
    return None


def render(tree, depth=0, out=None):
    """Render a workspace tree as indented text."""
    out = [] if out is None else out
    pad = "  " * depth
    s = tree.get("summary")
    out.append(f"{pad}{tree.get('title','')}  [{tree.get('node_id','')}]" + (f"  — {s}" if s else ""))
    for kid in (tree.get("nodes") or [])[:60]:
        render(kid, depth + 1, out)
    return "\n".join(out)
