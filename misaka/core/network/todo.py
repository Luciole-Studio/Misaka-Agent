"""Per-card tools: nested, durable to-do items, and a line in the card's log."""
import secrets
import time

STATUSES = ("open", "doing", "done", "blocked")
MAX_TEXT = 200
MAX_NOTE = 300
MAX_ITEMS = 200


def _flat(s, cap):
    return " ".join(str(s or "").split())[:cap]


def add(con, task_id, text, parent_id=None, owner=None):
    """Add a to-do item to a card; return ``(todo_id, error)``."""
    task = con.execute("SELECT generation FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return None, f"Card not found: {task_id}"
    text = _flat(text, MAX_TEXT)
    if not text:
        return None, "To-do text cannot be empty."
    n = con.execute("SELECT COUNT(*) AS n FROM todos WHERE task_id=?",
                    (task_id,)).fetchone()["n"]
    if n >= MAX_ITEMS:
        return None, f"A task card may have at most {MAX_ITEMS} to-do items; merge related items."
    if parent_id is not None:
        parent = con.execute("SELECT task_id FROM todos WHERE id=?",
                             (parent_id,)).fetchone()
        if parent is None or parent["task_id"] != task_id:
            return None, f"Parent item {parent_id} does not belong to card {task_id}."
    tid = "td_" + secrets.token_hex(3)
    now = int(time.time())
    con.execute(
        "INSERT INTO todos (id, task_id, parent_id, text, status, owner, generation,"
        " created_at, updated_at) VALUES (?,?,?,?,'open',?,?,?,?)",
        (tid, task_id, parent_id, text, _flat(owner, 60) or None,
         int(task["generation"]), now, now))
    return tid, None


def mark(con, task_id, todo_id, status, note=None, owner=None):
    """Update an item owned by the specified task card."""
    if status not in STATUSES:
        return False, f"Status must be one of: {', '.join(STATUSES)}."
    if status == "blocked" and not _flat(note, MAX_NOTE):
        return False, "A blocked item requires a note describing the concrete blocker."
    sets, args = ["status=?", "updated_at=?"], [status, int(time.time())]
    if note is not None:
        sets.append("note=?")
        args.append(_flat(note, MAX_NOTE) or None)
    if owner is not None:
        sets.append("owner=?")
        args.append(_flat(owner, 60) or None)
    cur = con.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id=? AND task_id=?",
                      [*args, todo_id, task_id])
    if cur.rowcount == 0:
        return False, f"Item {todo_id} does not belong to card {task_id}."
    return True, None


def items(con, task_id):
    """Return all items in stable insertion order."""
    return con.execute("SELECT * FROM todos WHERE task_id=? ORDER BY rowid",
                       (task_id,)).fetchall()


def tree(con, task_id):
    """Return nested task items with ``children`` lists."""
    nodes, roots = {}, []
    for r in items(con, task_id):
        node = {**{k: r[k] for k in r}, "children": []}
        nodes[r["id"]] = node
        parent = nodes.get(r["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots


def stats(con, task_id):
    """Return progress counters and active or blocked item summaries."""
    rows = items(con, task_id)
    return {
        "total": len(rows),
        "done": sum(1 for r in rows if r["status"] == "done"),
        "doing": [r["text"] for r in rows if r["status"] == "doing"],
        "blocked": [(r["text"], r["note"] or "") for r in rows
                    if r["status"] == "blocked"],
    }


GLYPH = {"open": "[ ]", "doing": "[~]", "done": "[x]", "blocked": "[!]"}


def render(con, task_id):
    """Render a task's nested to-do list."""
    lines = []

    def walk(nodes, indent):
        for n in nodes:
            owner = f" ({n['owner']})" if n["owner"] else ""
            note = f" ⚠{n['note']}" if n["status"] == "blocked" and n["note"] else ""
            lines.append(f"{indent}{GLYPH[n['status']]} {n['id']} {n['text']}{owner}{note}")
            walk(n["children"], indent + "  ")

    walk(tree(con, task_id), "")
    return "\n".join(lines) or "(No to-do items.)"


NAG_EMPTY_AFTER = 10
NAG_STALE_AFTER = 25


def tools_for(task_id, sender):
    """Return a registrar that adds the ``misaka_todo`` tools and reminder hooks for one task card."""

    def register(harn):
        from typing import Literal

        from pydantic import BaseModel, Field

        from misaka.config import CFG
        from misaka.core.extensions.types import ToolDefinition
        from misaka.core.platform import tasks as bdb

        state = {"con": None, "results": 0, "since_write": 0,
                 "empty_nagged": False, "stale_nags": 0}

        def con():
            if state["con"] is None:
                state["con"] = bdb.connect(CFG["db"])
            return state["con"]

        def _text(s):
            return {"content": [{"type": "text", "text": s}], "details": {}}

        class AddOp(BaseModel):
            text: str = Field(description="Concise, actionable to-do item.")
            parent_id: str | None = Field(None, description="Optional parent to-do ID; omit for a top-level item.")
            owner: str | None = Field(None, description="Optional agent or subagent responsible for this item.")

        class MarkOp(BaseModel):
            id: str = Field(description="To-do item ID.")
            status: Literal["open", "doing", "done", "blocked"] = Field(description="New item status.")
            note: str | None = Field(None, description="Required for blocked items: state the concrete blocker.")
            owner: str | None = Field(None, description="Optional new owner.")

        class TodoParams(BaseModel):
            add: list[AddOp] = Field(default_factory=list, description="Items to add")
            mark: list[MarkOp] = Field(default_factory=list, description="Items to update")

        async def todo_exec(tool_call_id, raw, signal, on_update, ctx):
            p = raw if isinstance(raw, TodoParams) else TodoParams(**(raw or {}))
            out = []
            for op in p.add:
                tid, err = add(con(), task_id, op.text,
                               parent_id=op.parent_id, owner=op.owner)
                out.append(f"+ {tid} {op.text}" if tid else f"✗ {err}")
            for op in p.mark:
                ok, err = mark(con(), task_id, op.id, op.status,
                               note=op.note, owner=op.owner)
                out.append(f"✓ {op.id} → {op.status}" if ok else f"✗ {op.id}: {err}")
            state["since_write"] = 0
            return _text(("\n".join(out) + "\n\n" if out else "")
                         + "Current list:\n" + render(con(), task_id))

        harn.registerTool(ToolDefinition(
            name="misaka_todo", label="Update task to-do list",
            description="Add, assign, and update the task card's nested to-do items; progress appears in the global execution tree.",
            parameters=TodoParams.model_json_schema(), execute=todo_exec,
            promptSnippet="Create or update this task card's to-do list",
            promptGuidelines=[
                "Before substantial work, create a short to-do list and mark one or two immediate items doing.",
                "Update items as work progresses: doing when started, done only after completion.",
                "Use blocked with a concrete note when work cannot proceed, then continue independent items.",
                "When delegating an item, set its owner to the agent name and mark it doing.",
                "Before submission, no item may remain doing: mark it done, return it to open, or block it with a note.",
            ]))

        class ListParams(BaseModel):
            pass

        async def list_exec(tool_call_id, raw, signal, on_update, ctx):
            return _text("Current list:\n" + render(con(), task_id))

        harn.registerTool(ToolDefinition(
            name="misaka_todo_list", label="View task to-do list",
            description="Show this task card's nested to-do items, owners, statuses, and blocker notes.",
            parameters=ListParams.model_json_schema(), execute=list_exec,
            promptSnippet='Check the list of sub-tasks for this card',
            promptGuidelines=["After context compaction, use `misaka_todo_list` to recover the task's current work state."]))

        class MyCardParams(BaseModel):
            pass

        async def my_card_exec(tool_call_id, raw, signal, on_update, ctx):
            from misaka.core.platform import cards
            c = con()
            row = bdb.get(c, task_id)
            state, parents = bdb.dependency_state(c, task_id)
            lines = [f"{row['id']}  {row['status']}  assignee {row['assignee']}"
                     + (f"  reviewer {row['reviewer']}" if row["reviewer"] else ""),
                     f"dependencies: {state}" + (f" ({', '.join(str(p) for p in parents)})" if parents else " (none)"),
                     f"generation {row['generation']}  timeout {row['timeout_seconds']}s"]
            if row["block_reason"]:
                lines.append(f"blocked: {row['block_reason']}")
            log = cards.read_log(row["workspace"], task_id)
            if log:
                lines += ["log:", *(f"  {line}" for line in log[-20:])]
            return _text("\n".join(lines))

        harn.registerTool(ToolDefinition(
            name="misaka_my_card", label="View my card",
            description="This card as the board sees it: status, dependencies and whether they are done, the reviewer "
                        "if one is named, a block reason, and the last lines of the card's log.",
            parameters=MyCardParams.model_json_schema(), execute=my_card_exec,
            promptSnippet="Check this card's status, dependencies, and log",
            promptGuidelines=["Check the card before waiting on something: a blocked dependency or a named reviewer changes what to do next."]))

        class NoteParams(BaseModel):
            text: str = Field(description="One line for the card's log: a decision, a change of course, a dead end.")

        async def note_exec(tool_call_id, raw, signal, on_update, ctx):
            from misaka.core.platform import cards
            p = raw if isinstance(raw, NoteParams) else NoteParams(**(raw or {}))
            row = bdb.get(con(), task_id)
            cards.append_log(row["workspace"], task_id, sender, p.text)
            return _text("Logged on the card.")

        harn.registerTool(ToolDefinition(
            name="misaka_card_note", label="Log a note on the card",
            description="Append one line to this card's log (its `## log` section in the card file): a decision, a change "
                        "of course, a dead end. Last Order reads the log when she looks at the card.",
            parameters=NoteParams.model_json_schema(), execute=note_exec,
            promptSnippet="Log a decision or change of course on this card",
            promptGuidelines=["Log why you changed course or dropped a line of inquiry as it happens; report.json is for the end."]))

        def _field(event, key, default=None):
            try:
                return event.get(key, default)
            except AttributeError:
                return getattr(event, key, default)

        def _nag(text):
            try:
                harn.sendMessage(
                    {"customType": "todo-reminder", "display": True,
                     "content": "[To-do reminder] " + text, "details": {}},
                    {"deliverAs": "followUp", "triggerTurn": False})
            except Exception:  # noqa: BLE001, S110 - reminders must never interrupt work
                pass

        async def on_result(event, _ctx=None):
            name = str(_field(event, "toolName", "") or "")
            if name in ("misaka_todo", "misaka_todo_list"):
                state["since_write"] = 0
                return
            state["results"] += 1
            state["since_write"] += 1
            s = stats(con(), task_id)
            if (not state["empty_nagged"] and s["total"] == 0
                    and state["results"] >= NAG_EMPTY_AFTER):
                state["empty_nagged"] = True
                _nag(
                    f"No to-do list after {state['results']} tool results. Use `misaka_todo` to break this card into trackable steps."
                )
            elif (s["doing"] and state["since_write"] >= NAG_STALE_AFTER
                    and state["stale_nags"] < 2):
                state["stale_nags"] += 1
                state["since_write"] = 0
                _nag(
                    "No to-do activity for a while. Still doing: " + "; ".join(s["doing"][:3])
                    + ". Mark completed work done, or mark a blocker with a note."
                )

        async def on_shutdown(_event=None, _ctx=None):
            if state["con"] is not None:
                state["con"].close()

        harn.on("tool_result", on_result)
        harn.on("session_shutdown", on_shutdown)

    return register
