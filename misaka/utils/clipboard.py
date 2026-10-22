"""Translated from pi's ``coding-agent/src/utils/clipboard.ts`` (v1.3.0): copying text, and reading the text and the
copied files."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import uuid
from collections.abc import Mapping
from typing import Any

from misaka.ui.tui import native_platform
from misaka.utils.clipboard_command import runClipboardCommand
from misaka.utils.wsl import isWSL

MAX_OSC52_ENCODED_LENGTH = 100_000


def _platform() -> str:
    """``os.platform()``."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in ("win32", "cygwin"):
        return "win32"
    return sys.platform


def isRemoteSession(env: Mapping[str, str]) -> bool:
    """Remote sessions also emit OSC 52 so the client terminal receives the copy. MISAKA_CLIPBOARD_OSC52=1
    covers remote clients that leave no SSH markers, such as multiplexer attaches (#10114, #10835)
    or IDE daemons (#10826)."""
    # MISAKA fork: `MISAKA_CLIPBOARD_OSC52`, where upstream reads `PI_CLIPBOARD_OSC52` (the `MISAKA_` prefix, as
    # `MISAKA_OFFLINE`).
    return bool(
        env.get("SSH_CONNECTION")
        or env.get("SSH_CLIENT")
        or env.get("MOSH_CONNECTION")
        or env.get("MISAKA_CLIPBOARD_OSC52") == "1"
    )


def emitOsc52(text: str) -> bool:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_OSC52_ENCODED_LENGTH:
        return False
    sys.stdout.write(f"\x1b]52;c;{encoded}\x07")
    # MISAKA fork: Node writes a TTY synchronously; Python's line-buffered stdout would hold the sequence, which
    # carries no newline, until something else writes one.
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        return False
    return True


async def copyViaWindowsClipboard(text: str) -> bool:
    """WSL without WSLg has no Linux display, so the Windows clipboard is written through interop. PowerShell reads the
    text from a file because ``clip.exe`` and PowerShell stdin decode piped bytes with the console code page, which
    mangles non-ASCII UTF-8."""
    tmpFile = os.path.join(tempfile.gettempdir(), f"pi-wsl-clip-{uuid.uuid4()}.txt")
    try:
        fd = os.open(tmpFile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        output = await runClipboardCommand("wslpath", ["-w", tmpFile], {"timeoutMs": 1000})
        winPath = output.decode("utf-8", "replace").strip() if output is not None else None
        if not winPath:
            return False
        escaped = winPath.replace("'", "''")
        script = f"Set-Clipboard -Value ([System.IO.File]::ReadAllText('{escaped}', [System.Text.Encoding]::UTF8))"
        result = await runClipboardCommand("powershell.exe", ["-NoProfile", "-Command", script], {"timeoutMs": 5000})
        return result is not None
    except Exception:  # noqa: BLE001 - upstream `catch { return false; }`
        return False
    finally:
        try:
            os.unlink(tmpFile)
        except OSError:
            # The file may not have been created.
            pass


async def readClipboardText() -> str | None:
    """Read plain text from the system clipboard."""
    commands: list[tuple[str, list[str]]] = []
    # Termux reports platform "android", not "linux" (#10391).
    if os.environ.get("TERMUX_VERSION"):
        commands.append(("termux-clipboard-get", []))
    if _platform() == "linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(("wl-paste", ["--no-newline", "--type", "text"]))
        if os.environ.get("DISPLAY"):
            commands.extend([("xclip", ["-selection", "clipboard", "-out"]), ("xsel", ["--clipboard", "--output"])])
    for command, args in commands:
        output = await runClipboardCommand(command, args, {"timeoutMs": 5000})
        if output is not None:
            return output.decode("utf-8", "replace") or None
    try:
        clipboard: Any = native_platform.getNativeClipboard()
        return (await clipboard.getText() if clipboard is not None else None) or None
    except Exception:  # noqa: BLE001 - upstream `catch { return null; }`
        return None


async def readClipboardFilePaths() -> list[str] | None:
    """Read file paths, such as Finder file copies, from the native clipboard."""
    clipboard: Any = native_platform.getNativeClipboard()
    getFilePaths = getattr(clipboard, "getFilePaths", None) if clipboard is not None else None
    paths = await getFilePaths() if getFilePaths is not None else None
    return paths if paths else None


async def copyToClipboard(text: str) -> None:
    p = _platform()
    env = os.environ
    copied = False
    # Direct writes precede OSC 52 so the terminal cannot race the native writer.
    # Linux tools retain clipboard selection ownership after this call returns.
    if p != "linux":
        try:
            clipboard: Any = native_platform.getNativeClipboard()
            if clipboard is not None and getattr(clipboard, "setText", None) is not None:
                await clipboard.setText(text)
                copied = True
        except Exception:  # noqa: BLE001, S110 - try platform commands next
            pass
    if not copied:
        commands: list[tuple[str, list[str]]] = []
        if p == "darwin":
            commands.append(("pbcopy", []))
        elif p == "win32":
            commands.append(("clip", []))
        else:
            if env.get("TERMUX_VERSION"):
                commands.append(("termux-clipboard-set", []))
            if env.get("WAYLAND_DISPLAY"):
                commands.append(("wl-copy", []))
            if env.get("DISPLAY"):
                commands.extend([("xclip", ["-selection", "clipboard"]), ("xsel", ["--clipboard", "--input"])])
        for command, args in commands:
            if await runClipboardCommand(command, args, {"input": text, "timeoutMs": 5000}) is not None:
                copied = True
                break
    osc52Emitted = False
    if not copied and p == "linux" and isWSL(env):
        # Windows Terminal supports OSC 52; prefer it over the slower PowerShell round trip.
        if env.get("WT_SESSION"):
            osc52Emitted = emitOsc52(text)
        copied = osc52Emitted or await copyViaWindowsClipboard(text)
    # OSC 52 cannot be verified, so a desktop session with a display reports the failure
    # instead (#9618). Without a display the terminal is the only clipboard route (containers,
    # WSL without WSLg), and remote sessions always emit it to reach the client clipboard.
    headless = p == "linux" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY") and not env.get("TERMUX_VERSION")
    oversized = False
    if not osc52Emitted and (isRemoteSession(env) or (not copied and headless)):
        if emitOsc52(text):
            copied = True
        else:
            oversized = True
    if copied:
        return
    if oversized:
        raise RuntimeError("Clipboard unavailable: text exceeds the OSC 52 size limit")
    if env.get("TERMUX_VERSION"):
        raise RuntimeError("Clipboard unavailable: install the Termux:API app and `termux-api` package")
    if p == "linux":
        if env.get("WAYLAND_DISPLAY"):
            raise RuntimeError("Clipboard unavailable: install `wl-clipboard` (`wl-copy`) or check Wayland access")
        if env.get("DISPLAY"):
            raise RuntimeError("Clipboard unavailable: install `xclip` or `xsel`, or check X11 access")
    raise RuntimeError("Clipboard unavailable")


__all__ = ["copyToClipboard", "readClipboardFilePaths", "readClipboardText"]
