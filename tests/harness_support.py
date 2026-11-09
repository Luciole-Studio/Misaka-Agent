"""pi durable's ``test/harness-support.ts``. Messages are the plain ``dict`` objects entries store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from misaka.ai.models import createModels
from misaka.durable import (
    Harness,
    createRegistry,
    defineExtension,
    defineTool,
    hook,
    section,
)
from tests.durable_support import context

_hookExtensions = 0


def tool(name: str, description: str | None = None) -> dict[str, Any]:
    async def execute(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    return defineTool(
        {
            "name": name,
            "description": description if description is not None else f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
            "execute": execute,
        }
    )


async def openHarness(storage: Any, toolNames: list[str] | None = None, options: dict[str, Any] | None = None) -> Any:
    """Open a Harness with a fresh registry holding the named tools. Returns ``SimpleNamespace(harness, registry)``."""
    options = options or {}
    registry = options.get("registry") or createRegistry()
    if toolNames:
        registry.install(defineExtension({"name": "tools", "tools": [tool(name) for name in toolNames]}))
    harness = await Harness.open(
        storage,
        {
            "models": createModels(),
            "registry": registry,
            **({} if options.get("onReport") is None else {"onReport": options["onReport"]}),
        },
        context,
    )
    return SimpleNamespace(harness=harness, registry=registry)


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text, "timestamp": 1}


def assistant(text: str, calls: list[str] | None = None, stopReason: str | None = None) -> dict[str, Any]:
    callBlocks = [{"type": "toolCall", "id": id, "name": f"tool-{id}", "arguments": {}} for id in calls or []]
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}, *callBlocks],
        "api": "faux",
        "provider": "faux",
        "model": "faux",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": stopReason if stopReason is not None else ("toolUse" if callBlocks else "stop"),
        "timestamp": 2,
    }


def toolResult(id: str, text: str | None = None) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": id,
        "toolName": f"tool-{id}",
        "content": [{"type": "text", "text": text if text is not None else f"result {id}"}],
        "isError": False,
        "timestamp": 3,
    }


def system(sections: dict[str, str | None]) -> dict[str, Any]:
    return {"role": "system", "content": "", "sections": sections, "timestamp": 4}


def describeMessage(message: dict[str, Any]) -> str:
    """Compact message rendering for assertions."""
    role = message["role"]
    if role == "user":
        return f"user:{message['content']}"
    if role == "assistant":
        text = next((content for content in message["content"] if content["type"] == "text"), None)
        return f"assistant:{text['text'] if text is not None else ''}"
    if role == "toolResult":
        text = next((content for content in message["content"] if content["type"] == "text"), None)
        shown = "error" if message["isError"] else (text["text"] if text is not None else "")
        return f"result:{message['toolCallId']}:{shown}"
    return f"system:{','.join((message.get('sections') or {}).keys())}"


class Installed:
    """Uninstalls what one of the helpers below installed."""

    def __init__(self, registry: Any, extension: dict[str, Any]) -> None:
        registry.install(extension)
        self._registry = registry
        self._extension = extension

    def dispose(self) -> None:
        self._registry.uninstall(self._extension)


def addTool(registry: Any, tool: dict[str, Any], name: str | None = None) -> Installed:
    """Install a one-tool extension named after the tool."""
    return Installed(registry, defineExtension({"name": name or f"tool:{tool['name']}", "tools": [tool]}))


def addTask(registry: Any, task: dict[str, Any], name: str | None = None) -> Installed:
    """Install a one-task extension named after the task."""
    return Installed(registry, defineExtension({"name": name or f"task:{task['definition']['name']}", "tasks": [task]}))


def addHooks(registry: Any, task: dict[str, Any], handlers: dict[str, Any], name: str | None = None) -> Installed:
    """Install an extension with one hook registration for ``task``."""
    global _hookExtensions
    if name is None:
        _hookExtensions += 1
        name = f"hooks:{_hookExtensions}"
    return Installed(registry, defineExtension({"name": name, "hooks": [hook(task, handlers)]}))


def addSection(
    registry: Any, key: str, render: Any, options: dict[str, Any] | None = None, name: str | None = None
) -> Installed:
    """Install a one-section extension named after the section."""
    return Installed(registry, defineExtension({"name": name or f"section:{key}", "sections": [section(key, render, options)]}))
