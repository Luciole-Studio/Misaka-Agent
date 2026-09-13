"""The conversation screen, independent of who drives or owns the session.

InteractiveMode adds the agent/editor lifecycle. Opening saved messages uses this
same screen without constructing either an agent or a writable SessionManager.
"""

from dataclasses import replace
from typing import Any

from misaka.ui.tui import Container

from .components.assistant_message import AssistantMessageComponent
from .components.transcript import TranscriptRenderer
from .theme.theme import get_markdown_theme


def _safe_call_bool(obj: Any, name: str, default: bool = False) -> bool:
    getter = getattr(obj, name, None)
    if not callable(getter):
        return default
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 - an accessor that fails yields the default
        return default


def _safe_call_int(obj: Any, name: str, default: int = 0) -> int:
    getter = getattr(obj, name, None)
    if not callable(getter):
        return default
    try:
        return int(getter())
    except Exception:  # noqa: BLE001 - an accessor that fails yields the default
        return default


def _safe_call_str(obj: Any, name: str, default: str | None = None) -> str | None:
    getter = getattr(obj, name, None)
    if not callable(getter):
        return default
    try:
        value = getter()
    except Exception:  # noqa: BLE001 - an accessor that fails yields the default
        return default
    return str(value) if value is not None else default


class Conversation:
    def __init__(self, ui, settingsManager, cwd):
        self.ui, self.settingsManager, self.cwd = ui, settingsManager, cwd
        for name in (
            "headerContainer", "chatContainer", "pendingMessagesContainer", "statusContainer",
            "widgetContainerAbove", "editorContainer", "widgetContainerBelow",
        ):
            if not hasattr(self, name):
                setattr(self, name, Container())
        self.customHeader = getattr(self, "customHeader", None)
        self.builtInHeader = getattr(self, "builtInHeader", None)
        self.toolOutputExpanded = bool(getattr(self, "toolOutputExpanded", False))
        self.hideThinkingBlock = bool(
            getattr(self, "hideThinkingBlock", _safe_call_bool(settingsManager, "getHideThinkingBlock"))
        )
        self.outputPad = int(getattr(self, "outputPad", _safe_call_int(settingsManager, "getOutputPad", 1)))
        self.defaultHiddenThinkingLabel = getattr(self, "defaultHiddenThinkingLabel", "Thinking...")
        self.hiddenThinkingLabel = getattr(self, "hiddenThinkingLabel", self.defaultHiddenThinkingLabel)
        self.entries = []
        self._toolComponentsById = {}

    def mount(self):
        for child in (
            self.headerContainer, self.chatContainer, self.pendingMessagesContainer, self.statusContainer,
            self.widgetContainerAbove, self.editorContainer, self.widgetContainerBelow, self.footer,
        ):
            self.ui.addChild(child)

    def _request_render(self, force: bool | None = None) -> None:
        request_render = getattr(self.ui, "requestRender", None)
        if callable(request_render):
            request_render() if force is None else request_render(force)

    def setToolsExpanded(self, expanded: bool) -> None:
        self.toolOutputExpanded = expanded
        for child in [self.customHeader or self.builtInHeader, *self.chatContainer.children]:
            set_expanded = getattr(child, "setExpanded", None)
            if callable(set_expanded):
                set_expanded(expanded)
        self._request_render()

    def toggleToolOutputExpansion(self) -> None:
        self.setToolsExpanded(not self.toolOutputExpanded)

    def toggleThinkingBlockVisibility(self) -> None:
        self.hideThinkingBlock = not self.hideThinkingBlock
        for child in self.chatContainer.children:
            if isinstance(child, AssistantMessageComponent):
                child.setHideThinkingBlock(self.hideThinkingBlock)
        self._request_render()

    def getMarkdownThemeWithSettings(self) -> Any:
        markdown_theme = get_markdown_theme()
        indent = _safe_call_str(self.settingsManager, "getCodeBlockIndent", "  ")
        try:
            return replace(markdown_theme, codeBlockIndent=indent)
        except TypeError:
            markdown_theme.codeBlockIndent = indent
            return markdown_theme

    def _transcriptRenderer(self) -> TranscriptRenderer:
        return TranscriptRenderer(
            self.ui, self.cwd,
            markdownTheme=self.getMarkdownThemeWithSettings(), outputPad=self.outputPad,
            hideThinkingBlock=self.hideThinkingBlock, hiddenThinkingLabel=self.hiddenThinkingLabel,
            expanded=self.toolOutputExpanded,
            showImages=_safe_call_bool(self.settingsManager, "getShowImages", True),
            imageWidthCells=_safe_call_int(self.settingsManager, "getImageWidthCells", 40),
        )

    def updateEntries(self, entries) -> None:
        """Follow committed context, preserving components unless its branch changed."""
        if entries[:len(self.entries)] != self.entries:
            self.entries = []
            self._toolComponentsById.clear()
            self.chatContainer.clear()
        self._transcriptRenderer().appendEntries(
            self.chatContainer, entries[len(self.entries):], self._toolComponentsById,
        )
        self.entries = entries
        self._request_render()
