"""Animated Armin easter-egg component rendered from XBM pixel data."""

from __future__ import annotations

import math
import random
import threading
from typing import Any, Literal

from misaka.tui import TUI

from misaka.modes.interactive.theme.theme import theme

# XBM image: 31x36 pixels, LSB first, 1=background, 0=foreground.
WIDTH = 31
HEIGHT = 36
BITS = [
    0xFF,
    0xFF,
    0xFF,
    0x7F,
    0xFF,
    0xF0,
    0xFF,
    0x7F,
    0xFF,
    0xED,
    0xFF,
    0x7F,
    0xFF,
    0xDB,
    0xFF,
    0x7F,
    0xFF,
    0xB7,
    0xFF,
    0x7F,
    0xFF,
    0x77,
    0xFE,
    0x7F,
    0x3F,
    0xF8,
    0xFE,
    0x7F,
    0xDF,
    0xFF,
    0xFE,
    0x7F,
    0xDF,
    0x3F,
    0xFC,
    0x7F,
    0x9F,
    0xC3,
    0xFB,
    0x7F,
    0x6F,
    0xFC,
    0xF4,
    0x7F,
    0xF7,
    0x0F,
    0xF7,
    0x7F,
    0xF7,
    0xFF,
    0xF7,
    0x7F,
    0xF7,
    0xFF,
    0xE3,
    0x7F,
    0xF7,
    0x07,
    0xE8,
    0x7F,
    0xEF,
    0xF8,
    0x67,
    0x70,
    0x0F,
    0xFF,
    0xBB,
    0x6F,
    0xF1,
    0x00,
    0xD0,
    0x5B,
    0xFD,
    0x3F,
    0xEC,
    0x53,
    0xC1,
    0xFF,
    0xEF,
    0x57,
    0x9F,
    0xFD,
    0xEE,
    0x5F,
    0x9F,
    0xFC,
    0xAE,
    0x5F,
    0x1F,
    0x78,
    0xAC,
    0x5F,
    0x3F,
    0x00,
    0x50,
    0x6C,
    0x7F,
    0x00,
    0xDC,
    0x77,
    0xFF,
    0xC0,
    0x3F,
    0x78,
    0xFF,
    0x01,
    0xF8,
    0x7F,
    0xFF,
    0x03,
    0x9C,
    0x78,
    0xFF,
    0x07,
    0x8C,
    0x7C,
    0xFF,
    0x0F,
    0xCE,
    0x78,
    0xFF,
    0xFF,
    0xCF,
    0x7F,
    0xFF,
    0xFF,
    0xCF,
    0x78,
    0xFF,
    0xFF,
    0xDF,
    0x78,
    0xFF,
    0xFF,
    0xDF,
    0x7D,
    0xFF,
    0xFF,
    0x3F,
    0x7E,
    0xFF,
    0xFF,
    0xFF,
    0x7F,
]

BYTES_PER_ROW = math.ceil(WIDTH / 8)
DISPLAY_HEIGHT = math.ceil(HEIGHT / 2)

type Effect = Literal["typewriter", "scanline", "rain", "fade", "crt", "glitch", "dissolve"]

EFFECTS: list[Effect] = ["typewriter", "scanline", "rain", "fade", "crt", "glitch", "dissolve"]


def get_pixel(x: int, y: int) -> bool:
    if y >= HEIGHT:
        return False
    byte_index = y * BYTES_PER_ROW + math.floor(x / 8)
    bit_index = x % 8
    return ((BITS[byte_index] >> bit_index) & 1) == 0


def get_char(x: int, row: int) -> str:
    upper = get_pixel(x, row * 2)
    lower = get_pixel(x, row * 2 + 1)
    if upper and lower:
        return "█"
    if upper:
        return "▀"
    if lower:
        return "▄"
    return " "


def build_final_grid() -> list[list[str]]:
    grid: list[list[str]] = []
    for row in range(DISPLAY_HEIGHT):
        line: list[str] = []
        for x in range(WIDTH):
            line.append(get_char(x, row))
        grid.append(line)
    return grid


def _shuffle_positions() -> list[tuple[int, int]]:
    positions = [(row, x) for row in range(DISPLAY_HEIGHT) for x in range(WIDTH)]
    random.shuffle(positions)
    return positions


class ArminComponent:
    wantsKeyRelease = False

    def __init__(self, ui: TUI | Any, effect: Effect | None = None) -> None:
        self.ui = ui
        self.intervalId: threading.Timer | None = None
        self._animationToken = 0
        self.effect = effect or random.choice(EFFECTS)
        self.finalGrid = build_final_grid()
        self.currentGrid = self._create_empty_grid()
        self.effectState: dict[str, object] = {}
        self.cachedLines: list[str] = []
        self.cachedWidth = 0
        self.gridVersion = 0
        self.cachedVersion = -1

        self._init_effect()
        self.startAnimation()

    def invalidate(self) -> None:
        self.cachedWidth = 0

    def handleInput(self, data: str) -> None:
        del data

    def render(self, width: int) -> list[str]:
        if width == self.cachedWidth and self.cachedVersion == self.gridVersion:
            return self.cachedLines

        padding = 1
        available_width = width - padding
        rendered: list[str] = []

        for row in self.currentGrid:
            clipped = "".join(row[:available_width])
            pad_right = max(0, width - padding - len(clipped))
            rendered.append(f" {theme.fg('accent', clipped)}{' ' * pad_right}")

        message = "ARMIN SAYS HI"
        msg_pad_right = max(0, width - padding - len(message))
        rendered.append(f" {theme.fg('accent', message)}{' ' * msg_pad_right}")

        self.cachedLines = rendered
        self.cachedWidth = width
        self.cachedVersion = self.gridVersion
        return rendered

    def _create_empty_grid(self) -> list[list[str]]:
        return [[" " for _ in range(WIDTH)] for _ in range(DISPLAY_HEIGHT)]

    def _init_effect(self) -> None:
        match self.effect:
            case "typewriter":
                self.effectState = {"pos": 0}
            case "scanline":
                self.effectState = {"row": 0}
            case "rain":
                self.effectState = {
                    "drops": [
                        {"y": -random.randrange(DISPLAY_HEIGHT * 2), "settled": 0}
                        for _ in range(WIDTH)
                    ]
                }
            case "fade":
                self.effectState = {"positions": _shuffle_positions(), "idx": 0}
            case "crt":
                self.effectState = {"expansion": 0}
            case "glitch":
                self.effectState = {"phase": 0, "glitchFrames": 8}
            case "dissolve":
                chars = [" ", "░", "▒", "▓", "█", "▀", "▄"]
                self.currentGrid = [
                    [random.choice(chars) for _ in range(WIDTH)]
                    for _ in range(DISPLAY_HEIGHT)
                ]
                self.effectState = {"positions": _shuffle_positions(), "idx": 0}

    def startAnimation(self) -> None:
        self.stopAnimation()
        fps = 60 if self.effect == "glitch" else 30
        self._schedule_next_frame(self._animationToken, 1000 / fps)

    def stopAnimation(self) -> None:
        self._animationToken += 1
        if self.intervalId is not None:
            self.intervalId.cancel()
            self.intervalId = None

    def _schedule_next_frame(self, token: int, interval_ms: float) -> None:
        def tick() -> None:
            if token != self._animationToken:
                return
            done = self._advance_frame()
            if done:
                self.stopAnimation()
                return
            self._schedule_next_frame(token, interval_ms)

        timer = threading.Timer(interval_ms / 1000, tick)
        timer.daemon = True
        self.intervalId = timer
        timer.start()

    def _advance_frame(self) -> bool:
        done = self._tick_effect()
        self._update_display()
        request_render = getattr(self.ui, "requestRender", None)
        if callable(request_render):
            request_render()
        return done

    def _tick_effect(self) -> bool:
        match self.effect:
            case "typewriter":
                return self._tick_typewriter()
            case "scanline":
                return self._tick_scanline()
            case "rain":
                return self._tick_rain()
            case "fade":
                return self._tick_fade()
            case "crt":
                return self._tick_crt()
            case "glitch":
                return self._tick_glitch()
            case "dissolve":
                return self._tick_dissolve()
        return True

    def _tick_typewriter(self) -> bool:
        state = self.effectState
        pos = int(state["pos"])
        for _ in range(3):
            row = pos // WIDTH
            x = pos % WIDTH
            if row >= DISPLAY_HEIGHT:
                state["pos"] = pos
                return True
            self.currentGrid[row][x] = self.finalGrid[row][x]
            pos += 1
        state["pos"] = pos
        return False

    def _tick_scanline(self) -> bool:
        state = self.effectState
        row = int(state["row"])
        if row >= DISPLAY_HEIGHT:
            return True
        for x in range(WIDTH):
            self.currentGrid[row][x] = self.finalGrid[row][x]
        state["row"] = row + 1
        return False

    def _tick_rain(self) -> bool:
        state = self.effectState
        drops = state["drops"]
        assert isinstance(drops, list)

        all_settled = True
        self.currentGrid = self._create_empty_grid()

        for x in range(WIDTH):
            drop = drops[x]
            assert isinstance(drop, dict)
            settled = int(drop["settled"])
            y = int(drop["y"])

            for row in range(DISPLAY_HEIGHT - 1, DISPLAY_HEIGHT - settled - 1, -1):
                if row >= 0:
                    self.currentGrid[row][x] = self.finalGrid[row][x]

            if settled >= DISPLAY_HEIGHT:
                continue

            all_settled = False
            target_row = -1
            for row in range(DISPLAY_HEIGHT - 1 - settled, -1, -1):
                if self.finalGrid[row][x] != " ":
                    target_row = row
                    break

            y += 1
            drop["y"] = y
            if 0 <= y < DISPLAY_HEIGHT:
                if target_row >= 0 and y >= target_row:
                    drop["settled"] = DISPLAY_HEIGHT - target_row
                    drop["y"] = -random.randrange(5) - 1
                else:
                    self.currentGrid[y][x] = "▓"

        return all_settled

    def _tick_fade(self) -> bool:
        state = self.effectState
        positions = state["positions"]
        assert isinstance(positions, list)
        idx = int(state["idx"])
        for _ in range(15):
            if idx >= len(positions):
                state["idx"] = idx
                return True
            row, x = positions[idx]
            self.currentGrid[row][x] = self.finalGrid[row][x]
            idx += 1
        state["idx"] = idx
        return False

    def _tick_crt(self) -> bool:
        state = self.effectState
        expansion = int(state["expansion"])
        mid_row = DISPLAY_HEIGHT // 2
        self.currentGrid = self._create_empty_grid()

        top = mid_row - expansion
        bottom = mid_row + expansion
        for row in range(max(0, top), min(DISPLAY_HEIGHT - 1, bottom) + 1):
            for x in range(WIDTH):
                self.currentGrid[row][x] = self.finalGrid[row][x]

        state["expansion"] = expansion + 1
        return expansion + 1 > DISPLAY_HEIGHT

    def _tick_glitch(self) -> bool:
        state = self.effectState
        phase = int(state["phase"])
        glitch_frames = int(state["glitchFrames"])

        if phase < glitch_frames:
            corrupted: list[list[str]] = []
            for row in self.finalGrid:
                offset = random.randint(-3, 3)
                glitch_row = list(row)
                if random.random() < 0.3 and offset != 0:
                    corrupted.append((glitch_row[offset:] + glitch_row[:offset])[:WIDTH])
                    continue
                if random.random() < 0.2:
                    corrupted.append(list(self.finalGrid[random.randrange(DISPLAY_HEIGHT)]))
                    continue
                corrupted.append(glitch_row)
            self.currentGrid = corrupted
            state["phase"] = phase + 1
            return False

        self.currentGrid = [row[:] for row in self.finalGrid]
        return True

    def _tick_dissolve(self) -> bool:
        state = self.effectState
        positions = state["positions"]
        assert isinstance(positions, list)
        idx = int(state["idx"])
        for _ in range(20):
            if idx >= len(positions):
                state["idx"] = idx
                return True
            row, x = positions[idx]
            self.currentGrid[row][x] = self.finalGrid[row][x]
            idx += 1
        state["idx"] = idx
        return False

    def _update_display(self) -> None:
        self.gridVersion += 1

    def dispose(self) -> None:
        self.stopAnimation()

    def __del__(self) -> None:
        self.stopAnimation()


getPixel = get_pixel
getChar = get_char
buildFinalGrid = build_final_grid

__all__ = [
    "ArminComponent",
]
