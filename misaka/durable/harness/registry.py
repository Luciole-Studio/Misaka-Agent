"""The application-owned registry of extensions.

Translated from pi's ``packages/durable/src/harness/registry.ts``. An extension is a ``dict`` (``name``, ``tools``,
``sections``, ``hooks``, ``wraps``, ``tasks``); a snapshot answers ``installed()``, ``extension(name)``, ``tools()``,
``sections()``, ``tasks()`` and ``task(name)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .agent import INSTRUCTIONS_KEY
from .compaction import CompactionTask
from .generation import GenerationTask
from .tool import ToolTask

SECTION_KEY = re.compile(r"[a-z][a-z0-9_-]*")
SECTION_KEY_SOURCE = "/^[a-z][a-z0-9_-]*$/"

BUILTIN_TASKS: list[dict[str, Any]] = [GenerationTask, ToolTask, CompactionTask]
"""Built-in task definitions every registry holds; they are not an extension and cannot be removed or replaced."""


class RegistryState:
    """Immutable published registry state."""

    def __init__(self, extensions: list[dict[str, Any]]) -> None:
        self._extensions = extensions
        self._byName = {extension["name"]: extension for extension in extensions}
        tasks: dict[str, dict[str, Any]] = {}
        for task in BUILTIN_TASKS:
            tasks[task["definition"]["name"]] = task
        for extension in extensions:
            for task in extension.get("tasks") or []:
                name = task["definition"]["name"]
                if name in tasks:
                    raise RuntimeError(f"Task {name} of extension {extension['name']} is already installed")
                tasks[name] = task
        self._tasks = tasks

    def installed(self) -> list[dict[str, Any]]:
        return self._extensions

    def extension(self, name: str) -> dict[str, Any] | None:
        return self._byName.get(name)

    def tools(self) -> list[dict[str, Any]]:
        """Every installed tool with its extension, in install order. Names may repeat across extensions."""
        return [
            {"extension": extension, "tool": tool}
            for extension in self._extensions
            for tool in extension.get("tools") or []
        ]

    def sections(self) -> list[dict[str, Any]]:
        return [
            {"extension": extension, "section": section}
            for extension in self._extensions
            for section in extension.get("sections") or []
        ]

    def tasks(self) -> list[dict[str, Any]]:
        """Built-in and installed task definitions."""
        return list(self._tasks.values())

    def task(self, name: str) -> dict[str, Any] | None:
        return self._tasks.get(name)


class RegistryImpl:
    def __init__(self) -> None:
        self._current = RegistryState([])
        self._listeners: dict[Callable[[], None], None] = {}

    def snapshot(self) -> RegistryState:
        return self._current

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners[listener] = None

        def unsubscribe() -> None:
            self._listeners.pop(listener, None)

        return unsubscribe

    def install(self, extension: dict[str, Any]) -> None:
        """Install ``extension``, or replace the installed extension with its name in place. Publishes at once."""
        validateExtension(extension)
        current = self._current.installed()
        index = next((at for at, installed in enumerate(current) if installed["name"] == extension["name"]), -1)
        following = (
            [*current, extension]
            if index < 0
            else [extension if at == index else installed for at, installed in enumerate(current)]
        )
        self._publish(following)

    def uninstall(self, extension: dict[str, Any]) -> None:
        """Remove the installed extension with ``extension["name"]``, whichever object it is. A later install appends."""
        current = self._current.installed()
        if not any(installed["name"] == extension["name"] for installed in current):
            return
        self._publish([installed for installed in current if installed["name"] != extension["name"]])

    def _publish(self, extensions: list[dict[str, Any]]) -> None:
        """Build and validate the next state, which throws on a task name collision, then publish it synchronously."""
        self._current = RegistryState(extensions)
        for listener in list(self._listeners):
            listener()


def validateExtension(extension: dict[str, Any]) -> None:
    """Unique tool names and section keys within one extension; valid, unreserved section keys."""
    tools: set[str] = set()
    for tool in extension.get("tools") or []:
        if tool["name"] in tools:
            raise RuntimeError(f"Extension {extension['name']} has two tools named {tool['name']}")
        tools.add(tool["name"])
    sections: set[str] = set()
    for section in extension.get("sections") or []:
        key = section["key"]
        if SECTION_KEY.fullmatch(key) is None:
            raise TypeError(f"Section key {_quote(key)} must match {SECTION_KEY_SOURCE}")
        if key == INSTRUCTIONS_KEY:
            raise RuntimeError(f"Section key {key} is reserved for the agent's instructions")
        if key in sections:
            raise RuntimeError(f"Extension {extension['name']} has two sections with key {key}")
        sections.add(key)


def _quote(key: str) -> str:
    from misaka.utils.js import jsonStringify

    return jsonStringify(key)


def createRegistry() -> RegistryImpl:
    """Create an application-owned registry holding only the built-in tasks."""
    return RegistryImpl()


__all__ = ["BUILTIN_TASKS", "createRegistry"]
