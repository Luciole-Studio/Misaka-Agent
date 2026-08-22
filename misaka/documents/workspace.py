"""Collect verified task artifacts into the canonical document store."""

import json
import os

from misaka.documents import index as corpus
from misaka.config import CFG


def ingest_artifacts(con, task, artifacts=None):
    """Ingest verified task artifacts, rejecting empty or escaping paths."""

    workspace = task["workspace"] or ""
    if artifacts is None:
        try:
            with open(os.path.join(CFG["tasks_root"], task["id"], "report.json"),
                      encoding="utf-8") as handle:
                artifacts = json.load(handle).get("artifacts", [])
        except (OSError, ValueError):
            return []
    project = task["project"] if "project" in task.keys() else None
    project_row = (con.execute("SELECT path FROM projects WHERE id=?", (project,)).fetchone()
                   if project else None)
    root = os.path.realpath(workspace) if workspace else ""
    collected = []
    for relative in artifacts:
        relative = str(relative)
        path = os.path.realpath(os.path.join(root, relative))
        if (
            not root
            or os.path.isabs(relative)
            or not path.startswith(root + os.sep)
            or not os.path.isfile(path)
        ):
            continue
        try:
            doc_id, _pages = corpus.ingest(
                path,
                title=f"[{task['id']}] {relative}",
                project=project,
                project_path=project_row["path"] if project_row else None,
                task_id=task["id"],
            )
        except ValueError:
            continue
        collected.append((doc_id, relative))
    return collected


__all__ = ["ingest_artifacts"]
