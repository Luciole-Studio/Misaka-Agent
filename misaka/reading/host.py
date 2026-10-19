"""The reading host: MISAKA's side of the reading engine process.

``misaka read`` and the TUI's ``/read`` both come here. This module owns the things the
Node engine cannot: the workspace (the extension command runs before the project layout
exists), the corpus (every material request is answered from ``misaka.core.documents``),
and the model credentials MISAKA's own registry resolved. The engine itself is
``packages/reading/dist/cli.js``, started as a child process and spoken to over NDJSON.

What crosses to Node is a plain dict for the model -- provider, id, api, baseURL, headers,
key -- never a provider object or a callable, and the key stays in memory for this one
handover. Nothing here writes credentials, and :func:`misaka.reading.models.safe_summary`
is the only rendering that reaches a log line.

Lifecycle: one host per CLI invocation or TUI window; ``hello`` once, then any number of
requests, each streamed as ``text`` events and settled by one ``result``/``error``. The
child is terminated when the window closes. A run's graph lives on disk, so the next
command resumes from ``graph.json`` rather than from process memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from misaka.reading import models
from misaka.reading.materials import Materials

#: The eight reading actions, in the order the engine lists them.
ACTIONS = (
    "overview",
    "reconstruct",
    "presuppose",
    "transform",
    "compare",
    "translate",
    "reread",
    "generate",
)

CONTEXT_MODES = ("continue", "independent", "compare", "reread")

#: Requests that need a text model. ``open`` only ingests material, and ``graph`` only
#: reads the disk, so neither should fail because no model is configured.
MODEL_METHODS = frozenset({"act", "fork", "compare", "reread", "chat"})

_STDERR_TAIL = 40
_READY_TIMEOUT = 60.0
_CLOSE_TIMEOUT = 10.0


class ReadingError(RuntimeError):
    """A request the engine refused, or a process that stopped before answering."""


# -- persona -----------------------------------------------------------------


_READING_LEADER = (
    "你是这次共读的领读。\n"
    "你手里有读者的问题、材料目录和一张正在生长的阅读地图。\n"
    "你的工作不是替读者写完文章，而是决定此刻该读哪一段、用哪个动作、带着什么焦点，\n"
    "然后把各条路线交回来的理解串起来；需要深入某一处时，调用研读。"
)

_READING_STUDY = (
    "你是一位研读者，这次只负责一个阅读动作。\n"
    "你手上有原文工具（source_outline / source_read / source_find），可以按需展开材料；\n"
    "先把原话读准，再写你自己的重构、追问与生成。原文、你的解释、你提出的新问题要区分清楚。"
)


def last_order_profile() -> str:
    """The Last Order profile directory, where the reader's voice and pin live."""
    from misaka.config import home

    return str(home.path("roles_root") / "last_order")


def persona_for(profile_dir: str | None = None) -> dict[str, str]:
    """The leader and study voices, read from the same files the TUI uses.

    Only the personality slot (SOUL.md and the shared soul) is taken from the profile:
    the research coordinator charter tells Last Order to delegate domain work, which is
    the opposite of leading a close reading. The reading role text supplies that part.
    """
    from misaka.config import identity, profiles

    voice: list[str] = []
    try:
        shared = Path(profiles.shared_soul())
        if shared.is_file():
            text = shared.read_text(encoding="utf-8-sig").strip()
            if text:
                voice.append(text)
    except OSError:
        pass
    soul = identity.read_soul(profile_dir) if profile_dir else None
    if soul:
        voice.append(soul)
    head = "\n\n".join(voice)
    return {
        "leader": f"{head}\n\n{_READING_LEADER}".strip() if head else _READING_LEADER,
        "study": f"{head}\n\n{_READING_STUDY}".strip() if head else _READING_STUDY,
    }


# -- process -----------------------------------------------------------------


def default_runtime() -> str:
    """The built engine, from ``MISAKA_READING_RUNTIME`` or this source checkout."""
    override = os.environ.get("MISAKA_READING_RUNTIME", "").strip()
    if override:
        return override
    # misaka/reading/host.py -> repository root
    return str(Path(__file__).resolve().parents[2] / "packages" / "reading" / "dist" / "cli.js")


def _require_runtime(runtime: str | None) -> str:
    path = os.path.abspath(os.path.expanduser(runtime or default_runtime()))
    if not os.path.isfile(path):
        raise ReadingError(
            f"The reading engine is not built at {path}.\n"
            "From the source checkout run:\n"
            "  npm --prefix packages/reading install\n"
            "  npm --prefix packages/reading run build\n"
            "or point MISAKA_READING_RUNTIME at an existing dist/cli.js."
        )
    return path


def _node_command() -> str:
    node = os.environ.get("MISAKA_READING_NODE", "").strip() or shutil.which("node") or ""
    if not node:
        raise ReadingError(
            "Node.js (>= 22.19.0) is required for reading mode but `node` was not found on PATH."
        )
    return node


class ReadingClient:
    """One engine process, and the request/response bookkeeping around it."""

    def __init__(
        self,
        workspace: str,
        *,
        runs_directory: str | None = None,
        runtime: str | None = None,
        model: dict[str, Any] | None = None,
        reader_intent: str | None = None,
        persona: dict[str, str] | None = None,
    ) -> None:
        self.workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace)))
        self.runs_directory = (
            os.path.abspath(os.path.expanduser(runs_directory)) if runs_directory else None
        )
        self.runtime = _require_runtime(runtime)
        self.model = model
        self.reader_intent = reader_intent
        self.persona = persona

        self.on_text: Callable[[str, str | None, str], None] | None = None
        self.on_graph_changed: Callable[[str, list[str]], None] | None = None
        self.on_diagnostic: Callable[[str], None] | None = None

        self._proc: asyncio.subprocess.Process | None = None
        self._ready = asyncio.Event()
        self._startup_error: str | None = None
        self._futures: dict[str, asyncio.Future] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._closed = False

    # -- startup ------------------------------------------------------------

    async def start(self) -> ReadingClient:
        if self._proc is not None:
            return self
        if not os.path.isdir(self.workspace):
            raise ReadingError(f"Workspace {self.workspace} is not a directory.")
        # A client outlives one engine: the TUI keeps it across runs, and an engine that
        # exited must not leave a stale readiness flag, error, or closed marker behind.
        self._ready = asyncio.Event()
        self._startup_error = None
        self._closed = False
        self._stderr_tail.clear()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            # No extra console window when the TUI starts the engine.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = await asyncio.create_subprocess_exec(
            _node_command(),
            self.runtime,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            limit=16 * 1024 * 1024,
            **kwargs,
        )
        self._write(self._hello())
        self._spawn(self._read_stdout(self._proc), "reading stdout")
        self._spawn(self._read_stderr(self._proc), "reading stderr")
        try:
            await asyncio.wait_for(self._ready.wait(), _READY_TIMEOUT)
        except TimeoutError:
            self.terminate()
            raise ReadingError(
                f"The reading engine did not start within {int(_READY_TIMEOUT)}s."
                + self._tail()
            ) from None
        if self._startup_error:
            self.terminate()
            raise ReadingError(self._startup_error)
        return self

    def _hello(self) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "hello", "workspace": self.workspace}
        if self.runs_directory:
            message["runsDirectory"] = self.runs_directory
        if self.model:
            message["model"] = self.model
        if self.reader_intent:
            message["readerIntent"] = self.reader_intent
        if self.persona:
            message["persona"] = self.persona
        return message

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- reading the child --------------------------------------------------

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        stream = proc.stdout
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                self._diagnose("reading: a protocol line was too long; the engine may have failed.")
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._diagnose(f"reading: unreadable line from the engine: {line[:200]}")
                continue
            if isinstance(message, dict):
                self._receive(message)
        self._on_exit(proc)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        stream = proc.stderr
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            self._diagnose(line)

    def _diagnose(self, line: str) -> None:
        if self.on_diagnostic is not None:
            self.on_diagnostic(line)
        else:
            print(line, file=sys.stderr, flush=True)

    def _receive(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ready":
            self._ready.set()
        elif kind == "text":
            callback = self.on_text
            if callback is not None:
                callback(
                    str(message.get("role") or "leader"),
                    message.get("nodeId"),
                    str(message.get("delta") or ""),
                )
        elif kind == "graph_changed":
            callback = self.on_graph_changed
            if callback is not None:
                nodes = [str(item) for item in message.get("nodeIds") or []]
                callback(str(message.get("runId") or ""), nodes)
        elif kind == "source_call":
            self._spawn(self._answer_source(message), "reading source_call")
        elif kind == "result":
            self._settle(str(message.get("requestId") or ""), message.get("result"), None)
        elif kind == "error":
            request_id = message.get("requestId")
            if request_id:
                self._settle(str(request_id), None, str(message.get("message") or "unknown error"))
            else:
                self._diagnose(f"reading: {message.get('message') or 'engine error'}")
        else:
            self._diagnose(f"reading: unknown message type {kind!r}")

    async def _answer_source(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("id") or "")
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        run_directory = params.get("runDirectory") or None
        try:
            materials = Materials(
                self.workspace, run_directory, runs_root=self.runs_directory
            )
            result = await materials.call(method, params)
            reply: dict[str, Any] = {"type": "source_result", "id": call_id, "result": result}
        except Exception as error:  # noqa: BLE001 - the engine needs a readable refusal
            reply = {"type": "source_result", "id": call_id, "error": str(error)}
        self._write(reply)

    def _settle(self, request_id: str, result: Any, error: str | None) -> None:
        future = self._futures.pop(request_id, None)
        if future is None:
            return
        if future.done():
            return
        if error is not None:
            future.set_exception(ReadingError(error))
        else:
            future.set_result(result)

    def _on_exit(self, proc: asyncio.subprocess.Process | None = None) -> None:
        if proc is not None and proc is not self._proc:
            # A reader belonging to an engine this client already let go.
            return
        # The engine is gone; the next request must spawn a fresh one rather than reuse a
        # process handle that can no longer answer.
        self._proc = None
        self._ready.set()
        reason = f"The reading engine stopped unexpectedly.{self._tail()}"
        self._startup_error = self._startup_error or reason
        for future in list(self._futures.values()):
            if not future.done():
                future.set_exception(ReadingError(reason))
        self._futures.clear()

    def _tail(self) -> str:
        if not self._stderr_tail:
            return ""
        return "\nEngine output:\n" + "\n".join(self._stderr_tail)

    # -- writing to the child ----------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing() or self._closed:
            return
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self._on_exit()

    # -- requests -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        await self.start()
        # One top-level operation at a time: the engine writes one graph for a run, and two
        # overlapping actions would race on the same file. Fork's own sessions still run
        # concurrently *inside* one request.
        async with self._request_lock:
            self._sequence += 1
            request_id = f"req-{self._sequence}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._futures[request_id] = future
            await self._drain_stdin()
            self._write({"type": "request", "id": request_id, "method": method, "params": params})
            try:
                if timeout:
                    return await asyncio.wait_for(future, timeout)
                return await future
            finally:
                self._futures.pop(request_id, None)

    async def _drain_stdin(self) -> None:
        """Let pending writes land before a new request (asyncio streams buffer on Windows)."""
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._on_exit()

    # -- the operations the CLI and the TUI share ---------------------------

    async def open(self, sources: list[str], question: str) -> dict[str, Any]:
        return await self._request("open", {"sources": list(sources), "question": question})

    async def act(
        self,
        run_id: str,
        action: str,
        *,
        node_id: str,
        focus: str | None = None,
        lens: str | None = None,
        context_mode: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"runId": run_id, "nodeId": node_id, "action": action}
        if question:
            params["question"] = question
        if context_mode:
            params["contextMode"] = context_mode
        if lens:
            params["lens"] = lens
        if focus:
            params["focus"] = focus
        return await self._request("act", params)

    async def fork(
        self,
        run_id: str,
        *,
        node_id: str,
        lenses: list[str],
        action: str = "reconstruct",
        question: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "runId": run_id,
            "nodeId": node_id,
            "lenses": list(lenses),
            "action": action,
        }
        if question:
            params["question"] = question
        return await self._request("fork", params)

    async def compare(
        self, run_id: str, node_ids: list[str], question: str
    ) -> dict[str, Any]:
        return await self._request(
            "compare", {"runId": run_id, "nodeIds": list(node_ids), "question": question}
        )

    async def reread(
        self,
        run_id: str,
        *,
        node_id: str,
        using: list[str],
        focus: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "runId": run_id,
            "nodeId": node_id,
            "usingNodeIds": list(using),
        }
        if focus:
            params["focus"] = focus
        return await self._request("reread", params)

    async def graph(self, run_id: str) -> dict[str, Any]:
        return await self._request("graph", {"runId": run_id})

    async def chat(self, run_id: str, message: str) -> dict[str, Any]:
        return await self._request("chat", {"runId": run_id, "message": message})

    # -- stopping -----------------------------------------------------------

    def terminate(self) -> None:
        """Stop the child without waiting. Used from cancellation and shutdown paths."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def aclose(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._closed = True
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), _CLOSE_TIMEOUT)
        except TimeoutError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()


# -- terminal rendering ------------------------------------------------------


class _Renderer:
    """Stream text to stdout, or to stderr when stdout is reserved for ``--json``."""

    def __init__(self, *, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.stream = sys.stderr if json_mode else sys.stdout
        self._active: tuple[str, str | None] | None = None
        self._columns = 0

    def text(self, role: str, node_id: str | None, delta: str) -> None:
        key = (role, node_id)
        if key != self._active:
            if self._active is not None and self._columns:
                self.stream.write("\n")
            label = "领读" if role == "leader" else ("对读" if role == "compare" else "研读")
            where = f" {node_id}" if node_id else ""
            self.stream.write(f"[{label}{where}] ")
            self._active = key
            self._columns = 0
        self.stream.write(delta)
        self.stream.flush()
        self._columns += len(delta)

    def graph_changed(self, run_id: str, node_ids: list[str]) -> None:
        if not node_ids or self.json_mode:
            return
        if self._columns:
            self.stream.write("\n")
            self.stream.flush()
        self.stream.write(f"（图已更新：{run_id} {' '.join(node_ids)}）\n")
        self.stream.flush()
        self._active = None
        self._columns = 0

    def finish(self) -> None:
        if self._columns:
            self.stream.write("\n")
            self.stream.flush()
        self._active = None
        self._columns = 0


def _emit(result: Any, *, json_mode: bool, human: Callable[[Any], None]) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        human(result)


def _print_lines(lines: list[str]) -> None:
    print("\n".join(lines))


def _human_open(result: dict[str, Any]) -> None:
    _print_lines(
        [
            f"run: {result.get('runId')}",
            f"目录: {result.get('runDirectory')}",
            f"入口节点: {result.get('currentNodeId')}",
        ]
    )


def _human_action(result: dict[str, Any]) -> None:
    _print_lines(
        [
            "",
            f"节点: {result.get('nodeId')}",
            f"正文: {result.get('bodyPath')}",
        ]
    )


def _human_fork(result: dict[str, Any]) -> None:
    nodes = result.get("nodeIds") or []
    _print_lines(["", f"分叉 {len(nodes)} 条："] + [f"  {node}" for node in nodes])


def _human_graph(result: dict[str, Any]) -> None:
    lines = [
        f"run: {result.get('runId')}",
        f"当前节点: {result.get('currentNodeId')}",
        f"图: {result.get('graphMarkdownPath') or result.get('graphPath')}",
        f"阅读地图: {result.get('readingPath')}",
        "",
        "节点：",
    ]
    for node in result.get("nodes") or []:
        lens = f" [{node['lens']}]" if node.get("lens") else ""
        actions = ", ".join(node.get("actions") or []) or "未读"
        lines.append(f"  {node.get('id')}{lens}  {actions}  {node.get('bodyPath')}")
    _print_lines(lines)


# -- command line ------------------------------------------------------------


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        default=None,
        help="Project directory the run belongs to (default: the current directory)",
    )
    parser.add_argument(
        "--runs-dir",
        default=None,
        help="Where runs are stored (default: <workspace>/reading)",
    )
    parser.add_argument("--json", action="store_true", help="Print only the result object on stdout")


def _model_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        metavar="PROVIDER/ID",
        help="Text model for this run (default: the profile's pin, then the site proxy)",
    )


def _parser(prog: str = "misaka read") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Reading action mode: read materials, fork views, compare and reread.",
    )
    sub = parser.add_subparsers(dest="operation", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("open", help="Open materials with a question and create a run")
    _common(p)
    _model_option(p)
    p.add_argument("sources", nargs="+", metavar="SOURCE", help="A file path or URL")
    p.add_argument("--question", required=True, help="The reader's question for this run")
    p.add_argument("--intent", default=None, help="What the reader wants out of the run")

    p = sub.add_parser("act", help="Run one reading action on a node")
    _common(p)
    _model_option(p)
    p.add_argument("run_id", metavar="RUN_ID")
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("--node", required=True, metavar="NODE_ID")
    p.add_argument("--focus", default=None, help="What to attend to in this action")
    p.add_argument("--lens", default=None, help="A view to read through")
    p.add_argument(
        "--context",
        choices=CONTEXT_MODES,
        default=None,
        help="How much prior understanding to carry (default: the action's own mode)",
    )
    p.add_argument("--question", default=None, help="Override the node's question")

    p = sub.add_parser("fork", help="Split a node into parallel reading lines")
    _common(p)
    _model_option(p)
    p.add_argument("run_id", metavar="RUN_ID")
    p.add_argument("--node", required=True, metavar="NODE_ID")
    p.add_argument("--lens", action="append", required=True, help="Repeat for each view")
    p.add_argument("--action", choices=ACTIONS, default="reconstruct")
    p.add_argument("--question", default=None)

    p = sub.add_parser("compare", help="Compare two or more completed nodes")
    _common(p)
    _model_option(p)
    p.add_argument("run_id", metavar="RUN_ID")
    p.add_argument("nodes", nargs="+", metavar="NODE_ID")
    p.add_argument("--question", required=True)

    p = sub.add_parser("reread", help="Return to an old node with a newer understanding")
    _common(p)
    _model_option(p)
    p.add_argument("run_id", metavar="RUN_ID")
    p.add_argument("--node", required=True, metavar="NODE_ID")
    p.add_argument("--using", action="append", required=True, metavar="NODE_ID")
    p.add_argument("--focus", default=None)

    p = sub.add_parser("graph", help="Show a run's reading map and node files")
    _common(p)
    p.add_argument("run_id", metavar="RUN_ID")

    p = sub.add_parser("chat", help="Talk with the run's leader (Ctrl+C or /stop to leave)")
    _common(p)
    _model_option(p)
    p.add_argument("run_id", metavar="RUN_ID")

    return parser


def _workspace(args: argparse.Namespace) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(args.workspace or os.getcwd())))


async def _resolve_model(args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        return await models.resolve_handover(
            getattr(args, "model", None), profile_dir=last_order_profile()
        )
    except models.ModelHandoverError as error:
        raise ReadingError(str(error)) from error


async def dispatch(client: ReadingClient, args: argparse.Namespace) -> dict[str, Any]:
    """Run one parsed subcommand against an already-configured client.

    This is the whole command surface, shared by ``misaka read`` and the TUI's ``/read``:
    the two differ only in where progress goes and whether the process outlives the call.
    """
    operation = args.operation
    if operation == "open":
        return await client.open(args.sources, args.question)
    if operation == "act":
        return await client.act(
            args.run_id,
            args.action,
            node_id=args.node,
            focus=args.focus,
            lens=args.lens,
            context_mode=args.context,
            question=args.question,
        )
    if operation == "fork":
        return await client.fork(
            args.run_id,
            node_id=args.node,
            lenses=args.lens,
            action=args.action,
            question=args.question,
        )
    if operation == "compare":
        if len(args.nodes) < 2:
            raise ReadingError("compare needs at least two node ids.")
        return await client.compare(args.run_id, args.nodes, args.question)
    if operation == "reread":
        return await client.reread(
            args.run_id, node_id=args.node, using=args.using, focus=args.focus
        )
    if operation == "graph":
        return await client.graph(args.run_id)
    raise ReadingError(f"{operation} is interactive; use `misaka read chat RUN_ID`.")


class ReadingSession:
    """One window's engine process, reused by every run it reads.

    The engine keeps opened runs and leader sessions in memory; creating a process per
    command would throw that away and re-open a session file each time. One process per
    window keeps the reading line continuous, while a run's graph on disk still makes the
    next window independent.
    """

    def __init__(
        self,
        workspace: str,
        *,
        runs_directory: str | None = None,
        runtime: str | None = None,
        handover: dict[str, Any] | None = None,
        reader_intent: str | None = None,
        persona: dict[str, str] | None = None,
    ) -> None:
        self.workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace)))
        self.runs_directory = runs_directory
        self.runtime = runtime
        self.handover = handover
        self.reader_intent = reader_intent
        self.persona = persona
        self.on_text: Callable[[str, str | None, str], None] | None = None
        self.on_graph_changed: Callable[[str, list[str]], None] | None = None
        self.on_diagnostic: Callable[[str], None] | None = None
        self._client: ReadingClient | None = None

    async def client(self) -> ReadingClient:
        if self._client is None:
            client = ReadingClient(
                self.workspace,
                runs_directory=self.runs_directory,
                runtime=self.runtime,
                model=self.handover,
                reader_intent=self.reader_intent,
                persona=self.persona,
            )
            client.on_text = self.on_text
            client.on_graph_changed = self.on_graph_changed
            client.on_diagnostic = self.on_diagnostic
            self._client = client
            await client.start()
        return self._client

    async def dispatch(self, args: argparse.Namespace) -> dict[str, Any]:
        return await dispatch(await self.client(), args)

    async def say(self, run_id: str, message: str) -> str:
        result = await (await self.client()).chat(run_id, message)
        return str(result.get("text") or "")

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def terminate(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.terminate()


async def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    operation = args.operation
    handover = await _resolve_model(args) if operation in MODEL_METHODS else None
    session = ReadingSession(
        _workspace(args),
        runs_directory=args.runs_dir,
        handover=handover,
        reader_intent=getattr(args, "intent", None),
        persona=persona_for(last_order_profile()),
    )
    renderer = _Renderer(json_mode=args.json)
    session.on_text = renderer.text
    session.on_graph_changed = renderer.graph_changed
    try:
        if operation == "chat":
            await _chat(await session.client(), args.run_id, renderer)
            return {"runId": args.run_id}
        result = await session.dispatch(args)
        renderer.finish()
        human = {
            "open": _human_open,
            "fork": _human_fork,
            "graph": _human_graph,
        }.get(operation, _human_action)
        _emit(result, json_mode=args.json, human=human)
        return result
    finally:
        renderer.finish()
        await session.close()


async def _chat(client: ReadingClient, run_id: str, renderer: _Renderer) -> None:
    """One line, one leader turn; ``/stop`` (or EOF) ends the session.

    Input is read on a thread so ``/stop`` can interrupt a turn that is still streaming.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def feed() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\n"))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    import threading

    threading.Thread(target=feed, daemon=True, name="reading-chat-input").start()
    print(f"与 {run_id} 的领读交谈。输入 /stop 或 /quit 离开。", file=sys.stderr, flush=True)

    while True:
        line = await queue.get()
        if line is None or line.strip() in {"/quit", "/exit"}:
            return
        if not line.strip():
            continue
        if line.strip() == "/stop":
            return
        turn = asyncio.ensure_future(client.chat(run_id, line))
        getter = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({turn, getter}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done and turn not in done:
            typed = getter.result()
            if typed is None:
                # End of input does not take back a turn already asked for: a piped message
                # expects its reply. Only /stop cancels; the next command resumes from disk.
                # A failed turn here is the whole reason this command ran, so it must reach
                # the caller as a failure rather than a cheerful exit code.
                await turn
                renderer.finish()
                return
            if typed.strip() in {"/stop", "/quit", "/exit"}:
                # Stop means stop: the child is closed and this run resumes from disk next time.
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
                renderer.finish()
                return
            await turn
            renderer.finish()
            if typed.strip():
                queue.put_nowait(typed)
            continue
        getter.cancel()
        try:
            await turn
        except ReadingError as error:
            renderer.finish()
            print(f"阅读出错：{error}", file=sys.stderr, flush=True)
        renderer.finish()


def usage_text(prog: str = "misaka read") -> str:
    return _parser(prog).format_help()


class ReadingHelp(Exception):
    """Argparse was asked for help: ``text`` is what it would have printed."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def parse_args(argv: list[str], prog: str = "misaka read") -> argparse.Namespace:
    """Parse one subcommand, turning argparse's own exit into an exception.

    ``misaka read`` could let argparse print and exit; the TUI cannot, because a ``SystemExit``
    inside a slash command would take the whole chat window with it. A help request raises
    :class:`ReadingHelp` (with the text to show), a bad one :class:`ReadingError`.
    """
    parser = _parser(prog)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            return parser.parse_args(list(argv))
    except SystemExit as exit:
        text = captured.getvalue()
        if not exit.code:
            raise ReadingHelp(text) from None
        # argparse leads with "usage:"; the last line carries the actual complaint.
        detail = text.strip().splitlines()[-1:] or ["invalid arguments"]
        raise ReadingError(detail[0]) from None


def main(argv: list[str] | None = None) -> int:
    """Run one reading command. The first docstring line is ``misaka --help`` text."""
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
        asyncio.run(_run_once(args))
    except ReadingHelp as help:
        print(help.text, end="", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\n已停止。阅读图与节点正文保留在磁盘上，可继续。", file=sys.stderr, flush=True)
        return 130
    except ReadingError as error:
        print(f"阅读失败：{error}", file=sys.stderr, flush=True)
        return 1
    return 0


__all__ = [
    "ACTIONS",
    "CONTEXT_MODES",
    "MODEL_METHODS",
    "ReadingClient",
    "ReadingError",
    "ReadingHelp",
    "ReadingSession",
    "default_runtime",
    "dispatch",
    "last_order_profile",
    "main",
    "parse_args",
    "persona_for",
    "usage_text",
]
