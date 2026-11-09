"""Translated from pi-tui's ``test/virtual-terminal.ts`` (v1.1.0): the terminal the TUI tests render into.

MISAKA fork: pi emulates the terminal with xterm.js (``@xterm/headless``); here it is libghostty-vt, the emulator the
panel bundles (``misaka.ui.panel.ghostty``). Writes reach the emulator at once, so ``flush`` has nothing to wait for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from misaka.ui.panel import ghostty


class Cell:
    """MISAKA fork: the attribute reads of xterm's ``IBufferCell`` over a libghostty-vt cell style
    ``(fg, bg, bold, faint, italic, underline, blink, inverse, strikethrough)``."""

    def __init__(self, style: tuple[object, ...]) -> None:
        self._style = style

    def isFgDefault(self) -> bool:
        return self._style[0] is None

    def getFgColor(self) -> int:
        """An RGB colour as ``0xRRGGBB``, a palette colour as its index, the default as -1."""
        fg = self._style[0]
        if isinstance(fg, tuple):
            r, g, b = fg
            return (int(r) << 16) | (int(g) << 8) | int(b)
        return -1 if fg is None else int(fg)  # type: ignore[call-overload]

    def isBold(self) -> int:
        return 1 if self._style[2] else 0

    def isDim(self) -> int:
        return 1 if self._style[3] else 0

    def isItalic(self) -> int:
        return 1 if self._style[4] else 0

    def isUnderline(self) -> int:
        return 1 if self._style[5] else 0


class VirtualTerminal:
    """Virtual terminal for testing using xterm.js for accurate terminal emulation"""

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self._columns = columns
        self._rows = rows
        self.inputHandler: Callable[[str], None] | None = None
        self.resizeHandler: Callable[[], None] | None = None
        # Create the emulator with specified dimensions
        self.xterm = ghostty.Terminal(columns, rows)

    def start(self, onInput: Callable[[str], None], onResize: Callable[[], None]) -> None:
        self.inputHandler = onInput
        self.resizeHandler = onResize
        # Enable bracketed paste mode for consistency with ProcessTerminal
        self.xterm.write(b"\x1b[?2004h")

    async def drainInput(self, _maxMs: int = 1000, _idleMs: int = 50) -> None:
        # No-op for virtual terminal - no stdin to drain
        pass

    def stop(self) -> None:
        # Disable bracketed paste mode
        self.xterm.write(b"\x1b[?2004l")
        self.inputHandler = None
        self.resizeHandler = None

    def write(self, data: str) -> None:
        self.xterm.write(data.encode("utf-8", "surrogatepass"))

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def kittyProtocolActive(self) -> bool:
        # Virtual terminal always reports Kitty protocol as active for testing
        return True

    def moveBy(self, lines: int) -> None:
        if lines > 0:
            # Move down
            self.write(f"\x1b[{lines}B")
        elif lines < 0:
            # Move up
            self.write(f"\x1b[{-lines}A")
        # lines === 0: no movement

    def hideCursor(self) -> None:
        self.write("\x1b[?25l")

    def showCursor(self) -> None:
        self.write("\x1b[?25h")

    def clearLine(self) -> None:
        self.write("\x1b[K")

    def clearFromCursor(self) -> None:
        self.write("\x1b[J")

    def clearScreen(self) -> None:
        self.write("\x1b[2J\x1b[H")  # Clear screen and move to home (1,1)

    def setTitle(self, title: str) -> None:
        # OSC 0;title BEL - set terminal window title
        self.write(f"\x1b]0;{title}\x07")

    def setProgress(self, _active: bool) -> None:
        pass

    def setProgramStatus(self, _status: object) -> None:
        pass

    # Test-specific methods not in Terminal interface

    def sendInput(self, data: str) -> None:
        """Simulate keyboard input"""
        if self.inputHandler:
            self.inputHandler(data)

    def resize(self, columns: int, rows: int) -> None:
        """Resize the terminal"""
        self._columns = columns
        self._rows = rows
        self.xterm.resize(columns, rows)
        if self.resizeHandler:
            self.resizeHandler()

    async def flush(self) -> None:
        """Wait for all pending writes to complete. Viewport and scroll buffer will be updated."""

    async def flushAndGetViewport(self) -> list[str]:
        """Flush and get viewport - convenience method for tests"""
        await self.flush()
        return self.getViewport()

    def viewportRows(self) -> list[list[tuple[str, int, tuple[object, ...]]]]:
        """MISAKA fork: the visible rows as libghostty-vt cells ``(text, width, style)``."""
        state = ghostty.RenderState()
        try:
            state.update(self.xterm)
            return [cells for _index, cells in state.rows(all_rows=True)]
        finally:
            state.close()

    def lineText(self, y: int) -> str:
        """MISAKA fork: ``line.translateToString(true)``: the row's text up to its last written cell (spaces written
        at the end stay, cells never written do not)."""
        if y >= self.xterm.total_rows():
            return ""
        return self.xterm.read_text((0, y), (self.xterm.cols - 1, y), unwrap=False, trim=False)

    def getViewport(self) -> list[str]:
        """Get the visible viewport (what's currently on screen)"""
        lines: list[str] = []
        _total, viewportY, _length = self.xterm.scrollbar()

        # Get only the visible lines (viewport)
        for i in range(self.xterm.rows):
            lines.append(self.lineText(viewportY + i))

        return lines

    def getScrollBuffer(self) -> list[str]:
        """Get the entire scroll buffer"""
        lines: list[str] = []

        # Get all lines in the buffer (including scrollback)
        for i in range(self.xterm.total_rows()):
            lines.append(self.lineText(i))

        return lines

    def clear(self) -> None:
        """Clear the terminal viewport"""
        self.write("\x1b[H\x1b[2J\x1b[3J")

    def reset(self) -> None:
        """Reset the terminal completely"""
        self.write("\x1bc")

    def getCursorPosition(self) -> dict[str, int]:
        """Get cursor position"""
        x, y = self.xterm.cursor()
        return {"x": x, "y": y}

    def getCell(self, row: int, col: int) -> Cell:
        """MISAKA fork: a viewport cell, read as xterm's ``IBufferCell`` (``buffer.getLine(row).getCell(col)``)."""
        rows = self.viewportRows()
        assert row < len(rows), f"Missing buffer line at row {row}"
        cells = rows[row]
        assert col < len(cells), f"Missing cell at row {row} col {col}"
        return Cell(cells[col][2])

    def getCellItalic(self, row: int, col: int) -> bool:
        """MISAKA fork: whether the viewport cell is italic (xterm's ``cell.isItalic()``)."""
        return bool(self.getCell(row, col).isItalic())

    async def waitForRender(self) -> None:
        """Wait for TUI's throttled render pipeline to settle."""
        await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        await self.flush()
