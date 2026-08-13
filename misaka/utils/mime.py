"""Image MIME sniffing helpers."""

from __future__ import annotations

import asyncio

IMAGE_TYPE_SNIFF_BYTES = 4100
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def detect_supported_image_mime_type(buffer: bytes | bytearray | memoryview) -> str | None:
    view = bytes(buffer)
    if _starts_with(view, b"\xff\xd8\xff"):
        if len(view) > 3 and view[3] == 0xF7:
            return None
        return "image/jpeg"
    if _starts_with(view, PNG_SIGNATURE):
        return "image/png" if _is_png(view) and not _is_animated_png(view) else None
    if _starts_with_ascii(view, 0, "GIF"):
        return "image/gif"
    if _starts_with_ascii(view, 0, "RIFF") and _starts_with_ascii(view, 8, "WEBP"):
        return "image/webp"
    return None


async def detect_supported_image_mime_type_from_file(file_path: str) -> str | None:
    def _read_prefix() -> bytes:
        with open(file_path, "rb") as handle:
            return handle.read(IMAGE_TYPE_SNIFF_BYTES)

    buffer = await asyncio.to_thread(_read_prefix)
    return detect_supported_image_mime_type(buffer)


def _is_png(buffer: bytes) -> bool:
    return len(buffer) >= 16 and _read_uint32_be(buffer, len(PNG_SIGNATURE)) == 13 and _starts_with_ascii(
        buffer,
        12,
        "IHDR",
    )


def _is_animated_png(buffer: bytes) -> bool:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(buffer):
        chunk_length = _read_uint32_be(buffer, offset)
        chunk_type_offset = offset + 4
        if _starts_with_ascii(buffer, chunk_type_offset, "acTL"):
            return True
        if _starts_with_ascii(buffer, chunk_type_offset, "IDAT"):
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > len(buffer):
            return False
        offset = next_offset
    return False


def _read_uint32_be(buffer: bytes, offset: int) -> int:
    chunk = buffer[offset : offset + 4]
    if len(chunk) < 4:
        return 0
    return int.from_bytes(chunk, "big")


def _starts_with(buffer: bytes, prefix: bytes) -> bool:
    return len(buffer) >= len(prefix) and buffer[: len(prefix)] == prefix


def _starts_with_ascii(buffer: bytes, offset: int, text: str) -> bool:
    prefix = text.encode("ascii")
    return len(buffer) >= offset + len(prefix) and buffer[offset : offset + len(prefix)] == prefix


detectSupportedImageMimeType = detect_supported_image_mime_type
detectSupportedImageMimeTypeFromFile = detect_supported_image_mime_type_from_file

__all__ = [
    "detectSupportedImageMimeType",
    "detectSupportedImageMimeTypeFromFile",
]
