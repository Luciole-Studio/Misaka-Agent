"""Translated from pi's ``coding-agent/src/utils/clipboard-image.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from collections.abc import Mapping
from io import BytesIO
from typing import Any, TypedDict

from PIL import Image

from misaka.ui.tui import native_platform
from misaka.utils.clipboard_command import runClipboardCommand
from misaka.utils.mime import detectSupportedImageMimeType
from misaka.utils.wsl import isWSL


class ClipboardImage:
    def __init__(self, *, bytes: bytes, mimeType: str) -> None:
        self.bytes = bytes
        self.mimeType = mimeType

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ClipboardImage) and (self.bytes, self.mimeType) == (other.bytes, other.mimeType)

    def __repr__(self) -> str:
        return f"ClipboardImage(mimeType={self.mimeType!r}, bytes={len(self.bytes)})"


class _NoImage:
    """MISAKA fork: pi's readers answer ``null`` (the backend has no image) or ``undefined`` (the backend failed).
    Python has one null, so ``None`` plays ``undefined`` and this falsy marker plays ``null``: an empty Wayland
    clipboard must not fall through to stale X11 clipboard contents."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False


NO_IMAGE = _NoImage()

SUPPORTED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

DEFAULT_LIST_TIMEOUT_MS = 1000
DEFAULT_POWERSHELL_TIMEOUT_MS = 5000


def isWaylandSession(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"


def baseMimeType(mimeType: str) -> str:
    return mimeType.split(";")[0].strip().lower()


def extensionForImageMimeType(mimeType: str) -> str | None:
    match baseMimeType(mimeType):
        case "image/png":
            return "png"
        case "image/jpeg":
            return "jpg"
        case "image/webp":
            return "webp"
        case "image/gif":
            return "gif"
        case _:
            return None


def selectPreferredImageMimeType(mimeTypes: list[str]) -> str | None:
    normalized = [{"raw": t, "base": baseMimeType(t)} for t in (t.strip() for t in mimeTypes) if t]

    for preferred in SUPPORTED_IMAGE_MIME_TYPES:
        match = next((t for t in normalized if t["base"] == preferred), None)
        if match is not None:
            return match["raw"]

    anyImage = next((t for t in normalized if t["base"].startswith("image/")), None)
    return anyImage["raw"] if anyImage is not None else None


def isSupportedImageMimeType(mimeType: str) -> bool:
    base = baseMimeType(mimeType)
    return any(t == base for t in SUPPORTED_IMAGE_MIME_TYPES)


async def convertToPng(bytes: bytes) -> bytes | None:
    """Convert unsupported image formats to PNG.
    Returns null if conversion is unavailable or fails.
    MISAKA fork: Pillow does the decoding pi does with Photon, off the event loop."""

    def convert() -> bytes | None:
        try:
            with Image.open(BytesIO(bytes)) as image:
                output = BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
        except Exception:  # noqa: BLE001 - upstream `catch { return null; }`
            return None

    return await asyncio.to_thread(convert)


def _lines(output: bytes) -> list[str]:
    """``toString("utf-8").split(/\\r?\\n/).map(trim).filter(Boolean)``."""
    return [line.strip() for line in output.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n") if line.strip()]


# Undefined means the backend failed; null means it has no image. An empty
# Wayland clipboard must not fall through to stale X11 clipboard contents.
async def readClipboardImageViaWlPaste() -> ClipboardImage | _NoImage | None:
    listed = await runClipboardCommand("wl-paste", ["--list-types"], {"timeoutMs": DEFAULT_LIST_TIMEOUT_MS})
    if listed is None:
        return None

    types = _lines(listed)

    selectedType = selectPreferredImageMimeType(types)
    if not selectedType:
        return NO_IMAGE

    data = await runClipboardCommand("wl-paste", ["--type", selectedType, "--no-newline"])
    if data is None:
        return None
    if len(data) == 0:
        return NO_IMAGE

    return ClipboardImage(bytes=data, mimeType=baseMimeType(selectedType))


async def readClipboardImageViaPowerShell() -> ClipboardImage | _NoImage:
    """On WSL, the Linux clipboard (Wayland/X11) does not receive image data from
    Windows screenshots (Win+Shift+S). PowerShell can access the Windows clipboard
    directly, so we use it as a fallback."""
    tmpFile = os.path.join(tempfile.gettempdir(), f"pi-wsl-clip-{uuid.uuid4()}.png")

    try:
        winPathResult = await runClipboardCommand("wslpath", ["-w", tmpFile], {"timeoutMs": DEFAULT_LIST_TIMEOUT_MS})
        if winPathResult is None:
            return NO_IMAGE

        winPath = winPathResult.decode("utf-8", errors="replace").strip()
        if not winPath:
            return NO_IMAGE

        psQuotedWinPath = winPath.replace("'", "''")
        psScript = "; ".join([
            "Add-Type -AssemblyName System.Windows.Forms",
            "Add-Type -AssemblyName System.Drawing",
            f"$path = '{psQuotedWinPath}'",
            "$img = [System.Windows.Forms.Clipboard]::GetImage()",
            "if ($img) { $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); Write-Output 'ok' } else { Write-Output 'empty' }",
        ])

        result = await runClipboardCommand(
            "powershell.exe", ["-NoProfile", "-Command", psScript], {"timeoutMs": DEFAULT_POWERSHELL_TIMEOUT_MS}
        )
        if result is None:
            return NO_IMAGE

        output = result.decode("utf-8", errors="replace").strip()
        if output != "ok":
            return NO_IMAGE

        with open(tmpFile, "rb") as file:  # noqa: ASYNC230 - pi reads it with readFileSync too
            bytes_ = file.read()
        if len(bytes_) == 0:
            return NO_IMAGE

        return ClipboardImage(bytes=bytes_, mimeType="image/png")
    except Exception:  # noqa: BLE001 - upstream `catch { return null; }`
        return NO_IMAGE
    finally:
        try:
            os.unlink(tmpFile)
        except OSError:
            pass  # Ignore cleanup errors.


async def readClipboardImageViaXclip() -> ClipboardImage | _NoImage | None:
    targets = await runClipboardCommand(
        "xclip", ["-selection", "clipboard", "-t", "TARGETS", "-o"], {"timeoutMs": DEFAULT_LIST_TIMEOUT_MS}
    )

    if targets is None:
        return None

    candidateTypes = _lines(targets)
    preferred = selectPreferredImageMimeType(candidateTypes)
    if not preferred:
        return NO_IMAGE

    data = await runClipboardCommand("xclip", ["-selection", "clipboard", "-t", preferred, "-o"])
    if data is None:
        return None
    if len(data) == 0:
        return NO_IMAGE
    return ClipboardImage(bytes=data, mimeType=baseMimeType(preferred))


async def readClipboardImageViaNativeClipboard() -> ClipboardImage | _NoImage | None:
    clipboard: Any = native_platform.getNativeClipboard()
    bytes_ = await clipboard.getImage() if clipboard is not None else None
    if bytes_ is None:
        return None
    if not bytes_:
        return NO_IMAGE
    data = bytes(bytes_)
    return ClipboardImage(bytes=data, mimeType=detectSupportedImageMimeType(data) or "application/octet-stream")


class ReadClipboardImageOptions(TypedDict, total=False):
    env: Mapping[str, str]
    platform: str


async def readClipboardImage(options: ReadClipboardImageOptions | None = None) -> ClipboardImage | None:
    options = options or {}
    env = options.get("env", os.environ)
    platform = options.get("platform") or _platform()

    if env.get("TERMUX_VERSION"):
        return None

    image: ClipboardImage | _NoImage | None = None

    if platform == "linux":
        wsl = isWSL(env)
        if isWaylandSession(env) or wsl:
            image = await readClipboardImageViaWlPaste()
        if image is None:
            image = await readClipboardImageViaXclip()
        # Preserve Linux's empty/unavailable distinction if Windows has no image.
        if not image and wsl:
            image = (await readClipboardImageViaPowerShell()) or image
        if image is None:
            image = await readClipboardImageViaNativeClipboard()
    else:
        image = await readClipboardImageViaNativeClipboard()

    if not isinstance(image, ClipboardImage):
        return None

    # Convert unsupported formats (e.g., Windows DIB data wrapped as BMP) to PNG
    if not isSupportedImageMimeType(image.mimeType):
        pngBytes = await convertToPng(image.bytes)
        if not pngBytes:
            return None
        return ClipboardImage(bytes=pngBytes, mimeType="image/png")

    return image


def _platform() -> str:
    """``process.platform``."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "cygwin":
        return "win32"
    return sys.platform


__all__ = ["ClipboardImage", "extensionForImageMimeType", "isWaylandSession", "readClipboardImage"]
