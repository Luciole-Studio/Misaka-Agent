"""Translated from pi-tui's ``packages/tui/src/tui-main-screen.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- D1: ``TuiMainScreenRenderState``, ``captureRenderState``/``restoreRenderState`` and ``mode`` serve the switch to the
  fullscreen renderer, which is not ported.
- ``PI_TUI_DEBUG_REDRAW`` and ``PI_TUI_DEBUG`` are ``MISAKA_TUI_DEBUG_REDRAW`` and ``MISAKA_TUI_DEBUG``.
- ``doRender`` runs on the loop that owns rendering, under a lock (a loop-less embedder renders from timer threads),
  and records the frame's cost for the frame pacing (``TuiBase.scheduleRender``); the body is ``_doRenderInner``.
- A resize repaints the visible screen from the transcript's tail first (``_try_viewport_render``) and rebuilds the
  whole buffer once the size settles; a frame laid out for a size that changed meanwhile is laid out again.
- Without a ``logDirectory`` the crash dump goes to MISAKA's home (``home.path("crash_log")``), not the temp directory.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from misaka.ui.tui.terminal_image import deleteKittyImage, isImageLine
from misaka.ui.tui.tui import Container, TuiBase, TuiStopOptions
from misaka.ui.tui.utils import visibleWidth
from misaka.utils.js import jsNumber, jsonStringify

KITTY_SEQUENCE_PREFIX = "\x1b_G"
MAX_RENDER_WRITE_CHARS = 1024 * 1024


class BoundedTerminalWriter:
    """Streams terminal output in 1 MiB chunks so a full render never forms one string large enough to exceed V8's limit.

    `append()` fills the current chunk and flushes it when full. Oversized input is split at chunk boundaries, preserving
    surrogate pairs so each write remains valid UTF-16. Callers append synchronized-output begin/end sequences themselves;
    the final `flush()` writes any remainder, including the end sequence.

    (A Python string holds code points, so the surrogate check below only ever sees lone surrogates.)
    """

    def __init__(self, write: Callable[[str], None]) -> None:
        self.buffer = ""
        self.writtenChars = 0
        self.write = write

    def append(self, value: str) -> None:
        """Append terminal data, flushing full chunks as needed. Callers must call `flush()` after the final append.
        @param value Terminal data to write in order; oversized values are split without splitting surrogate pairs.
        """
        offset = 0
        while offset < len(value):
            capacity = MAX_RENDER_WRITE_CHARS - len(self.buffer)
            if capacity == 0:
                self.flush()
                continue

            end = min(len(value), offset + capacity)
            if (
                end < len(value)
                and ord(value[end - 1]) >= 0xD800
                and ord(value[end - 1]) <= 0xDBFF
                and ord(value[end]) >= 0xDC00
                and ord(value[end]) <= 0xDFFF
            ):
                end -= 1
            if end == offset:
                self.flush()
                continue

            self.buffer += value[offset:end]
            offset = end
            if len(self.buffer) == MAX_RENDER_WRITE_CHARS:
                self.flush()

    def flush(self) -> None:
        """Write the current chunk, if any, and retain only its character count for debug output."""
        if not self.buffer:
            return
        self.write(self.buffer)
        self.writtenChars += len(self.buffer)
        self.buffer = ""

    @property
    def length(self) -> int:
        return self.writtenChars + len(self.buffer)


@dataclass(slots=True)
class KittyImageHeader:
    ids: list[int]
    rows: int


def parseKittyImageHeader(line: str) -> KittyImageHeader | None:
    sequenceStart = line.find(KITTY_SEQUENCE_PREFIX)
    if sequenceStart == -1:
        return None
    paramsStart = sequenceStart + len(KITTY_SEQUENCE_PREFIX)
    paramsEnd = line.find(";", paramsStart)
    if paramsEnd == -1:
        return None

    ids: list[int] = []
    rows = 1
    for param in line[paramsStart:paramsEnd].split(","):
        # `param.split("=", 2)`: at most two parts, the rest of the text dropped.
        parts = param.split("=")[:2]
        key = parts[0]
        value = parts[1] if len(parts) > 1 else None
        if value is None:
            continue
        numberValue = jsNumber(value)
        # `Number.isInteger`: `jsNumber` keeps an integral value an `int`.
        if not isinstance(numberValue, int) or numberValue <= 0 or numberValue > 0xFFFFFFFF:
            continue
        if key == "i":
            ids.append(numberValue)
        elif key == "r":
            rows = numberValue
    return KittyImageHeader(ids=ids, rows=rows)


def extractKittyImageIds(line: str) -> list[int]:
    header = parseKittyImageHeader(line)
    return header.ids if header is not None else []


def extractKittyImageRows(line: str) -> int:
    header = parseKittyImageHeader(line)
    return header.rows if header is not None else 1


def isTermuxSession() -> bool:
    return bool(os.environ.get("TERMUX_VERSION"))


# MISAKA fork (D1): upstream's `TuiMainScreenRenderState` follows here.


def toISOString() -> str:
    """MISAKA fork: ``new Date().toISOString()``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TuiMainScreen(TuiBase):
    """TUI implementation that renders into the terminal's main screen and scrollback."""

    def __init__(self, terminal: Any, showHardwareCursor: bool | None = None, logDirectory: str | None = None) -> None:
        super().__init__(terminal, showHardwareCursor, logDirectory)
        # MISAKA fork (D1): upstream's `mode` follows here.
        self.previousLines: list[str] = []
        self.previousKittyImageIds: set[int] = set()
        self.previousWidth = 0
        self.previousHeight = 0
        self.cursorRow = 0
        self.hardwareCursorRow = 0
        self.maxLinesRendered = 0
        self.previousViewportTop = 0
        # MISAKA fork: the render lock and the resize fast path (see the module docstring).
        self._renderLock = threading.Lock()
        self._needs_full_rebuild = False

    # MISAKA fork (D1): upstream's `captureRenderState` and `restoreRenderState` follow here.

    def resetRenderState(self) -> None:
        self.previousLines = []
        self.previousWidth = -1
        self.previousHeight = -1
        self.cursorRow = 0
        self.hardwareCursorRow = 0
        self.maxLinesRendered = 0
        self.previousViewportTop = 0

    def beforeTerminalStop(self, options: TuiStopOptions) -> None:
        if options.get("preserveScreen") or len(self.previousLines) == 0:
            return
        self.terminal.write(" ")
        targetRow = len(self.previousLines)
        lineDiff = targetRow - self.hardwareCursorRow
        if lineDiff > 0:
            self.terminal.write(f"\x1b[{lineDiff}B")
        elif lineDiff < 0:
            self.terminal.write(f"\x1b[{-lineDiff}A")
        self.terminal.write("\r\n")

    def collectKittyImageIds(self, lines: list[str]) -> set[int]:
        ids: set[int] = set()
        for line in lines:
            for id in extractKittyImageIds(line):
                ids.add(id)
        return ids

    def deleteKittyImages(self, ids: set[int] | list[int]) -> str:
        buffer = ""
        for id in ids:
            buffer += deleteKittyImage(id)
        return buffer

    def getKittyImageReservedRows(self, lines: list[str], index: int, maxIndex: int | None = None) -> int:
        if maxIndex is None:
            maxIndex = len(lines) - 1
        rows = extractKittyImageRows(lines[index] if index < len(lines) else "")
        if rows <= 1:
            return 1

        maxRows = min(rows, maxIndex - index + 1, len(lines) - index)
        reservedRows = 1
        while reservedRows < maxRows:
            line = lines[index + reservedRows] if index + reservedRows < len(lines) else ""
            if isImageLine(line) or visibleWidth(line) > 0:
                break
            reservedRows += 1
        return reservedRows

    def expandChangedRangeForKittyImages(self, firstChanged: int, lastChanged: int, newLines: list[str]) -> dict[str, int]:
        expandedFirstChanged = firstChanged
        expandedLastChanged = lastChanged

        def expandForLines(lines: list[str]) -> None:
            nonlocal expandedFirstChanged, expandedLastChanged
            for i in range(len(lines)):
                if len(extractKittyImageIds(lines[i])) == 0:
                    continue
                blockEnd = i + self.getKittyImageReservedRows(lines, i) - 1
                if i >= firstChanged or (i <= lastChanged and blockEnd >= firstChanged):
                    expandedFirstChanged = min(expandedFirstChanged, i)
                    expandedLastChanged = max(expandedLastChanged, blockEnd)

        expandForLines(self.previousLines)
        expandForLines(newLines)
        return {"firstChanged": expandedFirstChanged, "lastChanged": expandedLastChanged}

    def deleteChangedKittyImages(self, firstChanged: int, lastChanged: int) -> str:
        if firstChanged < 0 or lastChanged < firstChanged:
            return ""

        ids: set[int] = set()
        maxLine = min(lastChanged, len(self.previousLines) - 1)
        for i in range(firstChanged, maxLine + 1):
            for id in extractKittyImageIds(self.previousLines[i] if i < len(self.previousLines) else ""):
                ids.add(id)

        return self.deleteKittyImages(ids)

    def doRender(self) -> None:
        # MISAKA fork: on the loop that owns rendering, one frame at a time, its cost recorded.
        if self._queue_to_owner(self.doRender):
            return
        if self.stopped:
            return
        # Production renders run on the owning event loop. Synchronous embedders without a loop can still call
        # doRender() from different timer threads; without this lock two renders interleave their reads of
        # previousLines/hardwareCursorRow and their writes to the terminal (duplicated lines, mispositioned footers).
        if not self._renderLock.acquire(blocking=False):
            # Another render is in progress. Do NOT drop the frame: the callers already cleared renderRequested, so
            # waiting for the next requestRender() could strand the last frame (a typed key never appearing).
            self.renderRequested = True
            self._schedule_next_tick(lambda: self.scheduleRender())
            return
        # The frame's cost is the CPU this thread spent laying it out: a terminal write held up for seconds (a Windows
        # console with text selected, a stalled ssh link) is not work the loop must be left time for.
        started, wall = time.thread_time(), time.perf_counter()
        try:
            self._doRenderInner()
        finally:
            self.lastRenderCostMs = (time.thread_time() - started) * 1000
            self.lastRenderWallMs = (time.perf_counter() - wall) * 1000
            self._renderLock.release()

    def _doRenderInner(self) -> None:
        if self.stopped:
            return
        width = self.terminal.columns
        height = self.terminal.rows
        widthChanged = self.previousWidth != 0 and self.previousWidth != width
        heightChanged = self.previousHeight != 0 and self.previousHeight != height
        # MISAKA fork: a resize paints the viewport from the transcript's tail (a few ms) instead of re-wrapping the
        # whole transcript (hundreds of ms); the full buffer is rebuilt once the size settles. A height change on
        # Termux (its software keyboard) keeps upstream's differential path, which never clears the scrollback.
        if (
            (widthChanged or (heightChanged and not isTermuxSession()))
            and self.previousLines
            and not self.hasOverlayEntries
            and self._try_viewport_render(width, height)
        ):
            return
        previousBufferLength = self.previousViewportTop + self.previousHeight if self.previousHeight > 0 else height
        prevViewportTop = max(0, previousBufferLength - height) if heightChanged else self.previousViewportTop
        viewportTop = prevViewportTop
        hardwareCursorRow = self.hardwareCursorRow

        def computeLineDiff(targetRow: int) -> int:
            currentScreenRow = hardwareCursorRow - prevViewportTop
            targetScreenRow = targetRow - viewportTop
            return targetScreenRow - currentScreenRow

        # Render all components to get new lines
        # Resolve fake cursors before compositing so overlays and line slicing see plain SGR codes
        newLines = self.resolveFakeCursors(self.render(width))

        # Composite overlays into the rendered lines (before differential compare)
        if self.hasOverlayEntries:
            newLines = self.compositeOverlays(newLines, width, height)

        # MISAKA fork: a long transcript can take a second or more to lay out; a frame laid out for the old width
        # written into the new one wraps every row. Nothing was written yet, so lay it out again for the size now.
        if (self.terminal.columns, self.terminal.rows) != (width, height):
            self.requestRender()
            return

        # Extract cursor position before applying line resets (marker must be found first)
        cursorPos = self.extractCursorPosition(newLines, height)

        newLines = self.applyLineResets(newLines)

        # Helper to clear scrollback and viewport and render all new lines
        def fullRender(clear: bool) -> None:
            self.fullRedrawCount += 1
            output = BoundedTerminalWriter(lambda data: self.terminal.write(data))
            output.append("\x1b[?2026h")  # Begin synchronized output
            if clear:
                output.append(self.deleteKittyImages(self.previousKittyImageIds))
                output.append("\x1b[2J\x1b[H\x1b[3J")  # Clear screen, home, then clear scrollback
            i = 0
            while i < len(newLines):
                if i > 0:
                    output.append("\r\n")
                line = newLines[i]
                isImage = isImageLine(line)
                imageReservedRows = self.getKittyImageReservedRows(newLines, i) if isImage else 1
                if imageReservedRows > 1 and imageReservedRows <= height:
                    for _row in range(1, imageReservedRows):
                        output.append("\r\n")
                    output.append(f"\x1b[{imageReservedRows - 1}A")
                    output.append(line)
                    output.append(f"\x1b[{imageReservedRows - 1}B")
                    i += imageReservedRows - 1
                    i += 1
                    continue
                output.append(line)
                i += 1
            output.append("\x1b[?2026l")  # End synchronized output
            output.flush()
            self.cursorRow = max(0, len(newLines) - 1)
            self.hardwareCursorRow = self.cursorRow
            # Reset max lines when clearing, otherwise track growth
            if clear:
                self.maxLinesRendered = len(newLines)
            else:
                self.maxLinesRendered = max(self.maxLinesRendered, len(newLines))
            bufferLength = max(height, len(newLines))
            self.previousViewportTop = max(0, bufferLength - height)
            self.positionHardwareCursor(cursorPos, len(newLines))
            self.previousLines = newLines
            self.previousKittyImageIds = self.collectKittyImageIds(newLines)
            self.previousWidth = width
            self.previousHeight = height

        redrawLogDirectory = self.logDirectory if os.environ.get("MISAKA_TUI_DEBUG_REDRAW") == "1" else None

        def logRedraw(reason: str) -> None:
            if redrawLogDirectory is None:
                return
            logPath = os.path.join(redrawLogDirectory, "pi-tui-debug.log")
            msg = (
                f"[{toISOString()}] fullRender: {reason} "
                f"(prev={len(self.previousLines)}, new={len(newLines)}, height={height})\n"
            )
            os.makedirs(os.path.dirname(logPath), exist_ok=True)
            with open(logPath, "a", encoding="utf-8", newline="") as handle:
                handle.write(msg)

        # MISAKA fork: after one or more fast viewport frames the size has settled; rebuild the whole buffer once, so
        # the scrollback and the diff model are correct again.
        if self._needs_full_rebuild:
            self._needs_full_rebuild = False
            fullRender(True)
            return

        # First render - just output everything without clearing (assumes clean screen)
        if len(self.previousLines) == 0 and not widthChanged and not heightChanged:
            logRedraw("first render")
            fullRender(False)
            return

        # Width changes always need a full re-render because wrapping changes.
        if widthChanged:
            logRedraw(f"terminal width changed ({self.previousWidth} -> {width})")
            fullRender(True)
            return

        # Height changes normally need a full re-render to keep the visible viewport aligned,
        # but Termux changes height when the software keyboard shows or hides.
        # In that environment, a full redraw causes the entire history to replay on every toggle.
        if heightChanged and not isTermuxSession():
            logRedraw(f"terminal height changed ({self.previousHeight} -> {height})")
            fullRender(True)
            return

        # Content shrunk below the working area and no overlays - re-render to clear empty rows
        # (overlays need the padding, so only do this when no overlays are active)
        # Configurable via setClearOnShrink()
        if self.getClearOnShrink() and len(newLines) < self.maxLinesRendered and not self.hasOverlayEntries:
            logRedraw(f"clearOnShrink (maxLinesRendered={self.maxLinesRendered})")
            fullRender(True)
            return

        # Find first and last changed lines
        firstChanged = -1
        lastChanged = -1
        maxLines = max(len(newLines), len(self.previousLines))
        for i in range(maxLines):
            oldLine = self.previousLines[i] if i < len(self.previousLines) else ""
            newLine = newLines[i] if i < len(newLines) else ""

            if oldLine != newLine:
                if firstChanged == -1:
                    firstChanged = i
                lastChanged = i
        appendedLines = len(newLines) > len(self.previousLines)
        if appendedLines:
            if firstChanged == -1:
                firstChanged = len(self.previousLines)
            lastChanged = len(newLines) - 1
        if firstChanged != -1:
            expandedRange = self.expandChangedRangeForKittyImages(firstChanged, lastChanged, newLines)
            firstChanged = expandedRange["firstChanged"]
            lastChanged = expandedRange["lastChanged"]
        appendStart = appendedLines and firstChanged == len(self.previousLines) and firstChanged > 0

        # No changes - but still need to update hardware cursor position if it moved
        if firstChanged == -1:
            self.positionHardwareCursor(cursorPos, len(newLines))
            self.previousViewportTop = prevViewportTop
            self.previousHeight = height
            return

        # All changes are in deleted lines (nothing to render, just clear)
        if firstChanged >= len(newLines):
            if len(self.previousLines) > len(newLines):
                output = BoundedTerminalWriter(lambda data: self.terminal.write(data))
                output.append("\x1b[?2026h")
                output.append(self.deleteChangedKittyImages(firstChanged, lastChanged))
                # Move to end of new content (clamp to 0 for empty content)
                targetRow = max(0, len(newLines) - 1)
                if targetRow < prevViewportTop:
                    logRedraw(f"deleted lines moved viewport up ({targetRow} < {prevViewportTop})")
                    fullRender(True)
                    return
                lineDiff = computeLineDiff(targetRow)
                if lineDiff > 0:
                    output.append(f"\x1b[{lineDiff}B")
                elif lineDiff < 0:
                    output.append(f"\x1b[{-lineDiff}A")
                output.append("\r")
                # Clear extra lines without scrolling
                extraLines = len(self.previousLines) - len(newLines)
                if extraLines > height:
                    logRedraw(f"extraLines > height ({extraLines} > {height})")
                    fullRender(True)
                    return
                clearStartOffset = 0 if len(newLines) == 0 else 1
                if extraLines > 0 and clearStartOffset > 0:
                    output.append(f"\x1b[{clearStartOffset}B")
                for i in range(extraLines):
                    output.append("\r\x1b[2K")
                    if i < extraLines - 1:
                        output.append("\x1b[1B")
                moveBack = max(0, extraLines - 1 + clearStartOffset)
                if moveBack > 0:
                    output.append(f"\x1b[{moveBack}A")
                output.append("\x1b[?2026l")
                output.flush()
                self.cursorRow = targetRow
                self.hardwareCursorRow = targetRow
            self.positionHardwareCursor(cursorPos, len(newLines))
            self.previousLines = newLines
            self.previousKittyImageIds = self.collectKittyImageIds(newLines)
            self.previousWidth = width
            self.previousHeight = height
            self.previousViewportTop = prevViewportTop
            return

        # Differential rendering can only touch what was actually visible.
        # If the first changed line is above the previous viewport, we need a full redraw.
        if firstChanged < prevViewportTop:
            logRedraw(f"firstChanged < viewportTop ({firstChanged} < {prevViewportTop})")
            fullRender(True)
            return

        # Render from first changed line to end
        # Keep updates wrapped in synchronized output while writing bounded chunks.
        output = BoundedTerminalWriter(lambda data: self.terminal.write(data))
        output.append("\x1b[?2026h")  # Begin synchronized output
        output.append(self.deleteChangedKittyImages(firstChanged, lastChanged))
        prevViewportBottom = prevViewportTop + height - 1
        moveTargetRow = firstChanged - 1 if appendStart else firstChanged
        if moveTargetRow > prevViewportBottom:
            currentScreenRow = max(0, min(height - 1, hardwareCursorRow - prevViewportTop))
            moveToBottom = height - 1 - currentScreenRow
            if moveToBottom > 0:
                output.append(f"\x1b[{moveToBottom}B")
            scroll = moveTargetRow - prevViewportBottom
            output.append("\r\n" * scroll)
            prevViewportTop += scroll
            viewportTop += scroll
            hardwareCursorRow = moveTargetRow

        # Move cursor to first changed line (use hardwareCursorRow for actual position)
        lineDiff = computeLineDiff(moveTargetRow)
        if lineDiff > 0:
            output.append(f"\x1b[{lineDiff}B")  # Move down
        elif lineDiff < 0:
            output.append(f"\x1b[{-lineDiff}A")  # Move up

        output.append("\r\n" if appendStart else "\r")  # Move to column 0

        # Only render changed lines (firstChanged to lastChanged), not all lines to end
        # This reduces flicker when only a single line changes (e.g., spinner animation)
        renderEnd = min(lastChanged, len(newLines) - 1)
        i = firstChanged
        while i <= renderEnd:
            if i > firstChanged:
                output.append("\r\n")
            line = newLines[i]
            isImage = isImageLine(line)
            imageReservedRows = self.getKittyImageReservedRows(newLines, i, renderEnd) if isImage else 1
            if imageReservedRows > 1:
                imageStartScreenRow = i - viewportTop
                if imageStartScreenRow < 0 or imageStartScreenRow + imageReservedRows > height:
                    logRedraw(
                        f"kitty image pre-clear would scroll ({imageStartScreenRow} + {imageReservedRows} > {height})"
                    )
                    fullRender(True)
                    return

                output.append("\x1b[2K")
                for _row in range(1, imageReservedRows):
                    output.append("\r\n\x1b[2K")
                output.append(f"\x1b[{imageReservedRows - 1}A")
                output.append(line)
                output.append(f"\x1b[{imageReservedRows - 1}B")
                i += imageReservedRows - 1
                i += 1
                continue

            output.append("\x1b[2K")  # Clear current line
            if not isImage and visibleWidth(line) > width:
                # Log all lines to crash file for debugging
                crashLogPath = self._crashLogPath()
                crashData = "\n".join(
                    [
                        f"Crash at {toISOString()}",
                        f"Terminal width: {width}",
                        f"Line {i} visible width: {visibleWidth(line)}",
                        "",
                        "=== All rendered lines ===",
                        *[f"[{idx}] (w={visibleWidth(lineAt)}) {lineAt}" for idx, lineAt in enumerate(newLines)],
                        "",
                    ]
                )
                os.makedirs(os.path.dirname(crashLogPath), exist_ok=True)
                with open(crashLogPath, "w", encoding="utf-8") as handle:
                    handle.write(crashData)

                # Clean up terminal state before throwing
                self.stop()

                errorMsg = "\n".join(
                    [
                        f"Rendered line {i} exceeds terminal width ({visibleWidth(line)} > {width}).",
                        "",
                        "This is likely caused by a custom TUI component not truncating its output.",
                        "Use visibleWidth() to measure and truncateToWidth() to truncate lines.",
                        "",
                        f"Debug log written to: {crashLogPath}",
                    ]
                )
                raise RuntimeError(errorMsg)
            output.append(line)
            i += 1

        # Track where cursor ended up after rendering
        finalCursorRow = renderEnd

        # If we had more lines before, clear them and move cursor back
        if len(self.previousLines) > len(newLines):
            # Move to end of new content first if we stopped before it
            if renderEnd < len(newLines) - 1:
                moveDown = len(newLines) - 1 - renderEnd
                output.append(f"\x1b[{moveDown}B")
                finalCursorRow = len(newLines) - 1
            extraLines = len(self.previousLines) - len(newLines)
            for _i in range(len(newLines), len(self.previousLines)):
                output.append("\r\n\x1b[2K")
            # Move cursor back to end of new content
            output.append(f"\x1b[{extraLines}A")

        output.append("\x1b[?2026l")  # End synchronized output

        if os.environ.get("MISAKA_TUI_DEBUG") == "1":
            debugDir = "/tmp/tui"
            os.makedirs(debugDir, exist_ok=True)
            # `Math.random().toString(36).slice(2)`: a random suffix.
            debugPath = os.path.join(debugDir, f"render-{int(time.time() * 1000)}-{secrets.token_hex(6)}.log")
            debugData = "\n".join(
                [
                    f"firstChanged: {firstChanged}",
                    f"viewportTop: {viewportTop}",
                    f"cursorRow: {self.cursorRow}",
                    f"height: {height}",
                    f"lineDiff: {lineDiff}",
                    f"hardwareCursorRow: {hardwareCursorRow}",
                    f"renderEnd: {renderEnd}",
                    f"finalCursorRow: {finalCursorRow}",
                    # `JSON.stringify(undefined)` is undefined, which the template prints as "undefined".
                    f"cursorPos: {'undefined' if cursorPos is None else jsonStringify(cursorPos)}",
                    f"newLines.length: {len(newLines)}",
                    f"previousLines.length: {len(self.previousLines)}",
                    "",
                    "=== newLines ===",
                    jsonStringify(newLines, 2),
                    "",
                    "=== previousLines ===",
                    jsonStringify(self.previousLines, 2),
                    "",
                    "=== buffer ===",
                    f"[{output.length} chars written in bounded chunks]",
                ]
            )
            with open(debugPath, "w", encoding="utf-8", newline="") as handle:
                handle.write(debugData)

        output.flush()

        # Track cursor position for next render
        # cursorRow tracks end of content (for viewport calculation)
        # hardwareCursorRow tracks actual terminal cursor position (for movement)
        self.cursorRow = max(0, len(newLines) - 1)
        self.hardwareCursorRow = finalCursorRow
        # Track terminal's working area (grows but doesn't shrink unless cleared)
        self.maxLinesRendered = max(self.maxLinesRendered, len(newLines))
        self.previousViewportTop = max(prevViewportTop, finalCursorRow - height + 1)

        # Position hardware cursor for IME
        self.positionHardwareCursor(cursorPos, len(newLines))

        self.previousLines = newLines
        self.previousKittyImageIds = self.collectKittyImageIds(newLines)
        self.previousWidth = width
        self.previousHeight = height

    def positionHardwareCursor(self, cursorPos: dict[str, int] | None, totalLines: int) -> None:
        """Position the hardware cursor for IME candidate window.
        @param cursorPos The cursor position extracted from rendered output, or null
        @param totalLines Total number of rendered lines
        """
        if not cursorPos or totalLines <= 0:
            self.terminal.hideCursor()
            return

        # Clamp cursor position to valid range
        targetRow = max(0, min(cursorPos["row"], totalLines - 1))
        targetCol = max(0, cursorPos["col"])

        # Move cursor from current position to target
        rowDelta = targetRow - self.hardwareCursorRow
        buffer = ""
        if rowDelta > 0:
            buffer += f"\x1b[{rowDelta}B"  # Move down
        elif rowDelta < 0:
            buffer += f"\x1b[{-rowDelta}A"  # Move up
        # Move to absolute column (1-indexed)
        buffer += f"\x1b[{targetCol + 1}G"

        if buffer:
            self.terminal.write(buffer)

        self.hardwareCursorRow = targetRow
        if self.getShowHardwareCursor():
            self.terminal.showCursor()
        else:
            self.terminal.hideCursor()

    # --- MISAKA fork: the crash log and the resize fast path ----------------------------------------------------------

    def _crashLogPath(self) -> str:
        """``path.join(this.logDirectory ?? os.tmpdir(), "pi-tui-crash.log")``, with MISAKA's home in place of the
        temp directory."""
        if self.logDirectory is not None:
            return os.path.join(self.logDirectory, "pi-tui-crash.log")
        # Imported here, on the crash path only, so `ui/tui` keeps no import-time dependency on `config`.
        from misaka.config import home

        return str(home.path("crash_log"))

    def _leaf_chunks_reverse(self, comp: Any, width: int) -> Iterator[list[str]]:
        """Each visible component's rendered lines, bottom-up. Descends only into plain containers (whose ``render`` is
        ``Container.render``, which passes ``width`` straight through) and renders every other component whole at
        ``width``, as the full render does: the chunks top-to-bottom reproduce the full render."""
        if type(comp).render is Container.render and getattr(comp, "children", None) is not None:
            for child in reversed(comp.children):
                yield from self._leaf_chunks_reverse(child, width)
        else:
            yield comp.render(width)

    def _render_tail(self, width: int, height: int) -> list[str]:
        """The bottom ``height`` lines of a full render, rendering only the components that reach the viewport."""
        chunks: list[list[str]] = []
        total = 0
        for chunk in self._leaf_chunks_reverse(self, width):
            chunks.append(chunk)
            total += len(chunk)
            if total >= height:
                break
        lines: list[str] = []
        for chunk in reversed(chunks):
            lines.extend(chunk)
        return lines[-height:] if len(lines) > height else lines

    def _try_viewport_render(self, width: int, height: int) -> bool:
        """A fast frame after a resize: repaint only the visible screen from the transcript's tail, leaving the
        scrollback to the terminal's own reflow until the next settled render rebuilds it. False (fall back to the full
        render) when the tail holds a kitty image, which needs the whole-buffer path."""
        tail = self._render_tail(width, height)
        if (self.terminal.columns, self.terminal.rows) != (width, height):
            self.requestRender()  # the size moved while we laid out: try again for the new one
            return True
        if any(isImageLine(line) for line in tail):
            return False
        # MISAKA fork: this frame bypasses `doRender`'s body, so it resolves the fake cursors itself, as that does
        # before it looks for the cursor; otherwise the raw APC markers reach the terminal.
        tail = self.resolveFakeCursors(tail)
        cursorPos = self.extractCursorPosition(tail, height)
        tail = self.applyLineResets(tail)
        buffer = "\x1b[?2026h\x1b[2J\x1b[H"  # clear the visible screen only (2J, not 3J): keep scrollback
        for index, line in enumerate(tail):
            if index > 0:
                buffer += "\r\n"
            buffer += line
        buffer += "\x1b[?2026l"
        self.terminal.write(buffer)
        self.cursorRow = max(0, len(tail) - 1)
        self.hardwareCursorRow = self.cursorRow
        self.positionHardwareCursor(cursorPos, len(tail))
        self.previousWidth = width
        self.previousHeight = height
        self._needs_full_rebuild = True
        self.requestRender()  # a full, clean frame (and scrollback) once the size holds
        return True


__all__ = ["TuiMainScreen"]
