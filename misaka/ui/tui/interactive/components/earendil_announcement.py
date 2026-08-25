"""Announcement component for the Earendil blog post banner."""

from __future__ import annotations

import base64
from pathlib import Path

from misaka.tui import Container, Image, ImageOptions, ImageTheme, Spacer, Text

from misaka.config import get_bundled_interactive_asset_path
from misaka.modes.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder

BLOG_URL = "https://mariozechner.at/posts/2026-04-08-ive-sold-out/"
IMAGE_FILENAME = "clankolas.png"

_cached_image_base64: str | None = None
_attempted_image_load = False


def _load_image_base64() -> str | None:
    global _cached_image_base64, _attempted_image_load
    if _attempted_image_load:
        return _cached_image_base64

    _attempted_image_load = True
    try:
        image_bytes = Path(get_bundled_interactive_asset_path(IMAGE_FILENAME)).read_bytes()
    except OSError:
        _cached_image_base64 = None
    else:
        _cached_image_base64 = base64.b64encode(image_bytes).decode("ascii")
    return _cached_image_base64


class EarendilAnnouncementComponent(Container):
    def __init__(self) -> None:
        super().__init__()

        self.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))
        self.addChild(Text(theme.bold(theme.fg("accent", "harn has joined Earendil")), 1, 0))
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("muted", "Read the blog post:"), 1, 0))
        self.addChild(Text(theme.fg("mdLink", BLOG_URL), 1, 0))
        self.addChild(Spacer(1))

        image_base64 = _load_image_base64()
        if image_base64:
            self.addChild(
                Image(
                    image_base64,
                    "image/png",
                    ImageTheme(fallbackColor=lambda text: theme.fg("muted", text)),
                    ImageOptions(maxWidthCells=56, filename=IMAGE_FILENAME),
                )
            )
            self.addChild(Spacer(1))

        self.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))


__all__ = ["EarendilAnnouncementComponent"]
