"""Programmatic RPC client for the coding-agent headless protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from misaka.ai.types import ImageContent
from misaka.modes.rpc.jsonl import JsonlLineBuffer, serialize_json_line
from misaka.modes.rpc.rpc_types import (
    RpcCommand,
    RpcResponse,
    RpcSessionState,
    RpcSlashCommand,
)

type RpcEventListener = Callable[[Any], None]


class ModelInfo(TypedDict):
    provider: str
    id: str
    contextWindow: int
    reasoning: bool


@dataclass(slots=True)
class RpcClientOptions:
    cliPath: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    args: list[str] = field(default_factory=list)


class RpcClient:
    def __init__(self, options: RpcClientOptions | dict[str, Any] | None = None) -> None:
        if isinstance(options, RpcClientOptions):
            self.options = options
            self._python_executable = sys.executable
            self._module = "misaka.cli"
        else:
            options_dict = dict(options or {})
            self._python_executable = str(options_dict.pop("pythonExecutable", sys.executable))
            self._module = str(options_dict.pop("module", "misaka.cli"))
            self.options = RpcClientOptions(**options_dict)
        self.process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._event_listeners: list[RpcEventListener] = []
        self._pending_requests: dict[str, asyncio.Future[RpcResponse]] = {}
        self._request_id = 0
        self._stderr = ""

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Client already started")

        argv = self._build_argv()
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.options.cwd,
            env={**os.environ, **(self.options.env or {})},
        )
        self._stdout_task = asyncio.create_task(self._consume_stdout())
        self._stderr_task = asyncio.create_task(self._consume_stderr())
        await asyncio.sleep(0.1)
        if self.process.returncode is not None:
            raise RuntimeError(
                f"Agent process exited immediately with code {self.process.returncode}. "
                f"Stderr: {self._stderr}"
            )

    async def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
        if self._stdout_task or self._stderr_task:
            await asyncio.gather(
                *(task for task in (self._stdout_task, self._stderr_task) if task is not None),
                return_exceptions=True,
            )
        self.process = None
        self._stdout_task = None
        self._stderr_task = None
        self._pending_requests.clear()

    def on_event(self, listener: RpcEventListener) -> Callable[[], None]:
        self._event_listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return unsubscribe

    def get_stderr(self) -> str:
        return self._stderr

    async def prompt(self, message: str, images: list[ImageContent] | None = None) -> None:
        await self._send({"type": "prompt", "message": message, "images": images})

    async def steer(self, message: str, images: list[ImageContent] | None = None) -> None:
        await self._send({"type": "steer", "message": message, "images": images})

    async def follow_up(self, message: str, images: list[ImageContent] | None = None) -> None:
        await self._send({"type": "follow_up", "message": message, "images": images})

    async def abort(self) -> None:
        await self._send({"type": "abort"})

    async def new_session(self, parentSession: str | None = None) -> dict[str, Any]:
        response = await self._send({"type": "new_session", "parentSession": parentSession})
        return self._get_data(response)

    async def get_state(self) -> RpcSessionState:
        response = await self._send({"type": "get_state"})
        return self._get_data(response)

    async def set_model(self, provider: str, modelId: str) -> dict[str, Any]:
        response = await self._send({"type": "set_model", "provider": provider, "modelId": modelId})
        return self._get_data(response)

    async def cycle_model(self) -> dict[str, Any] | None:
        response = await self._send({"type": "cycle_model"})
        return self._get_data(response)

    async def get_available_models(self) -> list[ModelInfo]:
        response = await self._send({"type": "get_available_models"})
        return self._get_data(response)["models"]

    async def set_thinking_level(self, level: str) -> None:
        await self._send({"type": "set_thinking_level", "level": level})

    async def cycle_thinking_level(self) -> dict[str, Any] | None:
        response = await self._send({"type": "cycle_thinking_level"})
        return self._get_data(response)

    async def set_steering_mode(self, mode: str) -> None:
        await self._send({"type": "set_steering_mode", "mode": mode})

    async def set_follow_up_mode(self, mode: str) -> None:
        await self._send({"type": "set_follow_up_mode", "mode": mode})

    async def compact(self, customInstructions: str | None = None) -> dict[str, Any]:
        response = await self._send({"type": "compact", "customInstructions": customInstructions})
        return self._get_data(response)

    async def set_auto_compaction(self, enabled: bool) -> None:
        await self._send({"type": "set_auto_compaction", "enabled": enabled})

    async def set_auto_retry(self, enabled: bool) -> None:
        await self._send({"type": "set_auto_retry", "enabled": enabled})

    async def abort_retry(self) -> None:
        await self._send({"type": "abort_retry"})

    async def bash(self, command: str) -> dict[str, Any]:
        response = await self._send({"type": "bash", "command": command})
        return self._get_data(response)

    async def abort_bash(self) -> None:
        await self._send({"type": "abort_bash"})

    async def get_session_stats(self) -> dict[str, Any]:
        response = await self._send({"type": "get_session_stats"})
        return self._get_data(response)

    async def export_html(self, outputPath: str | None = None) -> dict[str, Any]:
        response = await self._send({"type": "export_html", "outputPath": outputPath})
        return self._get_data(response)

    async def switch_session(self, sessionPath: str) -> dict[str, Any]:
        response = await self._send({"type": "switch_session", "sessionPath": sessionPath})
        return self._get_data(response)

    async def fork(self, entryId: str) -> dict[str, Any]:
        response = await self._send({"type": "fork", "entryId": entryId})
        return self._get_data(response)

    async def clone(self) -> dict[str, Any]:
        response = await self._send({"type": "clone"})
        return self._get_data(response)

    async def get_fork_messages(self) -> list[dict[str, str]]:
        response = await self._send({"type": "get_fork_messages"})
        return self._get_data(response)["messages"]

    async def get_last_assistant_text(self) -> str | None:
        response = await self._send({"type": "get_last_assistant_text"})
        return self._get_data(response)["text"]

    async def set_session_name(self, name: str) -> None:
        await self._send({"type": "set_session_name", "name": name})

    async def get_messages(self) -> list[dict[str, Any]]:
        response = await self._send({"type": "get_messages"})
        return self._get_data(response)["messages"]

    async def get_commands(self) -> list[RpcSlashCommand]:
        response = await self._send({"type": "get_commands"})
        return self._get_data(response)["commands"]

    async def wait_for_idle(self, timeout: float = 60.0) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def listener(event: dict[str, Any]) -> None:
            if event.get("type") == "agent_end" and not future.done():
                future.set_result(None)

        unsubscribe = self.on_event(listener)
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as error:
            raise RuntimeError(f"Timeout waiting for agent to become idle. Stderr: {self._stderr}") from error
        finally:
            unsubscribe()

    async def collect_events(self, timeout: float = 60.0) -> list[dict[str, Any]]:
        future: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()
        events: list[dict[str, Any]] = []

        def listener(event: dict[str, Any]) -> None:
            events.append(event)
            if event.get("type") == "agent_end" and not future.done():
                future.set_result(list(events))

        unsubscribe = self.on_event(listener)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as error:
            raise RuntimeError(f"Timeout collecting events. Stderr: {self._stderr}") from error
        finally:
            unsubscribe()

    async def prompt_and_wait(
        self,
        message: str,
        images: list[ImageContent] | None = None,
        timeout: float = 60.0,
    ) -> list[dict[str, Any]]:
        events_task = asyncio.create_task(self.collect_events(timeout=timeout))
        await self.prompt(message, images)
        return await events_task

    def _build_argv(self) -> list[str]:
        args = ["--mode", "rpc"]
        if self.options.provider:
            args.extend(["--provider", self.options.provider])
        if self.options.model:
            args.extend(["--model", self.options.model])
        args.extend(self.options.args)
        if self.options.cliPath:
            return [self._python_executable, self.options.cliPath, *args]
        return [
            self._python_executable,
            "-m",
            self._module,
            *args,
        ]

    async def _consume_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        reader = JsonlLineBuffer()
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            for line in reader.feed(chunk):
                self._handle_line(line)
        for line in reader.end():
            self._handle_line(line)

    async def _consume_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            self._stderr += text
            sys.stderr.write(text)

    def _handle_line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if (
            isinstance(data, dict)
            and data.get("type") == "response"
            and isinstance(data.get("id"), str)
            and data["id"] in self._pending_requests
        ):
            future = self._pending_requests.pop(data["id"])
            if not future.done():
                future.set_result(data)
            return
        for listener in list(self._event_listeners):
            listener(data)

    async def _send(self, command: RpcCommand, timeout: float = 30.0) -> RpcResponse:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Client not started")
        request_id = f"req_{self._request_id + 1}"
        self._request_id += 1
        future: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        payload = dict(command)
        payload["id"] = request_id
        self.process.stdin.write(serialize_json_line(payload).encode("utf-8"))
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as error:
            self._pending_requests.pop(request_id, None)
            raise RuntimeError(
                f"Timeout waiting for response to {command.get('type')}. Stderr: {self._stderr}"
            ) from error

    def _get_data(self, response: RpcResponse) -> Any:
        if not response.get("success", False):
            raise RuntimeError(str(response.get("error", "Unknown RPC error")))
        return response.get("data")

    onEvent = on_event
    getStderr = get_stderr
    followUp = follow_up
    newSession = new_session
    getState = get_state
    setModel = set_model
    cycleModel = cycle_model
    getAvailableModels = get_available_models
    setThinkingLevel = set_thinking_level
    cycleThinkingLevel = cycle_thinking_level
    setSteeringMode = set_steering_mode
    setFollowUpMode = set_follow_up_mode
    setAutoCompaction = set_auto_compaction
    setAutoRetry = set_auto_retry
    abortRetry = abort_retry
    abortBash = abort_bash
    getSessionStats = get_session_stats
    exportHtml = export_html
    switchSession = switch_session
    getForkMessages = get_fork_messages
    getLastAssistantText = get_last_assistant_text
    setSessionName = set_session_name
    getMessages = get_messages
    getCommands = get_commands
    waitForIdle = wait_for_idle
    collectEvents = collect_events
    promptAndWait = prompt_and_wait


__all__ = ["ModelInfo", "RpcClient", "RpcClientOptions", "RpcEventListener"]
