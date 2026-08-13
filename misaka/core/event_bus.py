"""Lightweight event bus for coding-agent runtime services."""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

type _EventHandler = Callable[[Any], Any]


class EventBus(Protocol):
    def emit(self, channel: str, data: Any) -> None: ...

    def on(self, channel: str, handler: _EventHandler) -> Callable[[], None]: ...


class EventBusController(EventBus, Protocol):
    def clear(self) -> None: ...


@dataclass(slots=True)
class _EventBusController:
    _listeners: dict[str, list[_EventHandler]] = field(default_factory=dict)

    def emit(self, channel: str, data: Any) -> None:
        for handler in list(self._listeners.get(channel, [])):
            try:
                result = handler(data)
            except Exception as error:
                _report_handler_error(channel, error)
                continue
            if inspect.isawaitable(result):
                _schedule_awaitable(channel, result)

    def on(self, channel: str, handler: _EventHandler) -> Callable[[], None]:
        listeners = self._listeners.setdefault(channel, [])
        listeners.append(handler)

        def unsubscribe() -> None:
            current = self._listeners.get(channel)
            if current and handler in current:
                current.remove(handler)

        return unsubscribe

    def clear(self) -> None:
        self._listeners.clear()


async def _await_handler(channel: str, awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception as error:
        _report_handler_error(channel, error)


def _schedule_awaitable(channel: str, awaitable: Awaitable[Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(
            target=lambda: asyncio.run(_await_handler(channel, awaitable)),
            name=f"event-bus-{channel}",
            daemon=True,
        ).start()
        return
    loop.create_task(_await_handler(channel, awaitable))


def _report_handler_error(channel: str, error: Exception) -> None:
    print(f"Event handler error ({channel}):", error, file=sys.stderr)


def createEventBus() -> EventBusController:
    return _EventBusController()

__all__ = [
    "EventBus",
    "EventBusController",
    "createEventBus",
]
