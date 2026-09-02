"""Normalize image blocks returned by tools before they enter model history."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import ImageContent, TextContent
from misaka.utils.image_process import ProcessImageOptions, process_image
from misaka.utils.values import read_field


@dataclass(slots=True)
class NormalizeToolResultImagesOptions:
    autoResizeImages: bool | None = None


async def normalize_tool_result_images(
    content: list[Any],
    options: NormalizeToolResultImagesOptions | None = None,
) -> list[Any]:
    """Normalize tool-result images while preserving unchanged content identity."""
    auto_resize_images = (
        True
        if options is None or options.autoResizeImages is None
        else options.autoResizeImages
    )
    normalized: list[Any] = []
    changed = False

    for block in content:
        if read_field(block, "type") != "image":
            normalized.append(block)
            continue

        data = read_field(block, "data")
        mime_type = read_field(block, "mimeType")
        if not isinstance(data, str) or not isinstance(mime_type, str):
            raise TypeError("Image tool-result blocks require string data and mimeType")

        try:
            image_bytes = base64.b64decode(data)
        except (ValueError, binascii.Error):
            normalized.append(block)
            continue

        processed = await process_image(
            image_bytes,
            mime_type,
            ProcessImageOptions(autoResizeImages=auto_resize_images),
        )
        if not processed.ok:
            normalized.append(block)
            continue

        if (
            processed.data == data
            and processed.mimeType == mime_type
            and not processed.hints
        ):
            normalized.append(block)
            continue

        changed = True
        normalized.append(
            ImageContent(data=processed.data, mimeType=processed.mimeType)
        )
        if processed.hints:
            normalized.append(TextContent(text="\n".join(processed.hints)))

    return normalized if changed else content


__all__ = ["NormalizeToolResultImagesOptions", "normalize_tool_result_images"]
