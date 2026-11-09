"""Translated from pi-tui's ``packages/tui/src/terminal.ts`` (v1.1.0).

MISAKA forks, each marked where it happens:

- Node's ``process.stdin``/``process.stdout`` are the ``stdin``/``stdout`` attributes; ``process.stdout.write`` is
  ``write``, which flushes (Node writes a TTY synchronously, Python buffers).
- Raw mode follows libuv's ``UV_TTY_MODE_RAW``: termios on POSIX (output post-processing kept, unread input
  flushed on exit), the console modes on Windows (``misaka.utils.win_console``).
- ``process.stdin.on("data")`` is a reader on the event loop (POSIX) or a console input thread whose input is routed
  to the loop (Windows); the reader goes away at end of input. ``stdout.on("resize")`` is SIGWINCH on POSIX and the
  console reader's resize event on Windows. Signal and thread callbacks run on the event loop thread, as Node's do.
- ``setTimeout``/``setInterval`` are timers on the event loop.
- The escape timeout comes from settings (``tui.esc_timeout_ms``), not an environment variable, and a pane of
  MISAKA's panel gets the relayed-transport window ssh gets.
- ``PI_PROGRAM_STATUS`` and ``PI_TUI_WRITE_LOG`` are ``MISAKA_PROGRAM_STATUS`` and ``MISAKA_TUI_WRITE_LOG``.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from misaka.ui.tui.keys import setKittyProtocolActive
from misaka.ui.tui.native_modifiers import isNativeModifierPressed
from misaka.ui.tui.native_platform import getNativePlatformHelper
from misaka.ui.tui.program_status import (
    PROGRAM_STATUS_QUERY,
    ProgramStatus,
    formatProgramStatus,
    isProgramStatusReply,
)
from misaka.ui.tui.stdin_buffer import StdinBuffer
from misaka.utils import js_regexp
from misaka.utils.js import jsNumber
from misaka.utils.js_regexp import RegExp

try:
    import termios
except ImportError:  # pragma: no cover - Windows import path.
    termios = None  # type: ignore[assignment]

TERMINAL_PROGRESS_KEEPALIVE_MS = 1000
TERMINAL_PROGRESS_ACTIVE_SEQUENCE = "\x1b]9;4;3\x07"
TERMINAL_PROGRESS_CLEAR_SEQUENCE = "\x1b]9;4;0\x07"
NATIVE_SHIFT_ENTER_SEQUENCE = "\x1b[13;2u"
DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS = 7
KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_MS = 150
KITTY_KEYBOARD_PROTOCOL_QUERY = f"\x1b[>{DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS}u\x1b[?u"
DEVICE_ATTRIBUTES_QUERY = "\x1b[c"

# `{ type: "kitty-flags"; flags: number } | { type: "device-attributes" }`
type KeyboardProtocolNegotiationSequence = dict[str, Any]


def parseKeyboardProtocolNegotiationSequence(sequence: str) -> KeyboardProtocolNegotiationSequence | None:
    kittyFlags = js_regexp.match(sequence, RegExp(r"^\x1b\[\?(\d+)u$"))
    if kittyFlags:
        return {"type": "kitty-flags", "flags": int(kittyFlags[1], 10)}
    if RegExp(r"^\x1b\[\?[\d;]*c$").test(sequence):
        return {"type": "device-attributes"}
    return None


def isKeyboardProtocolNegotiationSequencePrefix(sequence: str) -> bool:
    return sequence == "\x1b[" or RegExp(r"^\x1b\[\?[\d;]*$").test(sequence)


def isAppleTerminalSession() -> bool:
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def refreshTerminalDimensions() -> None:
    """Refresh terminal dimensions on POSIX platforms by sending SIGWINCH to this process.
    Best-effort: some environments (restricted seccomp or LSM policies) return EACCES
    for `kill(2)`; in that case the dimensions refresh is skipped rather than crashing.
    """
    if sys.platform == "win32" or os.getpid() <= 0:
        return
    try:
        os.kill(os.getpid(), signal.SIGWINCH)
    except Exception:  # noqa: BLE001, S110 - signal delivery not permitted in this environment; ignore.
        pass


def normalizeNativeShiftEnterInput(data: str, shouldDetectNativeShiftEnter: bool, isShiftPressed: bool) -> str:
    if shouldDetectNativeShiftEnter and data == "\r" and isShiftPressed:
        return NATIVE_SHIFT_ENTER_SEQUENCE
    return data


def normalizeAppleTerminalInput(data: str, isAppleTerminal: bool, isShiftPressed: bool) -> str:
    return normalizeNativeShiftEnterInput(data, isAppleTerminal, isShiftPressed)


class Terminal(Protocol):
    """Minimal terminal interface for TUI"""

    # Start the terminal with input and resize handlers
    def start(self, onInput: Callable[[str], None], onResize: Callable[[], None]) -> None: ...

    # Stop the terminal and restore state
    def stop(self) -> None: ...

    async def drainInput(self, maxMs: int = 1000, idleMs: int = 50) -> None:
        """Drain stdin before exiting to prevent Kitty key release events from
        leaking to the parent shell over slow SSH connections.
        @param maxMs - Maximum time to drain (default: 1000ms)
        @param idleMs - Exit early if no input arrives within this time (default: 50ms)
        """
        ...

    # Write output to terminal
    def write(self, data: str) -> None: ...

    # Get terminal dimensions
    @property
    def columns(self) -> int: ...
    @property
    def rows(self) -> int: ...

    # Whether Kitty keyboard protocol is active
    @property
    def kittyProtocolActive(self) -> bool: ...

    # Cursor positioning (relative to current position)
    def moveBy(self, lines: int) -> None: ...  # Move cursor up (negative) or down (positive) by N lines

    # Cursor visibility
    def hideCursor(self) -> None: ...  # Hide the cursor
    def showCursor(self) -> None: ...  # Show the cursor

    # Clear operations
    def clearLine(self) -> None: ...  # Clear current line
    def clearFromCursor(self) -> None: ...  # Clear from cursor to end of screen
    def clearScreen(self) -> None: ...  # Clear entire screen and move cursor to (0,0)

    # Title operations
    def setTitle(self, title: str) -> None: ...  # Set terminal window title

    # Progress indicator (OSC 9;4)
    def setProgress(self, active: bool) -> None: ...

    def setProgramStatus(self, status: ProgramStatus) -> None:
        """Report what the program is doing (OSC 7501). Sent only to terminals that support it; the latest
        status is re-sent when support is confirmed or the terminal restarts.
        """
        ...


DEFAULT_ESCAPE_TIMEOUT_MS = 10
DEFAULT_SSH_ESCAPE_TIMEOUT_MS = 100


def resolveEscapeTimeoutMs(env: Any = None) -> float:
    """Resolve how long to wait for the rest of an escape sequence before
    dispatching a lone ESC as the Escape key. Legacy Alt+key input is ESC plus
    another byte, so high-latency transports need a longer reassembly window.

    MISAKA fork: the configured window is the ``tui.esc_timeout_ms`` setting (pi: ``PI_TUI_ESC_TIMEOUT``), and a
    pane of MISAKA's panel (``MISAKA_NET_PANE``, stamped on every pane the daemon spawns) gets the ssh window: its
    keystrokes travel host terminal -> panel -> socket -> daemon -> pty, which splits reads and adds a round trip.
    With the bare 10 ms a bracketed paste whose opener arrived as a lone ESC turned into the Escape key (a pasted
    OAuth redirect URL cancelled the login).
    """
    from misaka.config.product import setting

    if env is None:
        env = os.environ
    configured = jsNumber(setting("tui", "esc_timeout_ms", 0, float))
    if math.isfinite(configured) and configured > 0:
        return configured
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("MISAKA_NET_PANE"):
        return DEFAULT_SSH_ESCAPE_TIMEOUT_MS
    return DEFAULT_ESCAPE_TIMEOUT_MS


def jsTruthyNumber(value: float) -> bool:
    """MISAKA fork: a JavaScript number's truthiness (``NaN`` and 0 are false; Python's ``NaN`` is true)."""
    return bool(value) and not math.isnan(value)


def _writeLogPath() -> str:
    """``writeLogPath``: ``MISAKA_TUI_WRITE_LOG`` names a file, or a directory that gets a file per process."""
    env = os.environ.get("MISAKA_TUI_WRITE_LOG") or ""
    if not env:
        return ""
    # `fs.statSync(env).isDirectory()`: anything that is not an existing directory is used as-is (file path).
    if os.path.isdir(env):
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        return os.path.join(env, f"tui-{ts}-{os.getpid()}.log")
    return env


class ProcessTerminal:
    """Real terminal using process.stdin/stdout"""

    def __init__(self) -> None:
        self.wasRaw = False
        self.inputHandler: Callable[[str], None] | None = None
        self.resizeHandler: Callable[[], None] | None = None
        self._kittyProtocolActive = False
        self._modifyOtherKeysActive = False
        self.keyboardProtocolPushed = False
        # DA1 replies owed to keyboard protocol queries. Later DA1 replies answer other queries and are forwarded.
        self.pendingKeyboardProtocolDeviceAttributes = 0
        self.keyboardProtocolNegotiationBuffer = ""
        self.keyboardProtocolBufferFlushTimer: asyncio.TimerHandle | None = None
        self.stdinBuffer: StdinBuffer | None = None
        self.stdinDataHandler: Callable[[str | bytes], None] | None = None
        self.progressInterval: asyncio.TimerHandle | None = None
        # Latest program status, kept across stop() so start() can report it again.
        self.programStatus: ProgramStatus | None = None
        # Whether the terminal confirmed OSC 7501 support since the last start(), or `PI_PROGRAM_STATUS=1`.
        self.programStatusSupported = False
        # The support query was sent and its DA1 sentinel has not arrived yet.
        self.programStatusQueryPending = False
        self.writeLogPath = _writeLogPath()

        # MISAKA fork: the Node runtime pieces (see the module docstring).
        self.stdin: Any = sys.stdin
        self.stdout: Any = sys.stdout
        self.loop: asyncio.AbstractEventLoop | None = None
        self._previousSigwinchHandler: Any | None = None
        self._previousTermiosSettings: list[Any] | None = None
        self._previousConsoleModes: Any | None = None
        self._consoleInput: Any | None = None
        self._lastStdinActivityMs = self._now_ms()
        self._readerInstalled = False

    @property
    def kittyProtocolActive(self) -> bool:
        return self._kittyProtocolActive

    @property
    def modifyOtherKeysActive(self) -> bool:
        return self._modifyOtherKeysActive

    def start(self, onInput: Callable[[str], None], onResize: Callable[[], None]) -> None:
        self.inputHandler = onInput
        self.resizeHandler = onResize
        # MISAKA fork: capture the loop that timer, signal and reader callbacks run on.
        self._event_loop()
        self._lastStdinActivityMs = self._now_ms()

        # Save previous state and enable raw mode
        # MISAKA fork: `stdin.isRaw`/`setRawMode(true)`/`setEncoding("utf8")`/`resume()`.
        self.wasRaw = False
        self._enter_raw_mode()

        # Enable bracketed paste mode - terminal will wrap pastes in \x1b[200~ ... \x1b[201~
        self.write("\x1b[?2004h")

        # Set up resize handler immediately
        self._install_resize_handler()

        # Refresh terminal dimensions - they may be stale after suspend/resume
        # (SIGWINCH is lost while process is stopped). Unix only, best-effort.
        refreshTerminalDimensions()

        # On Windows, enable ENABLE_VIRTUAL_TERMINAL_INPUT so the console sends
        # VT escape sequences (e.g. \x1b[Z for Shift+Tab) instead of raw console
        # events that lose modifier information. Must run AFTER setRawMode(true)
        # since that resets console mode flags.
        self.enableWindowsVTInput()

        # Query Kitty keyboard protocol and fall back to modifyOtherKeys when DA confirms no Kitty response.
        # See: https://sw.kovidgoyal.net/kitty/keyboard-protocol/
        self.queryAndEnableKittyProtocol()

    def setupStdinBuffer(self) -> None:
        """Set up StdinBuffer to split batched input into individual sequences.
        This ensures components receive single events, making matchesKey/isKeyRelease work correctly.

        Also watches for Kitty protocol response and enables it when detected.
        This is done here (after stdinBuffer parsing) rather than on raw stdin
        to handle the case where the response arrives split across multiple events.
        """
        self.stdinBuffer = StdinBuffer({"escapeTimeout": resolveEscapeTimeoutMs()}, loop=self.loop)

        # Forward individual sequences to the input handler
        def onData(sequence: str) -> None:
            if isProgramStatusReply(sequence):
                if self.programStatusQueryPending:
                    self.programStatusQueryPending = False
                    self.programStatusSupported = True
                    self.writeProgramStatus()
                return
            negotiation = self.readKeyboardProtocolNegotiationSequence(sequence)
            if negotiation == "pending":
                self.scheduleKeyboardProtocolNegotiationBufferFlush()
                return  # Wait briefly for the rest of a split Kitty response.
            if negotiation and self.handleKeyboardProtocolNegotiationSequence(negotiation["parsed"]):
                return

            self.forwardInputSequence(negotiation["sequence"] if negotiation else sequence)

        self.stdinBuffer.on("data", onData)

        # Re-wrap paste content with bracketed paste markers for existing editor handling
        def onPaste(content: str) -> None:
            if self.inputHandler:
                self.inputHandler(f"\x1b[200~{content}\x1b[201~")

        self.stdinBuffer.on("paste", onPaste)

        # Handler that pipes stdin data through the buffer
        def stdinDataHandler(data: str | bytes) -> None:
            assert self.stdinBuffer is not None
            self.stdinBuffer.process(data)

        self.stdinDataHandler = stdinDataHandler

    def queryAndEnableKittyProtocol(self) -> None:
        """Query terminal for Kitty keyboard protocol support and enable it if available.

        Kitty's progressive enhancement detection requires requesting the desired
        flags before querying them. The trailing DA query is a sentinel supported by
        terminals that do not know Kitty keyboard protocol; receiving DA before a
        Kitty response enables modifyOtherKeys fallback without a startup timeout.

        The requested flags are:
        - 1 = disambiguate escape codes
        - 2 = report event types (press/repeat/release)
        - 4 = report alternate keys (shifted key, base layout key)

        The OSC 7501 program status query shares the DA sentinel: a terminal that supports it replies
        before DA. `PI_PROGRAM_STATUS=1` or `0` skips the query.
        """
        self.setupStdinBuffer()
        # MISAKA fork: `process.stdin.on("data", this.stdinDataHandler)`.
        self._install_reader()
        self.keyboardProtocolPushed = True
        self.pendingKeyboardProtocolDeviceAttributes += 1
        self.clearKeyboardProtocolNegotiationBuffer()
        programStatusOverride = os.environ.get("MISAKA_PROGRAM_STATUS")
        self.programStatusSupported = programStatusOverride == "1"
        self.programStatusQueryPending = programStatusOverride != "1" and programStatusOverride != "0"
        programStatusQuery = PROGRAM_STATUS_QUERY if self.programStatusQueryPending else ""
        self.write(f"{KITTY_KEYBOARD_PROTOCOL_QUERY}{programStatusQuery}{DEVICE_ATTRIBUTES_QUERY}")
        self.writeProgramStatus()

    def handleKeyboardProtocolNegotiationSequence(
        self, negotiationSequence: KeyboardProtocolNegotiationSequence
    ) -> bool:
        self.clearKeyboardProtocolNegotiationBuffer()
        if negotiationSequence["type"] == "device-attributes":
            if self.pendingKeyboardProtocolDeviceAttributes == 0:
                return False
            self.pendingKeyboardProtocolDeviceAttributes -= 1
            # The last owed DA answers the latest query, which got no program status reply first. Earlier DA
            # replies belong to queries from before a restart.
            if self.pendingKeyboardProtocolDeviceAttributes == 0:
                self.programStatusQueryPending = False
        if negotiationSequence["type"] == "kitty-flags":
            if negotiationSequence["flags"] != 0:
                self.disableModifyOtherKeys()
                if not self._kittyProtocolActive:
                    self._kittyProtocolActive = True
                    setKittyProtocolActive(True)
            else:
                self.enableModifyOtherKeys()
            return True

        if not self._kittyProtocolActive:
            self.enableModifyOtherKeys()
        return True

    def readKeyboardProtocolNegotiationSequence(
        self, sequence: str
    ) -> dict[str, Any] | Literal["pending"] | None:
        """Returns the parsed negotiation reply with its full (possibly reassembled) sequence."""
        if self.keyboardProtocolNegotiationBuffer:
            bufferedSequence = self.keyboardProtocolNegotiationBuffer + sequence
            negotiationSequence = parseKeyboardProtocolNegotiationSequence(bufferedSequence)
            if negotiationSequence:
                self.clearKeyboardProtocolNegotiationBuffer()
                return {"parsed": negotiationSequence, "sequence": bufferedSequence}
            if isKeyboardProtocolNegotiationSequencePrefix(bufferedSequence):
                self.setKeyboardProtocolNegotiationBuffer(bufferedSequence)
                return "pending"
            self.flushKeyboardProtocolNegotiationBufferAsInput()

        negotiationSequence = parseKeyboardProtocolNegotiationSequence(sequence)
        if negotiationSequence:
            return {"parsed": negotiationSequence, "sequence": sequence}
        if isKeyboardProtocolNegotiationSequencePrefix(sequence):
            self.setKeyboardProtocolNegotiationBuffer(sequence)
            return "pending"
        return None

    def setKeyboardProtocolNegotiationBuffer(self, sequence: str) -> None:
        self.clearKeyboardProtocolNegotiationBufferFlushTimer()
        self.keyboardProtocolNegotiationBuffer = sequence

    def clearKeyboardProtocolNegotiationBuffer(self) -> None:
        self.clearKeyboardProtocolNegotiationBufferFlushTimer()
        self.keyboardProtocolNegotiationBuffer = ""

    def flushKeyboardProtocolNegotiationBufferAsInput(self) -> None:
        if not self.keyboardProtocolNegotiationBuffer:
            return
        sequence = self.keyboardProtocolNegotiationBuffer
        self.clearKeyboardProtocolNegotiationBuffer()
        self.forwardInputSequence(sequence)

    def scheduleKeyboardProtocolNegotiationBufferFlush(self) -> None:
        if not self.keyboardProtocolNegotiationBuffer or self.keyboardProtocolBufferFlushTimer:
            return

        def onTimeout() -> None:
            self.keyboardProtocolBufferFlushTimer = None
            self.flushKeyboardProtocolNegotiationBufferAsInput()

        self.keyboardProtocolBufferFlushTimer = self._setTimeout(onTimeout, KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_MS)

    def clearKeyboardProtocolNegotiationBufferFlushTimer(self) -> None:
        if not self.keyboardProtocolBufferFlushTimer:
            return
        self.keyboardProtocolBufferFlushTimer.cancel()
        self.keyboardProtocolBufferFlushTimer = None

    def forwardInputSequence(self, sequence: str) -> None:
        if not self.inputHandler:
            return
        shouldDetectNativeShiftEnter = sequence == "\r" and (isAppleTerminalSession() or sys.platform == "win32")
        input_ = normalizeNativeShiftEnterInput(
            sequence,
            shouldDetectNativeShiftEnter,
            shouldDetectNativeShiftEnter and isNativeModifierPressed("shift"),
        )
        self.inputHandler(input_)

    def enableModifyOtherKeys(self) -> None:
        if self._kittyProtocolActive or self._modifyOtherKeysActive:
            return
        self.write("\x1b[>4;2m")
        self._modifyOtherKeysActive = True

    def disableModifyOtherKeys(self) -> None:
        if not self._modifyOtherKeysActive:
            return
        self.write("\x1b[>4;0m")
        self._modifyOtherKeysActive = False

    def enableWindowsVTInput(self) -> None:
        """On Windows, add ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200) to the stdin
        console handle so the terminal sends VT sequences for modified keys
        (e.g. \\x1b[Z for Shift+Tab). Without this, libuv's ReadConsoleInputW
        discards modifier state and Shift+Tab arrives as plain \\t.
        """
        if sys.platform != "win32":
            return
        try:
            helper = getNativePlatformHelper()
            enableVirtualTerminalInput = getattr(helper, "enableVirtualTerminalInput", None)
            if enableVirtualTerminalInput:
                enableVirtualTerminalInput()
        except Exception:  # noqa: BLE001, S110 - native helper not available: Shift+Tab won't be distinguishable from Tab.
            pass

    async def drainInput(self, maxMs: int = 1000, idleMs: int = 50) -> None:
        shouldDisableKittyProtocol = self.keyboardProtocolPushed or self._kittyProtocolActive
        self.clearKeyboardProtocolNegotiationBuffer()
        if shouldDisableKittyProtocol:
            # Disable Kitty keyboard protocol first so any late key releases
            # do not generate new Kitty escape sequences.
            self.write("\x1b[<u")
            self.keyboardProtocolPushed = False
            self._kittyProtocolActive = False
            setKittyProtocolActive(False)
        self.disableModifyOtherKeys()

        previousHandler = self.inputHandler
        self.inputHandler = None

        # MISAKA fork: every read stamps `_lastStdinActivityMs` (upstream adds a temporary "data" listener).
        self._lastStdinActivityMs = self._now_ms()
        endTime = self._now_ms() + maxMs

        try:
            while True:
                now = self._now_ms()
                timeLeft = endTime - now
                if timeLeft <= 0:
                    break
                if now - self._lastStdinActivityMs >= idleMs:
                    break
                await asyncio.sleep(min(idleMs, timeLeft) / 1000)
        finally:
            self.inputHandler = previousHandler

    def stop(self) -> None:
        if self.clearProgressInterval():
            self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)
        # Remove the status while stopped, for example after exit or while suspended. start() reports it again.
        if self.programStatusSupported and self.programStatus:
            self.write(formatProgramStatus({"state": "clear"}))
        self.programStatusSupported = False
        self.programStatusQueryPending = False

        # Disable bracketed paste mode
        self.write("\x1b[?2004l")

        shouldDisableKittyProtocol = self.keyboardProtocolPushed or self._kittyProtocolActive
        self.clearKeyboardProtocolNegotiationBuffer()

        # Disable Kitty keyboard protocol if not already done by drainInput()
        if shouldDisableKittyProtocol:
            self.write("\x1b[<u")
            self.keyboardProtocolPushed = False
            self._kittyProtocolActive = False
            setKittyProtocolActive(False)
        self.disableModifyOtherKeys()

        # Clean up StdinBuffer
        if self.stdinBuffer:
            self.stdinBuffer.destroy()
            self.stdinBuffer = None

        # Remove event handlers
        if self.stdinDataHandler:
            # MISAKA fork: `process.stdin.removeListener("data", this.stdinDataHandler)`.
            self._remove_reader()
            self.stdinDataHandler = None
        self.inputHandler = None
        if self.resizeHandler:
            self._restore_resize_handler()
            self.resizeHandler = None

        # Pause stdin to prevent any buffered input (e.g., Ctrl+D) from being
        # re-interpreted after raw mode is disabled. This fixes a race condition
        # where Ctrl+D could close the parent shell over SSH.
        # Restore raw mode state
        # MISAKA fork: `stdin.pause()` + `setRawMode(this.wasRaw)`; restoring the terminal discards unread input.
        self._restore_raw_mode()

    def write(self, data: str) -> None:
        self.stdout.write(data)
        # MISAKA fork: Node writes a TTY synchronously; Python's stdout buffers.
        flush = getattr(self.stdout, "flush", None)
        if callable(flush):
            flush()
        if self.writeLogPath:
            try:
                with open(self.writeLogPath, "a", encoding="utf-8", newline="") as handle:
                    handle.write(data)
            except Exception:  # noqa: BLE001, S110
                # Ignore logging errors
                pass

    @property
    def columns(self) -> int:
        size = self._tty_size()
        if size and size.columns:
            return size.columns
        configured = jsNumber(os.environ.get("COLUMNS", ""))
        return int(configured) if jsTruthyNumber(configured) else 80

    @property
    def rows(self) -> int:
        size = self._tty_size()
        if size and size.lines:
            return size.lines
        configured = jsNumber(os.environ.get("LINES", ""))
        return int(configured) if jsTruthyNumber(configured) else 24

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

    def setProgramStatus(self, status: ProgramStatus) -> None:
        self.programStatus = None if status["state"] == "clear" else status
        if self.programStatusSupported:
            self.write(formatProgramStatus(status))

    def writeProgramStatus(self) -> None:
        if self.programStatusSupported and self.programStatus:
            self.write(formatProgramStatus(self.programStatus))

    def setProgress(self, active: bool) -> None:
        if active:
            # OSC 9;4;3 - indeterminate progress
            self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
            if not self.progressInterval:
                self._scheduleProgressKeepalive()
        else:
            self.clearProgressInterval()
            # OSC 9;4;0 - clear progress
            self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)

    def clearProgressInterval(self) -> bool:
        if not self.progressInterval:
            return False
        self.progressInterval.cancel()
        self.progressInterval = None
        return True

    # --- MISAKA fork: the Node runtime pieces ---------------------------------------------------------------------

    def _scheduleProgressKeepalive(self) -> None:
        """``setInterval``: one loop timer after another."""

        def tick() -> None:
            self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
            self._scheduleProgressKeepalive()

        self.progressInterval = self._setTimeout(tick, TERMINAL_PROGRESS_KEEPALIVE_MS)

    def _setTimeout(self, callback: Callable[[], None], ms: float) -> asyncio.TimerHandle:
        return self._event_loop().call_later(ms / 1000, callback)

    def _install_resize_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH") or self.resizeHandler is None:
            return
        self._previousSigwinchHandler = signal.getsignal(signal.SIGWINCH)

        def handler(_signum: int, _frame: Any) -> None:
            # A signal handler can interrupt the main thread between any two bytecodes; re-rendering in place would
            # interleave with an in-progress write and garble the screen. Queue it on the event loop instead.
            self._on_loop(lambda: self.resizeHandler() if self.resizeHandler else None)

        signal.signal(signal.SIGWINCH, handler)

    def _restore_resize_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH"):
            return
        if self._previousSigwinchHandler is not None:
            signal.signal(signal.SIGWINCH, self._previousSigwinchHandler)
            self._previousSigwinchHandler = None

    def _install_reader(self) -> None:
        if self._readerInstalled:
            return
        fd = self._stdin_fileno()
        if fd is None:
            return
        if sys.platform == "win32":
            self._install_console_reader(fd)
            return
        loop = self._event_loop()
        add_reader = getattr(loop, "add_reader", None)
        if callable(add_reader):
            add_reader(fd, self._handle_stdin_ready)
            self._readerInstalled = True

    def _install_console_reader(self, fd: int) -> None:
        # Windows: a console handle cannot be selected on, so libuv reads its input records on a thread and emits
        # 'data' and the stdout 'resize' from them; the console reader does the same and routes both to the loop.
        from misaka.utils import win_console

        def on_text(text: str) -> None:
            self._on_loop(lambda: self._handle_input_data(text))

        def on_resize() -> None:
            self._on_loop(lambda: self.resizeHandler() if self.resizeHandler else None)

        self._consoleInput = win_console.ConsoleInput(fd, on_text, on_resize).start()
        self._readerInstalled = True

    def _remove_reader(self) -> None:
        if not self._readerInstalled:
            return
        if self._consoleInput is not None:
            self._consoleInput.stop()
            self._consoleInput = None
            self._readerInstalled = False
            return
        fd = self._stdin_fileno()
        if fd is None:
            self._readerInstalled = False
            return
        loop = self._event_loop()
        remove_reader = getattr(loop, "remove_reader", None)
        if callable(remove_reader):
            remove_reader(fd)
        self._readerInstalled = False

    def _handle_stdin_ready(self) -> None:
        fd = self._stdin_fileno()
        if fd is not None:
            try:
                data: str | bytes = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if data == b"":
                # End of input: Node streams emit no data after 'end'; a reader left in place would spin.
                self._remove_reader()
                return
        else:
            reader = getattr(self.stdin, "read", None)
            data = reader(4096) if callable(reader) else ""
            if not data:
                self._remove_reader()
                return
        self._handle_input_data(data)

    def _handle_input_data(self, data: str | bytes) -> None:
        self._lastStdinActivityMs = self._now_ms()
        if self.stdinDataHandler is not None:
            self.stdinDataHandler(data)

    def _stdin_fileno(self) -> int | None:
        try:
            return int(self.stdin.fileno())
        except Exception:  # noqa: BLE001 - no usable stdin fd
            return None

    def _enter_raw_mode(self) -> None:
        if sys.platform == "win32":
            # libuv's UV_TTY_MODE_RAW on a Windows console is a console mode, not termios.
            fd = self._stdin_fileno()
            if fd is not None:
                from misaka.utils import win_console

                self._previousConsoleModes = win_console.enter_raw_mode(fd)
            return
        if termios is None:
            return
        fd = self._stdin_fileno()
        is_tty = getattr(self.stdin, "isatty", None)
        if fd is None or (callable(is_tty) and not is_tty()):
            return
        self._previousTermiosSettings = termios.tcgetattr(fd)
        # Not tty.setraw: it clears OPOST (bare \n output staircases) and TCSAFLUSH discards input typed during
        # startup. libuv's UV_TTY_MODE_RAW keeps output post-processing.
        mode = termios.tcgetattr(fd)
        mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)  # iflag
        mode[2] |= termios.CS8  # cflag
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)  # lflag
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)

    def _restore_raw_mode(self) -> None:
        if self._previousConsoleModes is not None:
            from misaka.utils import win_console

            win_console.restore(self._previousConsoleModes)
            self._previousConsoleModes = None
            return
        if termios is None or self._previousTermiosSettings is None:
            return
        fd = self._stdin_fileno()
        if fd is None:
            return
        # TCSAFLUSH can hang forever on a macOS pty (the drain wake-up is lost; double Ctrl+C/Ctrl+D exit stuck on
        # this line). TCSANOW never blocks, and the explicit flush of unread input keeps a leftover Ctrl+D from
        # leaking to the parent shell.
        termios.tcsetattr(fd, termios.TCSANOW, self._previousTermiosSettings)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        self._previousTermiosSettings = None

    def _tty_size(self) -> os.terminal_size | None:
        """``process.stdout.columns``/``rows``: the live size of the stdout terminal."""
        try:
            return os.get_terminal_size(self.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            return None

    def _on_loop(self, cb: Callable[[], Any]) -> None:
        """Route a callback to the event loop thread; run it inline when there is no loop."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(cb)
                return
            except RuntimeError:
                pass
        cb()

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is not None:
            return self.loop
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop_policy().get_event_loop()
        return self.loop

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)


__all__ = [
    "KeyboardProtocolNegotiationSequence",
    "ProcessTerminal",
    "Terminal",
    "isAppleTerminalSession",
    "normalizeAppleTerminalInput",
    "normalizeNativeShiftEnterInput",
    "parseKeyboardProtocolNegotiationSequence",
    "refreshTerminalDimensions",
    "resolveEscapeTimeoutMs",
]
