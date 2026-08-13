"""Terminal image component with fallback rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.tui.utils import truncateToWidth
from misaka.tui.terminal_image import (
    ImageDimensions,
    allocateImageId,
    getCapabilities,
    getCellDimensions,
    getImageDimensions,
    imageFallback,
    renderImage,
)
from misaka.tui.tui import Component


@dataclass(slots=True)
class ImageTheme:
    fallbackColor: Callable[[str], str]


@dataclass(slots=True)
class ImageOptions:
    maxWidthCells: int | None = None
    maxHeightCells: int | None = None
    filename: str | None = None
    imageId: int | None = None


class Image(Component):
    def __init__(
        self,
        base64Data: str,
        mimeType: str,
        theme: ImageTheme,
        options: ImageOptions | None = None,
        dimensions: ImageDimensions | None = None,
    ) -> None:
        self.base64Data = base64Data
        self.mimeType = mimeType
        self.theme = theme
        self.options = options or ImageOptions()
        self.dimensions = dimensions or getImageDimensions(base64Data, mimeType) or ImageDimensions(800, 600)
        self.imageId = self.options.imageId
        self.cachedLines: list[str] | None = None
        self.cachedWidth: int | None = None

    def getImageId(self) -> int | None:
        return self.imageId

    def invalidate(self) -> None:
        self.cachedLines = None
        self.cachedWidth = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines is not None and self.cachedWidth == width:
            return self.cachedLines

        max_width_cells = 60 if self.options.maxWidthCells is None else self.options.maxWidthCells
        max_width = max(1, min(width - 2, max_width_cells))
        cell_dimensions = getCellDimensions()
        default_max_height = max(
            1,
            (max_width * cell_dimensions.widthPx + cell_dimensions.heightPx - 1) // cell_dimensions.heightPx,
        )
        max_height = default_max_height if self.options.maxHeightCells is None else self.options.maxHeightCells

        caps = getCapabilities()
        lines: list[str]
        if caps.images:
            if caps.images == "kitty" and self.imageId is None:
                self.imageId = allocateImageId()
            result = renderImage(
                self.base64Data,
                self.dimensions,
                {
                    "maxWidthCells": max_width,
                    "maxHeightCells": max_height,
                    "imageId": self.imageId,
                    "moveCursor": False,
                },
            )
            if result is not None:
                if result.imageId is not None:
                    self.imageId = result.imageId
                if caps.images == "kitty":
                    lines = [result.sequence]
                    lines.extend("" for _ in range(max(0, result.rows - 1)))
                else:
                    lines = ["" for _ in range(max(0, result.rows - 1))]
                    row_offset = max(0, result.rows - 1)
                    move_up = f"\x1b[{row_offset}A" if row_offset > 0 else ""
                    lines.append(move_up + result.sequence)
            else:
                lines = [truncateToWidth(self.theme.fallbackColor(imageFallback(self.mimeType, self.dimensions, self.options.filename)), width)]
        else:
            lines = [truncateToWidth(self.theme.fallbackColor(imageFallback(self.mimeType, self.dimensions, self.options.filename)), width)]

        self.cachedLines = lines
        self.cachedWidth = width
        return lines


__all__ = ["Image", "ImageOptions", "ImageTheme"]
