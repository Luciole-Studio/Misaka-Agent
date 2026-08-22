"""Render the task board and follow its event stream using only the standard library."""
import json
import os
import sys
import time

_COLORS = {"completed": "\033[32m", "failed": "\033[31m", "stopped": "\033[33m", "reclaimed": "\033[33m",
           "verify_reclaimed": "\033[33m",
           "skipped_nonspawnable": "\033[33m", "claimed": "\033[36m", "created": "\033[36m"}
_TTY = sys.stdout.isatty()


def _c(kind, text):
    if _TTY and kind in _COLORS:
        return f"{_COLORS[kind]}{text}\033[0m"
    return text


def _summarize_harn(payload):
    try:
        d = json.loads(payload)
    except Exception:  # noqa: BLE001
        return payload[:120]
    bits = [str(d.get("type", ""))]
    for k in ("toolName", "name"):
        if isinstance(d.get(k), str):
            bits.append(d[k])
    for k in ("text", "delta", "thinking", "error", "errorMessage"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            bits.append(v.strip().splitlines()[0])
            break
    return " ".join(bits)[:160]


def _fmt(row):
    ts = time.strftime("%H:%M:%S", time.localtime(row["created_at"]))
    kind, payload = row["kind"], row["payload"] or ""
    if kind == "harn_event":
        s = _summarize_harn(payload)
        if s.startswith("message_update"):
            return None  # Streaming deltas are noise on screen; the full text is in the ledger.
        return f"{ts}  {row['task_id']}    · {s}"
    return f"{ts}  {row['task_id']}  {_c(kind, kind)}  {payload[:200]}"


def follow(con, since=None, poll=0.5, once=False):
    if since is None:
        since = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    while True:
        rows = con.execute("SELECT * FROM events WHERE id>? ORDER BY id", (since,)).fetchall()
        for r in rows:
            line = _fmt(r)
            if line is not None:
                print(line, flush=True)
            since = r["id"]
        if once:
            return
        time.sleep(poll)


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
