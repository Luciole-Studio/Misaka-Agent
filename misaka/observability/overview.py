"""Read-only project, task-card, to-do, and agent execution-tree rendering."""

import json
import os
import sqlite3

from misaka.platform import tasks as db
from misaka.network import todo
from misaka.platform import projects as project_mod
from misaka.platform import budget

GLYPH = {"running": "●", "review": "◇", "verifying": "◆", "finalizing": "◆", "ready": "○",
         "done": "✓", "failed": "✗", "stopped": "■"}
MAX_DEPTH = 4
MAX_LINES = 200

# Per-card execution pulse derived from the trace parser.
V_GLYPH = {"ok": "✓", "answer": "●", "error": "✗", "deadend": "·", "retry": "↻"}
V_COLOR = {"ok": "\x1b[32m", "answer": "\x1b[32m", "error": "\x1b[31m",
           "deadend": "\x1b[90m", "retry": "\x1b[90m"}
_RESET = "\x1b[0m"
SPARK_W = 24
_lane_cache = {}


def _card_lane(row):
    """Build a card's trace lane from its session file, cached by mtime and size."""
    session_file = row["session_file"]
    if not (session_file and os.path.isfile(session_file)):
        from misaka.core.session_manager import find_most_recent_session
        session_file = find_most_recent_session(os.path.join(db.task_state_dir(row["id"]), "session"))
    if not session_file or not os.path.isfile(session_file):
        return None
    try:
        st = os.stat(session_file)
        key = (st.st_mtime_ns, st.st_size)
        hit = _lane_cache.get(session_file)
        if hit is not None and hit[0] == key:
            return hit[1]
        from misaka.observability.trace import build_lane
        with open(session_file, encoding="utf-8", errors="replace") as f:
            lane = build_lane(f.read(), "l1")
    except ValueError:
        lane = None            # Empty session or no assistant turns: nothing to show.
    except Exception:  # noqa: BLE001
        return None
    _lane_cache[session_file] = (key, lane)
    return lane


def _spark(lane, width=SPARK_W):
    """Render a compact verdict sparkline in execution order."""
    rows = sorted(lane["main"] + lane["detours"], key=lambda n: n["step"])
    marks = [(V_GLYPH.get(n["v"], "·"), n["v"]) for n in rows]
    if len(marks) > width:
        marks = marks[: width - 8] + [("…", None)] + marks[-7:]
    return "".join(f"{V_COLOR[v]}{ch}{_RESET}" if v in V_COLOR else ch
                   for ch, v in marks)


def _pulse_parts(lane, status):
    """Build the sparkline, detour count, elapsed time, and active tool fields."""
    from misaka.observability.trace import fmt_t
    stats = lane["stats"]
    parts = [_spark(lane)]
    if stats["detours"]:
        by = {}
        for d in lane["detours"]:
            by[d["v"]] = by.get(d["v"], 0) + 1
        seg = "".join(f"{V_GLYPH[k]}{v}" for k, v in sorted(by.items()))
        parts.append(f"detours:{stats['detours']}({seg})")
    parts.append(fmt_t(stats["T"]))
    if status == "running":
        live = [t for n in lane["main"] + lane["detours"]
                for t in n["tools"] if t["e"] is None]
        if live:
            parts.append(f"▶{live[-1]['name']}")
    return parts


def _card_pulse(row, indent):
    lane = _card_lane(row)
    if lane is None:
        return []
    return [indent + " ".join(_pulse_parts(lane, row["status"]))]


def _header_id(jsonl_path):
    """Return the session id from a JSONL file's header line, or None."""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            head = json.loads(f.readline())
        return head.get("id") if head.get("type") == "session" else None
    except (OSError, ValueError):
        return None


def _agents_under(session_dir, depth=0):
    """Read sub-agent metadata recursively from a session directory."""
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
    """Find sub-agents nested under plain card-shell session files."""
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


def card_agents(task_id, self_agent_id=None):
    """Return the visible sub-agent tree for a card."""
    if not task_id:
        return []
    root = os.path.join(db.task_state_dir(task_id), "session")
    agents = _agents_under(root) + _loose_children(root)
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
        lines.append(f"{indent}⇢ {mark} {a['id']} {a['desc']} ({a['status']})")
        lines += _agent_lines(a["children"], indent + "   ")
    return lines


def _pref(a, b):
    """Return whether two truncated labels share a compatible prefix."""
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def _attach(agents, trows):
    """Group sub-agents under the to-do item whose owner or text matches their description."""
    by_todo, loose = {}, []
    for a in agents:
        hit = next((t["id"] for t in trows
                    if _pref(t["owner"], a["desc"]) or _pref(t["text"], a["desc"])), None)
        (by_todo.setdefault(hit, []) if hit else loose).append(a)
    return by_todo, loose


def _card_lines(con, r, card_indent, tail_indent):
    """Render a card's status line, pulse, blocked to-dos, and sub-agents."""
    trows = todo.items(con, r["id"])
    doing = [t for t in trows if t["status"] == "doing"]
    badge = ""
    if trows:
        done = sum(1 for t in trows if t["status"] == "done")
        badge = (f"  to-do {done}/{len(trows)}"
                 + (f" ▶{doing[0]['text'][:20]}" if doing else ""))
    glyph = GLYPH.get(r["status"], "·")
    out = [f"{card_indent}{glyph} {r['id']} {r['title'][:44]}"
           f" ({r['assignee']}·{r['status']}){badge}"]
    out += _card_pulse(r, tail_indent)
    for t in trows:
        if t["status"] == "blocked":
            out.append(f"{tail_indent}⚠ {t['text'][:40]}: {(t['note'] or '')[:40]}")
    by_todo, loose = _attach(card_agents(r["id"], r["agent_id"]), trows)
    for t in trows:
        if t["id"] in by_todo:
            out.append(f"{tail_indent}{todo.GLYPH[t['status']]} {t['text'][:40]}")
            out += _agent_lines(by_todo[t["id"]], tail_indent + "   ")
    out += _agent_lines(loose, tail_indent)
    return out


def render(con, project=None):
    """Render the Project -> task card -> to-do -> agent execution tree."""
    rows = con.execute("SELECT * FROM tasks ORDER BY project IS NULL, project, created_at"
                       ).fetchall()
    if project is not None:
        resolved = project_mod.resolve(con, project)
        rows = [r for r in rows if r["project"] == resolved["id"]]
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r["project"], []).append(r)
    lines = []
    for proj, cards in by_proj.items():
        meta = project_mod.get(con, proj) if proj else None
        lines.append(f'▌Project "{(meta["name"] if meta else "(unclassified)")}"'
                     + _project_meter(con, proj))
        for c in cards:
            lines += _card_lines(con, c, "├ ", "│    ")
    if not lines:
        return "(No task cards on the board.)"
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"… ({len(lines)} total lines; output truncated)"]
    return "\n".join(lines)


def _project_meter(con, project):
    """Render a project's research run, open issues, budget mode, and division of labor."""
    if not project:
        return ""
    parts = []
    try:
        run = con.execute(
            "SELECT * FROM research_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (project,),
        ).fetchone()
    except sqlite3.Error:
        run = None
    if run:
        parts.append(f"Research w{run['wave']}·{run['status']}")
        open_count = con.execute(
            "SELECT COUNT(*) FROM research_issues WHERE run_id=? AND status='open'",
            (run["id"],),
        ).fetchone()[0]
        if open_count:
            parts.append(f"open {open_count}")
    from misaka.config import CFG
    parts.append(budget.status(con, CFG.get("token_cap"))["mode"])
    duty = _duty(con, project)
    if duty:
        parts.append("Division of labor: " + duty)
    return ' | ' + " ".join(parts)


def _duty(con, project):
    """Return the substantive lines of PROJECT.md's division-of-labor section, skipping parenthesised placeholders."""
    try:
        with open(os.path.join(project_mod.path(con, project), "PROJECT.md"),
                  encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    got, inside = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s in {"## Division of labor", "## Division of labour"}
        elif inside and s and not s.startswith('('):
            got.append(s)
    return ';'.join(got)[:40]


def transcript_tail(session_file, limit=40):
    """Read a compact human-readable tail from a session JSONL file."""
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
            continue  # the first line may be a partial record after seeking near EOF
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
                out.append(f"[assistant→tool] {', '.join(tools)}")
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
            out.append(f"[tool result] {first}")
    return "\n".join(out[-max(1, int(limit)):]) or None


def peek(con, task_id, limit=40):
    """Return ``(tail text, error)`` for a card's most recent session transcript."""
    row = db.get(con, task_id)
    if row is None:
        return None, f"Card not found: {task_id}"
    session_file = row["session_file"]
    if not (session_file and os.path.isfile(session_file)):
        from misaka.core.session_manager import find_most_recent_session
        session_file = find_most_recent_session(
            os.path.join(db.task_state_dir(task_id), "session"))
    if not session_file:
        return None, f"Card {task_id} has no session yet."
    text = transcript_tail(session_file, limit)
    if text is None:
        return None, f"Card {task_id}'s session could not be read."
    return text, None
