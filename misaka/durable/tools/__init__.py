"""Coding tools: ``read``, ``write``, ``edit``, ``bash`` and ``powershell``.

Translated from pi's ``packages/durable/src/tools/index.ts`` (v1.3.0).
"""

from __future__ import annotations

from typing import Any

from ..harness.define import defineExtension
from .bash import createBashTool, createPowerShellTool
from .edit import createEditTool
from .image_processor import (
    DEFAULT_IMAGE_LIMITS,
    ImageLimits,
    ImageProcessor,
    PreparedImage,
)
from .read import ReadToolOptions, createReadTool
from .write import createWriteTool


def createCodingTools(options: ReadToolOptions | None = None) -> dict[str, Any]:
    """The ``coding-tools`` extension: ``read``, ``write``, ``edit``, and ``bash``; nothing installs it automatically.
    ``createPowerShellTool()`` adds ``powershell``. ``images`` prepares the images ``read`` returns (see
    ``ReadToolOptions``)."""
    return defineExtension(
        {
            "name": "coding-tools",
            "tools": [createReadTool(options or {}), createWriteTool(), createEditTool(), createBashTool()],
        }
    )


CodingTools = createCodingTools()
"""``createCodingTools()`` without an image processor."""

__all__ = [
    "DEFAULT_IMAGE_LIMITS",
    "CodingTools",
    "ImageLimits",
    "ImageProcessor",
    "PreparedImage",
    "ReadToolOptions",
    "createBashTool",
    "createCodingTools",
    "createEditTool",
    "createPowerShellTool",
    "createReadTool",
    "createWriteTool",
]
