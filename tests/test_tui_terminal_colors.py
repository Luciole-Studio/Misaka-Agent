"""Translated from pi-tui's ``test/terminal-colors.test.ts`` (v1.3.0).

MISAKA fork (D11): the ``TUI.queryTerminalColors`` cases belong to pi's system theme, which is not ported. The delayed
Ghostty fragment case runs against MISAKA's OSC 11 probe (``queryTerminalBackgroundColor``) in its place; ``t.mock.timers``
is the editor tests' patched ``loop.call_later`` (``mockTimers``).
"""

import asyncio

from misaka.ui.tui.stdin_buffer import StdinBuffer
from misaka.ui.tui.terminal_colors import (
    RgbColor,
    parseOscColorResponse,
    parseTerminalColorSchemeReport,
)
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.test_tui_editor import mockTimers
from tests.tui_virtual_terminal import VirtualTerminal

# --- parseTerminalColorSchemeReport ---------------------------------------------------------------


def test_parses_color_scheme_reports():
    assert parseTerminalColorSchemeReport("\x1b[?997;1n") == "dark"
    assert parseTerminalColorSchemeReport("\x1b[?997;2n") == "light"
    assert parseTerminalColorSchemeReport("\x1b[?997;2n\x1b[?997;1n\x1b[?997;1n") == "dark"
    assert parseTerminalColorSchemeReport("\x1b[?997;1n\x1b[?997;2n\x1b[?997;2n") == "light"
    assert parseTerminalColorSchemeReport("\x1b[?997;3n") is None
    assert parseTerminalColorSchemeReport("\x1b[?996n") is None
    assert parseTerminalColorSchemeReport("x\x1b[?997;1n") is None


# --- parseOscColorResponse ------------------------------------------------------------------------


def test_parses_osc_10_11_and_4_replies():
    assert parseOscColorResponse("\x1b]10;rgb:ffff/ffff/ffff\x07") == {
        "target": "foreground",
        "rgb": RgbColor(r=255, g=255, b=255),
    }
    assert parseOscColorResponse("\x1b]4;13;#ff0080\x1b\\") == {
        "target": 13,
        "rgb": RgbColor(r=255, g=0, b=128),
    }
    assert parseOscColorResponse("\x1b]4;1;bogus\x07") == {"target": 1, "rgb": None}
    assert parseOscColorResponse("\x1b]12;#ffffff\x07") is None


# --- TUI.queryTerminalBackgroundColor (MISAKA fork D11, in place of TUI.queryTerminalColors) -----------------


class InputRecorder:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def render(self, _width: int) -> list[str]:
        return []

    def handleInput(self, data: str) -> None:
        self.inputs.append(data)

    def invalidate(self) -> None:
        pass


def setup() -> tuple[VirtualTerminal, TuiMainScreen, InputRecorder]:
    terminal = VirtualTerminal()
    tui = TuiMainScreen(terminal)
    component = InputRecorder()
    tui.addChild(component)
    tui.setFocus(component)
    tui.start()
    return terminal, tui, component


async def test_consumes_delayed_ghostty_color_fragments_without_typing_their_tail_or_opening_the_editor():
    with mockTimers() as timers:
        terminal, tui, component = setup()
        buffer = StdinBuffer()
        buffer.on("data", terminal.sendInput)
        try:
            query = asyncio.ensure_future(tui.queryTerminalBackgroundColor(timeoutMs=1000))
            await asyncio.sleep(0)
            # Ghostty 1.3.1's Ayu Light colour, split as in the reports: MISAKA asks for OSC 11 with a BEL terminator.
            buffer.process("\x1b]11;rgb:eaea/6c")
            timers.tick(100)
            buffer.process("6c/6d6d\x07")
            timers.tick(1000)  # the probe's own timeout: a lost reply resolves None instead of hanging the test
            assert await query == RgbColor(r=234, g=108, b=109)
            assert component.inputs == []

            # A real Ctrl+G (the external editor, app.editor.external) must still reach the editor after the reply.
            buffer.process("\x07")
            assert component.inputs == ["\x07"]
        finally:
            buffer.destroy()
            tui.stop()


# --- MISAKA ---------------------------------------------------------------------------------------


def test_a_reply_with_a_trailing_newline_is_not_a_reply():
    # `$` is the end of the input, as in JavaScript.
    assert parseTerminalColorSchemeReport("\x1b[?997;2n\n") is None
    assert parseOscColorResponse("\x1b]11;#ffffff\x07\n") is None
