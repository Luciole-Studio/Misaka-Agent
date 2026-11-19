"""The Windows console as a terminal: raw mode, VT input and output, and a reader thread.

On POSIX a terminal is a file descriptor: termios switches it to raw mode, ``select`` says when
it has bytes and SIGWINCH says when it changed size. A Windows console is none of those. Its
input is a queue of records (keys, mouse, focus, buffer size) read with ``ReadConsoleInputW``,
and its handle cannot be selected on. With ENABLE_VIRTUAL_TERMINAL_INPUT the console encodes
keys, mouse reports, focus and bracketed paste as the same VT sequences a POSIX terminal sends,
so the parsers above this module stay as they are: ``ConsoleInput`` turns the record queue back
into that text on a thread (as libuv's ``uv_tty_read_raw`` does for Node, and herdr's
``raw_console_reader_loop`` for its client) and reports a size change when a buffer-size record
arrives. Everything here is Windows-only and imported only there.
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import struct
import threading
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

KEY_EVENT = 0x0001
WINDOW_BUFFER_SIZE_EVENT = 0x0004
VK_MENU = 0x12
CP_UTF8 = 65001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
POLL_MS = 50


class _Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _KeyEventRecord(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WORD), ("dwControlKeyState", wintypes.DWORD)]


class _MouseEventRecord(ctypes.Structure):
    _fields_ = [("dwMousePosition", _Coord), ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]


class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEventRecord), ("MouseEvent", _MouseEventRecord),
                ("WindowBufferSizeEvent", _Coord), ("FocusEvent", wintypes.BOOL)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


_k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_InputRecord), wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD)]
_k32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
_k32.SetConsoleCP.argtypes = [wintypes.UINT]
_k32.SetConsoleOutputCP.argtypes = [wintypes.UINT]


def _handle(fd):
    try:
        return msvcrt.get_osfhandle(fd)
    except OSError:
        return None


def _mode(handle):
    mode = wintypes.DWORD()
    if handle is None or not _k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    return mode.value


def is_console(fd):
    """True when ``fd`` is a console handle (what ``isatty`` means on Windows)."""
    return _mode(_handle(fd)) is not None


def enter_raw_mode(fd_in=0, fd_out=1, *, mouse=False):
    """Switch the console to raw VT mode; returns what ``restore`` needs, or None when ``fd_in``
    is not a console.

    Input as crossterm's ``enable_raw_mode`` (no line editing, no echo, ctrl+c as a byte rather
    than a signal) plus VT input, and window records so a resize is seen. ``mouse`` also takes
    the mouse away from Quick Edit selection, which would otherwise swallow every click. Output
    gets VT processing and UTF-8, the code page ``os.write`` of UTF-8 bytes is read in; ONLCR's
    counterpart (a bare LF returning the carriage) stays on, as libuv's raw mode keeps OPOST."""
    handle_in = _handle(fd_in)
    mode_in = _mode(handle_in)
    if mode_in is None:
        return None
    raw = (mode_in & ~(ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
           | ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_WINDOW_INPUT)
    if mouse:
        raw = (raw | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
    _k32.SetConsoleMode(handle_in, raw)
    handle_out = _handle(fd_out)
    mode_out = _mode(handle_out)
    if mode_out is not None:
        _k32.SetConsoleMode(handle_out, mode_out | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    code_pages = (_k32.GetConsoleCP(), _k32.GetConsoleOutputCP())
    _k32.SetConsoleCP(CP_UTF8)
    _k32.SetConsoleOutputCP(CP_UTF8)
    return handle_in, mode_in, handle_out, mode_out, code_pages


def restore(saved, *, flush_input=True):
    """Undo ``enter_raw_mode``. ``flush_input`` drops what nobody read, as TCIFLUSH does on
    POSIX, so a stray keystroke does not reach the shell the program returns to."""
    if saved is None:
        return
    handle_in, mode_in, handle_out, mode_out, (cp_in, cp_out) = saved
    if flush_input:
        _k32.FlushConsoleInputBuffer(handle_in)
    _k32.SetConsoleMode(handle_in, mode_in)
    if mode_out is not None:
        _k32.SetConsoleMode(handle_out, mode_out)
    _k32.SetConsoleCP(cp_in)
    _k32.SetConsoleOutputCP(cp_out)


def size(fd=1):
    """``(rows, cols)`` of the console window, or None when ``fd`` is not a console."""
    try:
        columns, lines = os.get_terminal_size(fd)
    except OSError:
        return None
    return (lines, columns) if lines and columns else None


def _text(units):
    """UTF-16 code units -> (text, a trailing high surrogate still waiting for its pair)."""
    tail = units[-1:] if units and 0xD800 <= units[-1] <= 0xDBFF else []
    body = units[:len(units) - len(tail)]
    return struct.pack(f"<{len(body)}H", *body).decode("utf-16-le", "replace"), tail


class ConsoleInput:
    """A thread that reads the console's input records and hands on what they say.

    ``on_text(str)`` receives the characters of key-down records -- in VT input mode these are
    the VT sequences a POSIX terminal would have written -- and ``on_resize()`` fires on a
    buffer-size record. Both run on the reader thread: a caller with an event loop routes them
    there itself. Key-up records are dropped except an Alt release carrying a character, which
    is how Alt+numpad composes one (libuv's rule). ``stop`` returns once the thread is gone; the
    wait between reads is short, so no record is taken after it."""

    def __init__(self, fd, on_text, on_resize=None):
        self._handle = _handle(fd)
        self._on_text = on_text
        self._on_resize = on_resize
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="console-input", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stopping.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _run(self):
        records = (_InputRecord * 64)()
        count = wintypes.DWORD()
        pending = []
        while not self._stopping.is_set():
            waited = _k32.WaitForSingleObject(self._handle, POLL_MS)
            if waited == WAIT_TIMEOUT:
                continue
            if waited != WAIT_OBJECT_0 or self._stopping.is_set():
                return
            if not _k32.ReadConsoleInputW(self._handle, records, len(records), ctypes.byref(count)):
                return
            units, resized = list(pending), False
            for record in records[:count.value]:
                if record.EventType == KEY_EVENT:
                    key = record.Event.KeyEvent
                    if key.uChar and (key.bKeyDown or key.wVirtualKeyCode == VK_MENU):
                        units.extend([key.uChar] * max(1, key.wRepeatCount))
                elif record.EventType == WINDOW_BUFFER_SIZE_EVENT:
                    resized = True
            text, pending = _text(units)
            if text:
                self._on_text(text)
            if resized and self._on_resize is not None:
                self._on_resize()


def query(fd_in, fd_out, request, done, timeout):
    """Write ``request`` and collect what the console sends back until ``done(bytes)`` or
    ``timeout``: a terminal query answered before any reader of this process is running (the
    panel asks for the background colour this way). None when ``fd_in`` is not a console."""
    saved = enter_raw_mode(fd_in, fd_out)
    if saved is None:
        return None
    received, answered = bytearray(), threading.Event()

    def on_text(text):
        received.extend(text.encode())
        if done(bytes(received)):
            answered.set()

    reader = ConsoleInput(fd_in, on_text).start()
    try:
        os.write(fd_out, request)
        answered.wait(timeout)
    finally:
        reader.stop()
        restore(saved, flush_input=False)
    return bytes(received)
