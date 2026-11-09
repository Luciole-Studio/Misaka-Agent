"""EXIF orientation of JPEG and WebP files, read from the bytes without decoding the image.

Translated from pi's ``packages/durable/src/images/exif.ts`` (v1.3.0). (The product's ``misaka.utils.exif_orientation``
ports the coding agent's reader; durable keeps its own copy, as upstream does, so the two keep their own edge cases.)
"""

from __future__ import annotations


def exifOrientation(data: bytes) -> int:
    """The EXIF orientation of a JPEG or WebP, 1 to 8; 1 when absent or unreadable. (The parameter is pi's ``bytes``;
    Python needs that name for the type.)"""
    tiff = tiffStart(data)
    if tiff < 0 or tiff + 8 > len(data):
        return 1
    little = data[tiff] == 0x49 and data[tiff + 1] == 0x49

    def u16(at: int) -> int:
        return data[at] | (data[at + 1] << 8) if little else (data[at] << 8) | data[at + 1]

    def u32(at: int) -> int:
        if little:
            return data[at] | (data[at + 1] << 8) | (data[at + 2] << 16) | (data[at + 3] << 24)
        return (data[at] << 24) | (data[at + 1] << 16) | (data[at + 2] << 8) | data[at + 3]

    directory = tiff + u32(tiff + 4)
    if directory + 2 > len(data):
        return 1
    entries = u16(directory)
    for index in range(entries):
        entry = directory + 2 + index * 12
        if entry + 12 > len(data):
            return 1
        # Tag 0x0112 is Orientation, a SHORT whose value sits in the entry.
        if u16(entry) == 0x0112:
            value = u16(entry + 8)
            return value if 1 <= value <= 8 else 1
    return 1


def _byte(data: bytes, offset: int) -> int:
    """``bytes[offset]`` with JavaScript's ``undefined`` past the end, which compares unequal and shifts as 0."""
    return data[offset] if 0 <= offset < len(data) else -1


def tiffStart(data: bytes) -> int:
    """Where the TIFF header of a JPEG's APP1 or a WebP's EXIF chunk starts; -1 when there is none."""
    if _byte(data, 0) == 0xFF and _byte(data, 1) == 0xD8:
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                return -1
            marker = data[offset + 1]
            if marker == 0xFF:
                offset += 1
                continue
            if marker == 0xE1 and isExifHeader(data, offset + 4):
                return offset + 10
            offset += 2 + ((data[offset + 2] << 8) | data[offset + 3])
        return -1
    if ascii(data, 0, "RIFF") and ascii(data, 8, "WEBP"):
        offset = 12
        while offset + 8 <= len(data):
            size = data[offset + 4] | (data[offset + 5] << 8) | (data[offset + 6] << 16) | (data[offset + 7] << 24)
            # Some WebP files prefix the TIFF header with "Exif\0\0".
            if ascii(data, offset, "EXIF"):
                return offset + 14 if isExifHeader(data, offset + 8) else offset + 8
            # RIFF chunks are padded to an even size.
            offset += 8 + size + (size % 2)
    return -1


def isExifHeader(data: bytes, offset: int) -> bool:
    return ascii(data, offset, "Exif") and _byte(data, offset + 4) == 0 and _byte(data, offset + 5) == 0


def ascii(data: bytes, offset: int, text: str) -> bool:
    return all(_byte(data, offset + index) == ord(character) for index, character in enumerate(text))


__all__ = ["ascii", "exifOrientation", "isExifHeader", "tiffStart"]
