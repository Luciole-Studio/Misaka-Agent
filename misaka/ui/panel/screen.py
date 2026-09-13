"""A cell buffer for the host terminal, written as a diff.

herdr composes every frame into a ratatui ``Buffer`` and ``Terminal::draw`` writes only
the cells that changed since the previous frame. The panel does the same: every drawing
path puts cells into a ``ScreenBuffer``; ``diff()`` against the frame before yields one
write per frame, wrapped in a synchronized update. Nothing is ever cleared and repainted
on screen -- a popup that closes simply stops being composed, and the diff restores what
was under it. A spinner tick in one pane costs the bytes of that one cell.

Cells are ``(symbol, style)``; ``style`` is a tuple ``(fg, bg, bold, dim, italic,
underline, blink, reverse, strike)`` with colours as None (default), an ANSI code
(30-37, 90-97 for fg; the same values for bg), or an ``(r, g, b)`` triple. A wide
symbol owns the next cell too, which holds ``""`` with the same style.
"""
import re
import unicodedata

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_CSI_OTHER = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DEFAULT_STYLE = (None, None, False, False, False, False, False, False, False)
BLANK = (" ", DEFAULT_STYLE)


def char_width(ch):
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def style(fg=None, bg=None, bold=False, dim=False, italic=False, underline=False,
          blink=False, reverse=False, strike=False):
    return (fg, bg, bold, dim, italic, underline, blink, reverse, strike)


def apply_sgr(current, params):
    """One SGR sequence applied to a style tuple (the daemon's rows, the sidebar's rows)."""
    fg, bg, bold, dim, italic, underline, blink, reverse, strike = current
    values = [int(p) if p else 0 for p in params.split(";")] if params else [0]
    index = 0
    while index < len(values):
        value = values[index]
        if value == 0:
            fg = bg = None
            bold = dim = italic = underline = blink = reverse = strike = False
        elif value == 1:
            bold = True
        elif value == 2:
            dim = True
        elif value == 3:
            italic = True
        elif value == 4:
            underline = True
        elif value == 5:
            blink = True
        elif value == 7:
            reverse = True
        elif value == 9:
            strike = True
        elif value == 22:
            bold = dim = False
        elif value == 23:
            italic = False
        elif value == 24:
            underline = False
        elif value == 25:
            blink = False
        elif value == 27:
            reverse = False
        elif value == 29:
            strike = False
        elif 30 <= value <= 37 or 90 <= value <= 97:
            fg = value
        elif value == 39:
            fg = None
        elif 40 <= value <= 47 or 100 <= value <= 107:
            bg = value - 10
        elif value == 49:
            bg = None
        elif value in (38, 48) and index + 1 < len(values):
            kind = values[index + 1]
            if kind == 2 and index + 4 < len(values):
                colour = (values[index + 2], values[index + 3], values[index + 4])
                index += 4
            elif kind == 5 and index + 2 < len(values):
                colour = _colour_256(values[index + 2])
                index += 2
            else:
                index += 1
                continue
            if value == 38:
                fg = colour
            else:
                bg = colour
        index += 1
    return (fg, bg, bold, dim, italic, underline, blink, reverse, strike)


def _colour_256(n):
    if n < 8:
        return 30 + n
    if n < 16:
        return 90 + n - 8
    if n < 232:
        n -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    grey = 8 + (n - 232) * 10
    return (grey, grey, grey)


def sgr_for(current, wanted):
    """The SGR sequence that turns ``current`` into ``wanted``; empty when equal."""
    if current == wanted:
        return ""
    fg, bg, bold, dim, italic, underline, blink, reverse, strike = wanted
    parts = ["0"]
    for on, code in ((bold, "1"), (dim, "2"), (italic, "3"), (underline, "4"),
                     (blink, "5"), (reverse, "7"), (strike, "9")):
        if on:
            parts.append(code)
    if fg is not None:
        parts.append(f"38;2;{fg[0]};{fg[1]};{fg[2]}" if isinstance(fg, tuple) else str(fg))
    if bg is not None:
        parts.append(f"48;2;{bg[0]};{bg[1]};{bg[2]}" if isinstance(bg, tuple) else str(bg + 10))
    return "\x1b[" + ";".join(parts) + "m"


def cells_from_ansi(line, width=None):
    """A rendered row (SGR runs and text) as cells; clipped to ``width`` columns when given.
    A wide character that would straddle the edge is dropped, like the terminal would."""
    out, current, pos = [], DEFAULT_STYLE, 0
    limit = width if width is not None else float("inf")
    while pos < len(line):
        sgr = _SGR.match(line, pos)
        if sgr:
            current = apply_sgr(current, sgr.group(1))
            pos = sgr.end()
            continue
        other = _CSI_OTHER.match(line, pos)
        if other:
            pos = other.end()
            continue
        ch = line[pos]
        pos += 1
        w = char_width(ch)
        if w == 0:
            if out and ch != "\x1b":
                out[-1] = (out[-1][0] + ch, out[-1][1])   # a combining mark joins its base
            continue
        if len(out) + w > limit:
            break
        out.append((ch, current))
        if w == 2:
            out.append(("", current))
    return out


class ScreenBuffer:
    """One frame's cells. ``put`` writes; ``diff`` renders the change from another frame."""

    __slots__ = ("cells", "height", "width")

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[BLANK] * width for _ in range(height)]

    def put_cells(self, x, y, cells, clip=None):
        """Write cells from column ``x``; ``clip`` is an exclusive right edge. A wide glyph
        that would be cut by the edge is blanked rather than left as a dangling head."""
        if not 0 <= y < self.height:
            return
        row = self.cells[y]
        limit = self.width if clip is None else min(clip, self.width)
        if not cells or x >= limit:
            return
        # Overwriting half of a wide glyph leaves the other half meaningless: blank it.
        if 0 <= x < self.width and row[x][0] == "" and x > 0:
            row[x - 1] = (" ", row[x - 1][1])
        last = min(x + len(cells), limit) - 1
        if 0 <= last < self.width - 1 and row[last + 1][0] == "" and row[last][0] != "":
            row[last + 1] = (" ", row[last + 1][1])
        for index, cell in enumerate(cells):
            col = x + index
            if col < 0:
                continue
            if col >= limit:
                if cell[0] == "" and 0 <= col - 1 < limit:
                    row[col - 1] = (" ", row[col - 1][1])
                break
            row[col] = cell
        # A wide head is only ever stored with its tail beside it: a head at the edge (the
        # list ended, or the edge cut it) would be drawn two columns wide by the terminal.
        if 0 <= last < self.width and char_width(row[last][0][:1]) == 2 and (
                last + 1 >= self.width or row[last + 1][0] != ""):
            row[last] = (" ", row[last][1])

    def put_ansi(self, x, y, line, clip=None):
        """Write a rendered row (SGR runs and text) starting at column ``x``."""
        width = None if clip is None else max(0, min(clip, self.width) - x)
        self.put_cells(x, y, cells_from_ansi(line, width), clip)

    def put_text(self, x, y, text, style_=DEFAULT_STYLE, clip=None):
        """Write plain text in one style."""
        width = None if clip is None else max(0, min(clip, self.width) - x)
        self.put_cells(x, y, [(symbol, style_) for symbol, _s in cells_from_ansi(text, width)], clip)

    def fill(self, rect, cell=BLANK):
        for y in range(max(0, rect.y), min(self.height, rect.y + rect.height)):
            row = self.cells[y]
            for x in range(max(0, rect.x), min(self.width, rect.x + rect.width)):
                row[x] = cell

    def restyle(self, x, y, count, transform):
        """Apply ``transform(style) -> style`` to ``count`` cells (selection, cursor)."""
        if not 0 <= y < self.height:
            return
        row = self.cells[y]
        for col in range(max(0, x), min(self.width, x + count)):
            symbol, current = row[col]
            row[col] = (symbol, transform(current))

    def get(self, x, y):
        return self.cells[y][x]

    def diff(self, previous):
        """The bytes that turn ``previous`` (None = unknown) into this frame."""
        out = []
        current_style = None
        for y, row in enumerate(self.cells):
            old = previous.cells[y] if previous is not None and y < previous.height else None
            x = 0
            while x < self.width:
                cell = row[x]
                if old is not None and x < len(old) and old[x] == cell:
                    x += 1
                    continue
                # a changed continuation cell means its wide head must be rewritten
                start = x
                if cell[0] == "" and x > 0:
                    start = x - 1
                out.append(f"\x1b[{y + 1};{start + 1}H")
                if start != x:
                    head = row[start]
                    if head[1] != current_style:
                        out.append(sgr_for(current_style or DEFAULT_STYLE, head[1]))
                        current_style = head[1]
                    out.append(head[0])
                    x += 1
                    continue_run = True
                else:
                    continue_run = False
                while x < self.width:
                    cell = row[x]
                    if old is not None and x < len(old) and old[x] == cell and cell[0] != "" and not continue_run:
                        break
                    continue_run = False
                    if cell[0] == "":
                        x += 1
                        continue
                    if cell[1] != current_style:
                        out.append(sgr_for(current_style or DEFAULT_STYLE, cell[1]) if current_style is not None
                                   else sgr_for(None, cell[1]))
                        current_style = cell[1]
                    out.append(cell[0])
                    x += 1
        if not out:
            return b""
        return ("\x1b[?2026h" + "".join(out) + "\x1b[0m\x1b[?2026l").encode()
