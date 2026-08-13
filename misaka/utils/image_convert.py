"""Image conversion helpers for terminal display flows."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO

from misaka.utils.exif_orientation import apply_exif_orientation, load_image_bytes


@dataclass(slots=True)
class ConvertedImage:
    data: str
    mimeType: str


async def convert_to_png(base64_data: str, mime_type: str) -> ConvertedImage | None:
    if mime_type == "image/png":
        return ConvertedImage(data=base64_data, mimeType=mime_type)

    try:
        raw_bytes = base64.b64decode(base64_data)
        raw_image = load_image_bytes(raw_bytes)
    except Exception:
        return None

    normalized = apply_exif_orientation(raw_image, raw_bytes)
    try:
        output = BytesIO()
        normalized.save(output, format="PNG")
        return ConvertedImage(
            data=base64.b64encode(output.getvalue()).decode("ascii"),
            mimeType="image/png",
        )
    except Exception:
        return None
    finally:
        if normalized is not raw_image:
            normalized.close()
        raw_image.close()


convertToPng = convert_to_png

__all__ = [
    "convertToPng",
]
