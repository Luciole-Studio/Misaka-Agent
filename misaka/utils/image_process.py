"""Normalize and resize images before they enter inline model content."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Literal

from misaka.utils.image_convert import convert_image_bytes_to_png
from misaka.utils.image_resize import (
    ImageResizeOptions,
    format_dimension_note,
    resize_image_bytes,
)


@dataclass(slots=True)
class ProcessImageOptions:
    autoResizeImages: bool | None = None
    resizeOptions: ImageResizeOptions | None = None


@dataclass(slots=True)
class ProcessImageSuccess:
    data: str
    mimeType: str
    hints: list[str]
    ok: Literal[True] = field(init=False, default=True)


@dataclass(slots=True)
class ProcessImageFailure:
    message: str
    ok: Literal[False] = field(init=False, default=False)


type ProcessImageResult = ProcessImageSuccess | ProcessImageFailure


def _base_mime_type(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def _normalize_supported_image_mime_type(mime_type: str) -> str | None:
    match _base_mime_type(mime_type):
        case "image/png":
            return "image/png"
        case "image/jpeg" | "image/jpg":
            return "image/jpeg"
        case "image/gif":
            return "image/gif"
        case "image/webp":
            return "image/webp"
        case _:
            return None


def _conversion_hint(converted_from: str | None, converted_to: str) -> str | None:
    if not converted_from or converted_from == converted_to:
        return None
    return f"[Image converted from {converted_from} to {converted_to}.]"


async def process_image(
    image_bytes: bytes | bytearray | memoryview,
    mime_type: str,
    options: ProcessImageOptions | None = None,
) -> ProcessImageResult:
    raw_bytes = bytes(image_bytes)
    normalized_mime_type = _normalize_supported_image_mime_type(mime_type)
    converted_from: str | None = None

    if normalized_mime_type is None:
        converted_bytes = await convert_image_bytes_to_png(raw_bytes)
        if converted_bytes is None:
            return ProcessImageFailure(
                message="[Image omitted: could not be converted to a supported inline image format.]"
            )
        raw_bytes = converted_bytes
        normalized_mime_type = "image/png"
        converted_from = _base_mime_type(mime_type)

    auto_resize_images = (
        True
        if options is None or options.autoResizeImages is None
        else options.autoResizeImages
    )
    if auto_resize_images:
        resized = await resize_image_bytes(
            raw_bytes,
            normalized_mime_type,
            options.resizeOptions if options is not None else None,
        )
        if resized is None:
            return ProcessImageFailure(
                message="[Image omitted: could not be resized below the inline image size limit.]"
            )

        hints: list[str] = []
        converted_hint = _conversion_hint(converted_from, resized.mimeType)
        if converted_hint is not None:
            hints.append(converted_hint)
        dimension_note = format_dimension_note(resized)
        if dimension_note is not None:
            hints.append(dimension_note)
        return ProcessImageSuccess(
            data=resized.data, mimeType=resized.mimeType, hints=hints
        )

    hints = []
    converted_hint = _conversion_hint(converted_from, normalized_mime_type)
    if converted_hint is not None:
        hints.append(converted_hint)
    return ProcessImageSuccess(
        data=base64.b64encode(raw_bytes).decode("ascii"),
        mimeType=normalized_mime_type,
        hints=hints,
    )


__all__ = [
    "ProcessImageFailure",
    "ProcessImageOptions",
    "ProcessImageResult",
    "ProcessImageSuccess",
    "process_image",
]
