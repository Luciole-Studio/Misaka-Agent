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
from misaka.core.research import planner, runs, workflow
from misaka.core.research import tools as research_tools
from misaka.core.wiring import ToolCollector
from misaka.ui.tui.interactive.components.ask_user_question import (
    AskUserQuestionComponent,
)

_CON = None
_DEPTH_QUESTION = "How deep should this research run go?"
USAGE = (
    f"Usage: /research [DEPTH] [QUESTION]      start research on QUESTION at once (DEPTH defaults to {runs.DEFAULT_LIMITS['max_depth']}), or\n"
    "                                         omit QUESTION to enter research mode: pick a depth, then your\n"
    "                                         next regular message becomes the research question.\n"
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
    # Activation: [start] [--depth] [DEPTH] [QUESTION...]. The question is taken from the raw
    # line, not the shlex tokens: an apostrophe in "Stalin's constitution" is not an open quote.
    line = (raw or "").strip()
    words = line.split(None, 1)
    if words and words[0] == "start":
        line = words[1].strip() if len(words) > 1 else ""
        words = line.split(None, 1)
    explicit = bool(words)
    if words and words[0] == "--depth":
        line = words[1].strip() if len(words) > 1 else ""
        words = line.split(None, 1)
    question = ""
    if not words:
        depth = runs.DEFAULT_LIMITS["max_depth"]
    else:
        try:
            depth = int(words[0])
        except ValueError:
            depth = runs.DEFAULT_LIMITS["max_depth"]    # no depth given: the whole line is the question
            question = line
        else:
            question = words[1].strip() if len(words) > 1 else ""
    depth = runs.normalize_limits({"max_depth": depth})["max_depth"]
    return {"action": "activate", "depth": depth, "explicit": explicit, "question": question}


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
    token_text = f" | tokens added by this run {max(0, reading['used'] - int(run['token_start'])):,}"
    if reading["cap"]:
        token_text += f" | global tokens {reading['used']:,}/{reading['cap']:,}"
    return (
        f"{value['id']} | project {runs.project_name(run)!r} ({value['workspace']}) | "
        f"{value['status']}/{value['phase']} | depth {value['wave']}/{value['limits']['max_depth']} | "
        f"tasks {value['tasks']} | nodes {value['nodes']} | open issues {value['open_issues']}"
        + token_text
        + (f" | error {value['last_error']}" if value["last_error"] else "")
    )


class ResearchPart:
    """Last Order's research workflow: the ``/research`` command, the run drivers, and the read-only view tool."""

    def __init__(self, worker=None):
        drivers = {}     # this session's research drivers; another session's are not ours to stop
        con = _con()
        spawner = research_node.spawner()     # nodes are processes: panes beside this Last Order, or plain children
        collector = ToolCollector()
        research_tools.register(collector, _con)
        self.tools = collector.tools
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

        async def drive(run_id, ctx):
            try:
                def progress(event):
                    content = f"Research `{run_id}` | {event['message']}"
                    if event.get("tasks"):
                        content += "\n" + "\n".join(
                            f"- {item['title']} → Sister {item['assignee']}"
                            for item in event["tasks"])
                    send_progress(content, run_id=run_id, details=event)

                result = await workflow.run(
                    con, dict(_cfg()), spawner, worker, run_id=run_id, progress=progress)
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
                    if final.get("survey_path"):
                        content += f"\n\n---\nSurvey by node: `{final['survey_path']}`"
                    self.session.moments.send_message(
                        {"customType": "research-final", "display": True,
                         "content": content,
                         "details": {"run_id": run_id, "result": result.get("run")}},
                        {"deliverAs": "followUp", "triggerTurn": False},
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

        def launch(run_id, ctx):
            existing = drivers.get(run_id)
            if existing and not existing.done():
                return False
            drivers[run_id] = asyncio.create_task(drive(run_id, ctx))
            return True

        pending = {"depth": None, "workspace": None}
        intakes = set()

        async def begin(question, depth, workspace, ctx):
            ctx.ui.notify("Research question received. Last Order is preparing the project brief.", "info")
            send_progress("Research intake | Last Order drafts PROJECT.md unless the folder already has one; planning starts next.",
                          details={"stage": "brief_intake", "depth": depth})
            brief = await asyncio.to_thread(
                planner.ensure_project_brief, dict(_cfg()), worker, question, workspace)
            from misaka.core.platform import cards as card_files
            await asyncio.to_thread(card_files.init_project, workspace)     # the project is a git repository
            send_progress(f"Research intake | project brief ready at {brief}; creating the persistent run.",
                          details={"stage": "brief_ready", "depth": depth})
            run = runs.create(
                con, workspace=workspace, question=question, limits={"max_depth": depth},
                token_start=budget.spent(con),
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
                f"maximum depth {depth}. Use /research status to check progress.",
                "info",
            )

        async def begin_safely(question, depth, workspace, ctx):
            try:
                await begin(question, depth, workspace, ctx)
            except Exception as error:  # noqa: BLE001 - the failure is shown and the question is kept for a retry
                pending["depth"] = depth
                pending["workspace"] = workspace
                send_progress(
                    f"Research intake failed: {type(error).__name__}: {error}\n"
                    "Research mode is still waiting for a question: send it again, or enter /research to leave research mode.",
                    details={"stage": "intake_error", "depth": depth},
                )

        def start_intake(question, depth, workspace, ctx):
            """Echo the question, then draft the brief in the background.

            Whether it arrived on the /research line or as the next message, the question
            never becomes a chat turn: the intake consumes it. Without the echo it vanished
            from the screen the moment it was sent, with only a transient status line to
            show it had been taken. It is shown where the progress notices go.
            """
            self.session.moments.send_message(
                {"customType": "research-question", "display": True,
                 "content": f"Research question | {question}",
                 "details": {"question": question, "depth": depth, "workspace": workspace}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )
            task = asyncio.create_task(begin_safely(question, depth, workspace, ctx))
            intakes.add(task)
            task.add_done_callback(intakes.discard)

        async def capture_question(event, ctx):
            if pending["depth"] is None or event.get("source") == "extension":
                return {"action": "continue"}
            question = str(event.get("text") or "").strip()
            if not question:
                return {"action": "handled"}
            depth, workspace = pending["depth"], pending["workspace"]
            pending.update(depth=None, workspace=None)
            start_intake(question, depth, workspace, ctx)
            return {"action": "handled"}

        self._capture_question = capture_question

        async def command(raw, ctx):
            try:
                spec = parse_command(raw)
                if spec["action"] == "help":
                    ctx.ui.notify(USAGE, "info")
                    return
                if spec["action"] == "status":
                    if intakes and not spec["target"]:
                        ctx.ui.notify("Research intake in progress: Last Order is preparing the project brief.", "info")
                    elif pending["depth"] is not None and not spec["target"]:
                        ctx.ui.notify(f"Research mode is waiting for a question | maximum depth {pending['depth']}.", "info")
                    else:
                        ctx.ui.notify(_status(con, spec["target"], _workspace(ctx)), "info")
                    return
                if spec["action"] == "stop":
                    run = _find_run(con, spec["run_id"], _workspace(ctx), active=True)
                    if not run:
                        if pending["depth"] is not None:
                            pending.update(depth=None, workspace=None)
                            ctx.ui.notify("Research mode closed.", "info")
                            return
                        ctx.ui.notify("No active research run.", "info")
                        return
                    runs.request_stop(con, run["id"])
                    ctx.ui.notify(
                        f"Stop request recorded for {run['id']}. Running tasks will stop or drain.",
                        "info",
                    )
                    return
                if spec["action"] == "resume":
                    run = _find_run(con, spec["run_id"], _workspace(ctx))
                    if not run:
                        raise ValueError("No research run to resume.")
                    if run["status"] == "stopping":
                        ctx.ui.notify(f"Research run {run['id']} is still stopping; resume it once it has stopped.", "info")
                        return
                    if spec["clarification"]:
                        n = len(runs.artifacts(con, run["id"], kind="clarification")) + 1
                        runs.write_text(con, run["id"], "clarification", f"User clarification {n}",
                                        f"clarification-{n}.md", spec["clarification"] + "\n")
                        con.execute("UPDATE research_runs SET question=question||? WHERE id=?",
                                    (f"""

    User clarification: {spec['clarification']}""", run["id"]))
                    runs.resume(con, run["id"])
                    if not launch(run["id"], ctx):
                        ctx.ui.notify(f"Research run {run['id']} is already running in this session.", "info")
                    else:
                        ctx.ui.notify(f"Resumed research run {run['id']}.", "info")
                    return

                if not ctx.isIdle():
                    ctx.ui.notify("The current turn is still running. Enter /research after it finishes.", "error")
                    return
                if intakes:
                    ctx.ui.notify("Last Order is still preparing the project brief for the previous question; wait for it to finish.", "info")
                    return
                if pending["depth"] is not None and not spec["explicit"]:
                    pending.update(depth=None, workspace=None)
                    ctx.ui.notify("Research mode closed.", "info")
                    return
                if spec.get("question"):
                    # `/research [DEPTH] QUESTION`: no picker, no waiting for the next message.
                    pending.update(depth=None, workspace=None)
                    start_intake(spec["question"], spec["depth"], _workspace(ctx), ctx)
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
                            "I chose \"Chat about this\" in the research-depth picker. Ask me what I want "
                            "to clarify and talk through the depth options; do not start research yet."
                        )
                        return
                    picked = (result.get("answers") or {}).get(_DEPTH_QUESTION)
                    try:
                        depth = int(picked)
                    except (TypeError, ValueError) as error:
                        raise ValueError(f"Depth must be an integer.\n{USAGE}") from error
                    spec["depth"] = runs.normalize_limits({"max_depth": depth})["max_depth"]
                pending["depth"] = spec["depth"]
                pending["workspace"] = _workspace(ctx)
                ctx.ui.notify(
                    f"Research mode enabled | maximum depth {spec['depth']} | "
                    f"workspace {pending['workspace']}. Your next regular message becomes the "
                    "research question; Last Order drafts PROJECT.md if the folder has none.",
                    "info",
                )
            except (ValueError, RuntimeError) as error:
                ctx.ui.notify(str(error), "error")

        self.commands.append(CoreCommand(
            "research", "Start a persistent Research Workflow run, or check, stop, or resume one.", command))

        async def cleanup(_event, _ctx):
            intake_tasks = list(intakes)
            for task in intake_tasks:
                task.cancel()
            live = list(drivers.items())
            for run_id, _task in live:       # the drivers own the run's state: they see the stop, stop their nodes and settle
                runs.request_stop(con, run_id)
            for _run_id, task in live:
                if not task.done():
                    task.cancel()
            if live:
                await asyncio.gather(*(task for _run_id, task in live), return_exceptions=True)
            if intake_tasks:
                await asyncio.gather(*intake_tasks, return_exceptions=True)

        self._cleanup = cleanup

    def attach(self, session):
        self.session = session

    async def input(self, event, ctx):
        return await self._capture_question(event, ctx)

    async def session_shutdown(self, event, ctx):
        await self._cleanup(event, ctx)


SESSION_KINDS = {"foreground", "dm"}
ROLES = {"last_order"}


def part(spec):
    from misaka.core.network import worker
    return ResearchPart(worker)
