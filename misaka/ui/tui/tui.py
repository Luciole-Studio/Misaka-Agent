"""Minimal terminal UI container with differential rendering."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

from misaka.ui.tui.keys import isKeyRelease, matchesKey
from misaka.ui.tui.terminal import Terminal
from misaka.ui.tui.terminal_colors import (
    RgbColor,
    TerminalColorScheme,
    is_osc11_background_color_response,
    parse_osc11_background_color,
    parse_terminal_color_scheme_report,
)
from misaka.ui.tui.terminal_image import deleteKittyImage, getCapabilities, isImageLine, setCellDimensions
from misaka.ui.tui.utils import (
    extractSegments,
    normalizeTerminalOutput,
    sliceByColumn,
    sliceWithWidth,
    visibleWidth,
)

KITTY_SEQUENCE_PREFIX = "\x1b_G"
CURSOR_MARKER = "\x1b_misaka:c\x07"


def _utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extract_kitty_image_ids(line: str) -> list[int]:
    sequence_start = line.find(KITTY_SEQUENCE_PREFIX)
    if sequence_start == -1:
        return []
    params_start = sequence_start + len(KITTY_SEQUENCE_PREFIX)
    params_end = line.find(";", params_start)
    if params_end == -1:
        return []
    params = line[params_start:params_end]
    for param in params.split(","):
        if "=" not in param:
            continue
        key, value = param.split("=", 1)
        if key != "i":
            continue
        try:
            image_id = int(value)
        except ValueError:
            continue
        if 0 < image_id <= 0xFFFFFFFF:
            return [image_id]
    return []


class Component(Protocol):
    wantsKeyRelease: bool | None

    def render(self, width: int) -> list[str]: ...

    def handleInput(self, data: str) -> None: ...

    def invalidate(self) -> None: ...


class Focusable(Protocol):
    focused: bool


def isFocusable(component: Component | None) -> bool:
    return component is not None and hasattr(component, "focused")


type OverlayAnchor = Literal[
    "center",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "top-center",
    "bottom-center",
    "left-center",
    "right-center",
]
type SizeValue = int | str


class OverlayMargin(TypedDict, total=False):
    top: int
    right: int
    bottom: int
    left: int


class OverlayOptions(TypedDict, total=False):
    width: SizeValue
    minWidth: int
    maxHeight: SizeValue
    anchor: OverlayAnchor
    offsetX: int
    offsetY: int
    row: SizeValue
    col: SizeValue
    margin: OverlayMargin | int
    visible: Callable[[int, int], bool]
    nonCapturing: bool


def _parse_size_value(value: SizeValue | None, reference_size: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"(\d+(?:\.\d+)?)%", value)
    if match is None:
        return None
    return int((reference_size * float(match.group(1))) // 100)


def _is_termux_session() -> bool:
    return bool(os.environ.get("TERMUX_VERSION"))


@dataclass(slots=True)
class _OverlayLayout:
    width: int
    row: int
    col: int
    maxHeight: int | None


class OverlayHandle(Protocol):
    def hide(self) -> None: ...
    def setHidden(self, hidden: bool) -> None: ...
    def isHidden(self) -> bool: ...
    def focus(self) -> None: ...
    def unfocus(self) -> None: ...
    def isFocused(self) -> bool: ...


class _OverlayHandle:
    def __init__(self, tui: TUI, entry: dict[str, Any]) -> None:
        self._tui = tui
        self._entry = entry

    def hide(self) -> None:
        if self._entry not in self._tui.overlayStack:
            return
        self._tui.overlayStack.remove(self._entry)
        if self._tui.focusedComponent is self._entry["component"]:
            top_visible = self._tui._getTopmostVisibleOverlay()
            self._tui.setFocus(top_visible["component"] if top_visible is not None else self._entry["preFocus"])
        if not self._tui.overlayStack:
            self._tui.terminal.hideCursor()
        self._tui.requestRender()

    def setHidden(self, hidden: bool) -> None:
        if self._entry["hidden"] == hidden:
            return
        self._entry["hidden"] = hidden
        options = self._entry.get("options")
        if options is None:
            options = {}
        component = self._entry["component"]
        if hidden:
            if self._tui.focusedComponent is component:
                top_visible = self._tui._getTopmostVisibleOverlay()
                self._tui.setFocus(top_visible["component"] if top_visible is not None else self._entry["preFocus"])
        elif not options.get("nonCapturing") and self._tui._isOverlayVisible(self._entry):
            self._entry["focusOrder"] = self._tui._nextFocusOrder()
            self._tui.setFocus(component)
        self._tui.requestRender()

    def isHidden(self) -> bool:
        return bool(self._entry["hidden"])

    def focus(self) -> None:
        if self._entry not in self._tui.overlayStack or not self._tui._isOverlayVisible(self._entry):
            return
        if self._tui.focusedComponent is not self._entry["component"]:
            self._tui.setFocus(self._entry["component"])
        self._entry["focusOrder"] = self._tui._nextFocusOrder()
        self._tui.requestRender()

    def unfocus(self) -> None:
        if self._tui.focusedComponent is not self._entry["component"]:
            return
        top_visible = self._tui._getTopmostVisibleOverlay()
        target = (
            top_visible["component"]
            if top_visible is not None and top_visible is not self._entry
            else self._entry["preFocus"]
        )
        self._tui.setFocus(target)
        self._tui.requestRender()

    def isFocused(self) -> bool:
        return self._tui.focusedComponent is self._entry["component"]


class Container:
    def __init__(self) -> None:
        self.children: list[Component] = []

    def addChild(self, component: Component) -> None:
        self.children.append(component)

    def removeChild(self, component: Component) -> None:
        for index, child in enumerate(self.children):
            if child is component:
                del self.children[index]
                break

    def clear(self) -> None:
        self.children.clear()

    def invalidate(self) -> None:
        for child in self.children:
            invalidate = getattr(child, "invalidate", None)
            if callable(invalidate):
                invalidate()

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        return lines


class TUI(Container):
    MIN_RENDER_INTERVAL_MS = 16
    SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"

    def __init__(self, terminal: Terminal, showHardwareCursor: bool | None = None) -> None:
        super().__init__()
        self.terminal = terminal
        self.previousLines: list[str] = []
        self.previousKittyImageIds: set[int] = set()
        self.previousWidth = 0
        self.previousHeight = 0
        self.focusedComponent: Component | None = None
        self.inputListeners: dict[Any, None] = {}
        self.pendingOsc11BackgroundReplies = 0
        self.pendingOsc11BackgroundQueries: list[dict] = []
        self.terminalColorSchemeListeners: dict = {}
        self.terminalColorSchemeNotificationsEnabled = False
        self.onDebug: Any | None = None
        self.renderRequested = False
        self.immediateRenderScheduled = False
        self.renderTimer: threading.Timer | None = None
        self.lastRenderAt = 0.0
        self._renderLock = threading.Lock()
        self.cursorRow = 0
        self.hardwareCursorRow = 0
        self.showHardwareCursor = os.environ.get("MISAKA_HARDWARE_CURSOR") == "1"
        self.clearOnShrink = os.environ.get("MISAKA_CLEAR_ON_SHRINK") == "1"
        self.maxLinesRendered = 0
        self.previousViewportTop = 0
        self.fullRedrawCount = 0
        self.stopped = False
        self.focusOrderCounter = 0
        self.overlayStack: list[dict[str, Any]] = []
        if showHardwareCursor is not None:
            self.showHardwareCursor = showHardwareCursor

    @property
    def fullRedraws(self) -> int:
        return self.fullRedrawCount

    def _nextFocusOrder(self) -> int:
        self.focusOrderCounter += 1
        return self.focusOrderCounter

    def getShowHardwareCursor(self) -> bool:
        return self.showHardwareCursor

    def setShowHardwareCursor(self, enabled: bool) -> None:
        if self.showHardwareCursor == enabled:
            return
        self.showHardwareCursor = enabled
        if not enabled:
            self.terminal.hideCursor()
        self.requestRender()

    def getClearOnShrink(self) -> bool:
        return self.clearOnShrink

    def setClearOnShrink(self, enabled: bool) -> None:
        self.clearOnShrink = enabled

    def setFocus(self, component: Component | None) -> None:
        if isFocusable(self.focusedComponent):
            self.focusedComponent.focused = False
        self.focusedComponent = component
        if isFocusable(component):
            component.focused = True

    def showOverlay(self, component: Component, options: OverlayOptions | None = None) -> OverlayHandle:
        entry = {
            "component": component,
            "options": options,
            "preFocus": self.focusedComponent,
            "hidden": False,
            "focusOrder": self._nextFocusOrder(),
        }
        self.overlayStack.append(entry)
        entry_options = entry["options"]
        if not (entry_options is not None and entry_options.get("nonCapturing")) and self._isOverlayVisible(entry):
            self.setFocus(component)
        self.terminal.hideCursor()
        self.requestRender()
        return _OverlayHandle(self, entry)

    def hideOverlay(self) -> None:
        if not self.overlayStack:
            return
        overlay = self.overlayStack.pop()
        if self.focusedComponent is overlay["component"]:
            top_visible = self._getTopmostVisibleOverlay()
            self.setFocus(top_visible["component"] if top_visible is not None else overlay["preFocus"])
        if not self.overlayStack:
            self.terminal.hideCursor()
        self.requestRender()

    def hasOverlay(self) -> bool:
        return any(self._isOverlayVisible(entry) for entry in self.overlayStack)

    def _isOverlayVisible(self, entry: dict[str, Any]) -> bool:
        if entry["hidden"]:
            return False
        options = entry["options"]
        visible = options.get("visible") if options is not None else None
        if callable(visible):
            return bool(visible(self.terminal.columns, self.terminal.rows))
        return True

    def _getTopmostVisibleOverlay(self) -> dict[str, Any] | None:
        for entry in reversed(self.overlayStack):
            options = entry["options"]
            if options is not None and options.get("nonCapturing"):
                continue
            if self._isOverlayVisible(entry):
                return entry
        return None

    def invalidate(self) -> None:
        super().invalidate()
        for overlay in self.overlayStack:
            invalidate = getattr(overlay["component"], "invalidate", None)
            if callable(invalidate):
                invalidate()

    def start(self) -> None:
        self.stopped = False
        self.terminal.start(lambda data: self.handleInput(data), lambda: self.requestRender())
        self.terminal.hideCursor()
        if self.terminalColorSchemeNotificationsEnabled:
            self.terminal.write("\x1b[?2031h")
        self.queryCellSize()
        self.requestRender()

    def onTerminalColorSchemeChange(self, listener) -> Callable[[], None]:
        self.terminalColorSchemeListeners[listener] = None
        return lambda: self.terminalColorSchemeListeners.pop(listener, None)

    def setTerminalColorSchemeNotifications(self, enabled: bool) -> None:
        if self.terminalColorSchemeNotificationsEnabled == enabled:
            return
        self.terminalColorSchemeNotificationsEnabled = enabled
        if not self.stopped:
            self.terminal.write("\x1b[?2031h" if enabled else "\x1b[?2031l")

    async def queryTerminalBackgroundColor(self, timeoutMs: int = 1000) -> RgbColor | None:
        """Ask the terminal for its real background color via OSC 11 (pi tui.ts:1199). None on timeout or parse failure."""
        loop = __import__("asyncio").get_running_loop()
        fut = loop.create_future()
        query = {"settled": False, "future": fut, "timer": None}

        def _timeout() -> None:
            if query["settled"]:
                return
            query["settled"] = True
            if not fut.done():
                fut.set_result(None)

        query["timer"] = loop.call_later(timeoutMs / 1000.0, _timeout)
        self.pendingOsc11BackgroundQueries.append(query)
        self.pendingOsc11BackgroundReplies += 1
        self.terminal.write("\x1b]11;?\x07")
        return await fut

    async def queryTerminalColorScheme(self, timeoutMs: int = 1000) -> TerminalColorScheme | None:
        """Ask for the light/dark color scheme via CSI ?996n (pi tui.ts:1227); supporting terminals reply ?997;1/2 n."""
        loop = __import__("asyncio").get_running_loop()
        fut = loop.create_future()

        def _settle(scheme) -> None:
            if not fut.done():
                fut.set_result(scheme)

        unsubscribe = self.onTerminalColorSchemeChange(_settle)
        timer = loop.call_later(timeoutMs / 1000.0, lambda: _settle(None))
        self.terminal.write("\x1b[?996n")
        try:
            return await fut
        finally:
            timer.cancel()
            unsubscribe()

    def addInputListener(self, listener: Any) -> Any:
        self.inputListeners[listener] = None
        return lambda: self.removeInputListener(listener)

    def removeInputListener(self, listener: Any) -> None:
        self.inputListeners.pop(listener, None)

    def _iter_input_listeners(self):
        visited: set[Any] = set()
        while True:
            next_listener = next((listener for listener in self.inputListeners if listener not in visited), None)
            if next_listener is None:
                return
            visited.add(next_listener)
            yield next_listener

    def queryCellSize(self) -> None:
        if not getCapabilities().images:
            return
        self.terminal.write("\x1b[16t")

    def stop(self) -> None:
        self.stopped = True
        self._cancel_render_timer()
        if self.terminalColorSchemeNotificationsEnabled:
            self.terminal.write("\x1b[?2031l")
        if self.previousLines:
            target_row = len(self.previousLines)
            line_diff = target_row - self.hardwareCursorRow
            if line_diff > 0:
                self.terminal.write(f"\x1b[{line_diff}B")
            elif line_diff < 0:
                self.terminal.write(f"\x1b[{-line_diff}A")
            self.terminal.write("\r\n")
        self.terminal.showCursor()
        self.terminal.stop()

    def requestRender(self, force: bool = False) -> None:
        if force:
            self.previousLines = []
            self.previousWidth = -1
            self.previousHeight = -1
            self.cursorRow = 0
            self.hardwareCursorRow = 0
            self.maxLinesRendered = 0
            self.previousViewportTop = 0
            self._request_immediate_render()
            return
        if self.renderRequested:
            return
        self.renderRequested = True
        self._schedule_next_tick(self._scheduleRender)

    def _schedule_next_tick(self, callback: Callable[[], None]) -> None:
        timer = threading.Timer(0, callback)
        timer.daemon = True
        timer.start()

    def _request_immediate_render(self) -> None:
        """Render path that bypasses the 16ms throttle timer (pi 29d9f08: keyboard input is latency-sensitive)."""
        self._cancel_render_timer()
        self.renderRequested = True
        if self.immediateRenderScheduled:
            return
        self.immediateRenderScheduled = True
        self._schedule_next_tick(self._run_immediate_render)

    def _run_immediate_render(self) -> None:
        self.immediateRenderScheduled = False
        if self.stopped or not self.renderRequested:
            return
        # A previously queued scheduleRender() can create a timer before this
        # callback runs.  User input must preempt that throttled frame.
        self._cancel_render_timer()
        self.renderRequested = False
        self.lastRenderAt = time.perf_counter() * 1000
        self.doRender()

    def _cancel_render_timer(self) -> None:
        if self.renderTimer is None:
            return
        self.renderTimer.cancel()
        self.renderTimer = None

    def _run_scheduled_render(self, timer: threading.Timer) -> None:
        if self.renderTimer is not timer:
            return
        self.renderTimer = None
        if self.stopped or not self.renderRequested:
            return
        self.renderRequested = False
        self.lastRenderAt = time.perf_counter() * 1000
        self.doRender()
        if self.renderRequested:
            self._scheduleRender()

    def _scheduleRender(self) -> None:
        if self.stopped or self.renderTimer is not None or not self.renderRequested:
            return
        elapsed = (time.perf_counter() * 1000) - self.lastRenderAt
        delay_ms = max(0.0, self.MIN_RENDER_INTERVAL_MS - elapsed)
        timer = threading.Timer(delay_ms / 1000.0, lambda: self._run_scheduled_render(timer))
        timer.daemon = True
        self.renderTimer = timer
        timer.start()

    def handleInput(self, data: str) -> None:
        if self.consumeOsc11BackgroundResponse(data):
            return
        if self.consumeTerminalColorSchemeReport(data):
            return

        if self.inputListeners:
            current = data
            for listener in self._iter_input_listeners():
                result = listener(current)
                if isinstance(result, dict):
                    if result.get("consume"):
                        return
                    if "data" in result:
                        current = result["data"]
            if current == "":
                return
            data = current

        if self.consumeCellSizeResponse(data):
            return

        if matchesKey(data, "shift+ctrl+d") and callable(self.onDebug):
            self.onDebug()
            return

        focused_overlay = next(
            (entry for entry in self.overlayStack if entry["component"] is self.focusedComponent),
            None,
        )
        if focused_overlay is not None and not self._isOverlayVisible(focused_overlay):
            top_visible = self._getTopmostVisibleOverlay()
            if top_visible is not None:
                self.setFocus(top_visible["component"])
            else:
                self.setFocus(focused_overlay["preFocus"])

        if self.focusedComponent is not None and hasattr(self.focusedComponent, "handleInput"):
            if isKeyRelease(data) and not bool(getattr(self.focusedComponent, "wantsKeyRelease", False)):
                return
            self.focusedComponent.handleInput(data)
            # Keyboard input is latency-sensitive.  Avoid the throttled timer path.
            self._request_immediate_render()

    def consumeOsc11BackgroundResponse(self, data: str) -> bool:
        if self.pendingOsc11BackgroundReplies <= 0:
            return False
        if not is_osc11_background_color_response(data):
            return False
        rgb = parse_osc11_background_color(data)
        self.pendingOsc11BackgroundReplies -= 1
        query = self.pendingOsc11BackgroundQueries.pop(0) if self.pendingOsc11BackgroundQueries else None
        if query and not query["settled"]:
            query["settled"] = True
            timer = query.get("timer")
            if timer is not None:
                timer.cancel()
            fut = query["future"]
            if not fut.done():
                fut.set_result(rgb)
        return True

    def consumeTerminalColorSchemeReport(self, data: str) -> bool:
        scheme = parse_terminal_color_scheme_report(data)
        if not scheme:
            return False
        for listener in list(self.terminalColorSchemeListeners):
            listener(scheme)
        return True

    def consumeCellSizeResponse(self, data: str) -> bool:
        match = re.fullmatch(r"\x1b\[6;(\d+);(\d+)t", data)
        if match is None:
            return False
        height_px = int(match.group(1))
        width_px = int(match.group(2))
        if height_px <= 0 or width_px <= 0:
            return True
        setCellDimensions({"widthPx": width_px, "heightPx": height_px})
        self.invalidate()
        self.requestRender()
        return True

    def resolveOverlayLayout(
        self,
        options: dict[str, Any] | None,
        overlayHeight: int,
        termWidth: int,
        termHeight: int,
    ) -> _OverlayLayout:
        opt = options or {}
        margin_opt = opt.get("margin", {})
        if isinstance(margin_opt, int):
            margin = {"top": margin_opt, "right": margin_opt, "bottom": margin_opt, "left": margin_opt}
        else:
            margin = margin_opt

        margin_top = max(0, int(margin.get("top", 0)))
        margin_right = max(0, int(margin.get("right", 0)))
        margin_bottom = max(0, int(margin.get("bottom", 0)))
        margin_left = max(0, int(margin.get("left", 0)))
        avail_width = max(1, termWidth - margin_left - margin_right)
        avail_height = max(1, termHeight - margin_top - margin_bottom)

        width = _parse_size_value(opt.get("width"), termWidth)
        if width is None:
            width = min(80, avail_width)
        if opt.get("minWidth") is not None:
            width = max(width, int(opt["minWidth"]))
        width = max(1, min(width, avail_width))

        max_height = _parse_size_value(opt.get("maxHeight"), termHeight)
        if max_height is not None:
            max_height = max(1, min(max_height, avail_height))
        effective_height = min(overlayHeight, max_height) if max_height is not None else overlayHeight

        if opt.get("row") is not None:
            row_value = opt["row"]
            if isinstance(row_value, str):
                match = re.fullmatch(r"(\d+(?:\.\d+)?)%", row_value)
                if match is not None:
                    max_row = max(0, avail_height - effective_height)
                    row = margin_top + int(max_row * (float(match.group(1)) / 100))
                else:
                    row = self.resolveAnchorRow("center", effective_height, avail_height, margin_top)
            else:
                row = int(row_value)
        else:
            row = self.resolveAnchorRow(opt.get("anchor", "center"), effective_height, avail_height, margin_top)

        if opt.get("col") is not None:
            col_value = opt["col"]
            if isinstance(col_value, str):
                match = re.fullmatch(r"(\d+(?:\.\d+)?)%", col_value)
                if match is not None:
                    max_col = max(0, avail_width - width)
                    col = margin_left + int(max_col * (float(match.group(1)) / 100))
                else:
                    col = self.resolveAnchorCol("center", width, avail_width, margin_left)
            else:
                col = int(col_value)
        else:
            col = self.resolveAnchorCol(opt.get("anchor", "center"), width, avail_width, margin_left)

        if opt.get("offsetY") is not None:
            row += int(opt["offsetY"])
        if opt.get("offsetX") is not None:
            col += int(opt["offsetX"])

        row = max(margin_top, min(row, termHeight - margin_bottom - effective_height))
        col = max(margin_left, min(col, termWidth - margin_right - width))
        return _OverlayLayout(width=width, row=row, col=col, maxHeight=max_height)

    def resolveAnchorRow(self, anchor: OverlayAnchor, height: int, availHeight: int, marginTop: int) -> int:
        if anchor in {"top-left", "top-center", "top-right"}:
            return marginTop
        if anchor in {"bottom-left", "bottom-center", "bottom-right"}:
            return marginTop + availHeight - height
        return marginTop + ((availHeight - height) // 2)

    def resolveAnchorCol(self, anchor: OverlayAnchor, width: int, availWidth: int, marginLeft: int) -> int:
        if anchor in {"top-left", "left-center", "bottom-left"}:
            return marginLeft
        if anchor in {"top-right", "right-center", "bottom-right"}:
            return marginLeft + availWidth - width
        return marginLeft + ((availWidth - width) // 2)

    def compositeOverlays(self, lines: list[str], termWidth: int, termHeight: int) -> list[str]:
        if not self.overlayStack:
            return lines
        result = list(lines)
        rendered: list[dict[str, Any]] = []
        min_lines_needed = len(result)
        visible_entries = [entry for entry in self.overlayStack if self._isOverlayVisible(entry)]
        visible_entries.sort(key=lambda entry: entry["focusOrder"])
        for entry in visible_entries:
            component = entry["component"]
            options = entry["options"]
            layout = self.resolveOverlayLayout(options, 0, termWidth, termHeight)
            overlay_lines = component.render(layout.width)
            if layout.maxHeight is not None and len(overlay_lines) > layout.maxHeight:
                overlay_lines = overlay_lines[: layout.maxHeight]
            final_layout = self.resolveOverlayLayout(options, len(overlay_lines), termWidth, termHeight)
            rendered.append(
                {
                    "overlayLines": overlay_lines,
                    "row": final_layout.row,
                    "col": final_layout.col,
                    "width": final_layout.width,
                }
            )
            min_lines_needed = max(min_lines_needed, final_layout.row + len(overlay_lines))

        working_height = max(len(result), termHeight, min_lines_needed)
        while len(result) < working_height:
            result.append("")
        viewport_start = max(0, working_height - termHeight)

        for overlay in rendered:
            for index, overlay_line in enumerate(overlay["overlayLines"]):
                line_index = viewport_start + overlay["row"] + index
                if 0 <= line_index < len(result):
                    truncated_overlay = (
                        sliceByColumn(overlay_line, 0, overlay["width"], True)
                        if visibleWidth(overlay_line) > overlay["width"]
                        else overlay_line
                    )
                    result[line_index] = self.compositeLineAt(
                        result[line_index],
                        truncated_overlay,
                        overlay["col"],
                        overlay["width"],
                        termWidth,
                    )
        return result

    def applyLineResets(self, lines: list[str]) -> list[str]:
        reset = self.SEGMENT_RESET
        for index, line in enumerate(lines):
            if not isImageLine(line):
                lines[index] = normalizeTerminalOutput(line) + reset
        return lines

    def collectKittyImageIds(self, lines: list[str]) -> set[int]:
        ids: set[int] = set()
        for line in lines:
            ids.update(_extract_kitty_image_ids(line))
        return ids

    def deleteKittyImages(self, ids: set[int] | list[int]) -> str:
        return "".join(deleteKittyImage(image_id) for image_id in ids)

    def expandLastChangedForKittyImages(self, firstChanged: int, lastChanged: int) -> int:
        expanded = lastChanged
        for index in range(firstChanged, len(self.previousLines)):
            if _extract_kitty_image_ids(self.previousLines[index]):
                expanded = max(expanded, index)
        return expanded

    def deleteChangedKittyImages(self, firstChanged: int, lastChanged: int) -> str:
        if firstChanged < 0 or lastChanged < firstChanged:
            return ""
        ids: set[int] = set()
        max_line = min(lastChanged, len(self.previousLines) - 1)
        for index in range(firstChanged, max_line + 1):
            ids.update(_extract_kitty_image_ids(self.previousLines[index] if index < len(self.previousLines) else ""))
        return self.deleteKittyImages(ids)

    def compositeLineAt(
        self,
        baseLine: str,
        overlayLine: str,
        startCol: int,
        overlayWidth: int,
        totalWidth: int,
    ) -> str:
        if isImageLine(baseLine):
            return baseLine

        after_start = startCol + overlayWidth
        base = extractSegments(baseLine, startCol, after_start, totalWidth - after_start, True)
        overlay = sliceWithWidth(overlayLine, 0, overlayWidth, True)
        before_pad = max(0, startCol - base.beforeWidth)
        overlay_pad = max(0, overlayWidth - overlay.width)
        actual_before_width = max(startCol, base.beforeWidth)
        actual_overlay_width = max(overlayWidth, overlay.width)
        after_target = max(0, totalWidth - actual_before_width - actual_overlay_width)
        after_pad = max(0, after_target - base.afterWidth)

        result = (
            base.before
            + (" " * before_pad)
            + self.SEGMENT_RESET
            + overlay.text
            + (" " * overlay_pad)
            + self.SEGMENT_RESET
            + base.after
            + (" " * after_pad)
        )
        return result if visibleWidth(result) <= totalWidth else sliceByColumn(result, 0, totalWidth, True)

    def extractCursorPosition(self, lines: list[str], height: int) -> dict[str, int] | None:
        viewport_top = max(0, len(lines) - height)
        for row in range(len(lines) - 1, viewport_top - 1, -1):
            line = lines[row]
            marker_index = line.find(CURSOR_MARKER)
            if marker_index == -1:
                continue
            before_marker = line[:marker_index]
            col = visibleWidth(before_marker)
            lines[row] = line[:marker_index] + line[marker_index + len(CURSOR_MARKER) :]
            return {"row": row, "col": col}
        return None

    def doRender(self) -> None:
        if self.stopped:
            return

        # Prevent concurrent renders from overlapping.  doRender() can be
        # called from different threading.Timer threads (e.g. the Loader's
        # 80ms animation timer and the streaming-event render timer).
        # Without this lock, two renders can interleave their reads of
        # previousLines/hardwareCursorRow and their writes to the terminal,
        # producing duplicated lines, mispositioned footers, and scattered
        # content fragments.
        if not self._renderLock.acquire(blocking=False):
            # Another render is in progress.  Do NOT drop the frame: callers
            # (immediate/throttled runners) have already cleared renderRequested,
            # so "wait for the next requestRender()" can strand the last frame —
            # e.g. a typed key never appearing.  Re-request and let the throttle
            # path retry shortly.  (pi has no such lock: its doRender is
            # single-threaded and unconditionally renders.)
            self.renderRequested = True
            self._schedule_next_tick(self._scheduleRender)
            return

        try:
            self._doRenderInner()
        finally:
            self._renderLock.release()

    def _doRenderInner(self) -> None:
        width = self.terminal.columns
        height = self.terminal.rows
        width_changed = self.previousWidth != 0 and self.previousWidth != width
        height_changed = self.previousHeight != 0 and self.previousHeight != height
        previous_buffer_length = self.previousViewportTop + self.previousHeight if self.previousHeight > 0 else height
        prev_viewport_top = max(0, previous_buffer_length - height) if height_changed else self.previousViewportTop
        viewport_top = prev_viewport_top
        hardware_cursor_row = self.hardwareCursorRow

        def compute_line_diff(target_row: int) -> int:
            current_screen_row = hardware_cursor_row - prev_viewport_top
            target_screen_row = target_row - viewport_top
            return target_screen_row - current_screen_row

        new_lines = self.render(width)
        if self.overlayStack:
            new_lines = self.compositeOverlays(new_lines, width, height)

        cursor_pos = self.extractCursorPosition(new_lines, height)
        new_lines = self.applyLineResets(new_lines)

        debug_redraw = os.environ.get("MISAKA_DEBUG_REDRAW") == "1"

        def log_redraw(reason: str) -> None:
            if not debug_redraw:
                return
            log_path = Path.home() / ".misaka" / "agent" / "misaka-debug.log"
            message = (
                f"[{_utc_iso_timestamp()}] fullRender: {reason} "
                f"(prev={len(self.previousLines)}, new={len(new_lines)}, height={height})\n"
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(message)

        def full_render(clear: bool) -> None:
            self.fullRedrawCount += 1
            buffer = "\x1b[?2026h"
            if clear:
                buffer += self.deleteKittyImages(self.previousKittyImageIds)
                buffer += "\x1b[2J\x1b[H\x1b[3J"
            for index, line in enumerate(new_lines):
                if index > 0:
                    buffer += "\r\n"
                buffer += line
            buffer += "\x1b[?2026l"
            self.terminal.write(buffer)
            self.cursorRow = max(0, len(new_lines) - 1)
            self.hardwareCursorRow = self.cursorRow
            self.maxLinesRendered = len(new_lines) if clear else max(self.maxLinesRendered, len(new_lines))
            buffer_length = max(height, len(new_lines))
            self.previousViewportTop = max(0, buffer_length - height)
            self.positionHardwareCursor(cursor_pos, len(new_lines))
            self.previousLines = new_lines
            self.previousKittyImageIds = self.collectKittyImageIds(new_lines)
            self.previousWidth = width
            self.previousHeight = height

        if not self.previousLines and not width_changed and not height_changed:
            log_redraw("first render")
            full_render(False)
            return
        if width_changed:
            log_redraw(f"terminal width changed ({self.previousWidth} -> {width})")
            full_render(True)
            return
        if height_changed and not _is_termux_session():
            log_redraw(f"terminal height changed ({self.previousHeight} -> {height})")
            full_render(True)
            return
        if self.clearOnShrink and len(new_lines) < self.maxLinesRendered and not self.overlayStack:
            log_redraw(f"clearOnShrink (maxLinesRendered={self.maxLinesRendered})")
            full_render(True)
            return

        first_changed = -1
        last_changed = -1
        max_lines = max(len(new_lines), len(self.previousLines))
        for index in range(max_lines):
            old_line = self.previousLines[index] if index < len(self.previousLines) else ""
            new_line = new_lines[index] if index < len(new_lines) else ""
            if old_line != new_line:
                if first_changed == -1:
                    first_changed = index
                last_changed = index

        appended_lines = len(new_lines) > len(self.previousLines)
        if appended_lines:
            if first_changed == -1:
                first_changed = len(self.previousLines)
            last_changed = len(new_lines) - 1
        if first_changed != -1:
            last_changed = self.expandLastChangedForKittyImages(first_changed, last_changed)
        append_start = appended_lines and first_changed == len(self.previousLines) and first_changed > 0

        if first_changed == -1:
            self.positionHardwareCursor(cursor_pos, len(new_lines))
            self.previousViewportTop = prev_viewport_top
            self.previousHeight = height
            return

        if first_changed >= len(new_lines):
            if len(self.previousLines) > len(new_lines):
                buffer = "\x1b[?2026h"
                buffer += self.deleteChangedKittyImages(first_changed, last_changed)
                target_row = max(0, len(new_lines) - 1)
                if target_row < prev_viewport_top:
                    log_redraw(f"deleted lines moved viewport up ({target_row} < {prev_viewport_top})")
                    full_render(True)
                    return
                line_diff = compute_line_diff(target_row)
                if line_diff > 0:
                    buffer += f"\x1b[{line_diff}B"
                elif line_diff < 0:
                    buffer += f"\x1b[{-line_diff}A"
                buffer += "\r"
                extra_lines = len(self.previousLines) - len(new_lines)
                if extra_lines > height:
                    log_redraw(f"extraLines > height ({extra_lines} > {height})")
                    full_render(True)
                    return
                if extra_lines > 0:
                    buffer += "\x1b[1B"
                for index in range(extra_lines):
                    buffer += "\r\x1b[2K"
                    if index < extra_lines - 1:
                        buffer += "\x1b[1B"
                if extra_lines > 0:
                    buffer += f"\x1b[{extra_lines}A"
                buffer += "\x1b[?2026l"
                self.terminal.write(buffer)
                self.cursorRow = target_row
                self.hardwareCursorRow = target_row
            self.positionHardwareCursor(cursor_pos, len(new_lines))
            self.previousLines = new_lines
            self.previousKittyImageIds = self.collectKittyImageIds(new_lines)
            self.previousWidth = width
            self.previousHeight = height
            self.previousViewportTop = prev_viewport_top
            return

        if first_changed < prev_viewport_top:
            log_redraw(f"firstChanged < viewportTop ({first_changed} < {prev_viewport_top})")
            full_render(True)
            return

        buffer = "\x1b[?2026h"
        buffer += self.deleteChangedKittyImages(first_changed, last_changed)
        prev_viewport_bottom = prev_viewport_top + height - 1
        move_target_row = first_changed - 1 if append_start else first_changed
        if move_target_row > prev_viewport_bottom:
            current_screen_row = max(0, min(height - 1, hardware_cursor_row - prev_viewport_top))
            move_to_bottom = height - 1 - current_screen_row
            if move_to_bottom > 0:
                buffer += f"\x1b[{move_to_bottom}B"
            scroll = move_target_row - prev_viewport_bottom
            buffer += "\r\n" * scroll
            prev_viewport_top += scroll
            viewport_top += scroll
            hardware_cursor_row = move_target_row

        line_diff = compute_line_diff(move_target_row)
        if line_diff > 0:
            buffer += f"\x1b[{line_diff}B"
        elif line_diff < 0:
            buffer += f"\x1b[{-line_diff}A"
        buffer += "\r\n" if append_start else "\r"

        render_end = min(last_changed, len(new_lines) - 1)
        for index in range(first_changed, render_end + 1):
            if index > first_changed:
                buffer += "\r\n"
            buffer += "\x1b[2K"
            line = new_lines[index]
            if not isImageLine(line) and visibleWidth(line) > width:
                crash_log_path = Path.home() / ".misaka" / "agent" / "misaka-crash.log"
                crash_log_path.parent.mkdir(parents=True, exist_ok=True)
                crash_data = [
                    f"Crash at {_utc_iso_timestamp()}",
                    f"Terminal width: {width}",
                    f"Line {index} visible width: {visibleWidth(line)}",
                    "",
                    "=== All rendered lines ===",
                    *[f"[{idx}] (w={visibleWidth(value)}) {value}" for idx, value in enumerate(new_lines)],
                    "",
                ]
                crash_log_path.write_text("\n".join(crash_data), encoding="utf-8")
                self.stop()
                raise RuntimeError(
                    "\n".join(
                        [
                            f"Rendered line {index} exceeds terminal width ({visibleWidth(line)} > {width}).",
                            "",
                            "This is likely caused by a custom TUI component not truncating its output.",
                            "Use visibleWidth() to measure and truncateToWidth() to truncate lines.",
                            "",
                            f"Debug log written to: {crash_log_path}",
                        ]
                    )
                )
            buffer += line

        final_cursor_row = render_end
        if len(self.previousLines) > len(new_lines):
            if render_end < len(new_lines) - 1:
                move_down = len(new_lines) - 1 - render_end
                buffer += f"\x1b[{move_down}B"
                final_cursor_row = len(new_lines) - 1
            extra_lines = len(self.previousLines) - len(new_lines)
            for _index in range(len(new_lines), len(self.previousLines)):
                buffer += "\r\n\x1b[2K"
            buffer += f"\x1b[{extra_lines}A"

        buffer += "\x1b[?2026l"

        if os.environ.get("MISAKA_TUI_DEBUG") == "1":
            debug_dir = Path("/tmp/tui")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"render-{int(time.time() * 1000)}-{secrets.token_hex(6)}.log"
            debug_data = [
                f"firstChanged: {first_changed}",
                f"viewportTop: {viewport_top}",
                f"cursorRow: {self.cursorRow}",
                f"height: {height}",
                f"lineDiff: {line_diff}",
                f"hardwareCursorRow: {hardware_cursor_row}",
                f"renderEnd: {render_end}",
                f"finalCursorRow: {final_cursor_row}",
                f"cursorPos: {json.dumps(cursor_pos, ensure_ascii=False)}",
                f"newLines.length: {len(new_lines)}",
                f"previousLines.length: {len(self.previousLines)}",
                "",
                "=== newLines ===",
                json.dumps(new_lines, indent=2, ensure_ascii=False),
                "",
                "=== previousLines ===",
                json.dumps(self.previousLines, indent=2, ensure_ascii=False),
                "",
                "=== buffer ===",
                json.dumps(buffer, ensure_ascii=False),
            ]
            debug_path.write_text("\n".join(debug_data), encoding="utf-8")
        self.terminal.write(buffer)
        self.cursorRow = max(0, len(new_lines) - 1)
        self.hardwareCursorRow = final_cursor_row
        self.maxLinesRendered = max(self.maxLinesRendered, len(new_lines))
        self.previousViewportTop = max(prev_viewport_top, final_cursor_row - height + 1)
        self.positionHardwareCursor(cursor_pos, len(new_lines))
        self.previousLines = new_lines
        self.previousKittyImageIds = self.collectKittyImageIds(new_lines)
        self.previousWidth = width
        self.previousHeight = height

    def positionHardwareCursor(self, cursorPos: dict[str, int] | None, totalLines: int) -> None:
        if cursorPos is None or totalLines <= 0:
            self.terminal.hideCursor()
            return

        target_row = max(0, min(int(cursorPos["row"]), totalLines - 1))
        target_col = max(0, int(cursorPos["col"]))
        row_delta = target_row - self.hardwareCursorRow
        buffer = ""
        if row_delta > 0:
            buffer += f"\x1b[{row_delta}B"
        elif row_delta < 0:
            buffer += f"\x1b[{-row_delta}A"
        buffer += f"\x1b[{target_col + 1}G"
        if buffer:
            self.terminal.write(buffer)
        self.hardwareCursorRow = target_row
        if self.showHardwareCursor:
            self.terminal.showCursor()
        else:
            self.terminal.hideCursor()


__all__ = [
    "Component",
    "Container",
    "CURSOR_MARKER",
    "Focusable",
    "isFocusable",
    "OverlayAnchor",
    "OverlayHandle",
    "OverlayMargin",
    "OverlayOptions",
    "SizeValue",
    "TUI",
    "visibleWidth",
]
