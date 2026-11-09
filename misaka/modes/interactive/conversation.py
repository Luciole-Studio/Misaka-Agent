"""MISAKA fork: the conversation screen of the read-only/attach session viewer (``misaka chat --read-only/--attach``).

It opens saved messages without an agent or a writable SessionManager: the containers and transcript toggles of pi's
interactive mode, with ``TranscriptRenderer`` replaying the entries.
"""

from dataclasses import replace
from typing import Any

from misaka.ui.tui import Container

from .components.assistant_message import AssistantMessageComponent
from .components.transcript import TranscriptRenderer
from .theme.theme import getMarkdownTheme


class Conversation:
    def __init__(self, ui: Any, settingsManager: Any, cwd: str) -> None:
        self.ui = ui
        self.settingsManager = settingsManager
        self.cwd = cwd
        self.headerContainer = Container()
        self.chatContainer = Container()
        self.pendingMessagesContainer = Container()
        self.statusContainer = Container()
        self.widgetContainerAbove = Container()
        self.editorContainer = Container()
        self.widgetContainerBelow = Container()
        self.footer: Any = None
        self.customHeader: Any = None
        self.builtInHeader: Any = None
        self.toolOutputExpanded = False
        self.hideThinkingBlock = settingsManager.getHideThinkingBlock()
        self.outputPad = settingsManager.getOutputPad()
        self.defaultHiddenThinkingLabel = "Thinking..."
        self.hiddenThinkingLabel = self.defaultHiddenThinkingLabel
        self.entries: list[Any] = []
        self._toolComponentsById: dict[str, Any] = {}
        self._liveComponent: AssistantMessageComponent | None = None

    def mount(self) -> None:
        for child in (
            self.headerContainer,
            self.chatContainer,
            self.pendingMessagesContainer,
            self.statusContainer,
            self.widgetContainerAbove,
            self.editorContainer,
            self.widgetContainerBelow,
            self.footer,
        ):
            self.ui.addChild(child)

    def setToolsExpanded(self, expanded: bool) -> None:
        if expanded == self.toolOutputExpanded:
            return
        self.toolOutputExpanded = expanded
        for child in [self.customHeader or self.builtInHeader, *self.chatContainer.children]:
            setExpanded = getattr(child, "setExpanded", None)
            if callable(setExpanded):
                setExpanded(expanded)
        self.ui.requestRender()

    def toggleToolOutputExpansion(self) -> None:
        self.setToolsExpanded(not self.toolOutputExpanded)

    def toggleThinkingBlockVisibility(self) -> None:
        self.hideThinkingBlock = not self.hideThinkingBlock
        for child in self.chatContainer.children:
            if isinstance(child, AssistantMessageComponent):
                child.setHideThinkingBlock(self.hideThinkingBlock)
        self.ui.requestRender()

    def getMarkdownThemeWithSettings(self) -> Any:
        return replace(getMarkdownTheme(), codeBlockIndent=self.settingsManager.getCodeBlockIndent())

    def _transcriptRenderer(self) -> TranscriptRenderer:
        return TranscriptRenderer(
            self.ui,
            self.cwd,
            markdownTheme=self.getMarkdownThemeWithSettings(),
            outputPad=self.outputPad,
            hideThinkingBlock=self.hideThinkingBlock,
            hiddenThinkingLabel=self.hiddenThinkingLabel,
            expanded=self.toolOutputExpanded,
            showImages=self.settingsManager.getShowImages(),
            imageWidthCells=self.settingsManager.getImageWidthCells(),
        )

    def updateEntries(self, entries: list[Any]) -> None:
        """Follow committed context, preserving components unless its branch changed."""
        self.updateStreaming(None)
        if entries[: len(self.entries)] != self.entries:
            self.entries = []
            self._toolComponentsById.clear()
            self.chatContainer.clear()
        self._transcriptRenderer().appendEntries(
            self.chatContainer,
            entries[len(self.entries) :],
            self._toolComponentsById,
        )
        self.entries = entries
        self.ui.requestRender()

    def updateStreaming(self, message: Any) -> None:
        """Render an owner's transient assistant without adding it to saved entries."""
        if message is None:
            if self._liveComponent is not None:
                self.chatContainer.removeChild(self._liveComponent)
                self._liveComponent = None
                self.ui.requestRender()
            return
        if self._liveComponent is None:
            self._liveComponent = AssistantMessageComponent(
                hideThinkingBlock=self.hideThinkingBlock,
                markdownTheme=self.getMarkdownThemeWithSettings(),
                hiddenThinkingLabel=self.hiddenThinkingLabel,
                outputPad=self.outputPad,
            )
            self.chatContainer.addChild(self._liveComponent)
        self._liveComponent.updateContent(message, True)
        self.ui.requestRender()
