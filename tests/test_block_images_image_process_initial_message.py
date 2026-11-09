"""Translated from pi's ``packages/coding-agent/test/block-images.test.ts``, ``image-process.test.ts`` and
``initial-message.test.ts`` (v1.1.0)."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest

from misaka.cli.args import Args
from misaka.cli.file_processor import processFileArguments
from misaka.cli.initial_message import buildInitialMessage
from misaka.core.settings_manager import SettingsManager
from misaka.core.tools.read import createReadTool
from misaka.utils.image_process import ProcessImageOptions, processImage
from misaka.utils.mime import detectSupportedImageMimeType
from misaka.utils.values import read_field

# 1x1 red PNG image as base64 (smallest valid PNG)
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


def createTinyBmp1x1Red24bpp() -> bytes:
    buffer = bytearray(58)
    buffer[0:2] = b"BM"
    struct.pack_into("<I", buffer, 2, len(buffer))
    struct.pack_into("<I", buffer, 10, 54)
    struct.pack_into("<I", buffer, 14, 40)
    struct.pack_into("<i", buffer, 18, 1)
    struct.pack_into("<i", buffer, 22, 1)
    struct.pack_into("<H", buffer, 26, 1)
    struct.pack_into("<H", buffer, 28, 24)
    struct.pack_into("<I", buffer, 30, 0)
    struct.pack_into("<I", buffer, 34, 4)
    buffer[56] = 0xFF
    return bytes(buffer)


# --- blockImages setting: SettingsManager -------------------------------------------------------


def test_should_default_block_images_to_false():
    assert SettingsManager.inMemory({}).getBlockImages() is False


def test_should_return_true_when_block_images_is_set_to_true():
    assert SettingsManager.inMemory({"images": {"blockImages": True}}).getBlockImages() is True


def test_should_persist_block_images_setting_via_set_block_images():
    manager = SettingsManager.inMemory({})
    assert manager.getBlockImages() is False

    manager.setBlockImages(True)
    assert manager.getBlockImages() is True

    manager.setBlockImages(False)
    assert manager.getBlockImages() is False


def test_should_handle_block_images_alongside_auto_resize():
    manager = SettingsManager.inMemory({"images": {"autoResize": True, "blockImages": True}})
    assert manager.getImageAutoResize() is True
    assert manager.getBlockImages() is True


# --- blockImages setting: Read tool -------------------------------------------------------------


async def test_should_always_read_images_filtering_happens_at_convert_to_llm_layer(tmp_path: Path):
    imagePath = tmp_path / "test.png"
    imagePath.write_bytes(base64.b64decode(TINY_PNG_BASE64))

    result = await createReadTool(str(tmp_path)).execute("test-1", {"path": str(imagePath)}, None, None)

    # Should have text note + image content
    assert len(result.content) >= 1
    assert any(read_field(content, "type") == "image" for content in result.content)


async def test_should_read_text_files_normally(tmp_path: Path):
    textPath = tmp_path / "test.txt"
    textPath.write_text("Hello, world!", encoding="utf-8")

    result = await createReadTool(str(tmp_path)).execute("test-2", {"path": str(textPath)}, None, None)

    assert len(result.content) == 1
    assert read_field(result.content[0], "type") == "text"
    assert "Hello, world!" in read_field(result.content[0], "text")


# --- blockImages setting: processFileArguments --------------------------------------------------


async def test_should_always_process_images(tmp_path: Path):
    imagePath = tmp_path / "test.png"
    imagePath.write_bytes(base64.b64decode(TINY_PNG_BASE64))

    result = await processFileArguments([str(imagePath)])

    assert len(result.images) == 1
    assert read_field(result.images[0], "type") == "image"


async def test_should_process_bmp_images_from_disk_as_png_attachments(tmp_path: Path):
    imagePath = tmp_path / "test.bmp"
    imagePath.write_bytes(createTinyBmp1x1Red24bpp())

    result = await processFileArguments([str(imagePath)])

    assert len(result.images) == 1
    assert read_field(result.images[0], "type") == "image"
    assert read_field(result.images[0], "mimeType") == "image/png"
    assert "[Image converted from image/bmp to image/png.]" in result.text


async def test_should_process_text_files_normally(tmp_path: Path):
    textPath = tmp_path / "test.txt"
    textPath.write_text("Hello, world!", encoding="utf-8")

    result = await processFileArguments([str(textPath)])

    assert result.images == []
    assert "Hello, world!" in result.text


# --- image processing pipeline ------------------------------------------------------------------


def expectPngMagic(base64Data: str) -> None:
    assert base64.b64decode(base64Data)[:4] == b"\x89PNG"


@pytest.mark.parametrize("signature", ["GIF87a", "GIF89a"])
def test_detects_the_complete_gif_signature(signature: str):
    assert detectSupportedImageMimeType(signature.encode("ascii")) == "image/gif"


def test_detects_bmp_files_from_magic_bytes():
    assert detectSupportedImageMimeType(createTinyBmp1x1Red24bpp()) == "image/bmp"


async def test_converts_bmp_files_to_png_attachments_when_auto_resize_is_disabled():
    result = await processImage(createTinyBmp1x1Red24bpp(), "image/bmp", ProcessImageOptions(autoResizeImages=False))

    assert result.ok is True
    assert result.mimeType == "image/png"
    assert "[Image converted from image/bmp to image/png.]" in result.hints
    expectPngMagic(result.data)


async def test_converts_bmp_files_before_auto_resizing():
    result = await processImage(createTinyBmp1x1Red24bpp(), "image/bmp")

    assert result.ok is True
    assert result.mimeType == "image/png"
    assert "[Image converted from image/bmp to image/png.]" in result.hints
    expectPngMagic(result.data)


# --- buildInitialMessage ------------------------------------------------------------------------


def createArgs(messages: list[str] | None = None) -> Args:
    return Args(messages=list(messages or []))


def test_merges_piped_stdin_with_the_first_cli_message_into_one_prompt():
    parsed = createArgs(["Summarize the text given"])
    result = buildInitialMessage(parsed=parsed, stdinContent="README contents\n")

    # MISAKA fork: the parts are blank-line separated (pi joins them with "": "README contents\nSummarize ...").
    assert read_field(result, "initialMessage") == "README contents\n\nSummarize the text given"
    assert parsed.messages == []


def test_uses_stdin_as_the_initial_prompt_when_no_cli_message_is_present():
    parsed = createArgs()
    result = buildInitialMessage(parsed=parsed, stdinContent="README contents")

    assert read_field(result, "initialMessage") == "README contents"
    assert parsed.messages == []


def test_combines_stdin_file_text_and_first_cli_message_in_one_prompt():
    parsed = createArgs(["Explain it", "Second message"])
    result = buildInitialMessage(parsed=parsed, stdinContent="stdin\n", fileText="file\n")

    # MISAKA fork: the parts are blank-line separated (pi: "stdin\nfile\nExplain it").
    assert read_field(result, "initialMessage") == "stdin\n\nfile\n\nExplain it"
    assert parsed.messages == ["Second message"]
