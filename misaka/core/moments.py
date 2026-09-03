"""The kernel's direct calls into MISAKA's own subsystems, at the moments Pi's kernel marks.

Pi's kernel does its own work at those moments by direct call -- ``_check_compaction`` is
the pattern -- and offers everything else the extension runner. MISAKA's subsystems used
to sit on the runner as extensions. Now they are *parts* of the session: assembled by the
process entry (``core.wiring.assemble``), handed in through ``AgentSessionConfig.parts``,
and called from here, ahead of the runner, with the same event payload and the same
folding the runner applies -- so an extension still sees the session as core left it.

A part is an object with a ``tools`` list, a ``commands`` list (``CoreCommand``), and
any of the methods named below, each ``async (event, ctx) -> dict | None``. A method a
part does not define is skipped. A raising part is logged and skipped, as the runner
does for a raising extension: a subsystem failing at a moment is a bug to fix, not a
reason to end the user's turn (``tool_call`` is the one fail-closed exception, as in
the runner). A part that defines ``attach(session)`` is handed the session when the
session is built; between moments it calls the session directly -- ``sendCustomMessage``
through ``Moments.send_message``, ``getActiveToolNames``, ``registerCustomTools``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MISSING = object()


def _field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


@dataclass(slots=True)
class BeforeAgentStart:
    """What the parts did with a turn about to start, folded the way the runner folds it."""

    system_prompt: str
    system_prompt_changed: bool = False
    messages: list[Any] = field(default_factory=list)
    block: bool = False
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class CoreCommand:
    """A slash command a part offers; ``handler(args, ctx)`` runs like an extension command."""

    name: str
    description: str
    handler: Callable[..., Any]
    argument_hint: str | None = None


class Moments:
    """One session's parts, and the kernel's way of calling them."""

    def __init__(self, session: Any, parts: list[Any]) -> None:
        self.session = session
        self.parts = list(parts)
        for part in self.parts:
            attach = getattr(part, "attach", None)
            if attach is not None:
                attach(session)

    def _ctx(self) -> Any:
        return self.session.extensionRunner.create_context()

    # -- what a part calls between moments (the runtime actions the runner binds, by direct call) --

    def send_message(self, message: Any, options: dict[str, Any] | None = None) -> None:
        """``sendCustomMessage`` scheduled the way the runner's ``sendMessage`` is: in order, off the caller."""

        async def _run() -> None:
            try:
                await self.session.sendCustomMessage(message, options)
            except Exception:
                logger.warning("a core part's message was not delivered", exc_info=True)

        self.session._spawn_extension_message(_run())

    def send_user_message(self, content: Any, options: dict[str, Any] | None = None) -> None:
        async def _run() -> None:
            try:
                await self.session.sendUserMessage(content, options)
            except Exception:
                logger.warning("a core part's user message was not delivered", exc_info=True)

        self.session._spawn_extension_message(_run())

    # -- commands --

    def commands(self) -> list[CoreCommand]:
        return [command for part in self.parts for command in getattr(part, "commands", ())]

    def command(self, name: str) -> CoreCommand | None:
        return next((command for command in self.commands() if command.name == name), None)

    async def _call(self, part: Any, name: str, event: dict[str, Any], ctx: Any) -> Any:
        method = getattr(part, name, None)
        if method is None or not callable(method):
            return None
        try:
            result = method(event, ctx)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception:
            logger.warning("core part %s failed at %s", type(part).__name__, name, exc_info=True)
            return None

    async def _notify(self, name: str, event: dict[str, Any]) -> None:
        if not self.parts:
            return
        ctx = self._ctx()
        for part in self.parts:
            await self._call(part, name, event, ctx)

    async def session_start(self, event: dict[str, Any]) -> None:
        await self._notify("session_start", event)

    async def session_shutdown(self, event: dict[str, Any]) -> None:
        await self._notify("session_shutdown", event)

    async def session_compact(self, event: dict[str, Any]) -> None:
        await self._notify("session_compact", event)

    async def session_compact_failed(self, event: dict[str, Any]) -> None:
        await self._notify("session_compact_failed", event)

    async def agent_start(self, event: dict[str, Any]) -> None:
        await self._notify("agent_start", event)

    async def ui_prompt_start(self, event: dict[str, Any]) -> None:
        await self._notify("ui_prompt_start", event)

    async def ui_prompt_end(self, event: dict[str, Any]) -> None:
        await self._notify("ui_prompt_end", event)

    async def session_before_fork(self, event: dict[str, Any]) -> Any:
        """Runner ``emit`` for this type: the first part to cancel wins."""
        if not self.parts:
            return None
        ctx = self._ctx()
        for part in self.parts:
            result = await self._call(part, "session_before_fork", event, ctx)
            if _field(result, "cancel", False):
                return result
        return None

    async def before_agent_start(
        self, prompt: str, images: Any, system_prompt: str, options: Any
    ) -> BeforeAgentStart:
        """Runner ``emit_before_agent_start``: each part sees the prompt as the previous one left it."""
        folded = BeforeAgentStart(system_prompt=system_prompt)
        if not self.parts:
            return folded
        ctx = self._ctx()
        for part in self.parts:
            result = await self._call(part, "before_agent_start", {
                "type": "before_agent_start",
                "prompt": prompt,
                "images": images,
                "systemPrompt": folded.system_prompt,
                "systemPromptOptions": options,
            }, ctx)
            if result is None:
                continue
            if _field(result, "block", False):
                folded.block = True
                folded.reason = _field(result, "reason")
                return folded
            message = _field(result, "message")
            if message is not None:
                folded.messages.append(message)
            replaced = _field(result, "systemPrompt")
            if replaced is not None:
                folded.system_prompt = str(replaced)
                folded.system_prompt_changed = True
        return folded

    async def agent_end(self, event: dict[str, Any]) -> Any:
        """Runner ``emit_agent_end``: the first part to block wins."""
        if not self.parts:
            return None
        ctx = self._ctx()
        for part in self.parts:
            result = await self._call(part, "agent_end", event, ctx)
            if _field(result, "block", False):
                return result
        return None

    async def session_before_compact(self, event: dict[str, Any]) -> Any:
        """Runner ``emit`` for this type: the first part to answer -- cancel or a compaction -- wins."""
        if not self.parts:
            return None
        ctx = self._ctx()
        for part in self.parts:
            result = await self._call(part, "session_before_compact", event, ctx)
            if result:
                return result
        return None

    async def tool_call(self, event: dict[str, Any]) -> Any:
        """Runner ``emit_tool_call``: the last answer stands, a block returns at once, a crash is fatal."""
        if not self.parts:
            return None
        ctx = self._ctx()
        result: Any = None
        for part in self.parts:
            method = getattr(part, "tool_call", None)
            if method is None:
                continue
            answer = method(event, ctx)
            if hasattr(answer, "__await__"):
                answer = await answer
            if answer:
                result = answer
                if _field(result, "block", False):
                    return result
        return result

    async def tool_result(self, event: dict[str, Any]) -> Any:
        """Runner ``emit_tool_result``: parts may replace content, details, isError, usage."""
        if not self.parts:
            return None
        ctx = self._ctx()
        current = dict(event)
        modified = False
        for part in self.parts:
            result = await self._call(part, "tool_result", current, ctx)
            if result is None:
                continue
            for name in ("content", "details", "isError", "usage"):
                value = _field(result, name, _MISSING)
                if value is not _MISSING:
                    current[name] = value
                    modified = True
        if not modified:
            return None
        return {name: current.get(name) for name in ("content", "details", "isError", "usage")}

    async def input(self, text: str, images: Any, source: Any, streaming_behavior: Any) -> dict[str, Any]:
        """Runner ``emit_input``: the first part to handle the input wins; transforms chain."""
        current_text, current_images = text, images
        if self.parts:
            ctx = self._ctx()
            for part in self.parts:
                result = await self._call(part, "input", {
                    "type": "input",
                    "text": current_text,
                    "images": current_images,
                    "source": source,
                    "streamingBehavior": streaming_behavior,
                }, ctx)
                action = _field(result, "action")
                if action == "handled":
                    return result
                if action == "transform":
                    current_text = _field(result, "text", current_text)
                    replaced = _field(result, "images", _MISSING)
                    if replaced is not _MISSING and replaced is not None:
                        current_images = replaced
        if current_text != text or current_images != images:
            return {"action": "transform", "text": current_text, "images": current_images}
        return {"action": "continue"}

    async def context(self, messages: list[Any]) -> list[Any]:
        """Runner ``emit_context``: each part rewrites the list the previous one produced."""
        if not self.parts:
            return messages
        ctx = self._ctx()
        current = deepcopy(messages)
        for part in self.parts:
            result = await self._call(part, "context", {"type": "context", "messages": current}, ctx)
            replaced = _field(result, "messages")
            if replaced is not None:
                current = replaced
        return current


__all__ = ["BeforeAgentStart", "CoreCommand", "Moments"]
