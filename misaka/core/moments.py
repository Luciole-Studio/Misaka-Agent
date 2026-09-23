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

from misaka.ai.utils.transcript import get_current_system_message
from misaka.core.system_prompt import (
    build_system_prompt,
    normalize_build_system_prompt_options,
)

logger = logging.getLogger(__name__)

# What a part may say about a custom message it sends, in the message's ``details`` -- the one
# vocabulary a context engine (an extension that archives and summarises the conversation) reads,
# so that neither side has to know the other's message types.
MEMORY = "memory"   # False: a feed entry the model is never shown again (a status tick); an engine
                    # keeps it out of what it archives and summarises.
TURN = "turn"       # True: the message opens a turn the way a user prompt does (a work order); an
                    # engine that keys its per-turn work on ingress counts it as one.

_MISSING = object()


def _field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


@dataclass(slots=True)
class BeforeAgentStart:
    """What the parts did with a turn about to start, folded the way the runner folds it.

    `system_prompt_options` is the mutable, normalized options object every part saw; a part
    that returned `systemPrompt` set `forceSystemPrompt` on it, as an extension would."""

    system_prompt_options: dict[str, Any]
    messages: list[Any] = field(default_factory=list)
    block: bool = False
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class CoreCommand:
    """A part's slash command; prompt handlers return text, other handlers execute actions."""

    name: str
    description: str
    handler: Callable[..., Any]
    argument_hint: str | None = None
    # Return prompt text (or None for a handled/no-op command) to the owning
    # submission, rather than starting a second turn from a command context.
    is_prompt: bool = False


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

    async def resources_ready(self, event: dict[str, Any]) -> None:
        """Required resource barrier: failure stops startup, unlike notifications."""
        for part in self.parts:
            method = getattr(part, "resources_ready", None)
            if method is not None:
                await method(event, self._ctx())

    async def session_shutdown(self, event: dict[str, Any]) -> None:
        await self._notify("session_shutdown", event)

    async def session_compact(self, event: dict[str, Any]) -> None:
        await self._notify("session_compact", event)

    async def session_compact_failed(self, event: dict[str, Any]) -> None:
        await self._notify("session_compact_failed", event)

    async def agent_start(self, event: dict[str, Any]) -> None:
        await self._notify("agent_start", event)

    async def agent_settled(self, event: dict[str, Any]) -> None:
        await self._notify("agent_settled", event)

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

    async def before_agent_start(self, prompt: str, images: Any, options: Any) -> BeforeAgentStart:
        """Runner ``emit_before_agent_start``: each part sees the prompt options as the previous
        one left them, and ``systemPrompt`` renders them on every read."""
        current_options = normalize_build_system_prompt_options(options)
        folded = BeforeAgentStart(system_prompt_options=current_options)
        if not self.parts:
            return folded
        ctx = self._ctx()
        for part in self.parts:
            result = await self._call(part, "before_agent_start", {
                "type": "before_agent_start",
                "prompt": prompt,
                "images": images,
                "systemPrompt": build_system_prompt(current_options),
                "systemPromptOptions": current_options,
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
            # A native part can contribute independent catalog/hook messages.
            # Keep the existing singular contract and message order unchanged.
            messages = _field(result, "messages")
            if isinstance(messages, (list, tuple)):
                folded.messages.extend(messages)
            replaced = _field(result, "systemPrompt")
            if replaced is not None:
                current_options["forceSystemPrompt"] = str(replaced)
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

    async def session_context_prepare(self, event: dict[str, Any]) -> Any:
        """First context owner wins; a failed owner is not a request for Pi fallback."""
        for part in self.parts:
            method = getattr(part, "session_context_prepare", None)
            if method is not None:
                result = method(event, self._ctx())
                if hasattr(result, "__await__"):
                    result = await result
                if result is not None:
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
        updated_input = None
        for part in self.parts:
            method = getattr(part, "tool_call", None)
            if method is None:
                continue
            answer = method(event, ctx)
            if hasattr(answer, "__await__"):
                answer = await answer
            if answer:
                result = answer
                changed = _field(answer, "updatedInput", None)
                if changed is not None:
                    updated_input = changed
                    event["input"] = changed
                if _field(result, "block", False):
                    return result
        if updated_input is not None:
            return {"block": _field(result, "block", False),
                    "reason": _field(result, "reason", None),
                    "terminate": _field(result, "terminate", None),
                    "updatedInput": updated_input}
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
            for name in ("content", "details", "isError", "usage", "terminate"):
                value = _field(result, name, _MISSING)
                if value is not _MISSING:
                    current[name] = value
                    modified = True
        if not modified:
            return None
        folded = {name: current.get(name) for name in ("content", "details", "isError", "usage")}
        if "terminate" in current:
            folded["terminate"] = current["terminate"]
        return folded

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
        """Runner ``emit_context``: each part rewrites the list the previous one produced.

        As for extensions, a part's `context` sees the conversation only; the system messages
        are re-attached after it (an unchanged list keeps them in place, a changed one gets
        the replayed head). A part's `context_with_system` then sees the full transcript."""
        if not self.parts:
            return messages
        ctx = self._ctx()
        current = deepcopy(messages)
        for part in self.parts:
            visible = [m for m in current if _field(m, "role") != "system"]
            visible_snapshot = list(visible)
            result = await self._call(part, "context", {"type": "context", "messages": visible}, ctx)
            replaced = _field(result, "messages")
            if replaced is None:
                replaced = None if _same_messages(visible, visible_snapshot) else visible
            if replaced is None:
                continue
            current = _restore_system_messages(current, visible_snapshot, replaced)
        for part in self.parts:
            result = await self._call(part, "context_with_system", {"type": "context_with_system", "messages": current}, ctx)
            replaced = _field(result, "messages")
            if replaced is not None:
                current = replaced
        return current


def _same_messages(left: list[Any], right: list[Any]) -> bool:
    return len(left) == len(right) and all(message is right[index] for index, message in enumerate(left))


def _restore_system_messages(current: list[Any], visible: list[Any], returned: list[Any]) -> list[Any]:
    if _same_messages(returned, visible):
        return current
    head = get_current_system_message(current)
    return [head, *returned] if head else returned


__all__ = ["BeforeAgentStart", "CoreCommand", "Moments"]
