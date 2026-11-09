"""misaka's image processor does what upstream's Photon does where Pillow would not by default (V6-IMG-B01..B07).

Each expectation was measured against upstream's ``images/index.ts`` running on ``@silvia-odwyer/photon`` 0.3.3: 16-bit
grey samples round to 8 bits (``(v + 128) // 257``, all 65536 values checked) and a grey ``tRNS`` value is transparent;
JPEGs are written 4:4:4; re-encoded images carry no colour profile; the PNG encoder runs at its fastest level; Pillow's
pixel limit gives way to Photon's own (``tests/test_durable_verify_r3.py`` pins it). The worker-thread processor serves any event loop and
its worker exits by itself when idle, also after the loop that used it closed.
"""

from __future__ import annotations

import asyncio
import base64
import math
import struct
import threading
import time
import warnings
import zlib
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, ImageCms, JpegImagePlugin

import misaka.durable.images as images_module
import misaka.durable.images.node as node_module
from misaka.durable.images import createPhotonImages
from misaka.durable.images.node import createNodePhotonImages
from misaka.durable.search import rankDocuments

LIMITS = {"maxWidth": 2000, "maxHeight": 2000, "maxBytes": 4_718_592}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(width: int, height: int, depth: int, colorType: int, rows: bytes, *extra: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, depth, colorType, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + b"".join(extra)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def _grey16(values: list[int], height: int = 1, *extra: bytes) -> bytes:
    row = b"\0" + b"".join(struct.pack(">H", value) for value in values)
    return _png(len(values), height, 16, 0, row * height, *extra)


def _decoded(prepared: Any) -> Image.Image:
    assert prepared is not None
    return Image.open(BytesIO(base64.b64decode(prepared["data"])))


def _small(side: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (side, side), (1, 2, 3)).save(buffer, "PNG")
    return buffer.getvalue()


async def _prepare(data: bytes, mimeType: str, limits: dict[str, int] = LIMITS) -> Any:
    return await createPhotonImages().prepare(data, mimeType, limits)  # type: ignore[arg-type]


# V6-IMG-B01


async def test_16_bit_grey_rounds_to_8_bits_as_photon_does() -> None:
    values = [0, 1, 127, 128, 129, 384, 385, 32767, 32768, 65407, 65408, 65534, 65535]
    # BMP is not taken inline, so the image is re-encoded at its own size: the pixels are the decoder's.
    prepared = await _prepare(_grey16(values), "image/bmp")
    assert prepared["mimeType"] == "image/png"
    out = _decoded(prepared).convert("RGBA")
    assert [out.getpixel((x, 0)) for x in range(len(values))] == [
        ((value + 128) // 257,) * 3 + (255,) for value in values
    ]


async def test_a_resized_16_bit_grey_ramp_is_not_washed_out() -> None:
    width = 2400
    ramp = [x * 65535 // (width - 1) for x in range(width)]
    prepared = await _prepare(_grey16(ramp, 2), "image/png")
    assert prepared["resized"]["to"] == {"width": 2000, "height": 2}
    out = _decoded(prepared).convert("L")
    # Photon: [0, 64, 128, 255]; clipping gave [3, 255, 255, 255].
    assert [out.getpixel((x, 0)) for x in (0, 500, 1000, 1999)] == [0, 64, 128, 255]


async def test_a_16_bit_grey_transparent_value_is_transparent() -> None:
    values = [x * 1000 for x in range(64)]
    prepared = await _prepare(_grey16(values, 1, _chunk(b"tRNS", struct.pack(">H", 5000))), "image/bmp")
    out = _decoded(prepared).convert("RGBA")
    assert [out.getpixel((x, 0)) for x in (0, 4, 5, 6, 63)] == [
        (0, 0, 0, 255),
        (16, 16, 16, 255),
        (19, 19, 19, 0),
        (23, 23, 23, 255),
        (245, 245, 245, 255),
    ]


# V6-IMG-B02


async def test_a_re_encoded_jpeg_keeps_full_chroma_as_photon_writes_it() -> None:
    image = Image.new("RGB", (2400, 16))
    for x in range(2400):
        image.putpixel((x, 0), (255, 0, 0) if x % 2 else (0, 0, 255))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95, subsampling=0)
    prepared = await _prepare(buffer.getvalue(), "image/jpeg")
    assert prepared["mimeType"] == "image/jpeg"
    # 0 is 4:4:4; Pillow's default, 2, is 4:2:0.
    assert JpegImagePlugin.get_sampling(_decoded(prepared)) == 0


# V6-IMG-B04


async def test_prepares_an_image_above_pillows_decompression_bomb_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pillow refuses images over twice MAX_IMAGE_PIXELS and warns over it; Photon has no such limit.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    refused = _small(100)
    warned = _small(40)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # Over twice the limit Pillow refuses; between the limit and twice it warns, an error under ``-W error``.
        assert (await _prepare(refused, "image/bmp"))["mimeType"] == "image/png"
        assert (await _prepare(warned, "image/bmp"))["mimeType"] == "image/png"
    # The guard stays for every other Pillow user.
    with pytest.raises(Image.DecompressionBombError):
        Image.open(BytesIO(refused))


async def test_the_worker_thread_prepares_an_image_above_pillows_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    images = await createNodePhotonImages({"idleMs": 50})
    prepared = await images.prepare(_small(100), "image/bmp", LIMITS)  # type: ignore[arg-type]
    assert prepared is not None and prepared["mimeType"] == "image/png"


async def test_refuses_an_image_whose_rgba_pixels_cannot_fit_in_photons_memory() -> None:
    # 40000 x 30000 grey decodes to 1.2 GB, over image 0.24's 512 MiB: refused from its header, nothing is allocated.
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", 40000, 30000, 8, 0, 0, 0, 0))
    data = b"\x89PNG\r\n\x1a\n" + header + _chunk(b"IDAT", zlib.compress(b"\0" * 40001)) + _chunk(b"IEND", b"")
    started = time.perf_counter()
    assert await _prepare(data, "image/png") is None
    assert time.perf_counter() - started < 1


# V6-IMG-B05


async def test_png_is_compressed_at_zlibs_fastest_level_as_photons_encoder_runs() -> None:
    prepared = await _prepare(_small(64), "image/bmp")
    data = base64.b64decode(prepared["data"])
    start = data.index(b"IDAT") + 4
    # The zlib header's level bits: 0 is the fastest compressor, which Photon's (fdeflate) declares.
    assert data[start + 1] >> 6 == 0


# V6-IMG-B06


def _icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()  # type: ignore[no-any-return]


@pytest.mark.parametrize("format,mimeType", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
async def test_a_re_encoded_image_carries_no_colour_profile(format: str, mimeType: str) -> None:
    buffer = BytesIO()
    Image.new("RGB", (2400, 20), (200, 100, 50)).save(buffer, format, icc_profile=_icc())
    prepared = await _prepare(buffer.getvalue(), mimeType)
    assert prepared["resized"]["to"]["width"] == 2000
    assert "icc_profile" not in _decoded(prepared).info


# V6-IMG-B03


def _workers() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name == "misaka-durable-images"}


def test_a_processor_serves_another_event_loop_after_the_first_closed() -> None:
    images = asyncio.run(createNodePhotonImages({"idleMs": 2_000}))
    first = asyncio.run(images.prepare(_small(3000), "image/png", LIMITS))  # type: ignore[arg-type]

    async def again() -> Any:
        return await asyncio.wait_for(images.prepare(_small(3000), "image/png", LIMITS), 10)  # type: ignore[arg-type]

    second = asyncio.run(again())
    assert first is not None and second == first


def test_a_processor_serves_two_running_loops_at_once() -> None:
    images = asyncio.run(createNodePhotonImages({"idleMs": 2_000}))
    results: list[Any] = []

    def user() -> None:
        async def main() -> None:
            prepared = await asyncio.gather(*(images.prepare(_small(2500), "image/png", LIMITS) for _ in range(3)))  # type: ignore[arg-type]
            results.extend(prepared)

        asyncio.run(main())

    users = [threading.Thread(target=user) for _ in range(2)]
    for thread in users:
        thread.start()
    for thread in users:
        thread.join(30)
    assert len(results) == 6
    assert all(result["resized"]["to"] == {"width": 2000, "height": 2000} for result in results)


def test_the_worker_exits_when_idle_after_its_event_loop_closed() -> None:
    before = _workers()

    async def once() -> None:
        images = await createNodePhotonImages({"idleMs": 50})
        assert await images.prepare(_small(2500), "image/png", LIMITS) is not None  # type: ignore[arg-type]

    asyncio.run(once())
    started = _workers() - before
    assert len(started) == 1
    deadline = time.monotonic() + 5
    while any(thread.is_alive() for thread in started) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(thread.is_alive() for thread in started)


async def test_a_request_racing_the_idle_exit_starts_a_new_worker() -> None:
    images = await createNodePhotonImages({"idleMs": 1})
    for _ in range(20):
        assert await images.prepare(_small(10), "image/bmp", LIMITS) is not None  # type: ignore[arg-type]
        await asyncio.sleep(0.001)


# V6-IMG-B07


def test_rank_documents_takes_a_limit_as_slice_does() -> None:
    documents = [{"name": name, "text": "alpha"} for name in ("a", "b", "c")]
    assert rankDocuments("alpha", documents, math.nan) == []  # type: ignore[arg-type]
    assert rankDocuments("alpha", documents, math.inf) == ["a", "b", "c"]  # type: ignore[arg-type]
    assert rankDocuments("alpha", documents, 2.5) == ["a", "b"]  # type: ignore[arg-type]
    assert rankDocuments("alpha", documents, -math.inf) == []  # type: ignore[arg-type]


# V6-ME-B01 (images)


def test_the_images_modules_export_what_upstream_exports() -> None:
    assert images_module.__all__ == ["createPhotonImages"]
    assert node_module.__all__ == ["NodePhotonImagesOptions", "createNodePhotonImages"]
