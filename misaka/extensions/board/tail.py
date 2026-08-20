"""事件流 tail（beeai 轨迹打印器的形状 × SQLite）。纯标准库。"""
import json
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
            return None  # ponytail: 流式增量帧不上屏（库里全有），要看细节读 workspace/session
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


def board_text(con):
    """看板文本（/board 斜杠命令与 CLI 共用；返回串不直接打印）。"""
    rows = con.execute(
        "SELECT id,status,assignee,priority,title,project FROM tasks "
        "ORDER BY project IS NULL, project, created_at").fetchall()   # 按课题分组，未分类垫底
    if not rows:
        return "(板上无卡)"
    out = []
    cur_proj = object()   # 哨兵：与任何 project 值都不同,保证首行必打表头
    for r in rows:
        if r["project"] != cur_proj:
            cur_proj = r["project"]
            out.append(f"\n▌{cur_proj or '(未分类)'}")
        status = r["status"]
        if status == "failed":  # blocked（等输入）如实显示，不冒充 failed
            hit = con.execute(
                "SELECT 1 FROM events WHERE task_id=? AND kind='blocked' "
                "AND id > COALESCE((SELECT MAX(id) FROM events WHERE task_id=? AND kind='failed'), 0) "
                "LIMIT 1", (r["id"], r["id"])).fetchone()
            if hit:
                status = "blocked"
        out.append(f"{r['id']}  {_c(status, status):<18}  {r['assignee']:<16} p{r['priority']}  {r['title']}")
    return "\n".join(out)


def board_view(con):
    print(board_text(con))
