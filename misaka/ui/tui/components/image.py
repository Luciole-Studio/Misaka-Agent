"""Translated from pi-tui's ``packages/tui/src/components/image.ts`` (v1.1.0)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from misaka.ui.tui.terminal_image import (
    ImageDimensions,
    allocateImageId,
    getCapabilities,
    getCellDimensions,
    getImageDimensions,
    getPngDimensions,
    imageFallback,
    renderImage,
)
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth

# Converts base64 image data to base64 PNG data, or returns null if it cannot.
# Called synchronously during rendering.
type ImageTranscoder = Callable[[str, str], str | None]

imageTranscoder: ImageTranscoder | None = None
# Backstop for callers that recreate Image instances. Keyed by source data, least recently used first.
pngCache: dict[str, str | None] = {}


def setImageTranscoder(transcoder: ImageTranscoder | None) -> None:
    """Register the converter used for non-PNG images on Kitty-protocol terminals, which only accept PNG.
    Without one, such images render as text fallbacks.
    """
    global imageTranscoder
    imageTranscoder = transcoder
    pngCache.clear()


def toPng(base64Data: str, mimeType: str) -> str | None:
    if not imageTranscoder:
        return None
    png = pngCache.pop(base64Data) if base64Data in pngCache else imageTranscoder(base64Data, mimeType)
    pngCache[base64Data] = png
    if len(pngCache) > 32:
        del pngCache[next(iter(pngCache))]
    return png


@dataclass(slots=True)
class ImageTheme:
    fallbackColor: Callable[[str], str]


@dataclass(slots=True)
class ImageOptions:
    maxWidthCells: int | None = None
    maxHeightCells: int | None = None
    filename: str | None = None
    # Kitty image ID. If provided, reuses this ID (for animations/updates).
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
        if options is None:
            options = ImageOptions()
        self.base64Data = base64Data
        self.mimeType = mimeType
        self.theme = theme
        self.options = options
        self.dimensions = (
            dimensions or getImageDimensions(base64Data, mimeType) or ImageDimensions(widthPx=800, heightPx=600)
        )
        self.imageId = options.imageId
        # Converted PNG data for Kitty. Failures are not stored so a later transcoder can retry.
        self.pngData: str | None = None

        self.cachedLines: list[str] | None = None
        self.cachedWidth: int | None = None

    def getImageId(self) -> int | None:
        """Get the Kitty image ID used by this image (if any)."""
        return self.imageId

    def invalidate(self) -> None:
        self.cachedLines = None
        self.cachedWidth = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines and self.cachedWidth == width:
            return self.cachedLines

        maxWidthCells = self.options.maxWidthCells
        maxWidth = max(1, min(width - 2, maxWidthCells if maxWidthCells is not None else 60))
        cellDimensions = getCellDimensions()
        defaultMaxHeight = max(1, math.ceil((maxWidth * cellDimensions.widthPx) / cellDimensions.heightPx))
        maxHeight = self.options.maxHeightCells if self.options.maxHeightCells is not None else defaultMaxHeight

        caps = getCapabilities()
        data: str | None = self.base64Data
        dimensions = self.dimensions
        if caps.images == "kitty" and self.mimeType != "image/png":
            if self.pngData is None:
                self.pngData = toPng(self.base64Data, self.mimeType)
            data = self.pngData
            # Conversion may apply EXIF rotation, so prefer the PNG's own dimensions.
            if data:
                dimensions = getPngDimensions(data) or dimensions
        lines: list[str]

        if caps.images and data:
            if caps.images == "kitty" and self.imageId is None:
                self.imageId = allocateImageId()
            result = renderImage(
                data,
                dimensions,
                {
                    "maxWidthCells": maxWidth,
                    "maxHeightCells": maxHeight,
                    "imageId": self.imageId,
                    "moveCursor": False,
                },
            )

            if result:
                # Store the image ID for later cleanup
                if result.imageId:
                    self.imageId = result.imageId

                if caps.images == "kitty":
                    # For Kitty: C=1 prevents cursor movement.
                    # Don't need the cursor movement.
                    lines = [result.sequence]

                    # Return `rows` lines so TUI accounts for image height.
                    for _i in range(result.rows - 1):
                        lines.append("")
                else:
                    # Return `rows` lines so TUI accounts for image height.
                    # First (rows-1) lines are empty and cleared before the image is drawn.
                    # Last line: move cursor back up, draw the image, then move back down
                    # so TUI cursor accounting stays inside the scroll area.
                    lines = []
                    for _i in range(result.rows - 1):
                        lines.append("")
                    rowOffset = result.rows - 1
                    moveUp = f"\x1b[{rowOffset}A" if rowOffset > 0 else ""
                    lines.append(moveUp + result.sequence)
            else:
                fallback = imageFallback(self.mimeType, self.dimensions, self.options.filename)
                lines = [truncateToWidth(self.theme.fallbackColor(fallback), width)]
        else:
            fallback = imageFallback(self.mimeType, self.dimensions, self.options.filename)
            lines = [truncateToWidth(self.theme.fallbackColor(fallback), width)]

        self.cachedLines = lines
        self.cachedWidth = width

        return lines


__all__ = ["Image", "ImageOptions", "ImageTheme", "ImageTranscoder", "setImageTranscoder"]
