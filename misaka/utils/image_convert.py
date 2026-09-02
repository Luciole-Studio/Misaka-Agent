"""Image conversion helpers for terminal display flows."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO

from misaka.utils.exif_orientation import apply_exif_orientation, load_image_bytes


@dataclass(slots=True)
class ConvertedImage:
    data: str
    mimeType: str


def _convert_image_bytes_to_png_sync(image_bytes: bytes) -> bytes | None:
    raw_image = None
    normalized = None
    try:
        raw_image = load_image_bytes(image_bytes)
        normalized = apply_exif_orientation(raw_image, image_bytes)
        with BytesIO() as output:
            normalized.save(output, format="PNG")
            return output.getvalue()
    except Exception:  # noqa: BLE001 - an undecodable image converts to nothing
        return None
    finally:
        if normalized is not None and normalized is not raw_image:
            normalized.close()
        if raw_image is not None:
            raw_image.close()


async def convert_image_bytes_to_png(image_bytes: bytes) -> bytes | None:
    return await asyncio.to_thread(_convert_image_bytes_to_png_sync, image_bytes)


async def convert_to_png(base64_data: str, mime_type: str) -> ConvertedImage | None:
    if mime_type == "image/png":
        return ConvertedImage(data=base64_data, mimeType=mime_type)

    try:
        raw_bytes = base64.b64decode(base64_data)
    except Exception:  # noqa: BLE001 - an undecodable image converts to nothing
        return None

    png_bytes = await convert_image_bytes_to_png(raw_bytes)
    if png_bytes is None:
        return None
    return ConvertedImage(
        data=base64.b64encode(png_bytes).decode("ascii"),
        mimeType="image/png",
    )


__all__ = []
