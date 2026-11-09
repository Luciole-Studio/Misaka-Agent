"""Image MIME sniffing helpers."""

from __future__ import annotations

import asyncio

IMAGE_TYPE_SNIFF_BYTES = 4100
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def detectSupportedImageMimeType(buffer: bytes | bytearray | memoryview) -> str | None:
    view = bytes(buffer)
    if startsWith(view, b"\xff\xd8\xff"):
        if len(view) > 3 and view[3] == 0xF7:
            return None
        return "image/jpeg"
    if startsWith(view, PNG_SIGNATURE):
        return "image/png" if isPng(view) and not isAnimatedPng(view) else None
    if startsWithAscii(view, 0, "GIF"):
        return "image/gif"
    if startsWithAscii(view, 0, "RIFF") and startsWithAscii(view, 8, "WEBP"):
        return "image/webp"
    if startsWithAscii(view, 0, "BM") and isBmp(view):
        return "image/bmp"
    return None


async def detectSupportedImageMimeTypeFromFile(file_path: str) -> str | None:
    def _read_prefix() -> bytes:
        with open(file_path, "rb") as handle:
            return handle.read(IMAGE_TYPE_SNIFF_BYTES)

    buffer = await asyncio.to_thread(_read_prefix)
    return detectSupportedImageMimeType(buffer)


def isPng(buffer: bytes) -> bool:
    return len(buffer) >= 16 and readUint32BE(buffer, len(PNG_SIGNATURE)) == 13 and startsWithAscii(
        buffer,
        12,
        "IHDR",
    )


def isAnimatedPng(buffer: bytes) -> bool:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(buffer):
        chunk_length = readUint32BE(buffer, offset)
        chunk_type_offset = offset + 4
        if startsWithAscii(buffer, chunk_type_offset, "acTL"):
            return True
        if startsWithAscii(buffer, chunk_type_offset, "IDAT"):
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > len(buffer):
            return False
        offset = next_offset
    return False


def isBmp(buffer: bytes) -> bool:
    if len(buffer) < 26:
        return False

    declared_file_size = readUint32LE(buffer, 2)
    pixel_data_offset = readUint32LE(buffer, 10)
    dib_header_size = readUint32LE(buffer, 14)
    if declared_file_size != 0 and declared_file_size < 26:
        return False
    if pixel_data_offset < 14 + dib_header_size:
        return False
    if declared_file_size != 0 and pixel_data_offset >= declared_file_size:
        return False

    if dib_header_size == 12:
        color_planes = readUint16LE(buffer, 22)
        bits_per_pixel = readUint16LE(buffer, 24)
    elif 40 <= dib_header_size <= 124:
        if len(buffer) < 30:
            return False
        color_planes = readUint16LE(buffer, 26)
        bits_per_pixel = readUint16LE(buffer, 28)
    else:
        return False

    return color_planes == 1 and bits_per_pixel in {1, 4, 8, 16, 24, 32}


def readUint16LE(buffer: bytes, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 2], "little")


def readUint32BE(buffer: bytes, offset: int) -> int:
    chunk = buffer[offset : offset + 4]
    if len(chunk) < 4:
        return 0
    return int.from_bytes(chunk, "big")


def readUint32LE(buffer: bytes, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "little")


def startsWith(buffer: bytes, prefix: bytes) -> bool:
    return len(buffer) >= len(prefix) and buffer[: len(prefix)] == prefix


def startsWithAscii(buffer: bytes, offset: int, text: str) -> bool:
    prefix = text.encode("ascii")
    return len(buffer) >= offset + len(prefix) and buffer[offset : offset + len(prefix)] == prefix



__all__ = [
    "detectSupportedImageMimeType",
    "detectSupportedImageMimeTypeFromFile",
]
