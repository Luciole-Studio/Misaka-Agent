"""Process ``@file`` CLI arguments into text content and image attachments."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from misaka.ai.types import ImageContent
from misaka.core.tools.path_utils import resolve_read_path
from misaka.utils.image_process import ProcessImageOptions, process_image
from misaka.utils.mime import detect_supported_image_mime_type_from_file


@dataclass(slots=True)
class ProcessedFiles:
    text: str
    images: list[ImageContent]


@dataclass(slots=True)
class ProcessFileOptions:
    autoResizeImages: bool | None = None


async def process_file_arguments(
    file_args: list[str],
    options: ProcessFileOptions | None = None,
) -> ProcessedFiles:
    return await _process_file_arguments(file_args, options=options)


async def _process_file_arguments(
    file_args: list[str],
    options: ProcessFileOptions | None = None,
    *,
    cwd: str | None = None,
) -> ProcessedFiles:
    auto_resize_images = True if options is None or options.autoResizeImages is None else options.autoResizeImages
    resolved_cwd = cwd or os.getcwd()
    text = ""
    images: list[ImageContent] = []

    for file_arg in file_args:
        absolute_path = os.path.abspath(resolve_read_path(file_arg, resolved_cwd))
        if not os.path.exists(absolute_path):
            sys.stderr.write(f"Error: File not found: {absolute_path}\n")
            raise SystemExit(1)

        stats = await asyncio.to_thread(os.stat, absolute_path)
        if stats.st_size == 0:
            continue

        mime_type = await detect_supported_image_mime_type_from_file(absolute_path)
        if mime_type:
            content = await asyncio.to_thread(_read_bytes, absolute_path)
            processed = await process_image(
                content,
                mime_type,
                ProcessImageOptions(autoResizeImages=auto_resize_images),
            )
            if not processed.ok:
                text += f'<file name="{absolute_path}">{processed.message}</file>\n'
                continue

            attachment = ImageContent(
                type="image", mimeType=processed.mimeType, data=processed.data
            )
            images.append(attachment)
            note = "\n".join(processed.hints)
            text += f'<file name="{absolute_path}">{note}</file>\n'
            continue

        try:
            file_text = await asyncio.to_thread(_read_text, absolute_path)
        except OSError as error:
            sys.stderr.write(f"Error: Could not read file {absolute_path}: {error}\n")
            raise SystemExit(1) from error

        text += f'<file name="{absolute_path}">\n{file_text}\n</file>\n'

    return ProcessedFiles(text=text, images=images)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _read_text(path: str) -> str:
    # errors="replace" is what every other reader in the repo does (core/tools/read.py:287,
    # workspace.py:27) and what pi cli/file-processor.ts:77 gets for free from Node's decoder.
    # Strict decoding would raise UnicodeDecodeError -- a ValueError, which the caller's
    # ``except OSError`` does not catch -- and end the process on a traceback instead of
    # putting the (mostly readable) file into the <file> block.
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


__all__ = ["ProcessFileOptions", "ProcessedFiles", "process_file_arguments"]
