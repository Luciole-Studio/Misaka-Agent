"""pi coding-agent's ``src/utils/image-convert.ts`` (v1.1.0): image conversion for terminal display.

MISAKA fork: Pillow stands in for photon (a hard dependency, so nothing here reports it unavailable), and the
asynchronous conversions run it off the event loop.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

from misaka.utils.exif_orientation import applyExifOrientation, load_image_bytes
from misaka.utils.js import bufferFromBase64, startTask

if TYPE_CHECKING:
    from misaka.ui.tui.components.image import ImageTranscoder


@dataclass(slots=True)
class ConvertedImage:
    data: str
    mimeType: str


def encodePng(image_bytes: bytes) -> bytes | None:
    raw_image = None
    normalized = None
    try:
        raw_image = load_image_bytes(image_bytes)
        normalized = applyExifOrientation(raw_image, image_bytes)
        with BytesIO() as output:
            normalized.save(output, format="PNG")
            return output.getvalue()
    except Exception:  # noqa: BLE001 - conversion failed
        return None
    finally:
        if normalized is not None and normalized is not raw_image:
            normalized.close()
        if raw_image is not None:
            raw_image.close()


async def convertImageBytesToPng(image_bytes: bytes) -> bytes | None:
    return await asyncio.to_thread(encodePng, image_bytes)


async def convertToPng(base64_data: str, mime_type: str) -> ConvertedImage | None:
    """Convert image to PNG format for terminal display.
    Kitty graphics protocol requires PNG format (f=100).
    """
    # Already PNG, no conversion needed
    if mime_type == "image/png":
        return ConvertedImage(data=base64_data, mimeType=mime_type)

    png_bytes = await convertImageBytesToPng(bufferFromBase64(base64_data))
    if not png_bytes:
        return None

    return ConvertedImage(
        data=base64.b64encode(png_bytes).decode("ascii"),
        mimeType="image/png",
    )


async def loadPngTranscoder() -> ImageTranscoder | None:
    """Return a synchronous PNG transcoder for pi-tui's Kitty image rendering."""

    def transcoder(base64_data: str, _mime_type: str) -> str | None:
        png_bytes = encodePng(bufferFromBase64(base64_data))
        return base64.b64encode(png_bytes).decode("ascii") if png_bytes else None

    return transcoder


_png_transcoder_load: asyncio.Future[bool] | None = None
_png_transcoder_registered = False


async def _load_and_register_png_transcoder() -> bool:
    global _png_transcoder_registered
    from misaka.ui.tui.components.image import setImageTranscoder

    transcoder = await loadPngTranscoder()
    if not transcoder:
        return False
    setImageTranscoder(transcoder)
    _png_transcoder_registered = True
    return True


def ensurePngTranscoder(on_registered: Callable[[], None]) -> None:
    """On Kitty-protocol terminals, register the PNG transcoder as pi-tui's image transcoder so non-PNG images
    render. Loads it once. `on_registered` runs after registration so callers can re-render images that showed text
    fallbacks; it is not called if the transcoder was already registered or cannot load.
    """
    global _png_transcoder_load
    from misaka.ui.tui.terminal_image import getCapabilities

    if _png_transcoder_registered or getCapabilities().images != "kitty":
        return
    if _png_transcoder_load is None:
        _png_transcoder_load = startTask(_load_and_register_png_transcoder())

    def settled(load: asyncio.Future[bool]) -> None:
        if not load.cancelled() and load.exception() is None and load.result():
            on_registered()

    _png_transcoder_load.add_done_callback(settled)


__all__ = [
    "ConvertedImage",
    "convertImageBytesToPng",
    "convertToPng",
    "ensurePngTranscoder",
    "loadPngTranscoder",
]
