"""Terminal primitives for ``misaka setup``: a curses radio list, line prompts, headers.

The shape follows Hermes's setup wizard: a single-select menu driven by the arrow keys, Enter
to choose, Esc to cancel the whole wizard, Left to return to the previous section, digits to
jump to an entry; yes/no questions go through the same menu so every choice looks and feels
the same. Without a terminal (a pipe, a container) every menu falls back to a numbered line
prompt, so the wizard is never stuck waiting for a key nobody can press.
"""
from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable


class SetupCancelled(Exception):
    """Esc or Ctrl+C: the wizard stops and leaves the remaining sections untouched."""


class SetupGoBack(Exception):
    """Left arrow at a menu: the wizard returns to the previous section."""


BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _tty() else text


def print_banner(*lines: str) -> None:
    width = max(len(line) for line in lines) + 4
    print()
    print(color("┌" + "─" * (width - 2) + "┐", CYAN))
    for line in lines:
        print(color("│ " + line.ljust(width - 4) + " │", CYAN))
    print(color("└" + "─" * (width - 2) + "┘", CYAN))


def print_header(title: str) -> None:
    print()
    print(color(f"── {title} ", BOLD) + color("─" * max(0, 60 - len(title) - 4), DIM))


def print_info(*lines: str) -> None:
    for line in lines:
        print(f"  {line}")


def print_success(text: str) -> None:
    print(color(f"  ✓ {text}", GREEN))


def print_warning(text: str) -> None:
    print(color(f"  ! {text}", YELLOW))


def print_error(text: str) -> None:
    print(color(f"  ✗ {text}", RED))


def print_check(ok: bool | None, label: str, detail: str = "") -> None:
    """One row of a check table: ✓ present, ✗ missing, – optional and absent."""
    mark = "✓" if ok else "–" if ok is None else "✗"
    code = GREEN if ok else DIM if ok is None else RED
    print(color(f"  {mark} {label:<22}", code) + (color(detail, DIM) if detail else ""))


def prompt(question: str, default: str | None = None, *, password: bool = False) -> str:
    """A line prompt. Empty input returns ``default``; Ctrl+C or a closed stdin cancels."""
    suffix = f" [{default}]" if default else ""
    text = f"  {question}{suffix}: "
    try:
        value = getpass.getpass(text) if password else input(text)
    except (KeyboardInterrupt, EOFError):
        print()
        raise SetupCancelled from None
    value = value.strip()
    return value or (default or "")


def prompt_choice(question: str, choices: list[str], default: int = 0, description: str | None = None) -> int:
    """Single-select menu. Returns the chosen index; Esc raises SetupCancelled, Left raises SetupGoBack."""
    index = _radiolist(question, choices, default, description) if _curses_usable() else _numbered(question, choices, default, description)
    if index == -1:
        raise SetupCancelled
    if index == -2:
        raise SetupGoBack
    print(f"  {question} {color('→ ' + choices[index], DIM)}")
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

def _curses_usable() -> bool:
    if not _tty() or os.environ.get("MISAKA_SETUP_PLAIN"):
        return False
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def _numbered(question: str, choices: list[str], default: int, description: str | None) -> int:
    print(f"\n  {question}")
    if description:
        print(color(f"  {description}", DIM))
    for number, choice in enumerate(choices, 1):
        marker = "●" if number - 1 == default else "○"
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


def _radiolist(question: str, choices: list[str], default: int, description: str | None) -> int:
    import curses

    def run(screen) -> int:
        curses.curs_set(0)
        screen.keypad(True)
        cursor, top = default, 0
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            row = 0
            _put(screen, row, 0, question, width, curses.A_BOLD)
            row += 1
            if description:
                _put(screen, row, 0, description, width, curses.A_DIM)
                row += 1
            _put(screen, row, 0, "↑↓ move · Enter select · ← back · Esc cancel · 1-9 jump", width, curses.A_DIM)
            row += 2
            visible = max(1, height - row - 1)
            if cursor < top:
                top = cursor
            elif cursor >= top + visible:
                top = cursor - visible + 1
            for offset, choice in enumerate(choices[top:top + visible]):
                index = top + offset
                marker = "●" if index == cursor else "○"
                attr = curses.A_REVERSE if index == cursor else curses.A_NORMAL
                _put(screen, row + offset, 0, f"  {marker} {choice}", width, attr)
            screen.refresh()
            key = screen.getch()
            if key in (curses.KEY_UP, ord("k")):
                cursor = (cursor - 1) % len(choices)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = (cursor + 1) % len(choices)
            elif key in (curses.KEY_ENTER, 10, 13):
                return cursor
            elif key == curses.KEY_LEFT:
                return -2
            elif key in (27, 3, ord("q")):           # Esc, Ctrl+C, q
                if key == 27:
                    screen.nodelay(True)
                    following = screen.getch()          # a bare Esc, not the head of an arrow sequence
                    screen.nodelay(False)
                    if following != -1:
                        continue
                return -1
            elif ord("1") <= key <= ord("9") and key - ord("1") < len(choices):
                return key - ord("1")
            elif key == curses.KEY_RESIZE:
                continue

    try:
        return curses.wrapper(run)
    except (curses.error, OSError):
        return _numbered(question, choices, default, description)


def _put(screen, y: int, x: int, text: str, width: int, attr) -> None:
    try:
        screen.addnstr(y, x, text, max(1, width - x - 1), attr)
    except Exception:  # noqa: BLE001, S110 - a write at the last cell raises; the row is drawn anyway
        pass
