"""Render the task board using only the standard library."""
import os
import sys

_COLORS = {"done": "\033[32m", "failed": "\033[31m", "stopped": "\033[33m", "blocked": "\033[33m", "triage": "\033[33m",
           "running": "\033[36m", "review": "\033[36m"}
_TTY = sys.stdout.isatty()


def _c(kind, text):
    if _TTY and kind in _COLORS:
        return f"{_COLORS[kind]}{text}\033[0m"
    return text


def board_text(con, workspace=None):
    """Board text shared by the /board slash command and the CLI; returned, not printed.

    Cards are grouped by project folder; ``workspace`` limits the board to one folder.
    """
    from misaka.platform import cards
    workspaces = ([workspace] if workspace else
                  [row[0] for row in con.execute("SELECT DISTINCT workspace FROM tasks")])
    for project in workspaces:
        if project:
            cards.rebuild(con, project)
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
        # Pad the plain text first: format widths count ANSI escape bytes as visible columns.
        status = f"{r['status']:<18}"
        out.append(f"{r['id']}  {_c(r['status'], status)}  {r['assignee']:<16} p{r['priority']}  {r['title']}")
    return "\n".join(out)


def board_view(con, workspace=None):
    print(board_text(con, workspace))
