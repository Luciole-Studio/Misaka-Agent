"""Translated from pi's ``modes/interactive/components/custom-editor.ts`` (v1.1.0)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from misaka.core.keybindings import AppKeybinding, KeybindingsManager
from misaka.ui.tui import TUI, Editor, EditorOptions, EditorTheme, visibleWidth

if TYPE_CHECKING:
    from .status_indicator import StatusIndicator


@dataclass(slots=True)
class CustomEditorOptions(EditorOptions):
    # Render working, compaction, summarization, and retry status in the editor's top border.
    embedWorkingStatus: bool | None = None


class CustomEditor(Editor):
    """Custom editor that handles app-level keybindings for coding-agent."""

    def __init__(
        self,
        tui: TUI,
        theme: EditorTheme,
        keybindings: KeybindingsManager,
        options: EditorOptions | None = None,
    ) -> None:
        super().__init__(tui, theme, options)
        self.workingStatusIndicator: StatusIndicator | None = None
        self.actionHandlers: dict[AppKeybinding, Callable[[], None]] = {}

        # Special handlers that can be dynamically replaced
        self.onEscape: Callable[[], None] | None = None
        self.onCtrlD: Callable[[], None] | None = None
        self.onPasteImage: Callable[[], None] | None = None
        # Handler for extension-registered shortcuts. Returns true if handled.
        self.onExtensionShortcut: Callable[[str], bool] | None = None

        self.keybindings = keybindings
        embedWorkingStatus = options.embedWorkingStatus if isinstance(options, CustomEditorOptions) else None
        self.embedWorkingStatus: bool = embedWorkingStatus if embedWorkingStatus is not None else False

    def setWorkingStatusIndicator(self, indicator: StatusIndicator | None) -> None:
        self.workingStatusIndicator = indicator

    def renderTopBorder(self, width: int, hiddenLineCount: int) -> str:
        if not self.embedWorkingStatus or not self.workingStatusIndicator or width <= 0:
            return super().renderTopBorder(width, hiddenLineCount)

        status = self.workingStatusIndicator.renderInBorder(max(1, width - 5))
        statusWidth = visibleWidth(status)
        if statusWidth == 0:
            return super().renderTopBorder(width, hiddenLineCount)

        overflowLabel = f" ↑ {hiddenLineCount} more " if hiddenLineCount > 0 else None
        overflowLabelWidth = visibleWidth(overflowLabel) if overflowLabel else 0
        overflowStart = math.floor((width - overflowLabelWidth) / 2)

        def canFitOverflow() -> bool:
            return (
                overflowLabel is not None
                and overflowLabelWidth + 2 <= width
                and overflowStart - (3 + statusWidth + 1) >= 1
            )

        if overflowLabel and not canFitOverflow():
            status = self.workingStatusIndicator.renderSpinnerInBorder(width)
            statusWidth = visibleWidth(status)

        if canFitOverflow():
            leftBlockWidth = 3 + statusWidth + 1
            return (
                self.borderColor("── ")
                + status
                + self.borderColor(
                    f" {'─' * (overflowStart - leftBlockWidth)}{overflowLabel}"
                    f"{'─' * (width - overflowStart - overflowLabelWidth)}"
                )
            )

        if width >= statusWidth + 5:
            return self.borderColor("── ") + status + self.borderColor(f" {'─' * (width - statusWidth - 4)}")

        status = self.workingStatusIndicator.renderSpinnerInBorder(width)
        statusWidth = visibleWidth(status)
        prefixWidth = min(3, max(0, width - statusWidth))
        return (
            self.borderColor("─" * prefixWidth)
            + status
            + self.borderColor("─" * max(0, width - prefixWidth - statusWidth))
        )

    def onAction(self, action: AppKeybinding, handler: Callable[[], None]) -> None:
        """Register a handler for an app action."""
        self.actionHandlers[action] = handler

    def handleInput(self, data: str) -> None:
        # Check extension-registered shortcuts first
        if self.onExtensionShortcut is not None and self.onExtensionShortcut(data):
            return

        # Check for clipboard paste keybinding
        if self.keybindings.matches(data, "app.clipboard.pasteImage"):
            if self.onPasteImage is not None:
                self.onPasteImage()
            return

        # Check app keybindings first

        # Escape/interrupt - only if autocomplete is NOT active
        if self.keybindings.matches(data, "app.interrupt"):
            if not self.isShowingAutocomplete():
                # Use dynamic onEscape if set, otherwise registered handler
                handler = self.onEscape if self.onEscape is not None else self.actionHandlers.get("app.interrupt")
                if handler:
                    handler()
                    return
            # Let parent handle escape for autocomplete cancellation
            super().handleInput(data)
            return

        # Exit (Ctrl+D) - only when editor is empty
        if self.keybindings.matches(data, "app.exit"):  # noqa: SIM102 - upstream keeps the fall-through comment inside
            if len(self.getText()) == 0:
                handler = self.onCtrlD if self.onCtrlD is not None else self.actionHandlers.get("app.exit")
                if handler:
                    handler()
                return
            # Fall through to editor handling for delete-char-forward when not empty

        # Explicit history bindings take precedence over app actions while the editor is focused.
        # This lets users bind Ctrl+P even though it cycles models by default.
        if self.keybindings.matches(data, "tui.editor.historyPrevious") or self.keybindings.matches(
            data, "tui.editor.historyNext"
        ):
            super().handleInput(data)
            return

        # Check all other app actions
        for action, handler in list(self.actionHandlers.items()):
            if action != "app.interrupt" and action != "app.exit" and self.keybindings.matches(data, action):
                handler()
                return

        # Pass to parent for editor handling
        super().handleInput(data)


__all__ = ["CustomEditor", "CustomEditorOptions"]
