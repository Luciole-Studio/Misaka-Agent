"""Last Order's task-board tools.

Last Order plans and delegates through task cards. Starting model work is a separate, user-confirmed action.
"""
import asyncio
import json
import os
import secrets
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.config import CFG, sisters
from misaka.core.extensions.types import ToolDefinition
from misaka.network import validate
from misaka.network.sister_runtime import ACTIVE_BOARD_STATUSES, SisterRuntime
from misaka.platform import budget, notifications
from misaka.platform import tasks as db

_CON = None
TaskId = Annotated[str, Field(pattern=r"^t_[0-9a-f]{6}$")]


def _session_id(ctx):
    """This conversation's session id: stamped on the cards it creates, so a resumed
    conversation knows which Sisters are hers."""
    return getattr(getattr(ctx, "sessionManager", None), "sessionId", None) or None


def _session_line(ctx, limit=10):
    """This conversation's session id plus the ids it was forked from (header
    ``parentSession`` chain): a fork mints a new id, but the cards stamped before the
    fork still belong to this line."""
    ids = [i for i in (_session_id(ctx),) if i]
    current = getattr(getattr(ctx, "sessionManager", None), "sessionFile", None)
    if not current:
        return ids
    from misaka.core.session_manager import read_session_header
    for _ in range(limit):
        current = (read_session_header(current) or {}).get("parentSession")
        if not current:
            break
        parent_id = (read_session_header(current) or {}).get("id")
        if parent_id and parent_id not in ids:
            ids.append(parent_id)
    return ids


def _card_log(workspace, task_id, author, text):
    """Append to the card file's ## log; a stray index row without a file must not fail the tool."""
    from misaka.platform import cards as card_files
    try:
        card_files.append_log(workspace, task_id, author, text)
    except OSError:
        pass


def _mirror_card(workspace, task_id, **fields):
    """Reflect an at-rest change onto the card file; a stray index row without a file
    (pre-migration) must not fail the tool -- the board marks it instead."""
    from misaka.platform import cards as card_files
    try:
        card_files.set_fields(workspace, task_id, **fields)
    except OSError:
        pass


def _pane_for_card(task_id):
    """The live pane running or showing this card, if any (panel mode only)."""
    from misaka.ui.panel import client as net
    return next((p for p in net.request("panes.list")["panes"]
                 if p.get("card") == task_id and p.get("alive")), None)


def _cfg():
    return CFG


def _con():
    global _CON
    if _CON is None:
        _CON = db.connect(CFG["db"])
    return _CON


def _sisters():
    return sorted(sisters())


def _workspace(ctx):
    return db.canonical_workspace(getattr(ctx, "cwd", None))


def _text(s):
    return {"content": [{"type": "text", "text": s}], "details": {}}


def _schema(model):
    "Convert a Pydantic model to the JSON schema expected by the harness."
    return model.model_json_schema()


def _register(harn, name, label, description, parameters, snippet=None, guidelines=None):
    """Decorator: register ``fn`` as a harness tool whose raw arguments are parsed into ``parameters``."""
    def deco(fn):
        async def execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, parameters) else parameters(**(raw or {}))
            return await fn(tool_call_id, args, signal, on_update, ctx)
        harn.registerTool(ToolDefinition(
            name=name, label=label, description=description,
            parameters=_schema(parameters), execute=execute,
            promptSnippet=snippet, promptGuidelines=list(guidelines or []),
        ))
        return fn
    return deco


def register(harn):
    runtime = SisterRuntime(harn, _con, _cfg)

    class StrictParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class BoardParams(BaseModel):
        status: str | None = Field(None, description=(
            "Optional task status: todo, ready, running, blocked, triage, review, "
            "done, failed, or stopped."
        ))


    @_register(
        harn,
        name="misaka_board", label='board',
        description="List this project folder's task cards and their states, the Sister roster, and token usage.",
        snippet="View the Misaka task board and Sister roster",
        parameters=BoardParams)
    async def misaka_board(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        workspace = _workspace(ctx)
        from misaka.platform import cards as card_files
        rows = card_files.board(con, workspace)
        if params.status:
            rows = [r for r in rows if r["status"] == params.status]
        rows = rows[-40:]
        mine = _session_line(ctx)
        lines = [f"{'*' if r['origin_session'] in mine else ' '} {r['id']}  "
                 f"{r['status']:<10} {r['assignee']:<14} {r['title'][:50]}"
                 + ("  (no card file: run `misaka init --migrate`)" if r.get("missing_file") else "")
                 for r in rows]
        if mine and any(line.startswith("*") for line in lines):
            lines.append("* = created in this conversation")
        b = budget.status(con, _cfg()["token_cap"])
        from misaka.network import roster as roster_mod
        named = ", ".join(
            f"{s} ({roster_mod.describe_line(s, root=_cfg()['profiles_root']) or 'no description'})"
            for s in _sisters())
        header = (
            f"Sister roster: {named or '(empty)'}"
            f"{' (use misaka_sister_view for full profiles)' if named else ''}\n"
            f"Budget used: {b['used']:,} tokens ({b['mode']} mode)\n\n"
        )
        return _text(header + ("\n".join(lines) if lines else "(no task cards)"))


    class CardParams(BaseModel):
        title: str = Field(description="Short task-card title.")
        body: str = Field(description="Task contract: `## goal`, `## boundaries`, and a required, testable `## acceptance criteria` section.")
        assignee: str = Field(description="Sister ID from the `misaka_board` roster.")
        reviewer: str | None = Field(
            None, description="Optional independent reviewer; must be a different Sister from the assignee."
        )
        priority: int = Field(0, description="Relative priority; higher values run first.")


    @_register(
        harn,
        name="misaka_card", label="Create task card",
        description="Create a durable Sister task with explicit scope and testable acceptance criteria; this does not start work.",
        snippet="Create a Sister task card with goal, boundaries, and acceptance criteria",
        guidelines=[
            "The body should contain `## goal` and `## boundaries`, and must contain `## acceptance criteria` with verifiable outcomes (the only section that is validated).",
            "After creating cards, show the plan and wait for explicit user approval before calling `misaka_dispatch`.",
        ],
        parameters=CardParams)
    async def misaka_card(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        cards, errs = validate.validate_cards(
            [{"title": params.title, "body": params.body, "assignee": params.assignee,
              "priority": params.priority}], set(_sisters()))
        if errs:
            raise ValueError(';'.join(errs))
        if params.reviewer and params.reviewer not in set(_sisters()):
            raise ValueError(f"Reviewer {params.reviewer} is not in the Sister roster.")
        if params.reviewer == params.assignee:
            raise ValueError("The reviewer must be different from the assignee.")
        c = cards[0]
        from misaka.platform import cards as card_files
        tid = card_files.create(con, _workspace(ctx), c["title"], c["body"], c["assignee"],
                                priority=c["priority"], timeout_seconds=c["timeout"],
                                reviewer=params.reviewer, origin_session=_session_id(ctx))
        review = f" → reviewer {params.reviewer}" if params.reviewer else ""
        return _text(
            f"Added {tid}: {c['title']} → {c['assignee']}{review}.\n"
            "Work has not started. Wait for explicit user approval, then call "
            "misaka_dispatch or misaka_sister."
        )


    class ReviewConfigParams(StrictParams):
        task_id: TaskId
        reviewer: str | None = Field(
            None, description="Independent reviewer Sister ID; use null to remove review from an unstarted card."
        )

    @_register(
        harn,
        name="misaka_card_request_review", label="Configure independent review",
        description="Set or clear the independent reviewer for a todo or ready task card.",
        snippet="Configure a task card's independent reviewer",
        parameters=ReviewConfigParams)
    async def misaka_card_request_review(tool_call_id, params, signal, on_update, ctx):
        if params.reviewer and params.reviewer not in set(_sisters()):
            return _text(f"Reviewer {params.reviewer} is not in the Sister roster.")
        row = db.get(_con(), params.task_id)
        if row is None:
            return _text(f"Task card not found: {params.task_id}")
        if params.reviewer == row["assignee"]:
            return _text("The reviewer must be different from the assignee.")
        if not db.configure_review(_con(), params.task_id, params.reviewer):
            return _text(f"Task card {params.task_id} is {row['status']}; review can be configured only while todo or ready.")
        _mirror_card(row["workspace"], params.task_id, reviewer=params.reviewer)
        reviewer = params.reviewer or "none"
        return _text(f"Task card {params.task_id} reviewer: {reviewer}.")


    class ReviewDecisionParams(StrictParams):
        task_id: TaskId
        reviewer: str = Field(description="Reviewing Sister ID; must match the card's configured reviewer.")
        decision: Literal["approve", "request_changes"]
        feedback: str = Field("", description="Review summary; required when requesting changes.")

    @_register(
        harn,
        name="misaka_card_review", label="Submit independent review",
        description="Approve a reviewed submission or return it to the original Sister with specific requested changes.",
        snippet="Submit an independent task-card review",
        parameters=ReviewDecisionParams)
    async def misaka_card_review(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        row = db.get(con, params.task_id)
        if row is None:
            return _text(f"Task card not found: {params.task_id}")
        if row["reviewer"] != params.reviewer:
            return _text(
                f"Task card reviewer is {row['reviewer'] or 'none'}, not {params.reviewer}."
            )
        if params.decision == "request_changes" and not params.feedback.strip():
            return _text("`request_changes` requires specific feedback.")
        lock = f"review:{params.reviewer}:{secrets.token_hex(4)}"
        if not db.claim_review(
            con, params.task_id, params.reviewer, lock,
            generation=int(row["generation"]), reviewer_identity="board-tool",
        ):
            return _text(f"Task card {params.task_id} is {row['status']} or its review is already claimed.")
        try:
            if params.decision == "approve":
                changed = db.approve_review(
                    con, params.task_id, lock, generation=int(row["generation"]),
                    summary=params.feedback,
                )
                if changed:
                    from misaka.network import dispatch
                    dispatch.index_after_review(con, params.task_id, int(row["generation"]))
            else:
                changed = db.request_review_changes(
                    con, params.task_id, lock, params.feedback,
                    generation=int(row["generation"]),
                )
        except Exception:
            db.release_review(con, params.task_id, lock, generation=int(row["generation"]))
            raise
        if not changed:
            return _text("Review ownership expired; no decision was recorded.")
        if params.decision == "approve":              # the card is done; there is nothing to relaunch
            state = db.get(con, params.task_id)["status"]
            return _text(f"Review recorded: approve; the card is done ({state}).")
        follow = await runtime.launch_ready(
            context=ctx, tool_call_id=tool_call_id, on_update=on_update,
            task_ids=[params.task_id],
        )
        state = follow[0].get("status") if follow else db.get(con, params.task_id)["status"]
        outcome = "the card is done" if params.decision == "approve" else "sent back for revision"
        return _text(f"Review recorded: {params.decision}; {outcome} ({state}).")


    class DispatchParams(StrictParams):
        confirmed: bool = Field(description="True only when the user explicitly approved starting model work.")
        task_ids: list[TaskId] | None = Field(
            None, description="Optional list of ready task IDs; omit to start every ready task."
        )


    @_register(
        harn,
        name="misaka_dispatch", label="Start approved work",
        description="Start ready Sister task cards. This spends model quota and requires explicit user approval.",
        snippet="Start ready task cards after explicit user approval",
        guidelines=["Do not call `misaka_dispatch` until the user explicitly says to start, run, or execute the planned work."],
        parameters=DispatchParams)
    async def misaka_dispatch(tool_call_id, params, signal, on_update, ctx):
        if not params.confirmed:
            return _text("Work was not started. Show the plan and wait for explicit user approval.")
        con = _con()
        ready = len(db.by_status(con, "ready"))
        if not ready:
            return _text("No task cards are ready to run.")
        if on_update:
            on_update({"content": [{"type": "text", "text": f"Starting {ready} card(s)…"}], "details": {}})
        if os.environ.get("MISAKA_NET_PANE"):
            # Start worker cards in visible panes, a tab each.
            from misaka.ui.panel import client as net
            wanted = set(params.task_ids or [])
            lines, started = [], 0
            for row in db.fair_ready(con, lane="workers"):
                if wanted and row["id"] not in wanted:
                    continue
                try:
                    out = await asyncio.to_thread(
                        net.request, "pane.run_card",
                        {"task_id": row["id"], "place": {"tab": os.environ["MISAKA_NET_PANE"]}})
                    started += 1
                    lines.append(f"""  {row['id']} → {row['assignee']}  pane {out['pane_id']}""")
                except Exception as error:  # noqa: BLE001 - report individual launch failures
                    lines.append(f"  {row['id']} failed to start: {error}")
            return _text(f"Started {started} card(s) in network panes:\n" + "\n".join(lines))
        results = await runtime.launch_ready(
            context=ctx,
            tool_call_id=tool_call_id,
            on_update=on_update,
            task_ids=params.task_ids,
        )
        if not results:
            return _text("No cards were started.")
        lines = []
        for item in results:
            if item.get("status") == "error":
                lines.append(f"  {item['task_id']} failed to start: {item['error']}")
            else:
                lines.append(
                    f"  {item['task_id']} → {item['sister']}  {item['status']}"
                )
        started = sum(item.get("status") != "error" for item in results)
        return _text(
            f"Started {started} card(s); completion handling is automatic:\n"
            + "\n".join(lines)
        )


    class SisterParams(StrictParams):
        task_id: TaskId = Field(description="Ready task-card ID to start.")
        confirmed: bool = Field(description="True only when the user explicitly approved starting model work.")


    @_register(
        harn,
        name="misaka_sister", label="Start Sister task",
        description="Start one ready task card and return its durable task ID. This is separate from generic subagents.",
        snippet="Start one Sister task after explicit user approval",
        guidelines=["Start only user-approved cards; manage them with `misaka_sister_output`, `misaka_sister_message`, and `misaka_sister_stop`."],
        parameters=SisterParams)
    async def misaka_sister(tool_call_id, params, signal, on_update, ctx):
        if not params.confirmed:
            return _text("Work was not started. Show the plan and wait for explicit user approval.")
        row = db.get(_con(), params.task_id)
        if os.environ.get("MISAKA_NET_PANE") and row is not None and row["status"] == "ready":
            from misaka.ui.panel import client as net
            out = await asyncio.to_thread(
                net.request, "pane.run_card",
                {"task_id": params.task_id, "place": {"tab": os.environ["MISAKA_NET_PANE"]}})
            return _text(f"Card {params.task_id} started in pane {out['pane_id']}.")
        result = await runtime.launch(
            params.task_id,
            context=ctx,
            tool_call_id=tool_call_id,
            on_update=on_update,
        )
        return _text(json.dumps(result, ensure_ascii=False))


    class SisterOutputParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        block: bool = Field(True, description="Wait for a terminal acceptance state when true.")
        timeout: int = Field(30_000, ge=0, le=600_000, description="Maximum wait in milliseconds.")


    @_register(
        harn,
        name="misaka_sister_output", label="Get Sister result",
        description="Inspect a Sister task or wait for its accepted, failed, or stopped result.",
        snippet="Inspect or wait for a Sister task",
        guidelines=["Progress arrives as a <sister-notification>; do not poll. Read a result with misaka_sister_output after the notification or when the user asks; misaka_sister_peek is for diagnosing a stuck task, not for progress checks."],
        parameters=SisterOutputParams)
    async def misaka_sister_output(tool_call_id, params, signal, on_update, ctx):
        result = await runtime.output(
            params.task_id,
            block=params.block,
            timeout_ms=params.timeout,
            signal=signal,
        )
        return _text(json.dumps(result, ensure_ascii=False))


    class SisterMessageParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        message: str = Field(description="Full message to send into the task's existing Sister session.")
        summary: str = Field(description="Short, nonempty summary shown in the UI.")
        confirmed: bool = Field(
            False,
            description="Set true only when the user approved starting a new model turn for a completed task.",
        )

        @field_validator("message", "summary")
        @classmethod
        def nonempty(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("must not be empty")
            return value


    @_register(
        harn,
        name="misaka_sister_message", label="Message Sister",
        description="Steer a running Sister or continue its durable session with the same task ID, workspace, and transcript.",
        snippet="Send guidance to an existing Sister task",
        parameters=SisterMessageParams)
    async def misaka_sister_message(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Card not found: {params.task_id}")
        in_panel = bool(os.environ.get("MISAKA_NET_PANE"))
        net_owned = str(row["claim_lock"] or "").startswith("net:")
        if net_owned or (in_panel and await asyncio.to_thread(_pane_for_card, params.task_id)):
            # A network-owned or reopened task receives steering through its live pane.
            from misaka.ui.panel import client as net
            await asyncio.to_thread(
                net.request, "pane.send",
                {"card": params.task_id, "text": params.message, "enter": True})
            _card_log(row["workspace"], params.task_id, "last-order", f"[steer] {params.message}")
            return _text(f"Message sent to card {params.task_id}'s pane.")
        if in_panel and row["status"] not in ACTIVE_BOARD_STATUSES:
            # Her session is closed: reopen it beside Last Order with the message as its first
            # turn. Like the headless path, a finished card only restarts on the user's nod.
            if not params.confirmed:
                return _text(f"Card {params.task_id} is {row['status']}; continuing it starts a new "
                             "model turn. Ask the user, then call again with confirmed=true.")
            from misaka.ui.panel import client as net
            out = await asyncio.to_thread(
                net.request, "pane.continue_card",
                {"task_id": params.task_id, "place": {"tab": os.environ["MISAKA_NET_PANE"]},
                 "say": params.message})
            _card_log(row["workspace"], params.task_id, "last-order", f"[message] {params.message}")
            return _text(f"Continued card {params.task_id} in pane {out['pane_id']} (a new attempt "
                         "under this session's claim) and delivered the message.")
        result = await runtime.message(
            params.task_id,
            params.message,
            summary=params.summary,
            confirmed=params.confirmed,
            context=ctx,
        )
        row = db.get(_con(), params.task_id)
        if row is not None:
            _card_log(row["workspace"], params.task_id, "last-order",
                      f"[{result.get('mode') or 'message'}] {params.message}")
        return _text(json.dumps(result, ensure_ascii=False))


    class CardTodosParams(StrictParams):
        task_id: TaskId = Field(description="Card whose to-do list to show.")


    @_register(
        harn,
        name="misaka_card_todos", label="View a card's to-do list",
        description="Show a Sister card's nested to-do items, owners, statuses, and blocker notes as she keeps them.",
        snippet="Check a card's to-do list",
        parameters=CardTodosParams)
    async def misaka_card_todos(tool_call_id, params, signal, on_update, ctx):
        from misaka.network import todo
        return _text(todo.render(_con(), params.task_id))


    class SisterResumeParams(StrictParams):
        task_id: TaskId = Field(description="Card whose saved Sister session to reopen.")


    @_register(
        harn,
        name="misaka_sister_resume", label="Reopen Sister session",
        description="Reopen a finished, failed, stopped, or blocked card's saved Sister session in a tab of its own, without starting a model turn. Steer her afterwards with misaka_sister_message.",
        snippet="Reopen a finished Sister task's session in its own tab",
        guidelines=["After a resumed conversation, bring back only the cards listed as yours in the <resume-briefing>; a card that is already open in a pane is not reopened."],
        parameters=SisterResumeParams)
    async def misaka_sister_resume(tool_call_id, params, signal, on_update, ctx):
        if not os.environ.get("MISAKA_NET_PANE"):
            return _text(f"No panel, so there is no pane to reopen card {params.task_id} in. To continue her, "
                         "send the next instruction with misaka_sister_message (confirmed=true); it resumes her session.")
        live = await asyncio.to_thread(_pane_for_card, params.task_id)
        if live:
            return _text(f"Card {params.task_id} is already open in pane {live['id']}.")
        from misaka.ui.panel import client as net
        out = await asyncio.to_thread(
            net.request, "pane.open_card_session",
            {"task_id": params.task_id, "place": {"tab": os.environ["MISAKA_NET_PANE"]}})
        return _text(f"Reopened card {params.task_id}'s session in pane {out['pane_id']} (a tab of its own).")


    async def resume_briefing(event, ctx):
        """A resumed conversation is told which cards it created and where they stand, so Last
        Order brings the right Sisters back herself (misaka_sister_resume) instead of guessing
        from the transcript. Nothing is said when the conversation has no cards. A fork is
        the same living line twice over: its cards come via _session_line, and the in-place
        fork restart itself needs no briefing (nothing was forgotten)."""
        if (event or {}).get("reason") in ("reload", "fork"):
            return
        mine = _session_line(ctx)
        if not mine:
            return
        rows = _con().execute(
            "SELECT id,status,assignee,title FROM tasks WHERE origin_session IN "
            f"({','.join('?' * len(mine))}) ORDER BY created_at", mine).fetchall()
        if not rows:
            return
        open_cards = set()
        if os.environ.get("MISAKA_NET_PANE"):
            try:
                from misaka.ui.panel import client as net
                open_cards = {p["card"] for p in net.request("panes.list")["panes"]
                              if p.get("card") and p.get("alive")}
            except Exception:  # noqa: BLE001 - the daemon may be gone; the briefing still lists the cards
                pass
        lines = [f"  {r['id']}  {r['status']:<10} {r['assignee']:<8} {r['title'][:50]}"
                 + ("  (open in a pane)" if r["id"] in open_cards else "") for r in rows]
        harn.sendMessage(
            {"customType": "resume-briefing",
             "content": "<resume-briefing>\nCards created in this conversation:\n"
                        + "\n".join(lines)
                        + "\nReopen a closed one in its own tab with misaka_sister_resume; steer it with "
                          "misaka_sister_message (a finished card restarts only on the user's nod).\n"
                          "</resume-briefing>",
             "display": True, "details": {"cards": [r["id"] for r in rows]}},
            {"triggerTurn": False})

    harn.on("session_start", resume_briefing)


    async def settle_orphans(_event, _ctx):
        """kill -9 leaves a running card with a live-looking claim and nobody driving it
        (the card drives itself now; the daemon only hosts panes). Settle those whenever a
        coordinator session starts: dispatch.reconcile checks leases and process identity."""
        try:
            from misaka.network import dispatch
            await asyncio.to_thread(dispatch.reconcile, _con(), _cfg())
        except Exception:
            pass

    harn.on("session_start", settle_orphans)


    class SisterStopParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        confirmed: bool = Field(description="True only when the user explicitly requested the stop.")


    @_register(
        harn,
        name="misaka_sister_stop", label="Stop Sister task",
        description="Stop a running Sister task after explicit user confirmation.",
        snippet="Stop a running Sister task",
        parameters=SisterStopParams)
    async def misaka_sister_stop(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is not None and str(row["claim_lock"] or "").startswith("net:"):
            if not params.confirmed:
                raise ValueError("Explicit user confirmation is required before stopping a Sister.")
            from misaka.ui.panel import client as net
            await asyncio.to_thread(net.request, "card.stop", {"task_id": params.task_id})
            return _text(f"Stopped card {params.task_id} and closed its pane.")
        result = await runtime.stop(
            params.task_id,
            confirmed=params.confirmed,
            context=ctx,
        )
        return _text(json.dumps(result, ensure_ascii=False))


    class CardLinkParams(StrictParams):
        parent_id: TaskId = Field(description="Parent task-card ID that must finish first.")
        child_id: TaskId = Field(description="Child task-card ID that waits for the parent.")

    @_register(
        harn,
        name="misaka_card_link", label="Link task dependency",
        description="Make one unfinished task depend on another; self-links, cycles, and active children are rejected.",
        snippet="Create a parent-to-child task dependency",
        parameters=CardLinkParams)
    async def misaka_card_link(tool_call_id, params, signal, on_update, ctx):
        created = db.link_tasks(_con(), params.parent_id, params.child_id)
        state = db.get(_con(), params.child_id)["status"]
        return _text(
            f"{'Linked' if created else 'Dependency already exists'}: "
            f"{params.parent_id} → {params.child_id} (child is now {state})."
        )


    class CardCommentsParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID whose comments should be listed")
        after_id: int = Field(0, ge=0, description="Only return log entries whose index is greater than this value (each returned line starts with its index)")
        limit: int = Field(50, ge=1, le=200, description="Maximum comments to return")

    @_register(
        harn,
        name="misaka_card_comments", label="View comments",
        description="List persistent comments attached to a task card.",
        snippet="View a task card's comment history",
        parameters=CardCommentsParams)
    async def misaka_card_comments(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.platform import cards as card_files
        try:
            lines = card_files.read_log(row["workspace"], params.task_id)
        except OSError:
            lines = []
        entries = [(index, line) for index, line in enumerate(lines, 1) if index > params.after_id][-params.limit:]
        return _text("\n".join(f"{index}: {line}" for index, line in entries) or "(no log entries)")


    class CardAttachParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID that will receive the attachment.")
        path: str = Field(description="Path to a local regular file; Misaka copies it into managed attachment storage.")

    @_register(
        harn,
        name="misaka_card_attach", label="Attach local file",
        description="Copy a local regular file into managed task storage; symlinks and files over 50 MiB are rejected.",
        snippet="Attach a local file to a task card",
        parameters=CardAttachParams)
    async def misaka_card_attach(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.platform import cards as card_files
        rel = await asyncio.to_thread(card_files.attach, row["workspace"], params.task_id, params.path)
        return _text(f"Attached {rel} to card {params.task_id} (a file in the project repository).")


    class CardAttachUrlParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID that will receive the link.")
        url: str = Field(description="HTTP or HTTPS reference URL.")

    @_register(
        harn,
        name="misaka_card_attach_url", label="Attach reference URL",
        description="Record an HTTP or HTTPS URL as task input; the URL is provided at execution time and is not downloaded now.",
        snippet="Attach a reference URL to a task card",
        parameters=CardAttachUrlParams)
    async def misaka_card_attach_url(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.platform import cards as card_files
        card_files.attach_url(row["workspace"], params.task_id, params.url)
        return _text(f"Attached reference URL to card {params.task_id}.")


    class CardAttachmentsParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID whose attachments should be listed.")

    @_register(
        harn,
        name="misaka_card_attachments", label="View attachments",
        description="List local-file attachments and URL references for a task card.",
        snippet="View a task card's attachments",
        parameters=CardAttachmentsParams)
    async def misaka_card_attachments(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.platform import cards as card_files
        items = card_files.attachment_list(row["workspace"], params.task_id)
        return _text("\n".join(
            f"[{item['kind']}] {item['name']} — {item.get('path') or item.get('source')}"
            for item in items
        ) or "(No attachments.)")


    class CardUnblockParams(StrictParams):
        task_id: TaskId = Field(description="Blocked or triage task-card ID to reopen.")

    @_register(
        harn,
        name="misaka_card_unblock", label="Unblock task card",
        description="Return a blocked or triage card to todo or ready without starting it.",
        snippet="Unblock a task card",
        parameters=CardUnblockParams)
    async def misaka_card_unblock(tool_call_id, params, signal, on_update, ctx):
        if not db.unblock_task(_con(), params.task_id):
            row = db.get(_con(), params.task_id)
            raise ValueError(
                f"Task card not found: {params.task_id}" if row is None
                else f"Task card {params.task_id} is {row['status']}, not blocked or in triage."
            )
        row = db.get(_con(), params.task_id)
        _mirror_card(row["workspace"], params.task_id, status=row["status"])
        return _text(f"Card {params.task_id} unblocked and returned to {row['status']}; work has not started.")


    class CardDeleteParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID to delete.")
        confirmed: bool = Field(
            description="True only when the user explicitly requested this irreversible deletion; related events and budget records are also removed.")

    @_register(
        harn,
        name="misaka_card_delete", label='Delete card',
        description="Permanently delete a stopped or inactive task card after explicit user confirmation.",
        snippet="Permanently delete a task card",
        guidelines=["Deletion is irreversible. Never call misaka_card_delete without an explicit user request to delete the card."],
        parameters=CardDeleteParams)
    async def misaka_card_delete(tool_call_id, params, signal, on_update, ctx):
        if not params.confirmed:
            raise ValueError("Task-card deletion is irreversible and requires explicit user confirmation.")
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Card not found: {params.task_id}")
        from misaka.platform import cards as card_files
        ok, msg = card_files.remove(_con(), row["workspace"], params.task_id)
        if not ok:
            raise ValueError(msg)
        return _text(msg)

    class SisterViewParams(StrictParams):
        sister: str = Field(description="Sister ID from the `misaka_board` roster.")

    @_register(
        harn,
        name="misaka_sister_view", label="View Sister profile",
        description="Read a Sister's capabilities, model, profile, and current task counts before assigning work.",
        snippet="View a Sister's full profile and workload",
        guidelines=["When assignment is uncertain, inspect relevant Sister profiles instead of guessing from an ID."],
        parameters=SisterViewParams)
    async def misaka_sister_view(tool_call_id, params, signal, on_update, ctx):
        from misaka.network import roster as roster_mod
        sid = params.sister.strip()
        if sid not in set(_sisters()):
            return _text(f"Sister {sid} is not in the roster ({', '.join(_sisters())}).")
        root = _cfg()["profiles_root"]
        desc, body = roster_mod.describe(sid, root=root)
        model = None
        try:
            with open(os.path.join(root, sid, "config.json"), encoding="utf-8") as f:  # noqa: ASYNC230 - one small config.json per Sister
                model = json.load(f).get("model")
        except (OSError, ValueError):
            pass
        counts = {r[0]: r[1] for r in _con().execute(
            "SELECT status, COUNT(*) FROM tasks WHERE assignee=? GROUP BY status", (sid,))}
        cards = ','.join(f"{k}×{v}" for k, v in sorted(counts.items())) or 'none'
        head = (
            f"Sister {sid}\n"
            f"Summary: {desc or 'not written'}\n"
            f"Model: {model or 'global default'}\n"
            f"Task cards: {cards}"
        )
        return _text(
            head + ("\n" + body if body else
                    f"\nNo capability profile is available. Add one to profiles/sisters/{sid}/DESCRIBE.md.")
        )

    async def cleanup(_event, _ctx):
        await runtime.close()

    harn.on("session_shutdown", cleanup)

    async def collect_pending():
        """Deliver terminal-card notifications queued while no session was running, then hint about cards awaiting review."""
        con = _con()
        subscription = notifications.subscribe(
            con, "last-order", "board-harness", "task", "*", "terminal"
        )
        for _ in range(100):
            event = notifications.claim_next(con, subscription)
            if event is None:
                break
            try:
                payload = json.loads(event["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            generation = int(payload.get("generation") or 0)
            delivered = runtime.notify_row(event["resource_id"])
            row = db.get(con, event["resource_id"])
            consumed = (
                delivered or row is None or int(row["generation"]) != generation
                or int(row["notified_generation"]) >= generation
            )
            if consumed:
                notifications.ack(
                    con, subscription, event["id"], event["lease_token"]
                )
            else:
                notifications.nack(
                    con, subscription, event["id"], event["lease_token"],
                    "board harness delivery failed",
                )
                break
        reviewing = [r["id"] for r in db.by_status(con, "review")]
        if reviewing:
            harn.sendMessage(
                {"customType": "board-hint", "display": True,
                 "content": f"{len(reviewing)} task cards await independent review "
                            f"({', '.join(reviewing[:5])}{'…' if len(reviewing) > 5 else ''}).",
                 "details": {"reviewing": reviewing}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )

    async def _kickoff(_event, _ctx):
        asyncio.ensure_future(collect_pending())

    harn.on("session_start", _kickoff)

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    return register
