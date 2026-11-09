"""Translated from pi's ``modes/interactive/tui-renderer.ts`` (v1.1.0).

MISAKA fork (D1): the fullscreen overload and branch (``TuiAltScreen`` with its search styles, jump-to-bottom label,
``openUrl``, right-click paste and selection copy), the ``tuiMode``/``onRightClickPaste``/``fullscreenCopyOnSelect``/
``fullscreenWheelScrollLines`` options, and ``createInteractiveTuiReference`` (a stable reference that only matters while
``switchTuiMode`` swaps renderers) are not ported. MISAKA has the regular main-screen renderer only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from misaka.ui.tui import ProcessTerminal
from misaka.ui.tui.tui_main_screen import TuiMainScreen


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveTuiOptions:
    showHardwareCursor: bool
    logDirectory: str
    terminal: Any = None


def createInteractiveTui(options: InteractiveTuiOptions) -> TuiMainScreen:
    """Composition root shared by coding-agent presentations."""
    terminal = options.terminal if options.terminal is not None else ProcessTerminal()
    return TuiMainScreen(terminal, options.showHardwareCursor, options.logDirectory)


__all__ = ["InteractiveTuiOptions", "createInteractiveTui"]
