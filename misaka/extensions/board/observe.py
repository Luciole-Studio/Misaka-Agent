"""可观测性件（设计 docs/design/research-mode.md §九，R12）：

- 树装配：LO→卡→分身（含分身的分身）＋「缺口→卡」派生边，一份装配两个出口——
  LO 工具一次性快照（misaka_tree）与格子实时刷新（misaka tree --watch）。
- 现场尾巴：某张卡会话记录的最近 N 条消息（misaka_sister_peek 的读端）。

全部只读、无状态：树从 板＋工作区元数据＋图 现场拼出，不缓存不落盘。
"""
import json
import os

from misaka.extensions.board import db
from misaka.extensions.board import project as project_mod
from misaka.orchestration import budget
from misaka.research.kernel import rounds, store

GLYPH = {"running": "●", "verifying": "◆", "finalizing": "◆", "ready": "○",
         "done": "✓", "failed": "✗", "stopped": "■"}
MAX_DEPTH = 4          # ponytail: 分身递归展示到 4 层，再深不展
MAX_LINES = 200        # 快照上限；实时格子每帧也够用


def _header_id(jsonl_path):
    """会话文件头一行的 id——嵌套分身目录就叫这个名。"""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            head = json.loads(f.readline())
        return head.get("id") if head.get("type") == "session" else None
    except (OSError, ValueError):
        return None


def _agents_under(session_dir, depth=0):
    """目录下的分身概要（读 meta），并顺着各自会话 id 递归嵌套的分身。"""
    if depth >= MAX_DEPTH or not os.path.isdir(session_dir):
        return []
    out = []
    for name in sorted(os.listdir(session_dir)):
        if not (name.startswith("agent-") and name.endswith(".meta.json")):
            continue
        try:
            with open(os.path.join(session_dir, name), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        transcript = meta.get("transcript") or ""
        children = []
        sid = _header_id(transcript) if transcript else None
        if sid:
            children = _agents_under(
                os.path.join(os.path.dirname(transcript), sid, "subagents"), depth + 1)
        out.append({"id": meta.get("agentId") or "?",
                    "desc": (meta.get("description") or "")[:40],
                    "status": meta.get("status") or "?",
                    "children": children})
    return out


def _loose_children(session_dir, depth=0):
    """非分身会话（card-shell 直跑的现场）自己派的分身：顺会话 id 找嵌套目录。"""
    if not os.path.isdir(session_dir):
        return []
    out = []
    for name in sorted(os.listdir(session_dir)):
        if not name.endswith(".jsonl") or name.startswith("agent-"):
            continue
        sid = _header_id(os.path.join(session_dir, name))
        if sid:
            out += _agents_under(os.path.join(session_dir, sid, "subagents"), depth + 1)
    return out


def card_agents(workspace):
    """一张卡现场里的全部分身树（新落位 session/ ＋旧落位 session/sister/）。"""
    if not workspace:
        return []
    root = os.path.join(workspace, "session")
    return (_agents_under(root) + _agents_under(os.path.join(root, "sister"))
            + _loose_children(root))


def _agent_lines(agents, indent):
    lines = []
    for a in agents:
        mark = {"running": "●", "pending": "○", "completed": "✓",
                "failed": "✗", "killed": "■"}.get(a["status"], "·")
        lines.append(f"{indent}⇢ {mark} {a['id']} {a['desc']}（{a['status']}）")
        lines += _agent_lines(a["children"], indent + "   ")
    return lines


def render(con, project=None):
    """整棵树的文本快照。project=None＝全部课题（含未分类）。"""
    rows = con.execute("SELECT * FROM tasks ORDER BY project IS NULL, project, created_at"
                       ).fetchall()
    if project is not None:
        rows = [r for r in rows if r["project"] == project]
    origins = {dst: src for src, dst in con.execute(
        "SELECT src, dst FROM edges WHERE kind='expanded_to'")}
    lines, current = [], object()
    for r in rows:
        if r["project"] != current:
            current = r["project"]
            lines.append(f"▌课题「{current or '(未分类)'}」" + _project_meter(con, current))
        glyph = GLYPH.get(r["status"], "·")
        origin = ""
        if r["id"] in origins:
            node = store.get(con, origins[r["id"]])
            if node is not None:
                origin = f"  ←{node['text'][:32]}"
        lines.append(f"├ {glyph} {r['id']} {r['title'][:44]}（{r['assignee']}·{r['status']}）{origin}")
        lines += _agent_lines(card_agents(r["workspace"]), "│    ")
    if not lines:
        return "(板上无卡)"
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"…（截断，共 {len(lines)} 行）"]
    return "\n".join(lines)


def _project_meter(con, project):
    """课题行的仪表尾巴：轮数（有深研日志才有）／前沿存量／预算档。"""
    if not project:
        return ""
    open_gaps = len(store.nodes(con, kind="gap", status="open", project=project))
    parts = [f"前沿{open_gaps}"]
    md = os.path.join(project_mod.path(project), "PROJECT.md")
    done = rounds.rounds_done(md)
    if done:
        parts.insert(0, f"深研{done}轮")
    from misaka.config import CFG
    parts.append(budget.status(con, CFG.get("token_cap"))["mode"])
    return "  ｜" + " ".join(parts)


def transcript_tail(session_file, limit=40):
    """会话记录的最近 limit 条消息，一行一条（人能读的形状）。读不了返回 None。"""
    try:
        with open(session_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 128 * 1024))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue      # 尾读第一行可能是半截，跳过
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            texts, tools = [], []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                elif block.get("type") == "toolCall":
                    tools.append(block.get("name") or "?")
            if tools:
                out.append(f"[assistant→工具] {', '.join(tools)}")
            if texts:
                out.append("[assistant] " + " ".join(texts)[:200])
        elif role == "user":
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or []
                if isinstance(b, dict) and b.get("type") == "text")
            out.append("[user] " + str(text).strip()[:200])
        elif role in ("toolResult", "tool"):
            first = ""
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    first = block["text"].splitlines()[0][:160]
                    break
            out.append(f"[工具结果] {first}")
    return "\n".join(out[-max(1, int(limit)):]) or None


def peek(con, task_id, limit=40):
    """某张卡现场的输出尾巴。返回 (文本, 错误)。"""
    row = db.get(con, task_id)
    if row is None:
        return None, f"没有这张卡：{task_id}"
    session_file = row["session_file"]
    if not (session_file and os.path.isfile(session_file)):
        from misaka.core.session_manager import find_most_recent_session
        session_file = find_most_recent_session(
            os.path.join(row["workspace"], "session")) if row["workspace"] else None
    if not session_file:
        return None, f"卡 {task_id} 还没有会话现场"
    text = transcript_tail(session_file, limit)
    if text is None:
        return None, f"卡 {task_id} 的现场读不出来"
    return text, None
