"""Last Order's research calls and the card contracts, with a deliberately small machine-checked envelope.

The prose is open-ended. Python validates only the fields needed to route
work; it never judges whether a method, source, interpretation, or conclusion is sound.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from misaka.core.platform import budget, prompt_guard
from misaka.core.platform import tasks as task_store
from misaka.core.research import commands, ledger, runs
from misaka.core.session_manager import find_most_recent_session
from misaka.utils import atomic

ROOT_CONTRACT = """# Research plan — design only
Do not answer the user's question at this stage. Your only deliverable is a research design.
Do not assume that textbooks, mass media, the mainstream view, or the contrarian view is correct.

Start by working out what the user is actually asking, what else the question could mean, and which of its
premises are untested. Identify the relevant objects, processes, interactions, prior knowledge, time horizons
and disciplines without expanding beyond the agreed question and limits. Specify evidence needs, deliverables,
dependencies and acceptance criteria. Offer suitable methods, theories and source strategies with their blind
spots and competing approaches; Sisters choose and refine the specialist implementation.
Method Skills are a menu, not a mandatory research template: combine, reject or add methods as the problem demands.
Check for important missing dimensions. Consult available coverage maps or `coverage_scan` when they would improve
the design, not a fixed number of times on every node. Index counts are discovery signals, not proof of relevance
or completeness, particularly across languages, archives and interpretive traditions. Delegate substantial material
gathering to Sisters instead of doing their assignments during planning.
Choose Sisters by fit from the system catalog and explain each choice. End `plan_markdown` with "Coverage maps used":
record what informed the coverage check (or why no scan was needed or available), useful dimensions and remaining gaps.

Call `misaka_research_assign` with your plan and assignments.
This tool records your dispatch command; research cards are created only from an accepted call, never from
JSON in your final answer or a submission file.
After the tool accepts your command, end with a short plain-text summary.

Rules:
- Never force a question into PICO, a causal-variable model, or any other single-discipline template.
- A required human choice is asked in conversation when a user is talking to you; status=clarify with
  clarifying_questions is only for a run nobody is talking to. Uncertainty that can be researched belongs in a research task.
- Tasks must be substantive research assignments written for this question, not mechanical templates.
- The red team is the Sister whose profile makes her the best critic of this plan's conclusion; only you decide who that is.
"""


OPTIONAL_MATERIAL_TOOLS = ("x_search", "browser_navigate", "browser_snapshot", "browser_get_images", "browser_vision")
MATERIAL_TOOLS = ("read", "misaka_research_view", "web_search", "web_fetch", "web_extract", "download_file",
                  "doc_list", "doc_outline", "doc_read", "doc_find", "doc_page_image", "doc_add", *OPTIONAL_MATERIAL_TOOLS)
RESEARCH_TOOLS = (*MATERIAL_TOOLS, "coverage_scan", "skills_list", "skill_view")

# A research Sister's working rules, appended once to her system prompt (worker.card_session_setup)
# rather than repeated in every card. The card body keeps only what is specific to that task.
RESEARCH_SISTER_DISCIPLINE = """[Research card]
You are one researcher on a research tree that Last Order coordinates. How to work:
- Back every empirical claim with a traceable source and an exact quotation or precise location; read the saved
  material before citing it.
- Keep facts, inferences, interpretations and normative judgements apart, and say which is which.
- Record counterevidence, competing explanations and unresolved questions as you meet them. At the start of a
  substantive investigation, identify evidence that could overturn the working premise and seek it deliberately;
  revise that check when the premise changes rather than repeating a ritual before every search.
- Note whether your sources are independent of one another: three retellings of one source are one source.
- Authority, mainstream or contrarian opinion, and the task's own premise are not evidence.
- When material cannot be obtained you may still conclude, with reservations: name what is missing and what it
  would settle.
- Declare findings and concrete uncertainties with `misaka_card_note` as you work: `text`, `claim_type`
  (fact | inference | interpretation | normative), `source_file` or `doc_id` + `page`, optional `quote`.
  Notes accumulate; a correction says which earlier declaration it revises. The ledger records without judging;
  the red team and Last Order weigh it later.
- Use the available colleague directory and communication tools for advice or missing material. Follow the
  communication tool's input-request protocol only when an external decision or input is indispensable.
- This run's conversations -- Last Order's with the user, every Sister's -- are listed under Conversations in
  `misaka_research_view(view="workspace", run_id=...)`; `lcm_grep(session_scope="session", session_id=...)`
  searches one and `lcm_load_session` opens it. Sessions not listed there belong to other work; leave them alone.
"""


def session_tools(worker, tools=RESEARCH_TOOLS):
    """Keep already-enabled execution/extension capabilities, not Board/library mutation tools.

    New registrations are considered on the NEXT phase, not allowed to widen a running one.
    The registry still enforces the owning session's role/user permission ceiling.
    """
    session = getattr(worker, "session", None)
    # A new bare coordinator has no prior selection to inherit. Bootstrap the
    # same execution capabilities for one-shot calls and resident nodes.
    extra = ["bash", "office"] if session is None else []
    if session is not None:
        active = set(session.getActiveToolNames())
        for tool in session.getAllTools():
            if tool.name in active and (
                tool.name in {"bash", "powershell", "office", "browser_exec"}
                or tool.name.startswith("mcp__")
                or tool.sourceInfo.source not in {"builtin", "sdk"}
            ):
                extra.append(tool.name)
    return tuple(dict.fromkeys((*tools, *extra)))

SOURCES_FOOTER = """
End with a `## Sources` section listing every source this text rests on, one per line: the file's path inside the
project (`downloads/...`, `nodes/...`) or its `doc:<id>#p<n>` locator, plus the URL it was fetched from when it came
from the web. A program reads that section: whatever it does not list is not bundled beside this text.
"""

MARKDOWN_OUTPUT = """\nReturn the complete answer as free-form assistant Markdown, not JSON, in this turn.
The workflow saves your final assistant message automatically. Do not call a tool to submit or save this answer.
"""


def navigation(run_id):
    return f"""\n# Research context
Current workspace lookup: misaka_research_view(view="workspace", run_id="{run_id}").
"""


SYNTHESIS_FOLLOWUP = """
# Round {round}: conclude, or ask for more material ({left} more round(s) may still be asked for)
Do not write a conclusion the material cannot carry. If what the cards brought back leaves the question
unanswerable, call `misaka_research_assign` with a follow-up round of cards: say in `plan_markdown` what this
round left open and why each new card closes it, and end this turn with a short note; the conclusion is written
after those cards return, from all rounds together. A follow-up round is for the same question with missing
material; a doubt about the conclusion itself belongs to the red team, which reviews what you write next, and
issues it raises open child nodes -- do not use follow-up rounds to review yourself.
"""

SYNTHESIS_LAST_ROUND = """
# Round {round}: the last round
No further cards can be assigned on this node. Write the conclusion from what there is, and name what remains
unsupported and what evidence would settle it.
"""

SYNTHESIS_CONTRACT = """# Node conclusion — synthesize the submitted research output
Read full sources in context; summaries and the ledger are researchers' declarations, not certified evidence.

Separate shared findings, competing findings, key evidence, counterevidence,
methodological limits, value premises, and unresolved questions. Give traceable source paths, document locations or URLs.
Do not vote or hide competing interpretations or insufficient evidence. Source checks and limited supplementary
retrieval needed to assess returned evidence are part of synthesis; preserve and cite any new material for the red team.
Use the available follow-up assignment for substantive new research; when none remains, state the unresolved gap.
State what new evidence could change the judgement.
This run's conversations -- yours with the user, every Sister's -- are listed under Conversations in
`misaka_research_view(view="workspace", run_id=...)`; `lcm_grep(session_scope="session", session_id=...)` searches
one and `lcm_load_session` opens it. Sessions not listed there belong to other work; leave them alone.
""" + SOURCES_FOOTER + MARKDOWN_OUTPUT


RED_TEAM_CONTRACT = """## goal
Red-team the conclusion at `{synthesis_path}` for "{question}". Hunt for reasoning failures; do not extend the report and do not
polish prose. Deliver `critique.md` for the node Last Order to read. Record your issues with `misaka_card_note(issues=...)`.
This returns the review to that Last Order; it does not start investigations on your behalf.

## material
- Conclusion under review: `{synthesis_path}`
- Plan (every round of this node, oldest first): {plan_path}
- Source-task artifacts: read whichever the conclusion cites.
{own_cards}{deliberation}{evidence}
## what to inspect
Facts and quotations, inference and causation, concepts and scope, methods and sampling, standpoint and bias, omitted actors or
processes, interactions, time horizons, consequences, and normative claims disguised as facts. Use available coverage maps
or literature scans when useful to assess a suspected omission; an unmentioned dimension is not automatically a defect.
Explain how an omission materially affects this question within its agreed scope. Different frameworks can yield different interpretations
without either side being automatically wrong. A false objection does as much damage as a false claim.

## acceptance criteria
- `critique.md` exists and every criticism explains its significance and the evidence, correction or concrete check needed to resolve it.
- Call `misaka_card_note` with the complete `issues` list: kind, question, rationale, priority, material.
  Use issues=[] explicitly if there are no issues. Only material=true issues require investigation.
- Write the review under the card's deliverable directory. Do not create a machine-readable submission file.

"""


def sister_catalog(root=None, *, workspace=None):
    # Compatibility entry for CLI callers; parsing and filtering have one owner.
    from misaka.core.network.roster import capability_catalog
    return capability_catalog(root, workspace=workspace)


def _catalog_text(items):
    return json.dumps(items, ensure_ascii=False, indent=2)


def _call(worker, cfg, prompt, *, cwd, session_dir, continue_session=False,
          profile="last_order", tools=RESEARCH_TOOLS, raw=False, task_id=None,
          thinking="high", model=None, extra_tools=(), con=None, session_file=None, sister_catalog=None):
    kwargs = {
        "cwd": cwd, "tools": list(session_tools(worker, tools)),
        "timeout": None, "soul": False, "research_context": True,
        "raw": raw, "usage_db": cfg.get("db"), "usage_task_id": task_id,
        "usage_generation": 1, "usage_token_cap": cfg.get("token_cap"),
        "session_dir": session_dir, "continue_session": continue_session,
        "thinking": thinking,
    }
    if session_file:
        kwargs["session_file"] = session_file
    if extra_tools:
        kwargs["extra_tools"] = extra_tools
    if sister_catalog is not None:
        kwargs["sister_catalog"] = sister_catalog
    if model:
        kwargs["model"] = model
    while True:
        result = worker.run_llm_json(
            os.path.join(cfg["roles_root"], profile), prompt,
            cfg["provider"], cfg["default_model"], **kwargs,
        )
        if (result[2] != "shared token budget exhausted" or con is None
                or budget.exhausted(con, cfg.get("token_cap"))):
            return result
        if runs.stop_requested(con, task_id):
            return None, "", "Research stopped while waiting for token capacity"
        # Other node LOs share this ledger. A reservation is
        # backpressure, not a failed model call; no tokens were spent on this refusal.
        time.sleep(2)


def publish_project_brief(workspace, plan, *, round=1):
    """The accepted root plan is also the brief; no separate model or conversation. A later
    round of the root is appended as its own section, never written over the brief."""
    path = Path(workspace) / "PROJECT.md"
    if plan["status"] != "ready":
        return str(path)
    if not path.is_file():
        atomic.write_text(str(path), plan["plan_markdown"].rstrip() + "\n")
    elif round > 1:
        marker = f"\n\n## Round {round}\n\n"
        current = path.read_text(encoding="utf-8")
        if marker.strip() not in current:
            atomic.write_text(str(path), current.rstrip() + marker + plan["plan_markdown"].rstrip() + "\n")
    return str(path)


def _validate_task(raw, roster, index):
    if not isinstance(raw, dict):
        raise ValueError(f"Task {index} is not an object.")  # noqa: TRY004 - callers treat bad input as ValueError
    required = ("local_id", "title", "question", "rationale", "deliverable", "assignee")
    task = {k: raw.get(k) for k in raw}
    for key in required:
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f"Task {index} is missing {key!r}.")
        task[key] = task[key].strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", task["local_id"]):
        raise ValueError(
            f"Task {index} local_id may contain only letters, digits, dots, underscores, and hyphens."
        )
    if task["assignee"] not in roster:
        raise ValueError(
            f"Task {task['local_id']} names a Sister outside the roster: {task['assignee']}"
        )
    for key in ("method", "source_strategy", "falsifiers", "assignee_reason"):
        task[key] = str(task.get(key) or "").strip()
    for key in ("dependencies", "capabilities"):
        value = task.get(key)
        task[key] = [str(x).strip() for x in value or [] if str(x).strip()] if isinstance(value, list) else []
    try:
        task["priority"] = int(task.get("priority") or 0)
    except (TypeError, ValueError):
        task["priority"] = 0
    task["extensions"] = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    return task


def _validate_tasks(raw_tasks, roster_ids):
    tasks = [_validate_task(raw, roster_ids, i) for i, raw in enumerate(raw_tasks or [], 1)]
    local_ids = [task["local_id"] for task in tasks]
    if len(local_ids) != len(set(local_ids)):
        raise ValueError("Last Order returned duplicate task local_id values.")
    known = set(local_ids)
    for task in tasks:
        unknown = set(task["dependencies"]) - known
        if unknown:
            raise ValueError(
                f"Task {task['local_id']} refers to unknown dependencies: {sorted(unknown)}"
            )
        if task["local_id"] in task["dependencies"]:
            raise ValueError(f"Task {task['local_id']} depends on itself.")
    pending = {task["local_id"]: set(task["dependencies"]) for task in tasks}
    while pending:
        ready = {local_id for local_id, deps in pending.items() if not (deps & pending.keys())}
        if not ready:
            raise ValueError("Research task dependencies contain a cycle.")
        for local_id in ready:
            pending.pop(local_id)
    return tasks


def validate_plan(obj, roster):
    if not isinstance(obj, dict):
        raise ValueError("Last Order planning output is not an object.")  # noqa: TRY004 - callers treat bad input as ValueError
    status = obj.get("status")
    if status not in {"ready", "clarify"}:
        raise ValueError("Last Order returned an invalid planning status.")
    plan_markdown = obj.get("plan_markdown")
    if not isinstance(plan_markdown, str) or not plan_markdown.strip():
        raise ValueError("Last Order returned an incomplete plan_markdown value.")
    roster_ids = {r["id"] if isinstance(r, dict) else str(r) for r in roster}
    tasks = _validate_tasks(obj.get("tasks"), roster_ids)
    if status == "ready" and not tasks:
        raise ValueError("A ready research plan must contain at least one task.")
    questions = obj.get("clarifying_questions") or []
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("clarifying_questions must be a list of non-empty strings.")
    if status == "clarify" and not questions:
        raise ValueError("A clarify plan must ask at least one question.")
    red_team = obj.get("red_team") if isinstance(obj.get("red_team"), dict) else {}
    if status == "ready" and red_team.get("assignee") not in roster_ids:
        raise ValueError("Last Order must name one roster Sister as the red team.")
    return {
        **obj, "status": status, "plan_markdown": plan_markdown.strip(), "tasks": tasks,
        "red_team": {"assignee": red_team.get("assignee"), "reason": str(red_team.get("reason") or "")},
        "clarifying_questions": [q.strip() for q in questions],
        "reframed_question": " ".join(str(obj.get("reframed_question") or "").split()),
        "methods": obj.get("methods") if isinstance(obj.get("methods"), list) else [],
        "extensions": obj.get("extensions") if isinstance(obj.get("extensions"), dict) else {},
    }


def _roster(cfg):
    roster = sister_catalog(cfg.get("profiles_root"), workspace=cfg.get("workspace"))
    if not roster:
        raise RuntimeError("The Sister roster is empty; research tasks cannot be assigned.")
    return roster


def _lo_session(run, node, *parts):
    if node["parent_id"] is None and run["root_session"]:
        return os.path.join(os.path.dirname(run["root_session"]), *parts)
    return runs.session_dir(run, "root-lo" if node["parent_id"] is None else f"node-{node['id']}", *parts)


def plan_waits_for_user(cfg, worker):
    """Whether an accepted plan waits for the user's go-ahead before its cards are created: on by
    configuration, and only where the Last Order has a live conversation the user can join (a
    window, or a node's resident session); a one-shot headless call has nowhere to talk."""
    return bool(cfg.get("research_plan_approval", True)) and getattr(worker, "session", None) is not None


PLAN_WAITS = """
# The plan waits for the user
An accepted plan is not executed until the user agrees to it -- this plan, here, whether it is the root's, a
fork's, or a follow-up round's. An ancestor's approval does not carry over, and the driver never starts a node on
its own: it waits for your `misaka_research_start`. After the tool accepts your command, present the
plan to the user in plain language and talk it over with them. Revise it with `misaka_research_assign` (each call
replaces the recorded plan). Once the user has said the plan should go ahead, call `misaka_research_start`; it
is available from the next turn on, so this turn ends with your summary. While a follow-up round waits,
`misaka_research_withdraw` drops it if the user would rather have the conclusion from what there is. If the user
would rather not research a fork node at all, `misaka_research_skip` (a fork's first plan only) closes it
unresearched: no cards, no conclusion, its issue parked for final adjudication with the reason you record. Never
fake a completion or write one into project files instead. Anything
you would otherwise put in
`clarifying_questions`, ask the user in that conversation instead; `status=clarify` is for runs nobody is
talking to. If the question itself seems wrong, propose the reframing in `plan_markdown` and put the new
wording in `reframed_question`: it takes effect only once the user agrees.
"""

PLAN_AUTOMATIC = """
# Plan execution
This phase has no additional plan-approval wait. After an accepted ready plan, the driver proceeds within the
run's approved scope and configured limits. Report the handoff; do not request a redundant go-ahead or claim the
research is complete. A genuine clarification, pause or change of scope still needs the appropriate decision.
"""


def plan_approval_prompt(cfg, worker):
    """Describe the same gate the driver applies, including follow-up plans."""
    return PLAN_WAITS if plan_waits_for_user(cfg, worker) else PLAN_AUTOMATIC


def plan(run, cfg, worker, node, *, con, context_path=None):
    """Open or continue the node's Last Order session and return its research plan."""
    session_dir = _lo_session(run, node)
    roster = _roster({**cfg, "workspace": run["workspace"]})
    prompt = ROOT_CONTRACT + "\n# Artifact layout\nWithin the current workspace, every node's files live under " \
        "nodes/<node>/ and each of its cards under nodes/<node>/cards/<card>/; run-level products go to final/. " \
        "The runtime assigns these paths. Use simple deliverable filenames, not directories.\n" \
        + f"\n# Current node depth\n{node['depth']} (root = 0; max_depth = {runs.limits(run)['max_depth']})\n"
    if node["parent_id"] is None:
        brief = Path(run["workspace"]) / "PROJECT.md"
        brief_instruction = (f"PROJECT.md already exists. Read `{brief}` and respect its scope."
                             if brief.is_file() else
                             "PROJECT.md does not exist yet. Do not try to read it. Begin plan_markdown "
                             "with the original question, research goal, assumptions to test, and boundaries. "
                             "After your plan is accepted, the driver writes it to PROJECT.md before any "
                             "Sister task starts; no separate write call or intake session is needed.")
        prompt += f"""
# Original question
{run['question']}

# Project brief and root plan are ONE deliverable
{brief_instruction}
Do not make a separate intake call or delegate this design to another Last Order.
Assumptions are questions, not conclusions.
"""
    else:
        prompt += f"""
This is a targeted research node. Investigate the red-team issue against the parent conclusion, without assuming it is correct, without replanning the whole project.

# Issue that opened this node
{node['trigger_text']}

# Context packet (ancestor sessions and artifact map; read it first)
{context_path}
"""
    prompt += navigation(run["id"]) + """
Read the live workspace view before planning.
"""
    prompt += plan_approval_prompt(cfg, worker)
    action, raw = _command(
        con, run, cfg, worker, node, prompt, key="plan", name="misaka_research_assign",
        description="Last Order: assign the research plan and choose its red-team Sister",
        model=commands.Plan, validate=lambda value: validate_plan(value, roster),
        session_dir=session_dir, tools=RESEARCH_TOOLS, sister_catalog=roster,
    )
    return action["payload"], raw, action["session_file"]


def _command(con, run, cfg, worker, node, prompt, *, key, name, description, model, validate,
             session_dir, tools=RESEARCH_TOOLS, sister_catalog=None):
    previous = runs.action(con, run["id"], node["id"], key)
    if previous:
        return previous, ""
    session_file = run["root_session"] if node["parent_id"] is None else node["session_file"]
    command = commands.tool(con, run, node, key=key, name=name, description=description,
                            model=model, validate=validate, session_dir=session_dir,
                            session_file=session_file)
    _obj, text, err = _call(
        worker, cfg, prompt, cwd=run["workspace"], session_dir=session_dir,
        tools=tools, extra_tools=(command,), raw=True, sister_catalog=sister_catalog,
        continue_session=bool(find_most_recent_session(session_dir)), task_id=run["id"], con=con,
        session_file=session_file,
    )
    accepted = runs.action(con, run["id"], node["id"], key)
    if not accepted:
        raise RuntimeError(f"Last Order did not call {name} for {key}: {err or 'no command accepted'}")
    return accepted, text


def task_sources(con, run, rows):
    """What each done card delivered, from frozen records only: its submitted event (this
    generation), its registered artifacts, and the evidence ledger."""
    from misaka.core.platform import cards

    parts = []
    for row in rows:
        payload = json.loads(task_store.latest_payload(
            con, row["id"], "submitted", generation=row["generation"]) or "{}")
        artifacts = []
        for item in runs.artifacts(con, run["id"], task_id=row["id"]):
            path = runs.artifact_path(item)
            if os.path.isfile(path):
                artifacts.append(path)
        finds = []
        for finding in ledger.findings(con, run["id"], task_id=row["id"]):
            finds.append({"text": finding["text"], "claim_type": finding["claim_type"],
                          "claims": [dict(claim) for claim in ledger.claims(con, finding["id"])]})
        # The ledger already holds what it accepted; only a submitted finding it does not hold
        # (rejected, or never ingested) is worth a second listing.
        recorded = {item["text"] for item in finds}
        submitted = [item for item in payload.get("findings", [])
                     if not (isinstance(item, dict) and item.get("text") in recorded)]
        parts.append({"task_id": row["id"], "title": row["title"],
                      "card_path": cards.card_path(row["workspace"], row["id"]),
                      "summary": str(payload.get("summary") or ""), "artifacts": artifacts,
                      "findings": finds, "submitted_findings": submitted,
                      "uncertain": payload.get("uncertain") or []})
    return parts


def evidence_block(con, run, node):
    """The node's declarations and source locators, wrapped as untrusted data."""
    findings = []
    for finding in ledger.findings(con, run["id"], branch_id=node["id"]):
        findings.append({**dict(finding),
                         "claims": [dict(row) for row in ledger.claims(con, finding["id"])]})
    return prompt_guard.untrusted("evidence-ledger", json.dumps(findings, ensure_ascii=False, indent=2))


def deliberation_text(session_file):
    """Last Order's own reasoning from this node's session, for the red team to probe: the
    thinking blocks and working prose of her assistant turns, and nothing else. Everything the
    red team already holds by other means is left out on purpose -- the user turns (the
    workflow's own prompts), tool calls and their arguments (the plan and conclusion she
    committed are given by path), tool results (the evidence and Sister artifacts are given
    separately), custom entries (a prior red-team receipt) and redacted thinking (no text to
    read). Returns "" when the session holds no such reasoning."""
    if not session_file or not os.path.isfile(session_file):
        return ""
    turns = []
    with open(session_file, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thinking" and not block.get("redacted"):
                    text = block.get("thinking")
                    if isinstance(text, str) and text.strip():
                        parts.append(("thinking", text.strip()))
                elif block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(("text", text.strip()))
            if parts:
                turns.append(parts)
    if not turns:
        return ""
    lines = ["# Last Order's deliberation", "",
             "Her thinking and working notes while planning and synthesizing this node, in order.",
             "Probe them for reasoning failures. They are not the conclusion and carry no authority.", ""]
    for index, parts in enumerate(turns, 1):
        lines.append(f"## turn {index}")
        for kind, text in parts:
            lines.append(f"### {kind}")
            lines.append(text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def synthesize(con, run, cfg, worker, node, task_rows, *, followup=None, round=1, left=0):
    """Last Order writes the node's conclusion from its accepted cards. Returns Markdown. With
    ``followup`` (the next round's plan tool) she may instead assign more cards; the caller sees
    that as the recorded action, not in the text. ``left`` is how many more rounds the node may
    still ask for; a node that could never ask (or is on its first and only round) hears nothing
    about rounds."""
    if followup is not None:
        rounds = (SYNTHESIS_FOLLOWUP.format(round=round, left=left)
                  + "\nThe following approval policy applies only if you submit a follow-up plan; "
                  "it does not block writing the current conclusion.\n" + plan_approval_prompt(cfg, worker))
    elif round > 1:
        rounds = SYNTHESIS_LAST_ROUND.format(round=round)
    else:
        rounds = ""
    # The material map lists every card's ledger findings with their claims; the ledger block
    # the red team gets would repeat all of them here, so it is left out of this turn.
    prompt = (SYNTHESIS_CONTRACT + rounds + f"""
# Question
{node['trigger_text']}

# Material map (read the artifacts, not just this map; each card's findings and their source claims are here)
""" + prompt_guard.untrusted("research-material-map",
                            json.dumps(task_sources(con, run, task_rows), ensure_ascii=False, indent=2))
              + navigation(run["id"]))
    session_dir = _lo_session(run, node)
    _obj, text, err = _call(
        worker, cfg, prompt, cwd=run["workspace"], session_dir=session_dir, raw=True, tools=RESEARCH_TOOLS,
        continue_session=bool(find_most_recent_session(session_dir)), task_id=run["id"], con=con,
        session_file=run["root_session"] if node["parent_id"] is None else node["session_file"],
        extra_tools=(followup,) if followup is not None else (),
    )
    if followup is not None and runs.plan_round(con, run["id"], node["id"]) > round:
        return ""                                     # she asked for another round instead of concluding
    if err or not text or not text.strip():
        raise RuntimeError(f"Synthesis for node {node['id']} is empty or failed: {err or ''}")
    return text.strip() + "\n"


def red_team_body(node, *, synthesis_path, plan_path, evidence="", deliberation_path=None, own_cards=()):
    own = ("- Cards on this node you researched yourself, in an earlier session of yours (this review is not "
           "third-party to them; hold them to the same standard): "
           + ", ".join(f"[{row['id']}] {row['title']}" for row in own_cards) + "\n") if own_cards else ""
    deliberation = (
        "- Last Order's deliberation -- her thinking and working notes while planning and synthesizing, "
        "to probe for reasoning failures; not the conclusion, and no authority over it: "
        f"`{deliberation_path}`\n" if deliberation_path else "")
    return RED_TEAM_CONTRACT.format(
        question=node["trigger_text"], synthesis_path=synthesis_path, plan_path=plan_path,
        deliberation=deliberation, own_cards=own,
        evidence=f"- Evidence ledger:\n{evidence}\n" if evidence else "")


def fork_session(source, target_dir):
    """Fork the owning session, never the newest unrelated chat in its directory."""
    from misaka.core.session_manager import SessionManager
    if not source:
        return None
    os.makedirs(target_dir, exist_ok=True)
    manager = SessionManager.open(source, target_dir)
    leaf = manager.getLeafId()
    if not leaf:
        return None
    branched = manager.createBranchedSession(leaf)
    if branched and not os.path.isfile(branched):
        manager.rewrite_file()  # the write is deferred until an assistant turn; the fork resumes from disk
    return branched


INVESTIGATE_CONTRACT = """# Assign follow-up investigations from this node's completed review
Read the full review text and recorded issues supplied below. Below max_depth, formulate an assignment for EVERY material
issue and call `misaka_research_investigate` with its issue_id and assignment. Each assignment directly
creates a formal child node at depth + 1, forked from YOUR session. That fork plans, assigns its own
Sisters, synthesizes and runs its chosen red team, exactly like this node. There is no preliminary
probe, verdict handback, promotion or second fork. Do not investigate the issue yourself or pre-judge it.
Do not dismiss, merge or silently omit an issue. This dispatch turn runs only when material issues
need child research and the depth limit permits it. End in plain text after the tool accepts.
"""


def review_context(con, run, red, issues):
    """The full frozen review, shared by active dispatch and model-free receipt."""
    review = task_sources(con, run, [red])
    review[0]["review_text"] = [
        {"path": runs.artifact_path(item), "content": runs.artifact_text(item)}
        for item in runs.artifacts(con, run["id"], kind="critique", task_id=red["id"])
        if Path(item["path"]).suffix.lower() == ".md"
    ]
    return ("\n# Red-team review\n" + prompt_guard.untrusted("red-team-results", _catalog_text(review))
            + "\n# Recorded material issues\n" + prompt_guard.untrusted("issues", _catalog_text([dict(i) for i in issues])))


def investigate(con, run, cfg, worker, node, red):
    accepted = runs.action(con, run["id"], node["id"], "investigate")
    if accepted:
        return accepted["payload"]["assignments"]  # resume dispatch, not the already completed review
    issues = list(runs.issues(con, run["id"], node_id=node["id"]))
    if node["depth"] >= runs.limits(run)["max_depth"]:
        raise ValueError("Depth limit reached; no Last Order dispatch turn is needed.")
    expected = {item["id"] for item in issues}
    if not expected:
        raise ValueError("No material issues; no Last Order dispatch turn is needed.")

    def validate(value):
        ids = [item["issue_id"] for item in value["assignments"]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError("Assign every recorded material issue exactly once; use only this node's issue IDs.")
        return value

    prompt = (INVESTIGATE_CONTRACT + f"\n# Depth\n{node['depth']} / {runs.limits(run)['max_depth']}\n"
              + review_context(con, run, red, issues) + navigation(run["id"]))
    action, _raw = _command(
        con, run, cfg, worker, node, prompt, key="investigate", name="misaka_research_investigate",
        description="Last Order: assign next-depth fork LO nodes for material red-team issues",
        model=commands.Investigations, validate=validate, session_dir=_lo_session(run, node),
    )
    return action["payload"]["assignments"]


def task_body(task, *, run_id=None, node=None, siblings=(), previous=()):
    """The card's own contract: only what this task needs to say. The researcher's working rules
    are RESEARCH_SISTER_DISCIPLINE (her system prompt, once per session) and the tools' own
    guidelines, so they are not repeated here. ``node`` is the branch the card belongs to (its
    question is the larger one this card serves); ``siblings`` are the plan's other task specs on
    that node, so she knows whom she can ask; ``previous`` are the node's cards from earlier rounds
    (rows with title and output_dir), whose outputs this round builds on."""
    approach = """## Execution approach
Briefly outline your approach in ordinary prose: sources and methods, important risks, and when to stop.
Then use your tools and carry out the task in this same session; do not stop after the outline. Revise the approach
when evidence warrants it and explain why. No separate planning submission, file, or approval is required.
"""
    if run_id is not None:
        approach += navigation(run_id)
    if "instructions" in task:
        return task["instructions"].rstrip() + "\n\n" + approach
    larger = ""
    trigger = node["trigger_text"] if node is not None else None
    if trigger:
        larger = (f"\n## the larger question\n{trigger}\n"
                  "This card is one piece of it; the rationale above says which piece.\n")
    others = [spec for spec in siblings if spec.get("local_id") != task.get("local_id")]
    company = ""
    if previous:
        company += ("\n## earlier cards on this node\nAn earlier round already brought these back; read them "
                    "before doing anything they already did:\n"
                    + "\n".join(f"- [{row['id']}] {row['title']} → `{row['output_dir']}`" for row in previous) + "\n")
    if others:
        company = ("\n## sibling cards\nOther cards on the same node, in parallel with yours "
                   "(reach their Sisters with `SendMessage`):\n"
                   + "\n".join(f"- {spec.get('local_id')} · {spec.get('title')} → Sister {spec.get('assignee')}"
                               for spec in others) + "\n")
    return f"""## research question
{task['question']}

## rationale
{task['rationale']}
{larger}{company}
{approach}
## deliverable
{task['deliverable']}
Write it under the deliverable location the card names; successful writes and fetched source files are recorded
automatically.

## boundaries
Where material cannot be obtained you may still conclude -- with the gap named, and what it would settle.

## acceptance criteria
- At least one Markdown deliverable exists.
- Findings and concrete uncertainties are declared with `misaka_card_note` (`text`, `claim_type`
  fact|inference|interpretation|normative, `source_file` or `doc_id` + `page`, optional `quote`).
"""
