"""Render the task board and follow its event stream using only the standard library."""
import os
import sys

_COLORS = {"completed": "\033[32m", "failed": "\033[31m", "stopped": "\033[33m", "reclaimed": "\033[33m",
           "skipped_nonspawnable": "\033[33m", "claimed": "\033[36m", "created": "\033[36m"}
_TTY = sys.stdout.isatty()


def _c(kind, text):
    if _TTY and kind in _COLORS:
        return f"{_COLORS[kind]}{text}\033[0m"
    return text


def board_text(con, workspace=None):
    """Board text shared by the /board slash command and the CLI; returned, not printed.

    Cards are grouped by project folder; ``workspace`` limits the board to one folder.
    """
    sql = "SELECT id,status,assignee,priority,title,workspace FROM tasks"
    args = []
    if workspace:
        sql += " WHERE workspace=?"
        args.append(workspace)
    rows = con.execute(sql + " ORDER BY workspace, created_at", args).fetchall()
    if not rows:
        return "(No task cards on the board.)"
    out = []
    cur = object()   # Sentinel that matches no folder, so the first group always gets a header.
    for r in rows:
        if r["workspace"] != cur:
            cur = r["workspace"]
            out.append(f"\n▌{os.path.basename(cur.rstrip(os.sep)) or cur}")
        status = r["status"]
        if status == "failed":  # A card blocked on input is shown as blocked, not failed.
            hit = con.execute(
                "SELECT 1 FROM events WHERE task_id=? AND kind='blocked' "
                "AND id > COALESCE((SELECT MAX(id) FROM events WHERE task_id=? AND kind='failed'), 0) "
                "LIMIT 1", (r["id"], r["id"])).fetchone()
            if hit:
                status = "blocked"
        out.append(f"{r['id']}  {_c(status, status):<18}  {r['assignee']:<16} p{r['priority']}  {r['title']}")
    return "\n".join(out)


def board_view(con, workspace=None):
    print(board_text(con, workspace))
