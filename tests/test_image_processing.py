"""Translated from pi coding-agent's ``test/image-processing.test.ts`` (v1.1.0).

Tests for image processing utilities (pi: Photon; MISAKA: Pillow).
"""

import base64
import struct

from misaka.utils.image_convert import convertToPng, loadPngTranscoder
from misaka.utils.image_resize import ImageResizeOptions as Options
from misaka.utils.image_resize import (
    ResizedImage,
    formatDimensionNote,
    resizeImage,
)

# Small 2x2 red PNG image (base64) - generated with ImageMagick
TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACAQMAAABIeJ9nAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAAGUExURf8AAP///0EdNBEAAAABYktHRAH/Ai3eAAAAB3RJTUUH6gEOADM5Ddoh/wAAAAxJREFUCNdjYGBgAAAABAABJzQnCgAAACV0RVh0ZGF0ZTpjcmVhdGUAMjAyNi0wMS0xNFQwMDo1MTo1NyswMDowMOnKzHgAAAAldEVYdGRhdGU6bW9kaWZ5ADIwMjYtMDEtMTRUMDA6NTE6NTcrMDA6MDCYl3TEAAAAKHRFWHRkYXRlOnRpbWVzdGFtcAAyMDI2LTAxLTE0VDAwOjUxOjU3KzAwOjAwz4JVGwAAAABJRU5ErkJggg=="
)
# Small 2x2 blue JPEG image (base64) - generated with ImageMagick
TINY_JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAAAAAAAAAAAAAGCf/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AD3VTB3/2Q=="
)
TINY_JPEG_2X1 = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD/wAARCAABAAIDAREAAhEBAxEB/9sAQwADAgIDAgIDAwMDBAMDBAUIBQUEBAUKBwcGCAwKDAwLCgsLDQ4SEA0OEQ4LCxAWEBETFBUVFQwPFxgWFBgSFBUU/9sAQwEDBAQFBAUJBQUJFA0LDRQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD4H8Q/8h/Uv+vmX/0M1/o1wJ/ySWU/9g1D/wBNRMOM/wDkp8z/AOv9b/05I//Z"
)
# 100x100 gray PNG
MEDIUM_PNG_100x100 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAAAAABVicqIAAAAAmJLR0QA/4ePzL8AAAAHdElNRQfqAQ4AMzkN2iH/AAAAP0lEQVRo3u3NQQEAAAQEMASXXYrz2gqst/Lm4ZBIJBKJRCKRSCQSiUQikUgkEolEIpFIJBKJRCKRSCQSiSTsAP1cAUZeKtreAAAAJXRFWHRkYXRlOmNyZWF0ZQAyMDI2LTAxLTE0VDAwOjUxOjU3KzAwOjAw6crMeAAAACV0RVh0ZGF0ZTptb2RpZnkAMjAyNi0wMS0xNFQwMDo1MTo1NyswMDowMJiXdMQAAAAodEVYdGRhdGU6dGltZXN0YW1wADIwMjYtMDEtMTRUMDA6NTE6NTcrMDA6MDDPglUbAAAAAElFTkSuQmCC"
)
# 200x200 colored PNG
LARGE_PNG_200x200 = (
    "iVBORw0KGgoAAAANSUhEUgAAAMgAAADIEAIAAAByquWKAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAAGYktHRP///////wlY99wAAAAHdElNRQfqAQ4AMzqU03BFAAAEEElEQVR42u3dvWqUURSF4XXMRAyIhSAWQvwnWtjaWusFx9bW1kKD/wELESxEiBjDjJdwysOC57mCXW32+zGcGbtdACpsdi+T8Xz1GABzm91xMl6sHgNgblzcT/Y+rh4DYG6cv0/2H64eA2BOEgI1fHQHaoyzbXIwVo8BMCcJgRqSEKgxft9Lrn5aPQbA3Ph1klw7Wj0GwJwkBGpYWECN8fMiub63egyAORcWUMPCAmqMH3eTG59XjwEwN76/S24+Wj0GwJwkBGpYWECN8e1fcmuzegyAORcWUMPCAmqM09vJ4dfVYwDMjS9vkzuPV48BMCcJgRqeSAZqjA/nyYP91WMAzElCoIYkBGqMk8Pk6HT1GABzm+3L5JIkBApIQqCGj+5AjfHmb/Lk8uoxAOYkIVBDEgI1LCygxmZ7nFyShEABFxZQw8ICaozXf5KnV1aPATDnwgJqWFhADQsLqOFnDUANFxZQw8ICaoxXZ8mzg9VjAMy5sIAaFhZQw3tYQA1/QgHUkIRADUkI1JCEQA1JCNSQhEANFxZQwzcsoIYkBGpIQqCGJARqSEKghiQEalhYQA1/QgHUcGEBNSwsoIYkBGq4sIAaFhZQw8ICaviGBdRwYQE1LCyghiQEariwgBoWFlDDe1hADS+OAjUkIVBDEgI1JCFQQxICNSQhUMOFBdTwDQuoIQmBGpIQqCEJgRqSEKghCYEaFhZQw4ujQA0XFlDDwgJqSEKghgsLqGFhATUsLKCGb1hADRcWUMPCAmpIQqCGCwuoYWEBNbyHBdTw4ihQQxICNSQhUEMSAjUkIVBDEgI1XFhADd+wgBqSEKghCYEakhCoIQmBGpIQqGFhATW8OArUcGEBNSwsoIYkBGq4sIAaFhZQw8ICaviGBdRwYQE1LCyghiQEariwgBoWFlDDe1hADS+OAjUkIVBDEgI1JCFQQxICNSQhUMOFBdTwDQuoIQmBGpIQqCEJgRqSEKghCYEa49lB8ups9RgAc14cBWpIQqCGhQXUkIRADRcWUMPCAmqMp1eS139WjwEw5xsWUEMSAjUsLKCGJARquLCAGhYWUGM8uZy8+bt6DIA5L44CNSQhUMMTyUCNcXSanByuHgNgThICNSQhUGM82E8+nK8eA2DOzxqAGpIQqDHuPE6+vF09BsDcOPyanN5ePQbAnCQEavgdFlBj3Nok3/6tHgNgzoujQA1JCNQYNx8l39+tHgNgbtz4nPy4u3oMgDlJCNSwsIAa4/pe8vNi9RgAc37WANSQhECNce0o+XWyegyAuXH1U/L73uoxAOYkIVDDwgJqjIORnG1XjwEw508ogBqSEKgx9h8m5+9XjwEwN/Y+Jhf3V48BMCcJgRpjPE+2x6vHAJgbSbLbrR4DYO4/GqiSgXN+ksgAAAAldEVYdGRhdGU6Y3JlYXRlADIwMjYtMDEtMTRUMDA6NTE6NTcrMDA6MDDpysx4AAAAJXRFWHRkYXRlOm1vZGlmeQAyMDI2LTAxLTE0VDAwOjUxOjU3KzAwOjAwmJd0xAAAACh0RVh0ZGF0ZTp0aW1lc3RhbXAAMjAyNi0wMS0xNFQwMDo1MTo1NyswMDowMM+CVRsAAAAASUVORK5CYII="
)


def imageBytes(base64Data: str) -> bytes:
    return base64.b64decode(base64Data)


def app1Segment(payload: bytes) -> bytes:
    return bytes([0xFF, 0xE1]) + struct.pack(">H", len(payload) + 2) + payload


def jpegWithXmpBeforeOrientation() -> str:
    jpeg = base64.b64decode(TINY_JPEG_2X1)
    xmp = app1Segment(b'http://ns.adobe.com/xap/1.0/\0<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
    orientation6 = app1Segment(b"Exif\0\0" + bytes.fromhex("49492a0008000000010012010300010000000600000000000000"))
    return base64.b64encode(jpeg[:2] + xmp + orientation6 + jpeg[2:]).decode()


# --- convertToPng ---------------------------------------------------------------------------------


async def test_should_return_original_data_for_png_input():
    result = await convertToPng(TINY_PNG, "image/png")
    assert result is not None
    assert result.data == TINY_PNG
    assert result.mimeType == "image/png"


async def test_should_convert_jpeg_to_png():
    result = await convertToPng(TINY_JPEG, "image/jpeg")
    assert result is not None
    assert result.mimeType == "image/png"
    # Result should be valid base64
    buffer = base64.b64decode(result.data)
    # PNG magic bytes
    assert buffer[0] == 0x89
    assert buffer[1] == 0x50  # 'P'
    assert buffer[2] == 0x4E  # 'N'
    assert buffer[3] == 0x47  # 'G'


async def test_should_apply_exif_orientation_after_an_xmp_app1_segment():
    result = await convertToPng(jpegWithXmpBeforeOrientation(), "image/jpeg")

    assert result is not None
    png = base64.b64decode(result.data)
    assert struct.unpack(">I", png[16:20])[0] == 1
    assert struct.unpack(">I", png[20:24])[0] == 2


# Issue #10292: pi-tui uses this transcoder to show non-PNG images on Kitty-protocol terminals.
async def test_loadpngtranscoder_converts_synchronously_to_oriented_png_data():
    transcoder = await loadPngTranscoder()
    assert transcoder is not None
    converted = transcoder(jpegWithXmpBeforeOrientation(), "image/jpeg")
    assert converted is not None
    png = base64.b64decode(converted)
    assert [struct.unpack(">I", png[16:20])[0], struct.unpack(">I", png[20:24])[0]] == [1, 2]
    assert transcoder(base64.b64encode(b"not an image").decode(), "image/jpeg") is None


# --- resizeImage ----------------------------------------------------------------------------------


async def test_should_keep_caller_input_bytes_intact():
    input_ = bytearray(imageBytes(TINY_PNG))
    originalByteLength = len(input_)
    originalFirstByte = input_[0]

    result = await resizeImage(bytes(input_), "image/png", Options(maxWidth=100, maxHeight=100, maxBytes=1024 * 1024))

    assert result is not None
    assert len(input_) == originalByteLength
    assert input_[0] == originalFirstByte


async def test_should_return_original_image_if_within_limits():
    result = await resizeImage(
        imageBytes(TINY_PNG), "image/png", Options(maxWidth=100, maxHeight=100, maxBytes=1024 * 1024)
    )

    assert result is not None
    assert result.wasResized is False
    assert result.data == TINY_PNG
    assert result.originalWidth == 2
    assert result.originalHeight == 2
    assert result.width == 2
    assert result.height == 2


async def test_should_resize_image_exceeding_dimension_limits():
    result = await resizeImage(
        imageBytes(MEDIUM_PNG_100x100), "image/png", Options(maxWidth=50, maxHeight=50, maxBytes=1024 * 1024)
    )

    assert result is not None
    assert result.wasResized is True
    assert result.originalWidth == 100
    assert result.originalHeight == 100
    assert result.width <= 50
    assert result.height <= 50


async def test_should_resize_image_exceeding_byte_limit():
    originalBuffer = base64.b64decode(LARGE_PNG_200x200)
    originalSize = len(originalBuffer)

    # Set maxBytes to less than the original encoded image size
    result = await resizeImage(
        imageBytes(LARGE_PNG_200x200),
        "image/png",
        Options(maxWidth=2000, maxHeight=2000, maxBytes=int(len(LARGE_PNG_200x200) * 0.9)),
    )

    # Should have tried to reduce size
    assert result is not None
    resultBuffer = base64.b64decode(result.data)
    assert len(resultBuffer) < originalSize
    assert len(result.data) < len(LARGE_PNG_200x200)


async def test_should_return_null_when_image_cannot_be_resized_below_maxbytes():
    result = await resizeImage(
        imageBytes(LARGE_PNG_200x200), "image/png", Options(maxWidth=2000, maxHeight=2000, maxBytes=1)
    )

    assert result is None


async def test_should_handle_jpeg_input():
    result = await resizeImage(
        imageBytes(TINY_JPEG), "image/jpeg", Options(maxWidth=100, maxHeight=100, maxBytes=1024 * 1024)
    )

    assert result is not None
    assert result.wasResized is False
    assert result.originalWidth == 2
    assert result.originalHeight == 2


# --- formatDimensionNote --------------------------------------------------------------------------


def test_should_return_undefined_for_non_resized_images():
    note = formatDimensionNote(
        ResizedImage(
            data="",
            mimeType="image/png",
            originalWidth=100,
            originalHeight=100,
            width=100,
            height=100,
            wasResized=False,
        )
    )
    assert note is None


def test_should_return_formatted_note_for_resized_images():
    note = formatDimensionNote(
        ResizedImage(
            data="",
            mimeType="image/png",
            originalWidth=2000,
            originalHeight=1000,
            width=1000,
            height=500,
            wasResized=True,
        )
    )
    assert note is not None
    assert "original 2000x1000" in note
    assert "displayed at 1000x500" in note
    assert "2.00" in note  # scale factor
