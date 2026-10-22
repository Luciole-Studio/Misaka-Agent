"""pi durable's ``test/tools-images.test.ts``.

Adaptations: misaka's image processor uses Pillow instead of Photon (see ``misaka.durable.images``), so the fixtures are
built with Pillow too, with the same sizes, formats, pixels and EXIF orientations: ``encode`` writes Pillow's RGBA PNG,
or its JPEG at quality 95 as upstream's ``get_bytes_jpeg(95)``, and ``decodedImage`` decodes with Pillow. No case
asserts encoded bytes, which differ from Photon's. ``noisyPng`` fills the image with seeded ``random`` bytes instead of
upstream's LCG, which pure Python would take seconds to run over millions of pixels; it is noise all the same, which
neither PNG nor JPEG compresses. One ``createNodePhotonImages()`` processor serves the file, as upstream's ``beforeAll``
creates one: each test runs on its own event loop, and the processor answers each request on the loop that made it.
"encodes a 3 MiB image within a 128 MB heap" runs in this process and bounds ``toBase64``'s allocations with
``tracemalloc`` instead of a Node child process with ``--max-heap-size=128``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import shutil
import struct
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from misaka.chord.context import BACKGROUND_CONTEXT as context
from misaka.durable.env import getOrThrow
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.images.exif import exifOrientation
from misaka.durable.images.node import createNodePhotonImages
from misaka.durable.tools import createCodingTools
from misaka.durable.tools.image import imageDimensions
from misaka.durable.tools.image_processor import DEFAULT_IMAGE_LIMITS, toBase64
from misaka.durable.tools.read import createReadTool
from tests.durable_support import toMatchObject

directory = ""


@pytest.fixture(autouse=True, scope="module")
def imagesDirectory() -> Iterator[None]:
    global directory
    directory = tempfile.mkdtemp(prefix="pi-durable-images-")
    yield
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="module")
def processor() -> Any:
    # Fixtures are built with Pillow here; the processor prepares them in its worker thread, for every test's loop.
    return asyncio.run(createNodePhotonImages())


Rgb = tuple[int, int, int]


def blocks(colors: list[list[Rgb]], block: int) -> SimpleNamespace:
    """A ``width`` by ``height`` image of ``block``-pixel squares coloured row by row from ``colors``."""
    width = len(colors[0]) * block
    height = len(colors) * block
    image = Image.new("RGBA", (width, height))
    for by, row in enumerate(colors):
        for bx, color in enumerate(row):
            image.paste((*color, 255), (bx * block, by * block, (bx + 1) * block, (by + 1) * block))
    return SimpleNamespace(image=image, width=width, height=height)


def pixels(width: int, height: int) -> Image.Image:
    """RGBA pixels of a ``width`` by ``height`` image: red, with a green top-left pixel."""
    image = Image.new("RGBA", (width, height), (255, 0, 0, 255))
    image.putpixel((0, 0), (0, 255, 0, 255))
    return image


def encodeImage(image: Image.Image, format: str) -> bytes:
    """Photon's ``get_bytes()`` (RGBA PNG) or ``get_bytes_jpeg(95)``."""
    buffer = BytesIO()
    if format == "png":
        image.save(buffer, format="PNG")
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def encode(width: int, height: int, format: str) -> bytes:
    return encodeImage(pixels(width, height), format)


def noisyPng(width: int, height: int) -> bytes:
    """Noise, which neither PNG nor JPEG compresses well, so the byte limit decides."""
    noise = random.Random(1).randbytes(width * height * 3)
    return encodeImage(Image.frombytes("RGB", (width, height), noise).convert("RGBA"), "png")


def bmp(width: int, height: int) -> bytes:
    """A 24-bit BMP, which providers do not take inline."""
    row = -(-(width * 3) // 4) * 4
    data = bytearray(54 + row * height)
    data[0:2] = b"BM"
    struct.pack_into("<I", data, 2, len(data))
    struct.pack_into("<I", data, 10, 54)
    struct.pack_into("<I", data, 14, 40)
    struct.pack_into("<i", data, 18, width)
    struct.pack_into("<i", data, 22, height)
    struct.pack_into("<H", data, 26, 1)
    struct.pack_into("<H", data, 28, 24)
    struct.pack_into("<I", data, 34, row * height)
    for y in range(height):
        for x in range(width):
            data[54 + y * row + x * 3 : 54 + y * row + x * 3 + 3] = bytes([0, 0, 255])
    return bytes(data)


GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def gif(width: int = 1, height: int = 1) -> bytes:
    """A 1x1 GIF with its logical screen size set to ``width`` by ``height``."""
    data = bytearray(base64.b64decode(GIF))
    struct.pack_into("<H", data, 6, width)
    struct.pack_into("<H", data, 8, height)
    return bytes(data)


def tiff(orientation: int, littleEndian: bool) -> list[int]:
    """A TIFF header whose first directory holds only Orientation = ``orientation``."""
    order = "<" if littleEndian else ">"
    data = bytearray(26)
    data[0:2] = b"II" if littleEndian else b"MM"
    struct.pack_into(f"{order}H", data, 2, 42)
    struct.pack_into(f"{order}I", data, 4, 8)
    struct.pack_into(f"{order}H", data, 8, 1)
    struct.pack_into(f"{order}H", data, 10, 0x0112)
    struct.pack_into(f"{order}H", data, 12, 3)
    struct.pack_into(f"{order}I", data, 14, 1)
    struct.pack_into(f"{order}H", data, 18, orientation)
    return list(data)


EXIF_HEADER = [0x45, 0x78, 0x69, 0x66, 0, 0]


def withOrientation(jpeg: bytes, orientation: int, little: bool = False, app0: bool = False) -> bytes:
    """``jpeg`` with an APP1 segment whose EXIF says ``orientation``, after an APP0 segment when ``app0``."""
    exif = [*EXIF_HEADER, *tiff(orientation, little)]
    app1 = [0xFF, 0xE1, (len(exif) + 2) >> 8, (len(exif) + 2) & 0xFF, *exif]
    app0Segment = [0xFF, 0xE0, 0, 16, 0x4A, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0] if app0 else []
    return bytes([*jpeg[:2], *app0Segment, *app1, *jpeg[2:]])


def webpWithOrientation(orientation: int, prefixed: bool) -> bytes:
    """A RIFF WebP with a dummy ``VP8 `` chunk and an ``EXIF`` chunk saying ``orientation``, its TIFF data bare or
    prefixed."""

    def chunk(type: str, data: list[int]) -> list[int]:
        size = len(data)
        padding = [0] if size % 2 == 1 else []
        return [*type.encode("ascii"), size & 0xFF, (size >> 8) & 0xFF, 0, 0, *data, *padding]

    exif = [*(EXIF_HEADER if prefixed else []), *tiff(orientation, True)]
    body = [*b"WEBP", *chunk("VP8 ", [1, 2, 3]), *chunk("EXIF", exif)]
    return bytes([*b"RIFF", len(body) & 0xFF, (len(body) >> 8) & 0xFF, 0, 0, *body])


def decodedImage(data: str) -> dict[str, Any]:
    with Image.open(BytesIO(base64.b64decode(data))) as image:
        rgba = image.convert("RGBA")
        return {"width": rgba.width, "height": rgba.height, "rgba": rgba.tobytes()}


def readEnv(onRead: Callable[[int, int], None] | None = None) -> SimpleNamespace:
    """The environment of a ``read`` call, with ``onRead`` seeing each positional read of an opened file, and the
    largest read so far in ``reads.largest``."""
    env = NodeExecutionEnv({"cwd": directory})
    reads = SimpleNamespace(largest=0)
    open_ = env.openBinaryReader

    async def openBinaryReader(path: str, options: Any, ctx: Any) -> Any:
        reader = getOrThrow(await open_(path, options, ctx))

        async def read(offset: int, length: int, c: Any) -> Any:
            reads.largest = max(reads.largest, length)
            result = await reader.read(offset, length, c)
            if onRead is not None:
                onRead(offset, length)
            return result

        return {
            "ok": True,
            "value": SimpleNamespace(
                info=lambda c: reader.info(c),
                scanLines=lambda o, c: reader.scanLines(o, c),
                close=lambda c: reader.close(c),
                read=read,
            ),
        }

    env.openBinaryReader = openBinaryReader  # type: ignore[method-assign]
    return SimpleNamespace(env=env, reads=reads)


async def read(
    name: str,
    data: bytes,
    images: Any = None,
    input: list[str] | None = None,
    resize: dict[str, int] | None = None,
    env: Any = None,
    tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``read`` on ``data`` written to ``name``, for a conversation whose model takes ``input``."""
    Path(directory, name).write_bytes(data)
    limits = (
        None
        if resize is None
        else SimpleNamespace(
            images=SimpleNamespace(
                resize=SimpleNamespace(
                    maxWidth=resize.get("maxWidth"), maxHeight=resize.get("maxHeight"), maxBytes=resize.get("maxBytes")
                )
            )
        )
    )
    model = SimpleNamespace(input=input if input is not None else ["text", "image"], inputLimits=limits)

    async def details(*_: Any) -> None:
        return None

    async def agent(*_: Any) -> dict[str, Any]:
        return {"model": {"provider": "test", "modelId": "test"}}

    api = SimpleNamespace(
        env=env if env is not None else NodeExecutionEnv({"cwd": directory}),
        output=lambda *_: None,
        diagnostic=lambda *_: None,
        details=details,
        agent=agent,
        models=SimpleNamespace(getModel=lambda *_: model),
    )
    readTool = tool if tool is not None else createReadTool({} if images is None else {"images": images})
    return await readTool["execute"]({"path": name}, api, context)  # type: ignore[no-any-return]


def messages(result: dict[str, Any]) -> list[str]:
    return [diagnostic["message"] for diagnostic in result.get("diagnostics") or []]


def onlyImage(result: dict[str, Any]) -> dict[str, Any]:
    assert len(result["output"]) == 1
    return result["output"][0]  # type: ignore[no-any-return]


class TestReadOfImagesWithoutAnImageProcessor:
    async def test_returns_a_supported_image_within_the_limits_as_it_is(self) -> None:
        png = encode(8, 6, "png")
        result = await read("a.png", png)
        assert result["output"] == [{"type": "image", "data": toBase64(png), "mimeType": "image/png"}]
        assert messages(result) == ["Read image file [image/png]."]
        tiny = await read("a.gif", gif())
        assert onlyImage(tiny) == {"type": "image", "data": toBase64(gif()), "mimeType": "image/gif"}

    async def test_refuses_an_image_too_large_to_send_without_reading_it(self) -> None:
        opened = readEnv()
        result = await read("big.png", noisyPng(1100, 1100), env=opened.env)
        toMatchObject(result, {"output": [], "isError": True})
        assert messages(result)[0].startswith("big.png is an image (image/png) of ")
        assert ", too large to send" in messages(result)[0]
        # Only the header and the chunk walk, in blocks of at most 64 KiB.
        assert opened.reads.largest <= 64 * 1024

    async def test_refuses_an_image_wider_or_taller_than_the_limits_by_its_header(self) -> None:
        wide = await read("wide.png", encode(2001, 2, "png"))
        toMatchObject(wide, {"output": [], "isError": True})
        assert messages(wide) == [
            "wide.png is an image (image/png) of 2001x2, larger than 2000x2000, and no image processor is configured to shrink it"
        ]
        tall = await read("tall.gif", gif(1, 3000))
        assert messages(tall)[0].startswith("tall.gif is an image (image/gif) of 1x3000, larger than 2000x2000")

    async def test_refuses_a_format_that_needs_converting(self) -> None:
        result = await read("a.bmp", bmp(4, 4))
        toMatchObject(result, {"output": [], "isError": True})
        assert messages(result) == ["a.bmp is an image (image/bmp) that needs converting, and no image processor is configured"]

    async def test_says_when_the_model_sees_a_placeholder_instead_of_the_image(self) -> None:
        result = await read("a.png", encode(4, 4, "png"), input=["text"])
        assert len(result["output"]) == 1
        assert messages(result) == [
            "Read image file [image/png]. The current model does not support images; it sees a placeholder instead."
        ]

    async def test_applies_the_models_image_limits_instead_of_the_defaults(self, processor: Any) -> None:
        png = encode(8, 6, "png")
        refused = await read("a.png", png, resize={"maxWidth": 4})
        assert "of 8x6, larger than 4x2000," in messages(refused)[0]
        resized = await read("a.png", png, resize={"maxWidth": 4}, images=processor)
        toMatchObject(decodedImage(onlyImage(resized)["data"]), {"width": 4, "height": 3})

    async def test_lists_bmp_in_the_description_only_with_a_processor(self, processor: Any) -> None:
        assert "(jpg, png, gif, webp)" in createReadTool()["description"]
        assert "(jpg, png, gif, webp, bmp)" in createReadTool({"images": processor})["description"]


class TestReadOfAnImageThatChangesWhileItIsRead:
    async def test_reads_it_again_rather_than_return_a_cut_off_image_even_when_it_grew(self) -> None:
        png = encode(8, 6, "png")
        wholeReads: list[int] = []

        def onRead(offset: int, length: int) -> None:
            # A writer appends during the first read of the whole file; the second read sees it all.
            if offset != 0 or length < len(png):
                return
            wholeReads.append(length)
            if len(wholeReads) == 1:
                with open(os.path.join(directory, "growing.png"), "ab") as file:
                    file.write(bytes(16))

        opened = readEnv(onRead)
        result = await read("growing.png", png, env=opened.env)
        assert wholeReads == [len(png), len(png) + 16]
        assert onlyImage(result)["data"] == toBase64(png + bytes(16))

    async def test_fails_when_it_changes_during_the_second_read_too(self) -> None:
        png = encode(8, 6, "png")

        def onRead(offset: int, length: int) -> None:
            if offset == 0 and length >= len(png):
                with open(os.path.join(directory, "busy.png"), "ab") as file:
                    file.write(bytes(1))

        opened = readEnv(onRead)
        with pytest.raises(Exception, match="busy.png changed while it was read"):
            await read("busy.png", png, env=opened.env)


class TestReadOfImagesWithThePhotonImageProcessor:
    async def test_passes_an_image_that_needs_no_change_through_untouched(self, processor: Any) -> None:
        png = encode(8, 6, "png")
        result = await read("a.png", png, images=processor)
        assert result["output"] == [{"type": "image", "data": toBase64(png), "mimeType": "image/png"}]
        assert onlyImage(await read("a.gif", gif(), images=processor))["mimeType"] == "image/gif"

    async def test_scales_an_image_down_to_the_maximum_size_and_says_how_to_map_coordinates_back(self, processor: Any) -> None:
        result = await read("wide.png", encode(4000, 1000, "png"), images=processor)
        block = onlyImage(result)
        toMatchObject(decodedImage(block["data"]), {"width": 2000, "height": 500})
        assert messages(result) == [
            f"Read image file [{block['mimeType']}]. Resized from 4000x1000 to 2000x500. Multiply coordinates by 2.00 to map them to the original."
        ]

    async def test_re_encodes_a_png_too_large_for_the_byte_limit_as_jpeg_at_its_size_and_shrinks_it_when_that_is_not_enough(
        self, processor: Any
    ) -> None:
        result = await read("big.png", noisyPng(1100, 1100), images=processor)
        jpeg = onlyImage(result)
        assert jpeg["mimeType"] == "image/jpeg"
        assert messages(result) == ["Read image file [image/jpeg]. Converted from image/png to image/jpeg."]
        assert len(jpeg["data"]) <= DEFAULT_IMAGE_LIMITS["maxBytes"]
        toMatchObject(decodedImage(jpeg["data"]), {"width": 1100, "height": 1100})
        tight = {**DEFAULT_IMAGE_LIMITS, "maxBytes": 200_000}
        shrunk = await processor.prepare(noisyPng(2000, 2000), "image/png", tight)
        assert shrunk is not None
        assert len(shrunk["data"]) <= tight["maxBytes"]
        assert shrunk["resized"]["from"] == {"width": 2000, "height": 2000}
        assert shrunk["resized"]["to"]["width"] < 2000

    async def test_keeps_at_least_one_pixel_on_each_side_of_an_extreme_aspect_ratio(self, processor: Any) -> None:
        result = await processor.prepare(encode(6000, 2, "png"), "image/png", DEFAULT_IMAGE_LIMITS)
        assert result is not None
        assert result.get("resized") == {"from": {"width": 6000, "height": 2}, "to": {"width": 2000, "height": 1}}

    async def test_gives_up_when_not_even_one_pixel_fits_the_byte_limit(self, processor: Any) -> None:
        limits = {**DEFAULT_IMAGE_LIMITS, "maxBytes": 8}
        assert await processor.prepare(encode(4, 4, "png"), "image/png", limits) is None

    async def test_keeps_a_jpeg_that_must_be_re_encoded_a_jpeg(self, processor: Any) -> None:
        result = await processor.prepare(withOrientation(encode(40, 30, "jpeg"), 3), "image/jpeg", DEFAULT_IMAGE_LIMITS)
        assert result is not None and result["mimeType"] == "image/jpeg"

    async def test_converts_a_format_providers_do_not_take_inline_to_png(self, processor: Any) -> None:
        result = await read("a.bmp", bmp(4, 4), images=processor)
        toMatchObject(result["output"], [{"type": "image", "mimeType": "image/png"}])
        assert messages(result) == ["Read image file [image/png]. Converted from image/bmp to image/png."]

    async def test_reports_bytes_it_cannot_decode_as_an_error(self, processor: Any) -> None:
        broken = encode(4, 4, "png")[:40]
        result = await read("broken.png", broken, images=processor)
        toMatchObject(result, {"output": [], "isError": True})
        assert messages(result) == ["broken.png is an image (image/png) that cannot be prepared for the model"]

    async def test_is_what_create_coding_tools_images_gives_read(self, processor: Any) -> None:
        tool = next(candidate for candidate in createCodingTools({"images": processor})["tools"] if candidate["name"] == "read")
        result = await read("b.bmp", bmp(4, 4), tool=tool)
        toMatchObject(result["output"], [{"type": "image", "mimeType": "image/png"}])


R: Rgb = (255, 0, 0)
G: Rgb = (0, 255, 0)
B: Rgb = (0, 0, 255)
W: Rgb = (255, 255, 255)
K: Rgb = (0, 0, 0)
Y: Rgb = (255, 255, 0)
STORED = [[R, G, B], [W, K, Y]]


def mirror(grid: list[list[Rgb]]) -> list[list[Rgb]]:
    return [list(reversed(row)) for row in grid]


def turn(grid: list[list[Rgb]]) -> list[list[Rgb]]:
    """A quarter turn clockwise: the left column, read bottom to top, becomes the top row."""
    return [list(reversed([row[x] for row in grid])) for x in range(len(grid[0]))]


# What each orientation shows, from its EXIF definition: mirror, then turn clockwise.
UPRIGHT = {
    1: [list(row) for row in STORED],
    2: mirror(STORED),
    3: turn(turn(STORED)),
    4: turn(turn(mirror(STORED))),
    5: turn(turn(turn(mirror(STORED)))),
    6: turn(STORED),
    7: turn(mirror(STORED)),
    8: turn(turn(turn(STORED))),
}


class TestExifOrientation:
    @pytest.mark.parametrize("orientation", range(1, 9))
    async def test_turns_orientation_upright(self, orientation: int, processor: Any) -> None:
        stored = blocks(STORED, 16)
        jpeg = withOrientation(encodeImage(stored.image, "jpeg"), orientation)
        prepared = await processor.prepare(jpeg, "image/jpeg", DEFAULT_IMAGE_LIMITS)
        image = decodedImage(prepared["data"])
        expected = UPRIGHT[orientation]
        assert {"width": image["width"], "height": image["height"]} == {
            "width": len(expected[0]) * 16,
            "height": len(expected) * 16,
        }
        # The colour at the centre of each block, rounded to full channels against JPEG noise.
        seen = []
        for by, row in enumerate(expected):
            line = []
            for bx in range(len(row)):
                at = ((by * 16 + 8) * image["width"] + bx * 16 + 8) * 4
                line.append(tuple(255 if channel > 127 else 0 for channel in image["rgba"][at : at + 3]))
            seen.append(line)
        assert seen == expected

    def test_reads_big_and_little_endian_jpeg_exif_after_an_app0_segment_and_1_when_absent_or_out_of_range(self) -> None:
        jpeg = encode(2, 2, "jpeg")
        assert exifOrientation(withOrientation(jpeg, 6)) == 6
        assert exifOrientation(withOrientation(jpeg, 8, little=True)) == 8
        assert exifOrientation(withOrientation(jpeg, 5, app0=True)) == 5
        assert exifOrientation(withOrientation(jpeg, 9)) == 1
        assert exifOrientation(jpeg) == 1
        assert exifOrientation(encode(2, 2, "png")) == 1

    def test_reads_webp_exif_chunks_with_and_without_the_exif_prefix(self) -> None:
        assert exifOrientation(webpWithOrientation(6, False)) == 6
        assert exifOrientation(webpWithOrientation(3, True)) == 3


class TestImageDimensions:
    def test_reads_the_size_each_format_declares_in_its_header(self) -> None:
        assert imageDimensions(encode(7, 5, "png"), "image/png") == {"width": 7, "height": 5}
        assert imageDimensions(encode(7, 5, "jpeg"), "image/jpeg") == {"width": 7, "height": 5}
        assert imageDimensions(withOrientation(encode(7, 5, "jpeg"), 6, app0=True), "image/jpeg") == {"width": 7, "height": 5}
        assert imageDimensions(gif(300, 200), "image/gif") == {"width": 300, "height": 200}

        def webp(type: str, data: list[int]) -> bytes:
            return bytes([*b"RIFF", 0, 0, 0, 0, *b"WEBP", *type.encode("ascii"), 0, 0, 0, 0, *data, *([0] * 16)])

        # VP8: frame tag, start code, then 14-bit width and height.
        assert imageDimensions(webp("VP8 ", [0, 0, 0, 0x9D, 0x01, 0x2A, 0x2C, 0x01, 0xC8, 0x00]), "image/webp") == {
            "width": 300,
            "height": 200,
        }
        # VP8L: signature, then width - 1 and height - 1 in 14 bits each.
        bits = 299 | (199 << 14)
        assert imageDimensions(
            webp("VP8L", [0x2F, bits & 0xFF, (bits >> 8) & 0xFF, (bits >> 16) & 0xFF, bits >> 24]), "image/webp"
        ) == {"width": 300, "height": 200}
        # VP8X: flags, then width - 1 and height - 1 in 24 bits each.
        assert imageDimensions(webp("VP8X", [0, 0, 0, 0, 0x2B, 0x01, 0, 0xC7, 0, 0]), "image/webp") == {
            "width": 300,
            "height": 200,
        }
        assert imageDimensions(bytes([0xFF, 0xD8, 0xFF]), "image/jpeg") is None


class TestToBase64:
    def test_matches_buffer_for_every_remainder_length(self) -> None:
        for length in [0, 1, 2, 3, 4, 5, 8191, 8192, 8193, 10_000, 16_385]:
            data = bytes((index * 37) % 256 for index in range(length))
            assert toBase64(data) == base64.b64encode(data).decode("ascii")

    # #10923: building the output a character at a time took 138 MB of heap for a 3 MiB image.
    def test_encodes_a_3_mib_image_within_a_128_mb_heap(self) -> None:
        data = bytes((index * 37) % 256 for index in range(3147295))
        tracemalloc.start()
        try:
            encoded = toBase64(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert encoded == base64.b64encode(data).decode("ascii")
        assert peak < 128 * 1024 * 1024


class TestCreateNodePhotonImages:
    async def test_prepares_in_a_worker_thread_leaving_the_event_loop_free(self, processor: Any) -> None:
        photo = noisyPng(3000, 2000)

        async def prepareOn(images: Any) -> float:
            # The longest gap between timer ticks while the image is prepared: the time the event loop was blocked.
            state = SimpleNamespace(longest=0.0, last=time.perf_counter() * 1000)

            async def ticks() -> None:
                while True:
                    await asyncio.sleep(0.005)
                    now = time.perf_counter() * 1000
                    state.longest = max(state.longest, now - state.last)
                    state.last = now

            ticking = asyncio.ensure_future(ticks())
            try:
                prepared = images.prepare(photo, "image/png", DEFAULT_IMAGE_LIMITS)
                assert await prepared is not None
                # Counted until the result arrived, including the gap the last tick did not see.
                state.longest = max(state.longest, time.perf_counter() * 1000 - state.last)
            finally:
                ticking.cancel()
            return state.longest

        inline = await prepareOn(await createNodePhotonImages({"worker": False}))
        threaded = await prepareOn(processor)
        assert inline > 200
        assert threaded < 100

    async def test_gives_the_same_result_as_the_calling_thread_and_keeps_the_callers_bytes(self, processor: Any) -> None:
        png = noisyPng(1100, 1100)
        copy = bytes(png)
        threaded = await processor.prepare(png, "image/png", DEFAULT_IMAGE_LIMITS)
        inline = await (await createNodePhotonImages({"worker": False})).prepare(png, "image/png", DEFAULT_IMAGE_LIMITS)
        assert threaded == inline
        assert png == copy

    async def test_prepares_concurrent_images_and_starts_a_new_worker_after_the_idle_one_exited(self) -> None:
        images = await createNodePhotonImages({"idleMs": 10})
        results = await asyncio.gather(
            images.prepare(bmp(4, 4), "image/bmp", DEFAULT_IMAGE_LIMITS),
            images.prepare(encode(4000, 1000, "png"), "image/png", DEFAULT_IMAGE_LIMITS),
            images.prepare(bytes([1, 2, 3]), "image/png", DEFAULT_IMAGE_LIMITS),
        )
        assert results[0] is not None and results[0]["mimeType"] == "image/png"
        assert results[1] is not None and results[1]["resized"]["to"] == {"width": 2000, "height": 500}
        assert results[2] is None
        await asyncio.sleep(0.1)
        again = await images.prepare(bmp(2, 2), "image/bmp", DEFAULT_IMAGE_LIMITS)
        assert again is not None and again["mimeType"] == "image/png"
