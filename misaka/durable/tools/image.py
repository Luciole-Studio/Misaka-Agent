"""Detection of the image formats the read tool reads, and the dimensions their headers declare.

Translated from pi's ``packages/durable/src/tools/image.ts`` (v1.3.0).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
# Bytes every check except the APNG chunk walk needs: BMP reads up to offset 29.
HEADER_BYTES = 32
BLOCK_BYTES = 64 * 1024


class ByteSource(Protocol):
    """Positional reads of a file of ``size`` bytes."""

    size: int
    read: Callable[[int, int], Awaitable[bytes]]


async def detectSupportedImageMimeTypeOf(source: ByteSource) -> str | None:
    """``detectSupportedImageMimeType`` of a whole file, reading only its header and, for PNG, the chunk headers up to
    the first ``acTL`` or ``IDAT``."""
    header = await source.read(0, HEADER_BYTES)
    if not startsWith(header, PNG_SIGNATURE):
        return detectSupportedImageMimeType(header)
    return "image/png" if isPng(header) and not await _isAnimatedPngOf(source) else None


async def _isAnimatedPngOf(source: ByteSource) -> bool:
    """``isAnimatedPng`` over a file read in blocks."""
    block = b""
    blockStart = 0

    async def bytesAt(offset: int, length: int) -> bytes:
        nonlocal block, blockStart
        if offset < blockStart or offset + length > blockStart + len(block):
            blockStart = offset
            block = await source.read(offset, BLOCK_BYTES)
        return block[offset - blockStart : offset - blockStart + length]

    offset = len(PNG_SIGNATURE)
    while offset + 8 <= source.size:
        chunkHeader = await bytesAt(offset, 8)
        chunkLength = readUint32BE(chunkHeader, 0)
        if startsWithAscii(chunkHeader, 4, "acTL"):
            return True
        if startsWithAscii(chunkHeader, 4, "IDAT"):
            return False
        nextOffset = offset + 8 + chunkLength + 4
        if nextOffset <= offset or nextOffset > source.size:
            return False
        offset = nextOffset
    return False


def detectSupportedImageMimeType(buffer: bytes) -> str | None:
    if startsWith(buffer, bytes([0xFF, 0xD8, 0xFF])):
        return None if len(buffer) > 3 and buffer[3] == 0xF7 else "image/jpeg"
    if startsWith(buffer, PNG_SIGNATURE):
        return "image/png" if isPng(buffer) and not isAnimatedPng(buffer) else None
    if startsWithAscii(buffer, 0, "GIF87a") or startsWithAscii(buffer, 0, "GIF89a"):
        return "image/gif"
    if startsWithAscii(buffer, 0, "RIFF") and startsWithAscii(buffer, 8, "WEBP"):
        return "image/webp"
    if startsWithAscii(buffer, 0, "BM") and isBmp(buffer):
        return "image/bmp"
    return None


def isPng(buffer: bytes) -> bool:
    return len(buffer) >= 16 and readUint32BE(buffer, len(PNG_SIGNATURE)) == 13 and startsWithAscii(buffer, 12, "IHDR")


def isAnimatedPng(buffer: bytes) -> bool:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(buffer):
        chunkLength = readUint32BE(buffer, offset)
        chunkTypeOffset = offset + 4
        if startsWithAscii(buffer, chunkTypeOffset, "acTL"):
            return True
        if startsWithAscii(buffer, chunkTypeOffset, "IDAT"):
            return False
        nextOffset = offset + 8 + chunkLength + 4
        if nextOffset <= offset or nextOffset > len(buffer):
            return False
        offset = nextOffset
    return False


def imageDimensions(data: bytes, mimeType: str) -> dict[str, int] | None:
    """The width and height an image of ``mimeType`` declares in its header, without decoding it: PNG's IHDR, GIF's
    screen, WebP's VP8, VP8L, or VP8X chunk, JPEG's first SOF segment. ``None`` when the header cannot be read. (The
    first parameter is pi's ``bytes``; Python needs that name for the type.)"""
    if mimeType == "image/png":
        return None if len(data) < 24 else {"width": readUint32BE(data, 16), "height": readUint32BE(data, 20)}
    if mimeType == "image/gif":
        return None if len(data) < 10 else {"width": readUint16LE(data, 6), "height": readUint16LE(data, 8)}
    if mimeType == "image/webp":
        if len(data) < 30:
            return None
        if startsWithAscii(data, 12, "VP8 "):
            return {"width": readUint16LE(data, 26) & 0x3FFF, "height": readUint16LE(data, 28) & 0x3FFF}
        if startsWithAscii(data, 12, "VP8L"):
            bits = readUint32LE(data, 21)
            return {"width": (bits & 0x3FFF) + 1, "height": ((bits >> 14) & 0x3FFF) + 1}
        if startsWithAscii(data, 12, "VP8X"):

            def uint24(offset: int) -> int:
                return readUint16LE(data, offset) + (byteAt(data, offset + 2) << 16)

            return {"width": uint24(24) + 1, "height": uint24(27) + 1}
        return None
    if mimeType == "image/jpeg":
        offset = 2
        while offset + 9 <= len(data):
            if data[offset] != 0xFF:
                return None
            marker = data[offset + 1]
            if marker == 0xFF:
                offset += 1
                continue
            # SOF0 to SOF15, except DHT (C4), JPG (C8), and DAC (CC), which share the range.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return {"width": readUint16BE(data, offset + 7), "height": readUint16BE(data, offset + 5)}
            offset += 2 + readUint16BE(data, offset + 2)
        return None
    return None


def isBmp(buffer: bytes) -> bool:
    if len(buffer) < 26:
        return False
    declaredFileSize = readUint32LE(buffer, 2)
    pixelDataOffset = readUint32LE(buffer, 10)
    dibHeaderSize = readUint32LE(buffer, 14)
    if declaredFileSize != 0 and declaredFileSize < 26:
        return False
    if pixelDataOffset < 14 + dibHeaderSize:
        return False
    if declaredFileSize != 0 and pixelDataOffset >= declaredFileSize:
        return False

    if dibHeaderSize == 12:
        colorPlanes = readUint16LE(buffer, 22)
        bitsPerPixel = readUint16LE(buffer, 24)
    elif 40 <= dibHeaderSize <= 124:
        if len(buffer) < 30:
            return False
        colorPlanes = readUint16LE(buffer, 26)
        bitsPerPixel = readUint16LE(buffer, 28)
    else:
        return False
    return colorPlanes == 1 and bitsPerPixel in (1, 4, 8, 16, 24, 32)


def byteAt(buffer: bytes, offset: int) -> int:
    return buffer[offset] if offset < len(buffer) else 0


def readUint16LE(buffer: bytes, offset: int) -> int:
    return byteAt(buffer, offset) + (byteAt(buffer, offset + 1) << 8)


def readUint16BE(buffer: bytes, offset: int) -> int:
    return (byteAt(buffer, offset) << 8) + byteAt(buffer, offset + 1)


def readUint32BE(buffer: bytes, offset: int) -> int:
    return (
        byteAt(buffer, offset) * 0x1000000
        + (byteAt(buffer, offset + 1) << 16)
        + (byteAt(buffer, offset + 2) << 8)
        + byteAt(buffer, offset + 3)
    )


def readUint32LE(buffer: bytes, offset: int) -> int:
    return (
        byteAt(buffer, offset)
        + (byteAt(buffer, offset + 1) << 8)
        + (byteAt(buffer, offset + 2) << 16)
        + byteAt(buffer, offset + 3) * 0x1000000
    )


# The second parameter is pi's `bytes`; Python needs that name for the type.
def startsWith(buffer: bytes, prefix: bytes) -> bool:
    return buffer[: len(prefix)] == prefix


def startsWithAscii(buffer: bytes, offset: int, text: str) -> bool:
    if len(buffer) < offset + len(text):
        return False
    return buffer[offset : offset + len(text)] == text.encode("ascii")


__all__ = ["ByteSource", "detectSupportedImageMimeType", "detectSupportedImageMimeTypeOf", "imageDimensions"]
