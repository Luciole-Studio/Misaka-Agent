"""Last Order's task-board tools.

Last Order plans and delegates through task cards. Starting model work is a separate, user-confirmed action.
"""
import asyncio
import json
import os
import secrets
from typing import Annotated, Literal, Optional

from misaka.core.extensions.types import ToolDefinition
from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.platform import tasks as db
from misaka.network import validate
from misaka.platform import budget, notifications
from misaka.config import CFG, sisters
from misaka.network.sister_runtime import SisterRuntime

_CON = None
TaskId = Annotated[str, Field(pattern=r"^t_[0-9a-f]{6}$")]


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
        status: Optional[str] = Field(None, description=(
            "Optional task status: todo, ready, running, blocked, triage, review, "
            "verifying, finalizing, done, failed, or stopped."
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
        rows = [r for r in db.by_status(con, params.status) if r["workspace"] == workspace] \
            if params.status else con.execute(
                "SELECT * FROM tasks WHERE workspace=? ORDER BY created_at DESC LIMIT 40",
                (workspace,)).fetchall()
        lines = [f"{r['id']}  {r['status']:<10} {r['assignee']:<14} {r['title'][:50]}" for r in rows]
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
        body: str = Field(description="Task contract containing `## goal`, `## boundaries`, and `## acceptance criteria`.")
        assignee: str = Field(description="Sister ID from the `misaka_board` roster.")
        reviewer: Optional[str] = Field(
            None, description="Optional independent reviewer; must be a different Sister from the assignee."
        )
        priority: int = Field(0, description="Relative priority; higher values run first.")


    @_register(
        harn,
        name="misaka_card", label="Create task card",
        description="Create a durable Sister task with explicit scope and testable acceptance criteria; this does not start work.",
        snippet="Create a Sister task card with goal, boundaries, and acceptance criteria",
        guidelines=[
            "The body must contain `## goal`, `## boundaries`, and `## acceptance criteria` with verifiable outcomes.",
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
        tid = db.create_task(con, c["title"], body=c["body"], assignee=c["assignee"],
                             priority=c["priority"], timeout_seconds=c["timeout"],
                             reviewer=params.reviewer, workspace=_workspace(ctx))
        review = f" → reviewer {params.reviewer}" if params.reviewer else ""
        return _text(
            f"Added {tid}: {c['title']} → {c['assignee']}{review}.\n"
            "Work has not started. Wait for explicit user approval, then call "
            "misaka_dispatch or misaka_sister."
        )


    class ReviewConfigParams(StrictParams):
        task_id: TaskId
        reviewer: Optional[str] = Field(
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
        follow = await runtime.launch_ready(
            context=ctx, tool_call_id=tool_call_id, on_update=on_update,
            task_ids=[params.task_id],
        )
        target = "red-team verification" if params.decision == "approve" else "revision"
        state = follow[0].get("status") if follow else db.get(con, params.task_id)["status"]
        return _text(f"Review recorded: {params.decision}; moved to {target} ({state}).")


    class DispatchParams(StrictParams):
        confirmed: bool = Field(description="True only when the user explicitly approved starting model work.")
        task_ids: Optional[list[TaskId]] = Field(
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
        ready = (
            len(db.by_status(con, "ready"))
            + len(db.by_status(con, "verifying"))
            + len(db.by_status(con, "finalizing"))
        )
        if not ready:
            return _text("No task cards are ready to run.")
        if on_update:
            on_update({"content": [{"type": "text", "text": f"Starting {ready} card(s)…"}], "details": {}})
        if os.environ.get("MISAKA_NET_PANE"):
            # Start worker cards in visible panes; verification still uses the runtime.
            from misaka.net import client as net
            wanted = set(params.task_ids or [])
            lines, started = [], 0
            for row in db.fair_ready(con, lane="workers"):
                if wanted and row["id"] not in wanted:
                    continue
                try:
                    out = await asyncio.to_thread(
                        net.request, "pane.run_card", {"task_id": row["id"]})
                    started += 1
                    lines.append(f"""  {row['id']} → {row['assignee']}  pane {out['pane_id']}""")
                except Exception as error:  # noqa: BLE001 - report individual launch failures
                    lines.append(f"  {row['id']} failed to start: {error}")
            verifyish = [r["id"] for r in
                         [*db.by_status(con, "verifying"), *db.by_status(con, "finalizing")]
                         if not wanted or r["id"] in wanted]
            if verifyish:
                for item in await runtime.launch_ready(
                        context=ctx, tool_call_id=tool_call_id,
                        on_update=on_update, task_ids=verifyish):
                    lines.append(f"  {item.get('task_id', '?')}  {item.get('status', '?')}")
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
            from misaka.net import client as net
            out = await asyncio.to_thread(
                net.request, "pane.run_card", {"task_id": params.task_id})
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
        if row is not None and str(row["claim_lock"] or "").startswith("net:"):
            # A network-owned task receives steering through its live pane.
            from misaka.net import client as net
            await asyncio.to_thread(
                net.request, "pane.send",
                {"card": params.task_id, "text": params.message, "enter": True})
            db.add_comment(
                _con(), params.task_id, "last-order", params.message, kind="steer"
            )
            return _text(f"Message sent to card {params.task_id}'s pane.")
        result = await runtime.message(
            params.task_id,
            params.message,
            summary=params.summary,
            confirmed=params.confirmed,
            context=ctx,
        )
        db.add_comment(
            _con(), params.task_id, "last-order", params.message,
            kind=str(result.get("mode") or "message"),
        )
        return _text(json.dumps(result, ensure_ascii=False))


    class SisterStopParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        confirmed: bool = Field(description="True only when the user explicitly requested the stop.")


    @_register(
        harn,
        name="misaka_sister_stop", label="Stop Sister task",
        description="Stop a running or verifying Sister task after explicit user confirmation.",
        snippet="Stop a running Sister task",
        parameters=SisterStopParams)
    async def misaka_sister_stop(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is not None and str(row["claim_lock"] or "").startswith("net:"):
            if not params.confirmed:
                raise ValueError("Explicit user confirmation is required before stopping a Sister.")
            from misaka.net import client as net
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
        after_id: int = Field(0, ge=0, description="Only return comments after this comment ID")
        limit: int = Field(50, ge=1, le=200, description="Maximum comments to return")

    @_register(
        harn,
        name="misaka_card_comments", label="View comments",
        description="List persistent comments attached to a task card.",
        snippet="View a task card's comment history",
        parameters=CardCommentsParams)
    async def misaka_card_comments(tool_call_id, params, signal, on_update, ctx):
        rows = db.comments(
            _con(), params.task_id, after_id=params.after_id, limit=params.limit
        )
        if not rows and db.get(_con(), params.task_id) is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        return _text("\n".join(
            f"#{row['id']} [{row['kind']}] {row['author']}: {row['body']}" for row in rows
        ) or "(no comments)")


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
        attachment_id = await asyncio.to_thread(
            db.attach_file, _con(), params.task_id, params.path,
            author="last-order",
        )
        return _text(f"Attached local file {attachment_id} to card {params.task_id}.")


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
        attachment_id = db.attach_url(
            _con(), params.task_id, params.url, author="last-order"
        )
        return _text(f"Attached reference URL {attachment_id} to card {params.task_id}.")


    class CardAttachmentsParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID whose attachments should be listed.")

    @_register(
        harn,
        name="misaka_card_attachments", label="View attachments",
        description="List local-file attachments and URL references for a task card.",
        snippet="View a task card's attachments",
        parameters=CardAttachmentsParams)
    async def misaka_card_attachments(tool_call_id, params, signal, on_update, ctx):
        rows = db.attachments(_con(), params.task_id)
        if not rows and db.get(_con(), params.task_id) is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        return _text("\n".join(
            f"{row['id']} [{row['kind']}] {row['name']} — {row['source']}"
            for row in rows
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
        ok, msg = db.delete_task(_con(), params.task_id)
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
            with open(os.path.join(root, sid, "config.json"), encoding="utf-8") as f:
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
        """Deliver terminal-card notifications queued while no session was running, then hint about cards awaiting review or verification."""
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
        verifying = [r["id"] for r in db.by_status(con, "verifying")]
        verifying += [r["id"] for r in db.by_status(con, "finalizing")]
        if reviewing:
            harn.sendMessage(
                {"customType": "board-hint", "display": True,
                 "content": f"{len(reviewing)} task cards await independent review "
                            f"({', '.join(reviewing[:5])}{'…' if len(reviewing) > 5 else ''}).",
                 "details": {"reviewing": reviewing}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )
        if verifying:
            harn.sendMessage(
                {"customType": "board-hint", "display": True,
                 "content": f"{len(verifying)} task cards are waiting for verification "
                            f"({', '.join(verifying[:5])}{'…' if len(verifying) > 5 else ''}). "
                            "Say 'run' to continue verification.",
                 "details": {"verifying": verifying}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )

    async def _kickoff(_event, _ctx):
        asyncio.ensure_future(collect_pending())

    harn.on("session_start", _kickoff)

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    return register
