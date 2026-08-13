"""Generic bordered multi-line editor used by interactive extension dialogs."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from misaka.tui import Container, Editor, Spacer, Text, getKeybindings

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.theme.theme import get_editor_theme, theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint


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
        editor_cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor_cmd:
            return

        current_text = self.editor.getText()
        temp_file = Path(tempfile.gettempdir()) / f"harn-extension-editor-{int(time.time() * 1000)}.md"
        temp_file.write_text(current_text, encoding="utf-8")

        stop = getattr(self.tui, "stop", None)
        if callable(stop):
            stop()

        try:
            args = editor_cmd.split(" ")
            if not args or not args[0]:
                return
            editor, *editor_args = args
            sys.stdout.write(
                f"Launching external editor: {editor_cmd}\nHarn will resume when the editor exits.\n"
            )

            status: int | None
            if sys.platform == "win32":
                command = " ".join([editor, *editor_args, str(temp_file)])
                try:
                    process = await asyncio.create_subprocess_shell(command)
                except OSError:
                    status = None
                else:
                    status = await process.wait()
            else:
                try:
                    process = await asyncio.create_subprocess_exec(editor, *editor_args, str(temp_file))
                except OSError:
                    status = None
                else:
                    status = await process.wait()
            if status == 0:
                new_content = temp_file.read_text(encoding="utf-8").removesuffix("\n")
                self.editor.setText(new_content)
        finally:
            try:
                temp_file.unlink()
            except OSError:
                pass
            start = getattr(self.tui, "start", None)
            if callable(start):
                start()
            request_render = getattr(self.tui, "requestRender", None)
            if callable(request_render):
                request_render(True)


__all__ = ["ExtensionEditorComponent"]
