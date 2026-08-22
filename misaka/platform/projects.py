"""Workspace-local Project storage and lifecycle operations."""
from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path


PROJECT_TEMPLATE = """# {name}

## Goal
(One sentence: what this project must find out.)

## Bet
(The counter-intuitive judgement placed before work starts: which mainstream account is wrong. Must be falsifiable.)

## Boundaries
(What is out of scope; what counts as drifting off topic.)

## Division of labor
(Who owns what; card assignments follow this. Example: 10032 = archives and literature; 10033 = data checks.)

## Known gaps
(Not yet explored / explored but unobtainable / structurally absent. Part of the deliverable; update as research proceeds.)
"""


def _workspace(value):
    return str(Path(value or os.getcwd()).expanduser().resolve())


def _name(value):
    value = str(value or "").strip()
    if not value or len(value) > 120 or any(ord(ch) < 32 for ch in value):
        raise ValueError("Project name must contain 1–120 visible characters.")
    return value


def get(con, project_id):
    if not project_id:
        return None
    return con.execute("SELECT * FROM projects WHERE id=?", (str(project_id),)).fetchone()


def resolve(con, value, *, workspace=None):
    """Resolve an id, or an unambiguous display name within one workspace."""
    if not value:
        return None
    if row := get(con, value):
        if workspace and row["workspace"] != _workspace(workspace):
            raise ValueError(f"Project {value!r} does not belong to the current workspace.")
        return row
    args = [str(value)]
    sql = "SELECT * FROM projects WHERE name=?"
    if workspace:
        sql += " AND workspace=?"
        args.append(_workspace(workspace))
    rows = con.execute(sql + " ORDER BY created_at DESC", args).fetchall()
    if not rows:
        raise ValueError(f"Project {value!r} does not exist.")
    if len(rows) > 1:
        ids = ','.join(row["id"] for row in rows)
        raise ValueError(f'Project name "{value}" is not unique; use an ID: {ids}')
    return rows[0]


def require(con, value, *, workspace=None):
    return resolve(con, value, workspace=workspace)["id"] if value else None


def path(con, value):
    return resolve(con, value)["path"]


def exists(con, value, *, workspace=None):
    try:
        row = resolve(con, value, workspace=workspace)
    except ValueError:
        return False
    return bool(row and Path(row["path"]).is_dir())


def listing(con, *, workspace=None):
    if workspace:
        return con.execute(
            "SELECT * FROM projects WHERE workspace=? ORDER BY created_at DESC",
            (_workspace(workspace),),
        ).fetchall()
    return con.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()


def create(con, workspace, name, *, markdown=None):
    workspace, name = _workspace(workspace), _name(name)
    project_id = "p_" + secrets.token_hex(6)
    root = Path(workspace) / ".misaka" / "projects" / project_id
    root.mkdir(parents=True, exist_ok=False)
    try:
        (root / "pageindex").mkdir()
        (root / "runs").mkdir()
        (root / "PROJECT.md").write_text(
            (str(markdown).strip() + "\n") if markdown else PROJECT_TEMPLATE.format(name=name),
            encoding="utf-8",
        )
        con.execute(
            "INSERT INTO projects(id,name,workspace,path,created_at) VALUES(?,?,?,?,?)",
            (project_id, name, workspace, str(root), int(time.time())),
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return get(con, project_id)


def set_state(con, value, *, archived=None, pinned=None, workspace=None):
    row = resolve(con, value, workspace=workspace)
    if archived is not None:
        con.execute("UPDATE projects SET archived=? WHERE id=?", (int(archived), row["id"]))
    if pinned is not None:
        con.execute(
            "UPDATE projects SET pinned_at=? WHERE id=?",
            (time.time() if pinned else None, row["id"]),
        )
    verbs = []
    if archived is not None:
        verbs.append("archived" if archived else "restored")
    if pinned is not None:
        verbs.append("pinned" if pinned else "unpinned")
    return True, f"Project {row['name']!r} was {', '.join(verbs)}."


def delete(con, value, *, with_cards=False, workspace=None):
    row = resolve(con, value, workspace=workspace)
    cards = con.execute("SELECT id,status FROM tasks WHERE project=?", (row["id"],)).fetchall()
    if cards and not with_cards:
        return False, f"Project {row['name']!r} still has {len(cards)} task card(s) and cannot be deleted."
    active = [card["id"] for card in cards
              if card["status"] in {"running", "review", "verifying", "finalizing"}]
    if active:
        return False, f"Project {row['name']!r} has active task cards: {', '.join(active)}"
    from misaka.platform.tasks import delete_task
    for card in cards:
        delete_task(con, card["id"], allow_active=True)
    source = Path(row["path"])
    trash = Path(row["workspace"]) / ".misaka" / "trash" / "projects"
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / f"{row['id']}-{int(time.time())}"
    if source.exists():
        os.replace(source, destination)
    con.execute("DELETE FROM projects WHERE id=?", (row["id"],))
    return True, f"Deleted project {row['name']!r}; files moved to {destination}."
