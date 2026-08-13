"""Process ``@file`` CLI arguments into text content and image attachments."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from dataclasses import dataclass

from misaka.ai.types import ImageContent

from misaka.core.tools.path_utils import resolve_read_path
from misaka.utils.image_resize import format_dimension_note, resize_image
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
            base64_content = base64.b64encode(content).decode("ascii")

            if auto_resize_images:
                resized = await resize_image(ImageContent(type="image", data=base64_content, mimeType=mime_type))
                if resized is None:
                    text += (
                        f'<file name="{absolute_path}">'
                        "[Image omitted: could not be resized below the inline image size limit.]"
                        "</file>\n"
                    )
                    continue
                attachment = ImageContent(type="image", mimeType=resized.mimeType, data=resized.data)
                dimension_note = format_dimension_note(resized)
            else:
                attachment = ImageContent(type="image", mimeType=mime_type, data=base64_content)
                dimension_note = None

            images.append(attachment)
            note = dimension_note or ""
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
    with open(path, encoding="utf-8") as handle:
        return handle.read()


processFileArguments = process_file_arguments

__all__ = ["ProcessFileOptions", "ProcessedFiles", "processFileArguments"]
