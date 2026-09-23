"""Shared conversation rendering for interactive chat and read-only session views.

Only builds components. It owns no agent, session file, tools or dispatch loop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from misaka.core.agent_session import parse_skill_block
from misaka.core.session_manager import session_entry_to_display_messages
from misaka.ui.tui import Container, Spacer
from misaka.utils.values import read_field

from .assistant_message import AssistantMessageComponent
from .bash_execution import BashExecutionComponent
from .branch_summary_message import BranchSummaryMessageComponent
from .compaction_summary_message import CompactionSummaryMessageComponent
from .custom_entry import CustomEntryComponent
from .custom_message import CustomMessageComponent
from .skill_invocation_message import SkillInvocationMessageComponent
from .tool_execution import ToolExecutionComponent
from .user_message import UserMessageComponent


def user_text(message: Any) -> str:
    content = read_field(message, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(read_field(block, "text", "")) for block in content
        if read_field(block, "type") == "text"
    )


@dataclass
class TranscriptRenderer:
    ui: Any
    cwd: str
    markdownTheme: Any = None
    outputPad: int = 1
    hideThinkingBlock: bool = False
    hiddenThinkingLabel: str = "Thinking..."
    expanded: bool = False
    showImages: bool = True
    imageWidthCells: int = 40
    markdownTransformers: Sequence[Any] = ()
    extensionRunner: Any = None
    toolDefinition: Callable[[str], Any] | None = None
    retryAttempt: int = 0

    def customEntry(self, entry: Any) -> CustomEntryComponent | None:
        if self.extensionRunner is None:
            return None
        renderer = self.extensionRunner.get_entry_renderer(str(read_field(entry, "customType", "")))
        if renderer is None:
            return None
        component = CustomEntryComponent(entry, renderer)
        component.setExpanded(self.expanded)
        return component if component.hasContent() else None

    def addMessage(self, container: Container, message: Any) -> None:
        role = read_field(message, "role")
        if role == "user":
            text = user_text(message)
            if not text:
                return
            if container.children:
                container.addChild(Spacer(1))
            skill = parse_skill_block(text)
            if skill is not None:
                component = SkillInvocationMessageComponent(skill, self.markdownTheme)
                component.setExpanded(self.expanded)
                container.addChild(component)
                text = read_field(skill, "userMessage")
            if text:
                container.addChild(UserMessageComponent(
                    str(text), self.markdownTheme, self.outputPad, self.markdownTransformers,
                ))
            return
        if role == "assistant":
            container.addChild(AssistantMessageComponent(
                message, self.hideThinkingBlock, self.markdownTheme, self.hiddenThinkingLabel,
                self.outputPad, self.markdownTransformers,
            ))
            return
        if role == "bashExecution":
            component = BashExecutionComponent(
                str(read_field(message, "command", "")), self.ui,
                bool(read_field(message, "excludeFromContext", False)),
            )
            output = str(read_field(message, "output", ""))
            if output:
                component.appendOutput(output)
            component.setComplete(
                read_field(message, "exitCode"), bool(read_field(message, "cancelled", False)),
                None, read_field(message, "fullOutputPath"),
            )
        elif role == "custom":
            if not read_field(message, "display", False):
                return
            get_renderer = (getattr(self.extensionRunner, "get_message_renderer", None)
                            or getattr(self.extensionRunner, "getMessageRenderer", None))
            renderer = get_renderer(str(read_field(message, "customType", ""))) if get_renderer else None
            component = CustomMessageComponent(message, renderer, self.markdownTheme, self.outputPad)
        elif role in {"branchSummary", "compactionSummary"}:
            container.addChild(Spacer(1))
            cls = BranchSummaryMessageComponent if role == "branchSummary" else CompactionSummaryMessageComponent
            component = cls(message, self.markdownTheme)
        else:
            return
        component.setExpanded(self.expanded)
        container.addChild(component)

    def appendItem(self, container: Container, message: Any, pending: dict[str, ToolExecutionComponent]) -> None:
        """Replay one item; a result updates its call instead of appearing a second time."""
        role = read_field(message, "role")
        if read_field(message, "type") == "custom" and role is None:
            component = self.customEntry(message)
            if component is not None:
                container.addChild(component)
            return
        if role == "toolResult":
            component = pending.pop(str(read_field(message, "toolCallId", "")), None)
            if component is not None:
                component.updateResult(message)
            return
        self.addMessage(container, message)
        if role != "assistant":
            return
        for call in read_field(message, "content", []) or []:
            if read_field(call, "type") != "toolCall":
                continue
            name, call_id = str(read_field(call, "name", "")), str(read_field(call, "id", ""))
            component = ToolExecutionComponent(
                name, call_id, read_field(call, "arguments", {}),
                {"showImages": self.showImages, "imageWidthCells": self.imageWidthCells},
                self.toolDefinition(name) if self.toolDefinition else None, self.ui, self.cwd,
            )
            component.setArgsComplete()
            component.setExpanded(self.expanded)
            container.addChild(component)
            stop = read_field(message, "stopReason")
            if stop in {"aborted", "error"}:
                error = (f"Aborted after {self.retryAttempt} retry attempt{'s' if self.retryAttempt > 1 else ''}"
                         if self.retryAttempt else "Operation aborted")
                if stop == "error":
                    error = str(read_field(message, "errorMessage", "") or "Error")
                component.updateResult({"content": [{"type": "text", "text": error}], "isError": True})
            else:
                pending[call_id] = component

    def appendEntries(self, container: Container, entries: list[Any], pending: dict[str, ToolExecutionComponent]) -> None:
        for entry in entries:
            items = [entry] if read_field(entry, "type") == "custom" else session_entry_to_display_messages(entry)
            for item in items:
                self.appendItem(container, item, pending)
