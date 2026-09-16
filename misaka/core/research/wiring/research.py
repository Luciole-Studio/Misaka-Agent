"""The ``/research`` slash command: the human-only entry point to the persistent Research Workflow."""
from __future__ import annotations

import asyncio
import os
import shlex

from misaka.config import CFG, current_config
from misaka.core.moments import CoreCommand
from misaka.core.platform import budget
from misaka.core.platform import tasks as task_store
from misaka.core.research import node as research_node
from misaka.core.research import runs, workflow
from misaka.ui.tui.interactive.components.ask_user_question import (
    AskUserQuestionComponent,
)
from misaka.utils.values import read_field

_CON = None
_DEPTH_QUESTION = "How deep should this research run go?"
_PARALLEL_QUESTION = "How many Last Order nodes may run at once?"
_ROUNDS_QUESTION = "After its first cards are back, how many more times may a node send its Sisters out before it concludes?"
USAGE = (
    "Usage: /research [DEPTH] [--parallel N] [--followups N] [QUESTION]\n"
    f"       Start research at once (depth defaults to {runs.DEFAULT_LIMITS['max_depth']}, LO parallelism to "
    f"{runs.DEFAULT_LIMITS['parallel']}, follow-up rounds per node to {runs.DEFAULT_LIMITS['max_followups']}).\n"
    "       Bare /research lets you pick depth, LO parallelism and follow-ups; your next message is the question.\n"
    "       Every node's plan waits for your go-ahead: talk it over with that Last Order (the root's in this\n"
    "       window, a fork's in its own tab) and she starts it once you agree; MISAKA_RESEARCH_PLAN_APPROVAL=0 turns this off.\n"
    "       Options precede QUESTION; --depth N is also accepted. Sister slots per LO are unchanged.\n"
    "       --followups N: after its first cards are back, how many more times a node may send its Sisters out\n"
    "       before it concludes (0-6). Talking a plan over with you is never counted.\n"
    "       /research status [RUN_ID]          show the latest run of this folder, or the run you name\n"
    "       /research stop [RUN_ID]            ask the latest active run (or RUN_ID) to stop\n"
    "       /research resume [RUN_ID] [ANSWER] resume a paused run, optionally answering its clarification questions"
)


def _con():
    global _CON
    if _CON is None:
        _CON = task_store.connect(CFG["db"])
        runs.init(_CON)
    return _CON


def _cfg():
    """Small seam for tests and alternate frontends; production returns the live config."""
    return current_config()


def parse_command(raw):
    line = (raw or "").strip()
    head = line.split(None, 1)[0] if line else ""
    tokens: list[str] = []
    if head == "resume":
        # `resume [RUN_ID] [ANSWER...]`: the answer is free text, taken from the raw line.
        words = line.split(None, 2)
        run_id = words[1] if len(words) > 1 and words[1].startswith("r_") else None
        rest = (words[2] if len(words) > 2 else "") if run_id else line[len("resume"):]
        return {"action": "resume", "run_id": run_id, "clarification": rest.strip()}
    if head in {"help", "-h", "--help", "status", "stop"}:
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            raise ValueError(f"Unclosed quote in arguments: {error}") from error
    if tokens and tokens[0] in {"help", "-h", "--help"}:
        return {"action": "help"}
    if tokens and tokens[0] == "status":
        if len(tokens) > 2:
            raise ValueError(USAGE)
        return {"action": "status", "target": tokens[1] if len(tokens) == 2 else None}
    if tokens and tokens[0] == "stop":
        if len(tokens) > 2:
            raise ValueError(USAGE)
        return {"action": "stop", "run_id": tokens[1] if len(tokens) == 2 else None}
    # Activation: [start] [DEPTH] [--depth N] [--parallel N] [QUESTION...]. Use the raw
    # line, not the shlex tokens: an apostrophe in "Stalin's constitution" is not an open quote.
    line = (raw or "").strip()
    words = line.split(None, 1)
    if words and words[0] == "start":
        line = words[1].strip() if len(words) > 1 else ""
        words = line.split(None, 1)
    explicit = bool(words)
    selected = {}
    while words:
        option, separator, value = words[0].partition("=")
        if option in {"--depth", "--parallel", "--followups"}:
            key = {"--depth": "max_depth", "--parallel": "parallel", "--followups": "max_followups"}[option]
            if key in selected:
                raise ValueError(f"{option} was supplied more than once.")
            if not separator:
                words = words[1].split(None, 1) if len(words) > 1 else []
                value = words[0] if words else ""
            if not value:
                raise ValueError(f"{option} requires an integer.\n{USAGE}")
            selected[key] = value
        elif "max_depth" not in selected:
            try:
                selected["max_depth"] = int(words[0])
            except ValueError:
                break  # the rest is the question, including any apostrophes or options within it
        else:
            break
        line = words[1].strip() if len(words) > 1 else ""
        words = line.split(None, 1)
    limits = runs.normalize_limits(selected)
    return {"action": "activate", "depth": limits["max_depth"], "parallel": limits["parallel"],
            "rounds": limits["max_followups"], "explicit": explicit, "question": line}


def _workspace(ctx):
    return task_store.canonical_workspace(getattr(ctx, "cwd", None) or os.getcwd())


def _find_run(con, target, workspace, *, active=False):
    if target:
        return runs.get(con, target)
    return runs.latest(con, workspace=workspace, active_only=active)


def _status(con, target, workspace):
    run = _find_run(con, target, workspace)
    if not run:
        return "No matching research run was found."
    value = runs.summary(con, run["id"])
    reading = budget.status(con, _cfg().get("token_cap"))
    scope = {run["id"], *(t["id"] for t in runs.tasks(con, run["id"]))}
    token_text = f" | tokens added by this run {budget.spent(con, task_ids=scope):,}"
    if reading["cap"]:
        token_text += f" | global tokens {reading['used']:,}/{reading['cap']:,}"
    return (
        f"{value['id']} | project {runs.project_name(run)!r} ({value['workspace']}) | "
        f"{value['status']}/{value['phase']} | depth {value['wave']}/{value['limits']['max_depth']} | "
        f"LO parallelism {value['limits']['parallel']} | follow-ups per node {value['limits']['max_followups']} | "
        f"tasks {value['tasks']} | nodes {value['nodes']} | open issues {value['open_issues']}"
        + token_text
        + (f" | error {value['last_error']}" if value["last_error"] else "")
    )


class ResearchPart:
    """Last Order's human-only ``/research`` command and run drivers."""

    def __init__(self, worker=None):
        drivers = {}     # this session's research drivers; another session's are not ours to stop
        con = _con()
        # In a pane, a fork node is a pane too (its own tab beside this one); elsewhere a
        # background process.
        spawner = research_node.PaneSpawner() if os.environ.get("MISAKA_NET_PANE") else research_node.ProcessSpawner()
        self.tools = []
        self.commands = []
        self.session = None

        def send_progress(content, *, run_id=None, details=None):
            payload = dict(details or {})
            if run_id:
                payload["run_id"] = run_id
            self.session.moments.send_message(
                {"customType": "research-progress", "display": True,
                 "content": content, "details": payload},
                {"deliverAs": "followUp", "triggerTurn": False},
            )

        async def drive(run_id, ctx, *, resume=False, clarification=""):
            try:
                def progress(event):
                    content = f"Research `{run_id}` | {event['message']}"
                    if event.get("tasks"):
                        content += "\n" + "\n".join(
                            f"- {item['title']} → Sister {item['assignee']}"
                            for item in event["tasks"])
                    send_progress(content, run_id=run_id, details=event)

                result = await workflow.run(
                    con, dict(_cfg()), spawner, worker, run_id=run_id, progress=progress, resume=resume, clarification=clarification,
                    origin_session=getattr(getattr(ctx, "sessionManager", None), "sessionId", None), session=self.session)
                if result["reason"] == "waiting_input":
                    questions = "\n".join(f"- {q}" for q in result.get("questions") or [])
                    content = (f"""Research run `{run_id}` needs clarification:
    {questions}

    Continue with `/research resume {run_id} YOUR_ANSWER`.""")
                    kind = "research-clarification"
                    self.session.moments.send_message(
                        {"customType": kind, "display": True, "content": content,
                         "details": {"run_id": run_id, "result": result.get("run")}},
                        {"deliverAs": "followUp", "triggerTurn": False},
                    )
                else:
                    final = result.get("final") or {}
                    content = final.get("content") or f"Research run `{run_id}` finished: {result['reason']}."
                    final_artifact = final.get("artifact") if result["reason"] == "done" else None
                    if final_artifact:
                        content = (f"# Delivered final report — `{run_id}`\n\n"
                                   "This is the authoritative version; it supersedes this run's working drafts.\n"
                                   f"Report: `{final['path']}`\n\n---\n\n" + content)
                    if final.get("survey_path"):
                        content += f"\n\n---\nSurvey by node: `{final['survey_path']}`"
                    self.session.moments.send_message(
                        {"customType": "research-final", "display": True,
                         "content": content,
                         "details": {"run_id": run_id, "result": result.get("run"), "final_artifact": final_artifact}},
                        {"deliverAs": "followUp", "triggerTurn": False,
                         **({"_deliveryId": f"research-final:{run_id}:{final_artifact}"} if final_artifact else {})},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - infrastructure failures stay visible, and the run stays resumable
                self.session.moments.send_message(
                    {"customType": "research-error", "display": True,
                     "content": f"Research run `{run_id}` paused: {type(error).__name__}: {error}\n"
                                f"Continue with `/research resume {run_id}` after fixing the problem.",
                     "details": {"run_id": run_id}},
                    {"deliverAs": "followUp", "triggerTurn": False},
                )
            finally:
                drivers.pop(run_id, None)

        def launch(run_id, ctx, *, resume=False, clarification=""):
            existing = drivers.get(run_id)
            if existing and not existing.done():
                return False
            drivers[run_id] = asyncio.create_task(drive(run_id, ctx, resume=resume, clarification=clarification))
            return True

        pending = {"depth": None, "parallel": None, "rounds": None, "workspace": None}
        starts = set()

        async def begin(question, depth, parallel, workspace, ctx, rounds=None):
            from misaka.core.platform import cards as card_files
            await asyncio.to_thread(card_files.init_project, workspace, draft_brief=False)
            run = runs.create(
                con, workspace=workspace, question=question,
                limits={"max_depth": depth, "parallel": parallel,
                        **({"max_followups": rounds} if rounds is not None else {})},
                token_start=budget.spent(con),
                origin_session=getattr(getattr(ctx, "sessionManager", None), "sessionId", None),
            )
            self.session.moments.send_message(
                {"customType": "research-discipline", "display": True,
                 "content": workflow.RESEARCH_DISCIPLINE,
                 "details": {"run_id": run["id"]}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )
            launch(run["id"], ctx)
            ctx.ui.notify(
                f"Research run started: {run['id']} | project {runs.project_name(run)!r} | "
                f"maximum depth {depth} | LO parallelism {parallel} | follow-ups per node "
                f"{runs.limits(run)['max_followups']}. Use /research status to check progress.",
                "info",
            )

        async def begin_safely(question, depth, parallel, workspace, ctx, rounds=None):
            try:
                await begin(question, depth, parallel, workspace, ctx, rounds)
            except Exception as error:  # noqa: BLE001 - the failure is shown and the question is kept for a retry
                pending["depth"] = depth
                pending["parallel"] = parallel
                pending["rounds"] = rounds
                pending["workspace"] = workspace
                send_progress(
                    f"Research startup failed: {type(error).__name__}: {error}\n"
                    "Research mode is still waiting for a question: send it again, or enter /research to leave research mode.",
                    details={"stage": "startup_error", "depth": depth, "parallel": parallel},
                )

        def start_question(question, depth, parallel, workspace, ctx, rounds=None):
            """Persist the question, then let this window plan it as the run's root LO."""
            self.session.moments.send_message(
                {"customType": "research-question", "display": True,
                 "content": f"Research question | {question}",
                 "details": {"question": question, "depth": depth, "parallel": parallel, "rounds": rounds,
                             "workspace": workspace}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )
            task = asyncio.create_task(begin_safely(question, depth, parallel, workspace, ctx, rounds))
            starts.add(task)
            task.add_done_callback(starts.discard)

        async def capture_question(event, ctx):
            if pending["depth"] is None or event.get("source") == "extension":
                return {"action": "continue"}
            question = str(event.get("text") or "").strip()
            if not question:
                return {"action": "handled"}
            depth, parallel, rounds, workspace = pending["depth"], pending["parallel"], pending["rounds"], pending["workspace"]
            pending.update(depth=None, parallel=None, rounds=None, workspace=None)
            start_question(question, depth, parallel, workspace, ctx, rounds)
            return {"action": "handled"}

        self._capture_question = capture_question

        async def command(raw, ctx):
            try:
                spec = parse_command(raw)
                if spec["action"] == "help":
                    ctx.ui.notify(USAGE, "info")
                    return
                if spec["action"] == "status":
                    if starts and not spec["target"]:
                        ctx.ui.notify("Research startup in progress. This window will plan the root node.", "info")
                    elif pending["depth"] is not None and not spec["target"]:
                        ctx.ui.notify(f"Research mode is waiting for a question | maximum depth {pending['depth']} | "
                                      f"LO parallelism {pending['parallel']}.", "info")
                    else:
                        ctx.ui.notify(_status(con, spec["target"], _workspace(ctx)), "info")
                    return
                if spec["action"] == "stop":
                    run = _find_run(con, spec["run_id"], _workspace(ctx), active=True)
                    if not run:
                        if pending["depth"] is not None:
                            pending.update(depth=None, parallel=None, rounds=None, workspace=None)
                            ctx.ui.notify("Research mode closed.", "info")
                            return
                        ctx.ui.notify("No active research run.", "info")
                        return
                    runs.request_stop(con, run["id"])
                    # A waiting-input run has no driver left to observe this request.
                    if not run["driver_lock"]:
                        launch(run["id"], ctx)
                    ctx.ui.notify(
                        f"Stop request recorded for {run['id']}. Running tasks will stop or drain.",
                        "info",
                    )
                    return
                if spec["action"] == "resume":
                    run = _find_run(con, spec["run_id"], _workspace(ctx))
                    if not run:
                        raise ValueError("No research run to resume.")
                    if run["status"] == "done":
                        raise ValueError(f"Research run {run['id']} is done; start a new run.")
                    if not launch(run["id"], ctx, resume=True, clarification=spec["clarification"]):
                        ctx.ui.notify(f"Research run {run['id']} is already running in this session.", "info")
                    else:
                        ctx.ui.notify(f"Resumed research run {run['id']}.", "info")
                    return

                if not ctx.isIdle():
                    ctx.ui.notify("The current turn is still running. Enter /research after it finishes.", "error")
                    return
                if starts or drivers:
                    ctx.ui.notify("A research run is already active in this window; stop it before starting another.", "info")
                    return
                if pending["depth"] is not None and not spec["explicit"]:
                    pending.update(depth=None, parallel=None, rounds=None, workspace=None)
                    ctx.ui.notify("Research mode closed.", "info")
                    return
                if spec.get("question"):
                    # `/research [DEPTH] QUESTION`: no picker, no waiting for the next message.
                    pending.update(depth=None, parallel=None, rounds=None, workspace=None)
                    start_question(spec["question"], spec["depth"], spec["parallel"], _workspace(ctx), ctx, spec["rounds"])
                    return
                if not spec["explicit"]:
                    result = await ctx.ui.custom(
                        lambda tui, _theme, keybindings, done: AskUserQuestionComponent(
                            [{
                                "header": "Research depth",
                                "question": _DEPTH_QUESTION,
                                "multiSelect": False,
                                "options": [
                                    {"label": "2", "description": "Quick pass: follow-up branches go at most 2 levels deep."},
                                    {"label": "5", "description": "Standard deep research: follow-up branches go at most 5 levels deep."},
                                    {"label": "10", "description": "Exhaustive: follow-up branches go at most 10 levels deep. Slow and costly."},
                                ],
                            }, {
                                "header": "LO parallelism",
                                "question": _PARALLEL_QUESTION,
                                "multiSelect": False,
                                "options": [
                                    {"label": "4", "description": "Default: up to 4 LO nodes at once; Sister slots per LO stay unchanged."},
                                    {"label": "1", "description": "One LO node at a time; lowest concurrent resource use."},
                                    {"label": "8", "description": "Up to 8 LO nodes at once; more simultaneous sessions and requests."},
                                ],
                            }, {
                                "header": "Follow-ups",
                                "question": _ROUNDS_QUESTION,
                                "multiSelect": False,
                                "options": [
                                    {"label": "2", "description": "Default: up to two more rounds of cards after the first, per node."},
                                    {"label": "0", "description": "None: every node concludes from its first cards."},
                                    {"label": "4", "description": "Up to four more rounds per node; thorough, slow, and more plans to approve."},
                                ],
                            }],
                            done,
                            tui=tui,
                            keybindings=keybindings,
                        )
                    )
                    if not isinstance(result, dict) or result.get("action") == "cancel":
                        return
                    if result.get("action") == "clarify":
                        self.session.moments.send_user_message(
                            "I chose \"Chat about this\" in the research-options picker. Ask me what I want "
                            "to clarify and talk through depth, LO parallelism and rounds; do not start research yet."
                        )
                        return
                    answers = result.get("answers") or {}
                    picked = {"max_depth": answers.get(_DEPTH_QUESTION), "parallel": answers.get(_PARALLEL_QUESTION),
                              "max_followups": answers.get(_ROUNDS_QUESTION)}
                    limits = runs.normalize_limits({k: v for k, v in picked.items() if v is not None})
                    spec.update(depth=limits["max_depth"], parallel=limits["parallel"], rounds=limits["max_followups"])
                pending["depth"] = spec["depth"]
                pending["parallel"] = spec["parallel"]
                pending["rounds"] = spec["rounds"]
                pending["workspace"] = _workspace(ctx)
                ctx.ui.notify(
                    f"Research mode enabled | maximum depth {spec['depth']} | LO parallelism {spec['parallel']} | "
                    f"follow-ups per node {spec['rounds']} | "
                    f"workspace {pending['workspace']}. Your next regular message becomes the "
                    "research question; this Last Order writes the brief and root plan together.",
                    "info",
                )
            except (ValueError, RuntimeError) as error:
                ctx.ui.notify(str(error), "error")

        self.commands.append(CoreCommand(
            "research", "Start a persistent Research Workflow run, or check, stop, or resume one.", command))

        async def cleanup(_event, _ctx):
            startup_tasks = list(starts)
            for task in startup_tasks:
                task.cancel()
            live = list(drivers.items())
            for run_id, _task in live:       # the drivers own the run's state: they see the stop, stop their nodes and settle
                runs.request_stop(con, run_id)
            for _run_id, task in live:
                if not task.done():
                    task.cancel()
            if live:
                await asyncio.gather(*(task for _run_id, task in live), return_exceptions=True)
            if startup_tasks:
                await asyncio.gather(*startup_tasks, return_exceptions=True)
            for run_id, _task in live:
                current = runs.get(con, run_id)
                if current and current["status"] == "stopping" and not current["driver_lock"]:
                    await workflow.run(con, dict(_cfg()), spawner, worker, run_id=run_id)

        self._cleanup = cleanup

    def attach(self, session):
        self.session = session

    async def input(self, event, ctx):
        return await self._capture_question(event, ctx)

    async def context(self, event, _ctx):
        """What the model sees of the run, without editing the transcript or tool pairs: the
        status ticks of every node and card (still displayed) and the completion notices of
        research cards the phases already consumed are left out -- a run of a dozen nodes puts
        hundreds of them in the root window, and they were the bulk of what compaction ate --
        and a delivered final report archives its superseded drafts."""
        messages = event["messages"]
        delivered = {read_field(message, "details", {}).get("run_id") for message in messages
                     if read_field(message, "customType") == "research-final"
                     and (read_field(message, "details") or {}).get("final_artifact")}
        out, drafting, changed = [], False, False
        for message in messages:
            role, kind = read_field(message, "role"), read_field(message, "customType")
            if role == "custom" and _feed_noise(kind, read_field(message, "details") or {}):
                changed = True
                continue
            if kind == "research-phase" and delivered:
                details = read_field(message, "details") or {}
                drafting = details.get("stage") == "adjudication_draft" and details.get("run_id") in delivered
                if drafting:
                    changed = True
                    out.append({"role": "custom", "customType": kind, "display": False, "details": details,
                                "timestamp": read_field(message, "timestamp"),
                                "content": f"Archived adjudication drafting phase for {details['run_id']}. "
                                           "Use its delivered final report, not its superseded working drafts."})
                    continue
            elif role == "user" or (role == "custom" and kind not in {"research-progress", "sister-notification"}):
                drafting = False
            elif (role == "assistant" and drafting and read_field(message, "stopReason") == "stop"
                  and not any(read_field(part, "type") == "toolCall" for part in read_field(message, "content", []))):
                # The first completed answer belongs to this phase. A queued follow-up
                # after it is ordinary conversation and must remain in context.
                drafting = False
                changed = True
                continue
            out.append(message)
        return {"messages": out} if changed else None

    async def session_shutdown(self, event, ctx):
        await self._cleanup(event, ctx)


# Progress stages that are ticks -- a card changed status, a node changed phase, a card was
# created -- as opposed to the events Last Order acts on or the user is asked about.
TICK_STAGES = {"tasks", "node", "assigned", "red_team", "planning", "level"}


def _feed_noise(kind, details):
    """A custom message of the run's feed that the model has no use for: a status tick, or the
    completion notice of a research card whose result the phase turn already read."""
    if kind == "research-progress":
        return details.get("stage") in TICK_STAGES
    if kind == "sister-notification":
        return bool(details.get("research")) and details.get("boardStatus", details.get("status")) == "done"
    return False


SESSION_KINDS = {"foreground", "dm"}
ROLES = {"last_order"}


def part(spec):
    from misaka.core.network import worker
    from misaka.core.research.wiring.node import node_identity
    if node_identity():        # a fork node's own window: its routine is NodePart's, and no run starts from there
        return None
    return ResearchPart(worker)
