"""The Windows clipboard through the Win32 API: text in and out, and the files Explorer copied.

MISAKA fork: it stands in for pi-tui's native Windows addon (``native/win32/src/win32-platform.c``), which reads
and writes through the same API. Writing is a port of herdr's ``platform/windows.rs`` ``write_clipboard``: the
console window opens the clipboard, and the text goes in as CF_UNICODETEXT in movable global memory; the ``clip``
command pi falls back to reads its input in the OEM code page. Windows-only, imported only there.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def _failed(what):
    return ctypes.WinError(ctypes.get_last_error(), f"{what} failed")


def copy(text: str) -> None:
    """Replace the clipboard's content with ``text``; OSError when the clipboard refuses."""
    if "\0" in text:
        raise ValueError("clipboard text cannot hold a NUL")
    data = (text + "\0").encode("utf-16-le")
    owner = _kernel32.GetConsoleWindow()
    if not owner or not _user32.OpenClipboard(owner):
        raise _failed("OpenClipboard")
    try:
        if not _user32.EmptyClipboard():
            raise _failed("EmptyClipboard")
        memory = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not memory:
            raise _failed("GlobalAlloc")
        locked = _kernel32.GlobalLock(memory)
        if not locked:
            _kernel32.GlobalFree(memory)
            raise _failed("GlobalLock")
        ctypes.memmove(locked, data, len(data))
        _kernel32.GlobalUnlock(memory)
        if not _user32.SetClipboardData(CF_UNICODETEXT, memory):
            _kernel32.GlobalFree(memory)
            raise _failed("SetClipboardData")
    finally:
        _user32.CloseClipboard()


CF_HDROP = 15
# win32-platform.c's OPEN_CLIPBOARD_ATTEMPTS and OPEN_CLIPBOARD_RETRY_MS.
_OPEN_ATTEMPTS = 10
_OPEN_RETRY_SECONDS = 0.005
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT


def _open_for_reading() -> None:
    """Open the clipboard without an owner, as a reader may. Another process holds it for a moment
    while it copies, so a refusal is retried a few times before it is an error."""
    for attempt in range(_OPEN_ATTEMPTS):
        if _user32.OpenClipboard(None):
            return
        if attempt < _OPEN_ATTEMPTS - 1:
            time.sleep(_OPEN_RETRY_SECONDS)
    raise _failed("OpenClipboard")


def paste() -> str:
    """The clipboard's text (CF_UNICODETEXT), or an empty string when it holds none; OSError when the
    clipboard refuses to open."""
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    _open_for_reading()
    try:
        memory = _user32.GetClipboardData(CF_UNICODETEXT)
        if not memory:
            return ""
        locked = _kernel32.GlobalLock(memory)
        if not locked:
            raise _failed("GlobalLock")
        try:
            # The block can be larger than its text; the text ends at the first NUL.
            return ctypes.wstring_at(locked, _kernel32.GlobalSize(memory) // 2).split("\0", 1)[0]
        finally:
            _kernel32.GlobalUnlock(memory)
    finally:
        _user32.CloseClipboard()


def file_paths() -> list[str] | None:
    """The paths of the files copied in Explorer (CF_HDROP), or None when the clipboard holds none;
    OSError when the clipboard refuses to open. MISAKA fork: win32-platform.c exports no file-path reader."""
    if not _user32.IsClipboardFormatAvailable(CF_HDROP):
        return None
    _open_for_reading()
    try:
        drop = _user32.GetClipboardData(CF_HDROP)
        if not drop:
            return None
        paths = []
        for index in range(_shell32.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)):
            length = _shell32.DragQueryFileW(drop, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            _shell32.DragQueryFileW(drop, index, buffer, length + 1)
            paths.append(buffer.value)
        return paths or None
    finally:
        _user32.CloseClipboard()
