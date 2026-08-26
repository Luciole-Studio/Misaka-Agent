"""Generic bordered multi-line editor used by interactive extension dialogs."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from misaka.ui.tui import Container, Editor, Spacer, Text, getKeybindings

from misaka.core.keybindings import KeybindingsManager
from misaka.ui.tui.interactive.theme.theme import get_editor_theme, theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint


async def edit_text_external(tui, text: str) -> str | None:
    """Edit text with $VISUAL/$EDITOR while safely handing the terminal over."""
    editor_cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor_cmd:
        return None

    temp_file = Path(tempfile.gettempdir()) / f"misaka-extension-editor-{int(time.time() * 1000)}.md"
    temp_file.write_text(text, encoding="utf-8")
    stop = getattr(tui, "stop", None)
    if callable(stop):
        stop()

    try:
        args = shlex.split(editor_cmd)
        if not args:
            return None
        sys.stdout.write(f"Launching external editor: {editor_cmd}\nMISAKA will resume when the editor exits.\n")
        process = await asyncio.create_subprocess_exec(*args, str(temp_file))
        if await process.wait() != 0:
            return None
        return temp_file.read_text(encoding="utf-8").removesuffix("\n")
    except OSError:
        return None
    finally:
        try:
            temp_file.unlink()
        except OSError:
            pass
        start = getattr(tui, "start", None)
        if callable(start):
            start()
        request_render = getattr(tui, "requestRender", None)
        if callable(request_render):
            request_render(True)


class ExtensionEditorComponent(Container):
    def __init__(
        self,
        tui,
        keybindings: KeybindingsManager,
        title: str,
        prefill: str | None,
        onSubmit: Callable[[str], None],
        onCancel: Callable[[], None],
        options=None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.tui = tui
        self.keybindings = keybindings
        self.onSubmitCallback = onSubmit
        self.onCancelCallback = onCancel

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("accent", title), 1, 0))
        self.addChild(Spacer(1))

        self.editor = Editor(tui, get_editor_theme(), options)
        if prefill:
            self.editor.setText(prefill)
        self.editor.onSubmit = lambda text: self.onSubmitCallback(text)
        self.addChild(self.editor)

        self.addChild(Spacer(1))
        has_external_editor = bool(os.environ.get("VISUAL") or os.environ.get("EDITOR"))
        hint = (
            key_hint("tui.select.confirm", "submit")
            + "  "
            + key_hint("tui.input.newLine", "newline")
            + "  "
            + key_hint("tui.select.cancel", "cancel")
        )
        if has_external_editor:
            hint += "  " + key_hint("app.editor.external", "external editor")
        self.addChild(Text(hint, 1, 0))
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.editor.focused = value

    def handleInput(self, data: str) -> None:
        if getKeybindings().matches(data, "tui.select.cancel"):
            self.onCancelCallback()
            return
        if self.keybindings.matches(data, "app.editor.external"):
            self._schedule_external_editor()
            return
        self.editor.handleInput(data)

    def _schedule_external_editor(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.openExternalEditor())

    async def openExternalEditor(self) -> None:
        new_content = await edit_text_external(self.tui, self.editor.getText())
        if new_content is not None:
            self.editor.setText(new_content)


__all__ = ["ExtensionEditorComponent", "edit_text_external"]
