"""Translated from pi's ``packages/coding-agent/src/cli/file-processor.ts`` (v1.1.0).

Process @file CLI arguments into text content and image attachments
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from misaka.ai.types import ImageContent
from misaka.core.tools.path_utils import resolveReadPath
from misaka.utils.chalk import chalk
from misaka.utils.image_process import ProcessImageOptions, processImage
from misaka.utils.mime import detectSupportedImageMimeTypeFromFile
from misaka.utils.node_errors import withSyscall
from misaka.utils.text import stripBom


@dataclass(slots=True)
class ProcessedFiles:
    text: str
    images: list[ImageContent]


@dataclass(slots=True)
class ProcessFileOptions:
    # Whether to auto-resize images. Default: true
    autoResizeImages: bool | None = None


def _readFile(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


async def processFileArguments(fileArgs: list[str], options: ProcessFileOptions | None = None) -> ProcessedFiles:
    """Process @file arguments into text content and image attachments"""
    autoResizeImages = options.autoResizeImages if options is not None and options.autoResizeImages is not None else True
    text = ""
    images: list[ImageContent] = []

    for fileArg in fileArgs:
        # Expand and resolve path (handles ~ expansion and macOS screenshot Unicode spaces)
        absolutePath = os.path.abspath(resolveReadPath(fileArg, os.getcwd()))

        # Check if file exists
        if not os.path.exists(absolutePath):
            print(chalk.red(f"Error: File not found: {absolutePath}"), file=sys.stderr)
            raise SystemExit(1)

        # Check if file is empty
        stats = await asyncio.to_thread(os.stat, absolutePath)
        if stats.st_size == 0:
            # Skip empty files
            continue

        mimeType = await detectSupportedImageMimeTypeFromFile(absolutePath)

        if mimeType:
            # Handle image file
            content = await asyncio.to_thread(_readFile, absolutePath)
            processed = await processImage(content, mimeType, ProcessImageOptions(autoResizeImages=autoResizeImages))

            if not processed.ok:
                text += f'<file name="{absolutePath}">{processed.message}</file>\n'
                continue

            attachment = ImageContent(type="image", mimeType=processed.mimeType, data=processed.data)
            images.append(attachment)

            # Add text reference to image with optional processing hints
            if len(processed.hints) > 0:
                text += f'<file name="{absolutePath}">{chr(10).join(processed.hints)}</file>\n'
            else:
                text += f'<file name="{absolutePath}"></file>\n'
        else:
            # Handle text file
            try:
                raw = await asyncio.to_thread(withSyscall, "open", _readFile, absolutePath, path=absolutePath)
                # readFile(..., "utf-8"): bytes that are not UTF-8 become U+FFFD.
                content = stripBom(raw.decode("utf-8", errors="replace"))
                text += f'<file name="{absolutePath}">\n{content}\n</file>\n'
            except OSError as error:
                message = getattr(error, "uvMessage", None) or str(error)
                print(chalk.red(f"Error: Could not read file {absolutePath}: {message}"), file=sys.stderr)
                raise SystemExit(1) from error

    return ProcessedFiles(text=text, images=images)


__all__ = ["ProcessFileOptions", "ProcessedFiles", "processFileArguments"]
