"""Translated from pi's ``modes/interactive/components/extension-editor.ts`` (v1.1.0).

Multi-line editor component for extensions.
Supports Ctrl+G for external editor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.external_editor import editInExternalEditor
from misaka.modes.interactive.theme.theme import getEditorTheme, theme
from misaka.ui.tui import (
    TUI,
    Container,
    Editor,
    EditorOptions,
    Spacer,
    Text,
    getKeybindings,
)
from misaka.utils.js import spawn

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint


@dataclass(slots=True)
class ExtensionEditorOptions(EditorOptions):
    description: str | None = None


class ExtensionEditorComponent(Container):
    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.editor.focused = value

    def __init__(
        self,
        tui: TUI,
        keybindings: KeybindingsManager,
        title: str,
        prefill: str | None,
        onSubmit: Callable[[str], None],
        onCancel: Callable[[], None],
        options: EditorOptions | None = None,
        externalEditorCommand: str | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        # MISAKA fork: holds the running `void this.handleOpenExternalEditor()` (asyncio holds tasks weakly).
        self._tasks: set[asyncio.Task[Any]] = set()

        self.tui = tui
        self.keybindings = keybindings
        self.externalEditorCommand: str = (
            externalEditorCommand
            or os.environ.get("VISUAL")
            or os.environ.get("EDITOR")
            or ("notepad" if sys.platform == "win32" else "nano")
        )
        self.onSubmitCallback = onSubmit
        self.onCancelCallback = onCancel
        description = options.description if isinstance(options, ExtensionEditorOptions) else None
        editorOptions = (
            EditorOptions(paddingX=options.paddingX, autocompleteMaxVisible=options.autocompleteMaxVisible)
            if options is not None
            else EditorOptions()
        )

        # Add top border
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        # Add title and optional description
        self.addChild(Text(theme.fg("accent", title), 1, 0))
        if description:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("text", description), 1, 0))
        self.addChild(Spacer(1))

        # Create editor
        self.editor = Editor(tui, getEditorTheme(), editorOptions)
        if prefill:
            self.editor.setText(prefill)

        # Wire up Enter to submit (Shift+Enter for newlines, like the main editor)
        def onEditorSubmit(text: str) -> None:
            self.onSubmitCallback(text)

        self.editor.onSubmit = onEditorSubmit
        self.addChild(self.editor)

        self.addChild(Spacer(1))

        # Add hint
        hint = (
            keyHint("tui.select.confirm", "submit")
            + "  "
            + keyHint("tui.input.newLine", "newline")
            + "  "
            + keyHint("tui.select.cancel", "cancel")
            + f"  {keyHint('app.editor.external', 'external editor')}"
        )
        self.addChild(Text(hint, 1, 0))

        self.addChild(Spacer(1))

        # Add bottom border
        self.addChild(DynamicBorder())

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        # Escape or Ctrl+C to cancel
        if kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()
            return

        # External editor (app keybinding)
        if self.keybindings.matches(keyData, "app.editor.external"):
            spawn(self.handleOpenExternalEditor(), self._tasks)
            return

        # Forward to editor
        self.editor.handleInput(keyData)

    async def handleOpenExternalEditor(self) -> None:
        content = self.editor.getText()
        self.tui.stop()
        try:
            result = await editInExternalEditor({"command": self.externalEditorCommand, "content": content})
            if result["status"] == "complete":
                self.editor.setText(result["content"])
        finally:
            self.tui.start()
            self.tui.requestRender(True)


__all__ = ["ExtensionEditorComponent", "ExtensionEditorOptions"]
