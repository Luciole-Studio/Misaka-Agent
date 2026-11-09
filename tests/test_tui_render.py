"""Translated from pi-tui's ``test/tui-render.test.ts``, ``test/tui-shrink.test.ts``, ``test/tui-cell-size-input.test.ts``,
``test/tui-overlay-style-leak.test.ts``, ``test/overlay-short-content.test.ts``, the ``compositeTuiLine`` cases of
``test/regression-overlay-cjk-boundary.test.ts`` and the TUI case of ``test/tab-width.test.ts`` (v1.3.0).

MISAKA forks: the redraw log is switched by ``MISAKA_TUI_DEBUG_REDRAW`` (pi: ``PI_TUI_DEBUG_REDRAW``), and the crash dump
goes to the given log directory or else MISAKA's home (pi: the OS temp directory). The test terminal shows and hides
its cursor through ``write`` (pi's writes to the emulator directly), so the fake cursor cases read the writes without
that trailing ``\x1b[?25h``/``\x1b[?25l``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from misaka.config import home
from misaka.ui.tui.components.editor import Editor
from misaka.ui.tui.components.image import Image, ImageOptions, ImageTheme
from misaka.ui.tui.components.input import Input
from misaka.ui.tui.terminal_image import (
    CellDimensions,
    ImageDimensions,
    TerminalCapabilities,
    deleteKittyImage,
    encodeKitty,
    getCellDimensions,
    resetCapabilitiesCache,
    setCapabilities,
    setCellDimensions,
)
from misaka.ui.tui.tui import CURSOR_MARKER, TUI, compositeTuiLine, renderFakeCursor
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from misaka.ui.tui.utils import extractSegments, sliceByColumn, visibleWidth
from tests.tui_test_themes import defaultEditorTheme
from tests.tui_virtual_terminal import VirtualTerminal


class TestComponent:
    __test__ = False

    def __init__(self) -> None:
        self.lines: list[str] = []

    def render(self, _width: int) -> list[str]:
        return self.lines

    def invalidate(self) -> None:
        pass


class InputComponent(TestComponent):
    def __init__(self) -> None:
        super().__init__()
        self.renderCount = 0

    def render(self, width: int) -> list[str]:
        self.renderCount += 1
        return super().render(width)

    def handleInput(self, data: str) -> None:
        self.lines = [data]


MAX_RENDER_WRITE_CHARS = 1024 * 1024


class BoundedWriteTerminal:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.columns = 80
        self.rows = 24
        self.kittyProtocolActive = False

    def start(self, _onInput, _onResize) -> None:
        pass

    def stop(self) -> None:
        pass

    async def drainInput(self, _maxMs=None, _idleMs=None) -> None:
        pass

    def write(self, data: str) -> None:
        self.writes.append(data)

    def moveBy(self, _lines: int) -> None:
        pass

    def hideCursor(self) -> None:
        pass

    def showCursor(self) -> None:
        pass

    def clearLine(self) -> None:
        pass

    def clearFromCursor(self) -> None:
        pass

    def clearScreen(self) -> None:
        pass

    def setTitle(self, _title: str) -> None:
        pass

    def setProgress(self, _active: bool) -> None:
        pass

    def setProgramStatus(self, _status) -> None:
        pass


class LoggingVirtualTerminal(VirtualTerminal):
    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        super().__init__(columns, rows)
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)
        super().write(data)

    def getWrites(self) -> str:
        return "".join(self.writes)

    def clearWrites(self) -> None:
        self.writes = []


@pytest.fixture(autouse=True)
def _restoreImageState():
    yield
    resetCapabilitiesCache()
    setCellDimensions(CellDimensions(widthPx=9, heightPx=18))


def kitty() -> None:
    setCapabilities(TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})


def plainImage(maxWidthCells: int, sizePx: int) -> Image:
    return Image(
        "AAAA",
        "image/png",
        ImageTheme(fallbackColor=lambda value: value),
        ImageOptions(maxWidthCells=maxWidthCells),
        ImageDimensions(widthPx=sizePx, heightPx=sizePx),
    )


# --- TUI render scheduling ------------------------------------------------------------------------


async def test_renders_keyboard_input_without_waiting_for_a_throttled_frame():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = InputComponent()
    component.lines = ["initial"]
    tui.addChild(component)
    tui.setFocus(component)
    tui.start()
    tui.renderNow()
    renderCountBeforeInput = component.renderCount

    # Queue a normal throttled render first. Keyboard input should preempt it.
    component.lines = ["pending"]
    tui.requestRender()
    terminal.sendInput("first")
    terminal.sendInput("second")
    terminal.sendInput("typed")
    await asyncio.sleep(0)

    assert component.renderCount == renderCountBeforeInput + 1
    assert component.lines == ["typed"]
    tui.stop()


# --- TUI debug logging --------------------------------------------------------------------------


async def test_writes_redraw_logs_to_the_provided_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_TUI_DEBUG_REDRAW", "1")
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal, None, str(tmp_path))
    component = TestComponent()
    tui.addChild(component)
    component.lines = ["test"]
    tui.start()
    await terminal.waitForRender()

    assert "fullRender: first render" in (tmp_path / "pi-tui-debug.log").read_text(encoding="utf-8")
    tui.stop()


async def test_the_debug_key_calls_on_debug():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = InputComponent()
    tui.addChild(component)
    tui.setFocus(component)
    calls: list[None] = []
    tui.onDebug = lambda: calls.append(None)
    tui.start()

    terminal.sendInput("\x1b[100;6u")  # Shift+Ctrl+D (Kitty)
    await asyncio.sleep(0)

    assert calls == [None]
    tui.stop()


# --- TUI bounded render output --------------------------------------------------------------------


def test_splits_a_large_full_render_without_changing_its_output():
    terminal = BoundedWriteTerminal()
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    kittyLine = f"\x1b_Ga=T,f=100;{'A' * 1_200_000}\x1b\\"
    component.lines = [kittyLine, kittyLine]
    tui.addChild(component)

    tui.renderNow()

    assert len(terminal.writes) > 2, "large output should be split across terminal writes"
    assert all(len(write) <= MAX_RENDER_WRITE_CHARS for write in terminal.writes), (
        "each terminal write should stay below the configured limit"
    )
    assert "".join(terminal.writes) == f"\x1b[?2026h{kittyLine}\r\n{kittyLine}\x1b[?2026l", (
        "chunking must preserve the synchronized render output"
    )


def test_splits_large_differential_updates_without_a_full_redraw():
    terminal = BoundedWriteTerminal()
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)
    component.lines = ["before"]
    tui.renderNow()
    terminal.writes.clear()

    kittyLine = f"\x1b_Ga=T,f=100;{'A' * 1_200_000}\x1b\\"
    component.lines = ["before", kittyLine, kittyLine]
    tui.renderNow()

    assert len(terminal.writes) > 2, "large output should be split across terminal writes"
    assert all(len(write) <= MAX_RENDER_WRITE_CHARS for write in terminal.writes)
    output = "".join(terminal.writes)
    assert output.startswith("\x1b[?2026h")
    assert output.endswith("\x1b[?2026l")
    assert "\x1b[2J" not in output, "the update should stay on the differential render path"


# --- TUI crash dump -------------------------------------------------------------------------------


async def test_writes_the_crash_dump_to_the_log_directory(tmp_path):
    crashLogPath = os.path.join(str(tmp_path), "pi-tui-crash.log")
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal, None, str(tmp_path))
    component = TestComponent()
    tui.addChild(component)
    component.lines = ["ok"]
    tui.start()
    await terminal.waitForRender()

    # Width overflow is detected in the differential render path
    component.lines = ["ok", "x" * 60]
    with pytest.raises(RuntimeError) as raised:
        tui.renderNow()
    assert crashLogPath in str(raised.value), f"error message should reference {crashLogPath}"
    assert "Terminal width: 40" in Path(crashLogPath).read_text(encoding="utf-8")


# MISAKA fork: without a log directory the dump goes to MISAKA's home, not the OS temp directory.
async def test_writes_the_crash_dump_to_misakas_home_without_a_log_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)
    component.lines = ["ok"]
    tui.start()
    await terminal.waitForRender()

    component.lines = ["ok", "x" * 60]
    with pytest.raises(RuntimeError) as raised:
        tui.renderNow()
    assert str(home.path("crash_log")) in str(raised.value)
    assert "Terminal width: 40" in home.path("crash_log").read_text(encoding="utf-8")


# --- TUI Kitty image cleanup ----------------------------------------------------------------------


async def test_clears_reserved_kitty_image_rows_before_drawing_appended_image_placements():
    kitty()
    terminal = LoggingVirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["before"]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    image = plainImage(2, 20)
    imageLines = image.render(40)
    imageSequence = imageLines[0]
    component.lines = ["before", *imageLines, "after"]
    tui.requestRender()
    await terminal.waitForRender()

    writes = terminal.getWrites()
    assert f"\x1b[2K\r\n\x1b[2K\x1b[1A{imageSequence}\x1b[1B" in writes, (
        "reserved rows should be cleared before the image placement is drawn"
    )
    assert f"{imageSequence}\r\n\x1b[2K" not in writes, "reserved row clears must not run after the image placement is drawn"

    tui.stop()


async def test_falls_back_to_full_redraw_when_kitty_image_pre_clear_would_scroll():
    kitty()
    terminal = LoggingVirtualTerminal(40, 2)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["before"]
    tui.start()
    await terminal.waitForRender()
    redrawsBeforeImage = tui.fullRedraws
    terminal.clearWrites()

    image = plainImage(3, 30)
    component.lines = ["before", *image.render(40), "after"]
    tui.requestRender()
    await terminal.waitForRender()

    assert tui.fullRedraws > redrawsBeforeImage, "unsafe image pre-clear should force a full redraw"
    assert "\x1b[2J" in terminal.getWrites(), "fallback should clear and fully redraw"

    tui.stop()


async def test_reserves_kitty_image_rows_before_drawing_during_full_redraw_fallbacks():
    kitty()
    terminal = LoggingVirtualTerminal(40, 5)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["l0", "l1", "l2", "l3", "l4"]
    tui.start()
    await terminal.waitForRender()
    redrawsBeforeImage = tui.fullRedraws
    terminal.clearWrites()

    image = plainImage(3, 30)
    imageLines = image.render(40)
    imageSequence = imageLines[0]
    component.lines = ["l0", "l1", "l2", "l3", "l4", *imageLines, "after"]
    tui.requestRender()
    await terminal.waitForRender()

    writes = terminal.getWrites()
    assert tui.fullRedraws > redrawsBeforeImage, "scrolling image append should force a full redraw"
    assert f"\r\n\r\n\x1b[2A{imageSequence}\x1b[2B" in writes, (
        "full redraw should reserve visible image rows before drawing the placement"
    )
    assert f"{imageSequence}\r\n\x1b[0m" not in writes, (
        "full redraw must not write reserved padding rows after drawing the placement"
    )

    tui.stop()


async def test_does_not_use_cursor_up_placement_for_kitty_images_taller_than_the_viewport():
    kitty()
    terminal = LoggingVirtualTerminal(40, 5)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["before"]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    image = plainImage(6, 60)
    imageLines = image.render(40)
    imageSequence = imageLines[0]
    assert len(imageLines) > terminal.rows, "test image should exceed the viewport height"

    component.lines = ["before", *imageLines, "after"]
    tui.requestRender(True)
    await terminal.waitForRender()

    writes = terminal.getWrites()
    assert imageSequence in writes, "image placement should be drawn"
    assert f"\x1b[{len(imageLines) - 1}A{imageSequence}" not in writes, (
        "taller-than-viewport images must keep the #4461 first-row placement path"
    )

    tui.stop()


async def test_deletes_changed_image_ids_before_drawing_moved_placements():
    terminal = LoggingVirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    oldImage = encodeKitty("AAAA", {"columns": 2, "rows": 2, "imageId": 42, "moveCursor": False})
    component.lines = ["top", oldImage]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    newImage = encodeKitty("BBBB", {"columns": 2, "rows": 1, "imageId": 42, "moveCursor": False})
    component.lines = [newImage, ""]
    tui.requestRender()
    await terminal.waitForRender()

    writes = terminal.getWrites()
    deleteIndex = writes.find(deleteKittyImage(42))
    drawIndex = writes.find(newImage)
    assert deleteIndex >= 0, "changed old image should be deleted"
    assert drawIndex >= 0, "new image should be drawn"
    assert deleteIndex < drawIndex, "old image must be deleted before the new placement is drawn"

    tui.stop()


async def test_redraws_image_lines_when_an_earlier_reserved_image_row_changes():
    terminal = LoggingVirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    image = encodeKitty("AAAA", {"columns": 2, "rows": 2, "imageId": 88, "moveCursor": False})
    component.lines = ["", image]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    component.lines = ["covered", image]
    tui.requestRender()
    await terminal.waitForRender()

    writes = terminal.getWrites()
    deleteIndex = writes.find(deleteKittyImage(88))
    drawIndex = writes.find(image)
    assert deleteIndex >= 0, "image should be deleted when a reserved row changes"
    assert drawIndex >= 0, "unchanged image line should be redrawn after deleting the placement"
    assert deleteIndex < drawIndex, "old placement must be deleted before the image line is redrawn"
    assert "\x1b[2J" not in writes, "reserved row changes should not force a full redraw"

    tui.stop()


async def test_deletes_previously_rendered_image_ids_during_full_redraws():
    terminal = LoggingVirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = [encodeKitty("AAAA", {"columns": 2, "rows": 2, "imageId": 77, "moveCursor": False})]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    component.lines = ["plain text"]
    tui.requestRender(True)
    await terminal.waitForRender()

    writes = terminal.getWrites()
    deleteIndex = writes.find(deleteKittyImage(77))
    clearIndex = writes.find("\x1b[2J")
    assert deleteIndex >= 0, "previous image should be deleted during full redraw"
    assert clearIndex >= 0, "full redraw should clear the screen"
    assert deleteIndex < clearIndex, "old image should be deleted before the screen is cleared"

    tui.stop()


# --- TUI resize handling --------------------------------------------------------------------------


async def waitForRenders(terminal: VirtualTerminal) -> None:
    """MISAKA fork: a resize draws a fast frame and then the full one (see `TuiMainScreen._try_viewport_render`)."""
    await terminal.waitForRender()
    await terminal.waitForRender()


async def test_triggers_full_re_render_when_terminal_height_changes(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.start()
    await terminal.waitForRender()

    initialRedraws = tui.fullRedraws

    # Resize height
    terminal.resize(40, 15)
    await waitForRenders(terminal)

    # Should have triggered a full redraw
    assert tui.fullRedraws > initialRedraws, "Height change should trigger full redraw"

    viewport = terminal.getViewport()
    assert "Line 0" in viewport[0], "Content preserved after height change"

    tui.stop()


async def test_skips_full_re_render_on_height_changes_in_termux(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "1")
    terminal = LoggingVirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = [f"Line {i}" for i in range(20)]
    tui.start()
    await terminal.waitForRender()
    terminal.clearWrites()

    initialRedraws = tui.fullRedraws
    for height in [15, 8, 14, 11]:
        terminal.resize(40, height)
        await terminal.waitForRender()

    assert tui.fullRedraws == initialRedraws, "Height change should not trigger full redraw"
    assert "\x1b[2J" not in terminal.getWrites(), "Height change should not clear the screen"
    assert "\x1b[3J" not in terminal.getWrites(), "Height change should not clear scrollback"

    viewport = terminal.getViewport()
    assert "Line 19" in "\n".join(viewport), "Latest content remains visible after resize"

    tui.stop()


async def test_triggers_full_re_render_when_terminal_width_changes():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.start()
    await terminal.waitForRender()

    initialRedraws = tui.fullRedraws

    # Resize width
    terminal.resize(60, 10)
    await waitForRenders(terminal)

    # Should have triggered a full redraw
    assert tui.fullRedraws > initialRedraws, "Width change should trigger full redraw"

    tui.stop()


# --- TUI content shrinkage ------------------------------------------------------------------------


async def test_clears_empty_rows_when_content_shrinks_significantly():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    tui.setClearOnShrink(True)  # Explicitly enable (may be disabled via env var)
    component = TestComponent()
    tui.addChild(component)

    # Start with many lines
    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3", "Line 4", "Line 5"]
    tui.start()
    await terminal.waitForRender()

    initialRedraws = tui.fullRedraws

    # Shrink to fewer lines
    component.lines = ["Line 0", "Line 1"]
    tui.requestRender()
    await terminal.waitForRender()

    # Should have triggered a full redraw to clear empty rows
    assert tui.fullRedraws > initialRedraws, "Content shrinkage should trigger full redraw"

    viewport = terminal.getViewport()
    assert "Line 0" in viewport[0], "First line preserved"
    assert "Line 1" in viewport[1], "Second line preserved"
    # Lines below should be empty (cleared)
    assert viewport[2].strip() == "", "Line 2 should be cleared"
    assert viewport[3].strip() == "", "Line 3 should be cleared"

    tui.stop()


async def test_handles_shrink_to_single_line():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    tui.setClearOnShrink(True)  # Explicitly enable (may be disabled via env var)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3"]
    tui.start()
    await terminal.waitForRender()

    # Shrink to single line
    component.lines = ["Only line"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "Only line" in viewport[0], "Single line rendered"
    assert viewport[1].strip() == "", "Line 1 should be cleared"

    tui.stop()


async def test_handles_shrink_to_empty():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    tui.setClearOnShrink(True)  # Explicitly enable (may be disabled via env var)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.start()
    await terminal.waitForRender()

    # Shrink to empty
    component.lines = []
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    # All lines should be empty
    assert viewport[0].strip() == "", "Line 0 should be cleared"
    assert viewport[1].strip() == "", "Line 1 should be cleared"

    tui.stop()


# --- TUI differential rendering -------------------------------------------------------------------


async def test_tracks_cursor_correctly_when_content_shrinks_with_unchanged_remaining_lines():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    # Initial render: 5 identical lines
    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3", "Line 4"]
    tui.start()
    await terminal.waitForRender()

    # Shrink to 3 lines, all identical to before (no content changes in remaining lines)
    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.requestRender()
    await terminal.waitForRender()

    # cursorRow should be 2 (last line of new content)
    # Verify by doing another render with a change on line 1
    component.lines = ["Line 0", "CHANGED", "Line 2"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    # Line 1 should show "CHANGED", proving cursor tracking was correct
    assert "CHANGED" in viewport[1], f'Expected "CHANGED" on line 1, got: {viewport[1]}'

    tui.stop()


async def test_renders_correctly_when_only_a_middle_line_changes_spinner_case():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    # Initial render
    component.lines = ["Header", "Working...", "Footer"]
    tui.start()
    await terminal.waitForRender()

    # Simulate spinner animation - only middle line changes
    spinnerFrames = ["|", "/", "-", "\\"]
    for frame in spinnerFrames:
        component.lines = ["Header", f"Working {frame}", "Footer"]
        tui.requestRender()
        await terminal.waitForRender()

        viewport = terminal.getViewport()
        assert "Header" in viewport[0], f"Header preserved: {viewport[0]}"
        assert f"Working {frame}" in viewport[1], f"Spinner updated: {viewport[1]}"
        assert "Footer" in viewport[2], f"Footer preserved: {viewport[2]}"

    tui.stop()


async def test_resets_styles_after_each_rendered_line():
    terminal = VirtualTerminal(20, 6)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["\x1b[3mItalic", "Plain"]
    tui.start()
    await terminal.waitForRender()

    assert terminal.getCellItalic(1, 0) is False
    tui.stop()


async def test_renders_correctly_when_first_line_changes_but_rest_stays_same():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3"]
    tui.start()
    await terminal.waitForRender()

    # Change only first line
    component.lines = ["CHANGED", "Line 1", "Line 2", "Line 3"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "CHANGED" in viewport[0], f"First line changed: {viewport[0]}"
    assert "Line 1" in viewport[1], f"Line 1 preserved: {viewport[1]}"
    assert "Line 2" in viewport[2], f"Line 2 preserved: {viewport[2]}"
    assert "Line 3" in viewport[3], f"Line 3 preserved: {viewport[3]}"

    tui.stop()


async def test_renders_correctly_when_last_line_changes_but_rest_stays_same():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3"]
    tui.start()
    await terminal.waitForRender()

    # Change only last line
    component.lines = ["Line 0", "Line 1", "Line 2", "CHANGED"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "Line 0" in viewport[0], f"Line 0 preserved: {viewport[0]}"
    assert "Line 1" in viewport[1], f"Line 1 preserved: {viewport[1]}"
    assert "Line 2" in viewport[2], f"Line 2 preserved: {viewport[2]}"
    assert "CHANGED" in viewport[3], f"Last line changed: {viewport[3]}"

    tui.stop()


async def test_renders_correctly_when_multiple_non_adjacent_lines_change():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = ["Line 0", "Line 1", "Line 2", "Line 3", "Line 4"]
    tui.start()
    await terminal.waitForRender()

    # Change lines 1 and 3, keep 0, 2, 4 the same
    component.lines = ["Line 0", "CHANGED 1", "Line 2", "CHANGED 3", "Line 4"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "Line 0" in viewport[0], f"Line 0 preserved: {viewport[0]}"
    assert "CHANGED 1" in viewport[1], f"Line 1 changed: {viewport[1]}"
    assert "Line 2" in viewport[2], f"Line 2 preserved: {viewport[2]}"
    assert "CHANGED 3" in viewport[3], f"Line 3 changed: {viewport[3]}"
    assert "Line 4" in viewport[4], f"Line 4 preserved: {viewport[4]}"

    tui.stop()


async def test_handles_transition_from_content_to_empty_and_back_to_content():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    # Start with content
    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.start()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "Line 0" in viewport[0], "Initial content rendered"

    # Clear to empty
    component.lines = []
    tui.requestRender()
    await terminal.waitForRender()

    # Add content back - this should work correctly even after empty state
    component.lines = ["New Line 0", "New Line 1"]
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert "New Line 0" in viewport[0], f"New content rendered: {viewport[0]}"
    assert "New Line 1" in viewport[1], f"New content line 1: {viewport[1]}"

    tui.stop()


async def test_full_re_renders_when_deleted_lines_move_the_viewport_upward():
    terminal = VirtualTerminal(20, 5)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = [f"Line {i}" for i in range(12)]
    tui.start()
    await terminal.waitForRender()

    initialRedraws = tui.fullRedraws

    component.lines = [f"Line {i}" for i in range(7)]
    tui.requestRender()
    await terminal.waitForRender()

    assert tui.fullRedraws > initialRedraws, "Shrink should trigger a full redraw"
    assert terminal.getViewport() == ["Line 2", "Line 3", "Line 4", "Line 5", "Line 6"]

    tui.stop()


async def test_appends_after_a_shrink_without_another_full_redraw_once_the_viewport_is_reset():
    terminal = VirtualTerminal(20, 5)
    tui = TuiMainScreen(terminal)
    component = TestComponent()
    tui.addChild(component)

    component.lines = [f"Line {i}" for i in range(8)]
    tui.start()
    await terminal.waitForRender()

    initialRedraws = tui.fullRedraws

    component.lines = ["Line 0", "Line 1"]
    tui.requestRender()
    await terminal.waitForRender()

    assert tui.fullRedraws > initialRedraws, "Shrink should reset the viewport with a full redraw"
    redrawsAfterShrink = tui.fullRedraws

    component.lines = ["Line 0", "Line 1", "Line 2"]
    tui.requestRender()
    await terminal.waitForRender()

    assert tui.fullRedraws == redrawsAfterShrink, "Append should stay on the differential path"
    assert terminal.getViewport() == ["Line 0", "Line 1", "Line 2", "", ""]

    tui.stop()


async def test_clears_stale_content_when_maxlinesrendered_was_inflated_by_a_transient_component():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    chat = TestComponent()
    editor = TestComponent()
    tui.addChild(chat)
    tui.addChild(editor)

    longChat = [f"Chat {i}" for i in range(15)]
    shortChat = [f"Chat {i}" for i in range(12)]
    editorLines = ["Editor 0", "Editor 1", "Editor 2"]
    selectorLines = [f"Selector {i}" for i in range(8)]

    chat.lines = longChat
    editor.lines = editorLines
    tui.start()
    await terminal.waitForRender()

    editor.lines = selectorLines
    tui.requestRender()
    await terminal.waitForRender()

    editor.lines = editorLines
    tui.requestRender()
    await terminal.waitForRender()

    redrawsBeforeSwitch = tui.fullRedraws
    chat.lines = shortChat
    tui.requestRender()
    await terminal.waitForRender()

    assert tui.fullRedraws > redrawsBeforeSwitch, "Branch switch should trigger a full redraw"

    viewport = terminal.getViewport()
    for i in range(10):
        line = viewport[i] if i < len(viewport) else ""
        assert "Chat 12" not in line, f'Stale "Chat 12" at viewport row {i}'
        assert "Chat 13" not in line, f'Stale "Chat 13" at viewport row {i}'
        assert "Chat 14" not in line, f'Stale "Chat 14" at viewport row {i}'

    assert viewport == [
        "Chat 5",
        "Chat 6",
        "Chat 7",
        "Chat 8",
        "Chat 9",
        "Chat 10",
        "Chat 11",
        "Editor 0",
        "Editor 1",
        "Editor 2",
    ]

    tui.stop()


# --- TUI fake cursor ------------------------------------------------------------------------------


def withoutCursorVisibility(writes: str) -> str:
    """MISAKA fork: the writes without the trailing show/hide cursor (see the module docstring)."""
    return writes.removesuffix("\x1b[?25h").removesuffix("\x1b[?25l")


async def test_draws_the_focused_fake_cursor_in_reverse_video_and_omits_it_when_the_hardware_cursor_is_shown():
    terminal = LoggingVirtualTerminal(20, 5)
    tui: TUI = TuiMainScreen(terminal)
    input = Input()
    input.setValue("abc")
    tui.addChild(input)
    tui.setFocus(input)
    tui.start()
    await terminal.waitForRender()
    assert "> \x1b[7ma\x1b[27mbc" in terminal.getWrites()

    terminal.clearWrites()
    tui.setShowHardwareCursor(True)
    await terminal.waitForRender()
    assert "\x1b[7m" not in terminal.getWrites()
    assert "\x1b_pi:" not in terminal.getWrites()

    tui.stop()


async def test_keeps_the_fake_cursor_of_an_unfocused_input_when_the_hardware_cursor_is_shown():
    terminal = LoggingVirtualTerminal(20, 5)
    tui: TUI = TuiMainScreen(terminal, True)
    input = Input()
    input.setValue("abc")
    tui.addChild(input)
    tui.start()
    await terminal.waitForRender()
    assert "> \x1b[7ma\x1b[27mbc" in terminal.getWrites()
    tui.stop()


async def test_omits_the_focused_fake_cursor_when_truncation_dropped_its_end_marker():
    terminal = LoggingVirtualTerminal(20, 5)
    tui: TUI = TuiMainScreen(terminal, True)
    base = TestComponent()
    base.lines = [sliceByColumn(f"ab{CURSOR_MARKER}{renderFakeCursor('c')}def", 0, 3, True)]
    tui.addChild(base)
    tui.start()
    await terminal.waitForRender()
    assert "\x1b[7m" not in terminal.getWrites()
    assert withoutCursorVisibility(terminal.getWrites()).endswith("\x1b[3G")
    tui.stop()


async def test_keeps_the_cursor_when_an_overlay_ends_directly_left_of_it():
    for showHardwareCursor in [False, True]:
        terminal = LoggingVirtualTerminal(20, 5)
        tui: TUI = TuiMainScreen(terminal, showHardwareCursor)
        base = TestComponent()
        base.lines = [f"abc{CURSOR_MARKER}{renderFakeCursor('d')}ef"]
        tui.addChild(base)
        overlay = TestComponent()
        overlay.lines = ["XXX"]
        tui.showOverlay(overlay, {"row": 0, "col": 0, "width": 3})
        tui.start()
        await terminal.waitForRender()

        writes = terminal.getWrites()
        assert "XXX" in writes
        assert ("\x1b[7md" in writes) == (not showHardwareCursor)
        # The hardware cursor is positioned at column 4 in both modes
        assert withoutCursorVisibility(writes).endswith("\x1b[4G")
        tui.stop()


# --- MISAKA: the fake cursor in the resize fast path, and with wide text and IME ------------------


def isInverse(terminal: VirtualTerminal, row: int, col: int) -> bool:
    """Whether the viewport cell is drawn in reverse video (libghostty-vt's style field 7)."""
    return bool(terminal.viewportRows()[row][col][2][7])


async def test_the_resize_fast_path_resolves_the_fake_cursor_before_writing():
    for showHardwareCursor in [False, True]:
        terminal = LoggingVirtualTerminal(20, 5)
        tui: TUI = TuiMainScreen(terminal, showHardwareCursor)
        input = Input()
        input.setValue("abc")
        tui.addChild(input)
        tui.setFocus(input)
        tui.start()
        await terminal.waitForRender()

        terminal.clearWrites()
        terminal.resize(30, 5)
        tui.renderNow()  # the first frame after the resize: the viewport repaint (`_try_viewport_render`)
        fastFrame = terminal.getWrites()
        assert "\x1b[2J\x1b[H" in fastFrame, "the resize frame should take the fast path"
        assert "\x1b_pi:" not in fastFrame
        assert ("> \x1b[7ma\x1b[27mbc" in fastFrame) == (not showHardwareCursor)
        assert withoutCursorVisibility(fastFrame).endswith("\x1b[3G")
        await terminal.waitForRender()
        assert "\x1b_pi:" not in terminal.getWrites()
        tui.stop()


async def test_the_fake_cursor_covers_a_wide_cjk_character_and_the_ime_cursor_sits_on_it():
    for showHardwareCursor in [False, True]:
        terminal = LoggingVirtualTerminal(30, 6)
        tui: TUI = TuiMainScreen(terminal, showHardwareCursor)
        editor = Editor(tui, defaultEditorTheme)
        tui.addChild(editor)
        tui.setFocus(editor)
        editor.setText("日本語テスト")
        editor.handleInput("\x1b[D")
        editor.handleInput("\x1b[D")  # the cursor on ス, after 4 wide characters
        tui.start()
        await terminal.waitForRender()

        writes = terminal.getWrites()
        assert "\x1b_pi:" not in writes
        assert ("\x1b[7mス\x1b[27m" in writes) == (not showHardwareCursor)
        assert "日本語テスト" in terminal.getViewport()[1]
        # The IME candidate window follows the hardware cursor: row 1 (below the border), column 8.
        assert terminal.getCursorPosition() == {"x": 8, "y": 1}
        assert isInverse(terminal, 1, 8) == (not showHardwareCursor)
        assert isInverse(terminal, 1, 6) is False and isInverse(terminal, 1, 10) is False
        tui.stop()


async def test_the_fake_cursor_keeps_a_zwj_cluster_whole():
    terminal = LoggingVirtualTerminal(30, 6)
    tui: TUI = TuiMainScreen(terminal)
    editor = Editor(tui, defaultEditorTheme)
    tui.addChild(editor)
    tui.setFocus(editor)
    editor.setText("a👨‍👩‍👧b")
    editor.handleInput("\x1b[D")
    editor.handleInput("\x1b[D")  # the cursor on the family emoji
    tui.start()
    await terminal.waitForRender()

    assert "\x1b[7m👨‍👩‍👧\x1b[27mb" in terminal.getWrites()
    assert terminal.getCursorPosition() == {"x": 1, "y": 1}
    tui.stop()


async def test_a_focused_input_with_cjk_text_places_the_ime_cursor_after_the_wide_characters():
    for showHardwareCursor in [False, True]:
        terminal = LoggingVirtualTerminal(30, 3)
        tui: TUI = TuiMainScreen(terminal, showHardwareCursor)
        input = Input()
        tui.addChild(input)
        tui.setFocus(input)
        for ch in "中文输入":
            input.handleInput(ch)
        input.handleInput("\x1b[D")  # the cursor on 入
        tui.start()
        await terminal.waitForRender()

        writes = terminal.getWrites()
        assert "\x1b_pi:" not in writes
        assert ("> 中文输\x1b[7m入\x1b[27m" in writes) == (not showHardwareCursor)
        assert terminal.getCursorPosition() == {"x": 8, "y": 0}  # "> " + three wide characters
        tui.stop()


async def test_an_overlay_ending_left_of_a_cjk_cursor_keeps_the_ime_position():
    for showHardwareCursor in [False, True]:
        terminal = LoggingVirtualTerminal(20, 5)
        tui: TUI = TuiMainScreen(terminal, showHardwareCursor)
        base = TestComponent()
        base.lines = [f"日本{CURSOR_MARKER}{renderFakeCursor('語')}です"]
        tui.addChild(base)
        overlay = TestComponent()
        overlay.lines = ["XXXX"]
        tui.showOverlay(overlay, {"row": 0, "col": 0, "width": 4})
        tui.start()
        await terminal.waitForRender()

        writes = terminal.getWrites()
        assert "XXXX" in writes
        assert ("\x1b[7m語" in writes) == (not showHardwareCursor)
        assert withoutCursorVisibility(writes).endswith("\x1b[5G")
        assert terminal.getCursorPosition() == {"x": 4, "y": 0}
        tui.stop()


def test_extract_segments_keeps_an_apc_marker_at_the_after_boundary():
    # The overlay covers columns 0-3; the cursor marker sits right where "after" starts, before a wide character.
    segments = extractSegments(f"日本{CURSOR_MARKER}語です", 4, 4, 16)
    assert segments.after.startswith(CURSOR_MARKER)
    assert segments.after.endswith("語です")
    assert segments.afterWidth == 6


# --- TUI shrinking content (tui-shrink.test.ts) ---------------------------------------------------


class Lines:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def render(self, _width: int) -> list[str]:
        return self.lines

    def invalidate(self) -> None:
        pass


async def test_clears_all_rendered_lines_when_content_shrinks_to_zero():
    terminal = VirtualTerminal(40, 10)
    tui = TuiMainScreen(terminal)
    content = Lines(["first", "second", "third"])
    tui.addChild(content)
    tui.start()
    await terminal.waitForRender()

    assert any("first" in line for line in terminal.getViewport())
    assert any("second" in line for line in terminal.getViewport())
    assert any("third" in line for line in terminal.getViewport())

    tui.clear()
    tui.requestRender()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    assert not any("first" in line for line in viewport), "first line should be cleared"
    assert not any("second" in line for line in viewport), "second line should be cleared"
    assert not any("third" in line for line in viewport), "third line should be cleared"

    tui.stop()


# --- TUI cell size responses (tui-cell-size-input.test.ts) ----------------------------------------


class InputRecorder:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def render(self, _width: int) -> list[str]:
        return [""]

    def handleInput(self, data: str) -> None:
        self.inputs.append(data)

    def invalidate(self) -> None:
        pass


@pytest.fixture
def imageTerminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("GHOSTTY_RESOURCES_DIR", raising=False)
    for name in ("TMUX", "MISAKA_IMAGE_PROTOCOL", "MISAKA_HYPERLINKS", "MISAKA_TRUE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    resetCapabilitiesCache()
    yield
    resetCapabilitiesCache()


async def test_forwards_bare_escape_even_when_a_cell_size_query_was_sent_at_startup(imageTerminal):
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    recorder = InputRecorder()

    tui.setFocus(recorder)
    tui.start()

    terminal.sendInput("\x1b")

    assert recorder.inputs == ["\x1b"]
    tui.stop()


async def test_consumes_cell_size_responses_and_still_forwards_later_user_input(imageTerminal):
    setCellDimensions({"widthPx": 9, "heightPx": 18})

    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    recorder = InputRecorder()

    tui.setFocus(recorder)
    tui.start()

    terminal.sendInput("\x1b[6;20;10t")
    assert recorder.inputs == []
    assert getCellDimensions() == CellDimensions(widthPx=10, heightPx=20)

    terminal.sendInput("q")
    assert recorder.inputs == ["q"]
    tui.stop()


# --- TUI overlay compositing (tui-overlay-style-leak.test.ts) -------------------------------------


class StaticOverlay:
    def __init__(self, line: str) -> None:
        self.line = line

    def render(self, _width: int) -> list[str]:
        return [self.line]

    def invalidate(self) -> None:
        pass


async def renderAndFlush(tui: TuiMainScreen, terminal: VirtualTerminal) -> None:
    tui.requestRender(True)
    await asyncio.sleep(0)
    await terminal.waitForRender()


async def test_should_not_leak_styles_when_a_trailing_reset_sits_beyond_the_last_visible_column_no_overlay():
    width = 20
    baseLine = f"\x1b[3m{'X' * width}\x1b[23m"

    terminal = VirtualTerminal(width, 6)
    tui = TuiMainScreen(terminal)
    tui.addChild(Lines([baseLine, "INPUT"]))
    tui.start()
    await renderAndFlush(tui, terminal)
    assert terminal.getCellItalic(1, 0) is False
    tui.stop()


async def test_should_not_leak_styles_when_overlay_slicing_drops_trailing_sgr_resets():
    width = 20
    baseLine = f"\x1b[3m{'X' * width}\x1b[23m"

    terminal = VirtualTerminal(width, 6)
    tui = TuiMainScreen(terminal)
    tui.addChild(Lines([baseLine, "INPUT"]))

    tui.showOverlay(StaticOverlay("OVR"), {"row": 0, "col": 5, "width": 3})
    tui.start()
    await renderAndFlush(tui, terminal)

    assert terminal.getCellItalic(1, 0) is False
    tui.stop()


# --- TUI overlay with short content (overlay-short-content.test.ts) -------------------------------


class SimpleOverlay:
    def render(self, _width: int) -> list[str]:
        return ["OVERLAY_TOP", "OVERLAY_MID", "OVERLAY_BOT"]

    def invalidate(self) -> None:
        pass


async def test_should_render_overlay_when_content_is_shorter_than_terminal_height():
    # Terminal has 24 rows, but content only has 3 lines
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    # Only 3 lines of content
    tui.addChild(Lines(["Line 1", "Line 2", "Line 3"]))

    # Show overlay centered - should be around row 10 in a 24-row terminal
    overlay = SimpleOverlay()
    tui.showOverlay(overlay)

    # Trigger render
    tui.start()
    await terminal.waitForRender()

    viewport = terminal.getViewport()
    hasOverlay = any("OVERLAY" in line for line in viewport)

    assert hasOverlay, "Overlay should be visible when content is shorter than terminal"

    tui.stop()


# --- overlay CJK boundary regression (compositeTuiLine) -------------------------------------------


def test_composites_an_overlay_at_the_requested_column_when_it_starts_inside_a_wide_grapheme():
    out = compositeTuiLine("abcd让EFGH", "│XX│", 5, 4, 20)
    prefix = sliceByColumn(out, 0, 5, True)
    overlay = sliceByColumn(out, 5, 4, True)

    assert ("让" in out) is False
    assert visibleWidth(out) == 20
    assert visibleWidth(prefix) == 5
    assert visibleWidth(overlay) == 4
    assert ("│XX│" in overlay) is True


def test_composites_an_overlay_when_it_starts_at_a_wide_grapheme_boundary():
    out = compositeTuiLine("abcd让EFGH", "│XX│", 4, 4, 20)
    overlay = sliceByColumn(out, 4, 4, True)

    assert ("让" in out) is False
    assert visibleWidth(out) == 20
    assert visibleWidth(overlay) == 4
    assert ("│XX│" in overlay) is True


# --- tab width accounting (tab-width.test.ts) -----------------------------------------------------


class FullViewportContent:
    def render(self, width: int) -> list[str]:
        return [line.ljust(width) for line in ["base 0", "base 1", "base 2"]]

    def invalidate(self) -> None:
        pass


class CapturingVirtualTerminal(VirtualTerminal):
    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        super().__init__(columns, rows)
        self.output = ""

    def write(self, data: str) -> None:
        self.output += data
        super().write(data)

    def getOutput(self) -> str:
        return self.output


class TabStatusOverlay:
    def render(self, _width: int) -> list[str]:
        return ["	X"]

    def invalidate(self) -> None:
        pass


async def test_keeps_tab_containing_overlays_on_one_physical_terminal_row():
    terminal = CapturingVirtualTerminal(16, 3)
    tui = TuiMainScreen(terminal)
    tui.addChild(FullViewportContent())
    tui.showOverlay(TabStatusOverlay(), {"width": 4, "row": 1, "col": 4})
    tui.start()

    try:
        await terminal.waitForRender()
        assert terminal.getViewport() == ["base 0          ", "base   X        ", "base 2          "]
        assert "	" not in terminal.getOutput()
    finally:
        tui.stop()
