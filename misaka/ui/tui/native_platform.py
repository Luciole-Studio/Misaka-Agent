"""Translated from pi-tui's ``packages/tui/src/native-platform.ts`` (v1.1.0).

MISAKA fork: pi loads a prebuilt Node addon per platform (``native/<platform>/prebuilds``, found through
``native-module-path.ts``, which has no Python counterpart). Here each platform's helper is built in the process from
what Python reaches: ctypes (CoreGraphics on macOS; kernel32, user32 and shell32 on Windows), the ``pbcopy``,
``pbpaste`` and ``osascript`` commands on macOS, ``pyperclip`` on Linux, and Pillow's ``ImageGrab`` for images.
A helper offers the same methods its addon exports. Images come back as PNG bytes (Pillow re-encodes what it reads).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Callable
from io import BytesIO
from typing import Any, ClassVar, Literal, Protocol

type ModifierKey = Literal["shift", "command", "control", "option"]


class NativeClipboard(Protocol):
    """``getFilePaths`` and ``setText`` are optional: read them with ``getattr``."""

    # Undefined means unavailable, null means no text; transfer failures reject.
    async def getText(self) -> Any: ...

    # Undefined means unavailable, null means no image; transfer failures reject.
    async def getImage(self) -> Any: ...


# `NativeClipboard` plus the optional `enableVirtualTerminalInput()` and `isModifierPressed(name)`.
type NativePlatformHelper = Any

# Cache module loading, not display availability: a disconnected display can recover.
helpers: dict[str, NativePlatformHelper | None] = {}

_PASTEBOARD_TIMEOUT_SECONDS = 5

# MISAKA fork: the macOS addon reads the file URLs on the pasteboard through AppKit; Python has no AppKit binding,
# so JavaScript for Automation asks for them through the Objective-C bridge. The script answers a JSON list of POSIX
# paths, and anything that is not a file URL stays out of it.
_FINDER_PATHS_SCRIPT = r"""
ObjC.import('AppKit');
const paths = [];
try {
  const key = $.NSPasteboardURLReadingFileURLsOnlyKey || $('NSPasteboardURLReadingFileURLsOnlyKey');
  const options = $.NSDictionary.dictionaryWithObjectForKey($.NSNumber.numberWithBool(true), key);
  const urls = $.NSPasteboard.generalPasteboard.readObjectsForClassesOptions($.NSArray.arrayWithObject($.NSURL), options);
  for (let index = 0; index < urls.count; index++) {
    const url = urls.objectAtIndex(index);
    if (url.isFileURL) paths.push(ObjC.unwrap(url.path));
  }
} catch (error) {}
JSON.stringify(paths);
"""


def _readFinderPaths() -> list[str] | None:
    """The files copied in Finder, or ``None`` when the pasteboard holds none or ``osascript`` cannot be run."""
    try:
        answer = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _FINDER_PATHS_SCRIPT],
            capture_output=True,
            timeout=_PASTEBOARD_TIMEOUT_SECONDS,
            check=True,
        )
        paths = json.loads(answer.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(paths, list):
        return None
    return [path for path in paths if isinstance(path, str) and path.startswith("/")] or None


def _pasteboardEnv() -> dict[str, str]:
    # `LC_CTYPE=UTF-8` is how these tools are told to speak UTF-8 whatever the user's locale is.
    return {**os.environ, "LC_CTYPE": "UTF-8"}


def _pbcopy(text: str) -> None:
    subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        timeout=_PASTEBOARD_TIMEOUT_SECONDS,
        check=True,
        env=_pasteboardEnv(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pbpaste() -> str:
    answer = subprocess.run(
        ["pbpaste"],
        capture_output=True,
        timeout=_PASTEBOARD_TIMEOUT_SECONDS,
        check=True,
        env=_pasteboardEnv(),
    )
    return answer.stdout.decode("utf-8", errors="replace")


def _grabClipboardPng() -> bytes | None:
    """The clipboard image as PNG bytes, or ``None`` when it holds none."""
    from PIL import ImageGrab

    try:
        image = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001 - no clipboard image (or no way to read one): the addon answers null
        return None
    if not hasattr(image, "save"):
        return None
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    close = getattr(image, "close", None)
    if callable(close):
        close()
    return buffer.getvalue()


class _DarwinPlatformHelper:
    """``darwin-platform.node``: isModifierPressed, getText, setText, getImage, getFilePaths."""

    # kCGEventFlagMask{Shift,Command,Control,Alternate}
    MODIFIER_MASKS: ClassVar[dict[str, int]] = {"shift": 0x20000, "command": 0x100000, "control": 0x40000, "option": 0x80000}

    def __init__(self) -> None:
        self.coreGraphics: Any = None

    def isModifierPressed(self, name: ModifierKey) -> bool:
        mask = self.MODIFIER_MASKS.get(name, 0)
        if mask == 0:
            return False
        if self.coreGraphics is None:
            coreGraphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            coreGraphics.CGEventSourceFlagsState.restype = ctypes.c_uint64
            coreGraphics.CGEventSourceFlagsState.argtypes = [ctypes.c_int]
            self.coreGraphics = coreGraphics
        # 0 = kCGEventSourceStateCombinedSessionState
        return (self.coreGraphics.CGEventSourceFlagsState(0) & mask) != 0

    async def getText(self) -> str | None:
        return (await asyncio.to_thread(_pbpaste)) or None

    async def setText(self, text: str) -> None:
        await asyncio.to_thread(_pbcopy, text)

    async def getImage(self) -> bytes | None:
        return await asyncio.to_thread(_grabClipboardPng)

    async def getFilePaths(self) -> list[str] | None:
        return await asyncio.to_thread(_readFinderPaths)


class _Win32PlatformHelper:
    """``win32-platform.node``: enableVirtualTerminalInput, isModifierPressed, getText, setText, getImage."""

    KEY_PRESSED_MASK = 0x8000
    VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
    VK_LWIN, VK_RWIN = 0x5B, 0x5C
    VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5

    def enableVirtualTerminalInput(self) -> bool:
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        STD_INPUT_HANDLE = -10
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint()
        return bool(
            handle not in {0, -1}
            and kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT)
        )

    def isKeyPressed(self, virtualKey: int) -> bool:
        return (ctypes.windll.user32.GetAsyncKeyState(virtualKey) & 0xFFFF & self.KEY_PRESSED_MASK) != 0  # type: ignore[attr-defined]

    def isModifierPressed(self, name: str) -> bool:
        if name == "shift":
            return self.isKeyPressed(self.VK_SHIFT) or self.isKeyPressed(self.VK_LSHIFT) or self.isKeyPressed(self.VK_RSHIFT)
        if name == "control":
            return (
                self.isKeyPressed(self.VK_CONTROL)
                or self.isKeyPressed(self.VK_LCONTROL)
                or self.isKeyPressed(self.VK_RCONTROL)
            )
        if name == "option" or name == "alt":
            return self.isKeyPressed(self.VK_MENU) or self.isKeyPressed(self.VK_LMENU) or self.isKeyPressed(self.VK_RMENU)
        if name == "command" or name == "super" or name == "win":
            return self.isKeyPressed(self.VK_LWIN) or self.isKeyPressed(self.VK_RWIN)
        return False

    async def getText(self) -> str | None:
        from misaka.utils import win_clipboard

        return await asyncio.to_thread(win_clipboard.paste)

    async def setText(self, text: str) -> None:
        from misaka.utils import win_clipboard

        await asyncio.to_thread(win_clipboard.copy, text)

    async def getImage(self) -> bytes | None:
        return await asyncio.to_thread(_grabClipboardPng)

    # MISAKA fork: the files copied in Explorer (CF_HDROP); the Windows addon exports no `getFilePaths`.
    async def getFilePaths(self) -> list[str] | None:
        from misaka.utils import win_clipboard

        return await asyncio.to_thread(win_clipboard.file_paths)


class _LinuxX11ClipboardHelper:
    """``linux-platform-x11.node``: getText, getImage (no setText: Linux keeps clipboard ownership through the
    command-line tools)."""

    def __init__(self, paste: Callable[[], str]) -> None:
        self.paste = paste

    async def getText(self) -> str | None:
        return (await asyncio.to_thread(self.paste)) or None

    async def getImage(self) -> bytes | None:
        return await asyncio.to_thread(_grabClipboardPng)


def buildNativePlatformHelper(platform: str, suffix: str) -> NativePlatformHelper | None:
    """MISAKA fork: what ``require`` of the platform's addon gives upstream."""
    if platform == "darwin":
        return _DarwinPlatformHelper()
    if platform == "win32":
        return _Win32PlatformHelper()
    if platform == "linux" and suffix == "-x11":
        try:
            import pyperclip  # type: ignore[import-not-found]
        except ImportError:
            return None
        return _LinuxX11ClipboardHelper(pyperclip.paste)
    return None


def loadNativePlatformHelper(platform: str, suffix: str = "") -> NativePlatformHelper | None:
    nativePath = f"{platform}-platform{suffix}"
    if nativePath in helpers:
        return helpers.get(nativePath)

    helper = buildNativePlatformHelper(platform, suffix)
    helpers[nativePath] = helper
    return helper


def getNativePlatformHelper() -> NativePlatformHelper | None:
    if sys.platform != "darwin" and sys.platform != "win32":
        return None
    return loadNativePlatformHelper(sys.platform)


def getNativeClipboard() -> NativeClipboard | None:
    """Load a clipboard helper without opening the display until a read is requested."""
    if sys.platform != "linux":
        return getNativePlatformHelper()
    if not os.environ.get("DISPLAY"):
        return None
    return loadNativePlatformHelper("linux", "-x11")


__all__ = [
    "ModifierKey",
    "NativeClipboard",
    "getNativeClipboard",
    "getNativePlatformHelper",
]
