"""Translated from pi's ``packages/coding-agent/src/utils/image-resize.ts`` and ``image-resize-core.ts`` (v1.1.0).

MISAKA fork: Pillow stands in for Photon (``ImageOps.exif_transpose`` for ``applyExifOrientation``, LANCZOS for
Lanczos3), and a worker thread for pi's resize worker, so the event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from misaka.utils.js import jsRound, toFixed


@dataclass(slots=True)
class ImageResizeOptions:
    maxWidth: int | None = None  # Default: 2000
    maxHeight: int | None = None  # Default: 2000
    maxBytes: int | None = None  # Default: 4.5MB of base64 payload (below Anthropic's 5MB limit)
    jpegQuality: int | None = None  # Default: 80


@dataclass(slots=True)
class ResizedImage:
    data: str  # base64
    mimeType: str
    originalWidth: int
    originalHeight: int
    width: int
    height: int
    wasResized: bool


# 4.5MB of base64 payload. Provides headroom below Anthropic's 5MB limit.
DEFAULT_MAX_BYTES = int(4.5 * 1024 * 1024)

DEFAULT_OPTIONS = ImageResizeOptions(
    maxWidth=2000,
    maxHeight=2000,
    maxBytes=DEFAULT_MAX_BYTES,
    jpegQuality=80,
)


@dataclass(slots=True)
class EncodedCandidate:
    data: str
    encodedSize: int
    mimeType: str


def encodeCandidate(buffer: bytes, mimeType: str) -> EncodedCandidate:
    data = base64.b64encode(buffer).decode("ascii")
    return EncodedCandidate(data=data, encodedSize=len(data.encode("utf-8")), mimeType=mimeType)


def _pngBytes(image: Image.Image) -> bytes:
    """``image.get_bytes()``."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpegBytes(image: Image.Image, quality: int) -> bytes:
    """``image.get_bytes_jpeg(quality)``: JPEG has no alpha, so a transparent image is laid on white."""
    if image.mode not in {"RGB", "L"}:
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def resizeImageInProcess(
    inputBytes: bytes,
    mimeType: str,
    options: ImageResizeOptions | None = None,
) -> ResizedImage | None:
    """Resize an image to fit within the specified max dimensions and encoded file size.
    Returns null if the image cannot be resized below maxBytes.

    Strategy for staying under maxBytes:
    1. First resize to maxWidth/maxHeight
    2. Try both PNG and JPEG formats, pick the smaller one
    3. If still too large, try JPEG with decreasing quality
    4. If still too large, progressively reduce dimensions until 1x1
    """
    options = options or ImageResizeOptions()
    opts = ImageResizeOptions(
        **{
            name: getattr(options, name) if getattr(options, name) is not None else getattr(DEFAULT_OPTIONS, name)
            for name in ImageResizeOptions.__dataclass_fields__
        }
    )
    assert opts.maxWidth is not None and opts.maxHeight is not None and opts.maxBytes is not None
    inputBase64Size = math.ceil(len(inputBytes) / 3) * 4

    image: Image.Image | None = None
    try:
        with Image.open(BytesIO(inputBytes)) as rawImage:
            rawImage.load()
            oriented = ImageOps.exif_transpose(rawImage)
            image = oriented.copy()
            if oriented is not rawImage:
                oriented.close()

        originalWidth, originalHeight = image.size
        parts = mimeType.split("/")
        format = parts[1] if len(parts) > 1 else "png"

        # Check if already within all limits (dimensions AND encoded size)
        if originalWidth <= opts.maxWidth and originalHeight <= opts.maxHeight and inputBase64Size < opts.maxBytes:
            return ResizedImage(
                data=base64.b64encode(inputBytes).decode("ascii"),
                mimeType=mimeType or f"image/{format}",
                originalWidth=originalWidth,
                originalHeight=originalHeight,
                width=originalWidth,
                height=originalHeight,
                wasResized=False,
            )

        # Calculate initial dimensions respecting max limits
        targetWidth = originalWidth
        targetHeight = originalHeight

        if targetWidth > opts.maxWidth:
            targetHeight = jsRound((targetHeight * opts.maxWidth) / targetWidth)
            targetWidth = opts.maxWidth
        if targetHeight > opts.maxHeight:
            targetWidth = jsRound((targetWidth * opts.maxHeight) / targetHeight)
            targetHeight = opts.maxHeight

        def tryEncodings(width: int, height: int, jpegQualities: list[int]) -> list[EncodedCandidate]:
            assert image is not None
            resized = image.resize((width, height), Image.Resampling.LANCZOS)

            try:
                candidates = [encodeCandidate(_pngBytes(resized), "image/png")]
                for quality in jpegQualities:
                    candidates.append(encodeCandidate(_jpegBytes(resized, quality), "image/jpeg"))
                return candidates
            finally:
                resized.close()

        qualitySteps = list(dict.fromkeys([opts.jpegQuality, 85, 70, 55, 40]))
        currentWidth = targetWidth
        currentHeight = targetHeight

        while True:
            candidates = tryEncodings(currentWidth, currentHeight, qualitySteps)  # type: ignore[arg-type]
            for candidate in candidates:
                if candidate.encodedSize < opts.maxBytes:
                    return ResizedImage(
                        data=candidate.data,
                        mimeType=candidate.mimeType,
                        originalWidth=originalWidth,
                        originalHeight=originalHeight,
                        width=currentWidth,
                        height=currentHeight,
                        wasResized=True,
                    )

            if currentWidth == 1 and currentHeight == 1:
                break

            nextWidth = 1 if currentWidth == 1 else max(1, math.floor(currentWidth * 0.75))
            nextHeight = 1 if currentHeight == 1 else max(1, math.floor(currentHeight * 0.75))
            if nextWidth == currentWidth and nextHeight == currentHeight:
                break

            currentWidth = nextWidth
            currentHeight = nextHeight

        return None
    except Exception:  # noqa: BLE001 - pi's catch-all: an image that cannot be resized gives no image
        return None
    finally:
        if image is not None:
            image.close()


async def resizeImage(
    inputBytes: bytes,
    mimeType: str,
    options: ImageResizeOptions | None = None,
) -> ResizedImage | None:
    """Resize raw image bytes, encoding base64 only for the returned attachment."""
    return await asyncio.to_thread(resizeImageInProcess, inputBytes, mimeType, options)


def formatDimensionNote(result: ResizedImage) -> str | None:
    """Format a dimension note for resized images.
    This helps the model understand the coordinate mapping.
    """
    if not result.wasResized:
        return None

    scale = result.originalWidth / result.width
    return f"[Image: original {result.originalWidth}x{result.originalHeight}, displayed at {result.width}x{result.height}. Multiply coordinates by {toFixed(scale, 2)} to map to original image.]"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "ImageResizeOptions",
    "ResizedImage",
    "formatDimensionNote",
    "resizeImage",
    "resizeImageInProcess",
]
