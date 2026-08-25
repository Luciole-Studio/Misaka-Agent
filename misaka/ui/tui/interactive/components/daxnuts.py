"""Powered-by-daxnuts easter egg renderer for the interactive UI."""

from __future__ import annotations

import re
import threading
from typing import Any

from misaka.modes.interactive.theme.theme import theme

# 32x32 RGB image of dax, hex encoded (3 bytes per pixel)
DAX_HEX = (
    "bbbab8b9b9b6b9b8b5bcbbb8b8b7b4b7b5b2b6b5b2b8b7b4b7b6b3b6b4b1bdbcb8bab8b6bbb8b5b8b5b1bbb8b4c2bebb"
    "c1bebac0bdbabfbcb9c1bebabfbebbc0bfbcc0bdbabbb8b5c1bfbcbfbcb8bbb9b6bfbcb8c2bfbcc1bfbcbfbbb8bdb9b6"
    "b8b7b5b9b8b5b8b8b5b5b5b2b6b5b2b8b7b4b9b8b5b9b8b5b6b5b3bab8b5bcbab7bbb9b6bbb8b5bfb9b5bdb2abbcb0a8"
    "beb2aabeb5afbfbab6bebab7c0bfbcbebdbabebbb8c0bdbabfbebbc2bebbbdbab7c3c0bdc3c0bdc1bebbc2bebabfbcb8"
    "bab9b6b7b6b3b2b1aeb6b5b2b5b4b1b5b4b2b6b5b2b7b6b4b9b8b6b7b6b3bbbab7b2afaba5988fb49e90b09481b79a88"
    "b39683b09583b7a395bfb6b0c0bdbabdbbb8bebcb9c1bfbcc0bebbbdbab7bebbb8c2bfbcc0bdbac0bcb9bdb9b6c0bcb8"
    "b5b4b2b4b3b0bab9b6b9b9b6b5b4b1b5b4b1b6b5b3b9b8b5b9b8b6b9b8b6b2aeaa968174a6836eaa856eab846eaf8973"
    "ac8973b08f79b18f7ab39786b7a89dbbb3aebfbab6c2c0bdbebcb9bfbdbac3c1bdc2bebbc0bcb9bdb9b6c1bdbabfbbb8"
    "b4b3b0b9b8b5b8b7b5b4b3b1b5b4b1b8b7b4b8b7b5bab9b6bbbab7b1afad8c7a719d735ca47860a87d65a98069ae8972"
    "ae8c75af8d77aa826ba98067aa8974b39e90b6a79dbbb2adc0bdbac1bfbdbfbbb8c1bdb9bebab6c0bdb9bfbbb8c1bdba"
    "b4b2b0b7b6b4b7b6b3b4b2b0bab9b7b6b5b2b6b5b2bab9b6bab9b6958c87977663aa836bac8772b08f7aad8c77b2917d"
    "b0917db0907cac8971a77d64a87f67ac8972b29887b8a89dbfbab5bfbdbac1bebac0bcb9c0bcb9c0bcb9c1bebabebab7"
    "b8b7b4b7b6b4b5b4b1b5b4b2b7b6b3b5b4b2bab9b7bab9b6b4b1ada88f7fad8973ae8d78b19684b19685b29786b69a89"
    "b29582b1917daa856ea87e66a97e66ad866ea9826baf9280b8ada6bdbbb8bebab7bfbbb8c1bdbabfbbb8bcb8b4bcb8b5"
    "b6b4b2b7b5b3b6b5b2b8b7b4b3b2afb8b7b4b6b5b2b3b2b0b3a59aab856fad8d78b0917eb19886b49b8bb49a89b39785"
    "b0917eaf8f7cab866fa77d65a77a61a87d64a9816ab08f79b5a296c1bcb8c3bfbcc2bebbbebab7bfbbb7bdbab6c2beba"
    "b8b7b4b7b6b4b6b5b3b7b6b3b6b5b2b9b8b6b4b3b1b6b1acac8f7ca9826bae8f7aaf9583b49c8cb49c8bb79d8cb59987"
    "b19380ad8e79ae8c77af8e78ac8771a3775faa826bae8972b39888bbb6b2bebbb8bfbbb8bfbbb8c0bdb9bebbb7c0bdb9"
    "b6b5b2b9b8b5b4b3b1b8b7b5b4b3b0b7b6b4b6b5b3b1a7a0aa8772a77d65a88570b49887b19b8d9c887c907a6d987f71"
    "aa907faf917daf8e7aad8c78ac8b77a8836ca9836cac8770b49b8abdb6b2c0bcb9c0bdb9bfbbb8bebab7bfbcb9bebab7"
    "b9b8b6b5b4b2b9b8b5b8b7b5b8b7b4b7b6b4b5b4b2b3a9a2ad8973a1755da9856fb398858c776a65544b776358725d52"
    "6e594d9c7f6eb1907ba68672ad8e7aab8771ac856db18f79b3a092beb9b5c1bdbabdb9b5bebab7bfbbb7bebab7bcb9b6"
    "b7b6b4b6b6b3b8b7b4b5b4b2b8b6b4b7b6b3b4b3b0b4aba4a6826ba3775fb08e79b19584a88e7daa8e7db29481ad8f7c"
    "997e6da38674ac8d79ac8e7aae917f9a7c6a896a599a7c6ab3a398c1bdbabdb9b6bcb8b5bebab6bebab7bdb9b5bdb9b6"
    "b5b4b1b7b5b3b5b4b2b7b6b3b7b6b4b3b3b0b3b2b0b4aca5a7846fa97f68ae8f7bae9383b59c8bb2937fae8e79ac8b76"
    "af927eaf927eb29683b39885b2988891786a72594c6e594d978d86bdbab7bab7b3c0bcb9c0bcb9bebab7bebbb7bdb9b6"
    "b3b2b0b4b3b0b5b4b2b4b4b1b4b3b1b4b3b1b4b3b0b6ada5aa8670a57a62ad8e7ab29b8cb69d8dab856fa9826aa88069"
    "ab8771af907db49987b19684b29886b59987b39480b09787b5a9a1bcb8b5bebab7bdb9b5bebab7bfbbb8bfbbb7bbb7b4"
    "b3b2afb8b7b5b8b7b5b3b2b0b5b4b2b6b5b3b6b4b1afa299a98975a9826baf907cb39988b49a89af8e7aac8973aa856e"
    "af8c74b1917dae907dac907db39988b29785b49785b7a090b9aca3bfbab7bcb8b5bdb9b6bcb8b4bcb8b5bdb9b5bcb8b4"
    "b5b4b2b6b5b3b4b3b0b4b3b0b9b8b5b8b6b4908b88887467aa8f7ea78976ad8973b08b74b59885b69e8eb29888b1917c"
    "b1917db1937fae907cb19686b39a8ab29886b59b8ab8a192b6aaa3b7b2afbcb8b4bcb8b5bbb7b4c0bcb9bebab7c0bcb9"
    "b6b5b2b6b5b3b4b3b0bab9b7b7b6b4b1b0ae7b716ba083709b806f716158967764b08870b29481b69b8ab69f8fb39a89"
    "b69f90b49d8db39a89b29988b49c8cb6a090b8a496baa49593867f8f8986bfbbb7bdb9b5bcb7b4bab6b3b9b5b2bab6b2"
    "b4b3b1b3b3b0b6b5b3b8b7b5b4b2b0a7a5a38f837dae917ea084725a504c63544da28370b39784b59e8db2a093a69890"
    "9b918b998e8790857e95877dad998bb39c8cb5a091b9a2938d827c95908dbebab6bbb7b3bdbab7bbb7b4bdb9b6bbb7b4"
    "b4b3b0b5b4b1b8b7b5b6b5b3b8b8b5b4b2af968f8ab29a8bab9485544b483a323073655d96887f70655f61595547403e"
    "453e3c453f3d57504f655e5b90847db39c8db7a090b6a09189807aaba6a3bdb9b6c0bcb9bebab7bcb7b4bebab7bbb7b4"
    "b3b2b0b6b5b3b2b1afb7b6b4b8b7b4b5b4b1aeaba8b5a89fac998d4d44412d25244d46444e4744322b293a3230423937"
    "433a37352d2a59504c534b48524a48988a81b59f8fb19c8d827974b2afacbdb9b5bcb8b4bdb9b5bcb8b5bdb9b6bab6b2"
    "b8b7b5b5b4b2b6b6b3b9b8b5b7b6b3b6b5b2b8b6b3b9b4b1b2a9a26c64612d25242d2625312a28352d2c453d3a78675c"
    "8d7a6ea09792aea6a0615854332b29524a479f8e82b09d90a49b96c1bdb9bebab7bfbbb8bbb8b4b9b5b1b8b4b0b9b4b0"
    "b7b6b4b8b7b5b8b7b4b6b5b3b8b6b3bab9b6b9b8b5b4b3b0b7b5b2a5a29f453d3b261e1d261f1e2e2625413936857268"
    "977865b19482b5a69caca5a07c7572453d3b746963a0948cc5bfbbc0bbb8beb9b6bbb7b3bbb6b3b7b3afb8b4b0b9b5b1"
    "b7b6b3b6b5b3b5b4b2b5b4b2b7b6b3b7b6b3b8b6b3b4b2afb7b6b3b3b1ae6d6765251f1e1e18172a22212d2523443b39"
    "71625ab19888b09482a89182877e792c25243e3634766d6abeb9b5bfbbb7bebab6bcb7b3bbb6b3b9b5b1b7b3afb8b4b0"
    "b4b3b0b5b4b1b5b4b1b4b3b1b5b4b2b8b6b4b5b3b0b9b6b4b5b4b1b6b4b27f79762a2322221c1b2d2524221b1a443e3c"
    "47413f6f676281766f867971675e5a3e37352a222166605dbab7b3bdb9b5beb9b5bcb7b3bcb7b3b9b4b0bab6b2bab6b2"
    "b5b3b0b6b4b2b3b2afb7b6b3b4b4b1b4b3b0b6b4b1b5b4b1b4b3b0b9b6b29a8c8252474230292828201f181212322c2c"
    "231e1d1c16162c26252923222d26252d2523332b2a8e8885bcb8b5bcb7b3bbb6b2bcb7b3b9b4b1b9b5b1b7b2afb7b2ae"
    "7a838e9b9b9caeadacb3b2b0b3b2afb7b7b4b6b5b3b6b6b3b7b6b3b9ada4a991808e7b6f50453f2b24231a1414292322"
    "1f19181d17161f18182620201d17162a22215d5654b7b3b0bbb7b3bbb6b2b8b4b0bab5b1bbb6b2bab5b1b8b4b0bab6b2"
    "2c496b4c5d735f68766e727a828285929090adaba8b7b2aeb6a59ab39682a28470a387748e76674e403a1a14141d1716"
    "181211221c1c1f1918221c1b2f2827342d2c8d8884bab6b3b9b5b2bab5b1bab5b1b9b4b0bab6b2b8b4b0b9b4b0b7b2ae"
    "325e8b365f8a3a5d833f5b7a545f70646469706b6aa08f84b08e78b18e769f7e689e7f6b9e816d907766584940362d2a"
    "1c1615201b1a1a1413201a1a251e1d393331a39e9bbab5b1bcb7b3bab6b2b8b3afb8b4b0b9b4b0b9b4b1bab5b2b5b0ac"
    "3d6c9843729d44719c426e98415f805a64716f6a699d8677b1927eb3947faa89749d7a649f7f6ba487749e8371867164"
    "54463f2c25231e181837302e3a33317a7471beb9b6bcb8b4bbb6b2b6b2aebab5b1b9b5b1b8b3afbab6b2b6b1adb5aeaa"
    "4877a14c7aa44e7ba345719a3a5d80586b7f767475927b6eb1927faf8e79b08e78a78169a07861a17f6aa58570a68874"
    "9b83738270666f66618a8480a49e99b7b2aebab6b2bcb8b4b9b5b1b7b2aebab5b1b9b4b0b6b1aeb6b1adb2aca8b2aca8"
    "4876a04a78a2517fa74771973a5d80405c7a6161677c695fac8a75b08d77b4917aaf8971ad876fa5816aa6846ea78670"
    "a98a76ac9484ab9f96b2aca8bdb8b4bcb7b3bcb8b4bcb8b4b8b3afb7b2aeb9b4b0b8b3afb8b2aeb6afabb3aeaab2aeaa"
    "4878a14b7aa34c7ba44a759b3d63873b5f825b67766f5f569c7e6caf8c77b18f79b28f78b5927caf8e78a98872aa8a76"
    "a98a76ac917fada199b7b0acb9b3afbfb9b5c1bab6bdb6b2b8b3afbab5b1b9b4b0b6afabb7b1adb3ada9b3aeaab0aba8"
)

WIDTH = 32
HEIGHT = 32
_RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _parse_image() -> list[list[tuple[int, int, int]]]:
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(HEIGHT):
        row: list[tuple[int, int, int]] = []
        for x in range(WIDTH):
            idx = (y * WIDTH + x) * 6
            r = int(DAX_HEX[idx : idx + 2], 16)
            g = int(DAX_HEX[idx + 2 : idx + 4], 16)
            b = int(DAX_HEX[idx + 4 : idx + 6], 16)
            row.append((r, g, b))
        pixels.append(row)
    return pixels


def _rgb(r: int, g: int, b: int, bg: bool = False) -> str:
    return f"\x1b[{48 if bg else 38};2;{r};{g};{b}m"


def _build_image() -> list[str]:
    pixels = _parse_image()
    lines: list[str] = []
    for row in range(0, HEIGHT, 2):
        line_parts: list[str] = []
        for x in range(WIDTH):
            top = pixels[row][x]
            bottom = pixels[row + 1][x] if row + 1 < HEIGHT else top
            line_parts.append(
                f"{_rgb(bottom[0], bottom[1], bottom[2])}{_rgb(top[0], top[1], top[2], True)}▄"
            )
        lines.append("".join(line_parts) + _RESET)
    return lines


class DaxnutsComponent:
    def __init__(self, ui: Any) -> None:
        self.ui = ui
        self.image = _build_image()
        self.interval: threading.Timer | None = None
        self.tick = 0
        self.maxTicks = 25
        self.cachedLines: list[str] = []
        self.cachedWidth = 0
        self.cachedTick = -1
        self.startAnimation()

    def invalidate(self) -> None:
        self.cachedWidth = 0

    def startAnimation(self) -> None:
        self.interval = threading.Timer(0.08, self._advance_tick)
        self.interval.daemon = True
        self.interval.start()

    def stopAnimation(self) -> None:
        if self.interval is not None:
            self.interval.cancel()
            self.interval = None

    def _advance_tick(self) -> None:
        self.interval = None
        self.tick += 1
        if self.tick >= self.maxTicks:
            self.stopAnimation()
        else:
            self.startAnimation()
        self.cachedWidth = 0
        request_render = getattr(self.ui, "requestRender", None)
        if callable(request_render):
            request_render()

    def render(self, width: int) -> list[str]:
        if width == self.cachedWidth and self.cachedTick == self.tick:
            return self.cachedLines

        lines: list[str] = []

        def center(text: str) -> str:
            visible = len(_ANSI_RE.sub("", text))
            left = max(0, (width - visible) // 2)
            return (" " * left) + text

        lines.append("")

        revealed_rows = min(self.image.__len__(), int((self.tick / self.maxTicks) * (self.image.__len__() + 3)))
        for index, image_line in enumerate(self.image):
            if index < revealed_rows:
                lines.append(center(image_line))
            elif index == revealed_rows:
                lines.append(center(_rgb(100, 200, 255) + ("▓" * WIDTH) + _RESET))
            else:
                lines.append(center(" " * WIDTH))

        lines.append("")

        text_phase = max(0.0, self.tick - (self.maxTicks * 0.6))
        if text_phase > 0 or self.tick >= self.maxTicks:
            lines.append(center(theme.fg("accent", "Free Kimi K2.5 via OpenCode Zen")))
            lines.append(center(theme.fg("success", '"Powered by daxnuts"')))
            lines.append(center(theme.fg("muted", "— @thdxr")))
        else:
            lines.extend(["", "", ""])

        lines.append("")
        if text_phase > 2 or self.tick >= self.maxTicks:
            lines.append(center(theme.fg("dim", "Try OpenCode")))
            lines.append(center(theme.fg("mdLink", "https://mistral.ai/news/mistral-vibe-2-0")))
        else:
            lines.extend(["", ""])
        lines.append("")

        self.cachedLines = lines
        self.cachedWidth = width
        self.cachedTick = self.tick
        return lines

    def dispose(self) -> None:
        self.stopAnimation()


__all__ = ["DaxnutsComponent"]
