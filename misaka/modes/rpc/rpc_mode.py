"""Headless stdin/stdout RPC mode."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import uuid
from typing import Any

from misaka.core.output_guard import restoreStdout, takeOverStdout, writeRawStdout
from misaka.modes.rpc.jsonl import JsonlLineBuffer, serialize_json_line
from misaka.modes.rpc.rpc_types import (
    RpcCommand,
    RpcExtensionUIRequest,
    RpcExtensionUIResponse,
    RpcResponse,
    RpcSessionState,
    RpcSlashCommand,
)
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.shell import killTrackedDetachedChildren

_UNSET = object()


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


class _RpcUIContext:
    def __init__(
        self,
        output: Any,
        pending_requests: dict[str, asyncio.Future[RpcExtensionUIResponse]],
    ) -> None:
        self._output = output
        self._pending_requests = pending_requests

    async def _dialog(
        self,
        request: RpcExtensionUIRequest,
        *,
        default: Any,
        timeout: float | None = None,
        signal: Any = None,
    ) -> Any:
        if _value(signal, "aborted"):
            return default
        request_id = request["id"]
        future: asyncio.Future[RpcExtensionUIResponse] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        remove_abort_listener = None

        def cleanup() -> None:
            self._pending_requests.pop(request_id, None)
            if callable(remove_abort_listener):
                remove_abort_listener()

        def on_abort(*_args: Any) -> None:
            cleanup()
            if not future.done():
                future.set_result({"cancelled": True})

        add_abort_listener = getattr(signal, "addEventListener", None)
        remove_event_listener = getattr(signal, "removeEventListener", None)
        if callable(add_abort_listener):
            add_abort_listener("abort", on_abort, {"once": True})
            if callable(remove_event_listener):
                remove_abort_listener = lambda: remove_event_listener("abort", on_abort)

        self._output(request)
        try:
            response = await asyncio.wait_for(future, timeout=timeout) if timeout else await future
        except TimeoutError:
            cleanup()
            return default
        cleanup()
        if response.get("cancelled"):
            return default
        if "confirmed" in response:
            return response["confirmed"]
        return response.get("value", default)

    async def select(self, title: str, options: list[str], opts: Any = None) -> str | None:
        return await self._dialog(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "select",
                "title": title,
                "options": options,
                "timeout": _value(opts, "timeout"),
            },
            default=None,
            timeout=_value(opts, "timeout"),
            signal=_value(opts, "signal"),
        )

    async def confirm(self, title: str, message: str, opts: Any = None) -> bool:
        return bool(
            await self._dialog(
                {
                    "type": "extension_ui_request",
                    "id": str(uuid.uuid4()),
                    "method": "confirm",
                    "title": title,
                    "message": message,
                    "timeout": _value(opts, "timeout"),
                },
                default=False,
                timeout=_value(opts, "timeout"),
                signal=_value(opts, "signal"),
            )
        )

    async def input(self, title: str, placeholder: str | None = None, opts: Any = None) -> str | None:
        return await self._dialog(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "input",
                "title": title,
                "placeholder": placeholder or "",
                "timeout": _value(opts, "timeout"),
            },
            default=None,
            timeout=_value(opts, "timeout"),
            signal=_value(opts, "signal"),
        )

    async def editor(self, title: str, prefill: str | None = None) -> str | None:
        return await self._dialog(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "editor",
                "title": title,
                "prefill": prefill,
            },
            default=None,
            signal=None,
        )

    def notify(self, message: str, type: str | None = None) -> None:
        self._output(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "notify",
                "message": message,
                "notifyType": type,
            }
        )

    def onTerminalInput(self) -> Any:
        return lambda: None

    def setStatus(self, key: str, text: str | None) -> None:
        self._output(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "setStatus",
                "statusKey": key,
                "statusText": text,
            }
        )

    def setWorkingMessage(self, _message: str | None = None) -> None:
        return None

    def setWorkingVisible(self, _visible: bool) -> None:
        return None

    def setWorkingIndicator(self, _options: Any = None) -> None:
        return None

    def setHiddenThinkingLabel(self, _label: str | None = None) -> None:
        return None

    def setWidget(self, key: str, content: Any, options: Any = None) -> None:
        if content is None or isinstance(content, list):
            self._output(
                {
                    "type": "extension_ui_request",
                    "id": str(uuid.uuid4()),
                    "method": "setWidget",
                    "widgetKey": key,
                    "widgetLines": content,
                    "widgetPlacement": _value(options, "placement"),
                }
            )

    def setFooter(self, _factory: Any) -> None:
        return None

    def setHeader(self, _factory: Any) -> None:
        return None

    def setTitle(self, title: str) -> None:
        self._output(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "setTitle",
                "title": title,
            }
        )

    async def custom(self) -> None:
        return None

    def pasteToEditor(self, text: str) -> None:
        self.setEditorText(text)

    def setEditorText(self, text: str) -> None:
        self._output(
            {
                "type": "extension_ui_request",
                "id": str(uuid.uuid4()),
                "method": "set_editor_text",
                "text": text,
            }
        )

    def getEditorText(self) -> str:
        return ""

    def addAutocompleteProvider(self) -> None:
        return None

    def setEditorComponent(self) -> None:
        return None

    def getEditorComponent(self) -> None:
        return None

    @property
    def theme(self) -> Any:
        return theme

    def getAllThemes(self) -> list[Any]:
        return []

    def getTheme(self, _name: str | None = None) -> None:
        return None

    def setTheme(self, _theme: Any) -> dict[str, Any]:
        return {"success": False, "error": "Theme switching not supported in RPC mode"}

    def getToolsExpanded(self) -> bool:
        return False

    def setToolsExpanded(self, _expanded: bool) -> None:
        return None


class _ShutdownRequested(Exception):
    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


def _get_rpc_input_stream() -> Any:
    return sys.stdin


async def _run_rpc_mode(runtime_host: Any, *, input_stream: Any | None = None) -> int:
    takeOverStdout()
    session = runtime_host.session
    unsubscribe = None
    pending_extension_requests: dict[str, asyncio.Future[RpcExtensionUIResponse]] = {}
    shutdown_requested = False
    shutdown_exit_code = 0
    shutting_down = False
    cleaned_up = False
    signal_cleanup_handlers: list[tuple[int, Any]] = []
    source = getattr(input_stream or sys.stdin, "buffer", input_stream or sys.stdin)
    detach_input = lambda: None

    def output(obj: RpcResponse | RpcExtensionUIRequest | dict[str, Any]) -> None:
        writeRawStdout(serialize_json_line(obj))

    def success(request_id: str | None, command: str | None, data: Any = _UNSET) -> RpcResponse:
        response: RpcResponse = {"type": "response", "success": True}
        if request_id is not None:
            response["id"] = request_id
        if command is not None:
            response["command"] = command
        if data is not _UNSET:
            response["data"] = data
        return response

    def failure(request_id: str | None, command: str | None, message: str) -> RpcResponse:
        response: RpcResponse = {"type": "response", "success": False, "error": message}
        if request_id is not None:
            response["id"] = request_id
        if command is not None:
            response["command"] = command
        return response

    async def rebind_session() -> None:
        nonlocal session, unsubscribe
        session = runtime_host.session

        async def _fork(entry_id: str, options: Any = None) -> dict[str, Any]:
            result = await runtime_host.fork(entry_id, options)
            return {"cancelled": _value(result, "cancelled")}

        async def _navigate_tree(target_id: str, options: Any = None) -> dict[str, Any]:
            result = await session.navigateTree(
                target_id,
                {
                    "summarize": _value(options, "summarize"),
                    "customInstructions": _value(options, "customInstructions"),
                    "replaceInstructions": _value(options, "replaceInstructions"),
                    "label": _value(options, "label"),
                },
            )
            return {"cancelled": _value(result, "cancelled")}

        async def _reload() -> None:
            await session.reload()

        await session.bindExtensions(
            {
                "uiContext": _RpcUIContext(output, pending_extension_requests),
                "commandContextActions": {
                    "waitForIdle": lambda: session.agent.waitForIdle(),
                    "newSession": lambda options=None: runtime_host.newSession(options),
                    "fork": _fork,
                    "navigateTree": _navigate_tree,
                    "switchSession": (
                        lambda session_path, options=None: runtime_host.switchSession(session_path, options)
                    ),
                    "reload": _reload,
                },
                "shutdownHandler": lambda: _request_shutdown(),
                "onError": lambda error: output(
                    {
                        "type": "extension_error",
                        "extensionPath": _value(error, "extensionPath"),
                        "event": _value(error, "event"),
                        "error": _value(error, "error"),
                    }
                ),
            }
        )
        if callable(unsubscribe):
            unsubscribe()
        unsubscribe = session.subscribe(output)

    def _request_shutdown(exit_code: int = 0) -> None:
        nonlocal shutdown_requested, shutdown_exit_code
        shutdown_requested = True
        shutdown_exit_code = exit_code

    async def cleanup() -> None:
        nonlocal cleaned_up, unsubscribe
        if cleaned_up:
            return
        cleaned_up = True
        for current_signal, previous_handler in signal_cleanup_handlers:
            signal.signal(current_signal, previous_handler)
        if callable(unsubscribe):
            unsubscribe()
            unsubscribe = None
        await runtime_host.dispose()
        detach_input()
        pause = getattr(input_stream or sys.stdin, "pause", None)
        if callable(pause):
            pause()
        restoreStdout()

    async def shutdown(exit_code: int = 0) -> None:
        nonlocal shutting_down
        if shutting_down:
            raise _ShutdownRequested(exit_code)
        shutting_down = True
        await cleanup()
        raise _ShutdownRequested(exit_code)

    async def check_shutdown_requested() -> None:
        if not shutdown_requested:
            return
        await shutdown(shutdown_exit_code)

    def register_signal_handlers() -> None:
        current_loop = asyncio.get_running_loop()
        signals = [signal.SIGTERM]
        sighup = getattr(signal, "SIGHUP", None)
        if sys.platform != "win32" and sighup is not None:
            signals.append(sighup)

        for current_signal in signals:
            previous_handler = signal.getsignal(current_signal)

            def _handler(_signum: int, _frame: Any, *, current_signal: int = current_signal) -> None:
                killTrackedDetachedChildren()
                exit_code = 129 if sighup is not None and current_signal == sighup else 143
                current_loop.call_soon_threadsafe(_request_shutdown, exit_code)

            signal.signal(current_signal, _handler)
            signal_cleanup_handlers.append((current_signal, previous_handler))

    runtime_host.setRebindSession(rebind_session)
    await rebind_session()
    register_signal_handlers()

    async def handle_command(command: RpcCommand) -> RpcResponse | None:
        request_id = _value(command, "id")
        raw_command_type = _value(command, "type")
        command_type = raw_command_type if isinstance(raw_command_type, str) else None

        if command_type == "prompt":
            preflight_succeeded = False

            def on_preflight(success_value: bool) -> None:
                nonlocal preflight_succeeded
                if success_value and not preflight_succeeded:
                    preflight_succeeded = True
                    output(success(request_id, "prompt"))

            async def _run_prompt() -> None:
                try:
                    await session.prompt(
                        str(command.get("message", "")),
                        {
                            "images": command.get("images"),
                            "streamingBehavior": command.get("streamingBehavior"),
                            "source": "rpc",
                            "preflightResult": on_preflight,
                        },
                    )
                except Exception as error:  # noqa: BLE001
                    if not preflight_succeeded:
                        output(failure(request_id, "prompt", str(error)))

            asyncio.create_task(_run_prompt())
            return None

        if command_type == "steer":
            await session.steer(str(command.get("message", "")), command.get("images"))
            return success(request_id, "steer")

        if command_type == "follow_up":
            await session.followUp(str(command.get("message", "")), command.get("images"))
            return success(request_id, "follow_up")

        if command_type == "abort":
            await session.abort()
            return success(request_id, "abort")

        if command_type == "new_session":
            result = await runtime_host.newSession(
                {"parentSession": command["parentSession"]} if command.get("parentSession") else None
            )
            if not _value(result, "cancelled"):
                await rebind_session()
            return success(request_id, "new_session", result)

        if command_type == "get_state":
            state: RpcSessionState = {
                "model": session.model,
                "thinkingLevel": session.thinkingLevel,
                "isStreaming": session.isStreaming,
                "isCompacting": session.isCompacting,
                "steeringMode": session.steeringMode,
                "followUpMode": session.followUpMode,
                "sessionFile": session.sessionFile,
                "sessionId": session.sessionId,
                "sessionName": session.sessionName,
                "autoCompactionEnabled": session.autoCompactionEnabled,
                "messageCount": len(session.messages),
                "pendingMessageCount": session.pendingMessageCount,
            }
            return success(request_id, "get_state", state)

        if command_type == "set_model":
            models = await _maybe_await(session.modelRegistry.getAvailable())
            model = next(
                (
                    candidate
                    for candidate in models
                    if _value(candidate, "provider") == str(command.get("provider"))
                    and _value(candidate, "id") == str(command.get("modelId"))
                ),
                None,
            )
            if model is None:
                return failure(
                    request_id,
                    "set_model",
                    f"Model not found: {command.get('provider')}/{command.get('modelId')}",
                )
            await session.setModel(model)
            return success(request_id, "set_model", model.model_dump() if hasattr(model, "model_dump") else model)

        if command_type == "cycle_model":
            result = await session.cycleModel()
            if result is None:
                return success(request_id, "cycle_model", None)
            return success(
                request_id,
                "cycle_model",
                {
                    "model": result.model.model_dump() if hasattr(result.model, "model_dump") else result.model,
                    "thinkingLevel": result.thinkingLevel,
                    "isScoped": result.isScoped,
                },
            )

        if command_type == "get_available_models":
            return success(
                request_id,
                "get_available_models",
                {
                    "models": [
                        model.model_dump() if hasattr(model, "model_dump") else model
                        for model in await _maybe_await(session.modelRegistry.getAvailable())
                    ]
                },
            )

        if command_type == "set_thinking_level":
            session.setThinkingLevel(str(command.get("level")))
            return success(request_id, "set_thinking_level")

        if command_type == "cycle_thinking_level":
            level = session.cycleThinkingLevel()
            return success(request_id, "cycle_thinking_level", None if level is None else {"level": level})

        if command_type == "set_steering_mode":
            session.setSteeringMode(str(command.get("mode")))
            return success(request_id, "set_steering_mode")

        if command_type == "set_follow_up_mode":
            session.setFollowUpMode(str(command.get("mode")))
            return success(request_id, "set_follow_up_mode")

        if command_type == "compact":
            return success(request_id, "compact", await session.compact(command.get("customInstructions")))

        if command_type == "set_auto_compaction":
            session.setAutoCompactionEnabled(command.get("enabled"))
            return success(request_id, "set_auto_compaction")

        if command_type == "set_auto_retry":
            session.setAutoRetryEnabled(command.get("enabled"))
            return success(request_id, "set_auto_retry")

        if command_type == "abort_retry":
            session.abortRetry()
            return success(request_id, "abort_retry")

        if command_type == "bash":
            result = await session.executeBash(str(command.get("command", "")))
            return success(
                request_id,
                "bash",
                {
                    "output": result.output,
                    "exitCode": result.exitCode,
                    "cancelled": result.cancelled,
                    "truncated": result.truncated,
                    "fullOutputPath": result.fullOutputPath,
                },
            )

        if command_type == "abort_bash":
            session.abortBash()
            return success(request_id, "abort_bash")

        if command_type == "get_session_stats":
            return success(request_id, "get_session_stats", session.getSessionStats())

        if command_type == "export_html":
            path = await session.exportToHtml(command.get("outputPath"))
            return success(request_id, "export_html", {"path": path})

        if command_type == "switch_session":
            result = await runtime_host.switchSession(str(command.get("sessionPath")))
            if not _value(result, "cancelled"):
                await rebind_session()
            return success(request_id, "switch_session", result)

        if command_type == "fork":
            result = await runtime_host.fork(str(command.get("entryId")))
            if not _value(result, "cancelled"):
                await rebind_session()
            return success(
                request_id,
                "fork",
                {"text": _value(result, "selectedText"), "cancelled": bool(_value(result, "cancelled"))},
            )

        if command_type == "clone":
            leaf_id = session.sessionManager.getLeafId()
            if not leaf_id:
                return failure(request_id, "clone", "Cannot clone session: no current entry selected")
            result = await runtime_host.fork(leaf_id, {"position": "at"})
            if not _value(result, "cancelled"):
                await rebind_session()
            return success(request_id, "clone", {"cancelled": bool(_value(result, "cancelled"))})

        if command_type == "get_fork_messages":
            return success(request_id, "get_fork_messages", {"messages": session.getUserMessagesForForking()})

        if command_type == "get_last_assistant_text":
            return success(request_id, "get_last_assistant_text", {"text": session.getLastAssistantText()})

        if command_type == "set_session_name":
            name = str(command.get("name", "")).strip()
            if not name:
                return failure(request_id, "set_session_name", "Session name cannot be empty")
            session.setSessionName(name)
            return success(request_id, "set_session_name")

        if command_type == "get_messages":
            return success(request_id, "get_messages", {"messages": session.messages})

        if command_type == "get_commands":
            commands: list[RpcSlashCommand] = []
            for command_info in session.extensionRunner.get_registered_commands():
                commands.append(
                    {
                        "name": command_info.invocationName,
                        "description": command_info.description,
                        "source": "extension",
                        "sourceInfo": command_info.sourceInfo,
                    }
                )
            for template in session.promptTemplates:
                commands.append(
                    {
                        "name": template.name,
                        "description": template.description,
                        "source": "prompt",
                        "sourceInfo": template.sourceInfo,
                    }
                )
            return success(request_id, "get_commands", {"commands": commands})

        display_command_type = command_type if command_type is not None else "undefined"
        return failure(None, command_type, f"Unknown command: {display_command_type}")

    async def handle_input_line(line: str) -> None:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            output(failure(None, "parse", f"Failed to parse command: {error}"))
            return
        if isinstance(parsed, dict) and parsed.get("type") == "extension_ui_response":
            response = parsed
            request_id = response.get("id")
            if isinstance(request_id, str) and request_id in pending_extension_requests:
                future = pending_extension_requests.pop(request_id)
                if not future.done():
                    future.set_result(response)
            return
        command = parsed
        try:
            response = await handle_command(command)
            if response is not None:
                output(response)
            await check_shutdown_requested()
        except Exception as error:  # noqa: BLE001
            output(failure(_value(command, "id"), _value(command, "type"), str(error)))

    reader = JsonlLineBuffer()
    try:
        while True:
            # Read line by line: a fixed-size read() blocks until the buffer fills or EOF,
            # so clients that keep the connection open would never get a response.
            chunk = await asyncio.to_thread(source.readline)
            if not chunk:
                break
            for line in reader.feed(chunk):
                await handle_input_line(line)
                if shutdown_requested:
                    break
            if shutdown_requested:
                break
        if not shutdown_requested:
            for line in reader.end():
                await handle_input_line(line)
                if shutdown_requested:
                    break
        return shutdown_exit_code
    except _ShutdownRequested as stop:
        return stop.exit_code
    finally:
        await cleanup()


async def run_rpc_mode(runtime_host: Any) -> int:
    return await _run_rpc_mode(runtime_host, input_stream=_get_rpc_input_stream())


runRpcMode = run_rpc_mode

__all__ = [
    "RpcCommand",
    "RpcExtensionUIRequest",
    "RpcExtensionUIResponse",
    "RpcResponse",
    "RpcSessionState",
    "runRpcMode",
]
