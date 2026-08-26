"""Custom editor that intercepts coding-agent application keybindings."""

from __future__ import annotations

from collections.abc import Callable

from misaka.core.keybindings import AppKeybinding, KeybindingsManager
from misaka.ui.tui import TUI, Editor, EditorOptions, EditorTheme


class CustomEditor(Editor):
    """Editor wrapper that prioritizes app-level shortcuts over text editing."""

    def __init__(
        self,
        tui: TUI,
        theme: EditorTheme,
        keybindings: KeybindingsManager,
        options: EditorOptions | None = None,
    ) -> None:
        super().__init__(tui, theme, options)
        self.keybindings = keybindings
        self.actionHandlers: dict[AppKeybinding, Callable[[], None]] = {}
        self.onEscape: Callable[[], None] | None = None
        self.onCtrlD: Callable[[], None] | None = None
        self.onPasteImage: Callable[[], None] | None = None
        self.onExtensionShortcut: Callable[[str], bool] | None = None

    def onAction(self, action: AppKeybinding, handler: Callable[[], None]) -> None:
        self.actionHandlers[action] = handler

    def handleInput(self, data: str) -> None:
        if self.onExtensionShortcut is not None and self.onExtensionShortcut(data):
            return

        if self.keybindings.matches(data, "app.clipboard.pasteImage"):
            if self.onPasteImage is not None:
                self.onPasteImage()
            return

        if self.keybindings.matches(data, "app.interrupt"):
            if not self.isShowingAutocomplete():
                handler = self.onEscape or self.actionHandlers.get("app.interrupt")
                if handler is not None:
                    handler()
                    return
            super().handleInput(data)
            return

        if self.keybindings.matches(data, "app.exit"):
            if len(self.getText()) == 0:
                handler = self.onCtrlD or self.actionHandlers.get("app.exit")
                if handler is not None:
                    handler()
                return

        # Explicit history bindings take precedence over app actions while the editor is focused.
        # This lets users bind Ctrl+P even though it cycles models by default.  (pi 16ad96a)
        if (self.keybindings.matches(data, "tui.editor.historyPrevious")
                or self.keybindings.matches(data, "tui.editor.historyNext")):
            super().handleInput(data)
            return

        for action, handler in self.actionHandlers.items():
            if action in {"app.interrupt", "app.exit"}:
                continue
            if self.keybindings.matches(data, action):
                handler()
                return

        super().handleInput(data)


__all__ = ["CustomEditor"]
