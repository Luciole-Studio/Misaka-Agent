"""Translated from pi-tui's ``packages/tui/src/tui.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- D1: the mouse API (``TuiMouseEvent`` ... ``dispatchMouseEvent``, ``Component.handleMouse``, ``Container.handleMouse``,
  the rendered-overlay layouts), ``TuiMode``/``ViewportTUI`` and the overlay mouse helpers serve the fullscreen
  renderer, which is not ported. ``TuiMainScreen`` lives in ``tui_main_screen.py``.
- D11: ``queryTerminalColors`` (OSC 10/11/4 with a DA1 sentinel) belongs to pi's system theme, which is not ported.
  MISAKA's own background probe (``queryTerminalBackgroundColor``, OSC 11 only) takes its place.
- Node runs every callback on one thread. Here requests can come from worker threads (streaming, timers), so
  ``start``/``stop``/``requestRender``/``renderNow``/``doRender`` run on the loop that started the TUI, and
  ``process.nextTick``/``setTimeout`` are callbacks on that loop (a thread timer without a loop).
- Frame pacing (``scheduleRender``): a frame also waits as long as the last one took.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol, TypedDict, cast

from misaka.ui.tui.keys import isKeyRelease, matchesKey
from misaka.ui.tui.terminal import Terminal
from misaka.ui.tui.terminal_colors import (
    RgbColor,
    TerminalColorScheme,
    parseOscColorResponse,
    parseTerminalColorSchemeReport,
)
from misaka.ui.tui.terminal_image import getCapabilities, isImageLine, setCellDimensions
from misaka.ui.tui.utils import (
    extractSegments,
    normalizeTerminalOutput,
    sliceByColumn,
    sliceWithWidth,
    visibleWidth,
)
from misaka.utils import js_regexp
from misaka.utils.js import liveKeys
from misaka.utils.js_regexp import RegExp

# MISAKA fork (D1): upstream's mouse event types and `dispatchMouseEvent`/`retargetMouseEvent` come first here.


class Component(Protocol):
    """``handleInput(data)`` (keyboard input when the component has focus) and ``wantsKeyRelease`` (if true, the
    component receives key release events (Kitty protocol); default false - release events are filtered out) are
    optional: read them with ``getattr``."""

    def render(self, width: int) -> list[str]:
        """Render the component to lines for the given viewport width
        @param width - Current viewport width
        @returns Array of strings, each representing a line
        """
        ...

    def invalidate(self) -> None:
        """Invalidate any cached rendering state.
        Called when theme changes or when component needs to re-render from scratch.
        """
        ...


class TuiInputListenerResultValue(TypedDict, total=False):
    consume: bool
    data: str


type TuiInputListenerResult = TuiInputListenerResultValue | None
type TuiInputListener = Callable[[str], TuiInputListenerResult]

# MISAKA fork (D11): `PendingTerminalColorQuery`, `TERMINAL_PALETTE_SIZE`, `TERMINAL_COLOR_REPLY_COUNT`,
# `TERMINAL_COLOR_QUERY` and `DEVICE_ATTRIBUTES_RESPONSE_PATTERN` (queryTerminalColors) follow here.


class Focusable(Protocol):
    """Interface for components that can receive focus and display a hardware cursor.
    When focused, the component should emit CURSOR_MARKER at the cursor position
    in its render output. TUI will find this marker and position the hardware
    cursor there for proper IME candidate window positioning.
    """

    # Set by TUI when focus changes. Component should emit CURSOR_MARKER when true.
    focused: bool


def isFocusable(component: Component | None) -> bool:
    """Type guard to check if a component implements Focusable"""
    return component is not None and hasattr(component, "focused")


# Cursor position marker - APC (Application Program Command) sequence.
# This is a zero-width escape sequence that terminals ignore.
# Components emit this at the cursor position when focused.
# TUI finds and strips this marker, then positions the hardware cursor there.
CURSOR_MARKER = "\x1b_pi:c\x07"

# Fake cursor markers - zero-width APC sequences around the text drawn as the cursor. See resolveFakeCursors().
FAKE_CURSOR_START = "\x1b_pi:fc\x07"
FAKE_CURSOR_END = "\x1b_pi:/fc\x07"
# CURSOR_MARKER + FAKE_CURSOR_START + text + FAKE_CURSOR_END. Truncating the line right after the cursor cell drops
# the end.
FOCUSED_FAKE_CURSOR = RegExp(r"(\x1b_pi:c\x07)\x1b_pi:fc\x07(.*?)(?:\x1b_pi:\/fc\x07|$)", "s")


def renderFakeCursor(text: str) -> str:
    """Wrap text in fake cursor markers."""
    return f"{FAKE_CURSOR_START}{text}{FAKE_CURSOR_END}"


# Anchor position for overlays
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


class OverlayMargin(TypedDict, total=False):
    """Margin configuration for overlays"""

    top: int
    right: int
    bottom: int
    left: int


# Value that can be absolute (number) or percentage (string like "50%")
type SizeValue = int | float | str


def parseSizeValue(value: SizeValue | None, referenceSize: int) -> int | float | None:
    """Parse a SizeValue into absolute value given a reference size"""
    if value is None:
        return None
    if isinstance(value, int | float):
        return value
    # Parse percentage string like "50%"
    match = js_regexp.match(value, RegExp(r"^(\d+(?:\.\d+)?)%$"))
    if match:
        return math.floor((referenceSize * float(match[1])) / 100)
    return None


class OverlayOptions(TypedDict, total=False):
    """Options for overlay positioning and sizing.
    Values can be absolute numbers or percentage strings (e.g., "50%").
    """

    # === Sizing ===
    # Width in columns, or percentage of terminal width (e.g., "50%")
    width: SizeValue
    # Minimum width in columns
    minWidth: int
    # Maximum height in rows, or percentage of terminal height (e.g., "50%")
    maxHeight: SizeValue

    # === Positioning - anchor-based ===
    # Anchor point for positioning (default: 'center')
    anchor: OverlayAnchor
    # Horizontal offset from anchor position (positive = right)
    offsetX: int
    # Vertical offset from anchor position (positive = down)
    offsetY: int

    # === Positioning - percentage or absolute ===
    # Row position: absolute number, or percentage (e.g., "25%" = 25% from top)
    row: SizeValue
    # Column position: absolute number, or percentage (e.g., "50%" = centered horizontally)
    col: SizeValue

    # === Margin from terminal edges ===
    # Margin from terminal edges. Number applies to all sides.
    margin: OverlayMargin | int

    # === Visibility ===
    # Control overlay visibility based on terminal dimensions.
    # If provided, overlay is only rendered when this returns true.
    # Called each render cycle with current terminal dimensions.
    visible: Callable[[int, int], bool]
    # If true, don't capture keyboard focus when shown
    nonCapturing: bool


class OverlayUnfocusOptions(TypedDict):
    """Options for {@link OverlayHandle.unfocus}."""

    # Explicit target to focus after releasing this overlay.
    target: Component | None


class OverlayBounds(TypedDict):
    """Last rendered terminal-relative overlay rectangle."""

    row: int
    col: int
    width: int
    height: int


class OverlayHandle(Protocol):
    """Handle returned by showOverlay for controlling the overlay"""

    def hide(self) -> None:
        """Permanently remove the overlay (cannot be shown again)"""
        ...

    def setHidden(self, hidden: bool) -> None:
        """Temporarily hide or show the overlay"""
        ...

    def isHidden(self) -> bool:
        """Check if overlay is temporarily hidden"""
        ...

    def focus(self) -> None:
        """Focus this overlay and bring it to the visual front"""
        ...

    def unfocus(self, options: OverlayUnfocusOptions | None = None) -> None:
        """Release focus to the next visible capturing overlay or previous target, or to an explicit target when
        provided"""
        ...

    def isFocused(self) -> bool:
        """Check if this overlay currently has focus"""
        ...

    def getBounds(self) -> OverlayBounds | None:
        """Get the most recent rendered bounds for a visible overlay."""
        ...


@dataclass(eq=False)
class OverlayStackEntry:
    component: Component
    options: OverlayOptions | None
    preFocus: Component | None
    hidden: bool
    focusOrder: int
    bounds: OverlayBounds | None = None


# MISAKA fork (D1): upstream's `RenderedOverlayLayout` follows here.


class OverlayRestoreOverlayResume(TypedDict):
    status: Literal["restore-overlay"]


class OverlayFocusTargetResume(TypedDict):
    status: Literal["focus-target"]
    target: Component | None


type OverlayBlockedFocusResume = OverlayRestoreOverlayResume | OverlayFocusTargetResume


class EligibleOverlayFocusRestoreState(TypedDict):
    status: Literal["eligible"]
    overlay: OverlayStackEntry


class BlockedOverlayFocusRestoreState(TypedDict):
    status: Literal["blocked"]
    overlay: OverlayStackEntry
    blockedBy: Component
    resume: OverlayBlockedFocusResume


type ActiveOverlayFocusRestoreState = EligibleOverlayFocusRestoreState | BlockedOverlayFocusRestoreState


class InactiveOverlayFocusRestoreState(TypedDict):
    status: Literal["inactive"]


type OverlayFocusRestoreState = InactiveOverlayFocusRestoreState | ActiveOverlayFocusRestoreState
type OverlayFocusRestorePolicy = Literal["clear", "preserve"]


class SetFocusInternalOptions(TypedDict):
    component: Component | None
    overlayFocusRestore: OverlayFocusRestorePolicy


class Container:
    """Container - a component that contains other components"""

    def __init__(self) -> None:
        self.children: list[Component] = []

    def addChild(self, component: Component) -> None:
        self.children.append(component)

    def removeChild(self, component: Component) -> None:
        index = next((i for i, child in enumerate(self.children) if child is component), -1)
        if index != -1:
            del self.children[index]

    def clear(self) -> None:
        self.children = []

    def invalidate(self) -> None:
        for child in self.children:
            invalidate = getattr(child, "invalidate", None)
            if invalidate:
                invalidate()

    # MISAKA fork (D1): upstream's `handleMouse` follows here.

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self.children:
            childLines = child.render(width)
            lines.extend(childLines)
        return lines


# TUI - Main class for managing terminal UI with differential rendering
SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"


def compositeTuiLine(baseLine: str, overlayLine: str, startCol: int, overlayWidth: int, totalWidth: int) -> str:
    """Composite overlay content into a terminal line at a fixed column."""
    if isImageLine(baseLine):
        return baseLine

    afterStart = startCol + overlayWidth
    base = extractSegments(baseLine, startCol, afterStart, totalWidth - afterStart, True)
    overlay = sliceWithWidth(overlayLine, 0, overlayWidth, True)
    beforePad = max(0, startCol - base.beforeWidth)
    overlayPad = max(0, overlayWidth - overlay.width)
    actualBeforeWidth = max(startCol, base.beforeWidth)
    actualOverlayWidth = max(overlayWidth, overlay.width)
    afterTarget = max(0, totalWidth - actualBeforeWidth - actualOverlayWidth)
    afterPad = max(0, afterTarget - base.afterWidth)
    result = (
        base.before
        + " " * beforePad
        + SEGMENT_RESET
        + overlay.text
        + " " * overlayPad
        + SEGMENT_RESET
        + base.after
        + " " * afterPad
    )

    return result if visibleWidth(result) <= totalWidth else sliceByColumn(result, 0, totalWidth, True)


# MISAKA fork (D1): upstream's `TuiMode` follows here.


class TuiStopOptions(TypedDict, total=False):
    # Leave renderer output in place for another TUI taking over the same terminal.
    preserveScreen: bool


class TUI(Component, Protocol):
    children: list[Component]
    terminal: Terminal
    onDebug: Callable[[], None] | None

    @property
    def fullRedraws(self) -> int: ...
    def addChild(self, component: Component) -> None: ...
    def removeChild(self, component: Component) -> None: ...
    def clear(self) -> None: ...
    def getShowHardwareCursor(self) -> bool: ...
    def setShowHardwareCursor(self, enabled: bool) -> None: ...
    def getClearOnShrink(self) -> bool: ...
    def setClearOnShrink(self, enabled: bool) -> None: ...
    def setFocus(self, component: Component | None) -> None: ...
    def showOverlay(self, component: Component, options: OverlayOptions | None = None) -> OverlayHandle: ...
    def hideOverlay(self) -> None: ...
    def hasOverlay(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self, options: TuiStopOptions | None = None) -> None: ...
    def renderNow(self, force: bool = False) -> None: ...
    def requestRender(self, force: bool = False) -> None: ...
    def addInputListener(self, listener: TuiInputListener) -> Callable[[], None]: ...
    def removeInputListener(self, listener: TuiInputListener) -> None: ...
    def onTerminalColorSchemeChange(self, listener: Callable[[TerminalColorScheme], None]) -> Callable[[], None]: ...
    def setTerminalColorSchemeNotifications(self, enabled: bool) -> None: ...


# MISAKA fork (D1): upstream's `VIEWPORT_TUI`, `ViewportTUI` and `isViewportTUI` follow here.


def performanceNow() -> float:
    """MISAKA fork: ``performance.now()``, in milliseconds."""
    return time.perf_counter() * 1000


class TuiBase(Container):
    MIN_RENDER_INTERVAL_MS = 16

    @property
    def hasOverlayEntries(self) -> bool:
        return len(self.overlayStack) > 0

    def __init__(self, terminal: Terminal, showHardwareCursor: bool | None = None, logDirectory: str | None = None):
        super().__init__()
        self.terminal = terminal
        self.focusedComponent: Component | None = None
        self.inputListeners: dict[TuiInputListener, None] = {}

        self.onDebug: Callable[[], None] | None = None
        self.renderRequested = False
        self.immediateRenderScheduled = False
        self.renderTimer: asyncio.TimerHandle | threading.Timer | None = None
        self.lastRenderAt = 0.0
        self.showHardwareCursor = False
        self.clearOnShrink = False
        self.fullRedrawCount = 0
        self.stopped = False
        # MISAKA fork (D11): upstream's `pendingTerminalColorQueries` follows here; MISAKA's OSC 11 probe keeps these.
        self.pendingOsc11BackgroundReplies = 0
        self.pendingOsc11BackgroundQueries: list[dict[str, Any]] = []
        self.terminalColorSchemeListeners: dict[Callable[[TerminalColorScheme], None], None] = {}
        self.terminalColorSchemeNotificationsEnabled = False
        # Directory for debug/crash logs. When undefined, debug logging is disabled and crash dumps fall back to the OS
        # temp directory.
        # MISAKA fork: without one the crash dump goes to MISAKA's home (`home.path("crash_log")`).
        self.logDirectory = logDirectory

        # Overlay stack for modal components rendered on top of base content
        self.focusOrderCounter = 0
        self.overlayStack: list[OverlayStackEntry] = []
        self.overlayFocusRestore: OverlayFocusRestoreState = {"status": "inactive"}

        # MISAKA fork: the loop that owns rendering, and the last frame's cost (see `scheduleRender`).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._capture_loop()
        self.lastRenderCostMs = 0.0
        self.lastRenderWallMs = 0.0

        if showHardwareCursor is not None:
            self.showHardwareCursor = showHardwareCursor

    def doRender(self) -> None:
        raise NotImplementedError

    def resetRenderState(self) -> None:
        pass

    def beforeTerminalStart(self) -> None:
        pass

    def afterTerminalStart(self) -> None:
        pass

    def beforeTerminalStop(self, _options: TuiStopOptions) -> None:
        pass

    def afterTerminalStop(self, _options: TuiStopOptions) -> None:
        pass

    @property
    def fullRedraws(self) -> int:
        return self.fullRedrawCount

    def getShowHardwareCursor(self) -> bool:
        return self.showHardwareCursor

    def setShowHardwareCursor(self, enabled: bool) -> None:
        if self.showHardwareCursor == enabled:
            return
        self.showHardwareCursor = enabled
        if not enabled:
            self.hideTerminalCursor()
        self.requestRender()

    def getClearOnShrink(self) -> bool:
        return self.clearOnShrink

    def setClearOnShrink(self, enabled: bool) -> None:
        """Set whether to trigger full re-render when content shrinks.
        When true, empty rows are cleared when content shrinks.
        When false (default), empty rows remain (reduces redraws on slower terminals).
        """
        self.clearOnShrink = enabled

    def getFocusedComponent(self) -> Component | None:
        return self.focusedComponent

    def setFocus(self, component: Component | None) -> None:
        self.setFocusInternal({"component": component, "overlayFocusRestore": "clear"})

    def setFocusInternal(self, options: SetFocusInternalOptions) -> None:
        component = options["component"]
        overlayFocusRestore = options["overlayFocusRestore"]
        previousFocus = self.focusedComponent
        nextFocus = component
        previousFocusedOverlay = (
            next(
                (
                    entry
                    for entry in self.overlayStack
                    if entry.component is previousFocus and self.isOverlayVisible(entry)
                ),
                None,
            )
            if previousFocus
            else None
        )
        nextFocusIsOverlay = (
            any(entry.component is nextFocus for entry in self.overlayStack) if nextFocus else False
        )
        restoreState = self.getVisibleOverlayFocusRestore()
        if nextFocus and not nextFocusIsOverlay:
            if restoreState["status"] == "blocked" and restoreState["blockedBy"] is previousFocus:
                if restoreState["resume"]["status"] == "focus-target" or not self.isComponentMounted(
                    restoreState["blockedBy"]
                ):
                    nextFocus = self.resolveBlockedOverlayFocusResume(restoreState)
                else:
                    self.overlayFocusRestore = {
                        "status": "blocked",
                        "overlay": restoreState["overlay"],
                        "blockedBy": nextFocus,
                        "resume": restoreState["resume"],
                    }
            elif (
                previousFocusedOverlay
                and restoreState["status"] != "inactive"
                and restoreState["overlay"] is previousFocusedOverlay
                and not self.isOverlayFocusAncestor(previousFocusedOverlay, nextFocus)
            ):
                self.overlayFocusRestore = {
                    "status": "blocked",
                    "overlay": previousFocusedOverlay,
                    "blockedBy": nextFocus,
                    "resume": {"status": "restore-overlay"},
                }
        elif nextFocus is None:
            if restoreState["status"] == "blocked" and restoreState["blockedBy"] is previousFocus:
                nextFocus = self.resolveBlockedOverlayFocusResume(restoreState)
            elif overlayFocusRestore == "clear":
                self.clearOverlayFocusRestore()

        if isFocusable(self.focusedComponent):
            cast(Any, self.focusedComponent).focused = False

        self.focusedComponent = nextFocus

        if isFocusable(nextFocus):
            cast(Any, nextFocus).focused = True

        focusedOverlay = (
            next(
                (entry for entry in self.overlayStack if entry.component is nextFocus and self.isOverlayVisible(entry)),
                None,
            )
            if nextFocus
            else None
        )
        if focusedOverlay:
            self.overlayFocusRestore = {"status": "eligible", "overlay": focusedOverlay}

    def clearOverlayFocusRestore(self) -> None:
        self.overlayFocusRestore = {"status": "inactive"}

    def clearOverlayFocusRestoreFor(self, overlay: OverlayStackEntry) -> None:
        if self.overlayFocusRestore["status"] != "inactive" and self.overlayFocusRestore["overlay"] is overlay:
            self.clearOverlayFocusRestore()

    def resolveBlockedOverlayFocusResume(self, restoreState: BlockedOverlayFocusRestoreState) -> Component | None:
        resume = restoreState["resume"]
        if resume["status"] == "restore-overlay":
            return restoreState["overlay"].component
        self.clearOverlayFocusRestore()
        return resume["target"]

    def getVisibleOverlayFocusRestore(self) -> OverlayFocusRestoreState:
        restoreState = self.overlayFocusRestore
        if restoreState["status"] == "inactive":
            return restoreState
        if not any(entry is restoreState["overlay"] for entry in self.overlayStack) or not self.isOverlayVisible(
            restoreState["overlay"]
        ):
            return {"status": "inactive"}
        return restoreState

    def isOverlayFocusAncestor(self, entry: OverlayStackEntry, component: Component) -> bool:
        visited: list[Component] = []
        current = entry.preFocus
        while current and not any(seen is current for seen in visited):
            visited.append(current)
            if current is component:
                return True
            owner = next((overlay for overlay in self.overlayStack if overlay.component is current), None)
            current = owner.preFocus if owner is not None else None
        return False

    def retargetOverlayPreFocus(self, removed: OverlayStackEntry) -> None:
        for overlay in self.overlayStack:
            if overlay is not removed and overlay.preFocus is removed.component:
                overlay.preFocus = removed.preFocus

    def getMountedRoots(self) -> list[Component]:
        return self.children

    def isComponentMounted(self, component: Component) -> bool:
        return any(self.containsComponent(child, component) for child in self.getMountedRoots())

    def containsComponent(self, root: Component, target: Component) -> bool:
        if root is target:
            return True
        if not isinstance(root, Container):
            return False
        return any(self.containsComponent(child, target) for child in root.children)

    def showOverlay(self, component: Component, options: OverlayOptions | None = None) -> OverlayHandle:
        """Show an overlay component with configurable positioning and sizing.
        Returns a handle to control the overlay's visibility.
        """
        self.focusOrderCounter += 1
        entry = OverlayStackEntry(
            component=component,
            options=options,
            preFocus=self.focusedComponent,
            hidden=False,
            focusOrder=self.focusOrderCounter,
        )
        self.overlayStack.append(entry)
        # Only focus if overlay is actually visible
        if not (options or {}).get("nonCapturing") and self.isOverlayVisible(entry):
            self.setFocus(component)
        self.hideTerminalCursor()
        self.requestRender()

        # Return handle for controlling this overlay
        def hide() -> None:
            index = next((i for i, overlay in enumerate(self.overlayStack) if overlay is entry), -1)
            if index != -1:
                self.clearOverlayFocusRestoreFor(entry)
                self.retargetOverlayPreFocus(entry)
                del self.overlayStack[index]
                # Restore focus if this overlay had focus
                if self.focusedComponent is component:
                    topVisible = self.getTopmostVisibleOverlay()
                    self.setFocus(topVisible.component if topVisible else entry.preFocus)
                if len(self.overlayStack) == 0:
                    self.hideTerminalCursor()
                self.requestRender()

        def setHidden(hidden: bool) -> None:
            if entry.hidden == hidden:
                return
            entry.hidden = hidden
            # Update focus when hiding/showing
            if hidden:
                self.clearOverlayFocusRestoreFor(entry)
                # If this overlay had focus, move focus to next visible or preFocus
                if self.focusedComponent is component:
                    topVisible = self.getTopmostVisibleOverlay()
                    self.setFocus(topVisible.component if topVisible else entry.preFocus)
            else:
                # Restore focus to this overlay when showing (if it's actually visible)
                if not (options or {}).get("nonCapturing") and self.isOverlayVisible(entry):
                    self.focusOrderCounter += 1
                    entry.focusOrder = self.focusOrderCounter
                    self.setFocus(component)
            self.requestRender()

        def isHidden() -> bool:
            return entry.hidden

        def focus() -> None:
            if not any(overlay is entry for overlay in self.overlayStack) or not self.isOverlayVisible(entry):
                return
            self.focusOrderCounter += 1
            entry.focusOrder = self.focusOrderCounter
            self.setFocus(component)
            self.requestRender()

        def unfocus(unfocusOptions: OverlayUnfocusOptions | None = None) -> None:
            isFocused = self.focusedComponent is component
            restoreState = self.overlayFocusRestore
            hasPendingRestore = restoreState["status"] != "inactive" and restoreState["overlay"] is entry
            if not isFocused and not hasPendingRestore:
                return
            if (
                restoreState["status"] == "blocked"
                and restoreState["overlay"] is entry
                and self.focusedComponent is restoreState["blockedBy"]
            ):
                if unfocusOptions:
                    self.overlayFocusRestore = {
                        "status": "blocked",
                        "overlay": entry,
                        "blockedBy": restoreState["blockedBy"],
                        "resume": {"status": "focus-target", "target": unfocusOptions["target"]},
                    }
                else:
                    self.clearOverlayFocusRestore()
                self.requestRender()
                return
            self.clearOverlayFocusRestoreFor(entry)
            if isFocused or unfocusOptions:
                topVisible = self.getTopmostVisibleOverlay()
                fallbackTarget = topVisible.component if topVisible and topVisible is not entry else entry.preFocus
                self.setFocus(unfocusOptions["target"] if unfocusOptions else fallbackTarget)
            self.requestRender()

        def isFocused() -> bool:
            return self.focusedComponent is component

        def getBounds() -> OverlayBounds | None:
            if (
                not any(overlay is entry for overlay in self.overlayStack)
                or not self.isOverlayVisible(entry)
                or not entry.bounds
            ):
                return None
            return {**entry.bounds}

        # MISAKA fork: the object literal of closures is a namespace of them.
        return cast(
            OverlayHandle,
            SimpleNamespace(
                hide=hide,
                setHidden=setHidden,
                isHidden=isHidden,
                focus=focus,
                unfocus=unfocus,
                isFocused=isFocused,
                getBounds=getBounds,
            ),
        )

    def hideOverlay(self) -> None:
        """Hide the topmost overlay and restore previous focus."""
        overlay = self.overlayStack[len(self.overlayStack) - 1] if self.overlayStack else None
        if not overlay:
            return
        self.clearOverlayFocusRestoreFor(overlay)
        self.retargetOverlayPreFocus(overlay)
        self.overlayStack.pop()
        if self.focusedComponent is overlay.component:
            # Find topmost visible overlay, or fall back to preFocus
            topVisible = self.getTopmostVisibleOverlay()
            self.setFocus(topVisible.component if topVisible else overlay.preFocus)
        if len(self.overlayStack) == 0:
            self.hideTerminalCursor()
        self.requestRender()

    def hideTerminalCursor(self) -> None:
        """Hide the cursor while running. After stop(), the shell owns the cursor and it must stay visible."""
        if not self.stopped:
            self.terminal.hideCursor()

    def hasOverlay(self) -> bool:
        """Check if there are any visible overlays"""
        return any(self.isOverlayVisible(o) for o in self.overlayStack)

    # MISAKA fork (D1): upstream's `isOverlayFocused`, `resolveMouseFocusTarget` and `dispatchMouseToOverlay` follow
    # here.

    def isOverlayVisible(self, entry: OverlayStackEntry) -> bool:
        """Check if an overlay entry is currently visible"""
        if entry.hidden:
            return False
        visible = (entry.options or {}).get("visible")
        if visible:
            return visible(self.terminal.columns, self.terminal.rows)
        return True

    def getTopmostVisibleOverlay(self) -> OverlayStackEntry | None:
        """Find the visual-frontmost visible capturing overlay, if any"""
        topmost: OverlayStackEntry | None = None
        for overlay in self.overlayStack:
            if (overlay.options or {}).get("nonCapturing") or not self.isOverlayVisible(overlay):
                continue
            if not topmost or overlay.focusOrder > topmost.focusOrder:
                topmost = overlay
        return topmost

    def invalidate(self) -> None:
        for root in self.getMountedRoots():
            root.invalidate()
        for overlay in self.overlayStack:
            overlay.component.invalidate()

    def start(self) -> None:
        self._capture_loop()
        self.stopped = False
        self.beforeTerminalStart()
        self.terminal.start(
            lambda data: self.handleTerminalInput(data),
            lambda: self.requestRender(),
        )
        self.afterTerminalStart()
        self.terminal.hideCursor()
        if self.terminalColorSchemeNotificationsEnabled:
            self.terminal.write("\x1b[?2031h")
        self.queryCellSize()
        self.requestRender()

    def addInputListener(self, listener: TuiInputListener) -> Callable[[], None]:
        self.inputListeners[listener] = None

        def remove() -> None:
            self.inputListeners.pop(listener, None)

        return remove

    def removeInputListener(self, listener: TuiInputListener) -> None:
        self.inputListeners.pop(listener, None)

    def onTerminalColorSchemeChange(self, listener: Callable[[TerminalColorScheme], None]) -> Callable[[], None]:
        self.terminalColorSchemeListeners[listener] = None

        def remove() -> None:
            self.terminalColorSchemeListeners.pop(listener, None)

        return remove

    def setTerminalColorSchemeNotifications(self, enabled: bool) -> None:
        if self.terminalColorSchemeNotificationsEnabled == enabled:
            return
        self.terminalColorSchemeNotificationsEnabled = enabled
        if not self.stopped:
            self.terminal.write("\x1b[?2031h" if enabled else "\x1b[?2031l")

    def queryCellSize(self) -> None:
        # Only query if terminal supports images (cell size is only used for image rendering)
        if not getCapabilities().images:
            return
        # Query terminal for cell size in pixels: CSI 16 t
        # Response format: CSI 6 ; height ; width t
        self.terminal.write("\x1b[16t")

    def stop(self, options: TuiStopOptions | None = None) -> None:
        if options is None:
            options = {}
        # MISAKA fork: on the loop that owns rendering.
        if self._queue_to_owner(lambda: self.stop(options)):
            return
        self.stopped = True
        self.cancelRenderTimer()
        if self.terminalColorSchemeNotificationsEnabled:
            self.terminal.write("\x1b[?2031l")
        self.beforeTerminalStop(options)
        self.terminal.showCursor()
        self.terminal.stop()
        self.afterTerminalStop(options)

    def renderNow(self, force: bool = False) -> None:
        # MISAKA fork: on the loop that owns rendering.
        if self._queue_to_owner(lambda: self.renderNow(force)):
            return
        if force:
            self.resetRenderState()
        self.renderRequested = False
        self.cancelRenderTimer()
        self.lastRenderAt = performanceNow()
        self.doRender()

    def requestRender(self, force: bool = False) -> None:
        # MISAKA fork: requests from other threads go to the loop that owns rendering; once that loop has closed
        # (shutdown), nothing can be scheduled on it any more.
        self._capture_loop()
        if self._loop is not None and self._loop.is_closed():
            return
        if self._queue_to_owner(lambda: self.requestRender(force)):
            return
        if force:
            self.resetRenderState()
            self.requestImmediateRender()
            return
        if self.renderRequested:
            return
        self.renderRequested = True
        self._schedule_next_tick(lambda: self.scheduleRender())

    def requestImmediateRender(self) -> None:
        self.cancelRenderTimer()
        self.renderRequested = True
        if self.immediateRenderScheduled:
            return
        self.immediateRenderScheduled = True

        def tick() -> None:
            self.immediateRenderScheduled = False
            if self.stopped or not self.renderRequested:
                return
            # A previously queued scheduleRender() can create a timer before this
            # callback runs. User input must preempt that throttled frame.
            self.cancelRenderTimer()
            self.renderRequested = False
            self.lastRenderAt = performanceNow()
            self.doRender()

        self._schedule_next_tick(tick)

    def cancelRenderTimer(self) -> None:
        if not self.renderTimer:
            return
        self.renderTimer.cancel()
        self.renderTimer = None

    def scheduleRender(self) -> None:
        if self.stopped or self.renderTimer or not self.renderRequested:
            return
        elapsed = performanceNow() - self.lastRenderAt
        # MISAKA fork: pi waits MIN_RENDER_INTERVAL_MS from the start of the last frame, and in Node that is enough: a
        # network chunk's promise continuations all run before the next timer. asyncio has no such priority; every
        # await is a loop callback queued alongside the render timer. A frame slower than the interval (a long reply
        # is laid out whole every frame) then made frames run back to back, a streaming reply advanced about one event
        # per frame, fell minutes behind the provider and was cut off ("Connection error.", GitHub issue #6). So a
        # frame also waits as long as the last one took: painting gets at most half the loop. `elapsed` runs from the
        # frame's start, and the wait is counted from its end: a frame that took longer than its CPU (a write held
        # up, threads contending for the GIL) still leaves the loop as long as its CPU took.
        delay = max(0.0, self.MIN_RENDER_INTERVAL_MS - elapsed, self.lastRenderWallMs + self.lastRenderCostMs - elapsed)

        def onTimeout() -> None:
            # MISAKA fork: a thread timer cancelled after it fired still runs; only the current one may render.
            if self.renderTimer is not timer:
                return
            self.renderTimer = None
            if self.stopped or not self.renderRequested:
                return
            self.renderRequested = False
            self.lastRenderAt = performanceNow()
            self.doRender()
            if self.renderRequested:
                self.scheduleRender()

        timer: asyncio.TimerHandle | threading.Timer
        if self._loop is not None:
            if self._loop.is_closed():
                return
            timer = self._loop.call_later(delay / 1000, self._run_callback, onTimeout)
        else:
            timer = threading.Timer(delay / 1000, lambda: self._run_callback(onTimeout))
            timer.daemon = True
        self.renderTimer = timer
        if isinstance(timer, threading.Timer):
            timer.start()

    def handleTerminalInput(self, data: str) -> None:
        # MISAKA fork (D11): upstream consumes queryTerminalColors' replies here; MISAKA's OSC 11 probe takes its.
        if self.consumeOsc11BackgroundResponse(data):
            return
        if self.consumeTerminalColorSchemeReport(data):
            return

        if len(self.inputListeners) > 0:
            current = data
            for listener in liveKeys(self.inputListeners):
                result = listener(current)
                if result and result.get("consume"):
                    return
                if result and result.get("data") is not None:
                    current = result["data"]
            if len(current) == 0:
                return
            data = current

        # Consume terminal cell size responses without blocking unrelated input.
        if self.consumeCellSizeResponse(data):
            return

        # Global debug key handler (Shift+Ctrl+D)
        if matchesKey(data, "shift+ctrl+d") and self.onDebug:
            self.onDebug()
            return

        # If focused component is an overlay, verify it's still visible
        # (visibility can change due to terminal resize or visible() callback)
        focusedOverlay = next((o for o in self.overlayStack if o.component is self.focusedComponent), None)
        if focusedOverlay and not self.isOverlayVisible(focusedOverlay):
            # Focused overlay is no longer visible, redirect to topmost visible overlay
            topVisible = self.getTopmostVisibleOverlay()
            if topVisible:
                self.setFocus(topVisible.component)
            else:
                self.setFocusInternal({"component": focusedOverlay.preFocus, "overlayFocusRestore": "preserve"})

        focusIsOverlay = any(o.component is self.focusedComponent for o in self.overlayStack)
        if not focusIsOverlay:
            restoreState = self.getVisibleOverlayFocusRestore()
            if restoreState["status"] == "eligible":
                self.setFocus(restoreState["overlay"].component)
            elif restoreState["status"] == "blocked" and restoreState["blockedBy"] is not self.focusedComponent:
                resume = restoreState["resume"]
                if resume["status"] == "restore-overlay":
                    self.setFocus(restoreState["overlay"].component)
                else:
                    self.clearOverlayFocusRestore()
                    self.setFocus(resume["target"])

        # Pass input to focused component (including Ctrl+C)
        # The focused component can decide how to handle Ctrl+C
        handleInput = getattr(self.focusedComponent, "handleInput", None)
        if handleInput:
            # Filter out key release events unless component opts in
            if isKeyRelease(data) and not getattr(self.focusedComponent, "wantsKeyRelease", False):
                return
            handleInput(data)
            # Keyboard input is latency-sensitive. Avoid the throttled timer path,
            # where even setTimeout(0) can take a full 16 ms tick on Windows.
            self.requestImmediateRender()

    # MISAKA fork (D11): upstream's `consumeTerminalColorResponse`, `terminalColorQueryResult` and
    # `completeTerminalColorQuery` follow here.

    def consumeTerminalColorSchemeReport(self, data: str) -> bool:
        scheme = parseTerminalColorSchemeReport(data)
        if not scheme:
            return False

        for listener in liveKeys(self.terminalColorSchemeListeners):
            listener(scheme)
        return True

    def consumeCellSizeResponse(self, data: str) -> bool:
        # Response format: ESC [ 6 ; height ; width t
        match = js_regexp.match(data, RegExp(r"^\x1b\[6;(\d+);(\d+)t$"))
        if not match:
            return False

        heightPx = int(match[1], 10)
        widthPx = int(match[2], 10)
        if heightPx <= 0 or widthPx <= 0:
            return True

        setCellDimensions({"widthPx": widthPx, "heightPx": heightPx})
        # Invalidate all components so images re-render with correct dimensions.
        self.invalidate()
        self.requestRender()
        return True

    def resolveOverlayLayout(
        self,
        options: OverlayOptions | None,
        overlayHeight: int,
        termWidth: int,
        termHeight: int,
    ) -> dict[str, Any]:
        """Resolve overlay layout from options.
        Returns { width, row, col, maxHeight } for rendering.
        """
        opt: OverlayOptions = options if options is not None else {}

        # Parse margin (clamp to non-negative)
        marginOption = opt.get("margin")
        margin: OverlayMargin = (
            {"top": marginOption, "right": marginOption, "bottom": marginOption, "left": marginOption}
            if isinstance(marginOption, int | float)
            else (marginOption if marginOption is not None else {})
        )
        marginTop = max(0, _orZero(margin.get("top")))
        marginRight = max(0, _orZero(margin.get("right")))
        marginBottom = max(0, _orZero(margin.get("bottom")))
        marginLeft = max(0, _orZero(margin.get("left")))

        # Available space after margins
        availWidth = max(1, termWidth - marginLeft - marginRight)
        availHeight = max(1, termHeight - marginTop - marginBottom)

        # === Resolve width ===
        parsedWidth = parseSizeValue(opt.get("width"), termWidth)
        width = parsedWidth if parsedWidth is not None else min(80, availWidth)
        # Apply minWidth
        if opt.get("minWidth") is not None:
            width = max(width, opt["minWidth"])
        # Clamp to available space
        width = max(1, min(width, availWidth))

        # === Resolve maxHeight ===
        maxHeight = parseSizeValue(opt.get("maxHeight"), termHeight)
        # Clamp to available space
        if maxHeight is not None:
            maxHeight = max(1, min(maxHeight, availHeight))

        # Effective overlay height (may be clamped by maxHeight)
        effectiveHeight = min(overlayHeight, maxHeight) if maxHeight is not None else overlayHeight

        # === Resolve position ===
        row: int | float
        col: int | float

        optRow = opt.get("row")
        if optRow is not None:
            if isinstance(optRow, str):
                # Percentage: 0% = top, 100% = bottom (overlay stays within bounds)
                match = js_regexp.match(optRow, RegExp(r"^(\d+(?:\.\d+)?)%$"))
                if match:
                    maxRow = max(0, availHeight - effectiveHeight)
                    percent = float(match[1]) / 100
                    row = marginTop + math.floor(maxRow * percent)
                else:
                    # Invalid format, fall back to center
                    row = self.resolveAnchorRow("center", effectiveHeight, availHeight, marginTop)
            else:
                # Absolute row position
                row = optRow
        else:
            # Anchor-based (default: center)
            anchor = opt.get("anchor") or "center"
            row = self.resolveAnchorRow(anchor, effectiveHeight, availHeight, marginTop)

        optCol = opt.get("col")
        if optCol is not None:
            if isinstance(optCol, str):
                # Percentage: 0% = left, 100% = right (overlay stays within bounds)
                match = js_regexp.match(optCol, RegExp(r"^(\d+(?:\.\d+)?)%$"))
                if match:
                    maxCol = max(0, availWidth - width)
                    percent = float(match[1]) / 100
                    col = marginLeft + math.floor(maxCol * percent)
                else:
                    # Invalid format, fall back to center
                    col = self.resolveAnchorCol("center", width, availWidth, marginLeft)
            else:
                # Absolute column position
                col = optCol
        else:
            # Anchor-based (default: center)
            anchor = opt.get("anchor") or "center"
            col = self.resolveAnchorCol(anchor, width, availWidth, marginLeft)

        # Apply offsets
        if opt.get("offsetY") is not None:
            row += opt["offsetY"]
        if opt.get("offsetX") is not None:
            col += opt["offsetX"]

        # Clamp to terminal bounds (respecting margins)
        row = max(marginTop, min(row, termHeight - marginBottom - effectiveHeight))
        col = max(marginLeft, min(col, termWidth - marginRight - width))

        return {"width": width, "row": row, "col": col, "maxHeight": maxHeight}

    def resolveAnchorRow(self, anchor: OverlayAnchor, height: float, availHeight: float, marginTop: float) -> float:
        match anchor:
            case "top-left" | "top-center" | "top-right":
                return marginTop
            case "bottom-left" | "bottom-center" | "bottom-right":
                return marginTop + availHeight - height
            case "left-center" | "center" | "right-center":
                return marginTop + math.floor((availHeight - height) / 2)
        return marginTop + math.floor((availHeight - height) / 2)

    def resolveAnchorCol(self, anchor: OverlayAnchor, width: float, availWidth: float, marginLeft: float) -> float:
        match anchor:
            case "top-left" | "left-center" | "bottom-left":
                return marginLeft
            case "top-right" | "right-center" | "bottom-right":
                return marginLeft + availWidth - width
            case "top-center" | "center" | "bottom-center":
                return marginLeft + math.floor((availWidth - width) / 2)
        return marginLeft + math.floor((availWidth - width) / 2)

    def compositeOverlays(self, lines: list[str], termWidth: int, termHeight: int) -> list[str]:
        """Composite all overlays into content lines (sorted by focusOrder, higher = on top)."""
        if len(self.overlayStack) == 0:
            # MISAKA fork (D1): upstream resets its rendered overlay layouts here.
            return lines
        result = [*lines]

        for entry in self.overlayStack:
            entry.bounds = None

        # Pre-render all visible overlays and calculate positions
        rendered: list[dict[str, Any]] = []
        minLinesNeeded = len(result)

        visibleEntries = [e for e in self.overlayStack if self.isOverlayVisible(e)]
        visibleEntries.sort(key=lambda e: e.focusOrder)
        for entry in visibleEntries:
            component, options = entry.component, entry.options

            # Get layout with height=0 first to determine width and maxHeight
            # (width and maxHeight don't depend on overlay height)
            layout = self.resolveOverlayLayout(options, 0, termWidth, termHeight)
            width, maxHeight = layout["width"], layout["maxHeight"]

            # Render component at calculated width
            overlayLines = self.resolveFakeCursors(component.render(width))

            # Apply maxHeight if specified
            if maxHeight is not None and len(overlayLines) > maxHeight:
                overlayLines = overlayLines[:maxHeight]

            # Get final row/col with actual overlay height
            finalLayout = self.resolveOverlayLayout(options, len(overlayLines), termWidth, termHeight)
            row, col = finalLayout["row"], finalLayout["col"]
            entry.bounds = {"row": row, "col": col, "width": width, "height": len(overlayLines)}

            rendered.append({"entry": entry, "overlayLines": overlayLines, "row": row, "col": col, "w": width})
            minLinesNeeded = max(minLinesNeeded, row + len(overlayLines))
        # MISAKA fork (D1): upstream records the rendered overlay layouts (for mouse dispatch) here.

        # Pad to at least terminal height so overlays have screen-relative positions.
        # Excludes maxLinesRendered: the historical high-water mark caused self-reinforcing
        # inflation that pushed content into scrollback on terminal widen.
        workingHeight = max(len(result), termHeight, minLinesNeeded)

        # Extend result with empty lines if content is too short for overlay placement or working area
        while len(result) < workingHeight:
            result.append("")

        viewportStart = max(0, workingHeight - termHeight)

        # Composite each overlay
        for item in rendered:
            overlayLines, row, col, w = item["overlayLines"], item["row"], item["col"], item["w"]
            for i in range(len(overlayLines)):
                idx = viewportStart + row + i
                if idx >= 0 and idx < len(result):
                    # Defensive: truncate overlay line to declared width before compositing
                    # (components should already respect width, but this ensures it)
                    truncatedOverlayLine = (
                        sliceByColumn(overlayLines[i], 0, w, True) if visibleWidth(overlayLines[i]) > w else overlayLines[i]
                    )
                    result[idx] = self.compositeLineAt(result[idx], truncatedOverlayLine, col, w, termWidth)

        return result

    def applyLineResets(self, lines: list[str]) -> list[str]:
        reset = SEGMENT_RESET
        for i in range(len(lines)):
            line = lines[i]
            if not isImageLine(line):
                lines[i] = normalizeTerminalOutput(line) + reset
        return lines

    def compositeLineAt(self, baseLine: str, overlayLine: str, startCol: int, overlayWidth: int, totalWidth: int) -> str:
        return compositeTuiLine(baseLine, overlayLine, startCol, overlayWidth, totalWidth)

    def extractCursorPosition(self, lines: list[str], height: int) -> dict[str, int] | None:
        """Find and extract cursor position from rendered lines.
        Searches for CURSOR_MARKER, calculates its position, and strips it from the output.
        Only scans the bottom terminal height lines (visible viewport).
        @param lines - Rendered lines to search
        @param height - Terminal height (visible viewport size)
        @returns Cursor position { row, col } or null if no marker found
        """
        # Only scan the bottom `height` lines (visible viewport)
        viewportTop = max(0, len(lines) - height)
        for row in range(len(lines) - 1, viewportTop - 1, -1):
            line = lines[row]
            markerIndex = line.find(CURSOR_MARKER)
            if markerIndex != -1:
                # Calculate visual column (width of text before marker)
                beforeMarker = line[:markerIndex]
                col = visibleWidth(beforeMarker)

                # Strip marker from the line
                lines[row] = line[:markerIndex] + line[markerIndex + len(CURSOR_MARKER) :]

                return {"row": row, "col": col}
        return None

    def resolveFakeCursors(self, lines: list[str]) -> list[str]:
        """Resolve fake cursor markers to reverse video. When the hardware cursor is shown, a fake cursor
        directly after CURSOR_MARKER is dropped because the terminal cursor is drawn there. Fake cursors
        of components without focus have no CURSOR_MARKER and stay visible.
        """

        def resolve(line: str) -> str:
            if FAKE_CURSOR_START not in line:
                return line
            resolved = js_regexp.replace(line, FOCUSED_FAKE_CURSOR, "$1$2") if self.showHardwareCursor else line
            return resolved.replace(FAKE_CURSOR_START, "\x1b[7m").replace(FAKE_CURSOR_END, "\x1b[27m")

        return [resolve(line) for line in lines]

    # --- MISAKA fork (D11): the background probe in place of `queryTerminalColors` ----------------------------------

    async def queryTerminalBackgroundColor(self, timeoutMs: int = 1000) -> RgbColor | None:
        """Ask the terminal for its background color (OSC 11): ``None`` on timeout or an unparseable reply. MISAKA's
        light/dark palette follows it."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RgbColor | None] = loop.create_future()
        query: dict[str, Any] = {"settled": False, "future": future, "timer": None}

        def onTimeout() -> None:
            if query["settled"]:
                return
            query["settled"] = True
            if not future.done():
                future.set_result(None)

        query["timer"] = loop.call_later(timeoutMs / 1000, onTimeout)
        self.pendingOsc11BackgroundQueries.append(query)
        self.pendingOsc11BackgroundReplies += 1
        self.terminal.write("\x1b]11;?\x07")
        return await future

    def consumeOsc11BackgroundResponse(self, data: str) -> bool:
        if self.pendingOsc11BackgroundReplies <= 0:
            return False
        reply = parseOscColorResponse(data)
        if reply is None or reply["target"] != "background":
            return False
        self.pendingOsc11BackgroundReplies -= 1
        query = self.pendingOsc11BackgroundQueries.pop(0) if self.pendingOsc11BackgroundQueries else None
        if query and not query["settled"]:
            query["settled"] = True
            timer = query.get("timer")
            if timer is not None:
                timer.cancel()
            future = query["future"]
            if not future.done():
                future.set_result(reply["rgb"])
        return True

    # --- MISAKA fork: the loop that owns rendering --------------------------------------------------------------------

    def _capture_loop(self) -> None:
        if self._loop is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # synchronous embedders can still render without an event loop
        self._loop_thread = threading.get_ident()

    def _queue_to_owner(self, callback: Callable[[], None]) -> bool:
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            return False
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._run_callback, callback)
            except RuntimeError:
                pass  # the owning loop closed while an animation was queuing its last tick
        return True

    def _run_callback(self, callback: Callable[[], None]) -> None:
        if self._queue_to_owner(callback):
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - forward the original exception to the installed crash handler
            # Preserve InteractiveMode's uncaught-crash reporting/cleanup. asyncio would
            # otherwise only log the callback error and leave a stopped terminal waiting.
            sys.excepthook(*sys.exc_info())

    def _schedule_next_tick(self, callback: Callable[[], None]) -> None:
        """``process.nextTick``."""
        if self._loop is not None:
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._run_callback, callback)
            return
        timer = threading.Timer(0, lambda: self._run_callback(callback))
        timer.daemon = True
        timer.start()


def _orZero(value: float | None) -> float:
    """``value ?? 0``."""
    return value if value is not None else 0


__all__ = [
    "CURSOR_MARKER",
    "FAKE_CURSOR_END",
    "FAKE_CURSOR_START",
    "SEGMENT_RESET",
    "TUI",
    "Component",
    "Container",
    "Focusable",
    "OverlayAnchor",
    "OverlayBounds",
    "OverlayHandle",
    "OverlayMargin",
    "OverlayOptions",
    "OverlayUnfocusOptions",
    "SizeValue",
    "TuiBase",
    "TuiInputListener",
    "TuiInputListenerResult",
    "TuiStopOptions",
    "compositeTuiLine",
    "isFocusable",
    "renderFakeCursor",
    "visibleWidth",
]
