"""EXIF orientation helpers for image conversion flows."""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def read_orientation_from_tiff(data: bytes, tiff_start: int) -> int:
    if tiff_start + 8 > len(data):
        return 1

    byte_order = (data[tiff_start] << 8) | data[tiff_start + 1]
    little_endian = byte_order == 0x4949

    def read16(position: int) -> int:
        if little_endian:
            return data[position] | (data[position + 1] << 8)
        return (data[position] << 8) | data[position + 1]

    def read32(position: int) -> int:
        if little_endian:
            return (
                data[position]
                | (data[position + 1] << 8)
                | (data[position + 2] << 16)
                | (data[position + 3] << 24)
            )
        return (
            (data[position] << 24)
            | (data[position + 1] << 16)
            | (data[position + 2] << 8)
            | data[position + 3]
        ) & 0xFFFFFFFF

    ifd_offset = read32(tiff_start + 4)
    ifd_start = tiff_start + ifd_offset
    if ifd_start + 2 > len(data):
        return 1

    entry_count = read16(ifd_start)
    for index in range(entry_count):
        entry_position = ifd_start + 2 + index * 12
        if entry_position + 12 > len(data):
            return 1
        if read16(entry_position) == 0x0112:
            value = read16(entry_position + 8)
            return value if 1 <= value <= 8 else 1
    return 1


def find_jpeg_tiff_offset(data: bytes) -> int:
    offset = 2
    while offset < len(data) - 1:
        if data[offset] != 0xFF:
            return -1
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue

        if marker == 0xE1:
            if offset + 4 >= len(data):
                return -1
            segment_start = offset + 4
            if segment_start + 6 > len(data):
                return -1
            if not has_exif_header(data, segment_start):
                return -1
            return segment_start + 6

        if offset + 4 > len(data):
            return -1
        length = (data[offset + 2] << 8) | data[offset + 3]
        offset += 2 + length
    return -1


def find_webp_tiff_offset(data: bytes) -> int:
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4].decode("ascii", errors="ignore")
        chunk_size = (
            data[offset + 4]
            | (data[offset + 5] << 8)
            | (data[offset + 6] << 16)
            | (data[offset + 7] << 24)
        )
        data_start = offset + 8

        if chunk_id == "EXIF":
            if data_start + chunk_size > len(data):
                return -1
            return data_start + 6 if chunk_size >= 6 and has_exif_header(data, data_start) else data_start

        offset = data_start + chunk_size + (chunk_size % 2)
    return -1


def has_exif_header(data: bytes, offset: int) -> bool:
    return data[offset : offset + 6] == b"Exif\x00\x00"


def get_exif_orientation(data: bytes) -> int:
    tiff_offset = -1
    if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
        tiff_offset = find_jpeg_tiff_offset(data)
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        tiff_offset = find_webp_tiff_offset(data)
    if tiff_offset == -1:
        return 1
    return read_orientation_from_tiff(data, tiff_offset)


def apply_exif_orientation(image: Image.Image, original_bytes: bytes) -> Image.Image:
    orientation = get_exif_orientation(original_bytes)
    if orientation == 1:
        return image

    transpose_map: dict[int, Image.Transpose] = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    operation = transpose_map.get(orientation)
    if operation is None:
        return image
    return image.transpose(operation)


def load_image_bytes(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


applyExifOrientation = apply_exif_orientation
findJpegTiffOffset = find_jpeg_tiff_offset
findWebpTiffOffset = find_webp_tiff_offset
getExifOrientation = get_exif_orientation
hasExifHeader = has_exif_header
readOrientationFromTiff = read_orientation_from_tiff

__all__ = [
    "applyExifOrientation",
]
