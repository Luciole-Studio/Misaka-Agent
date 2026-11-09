"""Translated from pi's ``modes/interactive/components/radius-login-selector.ts`` (v1.1.0).

The `/login` menu with the animated "Sign in with Radius" option. Internal to the interactive mode: the shimmer
is Radius-only and is not exposed to other selectors.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Text
from misaka.ui.tui.colors import Color, foregroundAnsi, mixColors, parseColor
from misaka.utils.js import Interval, clearInterval, setInterval

from .extension_selector import ExtensionSelectorComponent

# The four colors of the Radius logo, in the order they stream across the text.
RADIUS_COLORS: tuple[Color, ...] = tuple(parseColor(hex) for hex in ("#4d9abf", "#83ccd2", "#f1be57", "#f09082"))
# Width of each color band, in characters.
CHARS_PER_COLOR = 4
CHARS_PER_SECOND = 10
ANIMATION_FRAME_MS = 50


def radiusShimmer(text: str, elapsedMs: float) -> str:
    """Color `text` with the Radius logo colors flowing left to right; `elapsedMs` is the animation time."""
    mode = theme.getColorMode()
    cycle = len(RADIUS_COLORS) * CHARS_PER_COLOR
    offset = (elapsedMs / 1000) * CHARS_PER_SECOND
    result = ""
    index = 0
    for char in text:
        position = (((index - offset) % cycle) + cycle) % cycle
        band = math.floor(position / CHARS_PER_COLOR)
        t = position / CHARS_PER_COLOR - band
        # Smoothstep keeps each band recognizable while still blending into the next one.
        amount = t * t * (3 - 2 * t)
        from_ = RADIUS_COLORS[band]
        to = RADIUS_COLORS[(band + 1) % len(RADIUS_COLORS)]
        result += foregroundAnsi(mixColors(from_, to, amount, "srgb"), mode) + char
        index += 1  # noqa: SIM113 - upstream loop
    return f"{result}\x1b[39m"


@dataclass(slots=True)
class RadiusOption:
    """The "Sign in with Radius" option: `label` is the full option, starting with the animated `text`."""

    label: str
    text: str


class RadiusLoginMenuComponent(ExtensionSelectorComponent):
    """Swaps the line `ExtensionSelectorComponent` draws for the selected Radius option with the animated one. When the
    selector's row style changes or the label wraps, the line no longer matches and the option renders normally.
    """

    def __init__(
        self,
        tui: Any,
        title: str,
        options: list[str],
        radiusOption: RadiusOption,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
    ) -> None:
        def select(option: str) -> None:
            self.stopAnimation()
            onSelect(option)

        def cancel() -> None:
            self.stopAnimation()
            onCancel()

        self.animationStart = time.monotonic()
        self.animationTimer: Interval | None = None
        self.animating = False
        super().__init__(title, options, select, cancel)
        self.radiusOption = radiusOption

        def tick() -> None:
            if self.animating:
                tui.requestRender()

        self.animationTimer = setInterval(tick, ANIMATION_FRAME_MS)

    def render(self, width: int) -> list[str]:
        lines = super().render(width)
        label, text = self.radiusOption.label, self.radiusOption.text
        rendered = Text(theme.fg("accent", "→ ") + theme.fg("accent", label), 1, 0).render(width)
        selectedLine = rendered[0] if rendered else None
        index = lines.index(selectedLine) if selectedLine is not None and selectedLine in lines else -1
        self.animating = index >= 0
        if self.animating:
            shimmer = radiusShimmer(text, (time.monotonic() - self.animationStart) * 1000)
            animatedLine = theme.fg("accent", "→ ") + shimmer + label[len(text) :]
            animated = Text(animatedLine, 1, 0).render(width)
            lines[index] = animated[0] if animated else ""
        return lines

    def stopAnimation(self) -> None:
        clearInterval(self.animationTimer)
        self.animationTimer = None
        self.animating = False

    def dispose(self) -> None:
        self.stopAnimation()
        super().dispose()


def createLoginMenuSelector(
    tui: Any,
    title: str,
    options: list[str],
    radiusOption: RadiusOption,
    onSelect: Callable[[str], None],
    onCancel: Callable[[], None],
) -> ExtensionSelectorComponent:
    """Top-level `/login` selector whose Radius option shimmers in the Radius logo colors while it is selected."""
    return RadiusLoginMenuComponent(tui, title, options, radiusOption, onSelect, onCancel)


__all__ = ["RadiusOption", "createLoginMenuSelector", "radiusShimmer"]
