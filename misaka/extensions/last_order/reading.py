"""Reading action mode in the chat window: ``/read``.

The command line (``misaka read ...``) and this command drive the same engine through
:mod:`misaka.reading.host`. The only difference is what a window can offer: progress goes
to the status line and a widget instead of stdout, the engine process stays alive between
commands so the reading line is continuous, and ``/read stop`` ends it.

``/read open ...`` and the other subcommands accept exactly what ``misaka read`` accepts.
Two commands belong to the window alone: ``/read say RUN_ID ...`` sends a line to that
run's leader, and ``/read stop RUN_ID`` stops the engine this window started.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from misaka.reading import host

EXTENSION_NAME = "reading"

#: Reading is a foreground activity: a window has a person to report to, and a leader
#: conversation to hold. Background sessions have neither.
SESSION_KINDS = {"foreground"}

COMMAND = "read"

_STATUS = "reading"
_WIDGET = "reading"
_TAIL_LINES = 6


def command(argv):
    """Co-read materials with a question: open, act, fork, compare, reread, graph, chat.

    The same subcommands as ``misaka read``; run ``misaka read --help`` for the full list."""
    return host.main(argv)


class _Window:
    """One window's engine process and whatever it is doing right now."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.session: host.ReadingSession | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self._role: str | None = None
        self._node: str | None = None
        self._tail = ""

    # -- lifecycle ----------------------------------------------------------

    def session_for(
        self, args: Any, handover: dict[str, Any] | None, ui: Any = None
    ) -> host.ReadingSession:
        current = self.session
        workspace = os.path.realpath(os.path.abspath(os.path.expanduser(
            getattr(args, "workspace", None) or (current.workspace if current else self.spec.workspace)
        )))
        runs_directory = getattr(args, "runs_dir", None)
        if runs_directory:
            runs_directory = os.path.realpath(os.path.abspath(os.path.expanduser(runs_directory)))
        elif current is not None and current.workspace == workspace:
            runs_directory = current.runs_directory
        selected_model = handover if handover is not None else (current.handover if current else None)
        reader_intent = getattr(args, "intent", None)
        if reader_intent is None and current is not None:
            reader_intent = current.reader_intent
        if (
            current is not None
            and (workspace != current.workspace or runs_directory != current.runs_directory
                 or selected_model != current.handover or reader_intent != current.reader_intent)
        ):
            # Workspace, corpus roots and model belong to the process's initial hello.
            current.terminate()
            self.session = None
            if ui is not None:
                ui.notify("已按新的阅读设置重启引擎；阅读图与节点正文从磁盘续上。", "info")
        if self.session is None:
            self.session = host.ReadingSession(
                workspace,
                runs_directory=runs_directory,
                handover=selected_model,
                reader_intent=reader_intent,
                persona=host.persona_for(self.spec.profile_dir),
            )
        return self.session

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self) -> None:
        self.stopping = True
        session, self.session = self.session, None
        if session is not None:
            session.terminate()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    def terminate(self) -> None:
        session, self.session = self.session, None
        if session is not None:
            session.terminate()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    # -- progress -----------------------------------------------------------

    def reset_progress(self) -> None:
        self._role = None
        self._node = None
        self._tail = ""

    def on_text(self, ui: Any, role: str, node_id: str | None, delta: str) -> None:
        label = "领读" if role == "leader" else ("对读" if role == "compare" else "研读")
        if role != self._role or node_id != self._node:
            self._role = role
            self._node = node_id
            if self._tail:
                self._tail += "\n"
        self._tail = (self._tail + delta)[-2400:]
        where = f" {node_id}" if node_id else ""
        lines = [f"[{label}{where}]"]
        lines += self._tail.rstrip().splitlines()[-_TAIL_LINES:]
        ui.setWidget(_WIDGET, lines, {"placement": "aboveEditor"})

    def on_graph_changed(self, ui: Any, run_id: str, node_ids: list[str]) -> None:
        if node_ids:
            ui.setStatus(_STATUS, f"阅读图已更新：{run_id} {' '.join(node_ids)}")

    def clear(self, ui: Any) -> None:
        ui.setStatus(_STATUS, None)
        ui.setWidget(_WIDGET, None)


def _describe(result: dict[str, Any]) -> str:
    # `graph` returns the same run fields as `open`; only the map paths tell them apart,
    # and calling a read-only command "已建立阅读" would claim it created something.
    if result.get("graphMarkdownPath") or result.get("graphPath"):
        return f"阅读 {result['runId']}：图 {result.get('graphMarkdownPath') or result.get('graphPath')}"
    if result.get("runId") and "runDirectory" in result:
        return (
            f"已建立阅读：{result['runId']}，入口节点 {result.get('currentNodeId')}。"
            f"目录 {result.get('runDirectory')}"
        )
    if result.get("nodeIds"):
        return f"已分叉：{', '.join(result['nodeIds'])}"
    if result.get("nodeId"):
        return f"节点 {result['nodeId']} 已写入 {result.get('bodyPath')}"
    if result.get("runId"):
        return f"阅读 {result['runId']} 已完成"
    return "完成"


def _split(argv: str) -> list[str]:
    # The editor hands over the raw argument string. Windows paths carry backslashes that a
    # POSIX split would eat, so split by that dialect and then drop the quote characters it
    # keeps -- the user meant what is inside them.
    try:
        parts = shlex.split(argv, posix=False)
    except ValueError:
        return argv.split()
    return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
            for part in parts]


def activate(spec: Any):
    def register_for_session(harn: Any) -> None:
        register(harn, spec)

    return register_for_session


def register(harn: Any, spec: Any | None = None) -> None:
    window = _Window(spec)

    async def handler(_args: str, ctx: Any) -> None:
        if ctx.mode != "tui":
            ctx.ui.notify("/read is available in interactive mode", "warning")
            return
        window.spec = window.spec or getattr(ctx, "sessionSpec", None)
        if window.spec is None:
            from misaka.core.wiring import SessionSpec

            window.spec = SessionSpec(
                profile_dir=host.last_order_profile(),
                role="last_order",
                workspace=ctx.cwd,
                kind="foreground",
            )
        await _handle(window, harn, _split(_args), ctx)

    harn.registerCommand(
        "read",
        {"description": "Co-read materials with a question (/read --help)", "handler": handler},
    )

    def on_shutdown(*_args: Any) -> None:
        window.terminate()

    harn.on("session_shutdown", on_shutdown)


async def _handle(window: _Window, harn: Any, argv: list[str], ctx: Any) -> None:
    ui = ctx.ui
    if not argv or argv[0] in {"--help", "-h", "help"}:
        _show_usage(ui, host.usage_text("misaka /read"))
        return

    if argv[0] == "stop":
        if window.session is None and not window.busy():
            ui.notify("本窗口没有正在运行的阅读。", "warning")
            return
        await window.stop()
        window.clear(ui)
        ui.notify("阅读已停止。图与节点正文保留在磁盘上，可继续。")
        return

    if argv[0] == "say":
        await _say(window, argv[1:], ctx)
        return

    if window.busy():
        ui.notify("这个窗口正在阅读；等它完成，或先 /read stop。", "warning")
        return

    try:
        args = host.parse_args(argv, "misaka /read")
    except host.ReadingHelp as help:
        _show_usage(ui, help.text)
        return
    except host.ReadingError as error:
        ui.notify(f"/read：{error}", "error")
        return

    handover: dict[str, Any] | None = None
    if args.operation in host.MODEL_METHODS:
        handover = await _handover(args, ctx, ui)
        if handover is None:
            return

    window.stopping = False
    window.reset_progress()
    session = window.session_for(args, handover, ui)
    if args.operation == "chat":
        _note_chat_only(ui)
        return

    ui.setStatus(_STATUS, "阅读中…")
    session.on_text = lambda role, node, delta: window.on_text(ui, role, node, delta)
    session.on_graph_changed = lambda run_id, nodes: window.on_graph_changed(ui, run_id, nodes)
    session.on_diagnostic = lambda line: ui.setStatus(_STATUS, line[-120:])

    async def run() -> None:
        try:
            result = await session.dispatch(args)
            window.clear(ui)
            ui.notify(_describe(result))
        except asyncio.CancelledError:
            window.clear(ui)
            raise
        except host.ReadingError as error:
            window.clear(ui)
            if window.stopping:
                ui.notify("阅读已停止。")
            else:
                ui.notify(f"阅读失败：{error}", "error")
        except Exception as error:  # noqa: BLE001 - a broken reading must not take the chat down
            window.clear(ui)
            ui.notify(f"阅读出错：{error}", "error")
        finally:
            window.task = None

    window.task = asyncio.ensure_future(run())


async def _handover(args: Any, ctx: Any, ui: Any) -> dict[str, Any] | None:
    """The model parameters: ``--model``, else the model this chat is using."""
    from misaka.reading import models

    try:
        if getattr(args, "model", None):
            return await models.resolve_handover(
                args.model, profile_dir=host.last_order_profile()
            )
        model = getattr(ctx, "model", None)
        if model is not None:
            return await models.handover_for_model(model, ctx.modelRegistry)
        return await models.resolve_handover(profile_dir=host.last_order_profile())
    except models.ModelHandoverError as error:
        ui.notify(f"/read：{error}", "error")
        return None


async def _say(window: _Window, argv: list[str], ctx: Any) -> None:
    ui = ctx.ui
    options = _SayArgs()
    # A window that has not started an engine yet can be told which model to use; once it
    # has, the engine's model is fixed and a different choice restarts it (see session_for).
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag in {"--model", "--workspace", "--runs-dir", "--intent"} and argv:
            setattr(options, flag[2:].replace("-", "_"), argv.pop(0))
        else:
            ui.notify(f"/read say 不认得 {flag}", "warning")
            return
    if not argv:
        ui.notify("/read say RUN_ID ...", "warning")
        return
    run_id, message = argv[0], " ".join(argv[1:]).strip()
    if not message:
        ui.notify("要在 /read say RUN_ID 后面写你说的话。", "warning")
        return
    if window.busy():
        ui.notify("这个窗口仍在阅读；等它完成再说话。", "warning")
        return
    # open/graph do not need a model. The first say must still obtain the window's
    # selected model, and later turns must notice a changed selection.
    handover = await _handover(options, ctx, ui)
    if handover is None:
        return
    window.session_for(options, handover, ui)

    session = window.session
    assert session is not None
    window.reset_progress()
    session.on_text = lambda role, node, delta: window.on_text(ui, role, node, delta)
    session.on_graph_changed = lambda rid, nodes: window.on_graph_changed(ui, rid, nodes)
    ui.setStatus(_STATUS, f"与 {run_id} 的领读交谈…")

    async def run() -> None:
        try:
            reply = await session.say(run_id, message)
            window.clear(ui)
            ui.notify(reply[:2000] if reply else "领读没有回复。", "info")
        except host.ReadingError as error:
            window.clear(ui)
            ui.notify(f"阅读失败：{error}", "error")
        except asyncio.CancelledError:
            window.clear(ui)
            raise
        finally:
            window.task = None

    window.task = asyncio.ensure_future(run())


class _SayArgs:
    model = None
    runs_dir = None
    intent = None


def _note_chat_only(ui: Any) -> None:
    ui.notify("在窗口里用 /read say RUN_ID ... 与领读交谈；命令行用 misaka read chat RUN_ID。", "info")


def _show_usage(ui: Any, text: str) -> None:
    """Show the parser's own help, then the two commands that belong to the window."""
    lines = text.splitlines()
    lines += [
        "",
        "本窗口另有：",
        "  /read say RUN_ID ...    与阅读的领读交谈",
        "  /read stop RUN_ID       停止本窗口的阅读引擎",
    ]
    ui.setWidget(_WIDGET, lines[:30], {"placement": "aboveEditor"})
    ui.notify("用法已显示在输入框上方。")
