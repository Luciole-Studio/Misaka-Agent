"""Phase-scoped LO tools. The driver executes recorded commands, never parses LO prose."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from misaka.core.extensions.types import ToolDefinition
from misaka.core.research import runs


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Task(Params):
    local_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    deliverable: str = Field(min_length=1)
    assignee: str = Field(min_length=1)
    assignee_reason: str = Field(min_length=1)
    method: str = ""
    source_strategy: str = ""
    falsifiers: str = ""
    dependencies: list[str] = Field(default_factory=list, description="local_id values from this same plan only; not prior rounds or Board task IDs.")
    capabilities: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, strict=True)


class RedTeam(Params):
    assignee: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Plan(Params):
    status: Literal["ready", "clarify"]
    plan_markdown: str = Field(min_length=1)
    reframed_question: str = Field("", description=(
        "Only when the question itself should change: the question as it should now read. It takes "
        "effect once the user agrees to this plan; until then it is a proposal in plan_markdown."))
    tasks: list[Task] = Field(default_factory=list)
    red_team: RedTeam | None = Field(None, description="Required when status is ready: name one roster Sister as the independent red team.")
    clarifying_questions: list[str] = Field(default_factory=list)
    methods: list[dict[str, Any]] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class Start(Params):
    summary: str = Field("", description="One or two lines on what was agreed with the user, for the record.")


class Withdraw(Params):
    reason: str = Field("", description="Why the node concludes without this round, for the record.")


class Skip(Params):
    reason: str = Field(min_length=1, description="Why the user chose not to research this node, for the record.")


class Issue(Params):
    kind: Literal["fact", "logic", "causation", "concept", "scope", "method", "bias", "normative", "unresolved"]
    question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    priority: int = Field(default=0, strict=True)
    material: bool = Field(strict=True)


class Investigation(Params):
    issue_id: str
    assignment: str = Field(min_length=1, description="The investigation this LO fork must perform, and why.")


class Investigations(Params):
    assignments: list[Investigation]


def tool(con, run, node, *, key, name, description, model, validate, session_dir, session_file=None,
         supersede=False):
    """Only the owning LO session can record this phase's command; no side effects on construction.
    With ``supersede`` a later call replaces the earlier record instead of being refused."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = model.model_validate(raw).model_dump()
        payload = validate(payload)
        manager = getattr(ctx, "sessionManager", None)
        path = getattr(manager, "sessionFile", None)
        if not path or os.path.dirname(os.path.realpath(path)) != os.path.realpath(session_dir):
            raise ValueError("Research command must come from this phase's Last Order session.")
        if session_file and os.path.realpath(path) != os.path.realpath(session_file):
            raise ValueError("Research command must come from the owning Last Order conversation.")
        record = runs.replace_action if supersede else runs.record_action
        accepted = record(con, run, node, key, payload, session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": f"Accepted {name}. The recorded command is queued for execution."}],
                "details": {"run_id": run["id"], "node_id": node["id"], "action_key": key,
                            "session_file": accepted["session_file"]}}

    return ToolDefinition(name=name, label=description, description=description,
                          parameters=model, execute=execute, promptSnippet=description)


def review_tools(con, run, node, *, validate, session_file, round=1):
    """What a Last Order can do while her plan waits for the user's go-ahead: revise it (each
    call replaces the recorded plan and the run keeps waiting), start it (once the user has
    agreed in conversation), withdraw a follow-up round, or -- a fork's first plan only -- skip
    the node at the user's decision (it closes unresearched, its issue parked). All belong to
    the node's own conversation and to this waiting state only; the driver decides nothing from prose."""
    @contextmanager
    def owner(ctx):
        manager = getattr(ctx, "sessionManager", None)
        path = getattr(manager, "sessionFile", None)
        if not path or os.path.realpath(path) != os.path.realpath(session_file):
            raise ValueError("Research command must come from the owning Last Order conversation.")
        with runs.task_store.write_txn(con):
            runs._owned(con, run, node)  # The captured epoch, never the replacement owner's row.
            current = runs.node(con, node["id"])
            if current["status"] != "awaiting_approval":
                raise ValueError("No plan of this node is waiting for the user's go-ahead right now.")
            yield path


    async def revise(call_id, raw, _signal, _on_update, ctx):
        payload = validate(Plan.model_validate(raw).model_dump())
        with owner(ctx) as path:
            runs.replace_action(con, run, node, runs.plan_key(round), payload, session_file=path, tool_call_id=call_id)
            runs.delete_action(con, run["id"], node["id"], runs.start_key(round))
        return {"content": [{"type": "text", "text": (
            "Revised plan recorded; it replaces the earlier one and the run keeps waiting. "
            "Call misaka_research_start once the user has agreed to it.")}],
            "details": {"run_id": run["id"], "node_id": node["id"], "action_key": runs.plan_key(round)}}

    async def start(call_id, raw, _signal, _on_update, ctx):
        payload = Start.model_validate(raw).model_dump()
        with owner(ctx) as path:
            plan = runs.action(con, run["id"], node["id"], runs.plan_key(round))
            if plan is None:
                raise ValueError("There is no recorded plan to start; record one with misaka_research_assign first.")
            runs.replace_action(con, run, node, runs.start_key(round),
                                {"plan_tool_call_id": plan["tool_call_id"], "summary": payload["summary"]},
                                session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": "Started: the plan's research cards are being created now."}],
                "details": {"run_id": run["id"], "node_id": node["id"], "action_key": runs.start_key(round)}}

    async def withdraw(call_id, raw, _signal, _on_update, ctx):
        Withdraw.model_validate(raw)
        with owner(ctx):
            runs.delete_action(con, run["id"], node["id"], runs.start_key(round))
            runs.delete_action(con, run["id"], node["id"], runs.plan_key(round))
        return {"content": [{"type": "text", "text": (
            "Follow-up round withdrawn: no more cards; the node concludes from the material it already has.")}],
            "details": {"run_id": run["id"], "node_id": node["id"], "action_key": runs.plan_key(round)}}

    async def skip(call_id, raw, _signal, _on_update, ctx):
        payload = Skip.model_validate(raw).model_dump()
        with owner(ctx) as path:
            runs.replace_action(con, run, node, runs.SKIP_KEY, payload, session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": (
            "Skipped: this node closes without research. No cards and no conclusion; its issue stays parked "
            "for final adjudication with the reason on record.")}],
            "details": {"run_id": run["id"], "node_id": node["id"], "action_key": runs.SKIP_KEY}}

    withdraw_description = ("Withdraw this follow-up round: no further cards, and the node concludes from the material "
                            "it already has. For when the user would rather have the conclusion now.")
    skip_description = ("Close this node without researching it, at the user's decision: no cards, no conclusion, and "
                        "its issue stays parked for final adjudication with the reason on record. Never fake a "
                        "completion or write one into project files instead.")
    revise_description = ("Revise the research plan that is waiting for the user's go-ahead. Replaces the recorded "
                          "plan; the run keeps waiting until misaka_research_start.")
    start_description = ("Start the research from the recorded plan. Call it once the user has agreed, in "
                         "conversation, that the plan should go ahead; until then the plan only waits.")
    return [
        ToolDefinition(name="misaka_research_assign", label=revise_description, description=revise_description,
                       parameters=Plan, execute=revise, promptSnippet=revise_description),
        ToolDefinition(name="misaka_research_start", label=start_description, description=start_description,
                       parameters=Start, execute=start, promptSnippet=start_description),
        *([ToolDefinition(name="misaka_research_withdraw", label=withdraw_description,
                          description=withdraw_description, parameters=Withdraw, execute=withdraw,
                          promptSnippet=withdraw_description)] if round > 1 else []),
        # The root's first plan is the run: skipping it is `/research stop`, not a node decision.
        *([ToolDefinition(name="misaka_research_skip", label=skip_description, description=skip_description,
                          parameters=Skip, execute=skip, promptSnippet=skip_description)]
          if round == 1 and node["parent_id"] is not None else []),
    ]
