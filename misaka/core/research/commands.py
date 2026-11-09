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
    deliverable: str = Field(min_length=1, description=(
        "The file's name on the first line, such as `naval-balance.md`; what it must contain on the lines after it. "
        "One file per card: other files it writes (a script, data) are named on those lines too."))
    assignee: str = Field(min_length=1)
    assignee_reason: str = Field(min_length=1)
    method: str = ""
    source_strategy: str = ""
    falsifiers: str = ""
    dependencies: list[str] = Field(default_factory=list, description="local_id values from this same plan only; not prior rounds or Board task IDs.")
    capabilities: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, strict=True)


class RedTeam(Params):
    assignee: str = Field(min_length=1, description="One roster Sister.")
    reason: str = Field(min_length=1, description=(
        "Why her profile fits both jobs: critic of every version of this node's conclusion, and the "
        "divergence review that proposes the paths the conclusion did not take."))


class Option(Params):
    label: str = Field(min_length=1, description="The possibility in a few words.")
    premise: str = Field(min_length=1, description=(
        "What this path assumes or changes -- its hypothesis, method, reading, perspective or "
        "decomposition -- so a node that pursues it knows where it stands."))
    own: bool = Field(False, strict=True, description=(
        "This node's own line (at most one per decision): the node itself pursues it. Every other "
        "option is a new possibility, opened as a node at the level's reconciliation."))


class Decision(Params):
    question: str = Field(min_length=1, description="What is being decided between the options.")
    stakes: str = Field(min_length=1, description="What turns on it: how the answer could change with the path taken.")
    options: list[Option] = Field(min_length=2, description=(
        "At least two genuinely different possibilities; the node's own line may be one of them."))


class Plan(Params):
    status: Literal["ready", "clarify"]
    plan_markdown: str = Field("", description=(
        "The research design. Required on a plan's first call; an append call may leave it empty to "
        "keep the recorded one."))
    reframed_question: str = Field("", description=(
        "Only when the question itself should change: the question as it should now read. It takes "
        "effect once the user agrees to this plan; until then it is a proposal in plan_markdown."))
    tasks: list[Task] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list, description=(
        "First plan only, below max_depth: forks this design already sees -- competing hypotheses, "
        "methods, readings, perspectives, scenarios or decompositions to pursue as separate nodes. Beside "
        "tasks, options open once this node concludes and answer its conclusion; a plan with decisions and no "
        "tasks is a pure branch point whose options open at once, independently and in parallel. Complementary "
        "work is not a fork: it belongs in tasks."))
    red_team: RedTeam | None = Field(None, description=(
        "Required when the plan has tasks: one roster Sister as this node's red team. She critiques every "
        "version of the conclusion and then, in a separate session, runs the divergence review."))
    clarifying_questions: list[str] = Field(default_factory=list)
    methods: list[dict[str, Any]] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)
    append: bool = Field(False, strict=True, description=(
        "Add to the plan already recorded for this round instead of replacing it: recorded tasks stay, "
        "a task with the same local_id is replaced, and plan_markdown, red_team and the other fields "
        "change only when given. Submit a plan too large for one reply in several calls this way; "
        "never shrink it to fit one call."))


def _refuse_repeated_ids(tasks):
    """One call naming a local_id twice: keyed by id, the second used to replace the first unseen."""
    seen, twice = set(), set()
    for task in tasks:
        (twice if task["local_id"] in seen else seen).add(task["local_id"])
    if twice:
        raise ValueError(f"local_id {', '.join(sorted(twice))} is given to more than one task in this call.")


def merge_plan(recorded, incoming):
    """An append call folded onto the plan recorded for its round: recorded tasks stay in order, an
    incoming task replaces the recorded one with its local_id, new ones follow, decisions add to the
    recorded ones, and every other field keeps its recorded value unless the call gives one. The result is validated as one whole plan, so
    dependencies across the two halves are checked together."""
    merged = dict(recorded)
    _refuse_repeated_ids(incoming.get("tasks") or [])
    tasks = {task["local_id"]: task for task in recorded.get("tasks") or []}
    for task in incoming.get("tasks") or []:
        tasks[task["local_id"]] = task      # a replacement keeps its recorded position
    merged["tasks"] = list(tasks.values())
    # A decision sent again (a retried call, a full re-send with append to fix one field) is the
    # same fork, not a second one: both used to open as pending options (0.18.10 sweep).
    decisions = list(recorded.get("decisions") or [])
    merged["decisions"] = decisions + [item for item in incoming.get("decisions") or [] if item not in decisions]
    merged["status"] = incoming["status"]
    for key in ("plan_markdown", "reframed_question", "clarifying_questions", "methods", "extensions"):
        if incoming.get(key):
            merged[key] = incoming[key]
    if incoming.get("red_team") is not None:
        merged["red_team"] = incoming["red_team"]
    return merged


def plan_receipt(payload, *, forks):
    """What an accepted plan is told: what was recorded, since only the recorded fields act. A fork
    written into plan_markdown alone opened nothing (2026-09-27: the root's plan described one and
    recorded decisions=[])."""
    text = f"Recorded {len(payload['tasks'])} card(s) and {len(payload['decisions'])} decision(s)."
    if forks and not payload["decisions"]:
        text += (" No fork is recorded: one described only in plan_markdown opens nothing. If the plan means "
                 "one, send it in `decisions` with append=true.")
    return text


class Start(Params):
    summary: str = Field("", description="One or two lines on what was agreed with the user, for the record.")


class Withdraw(Params):
    reason: str = Field("", description="Why the node concludes without this round, for the record.")


class Skip(Params):
    reason: str = Field(min_length=1, description="Why the user chose not to research this node, for the record.")


class Issue(Params):
    kind: str = Field("", description="What sort of problem this is, in a few words of your own.")
    question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    priority: int = Field(default=0, strict=True)
    material: bool = Field(strict=True)


DISPOSITIONS = Literal["revise", "correct", "rebut", "concede", "covered", "park", "branch"]


class Disposition(Params):
    issue_id: str = Field(min_length=1)
    disposition: DISPOSITIONS = Field(description=(
        "revise: this node reworks its conclusion (new cards if follow-ups remain, or a rewrite); "
        "correct: no revision is left and the issue names a plain error -- the corrected text is appended to the "
        "conclusion; rebut: the objection does not hold, and why; concede: the position stands and pays this cost; "
        "covered: another node of this run already owns it (covered_by = that node id); park: left open for "
        "the final adjudication; branch: it reveals a genuine alternative, taken to this node's decision."))
    covered_by: str = Field("", description="covered only: the id of the node that owns the issue.")
    reason: str = Field(min_length=1, description=(
        "revise: what will change; correct: the corrected text; rebut: the reply the red team reads on re-review; "
        "concede: the cost accepted; covered: how that node covers it; park: why it stays open; branch: the "
        "alternative it reveals."))


class Dispositions(Params):
    dispositions: list[Disposition]


class Alternative(Params):
    # Free text: what sort of alternative this is, in the reviewer's words. A closed list made her
    # file a theoretical framework as "other" after the call was refused (2026-09-27); nothing
    # downstream branches on it.
    kind: str = Field("", description="What sort of proposal this is, in a few words of your own.")
    proposal: str = Field(min_length=1, description="The possibility or the gap, stated so a Last Order could plan it.")
    premise: str = Field(min_length=1, description=(
        "What it assumes or changes compared with the conclusion under review; for a possibility not taken, what in "
        "the conclusion it cannot stand with."))
    rationale: str = Field(min_length=1, description="What it could change in the answer, and what research it would take.")
    gap: bool = Field(False, strict=True, description=(
        "true: a gap any line answering this question has to fill -- it goes back to this node, which fills it before "
        "it forks. false: a possibility the conclusion did not take -- a candidate fork."))
    covered_by: str = Field("", description=(
        "The id of a node already on the graph that pursues it, or of an option waiting to be opened that takes it up, "
        "if any."))


class DecideOption(Option):
    sources: list[str] = Field(default_factory=list, description=(
        "Ids of the divergence proposals and branch-disposed red-team issues that make up this possibility."))
    answers: list[str] = Field(default_factory=list, description=(
        "Ids of paths the paths list shows declined, covered or left unopened that this option brings back; its "
        "premise says what answers the reason recorded for each. Empty for a possibility new to the run."))


class DecideDecision(Decision):
    options: list[DecideOption] = Field(min_length=2)


class Declined(Params):
    issue_id: str = Field(min_length=1)
    disposition: Literal["covered", "decline"]
    covered_by: str = Field("", description=(
        "covered only: the id of the node already pursuing it, or of the option waiting to be opened that takes it up."))
    reason: str = Field(min_length=1, description="Why it is not opened here; the final report's record of paths not taken.")


class Decide(Params):
    decisions: list[DecideDecision] = Field(default_factory=list)
    declined: list[Declined] = Field(default_factory=list)


class CardsFailed(Params):
    decision: Literal["retry", "conclude", "fail"] = Field(description=(
        "retry: the cards go back to their Sisters; conclude: go on with the cards that finished; fail: the node "
        "fails."))
    reason: str = Field(min_length=1, description="Why, in a sentence or two.")


class Reassign(Params):
    card: str = Field(min_length=1, description="The card's local_id in your plan, or its card id (t_...).")
    assignee: str = Field(min_length=1, description="A roster Sister, the Sisters added since the plan included.")
    assignee_reason: str = Field(min_length=1)


class Cancel(Params):
    card: str = Field(min_length=1, description="The card's local_id in your plan, or its card id (t_...).")
    reason: str = Field(min_length=1)


class PlanChange(Params):
    reason: str = Field(min_length=1, description="Why the plan changes: what the user asked, for the record.")
    add: list[Task] = Field(default_factory=list, description=(
        "New cards, written as a plan's tasks; their dependencies may name this round's other cards."))
    reassign: list[Reassign] = Field(default_factory=list, description="Cards not started yet, given to another Sister.")
    cancel: list[Cancel] = Field(default_factory=list, description=(
        "Cards not started yet that are no longer wanted; a card that depends on one is changed in the same call."))


class Erratum(Params):
    node: str = Field(min_length=1, description="The id of the earlier node whose conclusion states it.")
    claim: str = Field(min_length=1, description="What that conclusion says, quoted as written.")
    correction: str = Field(min_length=1, description="What holds instead.")
    grounds: str = Field(min_length=1, description="What shows it: the card, finding or source, with its path.")


class ReportText(Params):
    markdown: str = Field(min_length=1, description="The complete text in Markdown, beginning with the answer itself.")


class Dissolve(Params):
    question: Literal["this_node", "research_question"] = Field(description=(
        "Which question the conclusion dissolves: this node's own, or the research question the user asked "
        "(the root's is always the research question). Dissolving the research question needs the user's consent "
        "before the final report."))
    grounds: str = Field(min_length=1, description=(
        "Why the question does not stand as asked: the conceptual confusion, false alternative or ideological "
        "presupposition it rests on, and what can be asked instead."))


class Consent(Params):
    agreed: bool = Field(strict=True, description=(
        "Whether the user agreed, in this conversation, that the final answer may dissolve the research question."))
    note: str = Field("", description="What the user said about it, in a line or two, for the record.")


class ReconcileNode(Params):
    question: str = Field(min_length=1, description="The new node's question, written for its Last Order.")
    options: list[str] = Field(min_length=1, description=(
        "Pending option ids this node pursues -- or ids of proposals whose coverage this reconciliation reopens. "
        "Options from several nodes that ask the same question go to one node, which then has each of their "
        "nodes as a parent."))
    rationale: str = Field(min_length=1)


class ReconcileJoin(Params):
    parents: list[str] = Field(min_length=2, description="Finished nodes whose work this node carries forward together.")
    question: str = Field(min_length=1)
    rationale: str = Field(min_length=1, description=(
        "Why their shared continuation warrants one node; what each parent brings and where they differ."))


class NotPursued(Params):
    option: str = Field(min_length=1, description=(
        "A pending option's id, or the id of a proposal whose coverage this reconciliation reopens."))
    reason: str = Field(min_length=1)
    covered_by: str = Field("", description=(
        "When a node answers it -- it already pursues this possibility, or this option complements it: that "
        "node's id, or the id of a pending option this reconciliation opens. Empty when a limit stops it or it is "
        "a correction."))


class Coverage(Params):
    proposal: str = Field(min_length=1, description="The id of a coverage claim listed in the material.")
    verdict: Literal["holds", "reopen"] = Field(description=(
        "holds: the covering node's research answers it. reopen: it does not -- the proposal is a possibility of "
        "its own and gets an outcome like a pending option: its id in a node's options, or in not_pursued."))
    note: str = Field(min_length=1, description=(
        "holds: how that node's research answers it. reopen: what that research leaves unanswered. The proposal "
        "keeps its own premise; this note is the record of the verdict."))


class Relation(Params):
    kind: Literal["converge", "diverge", "resonates", "appropriates", "displaces"]
    nodes: list[str] = Field(min_length=2)
    note: str = Field(min_length=1)


class Relate(Params):
    relations: list[Relation] = Field(min_length=1)


class Reconcile(Params):
    nodes: list[ReconcileNode] = Field(default_factory=list)
    joins: list[ReconcileJoin] = Field(default_factory=list)
    not_pursued: list[NotPursued] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list, description=(
        "One verdict for every coverage claim in the material; [] when there are none."))
    summary: str = Field(min_length=1, description="One or two lines on what this reconciliation does, for the record.")


def tool(con, run, node, *, key, name, description, model, validate, session_dir, session_file=None,
         supersede=False, merge=None, reply=None):
    """Only the owning LO session can record this phase's command; no side effects on construction.
    With ``supersede`` a later call replaces the earlier record instead of being refused. With
    ``merge`` (the plan phase) a call marked ``append`` is folded onto the record already accepted
    for ``key`` before validation, and replaces it. ``reply(payload)`` is what an accepted call is
    told, when the phase needs it to know more than that the command is queued."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        prior = None
        try:
            payload = model.model_validate(raw).model_dump()
            if payload.pop("append", False) and merge is not None:
                prior = runs.action(con, run["id"], node["id"], key)
                if prior:
                    payload = merge(prior["payload"], payload)
            payload = validate(payload)
        except ValueError as error:
            # A refused call records nothing. Said outright, because a model that read only the
            # complaint took its plan as accepted and sent an empty "append" to fix one field.
            raise ValueError(f"{error} Nothing from this call was recorded; send the corrected call in full.") from error
        manager = getattr(ctx, "sessionManager", None)
        path = getattr(manager, "sessionFile", None)
        if not path or os.path.dirname(os.path.realpath(path)) != os.path.realpath(session_dir):
            raise ValueError("Research command must come from this phase's Last Order session.")
        if session_file and os.path.realpath(path) != os.path.realpath(session_file):
            raise ValueError("Research command must come from the owning Last Order conversation.")
        # A plan phase (``merge``) takes a full plan sent again as the plan, as Plan.append says
        # ("instead of replacing it"): refused, it read "already accepted with different
        # arguments" and the first plan ran as it was (0.18.10 sweep).
        record = runs.replace_action if supersede or prior or merge is not None else runs.record_action
        try:
            accepted = record(con, run, node, key, payload, session_file=path, tool_call_id=call_id)
        except ValueError as error:
            raise ValueError(f"{error} Nothing from this call was recorded.") from error
        text = reply(payload) if reply else f"Accepted {name}. The recorded command is queued for execution."
        return {"content": [{"type": "text", "text": text}],
                "details": {"run_id": run["id"], "node_id": node["id"], "action_key": key,
                            "session_file": accepted["session_file"]}}

    return ToolDefinition(name=name, label=description, description=description,
                          parameters=model, execute=execute, promptSnippet=description)


def review_tools(con, run, node, *, validate, session_file, round=1, forks=False):
    """What a Last Order can do while her plan waits for the user's go-ahead: revise it (each
    call replaces the recorded plan and the run keeps waiting), start it (once the user has
    agreed in conversation), withdraw a follow-up round, or -- a non-root node's first plan only --
    skip the node at the user's decision (it closes unresearched). All belong to the node's own
    conversation and to this waiting state only; the driver decides nothing from prose."""
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
        payload = Plan.model_validate(raw).model_dump()
        append = payload.pop("append", False)
        with owner(ctx) as path:
            recorded = runs.action(con, run["id"], node["id"], runs.plan_key(round)) if append else None
            if recorded:
                payload = merge_plan(recorded["payload"], payload)
            payload = validate(payload)
            runs.replace_action(con, run, node, runs.plan_key(round), payload, session_file=path, tool_call_id=call_id)
            runs.delete_action(con, run["id"], node["id"], runs.start_key(round))
        text = (("Appended to the recorded plan. " if recorded else "Revised plan recorded; it replaces the earlier one. ")
                + plan_receipt(payload, forks=forks) + " The run keeps waiting. ")
        return {"content": [{"type": "text", "text": text + "Call misaka_research_start once the user has agreed to it."}],
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
            "Skipped: this node closes without research. No cards and no conclusion; the final adjudication "
            "sees it as a path not researched, with the reason on record.")}],
            "details": {"run_id": run["id"], "node_id": node["id"], "action_key": runs.SKIP_KEY}}

    withdraw_description = ("Withdraw this follow-up round: no further cards, and the node concludes from the material "
                            "it already has. For when the user would rather have the conclusion now.")
    skip_description = ("Close this node without researching it, at the user's decision: no cards, no conclusion; "
                        "the final adjudication sees it as a path not researched, with the reason on record. Never "
                        "fake a completion or write one into project files instead.")
    revise_description = ("Revise the research plan that is waiting for the user's go-ahead. Replaces the recorded "
                          "plan, or adds to it with append=true; the run keeps waiting until misaka_research_start.")
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
          if round == 1 and not runs.is_root(node) else []),
    ]


PLAN_CHANGE_DESCRIPTION = (
    "While this node's research cards run: add cards, give a card that has not started to another Sister, or "
    "cancel one that has not started -- when the user asks for it, or agrees to your proposal. Checked as a plan's "
    "tasks are; the run applies it at once and the cards already working go on.")


def plan_change_tool(con, run, node, *, round, session_file, validate):
    """What the node's Last Order can change while her cards run (GitHub issue #9: once accepted, a
    plan could not be changed, and Sisters created mid-run were never given work). She records the
    change; the driver applies it between two looks at the cards, so the board has one writer."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = PlanChange.model_validate(raw).model_dump()
        if not (payload["add"] or payload["reassign"] or payload["cancel"]):
            raise ValueError("Name at least one card to add, reassign or cancel.")
        path = _run_owner(session_file, ctx)
        with runs.task_store.write_txn(con):
            runs._owned(con, run, node)
            if runs.node(con, node["id"])["status"] != "executing":
                raise ValueError("This node's cards are not running now; its plan is changed in its next round.")
            payload = validate(payload)
            runs.record_action(con, run, node, f"{runs.plan_change_key(round)}:{call_id}", payload,
                               session_file=path, tool_call_id=call_id)
        said = ", ".join(part for part in (
            f"{len(payload['add'])} card(s) to add" if payload["add"] else "",
            f"{len(payload['reassign'])} to reassign" if payload["reassign"] else "",
            f"{len(payload['cancel'])} to cancel" if payload["cancel"] else "") if part)
        return {"content": [{"type": "text", "text": f"Recorded: {said}. The run applies it at its next look at the cards."}],
                "details": {"run_id": run["id"], "node_id": node["id"]}}

    return ToolDefinition(name="misaka_research_cards", label=PLAN_CHANGE_DESCRIPTION, description=PLAN_CHANGE_DESCRIPTION,
                          parameters=PlanChange, execute=execute, promptSnippet=PLAN_CHANGE_DESCRIPTION)


RECONCILE_DESCRIPTION = ("Root Last Order: record this level's reconciliation -- which pending options open as nodes "
                         "(options asking the same question share one node), which finished nodes a join carries "
                         "forward, which options are not pursued and why, whether each coverage claim across lines "
                         "holds, and how nodes relate")


def _run_owner(session_file, ctx):
    manager = getattr(ctx, "sessionManager", None)
    path = getattr(manager, "sessionFile", None)
    if not path or os.path.realpath(path) != os.path.realpath(session_file):
        raise ValueError("This command must come from the owning Last Order conversation.")
    return path


def reconcile_tool(con, run, *, round, validate, session_file):
    """The reconciliation command for the root Last Order's turn at a level barrier. A run-level
    command: the root node has usually finished its own work by then, so ownership is the run's
    (active, not stopping, this driver's lease), not a node's. A second call replaces the first."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = Reconcile.model_validate(raw).model_dump()
        validate(payload)
        path = _run_owner(session_file, ctx)
        runs.record_reconcile(con, run, round, payload, session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": f"Accepted reconciliation {round}."}],
                "details": {"run_id": run["id"], "reconcile": round}}

    return ToolDefinition(name="misaka_research_reconcile", label=RECONCILE_DESCRIPTION,
                          description=RECONCILE_DESCRIPTION, parameters=Reconcile, execute=execute,
                          promptSnippet=RECONCILE_DESCRIPTION)


def reconcile_review_tools(con, run, *, round, validate, session_file):
    """While a reconciliation waits for the user's go-ahead: revise it (the earlier go-ahead no
    longer counts) or start it once the user has agreed in conversation."""
    async def start(call_id, raw, _signal, _on_update, ctx):
        Start.model_validate(raw)
        _run_owner(session_file, ctx)
        current = runs.reconcile(con, run["id"], round)
        if current is None or current["applied_at"] is not None:
            raise ValueError("No reconciliation is waiting for the user's go-ahead right now.")
        runs.approve_reconcile(con, run, round)
        return {"content": [{"type": "text", "text": "Started: the reconciliation is being applied now."}],
                "details": {"run_id": run["id"], "reconcile": round}}

    start_description = ("Apply the recorded reconciliation. Call it once the user has agreed, in conversation, "
                         "to the nodes it opens and the options it leaves; until then it only waits.")
    return [reconcile_tool(con, run, round=round, validate=validate, session_file=session_file),
            ToolDefinition(name="misaka_research_start", label=start_description, description=start_description,
                           parameters=Start, execute=start, promptSnippet=start_description)]


def consent_tool(con, run, *, session_file):
    """While the final report waits because a conclusion dissolves the research question: record the
    user's decision, taken in conversation with the root Last Order."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = Consent.model_validate(raw).model_dump()
        path = _run_owner(session_file, ctx)
        runs.record_run_action(con, run, runs.CONSENT_KEY, payload, session_file=path, tool_call_id=call_id)
        word = "agreed" if payload["agreed"] else "did not agree"
        return {"content": [{"type": "text", "text": f"Recorded: the user {word}. The final report goes ahead."}],
                "details": {"run_id": run["id"], "agreed": payload["agreed"]}}

    description = ("Record the user's decision on whether the final answer may dissolve the research question. "
                   "Call it once the user has decided in conversation; the final report waits for it.")
    return ToolDefinition(name="misaka_research_consent", label=description, description=description,
                          parameters=Consent, execute=execute, promptSnippet=description)


def relate_tool(con, run, *, session_file, validate):
    """For the survey of a finished run: record connections between nodes seen only once they are all read."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = validate(Relate.model_validate(raw).model_dump())
        _run_owner(session_file, ctx)
        added = runs.add_relations(con, run, payload["relations"])
        return {"content": [{"type": "text", "text": f"Recorded {added} relation(s) on the research graph."}],
                "details": {"run_id": run["id"], "relations": added}}

    description = ("Record connections between nodes of the research graph -- converge, diverge, resonates, "
                   "appropriates, displaces -- that the survey brings to light.")
    return ToolDefinition(name="misaka_research_relate", label=description, description=description,
                          parameters=Relate, execute=execute, promptSnippet=description)


def erratum_tool(con, run, node, *, session_file):
    """Record a correction against an earlier node's conclusion. That conclusion stays as written;
    the erratum goes beside it -- into the graph views, the context of every node below it and the
    final report. One record per call."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = Erratum.model_validate(raw).model_dump()
        target = runs.node(con, payload["node"])
        if target is None or target["run_id"] != run["id"] or target["id"] == node["id"]:
            raise ValueError("An erratum corrects another node of this run: name its id (see the graph).")
        path = _run_owner(session_file, ctx)
        runs.record_action(con, run, node, f"erratum:{call_id}", payload, session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": f"Recorded the correction against node {target['id']}."}],
                "details": {"run_id": run["id"], "node_id": node["id"]}}

    description = ("Record that an earlier node's conclusion states something wrong -- a fact, a date, an attribution, "
                   "a term or a reference that does not exist -- with the correction and what shows it")
    return ToolDefinition(name="misaka_research_erratum", label=description, description=description,
                          parameters=Erratum, execute=execute, promptSnippet=description)


def report_tool(con, run, *, name, description, model, key, session_file, reply):
    """A final-report stage's command: what the stage delivers is what this records, never a reply.
    ``key(payload)`` names the record, and refuses a payload that belongs to no part of the report."""
    async def execute(call_id, raw, _signal, _on_update, ctx):
        payload = model.model_validate(raw).model_dump()
        record_key = key(payload)
        path = _run_owner(session_file, ctx)
        runs.record_run_action(con, run, record_key, payload, session_file=path, tool_call_id=call_id)
        return {"content": [{"type": "text", "text": reply(payload)}],
                "details": {"run_id": run["id"], "action_key": record_key}}

    return ToolDefinition(name=name, label=description, description=description, parameters=model,
                          execute=execute, promptSnippet=description)
