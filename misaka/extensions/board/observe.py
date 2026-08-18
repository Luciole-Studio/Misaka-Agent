"""可观测性件（设计 docs/design/research-mode.md §九，R12）：

- 树装配：LO→卡→分身（含分身的分身）＋「缺口→卡」派生边，一份装配两个出口——
  LO 工具一次性快照（misaka_tree）与格子实时刷新（misaka tree --watch）。
- 现场尾巴：某张卡会话记录的最近 N 条消息（misaka_sister_peek 的读端）。

全部只读、无状态：树从 板＋工作区元数据＋图 现场拼出，不缓存不落盘。
"""
import json
import os

from misaka.extensions.board import db, todo
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


def card_agents(workspace, self_agent_id=None):
    """一张卡现场里的全部分身树（新落位 session/ ＋旧落位 session/sister/）。
    self_agent_id＝这张卡 sister 本人的 agent 号：她不是自己的分身，折叠掉——
    她派的分身直接挂卡下，两条执行路径（LO 会话内跑 / 格子里跑）长同一种树形。"""
    if not workspace:
        return []
    root = os.path.join(workspace, "session")
    agents = (_agents_under(root) + _agents_under(os.path.join(root, "sister"))
              + _loose_children(root))
    if not self_agent_id:
        return agents
    out = []
    for a in agents:
        out += a["children"] if a["id"] == self_agent_id else [a]
    return out


def _agent_lines(agents, indent):
    lines = []
    for a in agents:
        mark = {"running": "●", "pending": "○", "completed": "✓",
                "failed": "✗", "killed": "■"}.get(a["status"], "·")
        lines.append(f"{indent}⇢ {mark} {a['id']} {a['desc']}（{a['status']}）")
        lines += _agent_lines(a["children"], indent + "   ")
    return lines


def _pref(a, b):
    """截断兼容的前缀互认（desc 截 40、owner 截 60、text 截 200，谁短谁当前缀）。"""
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def _attach(agents, trows):
    """2 期合同约定的机械挂接：条目 owner＝分身任务名，或分身描述以条目原文开头。
    挂不上的落回卡下（毛边无害，只是视觉归位）。返回 ({todo_id: [分身]}, 散的)。"""
    by_todo, loose = {}, []
    for a in agents:
        hit = next((t["id"] for t in trows
                    if _pref(t["owner"], a["desc"]) or _pref(t["text"], a["desc"])), None)
        (by_todo.setdefault(hit, []) if hit else loose).append(a)
    return by_todo, loose


def _card_lines(con, r, card_indent, tail_indent):
    """一张卡：状态行（含代办徽标）＋⚠卡壳行＋带分身的条目锚行＋散分身。"""
    trows = todo.items(con, r["id"])
    doing = [t for t in trows if t["status"] == "doing"]
    badge = ""
    if trows:
        done = sum(1 for t in trows if t["status"] == "done")
        badge = (f"  代办{done}/{len(trows)}"
                 + (f" ▶{doing[0]['text'][:20]}" if doing else ""))
    glyph = GLYPH.get(r["status"], "·")
    out = [f"{card_indent}{glyph} {r['id']} {r['title'][:44]}"
           f"（{r['assignee']}·{r['status']}）{badge}"]
    for t in trows:
        if t["status"] == "blocked":
            out.append(f"{tail_indent}⚠ {t['text'][:30]}：{(t['note'] or '')[:40]}")
    by_todo, loose = _attach(card_agents(r["workspace"], r["agent_id"]), trows)
    for t in trows:
        if t["id"] in by_todo:
            out.append(f"{tail_indent}{todo.GLYPH[t['status']]} {t['text'][:40]}")
            out += _agent_lines(by_todo[t["id"]], tail_indent + "   ")
    out += _agent_lines(loose, tail_indent)
    return out


def render(con, project=None):
    """整棵树的文本快照：课题→（方向）→卡→代办/分身。project=None＝全部课题。
    课题里有「缺口→卡」派生边才升出方向层；普通板保持平铺零噪音。"""
    rows = con.execute("SELECT * FROM tasks ORDER BY project IS NULL, project, created_at"
                       ).fetchall()
    if project is not None:
        rows = [r for r in rows if r["project"] == project]
    origins = {dst: src for src, dst in con.execute(
        "SELECT src, dst FROM edges WHERE kind='expanded_to'")}
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r["project"], []).append(r)
    lines = []
    for proj, cards in by_proj.items():
        lines.append(f"▌课题「{proj or '(未分类)'}」" + _project_meter(con, proj))
        if not any(c["id"] in origins for c in cards):
            for c in cards:
                lines += _card_lines(con, c, "├ ", "│    ")
            continue
        groups = {}
        for c in cards:
            groups.setdefault(origins.get(c["id"]), []).append(c)
        for origin_id, group in groups.items():
            node = store.get(con, origin_id) if origin_id else None
            lines.append(f"├ ◆ {node['text'][:44]}" if node is not None else "├ （直派）")
            for c in group:
                lines += _card_lines(con, c, "│ ├ ", "│ │    ")
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
    duty = _duty(project)
    if duty:
        parts.append("分工：" + duty)
    return "  ｜" + " ".join(parts)


def _duty(project):
    """PROJECT.md「## 分工」节的实义行（跳过模板的括号占位行）。"""
    try:
        with open(os.path.join(project_mod.path(project), "PROJECT.md"),
                  encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    got, inside = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s == "## 分工"
        elif inside and s and not s.startswith("（"):
            got.append(s)
    return "；".join(got)[:40]


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
