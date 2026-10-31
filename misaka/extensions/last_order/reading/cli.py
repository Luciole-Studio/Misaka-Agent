"""``misaka read``: the reading's operations from a terminal, and ``chat``, a conversation with the
reading's leader -- Last Order, in the conversation that leads the reading, run headless here so
a pipe can feed it. The parser is shared with ``/read``."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import threading

from . import engine, store
from .store import ReadingError

ENV_RUN = "MISAKA_READING_RUN"     # a headless leader session binds to this reading when it starts
ENV_MODEL = "MISAKA_READING_MODEL"  # the study readers' model without --model (PR #12's name for it)
OPERATIONS = ("act", "fork", "compare", "reread")
STOP_WORDS = ("/stop", "/quit", "/exit")


class Help(Exception):
    """``--help`` was asked for; the text is the help."""


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so ``/read`` can use it inside the window."""

    def error(self, message):
        raise ReadingError(message)

    def exit(self, status=0, message=None):
        if status:
            raise ReadingError((message or "").strip())
        raise Help(self.format_help())

    def print_help(self, file=None):
        raise Help(self.format_help())


def parser(prog="misaka read"):
    top = _Parser(prog=prog, description="Co-read materials with a question: open a reading, act on its nodes, "
                                         "fork, compare and reread, see its graph, or talk with its leader.")
    sub = top.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def command(name, help_text, model=True):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--workspace", help="the project folder (default: the current folder)")
        p.add_argument("--runs-dir", help="where reading folders go (default: <workspace>/reading)")
        p.add_argument("--json", action="store_true", help="print only the result, as JSON, on stdout")
        if model:
            p.add_argument("--model", help="the model the study readers use, as provider/id")
        return p

    p = command("open", "Add materials and open a reading with its entry node N0 (no model call).", model=False)
    p.add_argument("--model", help="accepted, so one command line serves every subcommand; open calls no model")
    p.add_argument("sources", nargs="+", metavar="SOURCE", help="a file or an http(s) URL")
    p.add_argument("--question", required=True)
    p.add_argument("--intent", default="", help="what you care about in this reading")
    p = command("act", "Do one reading action on a node.")
    p.add_argument("run", metavar="RUN_ID")
    p.add_argument("action", choices=store.ACTIONS)
    p.add_argument("--node", required=True)
    p.add_argument("--focus")
    p.add_argument("--lens")
    p.add_argument("--context", choices=store.MODES, help="default: continue (compare / reread for those actions)")
    p.add_argument("--question")
    p = command("fork", "Branch a node into independent lines, one per lens, read in parallel.")
    p.add_argument("run", metavar="RUN_ID")
    p.add_argument("--node", required=True)
    p.add_argument("--lens", action="append", required=True, help="repeat for each line")
    p.add_argument("--action", choices=store.ACTIONS, default="reconstruct")
    p.add_argument("--question")
    p = command("compare", "Compare finished nodes in a new node.")
    p.add_argument("run", metavar="RUN_ID")
    p.add_argument("nodes", nargs="+", metavar="NODE")
    p.add_argument("--question", required=True)
    p = command("reread", "Reread an earlier node's material with newer nodes' understanding.")
    p.add_argument("run", metavar="RUN_ID")
    p.add_argument("--node", required=True)
    p.add_argument("--using", action="append", required=True, help="repeat for each newer node")
    p.add_argument("--focus")
    p = command("graph", "Show a reading's graph (no model call).", model=False)
    p.add_argument("--model", help="accepted, so one command line serves every subcommand; graph calls no model")
    p.add_argument("run", metavar="RUN_ID")
    p = command("chat", "Talk with the reading's leader, Last Order, in the conversation that leads it.", model=False)
    p.add_argument("run", metavar="RUN_ID")
    p.add_argument("--model", help="the model Last Order uses in this conversation")
    return top


def parse(argv, prog="misaka read"):
    args = parser(prog).parse_args(argv)
    if args.command == "compare" and len(args.nodes) < 2:
        raise ReadingError("compare needs at least two nodes.")
    return args


# -- running a command ------------------------------------------------------------------------


def graph_result(run):
    return {"runId": run.id, "currentNodeId": run.info["current"], "runDirectory": run.directory,
            "graphPath": os.path.join(run.directory, "graph.json"),
            "graphMarkdownPath": os.path.join(run.directory, "graph.md"),
            "readingPath": os.path.join(run.directory, "reading.md"),
            "nodes": [{"id": node.id, "question": node.attrs["question"],
                       **({"lens": node.attrs["lens"]} if node.attrs.get("lens") else {}),
                       "bodyPath": f"nodes/{node.id}.md", "actions": [a["action"] for a in node.attrs["actions"]]}
                      for node in run.readings()]}


def describe(result):
    """A command's result in words, for the terminal and the window."""
    if "nodes" in result and "graphPath" in result:
        lines = [f"Run {result['runId']}, current node {result['currentNodeId']}",
                 f"Graph: {result['graphMarkdownPath']}", f"Map: {result['readingPath']}", "Nodes:"]
        for node in result["nodes"]:
            lens = f" [{node['lens']}]" if node.get("lens") else ""
            lines.append(f"  {node['id']}{lens}  {' → '.join(node['actions']) or 'unread'}  {node['bodyPath']}")
        return lines
    if "nodeIds" in result:
        return [f"Forked {len(result['nodeIds'])} lines: {', '.join(result['nodeIds'])}"]
    if "nodeId" in result:
        return [f"Node {result['nodeId']} written to {result['bodyPath']}"]
    return [f"Opened reading {result['runId']}", f"Folder: {result['runDirectory']}",
            f"Entry node: {result['currentNodeId']}"]


async def run_command(args, *, model=None, runtime=None, progress=engine.QUIET):
    """One command other than ``chat``; returns its result object. ``open`` and ``graph`` call no
    model; the others use ``model``/``runtime`` when given, else choose one as :func:`engine.choose_model`."""
    if args.command == "open":
        run = await engine.open_reading(args.sources, args.question, workspace=args.workspace or os.getcwd(),
                                        intent=args.intent, runs_dir=args.runs_dir, progress=progress)
        return {"runId": run.id, "runDirectory": run.directory, "currentNodeId": run.info["current"]}
    run = engine.find(args.run)
    if args.command == "graph":
        return graph_result(run)
    if runtime is None or args.model:
        runtime = runtime or await engine.model_runtime()
        model = engine.choose_model(runtime, args.model)
        progress.note(f"model: {model.provider}/{model.id}")
    reader = engine.Reader(model, runtime, progress)
    if args.command == "act":
        node_id, _ = await reader.act(run, args.node, args.action, question=args.question, lens=args.lens,
                                      focus=args.focus, mode=args.context)
    elif args.command == "fork":
        node_ids, _ = await reader.fork(run, args.node, args.lens, action=args.action,
                                        questions=[args.question] * len(args.lens) if args.question else None)
        return {"runId": run.id, "nodeIds": node_ids}
    elif args.command == "compare":
        node_id, _ = await reader.compare(run, args.nodes, args.question)
    else:
        node_id, _ = await reader.reread(run, args.node, args.using, focus=args.focus)
    return {"runId": run.id, "nodeId": node_id, "bodyPath": f"nodes/{node_id}.md"}


class TerminalProgress(engine.Progress):
    """Readers' text as it is written, under a ``[study N1]`` line whenever the speaker changes;
    notes on stderr. With ``--json`` the text goes to stderr, so stdout holds the result alone."""

    def __init__(self, json_mode=False):
        self.out = sys.stderr if json_mode else sys.stdout
        self.json_mode, self.speaker = json_mode, None

    def text(self, role, node_id, delta):
        if (role, node_id) != self.speaker:
            self.out.write(("\n" if self.speaker else "") + f"[{role}{f' {node_id}' if node_id else ''}] ")
            self.speaker = (role, node_id)
        self.out.write(delta)
        self.out.flush()

    def note(self, line):
        print(f"reading: {line}", file=sys.stderr)

    def changed(self, run_id, node_ids):
        if not self.json_mode:
            self.out.write(f"\n(graph updated: {run_id} {', '.join(node_ids)})\n")
            self.out.flush()
        self.speaker = None


# -- chat --------------------------------------------------------------------------------------


def leader_in_use(path):
    """Whether a live window has the conversation ``path`` open (``session_catalog``'s owner record,
    as ``deletable`` asks it): two processes must not write one conversation."""
    from misaka.core import session_catalog
    from misaka.core.platform import processes

    owner = session_catalog.owner_record(path)
    return owner.get("state") != "saved" and processes.identity_is_alive(owner.get("pid"), owner.get("identity"))


def leads(manager, run_id):
    """Whether the conversation ``manager`` keeps leads ``run_id``: its branch binds it last, and the
    reading names it. Either alone is not enough -- a reading can still name a conversation that has
    since gone back past its binding, or left it while the reading was busy."""
    bound = None
    for entry in manager.getBranch():
        if entry.get("type") == "custom" and entry.get("customType") == store.BIND:
            bound = (entry.get("data") or {}).get("run")
    return bound == run_id and store.same_path(engine.find(run_id).info.get("leader"), manager.getSessionFile())


def relay(progress, event):
    """One session event on the terminal: Last Order's text, and the study readers' text her
    reading tools pass on (other tools' updates carry details of their own)."""
    from misaka.utils.values import read_field

    kind = read_field(event, "type")
    if kind == "message_update":
        inner = read_field(event, "assistantMessageEvent")
        if read_field(inner, "type") == "text_delta" and read_field(inner, "delta"):
            progress.text("leader", None, read_field(inner, "delta"))
    elif kind == "tool_execution_update":
        details = read_field(read_field(event, "partialResult"), "details")
        update = details.get("reading") if isinstance(details, dict) else None
        if update:
            progress.text(update["role"], update["node"], update["delta"])


async def converse(lines, turn, abort, on_error=lambda error: None):
    """One turn per line from ``lines`` (a queue; None ends the input). Lines that arrive while a
    turn runs wait, in order, for the turns after it; the end of input lets them all run, then
    ends; a stop word ends at once, stopping the turn. Returns whether any turn failed."""
    pending, at_end, stopped, failed = collections.deque(), False, False, False
    while not stopped:
        if pending:
            line = pending.popleft()
        elif at_end:
            break
        else:
            line = await lines.get()
        if line is None or line.strip() in STOP_WORDS:
            break
        if not line.strip():
            continue
        running = asyncio.ensure_future(turn(line))
        while not running.done():
            getter = asyncio.ensure_future(lines.get()) if not at_end else None
            await asyncio.wait({running, *([getter] if getter else [])}, return_when=asyncio.FIRST_COMPLETED)
            if getter is None:
                continue
            if not getter.done():
                getter.cancel()
                continue
            typed = getter.result()
            if typed is None:
                at_end = True
            elif typed.strip() in STOP_WORDS:
                stopped = True
                await abort()
            else:
                pending.append(typed)
        try:
            await running
        except ReadingError as error:
            failed = True
            on_error(error)
    return failed


async def chat(args):
    """Last Order as the reading's leader, in the conversation that leads it (a new one when the
    reading has none yet), one turn per line of input. Returns the exit status: 1 when a turn
    failed, so a script that pipes its questions in can tell."""
    from misaka.core.platform.session import _env_window, dispose, open_session
    from misaka.core.wiring import role_session_setup

    run = engine.find(args.run)
    flags, assembly, env = role_session_setup(engine.lo_profile(), run.workspace, model=args.model)
    leader = run.info.get("leader")
    if leader and leader_in_use(leader):       # a live window, whether or not it has written its file yet
        raise ReadingError(f"The conversation that leads {run.id} is open in a window: talk to Last Order "
                           f"there, or attach to it with misaka chat --attach --session {leader}")
    if leader and os.path.exists(leader):
        flags += ["--session", leader]
    env[ENV_RUN] = run.id
    lines: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def read_stdin():
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line.rstrip("\n"))
        loop.call_soon_threadsafe(lines.put_nowait, None)

    async with _env_window():
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        runtime = None
        try:
            runtime, session, error = await open_session(flags, run.workspace, assembly)
            if error:
                raise ReadingError(error)
            # Input may be a pipe nobody watches: the guards every headless Last Order runs under
            # (research's nodes, run_session) stop a turn that repeats itself or spins.
            from misaka.agent.guards import install_guards
            from misaka.core.platform import metering
            from misaka.core.platform.vocabulary import NO_PROGRESS_VOCABULARY

            install_guards(session, metering.allowance, no_progress_vocabulary=NO_PROGRESS_VOCABULARY)
            if not leads(session.sessionManager, run.id):
                raise ReadingError(f"Could not take reading {run.id} up: it is busy with another operation; try "
                                   "again when it has ended.")
            progress = TerminalProgress(args.json)
            session.subscribe(lambda event: relay(progress, event))
            print(f"Talking with the leader of {run.id}. /stop or /quit leaves.", file=sys.stderr)
            threading.Thread(target=read_stdin, daemon=True).start()

            async def turn(line):
                finished, error = False, None
                try:
                    await _turn(session, line)
                    finished = True
                except BaseException as failure:  # whatever ends a turn, its line says it did not finish
                    error = str(failure) or type(failure).__name__
                    raise
                finally:
                    progress.speaker = None
                    print(file=progress.out, flush=True)
                    if args.json:             # with --json, stdout has one result per turn
                        print(json.dumps({"runId": run.id, "reply": _reply(session) if finished else "",
                                          "ok": finished, **({"error": error} if error else {})},
                                         ensure_ascii=False), flush=True)

            failed = await converse(lines, turn, session.abort,
                                    lambda error: print(f"Reading error: {error}", file=sys.stderr))
            return 1 if failed else 0
        finally:
            if runtime is not None:
                await dispose(runtime)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


async def _turn(session, line):
    """One turn, settled the way every headless MISAKA session's prompt is (``settle_after_prompt``),
    so what it set going ends before the next line or the end."""
    from misaka.core.platform.session import settle_after_prompt
    from misaka.utils.values import read_field

    await session.prompt(line)
    await settle_after_prompt(session)
    last = next((m for m in reversed(list(session.messages)) if read_field(m, "role") == "assistant"), None)
    if last is not None and read_field(last, "stopReason") == "error":
        raise ReadingError(read_field(last, "errorMessage") or "the provider reported an error")


def _reply(session):
    """What Last Order last said: the text of her latest answer."""
    from misaka.utils.values import read_field

    last = next((m for m in reversed(list(session.messages)) if read_field(m, "role") == "assistant"), None)
    content = read_field(last, "content") if last is not None else None
    if isinstance(content, str):
        return content
    return "".join(read_field(part, "text") or "" for part in content or [] if read_field(part, "type") == "text")


def main(argv):
    try:
        args = parse(argv)
    except Help as shown:
        print(shown.args[0])
        return 0
    except ReadingError as error:
        print(f"misaka read: {error}", file=sys.stderr)
        return 1
    if args.command in OPERATIONS and not args.model:
        # The study readers' model only: Last Order's in `chat` is her conversation's, kept in it,
        # and a variable left in the environment must not change it.
        args.model = os.environ.get(ENV_MODEL, "").strip() or None
    try:
        if args.command == "chat":
            return asyncio.run(chat(args))
        result = asyncio.run(run_command(args, progress=TerminalProgress(args.json)))
    except KeyboardInterrupt:
        print("\nStopped. The reading's graph and notes are kept; you can go on from them.", file=sys.stderr)
        return 130
    except ReadingError as error:
        print(f"\nReading failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - a command line says what went wrong, not a traceback
        print(f"\nReading failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("\n" + "\n".join(describe(result)))
    return 0
