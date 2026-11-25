"""MISAKA's own subsystems inside a session: its *parts*.

A part is an object with a ``tools`` list, a ``commands`` list (``CoreCommand``), and any of
the methods named in ``EVENTS``, each ``(event, ctx) -> dict | None``. The process entry
assembles the parts (``core.wiring.assemble``) and hands them in through
``AgentSessionConfig.parts``.

A part's event methods run on the session's extension runner, ahead of every extension
(``Moments.extensions``): one dispatcher folds the answers of parts and extensions alike, so
an extension sees an event as the parts left it. What differs for a part is written here and
nowhere else. A part that raises is logged and skipped, where an extension's error is reported
to the user; ``tool_call`` and ``session_context_prepare`` stay fail-closed for both. An
announcement raised on a replaced runner is not delivered to a part.

A part that defines ``attach(session)`` is handed the session when the session is built;
between events it calls the session directly -- ``sendCustomMessage`` through
``Moments.send_message``, ``getActiveToolNames``, ``registerCustomTools``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MEMORY = "memory"
TURN = "turn"
SENDERS = "senders"

EVENTS: tuple[str, ...] = (
    "session_start",
    "session_tree",
    "session_shutdown",
    "session_compact",
    "session_compact_failed",
    "agent_start",
    "agent_settled",
    "ui_prompt_start",
    "ui_prompt_end",
    "session_before_fork",
    "before_agent_start",
    "agent_end",
    "session_context_prepare",
    "session_before_compact",
    "tool_call",
    "tool_result",
    "input",
    "context",
    "context_with_system",
)
_ANNOUNCEMENTS = frozenset({
    "session_start",
    "session_tree",
    "session_shutdown",
    "session_compact",
    "session_compact_failed",
    "agent_start",
    "agent_settled",
    "ui_prompt_start",
    "ui_prompt_end",
})
_FAIL_CLOSED = frozenset({"tool_call", "session_context_prepare"})


def opens_turn(event: Any) -> bool:
    """Whether a ``before_agent_start`` event starts a turn the way a user prompt does: a user
    prompt, or a custom message that says so with ``details[TURN]`` (a research phase, a failed
    node). A run a notification wakes (a Sister's result, a task or shell notification, mail)
    does not; per-prompt work keys on this. An event without ``messageType`` is a user prompt."""
    if _field(event, "messageType", "user") == "user":
        return True
    details = _field(_field(event, "message"), "details")
    return isinstance(details, dict) and details.get(TURN) is True


def _field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


@dataclass(slots=True, frozen=True)
class CoreCommand:
    """A part's slash command; prompt handlers return text, other handlers execute actions."""

    name: str
    description: str
    handler: Callable[..., Any]
    argument_hint: str | None = None
    is_prompt: bool = False


class Moments:
    """One session's parts: what the kernel asks of them directly, and their seat on the runner."""

    def __init__(self, session: Any, parts: list[Any]) -> None:
        self.session = session
        self.parts = list(parts)
        for part in self.parts:
            attach = getattr(part, "attach", None)
            if attach is not None:
                attach(session)

    def _ctx(self) -> Any:
        return self.session.extensionRunner.createContext()

    def send_message(self, message: Any, options: dict[str, Any] | None = None) -> None:
        """``sendCustomMessage`` scheduled the way the runner's ``sendMessage`` is: in order, off the caller."""

        async def _run() -> None:
            try:
                await self.session.sendCustomMessage(message, options)
            except Exception:
                logger.warning("a core part's message was not delivered", exc_info=True)

        self.session._spawnExtensionMessage(_run())

    def send_user_message(self, content: Any, options: dict[str, Any] | None = None) -> None:
        async def _run() -> None:
            try:
                await self.session.sendUserMessage(content, options)
            except Exception:
                logger.warning("a core part's user message was not delivered", exc_info=True)

        self.session._spawnExtensionMessage(_run())

    def commands(self) -> list[CoreCommand]:
        return [command for part in self.parts for command in getattr(part, "commands", ())]

    def command(self, name: str) -> CoreCommand | None:
        return next((command for command in self.commands() if command.name == name), None)

    def settle_tool_turn(self, turn: Any) -> bool:
        """Whether an owning part requires this completed tool batch to settle before new work."""
        return any(consider(turn) for part in self.parts if (consider := getattr(part, "settle_tool_turn", None)))

    def project_tools(self, tools: list[Any]) -> list[Any]:
        """Request-visible views only; canonical registry definitions keep their owner."""
        for part in self.parts:
            project = getattr(part, "project_tools", None)
            if project is not None:
                tools = project(tools)
        return tools

    def configure_tool_options(self, options: dict[str, Any]) -> None:
        """Parts configure native builtins before creation; SDK/extension overrides keep ownership."""
        for part in self.parts:
            configure = getattr(part, "configure_tool_options", None)
            if configure is not None:
                configure(options)

    def configure_tools(self, definitions: list[Any], extensions: list[Any]) -> list[Any]:
        """Refresh extension-dependent parts in the same tool-registry transaction.

        This synchronous hook runs at construction, live registration and headless reload,
        before the existing permission ceiling. Identity matching preserves unrelated SDK
        and MCP tools, including same-named definitions supplied by another owner.
        """
        for part in self.parts:
            configure = getattr(part, "configure_tools", None)
            if configure is None:
                continue
            previous = tuple(part.tools)
            configure(extensions)
            if tuple(map(id, previous)) == tuple(map(id, part.tools)):
                continue
            old_ids = {id(tool) for tool in previous}
            position = next((i for i, tool in enumerate(definitions) if id(tool) in old_ids), len(definitions))
            definitions = [tool for tool in definitions if id(tool) not in old_ids]
            definitions[position:position] = part.tools
        return definitions

    def extensions(self) -> list[Any]:
        """The parts as hidden extensions, one handler table each, for the head of the runner's list."""
        from misaka.core.extensions.types import Extension
        from misaka.core.source_info import createSyntheticSourceInfo

        seats: list[Any] = []
        for part in self.parts:
            handlers = {
                event: [self._handler(part, event)] for event in EVENTS if callable(getattr(part, event, None))
            }
            if handlers:
                path = f"<core:{type(part).__name__}>"
                seats.append(Extension(
                    path=path,
                    resolvedPath=path,
                    sourceInfo=createSyntheticSourceInfo(path, {"source": "sdk"}),
                    handlers=handlers,
                    hidden=True,
                ))
        return seats

    def _handler(self, part: Any, name: str) -> Callable[..., Any]:
        async def handler(event: Any, ctx: Any) -> Any:
            if name in _ANNOUNCEMENTS and getattr(self.session.extensionRunner, "staleMessage", None):
                return None
            try:
                result = getattr(part, name)(event, ctx)
                if hasattr(result, "__await__"):
                    result = await result
                return result
            except Exception:
                if name in _FAIL_CLOSED:
                    raise
                logger.warning("core part %s failed at %s", type(part).__name__, name, exc_info=True)
                return None

        return handler

    async def resources_ready(self, event: dict[str, Any]) -> None:
        """Required resource barrier: failure stops startup, unlike notifications."""
        for part in self.parts:
            method = getattr(part, "resources_ready", None)
            if method is not None:
                await method(event, self._ctx())


__all__ = ["EVENTS", "CoreCommand", "Moments", "opens_turn"]
