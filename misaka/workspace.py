"""Workspace navigation across the project brief, task cards, research runs, and indexed artifacts."""
import json
import os
import re

from misaka.core.documents import index as corpus
from misaka.core.platform import cards
from misaka.utils.markdown import atx_headings

_OUTLINE_SCAN_CHARS = 256 * 1024
_OUTLINE_MAX_HEADINGS = 120
_OUTLINE_MAX_TITLE_CHARS = 200
_FOLD_FILES_OVER = 100      # files (artifacts, documents) under one node beyond which they are listed by folder
_FOLDER_KEEPS_FILES = 10    # a folder holding at most this many of them still lists each one


def _doc_node(m, workspace=None):
    """Build a workspace-tree node for an indexed document: its PageIndex structure when it has
    one. A document without one is a leaf; ``doc_outline`` lists its pages. Listing each page's
    first line here read every page of every document on each call -- 140,000 nodes on a
    33,308-document project, a 21 MB index the view cut at 200 KB before the task board
    (2026-10-27, R33)."""
    did = m["doc_id"]
    node = {"node_id": f"doc:{did}", "title": m["title"],
            "summary": f"{m['pages']} pages", "source": m.get("orig_path"), "nodes": []}
    tree = corpus._tree(did, workspace=workspace)
    if tree:
        node["nodes"] = tree
        node["summary"] += " (PageIndex structure)"
    return node


def _artifact_node(row):
    """Keep every artifact navigable; only preview bounded, textual Markdown headings."""
    node = {"node_id": f"artifact:{row['id']}", "title": row["title"],
            "summary": row["kind"], "path": row["path"], "nodes": []}
    if os.path.splitext(row["path"])[1].lower() not in {".md", ".markdown"}:
        return node
    try:
        metadata = json.loads(dict(row).get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return node
    if not isinstance(metadata, dict) or metadata.get("binary"):
        return node
    try:
        with open(row["path"], encoding="utf-8") as f:
            text = f.read(_OUTLINE_SCAN_CHARS + 1)
    except (OSError, UnicodeError):
        return node
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        return node  # A misleading .md suffix must not turn binary controls into prose.
    node["nodes"], truncated = _heading_nodes(text, f"artifact:{row['id']}")
    if truncated:
        node["summary"] += " · heading preview truncated; read the file for more"
    return node


def _heading_nodes(text, node_id):
    """Outline nodes for the Markdown headings in the first ``_OUTLINE_SCAN_CHARS`` of ``text``,
    bounded in count and title length, and whether anything was left out or cut short."""
    truncated = len(text) > _OUTLINE_SCAN_CHARS
    if truncated:
        text = text[:_OUTLINE_SCAN_CHARS].rpartition("\n")[0]  # Only complete physical lines.
    nodes = []
    for index, _level, title in atx_headings(text.split("\n")):
        if not title:
            continue
        if len(nodes) == _OUTLINE_MAX_HEADINGS:
            return nodes, True
        preview = (title if len(title) <= _OUTLINE_MAX_TITLE_CHARS
                   else title[:_OUTLINE_MAX_TITLE_CHARS - 1] + "…")
        truncated |= preview != title
        nodes.append({"node_id": f"{node_id}#L{index + 1}", "title": preview, "summary": f"Line {index + 1}"})
    return nodes, truncated


def _project_file(workspace):
    """Build a heading tree for ``<workspace>/PROJECT.md``, or None when there is none."""
    if not workspace:
        return None
    path = os.path.join(workspace, "PROJECT.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(_OUTLINE_SCAN_CHARS + 1)
    except OSError:
        return None
    children, truncated = _heading_nodes(text, "project")
    summary = "Project brief" + (" · heading preview truncated; read the file for more" if truncated else "")
    return {"node_id": "project", "title": "PROJECT.md", "summary": summary, "path": path, "nodes": children}


def _session_node(node_id, title, path):
    """One conversation of a run: its LCM session id (the session file's header id) and file."""
    from misaka.core.session_manager import readSessionHeader
    if not path:
        return None
    sid = str(readSessionHeader(path).get("id") or "")
    return {"node_id": node_id, "title": title, "summary": f"session_id {sid}" if sid else "session id unknown",
            "path": path}


def _conversation_nodes(bcon, run, research_store):
    """Every conversation this run consists of, so an agent can tell which sessions in the
    context store are this work: each node's Last Order session and each card's Sister session."""
    out = []
    for branch in research_store.nodes(bcon, run["id"]):
        root = research_store.is_root(branch)
        path = branch["session_file"] or (run["root_session"] if root else None)
        node = _session_node(f"session:node:{branch['id']}",
                             f"Node {branch['id']} · Last Order" + (" (root)" if root else ""), path)
        if node:
            out.append(node)
    for task in research_store.tasks(bcon, run["id"]):
        path = task["session_file"]
        if not path:
            row = bcon.execute("SELECT session_file FROM task_runs WHERE task_id=? AND session_file IS NOT NULL "
                               "ORDER BY started_at DESC LIMIT 1", (task["id"],)).fetchone()
            path = row["session_file"] if row else None
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
            bcon, run["id"], run_level=True) if a["kind"] not in {"workspace_index", "workspace_index_json"}]
        branch_nodes = []
        for branch in research_store.nodes(bcon, run["id"]):
            children = [_artifact_node(a) for a in research_store.artifacts(
                bcon, run["id"], branch_id=branch["id"])]
            parents = [row["id"] for row in research_store.parents(bcon, branch["id"])]
            branch_nodes.append({"node_id": f"branch:{branch['id']}",
                                 "title": f"Node {branch['id']}" + (" (root)" if research_store.is_root(branch) else ""),
                                 "summary": f"{branch['status']} · depth {branch['depth']}"
                                            + (f" · parents {', '.join(parents)}" if parents else ""),
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
    # The board first: the view is cut at 200 KB, and on a large project the runs' registered
    # files used to fill all of it before a single card (2026-10-27).
    top_nodes = ([project_node] if project_node else []) + [
        {"node_id": "board", "title": "Task board", "summary": f"{len(card_nodes)} cards",
         "nodes": card_nodes},
        {"node_id": "materials", "title": "Research materials", "summary": f"{len(materials)} documents",
         "nodes": materials},
        {"node_id": "runs", "title": "Research Runs", "summary": str(len(run_nodes)),
         "nodes": run_nodes},
        {"node_id": "ledger", "title": "Evidence ledger", "summary": "Research findings, quotations, and evaluations"},
    ]
    name = (os.path.basename(workspace.rstrip(os.sep)) or workspace) if workspace else ""
    return {"node_id": "ws", "title": f"Workspace{f' · {name}' if name else ''}",
            "nodes": top_nodes}


def render(tree):
    """Render a workspace tree as indented text.

    A node with more than ``_FOLD_FILES_OVER`` files under it lists them by the folder each one
    is in: a folder of a few keeps its files listed, a larger one is one line with its count and
    how to list it (``find`` there; ``doc_list`` with the folder as query for document IDs).
    Every file is either listed or counted under its own folder. Cards of a 33,308-document
    project declared tens of thousands of OCR pages, and their names filled the 200 KB view.
    The tree itself, and the saved JSON index, keep every file."""
    out = []
    _render_lines(tree, 0, out)
    return "\n".join(out)


def _file_location(node):
    """``(kind, path)`` for a registered artifact or an indexed document, else ``(None, None)``."""
    nid = node.get("node_id", "")
    if nid.startswith("artifact:") and node.get("path"):
        return "artifact", node["path"]
    if nid.startswith("doc:") and "#" not in nid and node.get("source"):
        return "doc", node["source"]
    return None, None


def _render_lines(tree, depth, out):
    # Joined once by ``render``: a join at every level copied the whole text per node, which on
    # a 214,587-node project took 746 s against 0.07 s (2026-10-27, R33).
    pad = "  " * depth
    s = tree.get("summary")
    out.append(f"{pad}{tree.get('title','')}  [{tree.get('node_id','')}]" + (f"  — {s}" if s else ""))
    if tree.get("path"):
        out.append(f"{pad}  Path: {tree['path']}")
    kids = tree.get("nodes") or []
    files = [kid for kid in kids if _file_location(kid)[0]]
    if len(files) <= _FOLD_FILES_OVER:
        for kid in kids:
            _render_lines(kid, depth + 1, out)
        return
    for kid in kids:
        if not _file_location(kid)[0]:
            _render_lines(kid, depth + 1, out)
    folders = {}
    for kid in files:
        kind, path = _file_location(kid)
        folders.setdefault((os.path.dirname(path), kind), []).append(kid)
    for (folder, kind), group in sorted(folders.items()):
        if len(group) <= _FOLDER_KEEPS_FILES:
            for kid in group:
                _render_lines(kid, depth + 1, out)
            continue
        where = os.path.join(folder, "")
        listing = (f"{len(group)} registered files, not listed here; find in this folder lists them"
                   if kind == "artifact" else
                   f"{len(group)} indexed documents, not listed here; doc_list with this folder as "
                   "its query gives their IDs")
        out.append(f"{pad}  {where}  — {listing}")
