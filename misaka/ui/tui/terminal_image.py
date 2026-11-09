"""Translated from pi-tui's ``packages/tui/src/terminal-image.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- D1: the Kitty image metadata registry, placement-only commands and cropping (``deleteAllKittyImages``,
  ``deleteAllKittyPlacements``, ``KittyImageMetadata`` ... ``cropKittyImageLine``) serve only the fullscreen
  renderer, which is not ported.
- The capability overrides read ``MISAKA_HYPERLINKS``, ``MISAKA_IMAGE_PROTOCOL`` and ``MISAKA_TRUE_COLOR`` (pi:
  ``PI_*``), as the other renamed terminal variables do.
- A pane of MISAKA's panel (``TERM_PROGRAM=misaka``) gets no images and no hyperlinks.
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, TypedDict

from misaka.ui.tui.colors import TerminalColorMode
from misaka.utils import node_path
from misaka.utils.js import UNDEFINED, bufferFromBase64, toWellFormed

type ImageProtocol = Literal["kitty", "iterm2"] | None


@dataclass(slots=True)
class TerminalCapabilities:
    images: ImageProtocol
    trueColor: bool
    hyperlinks: bool


@dataclass(slots=True)
class CellDimensions:
    widthPx: int
    heightPx: int


@dataclass(slots=True)
class ImageDimensions:
    widthPx: int
    heightPx: int


class ImageRenderOptions(TypedDict, total=False):
    maxWidthCells: int | None
    maxHeightCells: int | None
    preserveAspectRatio: bool | None
    # Kitty image ID. If provided, reuses/replaces existing image with this ID.
    imageId: int | None
    # Whether Kitty should apply its default cursor movement after placement.
    moveCursor: bool | None


cachedCapabilities: TerminalCapabilities | None = None
capabilityOverrides: dict[str, Any] = {}

# Default cell dimensions - updated by TUI when terminal responds to query
cellDimensions: CellDimensions = CellDimensions(widthPx=9, heightPx=18)


def getCellDimensions() -> CellDimensions:
    return cellDimensions


def setCellDimensions(dims: CellDimensions | dict[str, int]) -> None:
    global cellDimensions
    # MISAKA fork: a `{widthPx, heightPx}` mapping (the object literal callers write upstream) works too.
    if isinstance(dims, dict):
        dims = CellDimensions(widthPx=dims["widthPx"], heightPx=dims["heightPx"])
    cellDimensions = dims


def probeTmuxHyperlinks() -> bool:
    """Checks whether the attached tmux client forwards OSC 8 hyperlinks to the
    outer terminal. tmux only re-emits them when its `client_termfeatures` lists
    `hyperlinks`, and strips them otherwise. On any error fallbacks `false`.
    """
    try:
        termfeatures = subprocess.run(
            ["tmux", "display-message", "-p", "#{client_termfeatures}"],
            capture_output=True,
            text=True,
            timeout=0.25,
            stdin=subprocess.DEVNULL,
            check=True,
        ).stdout
        return "hyperlinks" in [feature.strip() for feature in termfeatures.split(",")]
    except Exception:  # noqa: BLE001 - upstream `catch { return false }`
        return False


def detectCapabilitiesFromEnvironment(tmuxForwardsHyperlink: Callable[[], bool]) -> TerminalCapabilities:
    termProgram = (os.environ.get("TERM_PROGRAM") or "").lower()
    terminalEmulator = (os.environ.get("TERMINAL_EMULATOR") or "").lower()
    term = (os.environ.get("TERM") or "").lower()
    colorTerm = (os.environ.get("COLORTERM") or "").lower()
    hasTrueColorHint = colorTerm == "truecolor" or colorTerm == "24bit" or term.endswith("-direct")
    isWindowsConsole = sys.platform == "win32"

    # Emit OSC 8 hyperlinks only when tmux confirms it forwards.
    # Image protocols are unreliable under tmux, so leave `images: null`.
    if os.environ.get("TMUX") or term.startswith("tmux"):
        return TerminalCapabilities(images=None, trueColor=hasTrueColorHint, hyperlinks=tmuxForwardsHyperlink())

    # screen does not forward OSC 8 hyperlinks, so keep them off there.
    if term.startswith("screen"):
        return TerminalCapabilities(images=None, trueColor=hasTrueColorHint, hyperlinks=False)

    # Herdr forwards OSC 8 hyperlinks and supports the Kitty graphics protocol. Check it before
    # outer-terminal detection because variables like KITTY_WINDOW_ID can leak into its panes.
    if termProgram == "herdr":
        return TerminalCapabilities(images="kitty", trueColor=hasTrueColorHint, hyperlinks=True)

    # MISAKA fork: a pane of MISAKA's panel draws through the panel's own emulator, which shows neither image
    # protocol nor hyperlinks. The host's variables may leak into the pane, so this comes before them too.
    if termProgram == "misaka":
        return TerminalCapabilities(images=None, trueColor=hasTrueColorHint, hyperlinks=False)

    if os.environ.get("KITTY_WINDOW_ID") or termProgram == "kitty":
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if termProgram == "ghostty" or "ghostty" in term or os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if os.environ.get("WEZTERM_PANE") or termProgram == "wezterm":
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    # Warp supports the Kitty graphics protocol and OSC 8 hyperlinks.
    if termProgram == "warpterminal" or os.environ.get("WARP_SESSION_ID") or os.environ.get("WARP_TERMINAL_SESSION_UUID"):
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if os.environ.get("ITERM_SESSION_ID") or termProgram == "iterm.app":
        return TerminalCapabilities(images="iterm2", trueColor=True, hyperlinks=True)

    if os.environ.get("WT_SESSION"):
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=True)

    if termProgram == "alacritty" or termProgram == "vscode" or termProgram == "zed":
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=True)

    if terminalEmulator == "jetbrains-jediterm":
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=False)

    # Windows Terminal does not always set WT_SESSION, for example when it hosts
    # a cmd.exe launched directly from Win+R. Modern Windows consoles support
    # truecolor; keep hyperlinks off unless we positively detected support above.
    if isWindowsConsole:
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=False)

    # Unknown terminal: be conservative. OSC 8 is rendered invisibly as "just
    # text" on terminals that swallow it, which means the URL disappears from
    # the rendered output. Default to the legacy `text (url)` behavior unless we
    # have positively identified a hyperlink-capable terminal above.
    return TerminalCapabilities(images=None, trueColor=hasTrueColorHint, hyperlinks=False)


def parseBooleanCapabilityOverride(value: str | None) -> bool | None:
    return True if value == "1" else False if value == "0" else None


def detectCapabilities(tmuxForwardsHyperlink: Callable[[], bool] | None = None) -> TerminalCapabilities:
    if tmuxForwardsHyperlink is None:
        tmuxForwardsHyperlink = probeTmuxHyperlinks
    # MISAKA fork: the overrides are `MISAKA_*` (pi: `PI_HYPERLINKS`, `PI_IMAGE_PROTOCOL`, `PI_TRUE_COLOR`).
    hyperlinks = parseBooleanCapabilityOverride(os.environ.get("MISAKA_HYPERLINKS"))
    detected = detectCapabilitiesFromEnvironment(
        tmuxForwardsHyperlink if hyperlinks is None else lambda: hyperlinks,
    )
    imageProtocol = (os.environ.get("MISAKA_IMAGE_PROTOCOL") or "").lower() or None
    images: Any = (
        imageProtocol
        if imageProtocol == "kitty" or imageProtocol == "iterm2"
        else None
        if imageProtocol == "none" or imageProtocol == "0"
        else UNDEFINED
    )
    trueColor = parseBooleanCapabilityOverride(os.environ.get("MISAKA_TRUE_COLOR"))
    overrides: dict[str, Any] = {
        **({"images": images} if images is not UNDEFINED else {}),
        **({"trueColor": trueColor} if trueColor is not None else {}),
        **({"hyperlinks": hyperlinks} if hyperlinks is not None else {}),
    }
    return replace(detected, **overrides)


def getCapabilities() -> TerminalCapabilities:
    global cachedCapabilities
    if not cachedCapabilities:
        hyperlinks = capabilityOverrides.get("hyperlinks")
        cachedCapabilities = replace(
            detectCapabilities(None if hyperlinks is None else lambda: hyperlinks),
            **capabilityOverrides,
        )
    return cachedCapabilities


def getTerminalColorMode(capabilities: TerminalCapabilities | None = None) -> TerminalColorMode:
    if capabilities is None:
        capabilities = getCapabilities()
    return "truecolor" if capabilities.trueColor else "256color"


def resetCapabilitiesCache() -> None:
    global cachedCapabilities
    cachedCapabilities = None


def setCapabilityOverrides(overrides: dict[str, Any]) -> None:
    """Override selected auto-detected capabilities."""
    global capabilityOverrides, cachedCapabilities
    if (
        capabilityOverrides.get("images", UNDEFINED) == overrides.get("images", UNDEFINED)
        and capabilityOverrides.get("trueColor", UNDEFINED) == overrides.get("trueColor", UNDEFINED)
        and capabilityOverrides.get("hyperlinks", UNDEFINED) == overrides.get("hyperlinks", UNDEFINED)
    ):
        return
    capabilityOverrides = {**overrides}
    cachedCapabilities = None


def setCapabilities(caps: TerminalCapabilities) -> None:
    """Override the cached capabilities. Useful in tests to exercise both code paths."""
    global cachedCapabilities
    cachedCapabilities = caps


KITTY_PREFIX = "\x1b_G"
ITERM2_PREFIX = "\x1b]1337;File="


def isImageLine(line: str) -> bool:
    # Fast path: sequence at line start (single-row images)
    if line.startswith(KITTY_PREFIX) or line.startswith(ITERM2_PREFIX):  # noqa: PIE810 - upstream's two checks
        return True
    # Slow path: sequence elsewhere (multi-row images have cursor-up prefix)
    return KITTY_PREFIX in line or ITERM2_PREFIX in line


def allocateImageId() -> int:
    """Generate a random image ID for Kitty graphics protocol.
    Uses random IDs to avoid collisions between different module instances
    (e.g., main app vs extensions).
    """
    # Use random ID in range [1, 0xffffffff] to avoid collisions
    return math.floor(random.random() * 0xFFFFFFFE) + 1


def encodeKitty(base64Data: str, options: dict[str, Any] | None = None) -> str:
    """``options``: ``columns``, ``rows``, ``imageId`` and ``moveCursor`` (whether Kitty should apply its default
    cursor movement after placement; default true)."""
    if options is None:
        options = {}
    CHUNK_SIZE = 4096

    params: list[str] = ["a=T", "f=100", "q=2"]

    if options.get("moveCursor") is False:
        params.append("C=1")
    if options.get("columns"):
        params.append(f"c={options['columns']}")
    if options.get("rows"):
        params.append(f"r={options['rows']}")
    if options.get("imageId"):
        params.append(f"i={options['imageId']}")

    if len(base64Data) <= CHUNK_SIZE:
        return f"\x1b_G{','.join(params)};{base64Data}\x1b\\"

    chunks: list[str] = []
    offset = 0
    isFirst = True

    while offset < len(base64Data):
        chunk = base64Data[offset : offset + CHUNK_SIZE]
        isLast = offset + CHUNK_SIZE >= len(base64Data)

        if isFirst:
            chunks.append(f"\x1b_G{','.join(params)},m=1;{chunk}\x1b\\")
            isFirst = False
        elif isLast:
            chunks.append(f"\x1b_Gm=0;{chunk}\x1b\\")
        else:
            chunks.append(f"\x1b_Gm=1;{chunk}\x1b\\")

        offset += CHUNK_SIZE

    return "".join(chunks)


def deleteKittyImage(imageId: int) -> str:
    """Delete a Kitty graphics image by ID.
    Uses uppercase 'I' to also free the image data.
    """
    return f"\x1b_Ga=d,d=I,i={imageId},q=2\x1b\\"


# MISAKA fork (D1): upstream's `deleteAllKittyImages` and `deleteAllKittyPlacements` follow here.


def base64ByteLength(base64Data: str) -> int:
    """MISAKA fork: ``Buffer.byteLength(base64Data, "base64")``: the decoded size, counting up to two ``=`` as
    padding."""
    length = len(base64Data)
    if length > 0 and base64Data[length - 1] == "=":
        length -= 1
    if length > 0 and base64Data[length - 1] == "=":
        length -= 1
    return (length * 3) >> 2


def encodeITerm2(base64Data: str, options: dict[str, Any] | None = None) -> str:
    """``options``: ``width``, ``height``, ``name``, ``preserveAspectRatio`` and ``inline``."""
    if options is None:
        options = {}
    params: list[str] = [
        f"inline={1 if options.get('inline') is not False else 0}",
        f"size={base64ByteLength(base64Data)}",
    ]

    if options.get("width", UNDEFINED) is not UNDEFINED:
        params.append(f"width={options['width']}")
    if options.get("height", UNDEFINED) is not UNDEFINED:
        params.append(f"height={options['height']}")
    if options.get("name"):
        import base64

        nameBase64 = base64.b64encode(options["name"].encode("utf-8")).decode("ascii")
        params.append(f"name={nameBase64}")
    if options.get("preserveAspectRatio") is False:
        params.append("preserveAspectRatio=0")

    return f"\x1b]1337;File={';'.join(params)}:{base64Data}\x07"


@dataclass(slots=True)
class ImageCellSize:
    columns: int
    rows: int


# MISAKA fork (D1): upstream's Kitty image metadata registry, placement-only commands and line cropping
# (`KittyImageMetadata` through `cropKittyImageLine`) follow here; only the fullscreen renderer reads them.


def chooseLessDistortedCellCount(upperCount: int, idealCount: float) -> int:
    if upperCount <= 1:
        return upperCount

    lowerCount = upperCount - 1
    upperDistortion = max(upperCount / idealCount, idealCount / upperCount)
    lowerDistortion = max(lowerCount / idealCount, idealCount / lowerCount)
    return lowerCount if lowerDistortion < upperDistortion else upperCount


def calculateImageCellSize(
    imageDimensions: ImageDimensions,
    maxWidthCells: float,
    maxHeightCells: float | None = None,
    cellDimensions: CellDimensions | None = None,
    optimizeAspectRatio: bool = False,
) -> ImageCellSize:
    if cellDimensions is None:
        cellDimensions = CellDimensions(widthPx=9, heightPx=18)
    maxWidth = max(1, math.floor(maxWidthCells))
    maxHeight = None if maxHeightCells is None else max(1, math.floor(maxHeightCells))
    imageWidth = max(1, imageDimensions.widthPx)
    imageHeight = max(1, imageDimensions.heightPx)

    widthScale = (maxWidth * cellDimensions.widthPx) / imageWidth
    heightScale = widthScale if maxHeight is None else (maxHeight * cellDimensions.heightPx) / imageHeight
    scale = min(widthScale, heightScale)

    scaledWidthPx = imageWidth * scale
    scaledHeightPx = imageHeight * scale
    columns = max(1, min(maxWidth, math.ceil(scaledWidthPx / cellDimensions.widthPx)))
    heightRows = scaledHeightPx / cellDimensions.heightPx
    rows = max(1, math.ceil(heightRows))
    if maxHeight is not None:
        rows = min(maxHeight, rows)

    if not optimizeAspectRatio:
        return ImageCellSize(columns=columns, rows=rows)

    if widthScale <= heightScale:
        idealRows = (columns * cellDimensions.widthPx * imageHeight) / (imageWidth * cellDimensions.heightPx)
        rows = chooseLessDistortedCellCount(rows, idealRows)
    else:
        idealColumns = (rows * cellDimensions.heightPx * imageWidth) / (imageHeight * cellDimensions.widthPx)
        columns = chooseLessDistortedCellCount(columns, idealColumns)

    return ImageCellSize(columns=columns, rows=rows)


def calculateImageRows(
    imageDimensions: ImageDimensions,
    targetWidthCells: float,
    cellDimensions: CellDimensions | None = None,
) -> int:
    if cellDimensions is None:
        cellDimensions = CellDimensions(widthPx=9, heightPx=18)
    return calculateImageCellSize(imageDimensions, targetWidthCells, None, cellDimensions).rows


def getPngDimensions(base64Data: str) -> ImageDimensions | None:
    try:
        buffer = bufferFromBase64(base64Data)

        if len(buffer) < 24:
            return None

        if buffer[0] != 0x89 or buffer[1] != 0x50 or buffer[2] != 0x4E or buffer[3] != 0x47:
            return None

        width = int.from_bytes(buffer[16:20], "big")
        height = int.from_bytes(buffer[20:24], "big")

        return ImageDimensions(widthPx=width, heightPx=height)
    except Exception:  # noqa: BLE001 - upstream `catch { return null }`
        return None


def getJpegDimensions(base64Data: str) -> ImageDimensions | None:
    try:
        buffer = bufferFromBase64(base64Data)

        if len(buffer) < 2:
            return None

        if buffer[0] != 0xFF or buffer[1] != 0xD8:
            return None

        offset = 2
        while offset < len(buffer) - 9:
            if buffer[offset] != 0xFF:
                offset += 1
                continue

            marker = buffer[offset + 1]

            if marker >= 0xC0 and marker <= 0xC2:
                height = int.from_bytes(buffer[offset + 5 : offset + 7], "big")
                width = int.from_bytes(buffer[offset + 7 : offset + 9], "big")
                return ImageDimensions(widthPx=width, heightPx=height)

            if offset + 3 >= len(buffer):
                return None
            length = int.from_bytes(buffer[offset + 2 : offset + 4], "big")
            if length < 2:
                return None
            offset += 2 + length

        return None
    except Exception:  # noqa: BLE001 - upstream `catch { return null }`
        return None


def getGifDimensions(base64Data: str) -> ImageDimensions | None:
    try:
        buffer = bufferFromBase64(base64Data)

        if len(buffer) < 10:
            return None

        sig = buffer[0:6].decode("latin-1")
        if sig != "GIF87a" and sig != "GIF89a":
            return None

        width = int.from_bytes(buffer[6:8], "little")
        height = int.from_bytes(buffer[8:10], "little")

        return ImageDimensions(widthPx=width, heightPx=height)
    except Exception:  # noqa: BLE001 - upstream `catch { return null }`
        return None


def getWebpDimensions(base64Data: str) -> ImageDimensions | None:
    try:
        buffer = bufferFromBase64(base64Data)

        if len(buffer) < 30:
            return None

        riff = buffer[0:4].decode("latin-1")
        webp = buffer[8:12].decode("latin-1")
        if riff != "RIFF" or webp != "WEBP":
            return None

        chunk = buffer[12:16].decode("latin-1")
        if chunk == "VP8 ":
            if len(buffer) < 30:
                return None
            width = int.from_bytes(buffer[26:28], "little") & 0x3FFF
            height = int.from_bytes(buffer[28:30], "little") & 0x3FFF
            return ImageDimensions(widthPx=width, heightPx=height)
        elif chunk == "VP8L":
            if len(buffer) < 25:
                return None
            bits = int.from_bytes(buffer[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return ImageDimensions(widthPx=width, heightPx=height)
        elif chunk == "VP8X":
            if len(buffer) < 30:
                return None
            width = (buffer[24] | (buffer[25] << 8) | (buffer[26] << 16)) + 1
            height = (buffer[27] | (buffer[28] << 8) | (buffer[29] << 16)) + 1
            return ImageDimensions(widthPx=width, heightPx=height)

        return None
    except Exception:  # noqa: BLE001 - upstream `catch { return null }`
        return None


def getImageDimensions(base64Data: str, mimeType: str) -> ImageDimensions | None:
    if mimeType == "image/png":
        return getPngDimensions(base64Data)
    if mimeType == "image/jpeg":
        return getJpegDimensions(base64Data)
    if mimeType == "image/gif":
        return getGifDimensions(base64Data)
    if mimeType == "image/webp":
        return getWebpDimensions(base64Data)
    return None


@dataclass(slots=True)
class RenderedImage:
    """MISAKA fork: the ``{ sequence, columns, rows, imageId? }`` object ``renderImage`` returns."""

    sequence: str
    columns: int
    rows: int
    imageId: int | None = None


def renderImage(
    base64Data: str,
    imageDimensions: ImageDimensions,
    options: ImageRenderOptions | None = None,
) -> RenderedImage | None:
    if options is None:
        options = {}
    caps = getCapabilities()

    if not caps.images:
        return None

    maxWidthCells = options.get("maxWidthCells")
    maxWidth = maxWidthCells if maxWidthCells is not None else 80
    # Reduce Kitty's cell-aligned distortion without shrinking iTerm2 reservations.
    size = calculateImageCellSize(
        imageDimensions,
        maxWidth,
        options.get("maxHeightCells"),
        getCellDimensions(),
        caps.images == "kitty",
    )

    if caps.images == "kitty":
        # MISAKA fork (D1): upstream registers the image's metadata here for the fullscreen renderer.
        sequence = encodeKitty(
            base64Data,
            {
                "columns": size.columns,
                "rows": size.rows,
                "imageId": options.get("imageId"),
                "moveCursor": options.get("moveCursor"),
            },
        )
        return RenderedImage(sequence=sequence, columns=size.columns, rows=size.rows, imageId=options.get("imageId"))

    if caps.images == "iterm2":
        preserveAspectRatio = options.get("preserveAspectRatio")
        sequence = encodeITerm2(
            base64Data,
            {
                "width": size.columns,
                "height": "auto",
                "preserveAspectRatio": preserveAspectRatio if preserveAspectRatio is not None else True,
            },
        )
        return RenderedImage(sequence=sequence, columns=size.columns, rows=size.rows)

    return None


def hyperlink(text: str, url: str) -> str:
    """Wrap text in an OSC 8 hyperlink sequence.
    The text is rendered as a clickable hyperlink in terminals that support OSC 8
    (Ghostty, Kitty, WezTerm, iTerm2, VSCode, and others).
    In terminals that do not support OSC 8, the escape sequences are ignored
    and only the plain text is displayed.

    @param text - The visible text to display
    @param url - The URL to link to
    """
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def shortenImagePath(filename: str) -> str:
    """Shorten home-prefixed absolute paths to ~/... for compact display."""
    home = os.path.expanduser("~")
    if home and (filename == home or filename.startswith(f"{home}/") or filename.startswith(f"{home}\\")):  # noqa: PIE810
        return f"~{filename[len(home) :]}"
    return filename


# The characters Node's `pathToFileURL` leaves as they are (letters and digits aside); it encodes every other one.
URL_PATH_SAFE = frozenset("!$&'()*+,-./:;=@_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def pathToFileURL(filename: str) -> str:
    """MISAKA fork: Node's ``pathToFileURL(filename).href``: the resolved path with every character outside
    ``URL_PATH_SAFE`` percent-encoded as UTF-8 (Windows: drive paths become ``/C:/...``, UNC paths name the host)."""
    resolved = node_path.resolve(filename)
    # `path.resolve` drops a trailing separator; it is put back.
    if (
        filename
        and (filename[-1] == "/" or (sys.platform == "win32" and filename[-1] == chr(92)))
        and not resolved.endswith(node_path.sep)
    ):
        resolved += "/"
    host = ""
    if sys.platform == "win32":
        resolved = resolved.replace("\\", "/")
        if resolved.startswith("//"):
            host, _, rest = resolved[2:].partition("/")
            resolved = f"/{rest}"
        else:
            resolved = f"/{resolved}"
    encoded = "".join(
        char if char in URL_PATH_SAFE else "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        for char in toWellFormed(resolved)
    )
    return f"file://{host}{encoded}"


def imageFallback(mimeType: str, dimensions: ImageDimensions | None = None, filename: str | None = None) -> str:
    """Text fallback when the terminal cannot render inline images.
    Absolute paths are shown shortened (~/...) and, when OSC 8 hyperlinks are
    available, linked to file:// so the full path remains openable.
    """
    parts: list[str] = []
    if filename:
        display = shortenImagePath(filename)
        if getCapabilities().hyperlinks and os.path.isabs(filename):
            parts.append(hyperlink(display, pathToFileURL(filename)))
        else:
            parts.append(display)
    parts.append(f"[{mimeType}]")
    if dimensions:
        parts.append(f"{dimensions.widthPx}x{dimensions.heightPx}")
    return f"[Image: {' '.join(parts)}]"


__all__ = [
    "CellDimensions",
    "ImageCellSize",
    "ImageDimensions",
    "ImageProtocol",
    "ImageRenderOptions",
    "RenderedImage",
    "TerminalCapabilities",
    "TerminalColorMode",
    "allocateImageId",
    "calculateImageCellSize",
    "calculateImageRows",
    "deleteKittyImage",
    "detectCapabilities",
    "encodeITerm2",
    "encodeKitty",
    "getCapabilities",
    "getCellDimensions",
    "getGifDimensions",
    "getImageDimensions",
    "getJpegDimensions",
    "getPngDimensions",
    "getTerminalColorMode",
    "getWebpDimensions",
    "hyperlink",
    "imageFallback",
    "isImageLine",
    "renderImage",
    "resetCapabilitiesCache",
    "setCapabilities",
    "setCapabilityOverrides",
    "setCellDimensions",
]
