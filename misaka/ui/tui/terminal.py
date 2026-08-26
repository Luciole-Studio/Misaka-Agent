"""Terminal lifecycle and ANSI control helpers for the TUI package.

PORT-NOTE (audit fixes, checked section by section against pi terminal.ts@686f193e):
- Kitty keyboard protocol negotiation rewritten to match the upstream state machine:
  push flags, then query, then a trailing DA sentinel (no 150ms guess timer);
  fragmented replies are reassembled with a 150ms flush; flags==0 falls back to
  modifyOtherKeys; modifyOtherKeys is disabled while kitty is active; stop/drainInput
  both clean up.
- Apple Terminal Shift+Enter normalization (native-modifiers .node addon -> ctypes CoreGraphics).
- Raw mode follows libuv UV_TTY_MODE_RAW semantics (keeps OPOST/ONLCR; TCSADRAIN on entry,
  TCSAFLUSH on exit so unread input is discarded, equivalent to upstream stdin.pause()
  preventing a stray Ctrl+D from leaking to the parent shell).
- SIGWINCH and timer callbacks always go through call_soon_threadsafe back to the event
  loop thread (Node single-thread semantics).
- The reader is removed on stdin EOF (Node streams emit no data after 'end'), avoiding a
  100% CPU spin.
- COLUMNS/LINES fallback follows JS semantics: non-numeric and 0 both become 80/24.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from misaka.ui.tui.keys import setKittyProtocolActive
from misaka.ui.tui.stdin_buffer import StdinBuffer

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows import path.
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

TERMINAL_PROGRESS_KEEPALIVE_MS = 1000
TERMINAL_PROGRESS_ACTIVE_SEQUENCE = "\x1b]9;4;3\x07"
TERMINAL_PROGRESS_CLEAR_SEQUENCE = "\x1b]9;4;0\x07"
APPLE_TERMINAL_SHIFT_ENTER_SEQUENCE = "\x1b[13;2u"
DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS = 7
KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_MS = 150
KITTY_KEYBOARD_PROTOCOL_QUERY = f"\x1b[>{DESIRED_KITTY_KEYBOARD_PROTOCOL_FLAGS}u\x1b[?u\x1b[c"

_KITTY_FLAGS_RE = re.compile(r"^\x1b\[\?(\d+)u$")
_DEVICE_ATTRS_RE = re.compile(r"^\x1b\[\?[\d;]*c$")
_NEGOTIATION_PREFIX_RE = re.compile(r"^\x1b\[\?[\d;]*$")


def parse_keyboard_protocol_negotiation_sequence(sequence: str):
    """Returns {"type": "kitty-flags", "flags": n} | {"type": "device-attributes"} | None."""
    m = _KITTY_FLAGS_RE.match(sequence)
    if m:
        return {"type": "kitty-flags", "flags": int(m.group(1))}
    if _DEVICE_ATTRS_RE.match(sequence):
        return {"type": "device-attributes"}
    return None


def is_keyboard_protocol_negotiation_sequence_prefix(sequence: str) -> bool:
    return sequence == "\x1b[" or _NEGOTIATION_PREFIX_RE.match(sequence) is not None


def is_apple_terminal_session() -> bool:
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def normalize_apple_terminal_input(data: str, is_apple_terminal: bool, is_shift_pressed: bool) -> str:
    if is_apple_terminal and data == "\r" and is_shift_pressed:
        return APPLE_TERMINAL_SHIFT_ENTER_SEQUENCE
    return data


# PORT-NOTE: upstream reads live modifier state via the darwin-modifiers.node addon; the Python
# equivalent is calling CoreGraphics CGEventSourceFlagsState through ctypes
# (0 = kCGEventSourceStateCombinedSessionState).
_MODIFIER_MASKS = {"shift": 0x20000, "control": 0x40000, "option": 0x80000, "command": 0x100000}
_cg_lib: Any = None
_cg_failed = False


def is_native_modifier_pressed(key: str) -> bool:
    global _cg_lib, _cg_failed
    if sys.platform != "darwin" or _cg_failed:
        return False
    mask = _MODIFIER_MASKS.get(key)
    if not mask:
        return False
    try:
        if _cg_lib is None:
            _cg_lib = ctypes.CDLL(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            _cg_lib.CGEventSourceFlagsState.restype = ctypes.c_uint64
            _cg_lib.CGEventSourceFlagsState.argtypes = [ctypes.c_int]
        return bool(_cg_lib.CGEventSourceFlagsState(0) & mask)
    except Exception:  # noqa: BLE001 - native layer unavailable: always False, as upstream on addon load failure
        _cg_failed = True
        return False


class Terminal(Protocol):
    def start(self, onInput: Any, onResize: Any) -> None: ...
    def stop(self) -> None: ...
    async def drainInput(self, maxMs: int = 1000, idleMs: int = 50) -> None: ...
    def write(self, data: str) -> None: ...
    @property
    def columns(self) -> int: ...
    @property
    def rows(self) -> int: ...
    @property
    def kittyProtocolActive(self) -> bool: ...
    def moveBy(self, lines: int) -> None: ...
    def hideCursor(self) -> None: ...
    def showCursor(self) -> None: ...
    def clearLine(self) -> None: ...
    def clearFromCursor(self) -> None: ...
    def clearScreen(self) -> None: ...
    def setTitle(self, title: str) -> None: ...
    def setProgress(self, active: bool) -> None: ...


class ProcessTerminal:
    def __init__(self) -> None:
        self.stdin = sys.stdin
        self.stdout = sys.stdout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wasRaw = False
        self.inputHandler: Any | None = None
        self.resizeHandler: Any | None = None
        self._kittyProtocolActive = False
        self._modifyOtherKeysActive = False
        self.keyboardProtocolPushed = False
        self.keyboardProtocolNegotiationBuffer = ""
        self.keyboardProtocolBufferFlushTimer: threading.Timer | None = None
        self.stdinBuffer: StdinBuffer | None = None
        self.stdinDataHandler: Any | None = None
        self.progressTimer: threading.Timer | None = None
        self._progressActive = False
        self._previousSigwinchHandler: Any | None = None
        self._previousTermiosSettings: list[Any] | None = None
        self._lastStdinActivityMs = self._now_ms()
        self._readerInstalled = False
        self.writeLogPath = self._resolve_write_log_path()

    @property
    def kittyProtocolActive(self) -> bool:
        return self._kittyProtocolActive

    @property
    def modifyOtherKeysActive(self) -> bool:
        return self._modifyOtherKeysActive

    def start(self, onInput: Any, onResize: Any) -> None:
        self.inputHandler = onInput
        self.resizeHandler = onResize
        self._lastStdinActivityMs = self._now_ms()
        self._event_loop()  # capture the event loop so timer/signal callbacks can be routed back to the main thread

        self.wasRaw = bool(getattr(self.stdin, "isRaw", False))
        if hasattr(self.stdin, "setRawMode"):
            self.stdin.setRawMode(True)
        else:
            self._enter_raw_mode()

        set_encoding = getattr(self.stdin, "setEncoding", None)
        if callable(set_encoding):
            set_encoding("utf8")

        if hasattr(self.stdin, "resume"):
            self.stdin.resume()

        self.write("\x1b[?2004h")
        self._install_resize_handler()
        self._refresh_dimensions()
        self.enableWindowsVTInput()
        self.queryAndEnableKittyProtocol()

    def queryAndEnableKittyProtocol(self) -> None:
        """Upstream semantics: push flags, query, then a trailing DA sentinel. A terminal that
        does not know the kitty protocol answers the DA first, and we switch to
        modifyOtherKeys without any guessing timer."""
        self.setupStdinBuffer()
        self._install_reader()
        self.keyboardProtocolPushed = True
        self.clear_keyboard_protocol_negotiation_buffer()
        self.write(KITTY_KEYBOARD_PROTOCOL_QUERY)

    def setupStdinBuffer(self) -> None:
        self.stdinBuffer = StdinBuffer({"timeout": 10}, loop=self.loop)

        def on_data(sequence: str) -> None:
            negotiation = self.read_keyboard_protocol_negotiation_sequence(sequence)
            if negotiation == "pending":
                self.schedule_keyboard_protocol_negotiation_buffer_flush()
                return  # wait briefly so a fragmented kitty reply can be reassembled
            if self.handle_keyboard_protocol_negotiation_sequence(negotiation):
                return
            self.forward_input_sequence(sequence)

        def on_paste(content: str) -> None:
            if self.inputHandler is not None:
                self.inputHandler(f"\x1b[200~{content}\x1b[201~")

        self.stdinBuffer.on("data", on_data)
        self.stdinBuffer.on("paste", on_paste)
        self.stdinDataHandler = lambda data: self.stdinBuffer.process(data)

    # ── kitty negotiation state machine (mirrors terminal.ts:228-306 one to one) ──────────────────

    def handle_keyboard_protocol_negotiation_sequence(self, negotiation) -> bool:
        if not negotiation:
            return False
        self.clear_keyboard_protocol_negotiation_buffer()
        if negotiation["type"] == "kitty-flags":
            if negotiation["flags"] != 0:
                self.disable_modify_other_keys()
                if not self._kittyProtocolActive:
                    self._kittyProtocolActive = True
                    setKittyProtocolActive(True)
            else:
                self.enable_modify_other_keys()
            return True
        if not self._kittyProtocolActive:  # device-attributes arrived first: terminal does not support kitty
            self.enable_modify_other_keys()
        return True

    def read_keyboard_protocol_negotiation_sequence(self, sequence: str):
        if self.keyboardProtocolNegotiationBuffer:
            buffered = self.keyboardProtocolNegotiationBuffer + sequence
            negotiation = parse_keyboard_protocol_negotiation_sequence(buffered)
            if negotiation:
                self.clear_keyboard_protocol_negotiation_buffer()
                return negotiation
            if is_keyboard_protocol_negotiation_sequence_prefix(buffered):
                self.set_keyboard_protocol_negotiation_buffer(buffered)
                return "pending"
            self.flush_keyboard_protocol_negotiation_buffer_as_input()

        negotiation = parse_keyboard_protocol_negotiation_sequence(sequence)
        if negotiation:
            return negotiation
        if is_keyboard_protocol_negotiation_sequence_prefix(sequence):
            self.set_keyboard_protocol_negotiation_buffer(sequence)
            return "pending"
        return None

    def set_keyboard_protocol_negotiation_buffer(self, sequence: str) -> None:
        self.clear_keyboard_protocol_negotiation_buffer_flush_timer()
        self.keyboardProtocolNegotiationBuffer = sequence

    def clear_keyboard_protocol_negotiation_buffer(self) -> None:
        self.clear_keyboard_protocol_negotiation_buffer_flush_timer()
        self.keyboardProtocolNegotiationBuffer = ""

    def flush_keyboard_protocol_negotiation_buffer_as_input(self) -> None:
        if not self.keyboardProtocolNegotiationBuffer:
            return
        sequence = self.keyboardProtocolNegotiationBuffer
        self.clear_keyboard_protocol_negotiation_buffer()
        self.forward_input_sequence(sequence)

    def schedule_keyboard_protocol_negotiation_buffer_flush(self) -> None:
        if not self.keyboardProtocolNegotiationBuffer or self.keyboardProtocolBufferFlushTimer:
            return

        def fire() -> None:
            self.keyboardProtocolBufferFlushTimer = None
            self.flush_keyboard_protocol_negotiation_buffer_as_input()

        timer = threading.Timer(
            KEYBOARD_PROTOCOL_RESPONSE_FRAGMENT_TIMEOUT_MS / 1000.0,
            lambda: self._on_loop(fire))
        timer.daemon = True
        self.keyboardProtocolBufferFlushTimer = timer
        timer.start()

    def clear_keyboard_protocol_negotiation_buffer_flush_timer(self) -> None:
        if self.keyboardProtocolBufferFlushTimer is None:
            return
        self.keyboardProtocolBufferFlushTimer.cancel()
        self.keyboardProtocolBufferFlushTimer = None

    def forward_input_sequence(self, sequence: str) -> None:
        if self.inputHandler is None:
            return
        is_apple = sequence == "\r" and is_apple_terminal_session()
        self.inputHandler(normalize_apple_terminal_input(
            sequence, is_apple, is_apple and is_native_modifier_pressed("shift")))

    def enable_modify_other_keys(self) -> None:
        if self._kittyProtocolActive or self._modifyOtherKeysActive:
            return
        self.write("\x1b[>4;2m")
        self._modifyOtherKeysActive = True

    def disable_modify_other_keys(self) -> None:
        if not self._modifyOtherKeysActive:
            return
        self.write("\x1b[>4;0m")
        self._modifyOtherKeysActive = False

    # ────────────────────────────────────────────────────────────────────────

    def enableWindowsVTInput(self) -> None:
        if sys.platform != "win32":
            return
        try:
            ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
            STD_INPUT_HANDLE = -10
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if handle in {0, -1}:
                return
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT)
        except Exception:
            return

    async def drainInput(self, maxMs: int = 1000, idleMs: int = 50) -> None:
        should_disable_kitty = self.keyboardProtocolPushed or self._kittyProtocolActive
        self.clear_keyboard_protocol_negotiation_buffer()
        if should_disable_kitty:
            # pop the kitty flags first so late release events no longer produce kitty escape sequences
            self.write("\x1b[<u")
            self.keyboardProtocolPushed = False
            self._kittyProtocolActive = False
            setKittyProtocolActive(False)
        self.disable_modify_other_keys()

        previous_handler = self.inputHandler
        self.inputHandler = None
        self._lastStdinActivityMs = self._now_ms()  # upstream resets the clock on entry: drain for at least idleMs
        end_time = self._now_ms() + maxMs

        try:
            while True:
                now = self._now_ms()
                time_left = end_time - now
                if time_left <= 0:
                    break
                if now - self._lastStdinActivityMs >= idleMs:
                    break
                await asyncio.sleep(min(idleMs, time_left) / 1000.0)
        finally:
            self.inputHandler = previous_handler

    def stop(self) -> None:
        if self.clearProgressInterval():
            self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)

        self.write("\x1b[?2004l")

        should_disable_kitty = self.keyboardProtocolPushed or self._kittyProtocolActive
        self.clear_keyboard_protocol_negotiation_buffer()
        if should_disable_kitty:  # fallback pop in case drainInput never ran
            self.write("\x1b[<u")
            self.keyboardProtocolPushed = False
            self._kittyProtocolActive = False
            setKittyProtocolActive(False)
        self.disable_modify_other_keys()

        if self.stdinBuffer is not None:
            self.stdinBuffer.destroy()
            self.stdinBuffer = None

        self._remove_reader()
        self.stdinDataHandler = None
        self.inputHandler = None
        self._restore_resize_handler()

        if hasattr(self.stdin, "pause"):
            self.stdin.pause()

        if hasattr(self.stdin, "setRawMode"):
            self.stdin.setRawMode(self.wasRaw)
        else:
            self._restore_raw_mode()

    def write(self, data: str) -> None:
        self.stdout.write(data)
        flush = getattr(self.stdout, "flush", None)
        if callable(flush):
            flush()
        if self.writeLogPath:
            try:
                Path(self.writeLogPath).parent.mkdir(parents=True, exist_ok=True)
                with open(self.writeLogPath, "a", encoding="utf-8") as handle:
                    handle.write(data)
            except OSError:
                pass

    # PORT-NOTE: pi's process.stdout.columns/rows are the live tty size maintained by Node; the
    # Python equivalent is an ioctl on the stdout fd. The env fallback follows JS semantics:
    # Number("abc") is NaN, and both NaN and 0 are falsy, so they become 80/24.
    def _tty_size(self):
        try:
            return os.get_terminal_size(self.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            return None

    @staticmethod
    def _env_dim(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name) or 0)
        except ValueError:
            value = 0
        return value or default

    @property
    def columns(self) -> int:
        size = self._tty_size()
        # 0 columns falls back too (JS || semantics): when a pty has no winsize set, ioctl returns
        # 0x0, which in practice rendered the whole screen one character per line
        return size.columns if size and size.columns > 0 else self._env_dim("COLUMNS", 80)

    @property
    def rows(self) -> int:
        size = self._tty_size()
        return size.lines if size and size.lines > 0 else self._env_dim("LINES", 24)

    def moveBy(self, lines: int) -> None:
        if lines > 0:
            self.write(f"\x1b[{lines}B")
        elif lines < 0:
            self.write(f"\x1b[{-lines}A")

    def hideCursor(self) -> None:
        self.write("\x1b[?25l")

    def showCursor(self) -> None:
        self.write("\x1b[?25h")

    def clearLine(self) -> None:
        self.write("\x1b[K")

    def clearFromCursor(self) -> None:
        self.write("\x1b[J")

    def clearScreen(self) -> None:
        self.write("\x1b[2J\x1b[H")

    def setTitle(self, title: str) -> None:
        self.write(f"\x1b]0;{title}\x07")

    def setProgress(self, active: bool) -> None:
        if active:
            self._progressActive = True
            self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
            if self.progressTimer is None:
                self._schedule_progress_keepalive()
            return

        self._progressActive = False
        self.clearProgressInterval()
        self.write(TERMINAL_PROGRESS_CLEAR_SEQUENCE)

    def clearProgressInterval(self) -> bool:
        if self.progressTimer is None:
            return False
        self.progressTimer.cancel()
        self.progressTimer = None
        return True

    def _schedule_progress_keepalive(self) -> None:
        if not self._progressActive:
            return

        def tick() -> None:
            if not self._progressActive:
                self.progressTimer = None
                return
            self.write(TERMINAL_PROGRESS_ACTIVE_SEQUENCE)
            self._schedule_progress_keepalive()

        # The keepalive write runs on the event loop thread: Node's setInterval is single-threaded,
        # and writing from the timer thread would interleave with render bytes (audit C8).
        self.progressTimer = threading.Timer(
            TERMINAL_PROGRESS_KEEPALIVE_MS / 1000.0, lambda: self._on_loop(tick))
        self.progressTimer.daemon = True
        self.progressTimer.start()

    def _resolve_write_log_path(self) -> str:
        raw = os.environ.get("MISAKA_TUI_WRITE_LOG", "")
        if not raw:
            return ""
        try:
            path = Path(raw)
            if path.is_dir():
                from datetime import datetime

                now = datetime.now()  # noqa: DTZ005 - local wall-clock time for a debug log name
                ts = now.strftime("%Y-%m-%d_%H-%M-%S")
                return str(path / f"tui-{ts}-{os.getpid()}.log")
        except OSError:
            pass
        return raw

    def _install_resize_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH") or self.resizeHandler is None:
            return
        self._previousSigwinchHandler = signal.getsignal(signal.SIGWINCH)

        def handler(_signum: int, _frame: Any) -> None:
            # A signal handler can interrupt the main thread between any two bytecodes; re-rendering
            # in place would interleave with an in-progress write and garble the screen (audit C7).
            # Queue it on the event loop instead.
            self._on_loop(lambda: self.resizeHandler() if self.resizeHandler else None)

        signal.signal(signal.SIGWINCH, handler)

    def _restore_resize_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH"):
            return
        if self._previousSigwinchHandler is not None:
            signal.signal(signal.SIGWINCH, self._previousSigwinchHandler)
            self._previousSigwinchHandler = None
        self.resizeHandler = None

    def _refresh_dimensions(self) -> None:
        if sys.platform == "win32" or not hasattr(signal, "SIGWINCH"):
            return
        try:
            os.kill(os.getpid(), signal.SIGWINCH)
        except OSError:
            return

    def _install_reader(self) -> None:
        if self._readerInstalled:
            return
        fd = self._stdin_fileno()
        if fd is None:
            return
        loop = self._event_loop()
        add_reader = getattr(loop, "add_reader", None)
        if callable(add_reader):
            add_reader(fd, self._handle_stdin_ready)
            self._readerInstalled = True

    def _remove_reader(self) -> None:
        if not self._readerInstalled:
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
                # EOF: Node streams emit no data after 'end'. Without removing the reader we spin at 100% CPU (audit C4).
                self._remove_reader()
                return
        else:
            reader = getattr(self.stdin, "read", None)
            data = reader(4096) if callable(reader) else ""
            if not data:
                self._remove_reader()
                return
        self._lastStdinActivityMs = self._now_ms()
        if self.stdinDataHandler is not None:
            self.stdinDataHandler(data)

    def _stdin_fileno(self) -> int | None:
        try:
            return int(self.stdin.fileno())
        except Exception:
            return None

    def _enter_raw_mode(self) -> None:
        if sys.platform == "win32" or termios is None:
            return
        fd = self._stdin_fileno()
        is_tty = getattr(self.stdin, "isatty", None)
        if fd is None or (callable(is_tty) and not is_tty()):
            return
        self._previousTermiosSettings = termios.tcgetattr(fd)
        # PORT-NOTE: not tty.setraw: it clears OPOST (bare \n output staircases) and TCSAFLUSH
        # discards input typed during startup. Node/libuv UV_TTY_MODE_RAW keeps output
        # post-processing, so mirror that here.
        mode = termios.tcgetattr(fd)
        mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                     | termios.ISTRIP | termios.IXON)          # iflag
        mode[2] |= termios.CS8                                  # cflag
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)  # lflag
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)

    def _restore_raw_mode(self) -> None:
        if termios is None or self._previousTermiosSettings is None:
            return
        fd = self._stdin_fileno()
        if fd is None:
            return
        # MISAKA: TCSAFLUSH could hang forever on a macOS pty (the drain wake-up edge is lost and
        # the call never returns even after the peer read the output; double Ctrl+C/Ctrl+D exit
        # reproducibly stuck on this line). Use TCSANOW (never blocks) plus an explicit tcflush of
        # unread input, which keeps the intent of audit C9: a leftover Ctrl+D must not leak to the
        # parent shell.
        termios.tcsetattr(fd, termios.TCSANOW, self._previousTermiosSettings)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        self._previousTermiosSettings = None

    def _on_loop(self, cb) -> None:
        """Route a callback back to the event loop thread (Node single-thread semantics); run it inline when there is no loop."""
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
    "ProcessTerminal",
    "Terminal",
    "is_apple_terminal_session",
    "is_native_modifier_pressed",
    "normalize_apple_terminal_input",
    "parse_keyboard_protocol_negotiation_sequence",
]
