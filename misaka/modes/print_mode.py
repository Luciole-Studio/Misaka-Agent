"""Print mode for single-shot non-interactive execution."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from misaka.ai.types import ImageContent
from misaka.core.output_guard import (
    flushRawStdout,
    writeRawStdout,
)
from misaka.utils.shell import killTrackedDetachedChildren
from misaka.utils.values import read_field


@dataclass(slots=True)
class PrintModeOptions:
    mode: str = "text"
    messages: list[str] = field(default_factory=list)
    initialMessage: str | None = None
    initialImages: list[ImageContent] | None = None


def _message_role(message: Any) -> str | None:
    role = read_field(message, "role")
    return role if isinstance(role, str) else None


def _message_content(message: Any) -> Any:
    return read_field(message, "content")


def _content_type(block: Any) -> str | None:
    block_type = read_field(block, "type")
    return block_type if isinstance(block_type, str) else None


def _serialize_json_line(value: Any) -> str:
    from misaka.modes.jsonl import to_json_event
    return json.dumps(to_json_event(value), ensure_ascii=False, separators=(",", ":")) + "\n"


async def run_print_mode(runtime_host: Any, options: PrintModeOptions | dict[str, Any]) -> int:
    resolved = options if isinstance(options, PrintModeOptions) else PrintModeOptions(**dict(options or {}))
    session = runtime_host.session
    unsubscribe = None
    disposed = False
    signal_cleanup_handlers: list[tuple[int, Any]] = []

    async def dispose_runtime() -> None:
        nonlocal disposed, unsubscribe
        if disposed:
            return
        disposed = True
        if callable(unsubscribe):
            unsubscribe()
            unsubscribe = None
        await runtime_host.dispose()

    async def rebind_session(_session=None) -> None:   # the runtime passes the new session; we read runtime_host.session
        nonlocal session, unsubscribe
        session = runtime_host.session

        async def _fork(entry_id: str, fork_options: Any = None) -> dict[str, Any]:
            result = await runtime_host.fork(entry_id, fork_options)
            return {"cancelled": read_field(result, "cancelled")}

        async def _navigate_tree(target_id: str, navigate_options: Any = None) -> dict[str, Any]:
            result = await session.navigateTree(
                target_id,
                {
                    "summarize": read_field(navigate_options, "summarize"),
                    "customInstructions": read_field(navigate_options, "customInstructions"),
                    "replaceInstructions": read_field(navigate_options, "replaceInstructions"),
                    "label": read_field(navigate_options, "label"),
                },
            )
            return {"cancelled": read_field(result, "cancelled")}

        async def _reload() -> None:
            await session.reload()

        await session.bindExtensions(
            {
                # pi print-mode.ts:76-77 tells extensions which non-interactive wire form is live.
                "mode": "json" if resolved.mode == "json" else "print",
                "commandContextActions": {
                    # Session-level, not agent-level (pi print-mode.ts:79).
                    "waitForIdle": lambda: session.waitForIdle(),
                    "newSession": lambda new_session_options=None: runtime_host.newSession(new_session_options),
                    "fork": _fork,
                    "navigateTree": _navigate_tree,
                    "switchSession": lambda session_path, switch_options=None: runtime_host.switchSession(
                        session_path,
                        switch_options,
                    ),
                    "reload": _reload,
                },
                "onError": lambda error: print(
                    f"Extension error ({read_field(error, 'extensionPath', '<unknown>')}): "
                    f"{read_field(error, 'error', error)}",
                    file=sys.stderr,
                ),
            }
        )
        if callable(unsubscribe):
            unsubscribe()
        unsubscribe = session.subscribe(
            lambda event: writeRawStdout(_serialize_json_line(event)) if resolved.mode == "json" else None
        )

    def register_signal_handlers() -> None:
        loop = asyncio.get_running_loop()
        signals = [signal.SIGTERM]
        sighup = getattr(signal, "SIGHUP", None)
        if sys.platform != "win32" and sighup is not None:
            signals.append(sighup)

        for current_signal in signals:
            previous_handler = signal.getsignal(current_signal)

            def _handler(_signum: int, _frame: Any, *, current_signal: int = current_signal) -> None:
                killTrackedDetachedChildren()
                exit_code = 129 if sighup is not None and current_signal == sighup else 143
                task = loop.create_task(dispose_runtime())
                task.add_done_callback(lambda _task, exit_code=exit_code: sys.exit(exit_code))

            signal.signal(current_signal, _handler)
            signal_cleanup_handlers.append((current_signal, previous_handler))

    try:
        register_signal_handlers()
        runtime_host.setRebindSession(rebind_session)

        if resolved.mode == "json":
            header = session.sessionManager.getHeader()
            if header:
                writeRawStdout(_serialize_json_line(header))

        await rebind_session()

        if resolved.initialMessage:
            await session.prompt(
                resolved.initialMessage,
                {
                    "images": resolved.initialImages,
                },
            )

        for message in resolved.messages:
            await session.prompt(message)

        if resolved.mode == "text":
            last_message = session.state.messages[-1] if session.state.messages else None
            if _message_role(last_message) == "assistant":
                stop_reason = read_field(last_message, "stopReason")
                if stop_reason in {"error", "aborted"}:
                    print(read_field(last_message, "errorMessage") or f"Request {stop_reason}", file=sys.stderr)
                    return 1
                for content in _message_content(last_message) or []:
                    if _content_type(content) == "text":
                        writeRawStdout(f"{read_field(content, 'text', '')}\n")

        return 0
    except Exception as error:  # noqa: BLE001
        print(str(error), file=sys.stderr)
        return 1
    finally:
        for current_signal, previous_handler in signal_cleanup_handlers:
            signal.signal(current_signal, previous_handler)
        await dispose_runtime()
        await flushRawStdout()

runPrintMode = run_print_mode

__all__ = ["PrintModeOptions", "runPrintMode"]
