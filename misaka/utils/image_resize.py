"""Image resizing helpers for inline tool attachments."""

from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from misaka.ai.types import ImageContent

DEFAULT_MAX_BYTES = int(4.5 * 1024 * 1024)


@dataclass(slots=True)
class ImageResizeOptions:
    maxWidth: int | None = None
    maxHeight: int | None = None
    maxBytes: int | None = None
    jpegQuality: int | None = None


@dataclass(slots=True)
class ResizedImage:
    data: str
    mimeType: str
    originalWidth: int
    originalHeight: int
    width: int
    height: int
    wasResized: bool


@dataclass(slots=True)
class _ResolvedImageResizeOptions:
    maxWidth: int = 2000
    maxHeight: int = 2000
    maxBytes: int = DEFAULT_MAX_BYTES
    jpegQuality: int = 80


@dataclass(slots=True)
class _EncodedCandidate:
    data: str
    encodedSize: int
    mimeType: str


def _resolve_options(options: ImageResizeOptions | None) -> _ResolvedImageResizeOptions:
    resolved = options or ImageResizeOptions()
    return _ResolvedImageResizeOptions(
        maxWidth=resolved.maxWidth or 2000,
        maxHeight=resolved.maxHeight or 2000,
        maxBytes=resolved.maxBytes or DEFAULT_MAX_BYTES,
        jpegQuality=resolved.jpegQuality or 80,
    )


def _encode_candidate(image: Image.Image, mime_type: str, *, jpeg_quality: int | None = None) -> _EncodedCandidate:
    buffer = BytesIO()
    if mime_type == "image/png":
        image.save(buffer, format="PNG")
    elif mime_type == "image/jpeg":
        jpeg_ready = _to_jpeg_ready(image)
        jpeg_ready.save(buffer, format="JPEG", quality=jpeg_quality or 80)
    else:
        raise ValueError(f"Unsupported mime type: {mime_type}")
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return _EncodedCandidate(data=data, encodedSize=len(data.encode("utf-8")), mimeType=mime_type)


def _to_jpeg_ready(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "L"}:
        return image
    if "A" in image.getbands():
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def _resize_image_sync(img: ImageContent, options: ImageResizeOptions | None) -> ResizedImage | None:
    resolved = _resolve_options(options)
    try:
        input_buffer = base64.b64decode(img.data)
    except Exception:
        return None

    input_base64_size = len(img.data.encode("utf-8"))

    try:
        with Image.open(BytesIO(input_buffer)) as opened:
            opened.load()
            normalized = ImageOps.exif_transpose(opened)
            image = normalized.copy()
            if normalized is not opened:
                normalized.close()
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    try:
        original_width, original_height = image.size
        format_suffix = (img.mimeType.split("/", 1)[1] if img.mimeType else (image.format or "png")).lower()

        if (
            original_width <= resolved.maxWidth
            and original_height <= resolved.maxHeight
            and input_base64_size < resolved.maxBytes
        ):
            return ResizedImage(
                data=img.data,
                mimeType=img.mimeType or f"image/{format_suffix}",
                originalWidth=original_width,
                originalHeight=original_height,
                width=original_width,
                height=original_height,
                wasResized=False,
            )

        target_width = original_width
        target_height = original_height
        if target_width > resolved.maxWidth:
            target_height = round((target_height * resolved.maxWidth) / target_width)
            target_width = resolved.maxWidth
        if target_height > resolved.maxHeight:
            target_width = round((target_width * resolved.maxHeight) / target_height)
            target_height = resolved.maxHeight

        quality_steps = list(dict.fromkeys([resolved.jpegQuality, 85, 70, 55, 40]))
        current_width = max(1, target_width)
        current_height = max(1, target_height)

        while True:
            resized = image.resize((current_width, current_height), Image.Resampling.LANCZOS)
            try:
                candidates = [_encode_candidate(resized, "image/png")]
                candidates.extend(
                    _encode_candidate(resized, "image/jpeg", jpeg_quality=quality) for quality in quality_steps
                )
                for candidate in candidates:
                    if candidate.encodedSize < resolved.maxBytes:
                        return ResizedImage(
                            data=candidate.data,
                            mimeType=candidate.mimeType,
                            originalWidth=original_width,
                            originalHeight=original_height,
                            width=current_width,
                            height=current_height,
                            wasResized=True,
                        )
            finally:
                resized.close()

            if current_width == 1 and current_height == 1:
                break

            next_width = 1 if current_width == 1 else max(1, math.floor(current_width * 0.75))
            next_height = 1 if current_height == 1 else max(1, math.floor(current_height * 0.75))
            if next_width == current_width and next_height == current_height:
                break
            current_width = next_width
            current_height = next_height

        return None
    finally:
        image.close()


async def resize_image(img: ImageContent, options: ImageResizeOptions | None = None) -> ResizedImage | None:
    return await asyncio.to_thread(_resize_image_sync, img, options)


def format_dimension_note(result: ResizedImage) -> str | None:
    if not result.wasResized:
        return None
    scale = result.originalWidth / result.width
    return (
        f"[Image: original {result.originalWidth}x{result.originalHeight}, "
        f"displayed at {result.width}x{result.height}. "
        f"Multiply coordinates by {scale:.2f} to map to original image.]"
    )


formatDimensionNote = format_dimension_note
resizeImage = resize_image

__all__ = [
    "DEFAULT_MAX_BYTES",
    "ImageResizeOptions",
    "ResizedImage",
    "formatDimensionNote",
    "resizeImage",
]
