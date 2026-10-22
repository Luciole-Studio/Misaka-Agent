"""What ``read`` needs of an image processor, and the base64 helpers it shares with one.

Translated from pi's ``packages/durable/src/tools/image-processor.ts`` (v1.3.0). Limits and a prepared image are plain
``dict`` objects with the upstream keys; a processor is any object with ``prepare(bytes, mimeType, limits)``.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Awaitable
from typing import Any, NotRequired, Protocol, TypedDict


class ImageLimits(TypedDict):
    """Limits an image the model sees must keep; the defaults below are the coding agent's."""

    maxWidth: int  # Pixels. Default 2000.
    maxHeight: int  # Pixels. Default 2000.
    maxBytes: int  # Bytes of base64. Default 4.5 MiB, below Anthropic's 5 MB.


DEFAULT_IMAGE_LIMITS: ImageLimits = {"maxWidth": 2000, "maxHeight": 2000, "maxBytes": int(4.5 * 1024 * 1024)}


class PreparedImage(TypedDict):
    """An image the model can take: a supported format, within the limits, as base64. ``resized`` (``{"from": {"width",
    "height"}, "to": {...}}``) is set when the image was resized, for the model to map coordinates back;
    ``convertedFrom``, the original format, when it differs from ``mimeType``: BMP becomes PNG, a large PNG may become
    JPEG."""

    data: str
    mimeType: str
    resized: NotRequired[dict[str, Any]]
    convertedFrom: NotRequired[str]


class ImageProcessor(Protocol):
    """Decodes, orients, resizes, and re-encodes images so the model can take them. ``read`` uses one when given;
    without one, it passes supported images within the byte limit through as they are. ``misaka.durable.images``
    provides one."""

    def prepare(self, bytes: bytes, mimeType: str, limits: ImageLimits) -> Awaitable[PreparedImage | None]:
        """The image, prepared to fit ``limits``, or ``None`` when it cannot be decoded or made to fit. Awaitable, so a
        processor can do the work off the event loop."""
        ...


INLINE_IMAGE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
"""Formats every provider takes inline; others, such as BMP, need converting."""


def toBase64(data: bytes) -> str:
    """Standard base64 of ``data``. (Upstream builds a binary string 8 KiB at a time for ``btoa``, since one
    ``String.fromCharCode`` call with megabytes of arguments overflows the stack; Python's encoder has no such limit.)"""
    return base64.b64encode(data).decode("ascii")


def base64Length(byteLength: int) -> int:
    """The length of the base64 of ``byteLength`` bytes."""
    return math.ceil(byteLength / 3) * 4


__all__ = [
    "DEFAULT_IMAGE_LIMITS",
    "INLINE_IMAGE_TYPES",
    "ImageLimits",
    "ImageProcessor",
    "PreparedImage",
    "base64Length",
    "toBase64",
]
