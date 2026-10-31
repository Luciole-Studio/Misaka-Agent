"""Reading mode in Last Order's window: ``/read``, and the window as the leader of a reading.

The conversation that opens a reading (``/read open``) or takes it up (``/read chat RUN``) leads it,
as the conversation that starts a research run leads that run. While it does, the reading tools are
active in it, and Last Order is given the reading-mode notice, written from the graph, once it leads
the reading; whenever the map has changed since, the new map follows the message that starts her
next run, and the earlier ones stay where they were, so her history is never rewritten. The binding
is an entry in the conversation, so it survives a restart and follows ``/tree``; ``/read close`` ends
it. The reading names the one conversation that leads it, so a window another one took the reading
from stops leading it. A window leads only a reading of its own project, whose documents its tools
can open.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex

from misaka.utils.values import read_field

from . import cli, engine, materials, prompts, store, views
from .store import ReadingError, same_path

BIND = store.BIND
MODE = "reading-mode"
WIDGET = "reading"
TOOLS = ("reading_act", "reading_fork", "reading_compare", "reading_reread")
LEADER_ACTIONS = ("overview", "reconstruct", "presuppose", "transform", "translate", "generate")
TAIL_CHARS, TAIL_LINES = 2400, 6
# The window shows ten lines of a widget: the usage fits in them; a subcommand's --help is a notice.
USAGE = ("/read open SOURCE... --question Q [--intent T]   open a reading; this window leads it",
         "/read act RUN_ID ACTION --node N [--lens L] [--focus F] [--context MODE] [--question Q]",
         "/read fork RUN_ID --node N --lens A --lens B [--action ACTION] [--question Q]",
         "/read compare RUN_ID N1 N2 ... --question Q",
         "/read reread RUN_ID --node OLD --using NEW [--focus F]",
         "/read graph RUN_ID      /read chat RUN_ID [--here]   lead an existing reading from this window",
         "/read close             leave reading mode         /read stop   stop the running operation",
         "Actions: overview reconstruct presuppose transform compare translate reread generate",
         "/read SUBCOMMAND --help for details; the guide is docs/guide/reading.md")


def split(text):
    """Words as a shell would split them -- quotes group and are dropped, inside a word too
    (``--question="a b"``) -- except that a backslash is a character, not an escape, so a Windows
    path survives; unbalanced quotes fall back to plain whitespace."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split, lexer.escape = True, ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _ui(ctx, method, *args):
    """A call on the window, which may already be gone (a session switch, a reload, shutting down):
    a reading that ends after its window says nothing rather than failing."""
    try:
        getattr(ctx.ui, method)(*args)
    except Exception:  # noqa: BLE001, S110 - a stale window is not the reading's failure
        pass


def _notice(content, **details):
    """A reading-mode notice, kept in the conversation: Last Order reads it, the window does not show it."""
    return {"message": {"customType": MODE, "content": content, "display": False, "details": details}}


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Tail(engine.Progress):
    """Readers' text as a rolling tail under a ``[study N1]`` line, handed to ``show``."""

    def __init__(self, show, note=None):
        self.show, self.on_note, self.buffer, self.speaker = show, note, "", None

    def text(self, role, node_id, delta):
        if (role, node_id) != self.speaker:
            self.buffer += ("\n" if self.buffer else "") + f"[{role} {node_id}] "
            self.speaker = (role, node_id)
        self.buffer = (self.buffer + delta)[-TAIL_CHARS:]
        self.show(self.buffer.splitlines()[-TAIL_LINES:], role, node_id, delta)

    def note(self, line):
        if self.on_note:
            self.on_note(line)


def register(harn):
    # ``here``: this conversation's file; ``tasks``: the reading operations running for this window,
    # each marked as started by /read or by Last Order's tools, so /read stop reaches both;
    # ``closing``: the window is going, so they end quietly.
    state = {"run": None, "open": None, "here": None, "tasks": {}, "closing": False}

    def leading():
        """The reading this conversation leads -- bound to it here and naming it as its leader --
        kept open between requests (it reads the graph's latest version every time), or None.
        Raises when the board cannot tell."""
        if not state["run"]:
            return None
        if state["open"] is None or state["open"].id != state["run"]:
            state["open"] = engine.find(state["run"])
        return state["open"] if same_path(state["open"].info.get("leader"), state["here"]) else None

    def bound():
        try:
            return leading()
        except ReadingError:
            return None

    def activate_tools(on):
        active = [name for name in harn.getActiveTools() if name not in TOOLS]
        harn.setActiveTools([*active, *TOOLS] if on else active)

    def bind(run_id, ctx):
        """This conversation now leads ``run_id`` (None: no reading). A reading it led before stops
        naming it as its leader, so that reading is taken up elsewhere without being pulled back."""
        here = ctx.sessionManager.getSessionFile()
        if run_id and not here:
            raise ReadingError("This window keeps no conversation (it was started without a session), so it "
                               "cannot lead a reading.")
        previous = bound()
        # The new reading is taken first: one busy elsewhere leaves this window as it was.
        if run_id:
            engine.write(engine.find(run_id), "lead", {"session": here},
                         lambda change: change.update("run", {"leader": here}))
        if previous is not None and previous.id != run_id:
            try:
                engine.write(previous, "lead", {"session": None},
                             lambda change: change.update("run", {"leader": None}))
            except ReadingError:
                pass      # busy elsewhere: it names this conversation still, which no longer leads it (bound)
        state.update(run=run_id, here=here)
        harn.appendEntry(BIND, {"run": run_id})
        activate_tools(bound() is not None)

    def track(task, by):
        state["tasks"][task] = by
        task.add_done_callback(lambda done: state["tasks"].pop(done, None))
        return task

    def restore(ctx):
        """The binding as this conversation's current branch records it."""
        state.update(run=None, here=ctx.sessionManager.getSessionFile())
        for entry in ctx.sessionManager.getBranch():
            if entry.get("type") == "custom" and entry.get("customType") == BIND:
                state["run"] = (entry.get("data") or {}).get("run")

    async def session_start(event, ctx):
        state["closing"] = False
        restore(ctx)
        # ``misaka read chat`` names its reading once, for this session alone: taken, not inherited
        # by whatever this session runs.
        wanted = os.environ.pop(cli.ENV_RUN, None)
        if wanted and (bound() is None or bound().id != wanted):
            try:
                bind(wanted, ctx)
            except ReadingError:
                pass                          # misaka read chat sees it did not take the reading up, and says so
        activate_tools(bound() is not None)

    async def session_tree(event, ctx):
        restore(ctx)
        activate_tools(bound() is not None)

    def told(ctx):
        """The reading-mode notices in Last Order's context now, oldest first, as the session projects
        it for the model -- after a compaction only those it kept, as entries or inside a context
        engine's replacement (LCM's): their details."""
        from misaka.core.session_manager import buildSessionContext

        manager = ctx.sessionManager
        return [read_field(message, "details") or {}
                for message in buildSessionContext(manager.getEntries(), manager.getLeafId()).messages
                if read_field(message, "role") == "custom" and read_field(message, "customType") == MODE]

    async def before_agent_start(event, ctx):
        """Tell Last Order what her context does not yet say: the whole notice when it holds none for
        the reading she leads, the map when the map changed since, that reading mode ended when it
        has. Each is a message of its own after the one that starts the run, as PR #12 hands its
        leader the map, and the earlier ones stay where they are: a notice moved or rewritten in the
        history voids every thinking block written after it (preserved thinking) and the cached prefix
        with them. A window another conversation took the reading from loses the reading tools here,
        where the run's tools are still to be chosen, so the run that is told is the run without them."""
        try:
            run, said = leading(), told(ctx)
        except Exception:  # noqa: BLE001 - the board out of reach costs this run its notice, not the run
            return None
        last = said[-1] if said else {}
        if run is None:
            if state["run"] and set(TOOLS) & set(harn.getActiveTools()):
                activate_tools(False)         # bound here, led elsewhere: another conversation took it up
            if last.get("run") and not last.get("ended"):
                return _notice(prompts.leader_left(last["run"]), run=last["run"], ended=True)
            return None
        held = False
        for notice in reversed(said):
            if notice.get("run") != run.id or notice.get("ended"):
                break
            if notice.get("whole"):
                held = True
                break
        try:
            current = _digest(prompts.leader_map(run))
            if not held:
                content = prompts.leader_mode(run)
                if last.get("run") not in (None, run.id) and not last.get("ended"):
                    content = f"{prompts.leader_left(last['run'])}\n\n{content}"     # the one it led until now
                return _notice(content, run=run.id, map=current, whole=True)
            if last.get("map") != current:
                return _notice(prompts.leader_map_update(run), run=run.id, map=current)
        except Exception:  # noqa: BLE001 - as above
            return None
        return None

    async def session_shutdown(event, ctx):
        state["closing"] = True
        for task in list(state["tasks"]):
            task.cancel()

    harn.on("session_start", session_start)
    harn.on("session_tree", session_tree)
    harn.on("before_agent_start", before_agent_start)
    harn.on("session_shutdown", session_shutdown)
    _register_tools(harn, bound, track)
    harn.registerCommand("read", {"description": "Co-read materials with a question (/read --help)",
                                  "handler": lambda args, ctx: _command(args, ctx, state, bind, track)})


# -- the leader's tools ------------------------------------------------------------------------


def _register_tools(harn, bound, track):
    from typing import Literal

    from pydantic import BaseModel, Field

    from misaka.ai.utils.typebox_helpers import typeboxSchema
    from misaka.core.extensions.types import ToolDefinition

    Action = Literal[LEADER_ACTIONS]

    class ActParams(BaseModel):
        action: Action = Field(description="The reading action.")
        node: str = Field("", description="The node to act on; the current node when omitted.")
        question: str = Field("", description="The question for this action; the node's own when omitted.")
        lens: str = Field("", description="A lens, such as 'conceptual history' or 'economic conditions'. With a "
                                          "lens the action starts a new, independent line.")
        focus: str = Field("", description="A focus for this action, such as 'the last pages' or one term.")

    class ForkParams(BaseModel):
        lenses: list[str] = Field(min_length=1, description="One lens per line.")
        node: str = Field("", description="The node to branch from; the current node when omitted.")
        action: Action = Field("reconstruct", description="The action each line does.")
        questions: list[str] = Field(default_factory=list, description="A question per lens, in the same order; "
                                                                       "the node's own question when omitted.")

    class CompareParams(BaseModel):
        nodes: list[str] = Field(min_length=2, description="The finished nodes to compare.")
        question: str = Field("", description="What the comparison should answer; the reading's question when "
                                              "omitted.")

    class RereadParams(BaseModel):
        node: str = Field(description="The earlier node whose material is reread.")
        using: list[str] = Field(min_length=1, description="The newer nodes whose understanding is brought back.")
        focus: str = Field("", description="A focus for the rereading.")

    def tool(name, label, params, operation):
        async def execute(tool_call_id, raw, signal, on_update, ctx):
            from misaka.utils.js import addAbortListener
            from misaka.utils.values import signal_aborted

            values = raw if isinstance(raw, params) else params(**(raw or {}))
            run = bound()
            if run is None:
                raise ReadingError("No reading is led from this window: /read open, or /read chat RUN_ID.")
            if signal_aborted(signal):
                return STOPPED

            def show(lines, role, node_id, delta):
                if on_update:
                    on_update({"content": [{"type": "text", "text": "\n".join(lines)}],
                               "details": {"reading": {"role": role, "node": node_id, "delta": delta}}})

            notes = []
            reader = engine.Reader(ctx.model, ctx.modelRegistry.modelRuntime, Tail(show, note=notes.append))
            task = track(asyncio.ensure_future(operation(reader, run, values)), "tool")
            remove = addAbortListener(signal, task.cancel)
            try:
                node_ids = await task
            except asyncio.CancelledError:
                # Escape or /read stop cancelled the operation alone: say so. A cancellation of this
                # call itself (its turn torn down, a timeout around it) goes on up, as any tool's must.
                current = asyncio.current_task()
                if task.cancelled() and not (current and current.cancelling()):
                    return STOPPED
                raise
            finally:
                remove()
            text = _written(run, node_ids) + "".join(f"\nNote: {line}" for line in notes)
            return {"content": [{"type": "text", "text": text}], "details": {}}

        # One reading operation at a time: two in one message would race for the reading's lease.
        harn.registerTool(ToolDefinition(name=name, label=label, description=prompts.TOOLS[name],
                                         parameters=typeboxSchema(params), execute=execute,
                                         executionMode="sequential"))

    async def act(reader, run, v):
        node = v.node or run.info["current"]
        lens = v.lens.strip() or None
        node_id, _ = await reader.act(run, node, v.action, question=v.question, lens=lens, focus=v.focus or None,
                                      mode="independent" if lens else "continue")
        return [node_id]

    async def fork(reader, run, v):
        node_ids, _ = await reader.fork(run, v.node or run.info["current"], v.lenses, action=v.action,
                                        questions=v.questions or None)
        return node_ids

    async def compare(reader, run, v):
        node_id, _ = await reader.compare(run, v.nodes, v.question.strip())
        return [node_id]

    async def reread(reader, run, v):
        node_id, _ = await reader.reread(run, v.node, v.using, focus=v.focus or None)
        return [node_id]

    tool("reading_act", "Reading action", ActParams, act)
    tool("reading_fork", "Fork the reading", ForkParams, fork)
    tool("reading_compare", "Compare lines", CompareParams, compare)
    tool("reading_reread", "Reread", RereadParams, reread)


STOPPED = {"content": [{"type": "text", "text": "Stopped by the reader; nothing was written. Wait for the "
                                               "reader before reading on."}], "details": {}}


def _written(run, node_ids):
    """What the leader is told of the nodes an operation wrote: for each, where its note is, its
    summary and further questions, and the reading itself (the section the operation just added,
    whole), as PR #12 hands its leader the whole understanding, so she connects lines from what
    they found rather than from a sentence."""
    parts = []
    for node_id in node_ids:
        node = run.reading(node_id)
        lens = f" · {node.attrs['lens']}" if node.attrs.get("lens") else ""
        lines = [f"- {node_id} {node.attrs['question']}{lens}", f"  Note: {run.note_path(node_id)}",
                 f"  Summary: {node.attrs['summary'] or '(none)'}"]
        if node.attrs["questions"]:
            lines.append(f"  Further questions: {'; '.join(node.attrs['questions'])}")
        sections = run.sections(node_id)
        if sections:
            lines.append(views.section_text(sections[-1]).rstrip())
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# -- /read ---------------------------------------------------------------------------------------


async def _command(text, ctx, state, bind, track):
    if ctx.mode != "tui":
        ctx.ui.notify("/read is available in interactive mode.", "warning")
        return
    words = split(text or "")
    head = words[0] if words else "help"
    if head in ("help", "-h", "--help"):
        ctx.ui.setWidget(WIDGET, list(USAGE), {"placement": "aboveEditor"})
        return ctx.ui.notify("Usage shown above the editor.", "info")
    if head == "stop":
        if not state["tasks"]:
            return ctx.ui.notify("No reading is running in this window.", "info")
        started_by_her = "tool" in state["tasks"].values()
        for task in list(state["tasks"]):
            task.cancel()
        if started_by_her:
            ctx.abort()                       # her turn stops with it, as Escape would stop it
        return
    if head == "close":
        if state["run"] is None:
            return ctx.ui.notify("This window is not leading a reading.", "info")
        closed = state["run"]
        bind(None, ctx)
        return ctx.ui.notify(f"Left reading mode; reading {closed} stays on disk (/read chat {closed} to go on).",
                             "info")
    if head == "chat":
        return _chat(words[1:], ctx, bind)
    try:
        args = cli.parse(words, prog="/read")
    except cli.Help as shown:
        return ctx.ui.notify(shown.args[0].rstrip(), "info")
    except ReadingError as error:
        return ctx.ui.notify(f"/read: {error}", "error")
    if args.command == "graph":                # a look, so it does not wait for a running operation
        try:
            lines = cli.describe(cli.graph_result(engine.find(args.run)))
        except ReadingError as error:
            return ctx.ui.notify(f"/read: {error}", "error")
        return ctx.ui.notify("\n".join(lines), "info")
    if state["tasks"]:
        return ctx.ui.notify("This window is reading; wait for it to finish, or /read stop.", "warning")
    if args.command == "open":
        # The window's project, not the process's folder: a relative path is the window's too.
        args.workspace = args.workspace or ctx.cwd
        args.sources = [source if materials.is_url(source) else os.path.join(ctx.cwd, os.path.expanduser(source))
                        for source in args.sources]
    try:
        model = engine.choose_model(ctx.modelRegistry.modelRuntime, getattr(args, "model", None), ctx.model) \
            if args.command != "open" else None
    except ReadingError as error:
        return ctx.ui.notify(f"/read: {error}", "error")

    def show(lines, *_):
        _ui(ctx, "setWidget", WIDGET, lines, {"placement": "aboveEditor"})

    progress = Tail(show, note=lambda line: _ui(ctx, "setStatus", WIDGET, line[-120:]))
    progress.changed = lambda run_id, ids: _ui(ctx, "setStatus", WIDGET,
                                               f"Reading graph updated: {run_id} {', '.join(ids)}")

    async def work():
        try:
            result = await cli.run_command(args, model=model, runtime=ctx.modelRegistry.modelRuntime,
                                           progress=progress)
            if state["closing"]:
                return
            if args.command != "open":
                _ui(ctx, "notify", "; ".join(cli.describe(result)), "info")
            elif state["run"] != leading_before:
                _ui(ctx, "notify", f"Opened reading {result['runId']} in {result['runDirectory']}. This window "
                                   f"took up another reading meanwhile; /read chat {result['runId']} leads this one.",
                    "info")
            elif same_path(args.workspace, ctx.cwd):
                try:
                    bind(result["runId"], ctx)
                except ReadingError as error:
                    return _ui(ctx, "notify", f"Opened reading {result['runId']} in {result['runDirectory']}, but "
                                              f"this window cannot lead it: {error}", "warning")
                _ui(ctx, "notify", f"Opened reading {result['runId']} in {result['runDirectory']}; entry node "
                                   "N0. This window now leads it.", "info")
            else:
                _ui(ctx, "notify", f"Opened reading {result['runId']} in {result['runDirectory']}. It belongs to "
                                   f"{args.workspace}: lead it from a window there (/read chat {result['runId']}).",
                    "info")
        except asyncio.CancelledError:
            if not state["closing"]:
                _ui(ctx, "notify", "Reading stopped. Its graph and notes are kept; you can go on.", "info")
        except ReadingError as error:
            _ui(ctx, "notify", f"Reading failed: {error}", "error")
        except Exception as error:  # noqa: BLE001 - the window outlives a failed operation and says why
            _ui(ctx, "notify", f"Reading error: {type(error).__name__}: {error}", "error")
        finally:
            _ui(ctx, "setWidget", WIDGET, None)
            _ui(ctx, "setStatus", WIDGET, None)

    leading_before = state["run"]
    ctx.ui.setStatus(WIDGET, "Reading…")
    track(asyncio.ensure_future(work()), "command")


def _chat(words, ctx, bind):
    here = "--here" in words
    words = [word for word in words if word != "--here"]
    if len(words) != 1:
        return ctx.ui.notify("/read chat RUN_ID [--here]", "warning")
    try:
        run = engine.find(words[0])
    except ReadingError as error:
        return ctx.ui.notify(f"/read: {error}", "error")
    if not same_path(run.workspace, ctx.cwd):
        return ctx.ui.notify(f"Reading {run.id} belongs to {run.workspace}, whose documents this window cannot "
                             "open: lead it from a window there.", "warning")
    leader, this = run.info.get("leader"), ctx.sessionManager.getSessionFile()
    if leader and not same_path(leader, this) and not here:
        if cli.leader_in_use(leader):
            return ctx.ui.notify(f"Reading {run.id} is led by a conversation open in another window ({leader}): talk "
                                 f"to Last Order there, or use /read chat {run.id} --here to lead it from this window "
                                 "instead.", "info")
        if os.path.exists(leader):
            return ctx.ui.notify(f"Reading {run.id} is led by the conversation in {leader}: open it with /resume, or "
                                 f"use /read chat {run.id} --here to lead it from this window (Last Order here will "
                                 "not remember that conversation).", "info")
    try:
        bind(run.id, ctx)
    except ReadingError as error:
        return ctx.ui.notify(f"/read: {error}", "error")
    ctx.ui.notify(f"This window now leads reading {run.id}; current node {run.info['current']}.", "info")


__all__ = ["register", "split"]
