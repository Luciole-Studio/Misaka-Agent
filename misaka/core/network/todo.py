"""Per-card tools: nested, durable to-do items, and a line in the card's log."""
import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.utils.paths import posix_relpath

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
        node = {**dict(r), "children": []}
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


def _field(event, key, default=None):
    try:
        return event.get(key, default)
    except AttributeError:
        return getattr(event, key, default)


def _assistant_end(event):
    for message in reversed(list(_field(event, "messages", []) or [])):
        if _field(message, "role") != "assistant":
            continue
        content = _field(message, "content", [])
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                str(_field(block, "text", ""))
                for block in (content or [])
                if _field(block, "type") == "text" and _field(block, "text")
            )
        return str(_field(message, "stopReason", "") or ""), text.strip()
    return "", ""


def _turn_failure(event):
    """Why the run's last reply failed, for the stops a headless card fails on
    (``platform.session.run_session``): a provider or runtime error, or a reply that spent the
    whole output cap. ``aborted`` is not one: in a pane it is a person pressing Esc."""
    for message in reversed(list(_field(event, "messages", []) or [])):
        if _field(message, "role") != "assistant":
            continue
        stop = _field(message, "stopReason")
        if stop == "error":
            return "error", str(_field(message, "errorMessage", "") or "request error")
        if stop == "length":
            from misaka.ai.utils._output_limit import output_limit_error
            return "length", output_limit_error(message)
        return None
    return None


PROGRESS_SECONDS = 60     # how often a busy Sister stamps progress on its card
UNSETTLED_GRACE_SECONDS = 30   # how long a card told it is still running may sit with nothing to wake it


def _review_record(submission):
    """What a review card recorded, as a list for its author to check, with its length; None for
    a card that reviews nothing."""
    if "alternatives" in submission:
        items = [f"- [{'gap' if a.get('gap') else 'possibility not taken'}] {a.get('proposal', '')}"
                 for a in submission["alternatives"]]
    elif "issues" in submission:
        items = [f"- [{'material' if i.get('material') else 'minor'}] {i.get('question', '')}"
                 for i in submission["issues"]]
    else:
        return None
    return ("\n".join(items) or "(nothing recorded)"), len(items)


class TodoPart:
    """The ``misaka_todo`` tools and reminder nudges for one task card."""

    _consultation = None                  # (run_id, lock) while a finished card answers in this session

    def __init__(self, task_id, sender):
        from typing import Literal

        from pydantic import BaseModel, Field, create_model

        from misaka.config import CFG
        from misaka.core.extensions.types import ToolDefinition
        from misaka.core.platform import tasks as bdb

        self.task_id = task_id
        self.sender = sender
        self.session = None
        self.commands = []
        self._db_path = CFG["db"]
        self._bdb = bdb
        self._con = None
        self._results = 0
        self._since_write = 0
        self._empty_nagged = False
        self._stale_nags = 0
        self._summary = None
        self._summary_token = None
        self._nudged_generation = None    # the generation already given its one extra turn
        self._held_generation = None      # the generation already told that a turn without misaka_card_complete leaves the card running
        self._turn_clean = False          # the last agent_end was a plain stop (not error/aborted/length)
        self._failure = None              # (stop, reason) when the last agent_end was an error or a cut reply
        self._continued_generation = None # the generation already given its one turn after a cut reply
        self._idle_watch = None           # the wait that ends an attempt left idle after that notice
        self._refusal = None              # what misaka_card_complete last refused with
        self._touched = 0.0
        self._in_flight = 0               # tool calls started and not yet answered
        self._stamper = None              # the task that stamps progress while one is

        def con():
            return self.con()

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
            self._since_write = 0
            return _text(("\n".join(out) + "\n\n" if out else "")
                         + "Current list:\n" + render(con(), task_id))

        class ListParams(BaseModel):
            pass

        async def list_exec(tool_call_id, raw, signal, on_update, ctx):
            return _text("Current list:\n" + render(con(), task_id))

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
                     f"generation {row['generation']}"]
            if row["block_reason"]:
                lines.append(f"blocked: {row['block_reason']}")
            log = cards.read_log(row["workspace"], task_id)
            if log:
                lines += ["log:", *(f"  {line}" for line in log[-20:])]
            return _text("\n".join(lines))

        from misaka.core.network.card_contract import Finding
        from misaka.core.platform import owners

        class BaseNoteParams(BaseModel):
            # A field the tool does not know is refused, never dropped: findings sent flat beside
            # `text` were ignored and every declaration of a run was lost (2026-09-27).
            model_config = {"extra": "forbid"}
            text: str = Field(description="One line for the card's log: a decision, a change of course, a dead end.")
            findings: list[Finding] = Field(default_factory=list, description=(
                "Findings, each with its own text, claim_type and source. Append declarations; corrections do "
                "not erase earlier notes."))
            uncertain: list[str] = Field(default_factory=list, description="Concrete uncertainties, one per item.")

        # The fields of the workflows that own cards (a red team's issues, a divergence review's
        # alternatives), each answered by the owner that declared it.
        owned_fields = {name: (owner, spec) for owner in owners.registered()
                        for name, spec in owner.note_fields().items()}
        NoteParams = create_model("NoteParams", __base__=BaseNoteParams,
                                  **{name: spec for name, (_owner, spec) in owned_fields.items()})

        async def note_exec(tool_call_id, raw, signal, on_update, ctx):
            from misaka.core.platform import cards
            p = raw if isinstance(raw, NoteParams) else NoteParams(**(raw or {}))
            c = con()
            row = bdb.get(c, task_id)
            evidence = {
                "findings": [item.model_dump(exclude_none=True) for item in p.findings],
                "uncertain": [item.strip() for item in p.uncertain if item.strip()],
            }

            def record(kind, payload):
                generation, claim_lock = self._ownership()
                if generation is not None and claim_lock is None:
                    # A review's issues or alternatives are its round's answer; changing them
                    # after the card finished is a re-review, which Last Order asks for.
                    raise ValueError("This card is finished: its review is not revised in an answer to a "
                                     "message. Say what you would change with SendMessage; Last Order "
                                     "asks for a re-review if it should be recorded.")
                return generation is not None and bdb.add_event(
                    c, task_id, kind, {**payload, "claim_lock": claim_lock},
                    generation=generation, claim_lock=claim_lock)

            given = {name: getattr(p, name) for name in owned_fields if getattr(p, name) is not None}
            owner_records = []
            if given:
                card_owner, context = owners.card(c, task_id)
                for owner in dict.fromkeys(owner for name, (owner, _spec) in owned_fields.items() if name in given):
                    mine = {name: value for name, value in given.items() if owned_fields[name][0] is owner}
                    owner_records += owner.note(c, task_id, context if card_owner is owner else None, mine, record)
            if {"findings", "uncertain"} & p.model_fields_set:
                generation, claim_lock = self._ownership()
                # A finished card whose session works on -- a Sister's question, a note from Last
                # Order -- records under the generation it finished, fenced by that generation:
                # its claim went with the completion, and the turn's end declares the findings
                # again with its outputs (``_redeclare_if_done``). Before, they stayed only in the
                # message she sent (2026-10-01, B110).
                finished = self._completed_row() is not None
                if generation is None or not bdb.add_event(
                    c,
                    task_id,
                    "card_evidence",
                    evidence,
                    generation=generation,
                    claim_lock=None if finished else claim_lock,
                ):
                    raise ValueError("Card ownership changed before its evidence was recorded.")
            cards.append_log(row["workspace"], task_id, sender, p.text)
            recorded = [f"{len(evidence['findings'])} finding(s)" if evidence["findings"] else "",
                        f"{len(evidence['uncertain'])} uncertainty(ies)" if evidence["uncertain"] else "",
                        *owner_records]
            recorded = ", ".join(item for item in recorded if item)
            return _text("Logged on the card" + (f"; recorded {recorded}." if recorded else "; nothing declared."))

        class CompleteParams(BaseModel):
            summary: str = Field(description="What the card delivered, for Last Order: the deliverable, the main "
                                             "findings, and what could not be settled.")

        async def complete_exec(tool_call_id, raw, signal, on_update, ctx):
            from misaka.core.network import worker
            p = raw if isinstance(raw, CompleteParams) else CompleteParams(**(raw or {}))
            row = self._owned_row()
            if row is None and (self._consultation is not None or worker.consult_env(task_id)[1] is not None):
                return _text("This card is already finished and stays so: there is nothing to complete. "
                             "Files you changed and findings you noted are declared again when this turn "
                             "ends; answer the message with SendMessage.")
            if row is None:
                raise ValueError("This card is not running under this session; nothing to complete.")
            missing = worker.missing_deliverable(row)
            if missing:
                raise ValueError(f"Cannot complete: the contract deliverable `{missing}` is not under "
                                 f"`{row['output_dir']}` yet. Write it, then call misaka_card_complete again.")
            # The same checks the submission will make, so a red-team card without its issue
            # list hears it now rather than at the end of the turn.
            submission = worker.build_submission(con(), row, p.summary.strip())
            # A review acts on what was recorded, never on the file: a divergence review that
            # wrote five gaps and recorded one left four that nobody answered, and no later
            # review found them (2026-10-01, B117). Once per attempt, before the review is
            # submitted, its author checks her own file against her record.
            recorded = _review_record(submission)
            if recorded is not None and worker.attempt_payload(con(), row, "review_record_checked") is None:
                bdb.add_event(con(), task_id, "review_record_checked", {"items": recorded[1], "claim_lock": row["claim_lock"]},
                              generation=row["generation"], claim_lock=row["claim_lock"])
                raise ValueError(
                    "Before this review is submitted, check the file you wrote against what you recorded "
                    "with misaka_card_note. Only the record is acted on; anything only in the file is lost:\n"
                    + recorded[0] + "\nIf your file names anything this list does not have -- every gap "
                    "included -- call misaka_card_note again with the complete list (it replaces this one). "
                    "Then call misaka_card_complete again.")
            worker.declare_completion(con(), row, p.summary.strip())
            return _text("Completion recorded for this attempt. End the turn now with a short plain-text "
                         "summary; the card is submitted when the turn ends.")

        self.tools = [
            ToolDefinition(
                name="misaka_card_complete", label="Complete the card",
                description="Declare this card's work finished: the contract deliverable is in place and the summary "
                            "is what Last Order will read. The card is submitted when the turn then ends. A turn that "
                            "ends without this call leaves the card running: use that for replies to Last Order and "
                            "progress reports.",
                parameters=typeboxSchema(CompleteParams), execute=complete_exec,
                promptSnippet="Declare the card finished and submit it",
                promptGuidelines=[
                    ("Call `misaka_card_complete` only when the contract deliverable exists in the card's output "
                     "directory and the work is done; then end the turn with a short summary."),
                    ("Answering a message from Last Order or reporting progress does not finish the card: just end "
                     "the turn without the call and keep working on the next one."),
                ]),
            ToolDefinition(
                name="misaka_todo", label="Update task to-do list",
                description="Add, assign, and update the task card's nested to-do items; progress appears in the global execution tree.",
                parameters=typeboxSchema(TodoParams), execute=todo_exec,
                promptSnippet="Create or update this task card's to-do list",
                promptGuidelines=[
                    "Before substantial work, create a short to-do list and mark one or two immediate items doing.",
                    "Update items as work progresses: doing when started, done only after completion.",
                    "Use blocked with a concrete note when work cannot proceed, then continue independent items.",
                    "When delegating an item, set its owner to the agent name and mark it doing.",
                    "Before submission, no item may remain doing: mark it done, return it to open, or block it with a note.",
                ]),
            ToolDefinition(
                name="misaka_todo_list", label="View task to-do list",
                description="Show this task card's nested to-do items, owners, statuses, and blocker notes.",
                parameters=typeboxSchema(ListParams), execute=list_exec,
                promptSnippet='Check the list of sub-tasks for this card',
                promptGuidelines=["After context compaction, use `misaka_todo_list` to recover the task's current work state."]),
            ToolDefinition(
                name="misaka_my_card", label="View my card",
                description="This card as the board sees it: status, dependencies and whether they are done, the reviewer "
                            "if one is named, a block reason, and the last lines of the card's log.",
                parameters=typeboxSchema(MyCardParams), execute=my_card_exec,
                promptSnippet="Check this card's status, dependencies, and log",
                promptGuidelines=["Check the card before waiting on something: a blocked dependency or a named reviewer changes what to do next."]),
            ToolDefinition(
                name="misaka_card_note", label="Log a note on the card",
                description="Append one line to this card's log (its `## log` section in the card file): a decision, a change "
                            "of course, or a dead end. Findings and uncertainty attach here too, and a workflow's card "
                            "attaches the fields its workflow adds.",
                parameters=NoteParams, execute=note_exec,
                promptSnippet="Log a decision or change of course on this card",
                promptGuidelines=["Append findings and uncertainty as work progresses; explain corrections in new notes. Earlier declarations remain visible for review."]),
        ]

    def attach(self, session):
        self.session = session

    def con(self):
        if self._con is None:
            path = (os.environ.get("MISAKA_SISTER_OWNER_DB")
                    or os.environ.get("MISAKA_USAGE_DB") or self._db_path)
            self._con = self._bdb.connect(path)
        return self._con

    def _ownership(self):
        from misaka.core.network import worker
        return worker.attempt_env(self.task_id)

    def _owned_row(self):
        from misaka.core.network import worker
        return worker.owned_card(self.con(), self.task_id, *self._ownership())

    def _declared(self):
        """Whether this attempt has declared its work done with ``misaka_card_complete``."""
        from misaka.core.network import worker
        row = self._owned_row()
        return row is not None and worker.declared_completion(self.con(), row) is not None

    def _completed_row(self):
        """The card this session completed and still sits in: done, at this attempt's
        generation, its recorded session this one. Completion released the claim, so the
        generation and the session file are the ownership here."""
        generation, _ = self._ownership()
        row = self._bdb.get(self.con(), self.task_id)
        if generation is None or row is None or row["status"] != "done" or int(row["generation"]) != generation:
            return None
        mine = getattr(getattr(self.session, "sessionManager", None), "sessionFile", None)
        recorded = row["session_file"]
        if mine and recorded and os.path.realpath(mine) != os.path.realpath(recorded):
            return None
        return row

    async def _redeclare_if_done(self):
        """A finished card whose session went on working declares its outputs again
        (``dispatch.redeclare``)."""
        row = self._completed_row()
        if row is None:
            return False
        from misaka.core.network import dispatch
        return await asyncio.to_thread(dispatch.redeclare, self.con(), row)

    def get_permission_settings(self):
        """Delegate only this live card's output writes, never a workspace-wide grant. A finished
        card answering a consultation in this session keeps her own folder."""
        generation, claim_lock = self._ownership()
        if generation is None or self.session is None:
            return {}
        row = self._bdb.get(self.con(), self.task_id)
        if row is None or int(row["generation"]) != generation or not row["output_dir"]:
            return {}
        running = row["status"] == "running" and row["claim_lock"] == claim_lock
        consulting = self._consultation is not None and row["status"] in self._bdb.CONSULTABLE
        if not (running or consulting):
            return {}
        workspace = Path(self._bdb.workspace_for(row)).resolve()
        output = Path(row["output_dir"]).resolve()
        # A missing/invalid output directory must not fall back to the entire project.
        # Board paths are canonical at creation; a replaced symlink must not move the grant.
        if output != Path(row["output_dir"]) or output == workspace or not output.is_relative_to(workspace):
            return {}
        tools = {name.casefold() for name in self.session.getActiveToolNames()}
        return {"writeDirectories": {name: [str(output)] for name in ("write", "edit") if name in tools}}

    async def agent_start(self, event=None, ctx=None):
        self._cancel_idle_watch()
        self._summary = None
        self._summary_token = None
        row = self._owned_row()
        if row is not None:
            from misaka.core.network import worker

            worker.record_output_baseline(self.con(), row)
        else:
            self._begin_consultation()

    def _begin_consultation(self):
        """A finished card's session starting a turn is answering a consultation: the one the
        daemon opened for the message that woke her (``pane.consult_card``), or -- a person
        typing in her window -- one of her own, so that what she changes is checked and
        declared like any answer. The card itself is not touched (``tasks.start_consultation``)."""
        if self._consultation is not None:
            run = self._bdb.get_run(self.con(), self._consultation[0])
            if run is not None and run["status"] == "running":
                return
            self._consultation = None
        generation, _claim = self._ownership()
        if generation is None:
            return
        con = self.con()
        row = self._bdb.get(con, self.task_id)
        if row is None or row["status"] not in self._bdb.CONSULTABLE or int(row["generation"]) != generation:
            return
        import socket

        from misaka.core.network import worker
        from misaka.core.platform import processes
        pid = os.getpid()
        _gen, run_id, lock = worker.consult_env(self.task_id)
        if run_id is not None:
            run = self._bdb.get_run(con, run_id)
            if run is not None and run["status"] == "running" and run["claim_lock"] == lock:
                self._consultation = (run_id, lock)
                return
        live = self._bdb.live_consultation(con, self.task_id)
        if live is not None:
            # The daemon opens the consultation for a message to her open window before the
            # message reaches this session; the pane's process is this one.
            if int(live["generation"]) == generation and live["pid"] in (None, pid):
                if live["pid"] is None:
                    self._bdb.bind_consultation(con, live["id"], live["claim_lock"], pid,
                                                processes.identity(pid))
                self._consultation = (live["id"], live["claim_lock"])
            return
        lock = f"consult:{socket.gethostname()}:{pid}:{secrets.token_hex(4)}"
        run = self._bdb.start_consultation(con, self.task_id, generation=generation, lock=lock, pid=pid,
                                           identity=processes.identity(pid), reason="typed in her window")
        if run is not None and run["claim_lock"] == lock:
            self._consultation = (run["id"], lock)

    def _end_consultation(self, status, reason=None):
        consultation, self._consultation = self._consultation, None
        if consultation is not None:
            from misaka.core.network import messages
            messages.settle_consultation(self.con(), *consultation, status, reason=reason)

    async def agent_end(self, event, ctx=None):
        reason, text = _assistant_end(event)
        self._turn_clean = reason == "stop"
        self._failure = _turn_failure(event)
        row = self._owned_row() if self._turn_clean else None
        from misaka.core.network import worker
        # A declaration is an event of this attempt's generation: one from an earlier attempt
        # never counts. Only a turn that declared completion submits (2026-09-18, B27): before
        # this, any turn ending in plain text completed the card, so every note Last Order sent
        # a running Sister ended it -- even one whose reply said "not submitting yet".
        completion = worker.declared_completion(self.con(), row) if row is not None else None
        self._summary = (completion or text) if completion is not None else None
        self._summary_token = object() if self._summary is not None else None

    async def agent_settled(self, event=None, ctx=None):
        self._calls_settled()
        failure, self._failure = self._failure, None
        if failure is not None:
            self._settle_failure(*failure)
            return
        summary, token = self._summary, self._summary_token
        if summary is None or token is None:
            if self._turn_clean and not self._declared():
                self._hold_if_running()
                await self._redeclare_if_done()
            if self._consultation is not None:
                # Esc in her window is the only unclean end that is not a failure (_turn_failure).
                await asyncio.to_thread(self._end_consultation, *(
                    ("done",) if self._turn_clean else ("stopped", "interrupted in her window")))
            return
        from misaka.core.subagent import extension as subagent

        while subagent.has_background_tasks() or subagent.has_async_hooks():
            await subagent.wait_for_background_tasks()
            await subagent.wait_for_async_hooks()
            await self.session.agent.waitForIdle()
            if self._summary_token is not token:
                return
        if self._summary_token is not token:
            return
        row = self._owned_row()
        if row is None:
            if self._summary_token is token:
                self._summary = self._summary_token = None
            return
        from misaka.core.network import dispatch, worker

        try:
            submission = worker.build_submission(self.con(), row, summary)
            prepared = await asyncio.to_thread(dispatch.prepare_submission, row, submission)
            if self._summary_token is not token:
                return
            accepted = dispatch.accept_state(
                self.con(),
                row,
                prepared,
                generation=row["generation"],
                claim_lock=row["claim_lock"],
            )
            if accepted:
                await asyncio.to_thread(
                    dispatch.accept_side_effects,
                    self.con(),
                    row,
                    submission,
                    generation=row["generation"],
                )
        except Exception as error:  # noqa: BLE001 - a finalizer error is a real system failure
            accepted = False
            if self._summary_token is not token:
                return
            if (isinstance(error, worker.IncompleteSubmission)
                    and self._nudged_generation != int(row["generation"])
                    and self._owned_row() is not None):
                # What is missing is the model's to supply: one more turn, in this session,
                # before the miss costs an attempt. The second miss fails like any other.
                self._nudged_generation = int(row["generation"])
                self._prompt(f"Submission incomplete: {error} Then call misaka_card_complete again and "
                             "end the turn with the plain-text summary.")
                self._summary = self._summary_token = None
                return
            if self._owned_row() is not None:
                self._bdb.add_event(
                    self.con(), self.task_id, "failed", {"reason": f"finalizer: {error}"},
                    generation=row["generation"], claim_lock=row["claim_lock"],
                )
                self._bdb.mark_failed(
                    self.con(), self.task_id, generation=row["generation"],
                    claim_lock=row["claim_lock"], failure_kind="crash", reason=f"finalizer: {error}",
                )
        if self._summary_token is token and (accepted or self._owned_row() is None):
            self._summary = self._summary_token = None

    def _settle_failure(self, stop, reason):
        """A run that ended in an error or a cut reply, with nothing left to run automatically
        (pi's retries and recovery are behind ``agent_settled``). A pane used to sit on it with
        the card running, its claim heartbeated and nobody to type -- a headless card fails the
        same attempt. A cut reply gets one more turn in this session first, told what happened,
        as a workflow's driver gives Last Order; the second cut fails like an error. The board's
        failure model decides the rest: a backoff and a retry, or a card that stays failed."""
        row = self._owned_row()
        if row is None:
            if self._consultation is not None:
                from misaka.core.network import worker
                if worker.stopped_at_cap(reason):
                    self._end_consultation("stopped", "the run's token budget is spent")
                else:
                    self._end_consultation("failed", reason)
            return
        generation = int(row["generation"])
        from misaka.core.network import worker
        if worker.stopped_at_cap(reason):
            # The run's token cap, not this card: it waits, ready, for a resume.
            if self._bdb.back_to_ready(self.con(), self.task_id, generation=generation,
                                       claim_lock=row["claim_lock"]):
                self._bdb.add_event(self.con(), self.task_id, "budget_stop", {"reason": reason},
                                    generation=generation)
            return
        if stop == "length" and self._continued_generation != generation:
            self._continued_generation = generation
            self._prompt(f"Your last reply was cut off: {reason}. Continue the card from where it "
                         "stopped, writing long material in several smaller steps.")
            return
        if self._bdb.add_event(self.con(), self.task_id, "failed", {"reason": reason},
                               generation=generation, claim_lock=row["claim_lock"]):
            self._bdb.mark_failed(self.con(), self.task_id, generation=generation,
                                  claim_lock=row["claim_lock"], reason=reason)

    def _hold_if_running(self):
        """A clean turn end without ``misaka_card_complete``: the card stays running and the
        claim stays held. Not a failure -- a reply to Last Order or a progress report is
        supposed to end this way -- and said once per attempt so a long card is not nagged
        on every turn. The notice starts one more turn: a card nobody will write to again
        would otherwise wait for ever (2026-09-27: a red team wrote its critique, ended the turn
        without ``misaka_card_complete``, and its node waited forty minutes). After it, the
        same end is watched (``_watch_idle``)."""
        row = self._owned_row()
        if row is None:
            return
        if self._held_generation == int(row["generation"]):
            self._watch_idle(int(row["generation"]))
            return
        from misaka.core.network import worker
        self._held_generation = int(row["generation"])
        missing = worker.missing_deliverable(row)
        detail = (f"its deliverable `{missing}` is not under `{row['output_dir']}` yet"
                  if missing else "the turn ended without `misaka_card_complete`")
        self._hold(f"The card is still running: {detail}. Call `misaka_card_complete` when the work "
                   "is done; if this turn only answered a message, keep working.")

    def _watch_idle(self, generation):
        """A clean end without ``misaka_card_complete`` after the notice: only a message would
        ever wake the card again, and a card whose completion was refused sat running for good
        (2026-10-27). If nothing does within ``UNSETTLED_GRACE_SECONDS`` -- no turn starts, and
        no background agent or shell is left to report back -- the attempt ends unsettled, as
        when a card's process exits without settling (the daemon's ``mark_unsettled``): one
        failure, the next attempt goes on in this conversation told why, and the last one
        leaves the card failed for Last Order to decide."""
        self._cancel_idle_watch()
        self._idle_watch = asyncio.ensure_future(self._unsettle_if_idle(generation))

    def _cancel_idle_watch(self):
        if self._idle_watch is not None:
            self._idle_watch.cancel()
            self._idle_watch = None

    async def _unsettle_if_idle(self, generation):
        from misaka.core.network import worker
        from misaka.core.subagent import extension as subagent

        await asyncio.sleep(UNSETTLED_GRACE_SECONDS)
        while subagent.has_background_tasks() or subagent.has_async_hooks():
            await asyncio.sleep(UNSETTLED_GRACE_SECONDS)     # their notices start her next turn
        if self.session.isStreaming:
            return
        try:
            row = self._owned_row()
            if row is None or int(row["generation"]) != generation:
                return
            self._bdb.mark_unsettled(self.con(), self.task_id, generation=generation, claim_lock=row["claim_lock"],
                                     reason=worker.unsettled_reason(row, self._refusal))
        except Exception:  # logged; the card stays running, as it did before this watch
            logging.getLogger(__name__).warning("Could not end idle card %s unsettled", self.task_id, exc_info=True)

    def _record_artifact(self, value):
        row = self._owned_row()
        if row is None or not value:
            return
        root = Path(row["workspace"]).resolve()
        path = Path(str(value))
        try:
            path = (path if path.is_absolute() else root / path).resolve(strict=True)
            relative = posix_relpath(path, root)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return
        if path.is_file():
            self._bdb.add_event(
                self.con(), self.task_id, "artifact_written", {"path": relative},
                generation=row["generation"], claim_lock=row["claim_lock"],
            )

    def _nag(self, text):
        try:
            self.session.moments.send_message(
                {"customType": "todo-reminder", "display": True,
                 "content": "[To-do reminder] " + text, "details": {}},
                {"deliverAs": "followUp", "triggerTurn": False})
        except Exception:  # noqa: BLE001, S110 - reminders must never interrupt work
            pass

    def _hold(self, text):
        """The turn ended but the card did not: one more turn to finish it or to go on working."""
        try:
            self.session.moments.send_message(
                {"customType": "submission-held", "display": True,
                 "content": "[Card still running] " + text, "details": {}},
                {"deliverAs": "followUp", "triggerTurn": True})
        except Exception:  # noqa: BLE001, S110 - the hold itself is the safe state; the notice is best-effort
            pass

    def _prompt(self, text):
        """A follow-up that starts a turn: the session is idle, and the card needs one more."""
        try:
            self.session.moments.send_message(
                {"customType": "submission-incomplete", "display": True,
                 "content": text, "details": {}},
                {"deliverAs": "followUp", "triggerTurn": True})
        except Exception:  # noqa: BLE001, S110 - without the turn the next settle fails the card
            pass

    def _touch(self):
        """Stamp progress at most once a minute: a tool result is the model getting somewhere,
        and the stamp is what tells a wedged worker from a busy one."""
        now = time.monotonic()
        if now - self._touched < PROGRESS_SECONDS:
            return
        self._touched = now
        self._owned_row()

    async def _stamp_in_flight(self):
        while self._in_flight > 0:
            await asyncio.sleep(PROGRESS_SECONDS)
            if self._in_flight > 0:
                try:
                    self._touch()
                except Exception:  # noqa: BLE001, S110 - a busy board skips one stamp, not the rest
                    pass

    def _calls_settled(self):
        """The turn is over: nothing is in flight, whatever the call/result bookkeeping says."""
        self._in_flight = 0
        if self._stamper is not None:
            self._stamper.cancel()
            self._stamper = None

    async def tool_call(self, event=None, ctx=None):
        """A tool call in flight is progress too: a build that runs an hour, or a foreground
        sub-agent, would otherwise read as a wedged worker while it works. The stamping runs
        until the result lands or the turn ends."""
        try:
            self._in_flight += 1
            self._touch()
            if self._stamper is None or self._stamper.done():
                self._stamper = asyncio.ensure_future(self._stamp_in_flight())
        except Exception:  # noqa: BLE001, S110 - a crash here would fail the call; a stamp is best-effort
            pass

    async def tool_result(self, event, ctx=None):
        self._in_flight = max(0, self._in_flight - 1)
        self._touch()
        name = str(_field(event, "toolName", "") or "")
        if name == "misaka_card_complete":      # what a refusal said, for the reason an idle attempt ends on
            said = " ".join(str(_field(block, "text", "")) for block in (_field(event, "content", []) or [])
                            if _field(block, "type") == "text")
            self._refusal = _flat(said, 300) if _field(event, "isError", False) else None
        if not _field(event, "isError", False):
            raw = _field(event, "input", {}) or {}
            details = _field(event, "details", {}) or {}
            if name in {"write", "edit", "office"}:
                self._record_artifact(_field(raw, "path"))
            elif name == "download_file":
                self._record_artifact(_field(details, "path"))
            elif name in {"web_fetch", "x_search"}:
                self._record_artifact(_field(details, "saved_path"))
            elif name == "web_extract" or name.startswith("browser_"):
                for path in _field(details, "saved_paths", []) or []:
                    self._record_artifact(path)
        if name in ("misaka_todo", "misaka_todo_list"):
            self._since_write = 0
            return
        self._results += 1
        self._since_write += 1
        s = stats(self.con(), self.task_id)
        if (not self._empty_nagged and s["total"] == 0
                and self._results >= NAG_EMPTY_AFTER):
            self._empty_nagged = True
            self._nag(
                f"No to-do list after {self._results} tool results. Use `misaka_todo` to break this card into trackable steps."
            )
        elif (s["doing"] and self._since_write >= NAG_STALE_AFTER
                and self._stale_nags < 2):
            self._stale_nags += 1
            self._since_write = 0
            self._nag(
                "No to-do activity for a while. Still doing: " + "; ".join(s["doing"][:3])
                + ". Mark completed work done, or mark a blocker with a note."
            )

    async def session_shutdown(self, event=None, ctx=None):
        self._calls_settled()
        self._cancel_idle_watch()
        if self._consultation is not None:
            self._end_consultation("stopped", "her session ended before she answered")
        if self._con is not None:
            self._con.close()
            self._con = None


def card_tools(task_id, sender):
    """A card's own tools for a worker that is not a misaka session (an ally, through its MCP
    bridge): the same definitions a Sister's session carries. They need no session -- only the
    board and the claim the process was handed -- and completion is a board event, which the
    host reads when the worker's turn ends."""
    return TodoPart(task_id, sender).tools
