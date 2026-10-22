"""An image processor for ``read``: orient, convert, and shrink images so the model can take them.

Translated from pi's ``packages/durable/src/images/index.ts`` (v1.3.0).

MISAKA fork: upstream decodes and encodes with Photon (Rust compiled to WebAssembly, the ``@silvia-odwyer/photon``
package, which builds on the ``image`` 0.24 crate). misaka's own image tools already do this work with Pillow
(``misaka.utils.image_resize``), so durable uses Pillow too and adds no dependency. Where the two libraries differ,
Pillow is made to do what Photon does:

- Pixels: decoded images are converted to RGBA, as Photon holds them. 16-bit grey samples become 8-bit by rounding,
  ``(v + 128) // 257``, as the crate's conversion does (Pillow's own ``convert`` would clip them to 255); a grey ``tRNS``
  value becomes transparent. 16-bit colour PNGs are decoded by Pillow to 8 bits by dropping the low byte, so a channel
  can be one level below Photon's rounded value. Re-encoded images carry no colour profile, as Photon writes none.
- Resizing: Pillow's ``LANCZOS`` is Photon's ``Lanczos3``; ``Image.transpose`` turns an image upright.
- Size: Photon's bound is checked instead of Pillow's (``_photonDecodes``), from the image's header, before any pixel
  is decoded. Photon's ``new_from_byteslice`` is ``image::load_from_memory(…).to_rgba8().to_vec()`` on ``image``
  0.24.9, whose default ``Limits`` refuse an image whose decoded buffer, in the decoder's own colour type, needs more
  than 512 MiB (a 16-bit RGBA PNG holds 8 bytes a pixel, a palette PNG 3, or 4 with ``tRNS``; a JPEG 1 or 3; a GIF 4);
  and everything ``new_from_byteslice`` holds at once (the input, the decoded image, ``to_rgba8()`` and its copy)
  must fit Photon's 32-bit WebAssembly memory (4 GiB, ``isize::MAX`` for one allocation), which only binds for 8-bit grey
  images. That memory bound assumes a fresh WebAssembly heap: upstream's heap never shrinks, so after earlier images
  it may refuse a little sooner. Pillow's process-wide decompression-bomb guard (``Image.MAX_IMAGE_PIXELS``) is lifted
  while this module opens an image, on that thread only; every other Pillow user in the process keeps it.
- JPEG: baseline at the same quality with 4:4:4 chroma sampling, as the crate's encoder writes; sizes match Photon's to
  within a fraction of a percent.
- PNG: 8-bit RGBA, as Photon writes, with zlib's fastest level, as the crate's encoder uses its fastest compressor.
  That compressor (``fdeflate``) only encodes runs of zero bytes, so Photon's PNGs are larger than Pillow's, often
  several times; a PNG that just fits the byte limit here may not fit upstream, which then sends JPEG instead
  ("Converted from image/png…"). Encoded bytes differ from Photon's in any case.

The algorithm, its order of encodings and its 3/4 shrink steps are upstream's. ``createPhotonImages`` keeps upstream's
name and ignores its ``wasm`` argument. Nothing imports Pillow unless this module is imported.
"""

from __future__ import annotations

import threading
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from PIL import Image

from misaka.utils.js import jsRound

from ..tools.image_processor import (
    INLINE_IMAGE_TYPES,
    ImageLimits,
    ImageProcessor,
    PreparedImage,
    base64Length,
    toBase64,
)
from .exif import exifOrientation

JPEG_QUALITIES = [80, 70, 55, 40]
"""JPEG qualities tried in order."""


def createPhotonImages(wasm: Any = None) -> ImageProcessor:
    """An ``ImageProcessor`` for ``createCodingTools({"images": ...})``. An image already upright, in a format providers
    take inline, and within the limits is returned as it is, once it decodes. Otherwise it is turned upright by its EXIF
    orientation, scaled to the maximum width and height, and encoded as PNG or JPEG at falling qualities (JPEG first for
    JPEG sources), then at smaller dimensions, 3/4 at a time, until the base64 fits. Formats providers do not take inline
    (BMP) become PNG.

    The work runs on the calling thread, as upstream's does: ``misaka.durable.images.node`` prepares images in a worker
    thread instead. (``wasm`` is upstream's Photon module; see the module note.)"""

    async def prepareImage(data: bytes, mimeType: str, limits: ImageLimits) -> PreparedImage | None:
        return prepare(data, mimeType, limits)

    return SimpleNamespace(prepare=prepareImage)  # type: ignore[return-value]


def prepare(data: bytes, mimeType: str, limits: ImageLimits) -> PreparedImage | None:
    orientation = exifOrientation(data)
    inline = mimeType in INLINE_IMAGE_TYPES
    image: Image.Image | None = None
    try:
        # Decoded even when it is returned as it is: a damaged file is an error, not an image the provider rejects.
        image = _decode(data)
        if (
            inline
            and orientation == 1
            and image.width <= limits["maxWidth"]
            and image.height <= limits["maxHeight"]
            and base64Length(len(data)) <= limits["maxBytes"]
        ):
            return {"data": toBase64(data), "mimeType": mimeType}
        if orientation != 1:
            upright = transposed(image, orientation)
            image.close()
            image = upright
        width = image.width
        height = image.height
        scale = min(1, limits["maxWidth"] / width, limits["maxHeight"] / height)
        target = {"width": max(1, jsRound(width * scale)), "height": max(1, jsRound(height * scale))}
        while True:
            encoded = encodeWithin(image, target, limits["maxBytes"], mimeType == "image/jpeg")
            if encoded is not None:
                resized = (
                    {}
                    if target["width"] == width and target["height"] == height
                    else {"resized": {"from": {"width": width, "height": height}, "to": target}}
                )
                converted = {} if encoded["mimeType"] == mimeType else {"convertedFrom": mimeType}
                return {**encoded, **converted, **resized}  # type: ignore[typeddict-item]
            if target["width"] == 1 and target["height"] == 1:
                return None
            target = {"width": max(1, int(target["width"] * 0.75)), "height": max(1, int(target["height"] * 0.75))}
    except Exception:  # noqa: BLE001 - bytes Pillow cannot decode (Photon throws for them too)
        return None
    finally:
        if image is not None:
            image.close()


def _decode(data: bytes) -> Image.Image:
    """``PhotonImage.new_from_byteslice``: the first frame, as RGBA."""
    _photonDecoding.active = True
    try:
        with Image.open(BytesIO(data)) as opened:
            if not _photonDecodes(data, opened):
                raise Image.DecompressionBombError(f"Photon cannot decode a {opened.width}x{opened.height} image")
            opened.load()
            rgba = _rgba(opened)
    finally:
        _photonDecoding.active = False
    # Photon keeps pixels only: no colour profile or other metadata reaches the encoders.
    rgba.info = {}
    return rgba


_SIXTEEN_BITS = {"I;16", "I;16B", "I;16L", "I;16N", "I"}
"""Pillow's modes for 16-bit grey (``I`` too: older Pillow and some formats load 16-bit grey as 32-bit integers)."""

_TO_EIGHT_BITS = [(value + 128) // 257 for value in range(65536)]
"""16-bit samples to 8-bit, rounded, as the ``image`` crate converts them."""


def _rgba(image: Image.Image) -> Image.Image:
    """``image`` as 8-bit RGBA, as the ``image`` crate converts it (``DynamicImage::to_rgba8``)."""
    if image.mode not in _SIXTEEN_BITS:
        return image.convert("RGBA")
    wide = image if image.mode == "I" else image.convert("I")
    try:
        # A 65536-entry table maps ``I`` to ``L`` exactly; values outside 0..65535 clamp, as the crate's do.
        grey = wide.point(_TO_EIGHT_BITS, "L")
        transparent = image.info.get("transparency")
        if isinstance(transparent, int):
            alpha = wide.point([0 if value == transparent else 255 for value in range(65536)], "L")
        else:
            alpha = Image.new("L", image.size, 255)
        return Image.merge("RGBA", (grey, grey, grey, alpha))
    finally:
        if wide is not image:
            wide.close()


_IMAGE_MAX_ALLOC = 512 * 1024 * 1024
"""``image`` 0.24.9's default ``Limits::max_alloc``, which ``load_from_memory`` reserves the decoded buffer from."""

_WASM_MEMORY = 65536 * 65536
"""4 GiB: V8's limit for a 32-bit WebAssembly memory; Photon's module declares no smaller maximum."""

_WASM_STATIC = 22 * 65536
"""Photon's initial memory (data, stack), below its heap."""

_ISIZE_MAX = 2**31 - 1
"""The largest allocation Rust makes on ``wasm32``."""


def _photonDecodes(data: bytes, image: Image.Image) -> bool:
    """Whether Photon's ``new_from_byteslice`` decodes ``data`` (opened as ``image``, header only) within its limits."""
    width, height, perPixel, extra = _decodedLayout(data, image)
    pixels = width * height
    total = pixels * perPixel
    # `Limits::reserve(decoder.total_bytes())`, then what the decoder reserves itself from what is left.
    if total > _IMAGE_MAX_ALLOC or extra > _IMAGE_MAX_ALLOC - total:
        return False
    rgba = 4 * pixels
    if max(total, rgba) > _ISIZE_MAX:
        return False
    # Live at once at the end of `new_from_byteslice`: the input, the decoded image, `to_rgba8()` and its `to_vec()`.
    return _WASM_STATIC + len(data) + total + 2 * rgba <= _WASM_MEMORY


def _decodedLayout(data: bytes, image: Image.Image) -> tuple[int, int, int, int]:
    """``(width, height, bytes per pixel, extra)`` of the buffer ``image`` 0.24.9 decodes ``data`` into: the decoder's
    colour type and dimensions, and what else its decoder reserves from the same limit (a GIF's first frame when it
    does not span the screen's width)."""
    width, height = image.size
    format = image.format
    if format == "PNG" and len(data) >= 29:
        # `png::Transformations::EXPAND`: bit depths below 8 become 8, a palette becomes RGB, `tRNS` adds alpha.
        depth, colorType = data[24], data[25]
        sample = 2 if depth == 16 else 1
        channels = {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}.get(colorType, 4)
        if colorType in (0, 2, 3) and _pngHasTransparency(data):
            channels += 1
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), channels * sample, 0
    if format == "JPEG":
        # `jpeg-decoder`: one component is L8 (L16 above 8 bits), three are RGB; CMYK is converted to RGB.
        return width, height, {"L": 1, "I;16": 2}.get(image.mode, 3), 0
    if format == "GIF" and len(data) >= 10:
        # RGBA over the logical screen; a first frame that does not span its width is read into a buffer of its own.
        screenWidth, screenHeight = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        frame = _gifFirstFrame(data)
        extra = 0
        if frame is not None:
            left, top, frameWidth, frameHeight = frame
            if not (left == 0 and frameWidth == screenWidth and top + frameHeight <= screenHeight):
                extra = 4 * frameWidth * frameHeight
        return screenWidth, screenHeight, 4, extra
    if format == "WEBP":
        # Lossy without alpha is RGB; lossless, or lossy with an alpha chunk, is RGBA.
        return width, height, 4 if _webpHasAlpha(data) else 3, 0
    if format == "BMP":
        # A palette is expanded to RGB; RGBA only with an alpha bit mask (Pillow opens those as RGBA too).
        return width, height, 4 if image.mode == "RGBA" else 3, 0
    # Formats the read tool never sends (`detectSupportedImageMimeType`): Pillow's own channels stand in.
    bands = len(image.getbands())
    return width, height, bands * (2 if image.mode.startswith("I;16") else 1), 0


def _pngHasTransparency(data: bytes) -> bool:
    """Whether a ``tRNS`` chunk comes before the first ``IDAT``, where the decoder reads the header up to."""
    offset = 8
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        if kind == b"tRNS":
            return True
        if kind in (b"IDAT", b"IEND"):
            return False
        offset += 12 + length
    return False


def _gifFirstFrame(data: bytes) -> tuple[int, int, int, int] | None:
    """``(left, top, width, height)`` of a GIF's first image descriptor."""
    if len(data) < 13:
        return None
    offset = 13 + (3 << ((data[10] & 7) + 1) if data[10] & 0x80 else 0)
    while offset < len(data):
        block = data[offset]
        if block == 0x2C:
            if offset + 9 > len(data):
                return None
            fields = [int.from_bytes(data[offset + 1 + 2 * index : offset + 3 + 2 * index], "little") for index in range(4)]
            return fields[0], fields[1], fields[2], fields[3]
        if block != 0x21:
            return None
        offset += 2
        while offset < len(data) and data[offset] != 0:
            offset += 1 + data[offset]
        offset += 1
    return None


def _webpHasAlpha(data: bytes) -> bool:
    """Whether ``image`` decodes a WebP as RGBA: a ``VP8L`` or ``ALPH`` chunk, at the top or in the first frame."""
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset : offset + 4]
        length = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if kind in (b"VP8L", b"ALPH"):
            return True
        if kind == b"VP8 ":
            return False
        if kind == b"ANMF":
            # The first frame's own chunks follow its 16-byte header.
            return _webpHasAlpha(b"RIFF\0\0\0\0WEBP" + data[offset + 24 : offset + 8 + length])
        offset += 8 + length + (length & 1)
    return False


_photonDecoding = threading.local()
# Pillow's own check, also when this module is loaded again over the wrapper it installed.
_pillowBombCheck = getattr(Image._decompression_bomb_check, "pillowCheck", Image._decompression_bomb_check)


def _bombCheck(size: tuple[int, int]) -> None:
    """Pillow's decompression-bomb check, except while this module opens an image on the calling thread: then
    ``_photonDecodes`` decides, from the header (see the module note)."""
    if not getattr(_photonDecoding, "active", False):
        _pillowBombCheck(size)


_bombCheck.pillowCheck = _pillowBombCheck  # type: ignore[attr-defined]
# Pillow looks the check up as a module global at each use, from ``Image.open`` and from the plugins.
Image._decompression_bomb_check = _bombCheck


def encodeWithin(image: Image.Image, size: dict[str, int], maxBytes: int, jpegFirst: bool) -> dict[str, str] | None:
    """``image`` at ``size`` in the first encoding whose base64 fits ``maxBytes``: PNG, then JPEG at falling qualities,
    or the JPEG qualities first when ``jpegFirst``; ``None`` if none fits."""
    same = size["width"] == image.width and size["height"] == image.height
    scaled = image if same else image.resize((size["width"], size["height"]), Image.Resampling.LANCZOS)
    try:
        png = ("image/png", lambda: _pngBytes(scaled))
        jpegs = [("image/jpeg", lambda quality=quality: _jpegBytes(scaled, quality)) for quality in JPEG_QUALITIES]
        for candidateMimeType, encode in [*jpegs, png] if jpegFirst else [png, *jpegs]:
            encoded = encode()
            if base64Length(len(encoded)) <= maxBytes:
                return {"data": toBase64(encoded), "mimeType": candidateMimeType}
        return None
    finally:
        if not same:
            scaled.close()


def _pngBytes(image: Image.Image) -> bytes:
    """``PhotonImage.get_bytes()``: 8-bit RGBA PNG, compressed fast (see the module note)."""
    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _jpegBytes(image: Image.Image, quality: int) -> bytes:
    """``PhotonImage.get_bytes_jpeg(quality)``: JPEG has no alpha, so it is dropped; 4:4:4, as Photon writes."""
    buffer = BytesIO()
    with image.convert("RGB") as rgb:
        rgb.save(buffer, format="JPEG", quality=quality, subsampling=0)
    return buffer.getvalue()


_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def transposed(image: Image.Image, orientation: int) -> Image.Image:
    """``image`` turned upright for EXIF ``orientation`` 2 to 8. MISAKA fork: upstream flips orientations 2 to 4 with
    Photon's ``fliph``/``flipv`` and moves the pixels of 5 to 8 itself, as Photon's ``rotate`` drops pixels at right
    angles; Pillow's ``transpose`` moves every pixel exactly, to the same places."""
    return image.transpose(_UPRIGHT[orientation])


__all__ = ["createPhotonImages"]
