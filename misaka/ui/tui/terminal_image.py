"""Terminal image protocol helpers and capability detection."""

from __future__ import annotations

import base64
import os
import random
import struct
import subprocess
import sys
from dataclasses import dataclass

type ImageProtocol = str | None


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


@dataclass(slots=True)
class ImageRenderOptions:
    maxWidthCells: int | None = None
    maxHeightCells: int | None = None
    preserveAspectRatio: bool | None = None
    imageId: int | None = None
    moveCursor: bool | None = None


@dataclass(slots=True)
class ImageCellSize:
    columns: int
    rows: int


@dataclass(slots=True)
class _RenderedImage:
    sequence: str
    rows: int
    imageId: int | None = None


_cached_capabilities: TerminalCapabilities | None = None
_cell_dimensions = CellDimensions(widthPx=9, heightPx=18)

KITTY_PREFIX = "\x1b_G"
ITERM2_PREFIX = "\x1b]1337;File="


def getCellDimensions() -> CellDimensions:
    return _cell_dimensions


def setCellDimensions(dims: CellDimensions | dict[str, int]) -> None:
    global _cell_dimensions
    if isinstance(dims, dict):
        _cell_dimensions = CellDimensions(widthPx=int(dims["widthPx"]), heightPx=int(dims["heightPx"]))
        return
    _cell_dimensions = dims


def _probe_tmux_hyperlinks() -> bool:
    """tmux forwards OSC 8 only when client_termfeatures includes hyperlinks; otherwise it strips them. Errors count as False."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "#{client_termfeatures}"],
            capture_output=True, text=True, timeout=0.25, stdin=subprocess.DEVNULL)
        return "hyperlinks" in (part.strip() for part in out.stdout.split(","))
    except Exception:  # noqa: BLE001
        return False


def detectCapabilities(tmux_forwards_hyperlink=None) -> TerminalCapabilities:
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    terminal_emulator = os.environ.get("TERMINAL_EMULATOR", "").lower()
    term = os.environ.get("TERM", "").lower()
    color_term = os.environ.get("COLORTERM", "").lower()
    has_truecolor_hint = color_term in {"truecolor", "24bit"}
    is_windows_console = sys.platform == "win32"

    if os.environ.get("TMUX") or term.startswith("tmux"):
        probe = tmux_forwards_hyperlink or _probe_tmux_hyperlinks
        return TerminalCapabilities(images=None, trueColor=has_truecolor_hint, hyperlinks=probe())

    if term.startswith("screen"):  # screen does not forward OSC 8
        return TerminalCapabilities(images=None, trueColor=has_truecolor_hint, hyperlinks=False)

    if os.environ.get("KITTY_WINDOW_ID") or term_program == "kitty":
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if term_program == "ghostty" or "ghostty" in term or os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if os.environ.get("WEZTERM_PANE") or term_program == "wezterm":
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if (term_program == "warpterminal" or os.environ.get("WARP_SESSION_ID")
            or os.environ.get("WARP_TERMINAL_SESSION_UUID")):
        return TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True)

    if os.environ.get("ITERM_SESSION_ID") or term_program == "iterm.app":
        return TerminalCapabilities(images="iterm2", trueColor=True, hyperlinks=True)

    if os.environ.get("WT_SESSION"):
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=True)

    if term_program == "vscode":
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=True)

    if term_program == "alacritty":
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=True)

    if terminal_emulator == "jetbrains-jediterm":
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=False)

    if is_windows_console:
        return TerminalCapabilities(images=None, trueColor=True, hyperlinks=False)

    return TerminalCapabilities(images=None, trueColor=has_truecolor_hint, hyperlinks=False)


def getCapabilities() -> TerminalCapabilities:
    global _cached_capabilities
    if _cached_capabilities is None:
        _cached_capabilities = detectCapabilities()
    return _cached_capabilities


def resetCapabilitiesCache() -> None:
    global _cached_capabilities
    _cached_capabilities = None


def setCapabilities(caps: TerminalCapabilities | dict[str, object]) -> None:
    global _cached_capabilities
    if isinstance(caps, dict):
        _cached_capabilities = TerminalCapabilities(
            images=caps.get("images"), trueColor=bool(caps.get("trueColor")), hyperlinks=bool(caps.get("hyperlinks"))
        )
        return
    _cached_capabilities = caps


def isImageLine(line: str) -> bool:
    return (
        line.startswith(KITTY_PREFIX) or line.startswith(ITERM2_PREFIX) or KITTY_PREFIX in line or ITERM2_PREFIX in line
    )


def allocateImageId() -> int:
    return random.randint(1, 0xFFFFFFFF)


def encodeKitty(
    base64_data: str,
    options: dict[str, int | bool | None] | None = None,
) -> str:
    options = options or {}
    chunk_size = 4096
    params = ["a=T", "f=100", "q=2"]

    if options.get("moveCursor") is False:
        params.append("C=1")
    if options.get("columns"):
        params.append(f"c={int(options['columns'])}")
    if options.get("rows"):
        params.append(f"r={int(options['rows'])}")
    if options.get("imageId"):
        params.append(f"i={int(options['imageId'])}")

    if len(base64_data) <= chunk_size:
        return f"\x1b_G{','.join(params)};{base64_data}\x1b\\"

    chunks: list[str] = []
    offset = 0
    first = True
    while offset < len(base64_data):
        chunk = base64_data[offset : offset + chunk_size]
        is_last = offset + chunk_size >= len(base64_data)
        if first:
            chunks.append(f"\x1b_G{','.join(params)},m=1;{chunk}\x1b\\")
            first = False
        elif is_last:
            chunks.append(f"\x1b_Gm=0;{chunk}\x1b\\")
        else:
            chunks.append(f"\x1b_Gm=1;{chunk}\x1b\\")
        offset += chunk_size
    return "".join(chunks)


def deleteKittyImage(image_id: int) -> str:
    return f"\x1b_Ga=d,d=I,i={image_id},q=2\x1b\\"


def deleteAllKittyImages() -> str:
    return "\x1b_Ga=d,d=A,q=2\x1b\\"


def encodeITerm2(
    base64_data: str,
    options: dict[str, int | str | bool | None] | None = None,
) -> str:
    options = options or {}
    params = [f"inline={1 if options.get('inline', True) else 0}"]
    if options.get("width") is not None:
        params.append(f"width={options['width']}")
    if options.get("height") is not None:
        params.append(f"height={options['height']}")
    if options.get("name"):
        name_base64 = base64.b64encode(str(options["name"]).encode("utf-8")).decode("ascii")
        params.append(f"name={name_base64}")
    if options.get("preserveAspectRatio") is False:
        params.append("preserveAspectRatio=0")
    return f"\x1b]1337;File={';'.join(params)}:{base64_data}\x07"


def calculateImageCellSize(
    image_dimensions: ImageDimensions,
    max_width_cells: int,
    max_height_cells: int | None = None,
    cell_dimensions: CellDimensions | None = None,
) -> ImageCellSize:
    dims = cell_dimensions or CellDimensions(widthPx=9, heightPx=18)
    max_width = max(1, int(max_width_cells))
    max_height = None if max_height_cells is None else max(1, int(max_height_cells))
    image_width = max(1, int(image_dimensions.widthPx))
    image_height = max(1, int(image_dimensions.heightPx))

    width_scale = (max_width * dims.widthPx) / image_width
    height_scale = width_scale if max_height is None else (max_height * dims.heightPx) / image_height
    scale = min(width_scale, height_scale)

    scaled_width_px = image_width * scale
    scaled_height_px = image_height * scale
    columns = max(1, min(max_width, int(-(-scaled_width_px // dims.widthPx))))
    rows = int(-(-scaled_height_px // dims.heightPx))
    if max_height is not None:
        rows = min(rows, max_height)
    return ImageCellSize(columns=columns, rows=max(1, rows))


def calculateImageRows(
    image_dimensions: ImageDimensions,
    target_width_cells: int,
    cell_dimensions: CellDimensions | None = None,
) -> int:
    dims = cell_dimensions or CellDimensions(widthPx=9, heightPx=18)
    return calculateImageCellSize(image_dimensions, target_width_cells, None, dims).rows


def getPngDimensions(base64_data: str) -> ImageDimensions | None:
    try:
        buffer = base64.b64decode(base64_data)
    except Exception:
        return None
    if len(buffer) < 24 or buffer[:4] != b"\x89PNG":
        return None
    width = struct.unpack(">I", buffer[16:20])[0]
    height = struct.unpack(">I", buffer[20:24])[0]
    return ImageDimensions(widthPx=width, heightPx=height)


def getJpegDimensions(base64_data: str) -> ImageDimensions | None:
    try:
        buffer = base64.b64decode(base64_data)
    except Exception:
        return None
    if len(buffer) < 2 or buffer[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset < len(buffer) - 9:
        if buffer[offset] != 0xFF:
            offset += 1
            continue
        marker = buffer[offset + 1]
        if 0xC0 <= marker <= 0xC2:
            height = struct.unpack(">H", buffer[offset + 5 : offset + 7])[0]
            width = struct.unpack(">H", buffer[offset + 7 : offset + 9])[0]
            return ImageDimensions(widthPx=width, heightPx=height)
        if offset + 3 >= len(buffer):
            return None
        length = struct.unpack(">H", buffer[offset + 2 : offset + 4])[0]
        if length < 2:
            return None
        offset += 2 + length
    return None


def getGifDimensions(base64_data: str) -> ImageDimensions | None:
    try:
        buffer = base64.b64decode(base64_data)
    except Exception:
        return None
    if len(buffer) < 10 or buffer[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    width = struct.unpack("<H", buffer[6:8])[0]
    height = struct.unpack("<H", buffer[8:10])[0]
    return ImageDimensions(widthPx=width, heightPx=height)


def getWebpDimensions(base64_data: str) -> ImageDimensions | None:
    try:
        buffer = base64.b64decode(base64_data)
    except Exception:
        return None
    if len(buffer) < 30 or buffer[:4] != b"RIFF" or buffer[8:12] != b"WEBP":
        return None
    chunk = buffer[12:16]
    if chunk == b"VP8 ":
        width = struct.unpack("<H", buffer[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", buffer[28:30])[0] & 0x3FFF
        return ImageDimensions(widthPx=width, heightPx=height)
    if chunk == b"VP8L":
        bits = struct.unpack("<I", buffer[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ImageDimensions(widthPx=width, heightPx=height)
    if chunk == b"VP8X":
        width = buffer[24] | (buffer[25] << 8) | (buffer[26] << 16)
        height = buffer[27] | (buffer[28] << 8) | (buffer[29] << 16)
        return ImageDimensions(widthPx=width + 1, heightPx=height + 1)
    return None


def getImageDimensions(base64_data: str, mime_type: str) -> ImageDimensions | None:
    if mime_type == "image/png":
        return getPngDimensions(base64_data)
    if mime_type == "image/jpeg":
        return getJpegDimensions(base64_data)
    if mime_type == "image/gif":
        return getGifDimensions(base64_data)
    if mime_type == "image/webp":
        return getWebpDimensions(base64_data)
    return None


def renderImage(
    base64_data: str,
    image_dimensions: ImageDimensions,
    options: ImageRenderOptions | dict[str, int | bool | None] | None = None,
) -> _RenderedImage | None:
    caps = getCapabilities()
    if not caps.images:
        return None

    if options is None:
        options_obj = ImageRenderOptions()
    elif isinstance(options, dict):
        options_obj = ImageRenderOptions(
            maxWidthCells=options.get("maxWidthCells"),
            maxHeightCells=options.get("maxHeightCells"),
            preserveAspectRatio=options.get("preserveAspectRatio"),
            imageId=options.get("imageId"),
            moveCursor=options.get("moveCursor"),
        )
    else:
        options_obj = options

    max_width = 80 if options_obj.maxWidthCells is None else options_obj.maxWidthCells
    size = calculateImageCellSize(image_dimensions, max_width, options_obj.maxHeightCells, getCellDimensions())
    if caps.images == "kitty":
        sequence = encodeKitty(
            base64_data,
            {
                "columns": size.columns,
                "rows": size.rows,
                "imageId": options_obj.imageId,
                "moveCursor": options_obj.moveCursor,
            },
        )
        return _RenderedImage(sequence=sequence, rows=size.rows, imageId=options_obj.imageId)
    if caps.images == "iterm2":
        sequence = encodeITerm2(
            base64_data,
            {
                "width": size.columns,
                "height": "auto",
                "preserveAspectRatio": (
                    options_obj.preserveAspectRatio if options_obj.preserveAspectRatio is not None else True
                ),
            },
        )
        return _RenderedImage(sequence=sequence, rows=size.rows)
    return None


def hyperlink(text: str, url: str) -> str:
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def _shorten_image_path(filename: str) -> str:
    home = os.path.expanduser("~")
    if home and (filename == home or filename.startswith(home + os.sep)):
        return "~" + filename[len(home):]
    return filename


def imageFallback(mime_type: str, dimensions: ImageDimensions | None = None, filename: str | None = None) -> str:
    parts: list[str] = []
    if filename:
        display = _shorten_image_path(filename)
        if getCapabilities().hyperlinks and os.path.isabs(filename):
            from pathlib import Path
            parts.append(hyperlink(display, Path(filename).as_uri()))
        else:
            parts.append(display)
    parts.append(f"[{mime_type}]")
    if dimensions is not None:
        parts.append(f"{dimensions.widthPx}x{dimensions.heightPx}")
    return f"[Image: {' '.join(parts)}]"

__all__ = [
    "CellDimensions",
    "ImageCellSize",
    "ImageDimensions",
    "ImageProtocol",
    "ImageRenderOptions",
    "TerminalCapabilities",
    "allocateImageId",
    "calculateImageCellSize",
    "calculateImageRows",
    "deleteAllKittyImages",
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
    "getWebpDimensions",
    "hyperlink",
    "imageFallback",
    "isImageLine",
    "renderImage",
    "resetCapabilitiesCache",
    "setCapabilities",
    "setCellDimensions",
]
