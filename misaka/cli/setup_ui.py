"""Terminal primitives for ``misaka setup``: the logo, MISAKA's palette, an in-place radio list,
line prompts, headers.

The shape follows Hermes's setup wizard: a single-select menu driven by the arrow keys, Enter
to choose, Esc to cancel the whole wizard, Left to return to the previous section, digits to
jump to an entry; yes/no questions go through the same menu so every choice looks and feels
the same. Without a terminal (a pipe, a container) every menu falls back to a numbered line
prompt, so the wizard is never stuck waiting for a key nobody can press.

Colour comes from the theme files the chat and the panel draw from
(``ui/tui/interactive/theme/{dark,light}.json``), the variant picked from the terminal's
background the way the panel picks it, so the wizard is the same rose as the rest of the
program rather than a generic cyan-and-green shell script. Truecolor when the terminal
reports it, the nearest xterm-256 index otherwise; plain text, with the emblem in block
characters, when there is no tty, ``NO_COLOR`` is set, or ``TERM`` is dumb.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import select
import shutil
import sys
import textwrap
import threading
from collections.abc import Callable
from functools import cache


class SetupCancelled(Exception):
    """Esc or Ctrl+C: the wizard stops and leaves the remaining sections untouched."""


class SetupGoBack(Exception):
    """Left arrow at a menu: the wizard returns to the previous section."""


BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

# dark.json's vars, used when the theme file cannot be read. Its accent is the studio's own
# rose: the master artwork's ground is #cb3862 to the byte.
_FALLBACK_VARS = {"accent": "#cb3862", "roseLite": "#e8698c", "roseDeep": "#8f2545", "green": "#7fa87f",
                  "amber": "#d99a4e", "red": "#e05252", "selectedBg": "#3d2029"}
_EMBLEM_INK = (255, 255, 255)      # the mark's white, as in the master

WORDMARK = ("█▀▄▀█ █ █▀▀ ▄▀█ █▄▀ ▄▀█",
            "█ ▀ █ █ ▄██ █▀█ █ █ █▀█")

# Luciole Studio's mark: a white disc with a round notch bitten out of the top and a larger one
# out of the bottom, which is what leaves the crescent and its spark. Three concentric circles,
# least-squares fitted to the studio's 4800x4800 master -- the same file ``assets/logo.svg``
# transcribes, and the fit differs from it only along the antialiased rim -- and written here as
# fractions of the outer disc's radius, so the mark can be drawn at whatever size the window
# allows instead of being frozen into one bitmap:
#
#   outer disc   centre (2400, 2441.27)  r 1159.78  ->  (0,  0      )  r 1
#   top notch    centre (2400, 1571.18)  r  291.00  ->  (0, -0.75022)  r 0.25091
#   bottom bite  centre (2400, 2934.44)  r  726.19  ->  (0,  0.42523)  r 0.62615
#
# The notch is a quarter of the disc's radius and tangent to its top: that tangency is what
# breaks the outline into two horns. The master frames the mark at 2.07 radii of rose; the badge
# here crops to 1.30 because 22 terminal cells are not 4800 pixels.
_NOTCH = (0.0, -0.75022, 0.25091)
_BITE = (0.0, 0.42523, 0.62615)
_MARGIN = 1.30                 # half-width of the drawn square, in outer radii


# -- palette ----------------------------------------------------------------------------------

def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@cache
def _palette() -> tuple[str, dict[str, str]]:
    """(``truecolor`` | ``256color`` | ``mono``, the theme variant's colour vars by name)."""
    if not _tty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return "mono", dict(_FALLBACK_VARS)
    colours = dict(_FALLBACK_VARS)
    try:
        from misaka.config import get_themes_dir
        with open(os.path.join(get_themes_dir(), f"{_variant()}.json"), encoding="utf-8") as handle:
            found = json.load(handle)["vars"]
        colours.update({key: value for key, value in found.items()
                        if isinstance(value, str) and value.startswith("#")})
    except (OSError, ValueError, KeyError, ImportError):
        pass
    try:
        from misaka.ui.tui.terminal_image import getCapabilities
        truecolor = bool(getCapabilities().trueColor)
    except Exception:  # noqa: BLE001 - the env hint is what detection reads anyway
        truecolor = os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
    return ("truecolor" if truecolor else "256color"), colours


def _variant() -> str:
    """``dark`` or ``light``: MISAKA_THEME first, then the panel's OSC-11 probe of the terminal background."""
    try:
        from misaka.ui.panel.geometry import theme_variant
        return theme_variant()
    except Exception:  # noqa: BLE001 - detection is a nicety; dark is MISAKA's primary palette
        return "dark"


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _blend(start: tuple[int, int, int], end: tuple[int, int, int], level: float) -> tuple[int, int, int]:
    red, green, blue = (round(a + (b - a) * level) for a, b in zip(start, end, strict=True))
    return red, green, blue


_CUBE = (0, 95, 135, 175, 215, 255)


def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """CIELAB (D65), where distance follows what the eye sees rather than what the channels say."""
    def linear(channel: float) -> float:
        channel /= 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / 0.95047
    y = red * 0.2126 + green * 0.7152 + blue * 0.0722
    z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / 1.08883

    def f(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


@cache
def _xterm_palette() -> tuple[tuple[int, tuple[float, float, float]], ...]:
    """The 216-colour cube and the 24 greys, each with its CIELAB coordinates."""
    entries = [(16 + 36 * red + 6 * green + blue, (_CUBE[red], _CUBE[green], _CUBE[blue]))
               for red in range(6) for green in range(6) for blue in range(6)]
    entries += [(232 + step, (8 + step * 10,) * 3) for step in range(24)]
    return tuple((index, _lab(rgb)) for index, rgb in entries)


@cache
def _nearest_256(rgb: tuple[int, int, int]) -> int:
    """The closest xterm-256 entry, by CIELAB distance. Rounding each channel to the cube's six
    levels instead -- what the chat theme does, and good enough for a glyph -- costs the hue:
    the rose accent #cb3862 lands on #d75f5f, a salmon, which a filled badge makes obvious."""
    target = _lab(rgb)
    return min(_xterm_palette(),
               key=lambda entry: sum((a - b) ** 2 for a, b in zip(entry[1], target, strict=True)))[0]


def _sgr(rgb: tuple[int, int, int], *, background: bool = False) -> str:
    mode = _palette()[0]
    layer = 48 if background else 38
    if mode == "truecolor":
        return f"\x1b[{layer};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if mode == "256color":
        return f"\x1b[{layer};5;{_nearest_256(rgb)}m"
    return ""


def ink(role: str) -> str:
    """The SGR prefix for one of the theme's colour vars (``accent``, ``green``, ...); empty in mono."""
    return _sgr(_rgb(_palette()[1][role]))


def tilde(path: str) -> str:
    """``~/.misaka/...`` rather than the absolute path: these are lines to read, not to copy."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if code and _palette()[0] != "mono" else text


# -- the logo ---------------------------------------------------------------------------------

@cache
def _coverage(size: int, samples: int = 4) -> tuple[tuple[float, ...], ...]:
    """The mark over a ``size``x``size`` pixel grid, each pixel its 0..1 ink coverage. Sampled
    ``samples``x``samples`` per pixel, which is what softens the arcs at terminal resolution."""
    def inked(x: float, y: float) -> bool:
        return (math.hypot(x, y) <= 1.0
                and math.hypot(x - _NOTCH[0], y - _NOTCH[1]) > _NOTCH[2]
                and math.hypot(x - _BITE[0], y - _BITE[1]) > _BITE[2])

    step = 2 * _MARGIN / size
    grid = []
    for row in range(size):
        pixels = []
        for column in range(size):
            hits = sum(inked(-_MARGIN + (column + (sx + 0.5) / samples) * step,
                             -_MARGIN + (row + (sy + 0.5) / samples) * step)
                       for sy in range(samples) for sx in range(samples))
            pixels.append(hits / (samples * samples))
        grid.append(tuple(pixels))
    return tuple(grid)


def _emblem_rows(size: int) -> list[str]:
    """The mark as ``size``//2 lines of upper-half blocks: two pixels per cell, so the badge is
    square on a terminal whose cells are twice as tall as they are wide."""
    mode, colours = _palette()
    ground = _rgb(colours["accent"])
    grid = _coverage(size)
    rows = []
    for upper, lower in zip(grid[0::2], grid[1::2], strict=True):
        cells: list[str] = []
        last = None
        for above, below in zip(upper, lower, strict=True):
            if mode == "mono":
                cells.append("█" if above >= 0.5 and below >= 0.5 else "▀" if above >= 0.5 else "▄" if below >= 0.5 else " ")
                continue
            if mode == "256color":                 # no smooth blends in the cube: hard edges beat speckle
                above, below = float(above >= 0.5), float(below >= 0.5)
            paint = (_blend(ground, _EMBLEM_INK, above), _blend(ground, _EMBLEM_INK, below))
            if paint != last:
                cells.append(_sgr(paint[0]) + _sgr(paint[1], background=True))
                last = paint
            cells.append("▀")
        rows.append("".join(cells) + ("" if mode == "mono" else RESET))
    return rows


def _wordmark_row(row: str) -> str:
    """The wordmark in a rose gradient: roseLite at the left, the theme's accent at the right."""
    mode, colours = _palette()
    if mode == "mono":
        return row
    if mode != "truecolor":       # a gradient over two or three palette entries only bands
        return BOLD + ink("accent") + row + RESET
    start, end = _rgb(colours["roseLite"]), _rgb(colours["accent"])
    span = max(1, len(row) - 1)
    return BOLD + "".join(ch if ch == " " else _sgr(_blend(start, end, i / span)) + ch for i, ch in enumerate(row)) + RESET


def print_logo(title: str, *lines: str) -> None:
    """The mark, the wordmark, the title and the notes: side by side when the terminal is wide
    enough, stacked otherwise, and the mark sized (or dropped) to fit a short window."""
    from misaka.config import VERSION
    credit = f"  v{VERSION} · Luciole Studio"
    body: list[tuple[str, str]] = [(row, _wordmark_row(row)) for row in WORDMARK]
    body += [(title + credit, color(title, BOLD) + color(credit, DIM)), ("", "")]
    body += [(line, color(line, DIM)) for line in lines]
    text_width = max(len(plain) for plain, _styled in body)
    size = shutil.get_terminal_size((80, 24))
    width = 22 if size.lines >= 24 else 14 if size.lines >= 16 else 0
    mark = _emblem_rows(width) if width else []
    print()
    if mark and size.columns >= width + 5 + text_width:
        top = max(0, (len(mark) - len(body)) // 2)
        for index, row in enumerate(mark):
            styled = body[index - top][1] if 0 <= index - top < len(body) else ""
            print(f"  {row}   {styled}".rstrip())
        for _plain, styled in body[len(mark) - top:]:
            print(" " * (width + 5) + styled)
    else:
        for row in mark:
            print(f"  {row}")
        if mark:
            print()
        for plain, styled in body:
            if len(plain) + 2 <= size.columns:
                print(f"  {styled}")
            else:
                for piece in textwrap.wrap(plain, max(20, size.columns - 2)):
                    print(f"  {color(piece, DIM)}")
    print()


# -- lines ------------------------------------------------------------------------------------

def print_banner(*lines: str) -> None:
    rule = "─" * max(60, min(72, max(len(line) for line in lines) + 2))
    print()
    print(color(rule, ink("accent")))
    for index, line in enumerate(lines):
        print(color(f" {line}", BOLD if index == 0 else DIM))
    print(color(rule, ink("accent")))


def print_header(title: str) -> None:
    print()
    print(color("── ", ink("accent")) + color(title, BOLD) + " "
          + color("─" * max(0, 60 - len(title) - 4), ink("roseDeep")))


def print_info(*lines: str) -> None:
    for line in lines:
        print(f"  {line}")


def print_success(text: str) -> None:
    print(color("  ✓ ", ink("green")) + text)


def print_warning(text: str) -> None:
    print(color("  ! ", ink("amber")) + text)


def print_error(text: str) -> None:
    print(color("  ✗ ", ink("red")) + text)


def print_check(ok: bool | None, label: str, detail: str = "") -> None:
    """One row of a check table: ✓ present, ✗ missing, – optional and absent."""
    mark, code = ("✓", ink("green")) if ok else ("–", DIM) if ok is None else ("✗", ink("red"))
    print(color(f"  {mark} ", code) + color(f"{label:<22}", DIM if ok is None else "")
          + (color(detail, DIM) if detail else ""))


def prompt(question: str, default: str | None = None, *, password: bool = False) -> str:
    """A line prompt. Empty input returns ``default``; Ctrl+C or a closed stdin cancels."""
    suffix = color(f" [{default}]", DIM) if default else ""
    text = f"  {question}{suffix}: "
    try:
        value = getpass.getpass(text) if password else input(text)
    except (KeyboardInterrupt, EOFError):
        print()
        raise SetupCancelled from None
    value = value.strip()
    return value or (default or "")


def prompt_cancellable(question: str, cancel: threading.Event) -> str:
    """A line prompt that gives up when ``cancel`` is set, instead of holding stdin forever.

    OAuth's browser login races a local callback server against a paste-the-code prompt: when
    the browser wins, the prompt has to stop reading, or the next thing the wizard asks will
    be answered by a thread still sitting in ``input()``. A plain ``input()`` cannot be
    interrupted, so this polls instead.
    """
    import termios
    import tty
    fd = sys.stdin.fileno()
    sys.stdout.write(f"  {question}: ")
    sys.stdout.flush()
    try:
        old_attrs = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return input()
    typed = ""
    try:
        tty.setcbreak(fd)
        while not cancel.is_set():
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            char = os.read(fd, 1)
            if not char or char in (b"\r", b"\n"):
                break
            if char == b"\x03":
                raise SetupCancelled
            if char in (b"\x7f", b"\b"):
                if typed:
                    typed = typed[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            # Echoed, not masked: this is an authorization code being pasted back, and a
            # row of stars makes a mistyped one impossible to spot.
            decoded = char.decode("utf-8", "replace")
            typed += decoded
            sys.stdout.write(decoded)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        print()
    return "" if cancel.is_set() else typed.strip()


def prompt_choice(question: str, choices: list[str], default: int = 0, description: str | None = None) -> int:
    """Single-select menu. Returns the chosen index; Esc raises SetupCancelled, Left raises SetupGoBack."""
    index = _radiolist(question, choices, default, description) if _menu_usable() else _numbered(question, choices, default, description)
    if index == -1:
        raise SetupCancelled
    if index == -2:
        raise SetupGoBack
    print(f"  {question} " + color("→ ", DIM) + color(choices[index], ink("accent")))
    return index


def prompt_yes_no(question: str, default: bool = True) -> bool:
    return prompt_choice(question, ["Yes", "No"], 0 if default else 1) == 0


def run_steps(steps: list[tuple[str, Callable[[], None]]]) -> None:
    """Run the sections in order; a Left arrow at a section's menu reopens the previous one."""
    index = 0
    while index < len(steps):
        _label, action = steps[index]
        try:
            action()
        except SetupGoBack:
            if index == 0:
                continue
            index -= 1
            print_info("", color(f"Returning to {steps[index][0]}...", DIM))
            continue
        index += 1


# -- the menu --------------------------------------------------------------------------------

def _menu_usable() -> bool:
    if not _tty() or os.environ.get("MISAKA_SETUP_PLAIN"):
        return False
    try:
        import termios  # noqa: F401 - absent on Windows, which gets the numbered prompt
        import tty  # noqa: F401
    except ImportError:
        return False
    return True


def _numbered(question: str, choices: list[str], default: int, description: str | None) -> int:
    print(f"\n  {question}")
    if description:
        print(color(f"  {description}", DIM))
    for number, choice in enumerate(choices, 1):
        marker = color("●", ink("accent")) if number - 1 == default else color("○", DIM)
        print(f"    {marker} {number}. {choice}")
    while True:
        try:
            raw = input(f"  Choice [{default + 1}] (b = back, q = quit): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return -1
        if raw == "":
            return default
        if raw == "q":
            return -1
        if raw == "b":
            return -2
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return int(raw) - 1


_ARROWS = {"[A": "up", "[B": "down", "[D": "left", "OA": "up", "OB": "down", "OD": "left"}


def _read_key(fd: int) -> str:
    data = os.read(fd, 1)
    if not data:
        return "esc"                                  # stdin closed under us
    if data == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:  # a bare Esc, not the head of an arrow sequence
            return "esc"
        data += os.read(fd, 8)
        return _ARROWS.get(data[1:3].decode("ascii", "replace"), "")
    if data in (b"\r", b"\n"):
        return "enter"
    if data == b"\x03":
        return "ctrl-c"
    return data.decode("utf-8", "replace")


def _radiolist(question: str, choices: list[str], default: int, description: str | None) -> int:
    """The menu, drawn in place with the theme's colours rather than on an alternate screen, so
    the logo and the answers so far stay in view; erased once a choice is made, and the caller
    echoes the choice on one line."""
    import termios
    import tty

    fd, out = sys.stdin.fileno(), sys.stdout
    _mode, colours = _palette()
    accent, selected = ink("accent"), _sgr(_rgb(colours["selectedBg"]), background=True)
    header = ["  " + color(question, BOLD)]
    if description:
        header.append("  " + color(description, DIM))
    header.append("  " + color("↑↓ move · Enter select · ← back · Esc cancel · 1-9 jump", DIM))
    size = shutil.get_terminal_size((80, 24))
    room = max(1, size.lines - len(header) - 2)        # the block is redrawn in place: it has to fit
    visible = min(len(choices), room)
    scrolls = len(choices) > visible
    if scrolls:
        visible = max(1, visible - 2)                  # the two "N more" rows share the same room
    block = len(header) + visible + (2 if scrolls else 0)
    cursor, top = default, 0

    def draw(first: bool) -> None:
        nonlocal top
        if cursor < top:
            top = cursor
        elif cursor >= top + visible:
            top = cursor - visible + 1
        lines = list(header)
        if scrolls:
            lines.append(color(f"      ↑ {top} more", DIM) if top else "")
        for index in range(top, top + visible):
            if index >= len(choices):
                lines.append("")
                continue
            label = choices[index][:max(10, size.columns - 9)]
            if index == cursor:
                lines.append("  " + color(" ", selected) + color("●", accent + selected) + color(f" {label} ", selected + BOLD))
            else:
                lines.append("    " + color("○", DIM) + f" {label}")
        if scrolls:
            rest = len(choices) - top - visible
            lines.append(color(f"      ↓ {rest} more", DIM) if rest > 0 else "")
        out.write(("" if first else f"\x1b[{block}A") + "".join(f"\r\x1b[2K{line}\n" for line in lines))
        out.flush()

    try:
        old = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return _numbered(question, choices, default, description)
    result, drawn = -1, False
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?25l")
        draw(True)
        drawn = True
        while True:
            key = _read_key(fd)
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(choices)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(choices)
            elif key == "enter":
                result = cursor
                break
            elif key == "left":
                result = -2
                break
            elif key in ("esc", "q", "ctrl-c"):
                result = -1
                break
            elif key.isdigit() and key != "0" and int(key) <= len(choices):
                result = int(key) - 1
                break
            else:
                continue
            draw(False)
    except KeyboardInterrupt:
        result = -1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.write("\x1b[?25h")
        if drawn:
            out.write(f"\x1b[{block}A\r\x1b[J")   # the menu goes; the caller's echo takes its place
        out.flush()
    return result
