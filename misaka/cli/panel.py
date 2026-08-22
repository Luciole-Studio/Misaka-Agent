"""Multi-pane panel: MISAKA's default entry point (herdr-style; geometry from herdr_ui).

Rendering: the daemon keeps a terminal emulator per pane; the panel subscribes
to dirty rows from every pane and lays them out itself: a sidebar on the left
(Sister roster plus the Windows/Projects sections), a tab row at the top of the
main area, and a per-tab BSP split tree (splitting only the focused pane; each
pane gets a border and joints are merged). Colors come from the engine's
built-in dark/light theme, adapted to the terminal background; only herdr's
color roles are borrowed.

Controls: click a tab, sidebar entry or pane to focus it. Prefix key ctrl+b,
then: 1-9 switch tab | n/p cycle tabs | hjkl move focus | c new tab |
v / - split side by side / stacked | z zoom | t summon the global tree |
x close pane | d detach | ? key help | ctrl+b again sends a literal ctrl+b.
"""
import base64
import fcntl
import json
import math
import os
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
import unicodedata

from misaka.cli import herdr_ui as hui
from misaka.config import sisters as _sisters_roster
from misaka.net import client as net

def _prefix_key():
    """Prefix key, default ctrl+b. Inside tmux that key is taken, so set
    MISAKA_PANEL_PREFIX=ctrl+g (or similar) to change it."""
    name = os.environ.get("MISAKA_PANEL_PREFIX", "ctrl+b").lower().strip()
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return bytes([ord(name[5]) - 96])
    return b"\x02"


PREFIX = _prefix_key()
POLL_SECONDS = 2.0
SIDEBAR_W = 24            # Sidebar width in columns; the divider sits at SIDEBAR_W+1 and the main area starts one column later.
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_MOUSE_X10 = re.compile(rb"\x1b\[M[\x20-\xff]{3}")   # Legacy X10 mouse reports (some terminals lack SGR mode).
# Word-break characters for double-click selection (herdr's embedded set) plus whitespace.
_WORD_BREAK = set(" \t" + "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~")

_P = hui.PALETTE
# Windows section: a pulsing dot on the left while a card is actually running,
# a status word on the right.
BREATH_SECONDS = 1.6             # One full pulse (Claude Code-style fade).
BREATH_FPS = 0.12                # Redraw interval for the fade.
# Only terminal and waiting states get a word; "running" is the pulsing dot alone.
_STATUS_WORD = {
    "review": ("review", hui.sgr_fg(_P["plum"])),
    "verifying": ("verify", hui.sgr_fg(_P["plum"])),
    "finalizing": ("verify", hui.sgr_fg(_P["plum"])),
    "ready": ("ready", hui.sgr_fg(hui.OVERLAY0)),
    "failed": ("failed", hui.sgr_fg(_P["red"])),
    "stopped": ("stopped", hui.sgr_fg(_P["plum"])),
    "done": ("done", hui.sgr_fg(hui.OVERLAY0)),
}
_UNSEEN_WORD = {                 # Finished but not yet viewed: bright (herdr's "done, nobody looked").
    "done": ("done", hui.sgr_fg(_P["green"])),
    "failed": ("failed", hui.sgr_fg(_P["red"])),
    "stopped": ("stopped", hui.sgr_fg(_P["plum"])),
}


def breath_level(now, period=BREATH_SECONDS):
    """Breathing phase in 0..1 (sine, so it fades rather than blinks). Pure, so testable."""
    return (math.sin(2 * math.pi * (now % period) / period) + 1) / 2


# Pulse range for the running indicator: dark and bright versions of the theme green.
DOT_DIM = tuple(round(c * 0.42) for c in _P["green"])
DOT_BRIGHT = tuple(min(255, round(c * 1.28)) for c in _P["green"])


def busy_dot(busy, level, dim=None, bright=None):
    """Running indicator: a green dot fading between dim and bright while busy, a space otherwise.
    ``level`` comes from breath_level(). The result carries no reset sequence on purpose;
    one would wipe the selection background."""
    if not busy:
        return " "
    color = hui.blend(dim or DOT_DIM, bright or DOT_BRIGHT, level)
    return f"{hui.sgr_fg(color)}●"


_wcwidth = hui.display_width


# Random quips shown when someone opens the panel inside a pane (herdr main.rs:13-20, MISAKA edition).
NESTED_MESSAGES = [
    "A panel inside a panel would recurse forever. Nice try.",
    "Nested panel detected; keeping the current network intact.",
    "This pane is already managed by the MISAKA panel.",
    "The network declined to open another panel inside this one.",
    "Recursive panels make terrible roommates.",
    "One panel is enough for this pane.",
]


def next_tile_placement(tree):
    """MISAKA's default tiling rule (not from herdr): at most two panes per column,
    stacking first, then opening a new column on the right once a column is full.
    Returns ``(pane_to_split, direction)``; ``None`` means wrap the whole tree (new column). Pure, so testable.

        1 [A]   2 [A]   3 [A][C]   4 [A][C]   5 [A][C][E]
                  [B]     [B]        [B][D]     [B][D]
    """
    ids = hui.pane_ids(tree)
    if len(ids) % 2 == 0:                      # Every column holds two: open a new column on the right.
        return None, "h"
    placed = hui.collect_panes(tree, hui.Rect(0, 0, 1000, 1000), ids[0])
    rightmost = max(placed, key=lambda item: (item[1].x, -item[1].y))
    return rightmost[0], "v"                   # Stack inside the rightmost column.


def render_tab_bar(tab_names, active_index, view, area, tab_scroll=0):
    """Port of herdr tabs.rs:319-470 render_tab_bar. The row is filled with panel_bg;
    scroll buttons " < " / " > " (overlay0 + dim when they cannot scroll); tab labels
    centered, the active one on accent with a contrasting foreground, the rest overlay1
    on surface0; a " + " button (overlay1, no background); an ellipsis on either side when
    tabs are cut off. Returns the whole row as text. Pure, so testable.
    (herdr's is_auto_named is ignored: MISAKA tab names are always agent names.)"""
    width = max(0, area.width)
    if width == 0:
        return ""
    base = hui.sgr_bg(hui.PALETTE["panel_bg"])
    canvas = [(" ", base) for _ in range(width)]

    def put(x, text, style):
        col = x - area.x
        for ch in text:
            if 0 <= col < width:
                canvas[col] = (ch, style)
            col += 1
            if _wcwidth(ch) == 2:            # A wide character also consumes the next cell.
                if 0 <= col < width:
                    canvas[col] = ("", style)
                col += 1

    visible = [i for i, r in enumerate(view.tab_hit_areas) if r.width > 0]
    first_visible = visible[0] if visible else None
    last_visible = visible[-1] if visible else None
    can_left = view.scroll_left_hit_area.width > 0 and tab_scroll > 0
    can_right = (view.scroll_right_hit_area.width > 0 and last_visible is not None
                 and last_visible + 1 < len(tab_names))

    enabled = f"{hui.sgr_fg(hui.OVERLAY1)}{hui.sgr_bg(hui.SURFACE0)}"
    disabled = f"{hui.sgr_fg(hui.OVERLAY0)}{hui.sgr_bg(hui.SURFACE0)}\x1b[2m"
    if view.scroll_left_hit_area.width:
        put(view.scroll_left_hit_area.x, " < ", enabled if can_left else disabled)
    if view.scroll_right_hit_area.width:
        put(view.scroll_right_hit_area.x, " > ", enabled if can_right else disabled)

    for index, rect in enumerate(view.tab_hit_areas):
        if rect.width == 0 or index >= len(tab_names):
            continue
        style = (f"{hui.sgr_bg(hui.ACCENT)}{hui.sgr_fg(hui.panel_contrast_fg())}\x1b[1m"
                 if index == active_index
                 else f"{hui.sgr_bg(hui.SURFACE0)}{hui.sgr_fg(hui.OVERLAY1)}")
        name = hui.tab_chrome_label(tab_names, index)
        padding = max(0, rect.width - hui.display_width(name))
        left = padding // 2
        put(rect.x, " " * left + name + " " * (padding - left), style)

    if view.new_tab_hit_area.width:          # The plus button: foreground only, no background.
        put(view.new_tab_hit_area.x, " + ", f"{hui.sgr_fg(hui.OVERLAY1)}{base}")

    if first_visible is not None and first_visible > 0:      # Tabs cut off on the left.
        x = (view.scroll_left_hit_area.x + view.scroll_left_hit_area.width
             if view.scroll_left_hit_area.width else area.x)
        put(x, "…", f"{hui.sgr_fg(hui.OVERLAY0)}{base}")
    if last_visible is not None and last_visible + 1 < len(tab_names):
        x = (view.scroll_right_hit_area.x - 1 if view.scroll_right_hit_area.width
             else area.x + width - 1)
        put(x, "…", f"{hui.sgr_fg(hui.OVERLAY0)}{base}")

    out, last_style = [], None
    for ch, style in canvas:
        if ch == "":
            continue
        if style != last_style:
            out.append("\x1b[0m" + style)
            last_style = style
        out.append(ch)
    out.append("\x1b[0m")
    return "".join(out)


_CARD_GLYPH = {          # Status glyphs for card rows under an expanded project: done in theme rose, muted for unfinished.
    "running": ("●", "green"), "review": ("◇", "plum"),
    "verifying": ("◆", "plum"), "finalizing": ("◆", "plum"),
    "ready": ("○", "overlay"), "done": ("✓", "accent"),
    "failed": ("✗", "red"), "stopped": ("■", "plum"),
}


def _row(body, body_w, selected):
    """Finish one sidebar row: one blank column on each side; a selected row is covered
    in surface0 (herdr sidebar.rs:791), re-applied after every reset so the block is not broken."""
    body += " " * max(0, (SIDEBAR_W - 2) - body_w)
    if selected:
        bg = hui.sgr_bg(hui.SURFACE0)
        body = bg + body.replace("\x1b[0m", "\x1b[0m" + bg) + "\x1b[0m"
    else:
        body += "\x1b[0m"
    return " " + body + " "


def _alloc_sections(rows, wants):
    """Split the sidebar height across the three sections (one divider row between each):
    the smallest requests are satisfied first, then leftovers go to the rest in order.
    Not from herdr (it has only two sections, sidebar.rs:42). Pure, so testable."""
    avail = max(0, rows - (len(wants) - 1))
    alloc = [0] * len(wants)
    left = avail
    order = sorted(range(len(wants)), key=lambda i: wants[i])
    for pos, i in enumerate(order):
        share = left // (len(order) - pos)
        alloc[i] = min(wants[i], share)
        left -= alloc[i]
    for i in range(len(wants)):
        if left <= 0:
            break
        extra = min(left, wants[i] - alloc[i])
        alloc[i] += extra
        left -= extra
    return alloc


def format_sidebar(panes, focused_id, rows, roster=(), projects=(),
                   expanded=(), scrolls=None, level=1.0, selected=None):
    """The three sidebar sections: Sisters roster | Projects (a project is a folder; click to
    expand its cards) | Windows (pane list).
    A section taller than its slot gets a thin scrollbar in its last column.
    Returns ``(lines, targets, spans)``; ``targets`` parallels ``lines``:
    None | ("roster", name) | ("pane", id) | ("proj", project_id) | ("card", card_id). Pure, so testable."""
    scrolls = scrolls or {}
    heading = f"{hui.sgr_fg(hui.OVERLAY1)}\x1b[1m"

    def pad(text, visible):
        return text + " " * max(0, SIDEBAR_W - visible)

    def live_pane_for(name):
        title = "Last Order" if name == "last-order" else name
        for pane in panes:
            if pane["alive"] and (pane["title"] == title
                                  or pane["title"].startswith(title + "·")):
                return pane["id"]
        return None

    sisters = []
    for name in roster:
        live = live_pane_for(name)
        dot = (f"{hui.sgr_fg(_P['plum'])}●" if live
               else f"{hui.sgr_fg(hui.OVERLAY0)}○")
        shown, shown_w = _cut("Last Order" if name == "last-order" else name,
                              SIDEBAR_W - 5)
        sisters.append((pad(f" {dot}\x1b[0m {shown}", 3 + shown_w),
                        ("pane", live) if live else ("roster", name)))
    # Allies: third-party agents running inside MISAKA. Marked with a star and listed
    # only while the process is alive; they are never written to the roster file.
    for pane in panes:
        if not pane["alive"] or not pane.get("ally"):
            continue
        star = (f"{hui.sgr_fg(hui.ACCENT)}★" if pane.get("busy")
                else f"{hui.sgr_fg(_P['plum'])}☆")
        shown, shown_w = _cut(pane["ally"], SIDEBAR_W - 5)
        sisters.append((pad(f" {star}\x1b[0m {shown}", 3 + shown_w),
                        ("pane", pane["id"])))

    projs = []
    for proj in projects:
        project_id = proj.get("id")
        name = proj["name"]
        shown = name if name is not None else '(unclassified)'
        arrow = "▾" if (project_id in expanded) else "▸"
        live_n = sum(1 for c in proj["cards"]
                     if c["status"] in ("ready", "running", "review", "verifying", "finalizing"))
        label = str(live_n) if live_n else ""
        inner = SIDEBAR_W - 2
        shown, shown_w = _cut(shown, max(4, inner - 5 - _wcwidth(label)))
        left = f"{arrow} {shown}"
        left_w = 2 + shown_w
        gap = max(1, inner - left_w - _wcwidth(label))
        projs.append((_row(f"{left}{' ' * gap}{hui.sgr_fg(hui.OVERLAY0)}{label}\x1b[0m",
                           left_w + gap + _wcwidth(label),
                           selected == ("proj", project_id)),
                      ("proj", project_id)))
        if project_id in expanded:
            for card in proj["cards"]:
                glyph, tone_key = _CARD_GLYPH.get(card["status"], ("?", "overlay"))
                color = (hui.sgr_fg(hui.OVERLAY0) if tone_key == "overlay"
                         else hui.sgr_fg(_P[tone_key]))
                title, title_w = _cut(card["title"], SIDEBAR_W - 7)
                projs.append((_row(f"  {color}{glyph}\x1b[0m {title}", 4 + title_w,
                                   selected == ("card", card["id"])),
                              ("card", card["id"])))

    windows = []
    for index, pane in enumerate(panes, 1):
        status = pane.get("status") or ""
        busy = pane.get("busy", False)
        if not pane["alive"]:
            glyph, (label, color) = " ", ("exited", hui.sgr_fg(_P["red"]))
        else:
            glyph = busy_dot(busy, level)     # The left column only says "running right now".
            label, color = "", ""
            if pane["card"]:
                if pane.get("unseen"):
                    label, color = _UNSEEN_WORD.get(status, ("", ""))
                elif status != "running":
                    label, color = _STATUS_WORD.get(status, ("", ""))
        mail = (f"{hui.sgr_fg(_P['red'])}✉{pane['mail']}\x1b[0m"
                if pane.get("mail") else "")
        mail_w = (1 + len(str(pane["mail"]))) if pane.get("mail") else 0
        inner_w = SIDEBAR_W - 2               # One blank column on each side.
        name, name_w = _cut(pane["title"] or pane["id"], inner_w - 9 - mail_w)
        left = f"{index}. {glyph} {name}{mail}"
        left_w = len(f"{index}. ") + 1 + 1 + name_w + mail_w
        gap = max(1, inner_w - left_w - len(label))
        windows.append((_row(left + " " * gap + f"{color}{label}",
                             left_w + gap + len(label),
                             pane["id"] == focused_id),
                        ("pane", pane["id"])))

    sections = [("sisters", "Sisters", sisters),
                ("windows", "Windows", windows),
                ("projects", "Projects", projs)]
    alloc = _alloc_sections(rows, [1 + len(body) for _k, _t, body in sections])
    lines, targets, spans = [], [], []
    for idx, (key, title, body) in enumerate(sections):
        if idx:
            lines.append(hui.sgr_fg(hui.OVERLAY0) + "─" * SIDEBAR_W + "\x1b[0m")
            targets.append(None)
        height = alloc[idx]
        if height <= 0:
            spans.append({"key": key, "y0": len(lines) + 1, "height": 0,
                          "scroll": 0, "total": len(body)})
            continue
        lines.append(pad(f" {heading}{title}\x1b[0m", 1 + len(title)))
        targets.append(None)
        vis = height - 1
        total = len(body)
        scroll = max(0, min(scrolls.get(key, 0), max(0, total - vis)))
        spans.append({"key": key, "y0": len(lines) + 1, "height": vis,
                      "scroll": scroll, "total": total})
        window = body[scroll: scroll + vis]
        for line, target in window:
            lines.append(line)
            targets.append(target)
        for _ in range(vis - len(window)):
            lines.append(" " * SIDEBAR_W)
            targets.append(None)
        if total > vis and vis > 0:          # Overflow: thin track plus thumb in the last column.
            metrics = {"offset_from_bottom": total - vis - scroll,
                       "max_offset_from_bottom": total - vis, "viewport_rows": vis}
            thumb = hui.scrollbar_thumb(metrics, hui.Rect(0, 0, 1, vis))
            base = len(lines) - vis
            for j in range(vis):
                on_thumb = thumb and thumb[0] <= j < thumb[0] + thumb[1]
                glyph = (hui.sgr_fg(hui.OVERLAY1) if on_thumb
                         else hui.sgr_fg(hui.PALETTE["surface1"])) + "▕\x1b[0m"
                lines[base + j] = lines[base + j][:-1] + glyph
    while len(lines) < rows:
        lines.append(" " * SIDEBAR_W)
        targets.append(None)
    # Final clamp: no row may be wider than the sidebar, even if something upstream skipped _cut.
    lines = [_clamp_row(line, SIDEBAR_W) for line in lines]
    return lines[:rows], targets[:rows], spans


def draw_scrollbar(metrics, track, focused):
    """herdr scrollbar.rs:135-162 render_scrollbar: track '▕' plus thumb, brighter and
    thicker when focused. Returns an escape-sequence string. Pure, so testable."""
    thumb = hui.scrollbar_thumb(metrics, track)
    if thumb is None:
        return ""
    thumb_top, thumb_len = thumb
    track_color, thumb_color, thumb_symbol = hui.scrollbar_style(focused)
    out = [hui.sgr_fg(track_color)]
    for y in range(track.y, track.y + track.height):
        out.append(f"\x1b[{y + 1};{track.x + 1}H▕")
    out.append(hui.sgr_fg(thumb_color))
    for y in range(thumb_top, min(thumb_top + thumb_len, track.y + track.height)):
        out.append(f"\x1b[{y + 1};{track.x + 1}H{thumb_symbol}")
    out.append("\x1b[0m")
    return "".join(out)


def draw_pane_borders(chromed, splits=(), area=None):
    """Draw every pane border (output side of herdr panes.rs:484-528 render_pane_borders):
    focused edges in accent, the rest in overlay0. Called after all pane content has been
    rendered, as in herdr. Pure, so testable."""
    cells = hui.pane_border_cells(chromed, splits=splits, area=area)
    out, last_color = [], None
    for (x, y), (symbol, focused) in sorted(cells.items(), key=lambda kv: kv[0][::-1]):
        color = hui.ACCENT if focused else hui.OVERLAY0
        if color != last_color:
            out.append(hui.sgr_fg(color))
            last_color = color
        out.append(f"\x1b[{y + 1};{x + 1}H{symbol}")
    if out:
        out.append("\x1b[0m")
    return "".join(out)


def format_prefix_bar(width, prefix_name="ctrl+b"):
    """herdr's prefix-mode bottom bar (src/ui/menus.rs render_prefix_overlay): a highlighted
    PREFIX badge followed by key hints, filling the whole bottom row. Pure, so testable."""
    key = f"{hui.sgr_fg(hui.ACCENT)}\x1b[1m"      # herdr menus.rs: key names in accent, bold.
    dim = hui.sgr_fg(hui.OVERLAY1)
    badge = f"{hui.sgr_bg(hui.ACCENT)}{hui.sgr_fg(hui.panel_contrast_fg())}\x1b[1m"
    parts = [(f"{badge} PREFIX \x1b[0m", 8)]
    for name, desc in ((f"{prefix_name}", "Send literally"), ("1-9", "Tab"),
                       ("hjkl", "Focus"), ("c", "New tab"), ("v/-", "Split"),
                       ("z", "Zoom"), ("t", "Tree"), ("x", "Close pane"),
                       ("d", "Detach"), ("?", "Help"), ("esc", "Cancel")):
        parts.append((f" {key}{name}\x1b[0m{dim} {desc}\x1b[0m",
                      1 + len(name) + 1 + _wcwidth(desc)))
    out, used = [], 0
    for text, visible in parts:            # Drop hints that do not fit; never overflow the row.
        if used + visible > width:
            break
        out.append(text)
        used += visible
    return "".join(out) + " " * max(0, width - used)


TREE_TITLE = "Tree"
TREE_ARGV = [sys.executable, "-m", "misaka", "trace", "--watch"]


def tree_summon_action(listing):
    """Decide what prefix+t does: focus a live tree pane if there is one, else open a new one. Pure, so testable."""
    existing = next((p["id"] for p in listing
                     if p["alive"] and p["title"] == TREE_TITLE), None)
    return ("focus", existing) if existing else ("create", TREE_ARGV)


_HELP_ROWS = [
    ("1-9", "Switch to tab N"), ("n / p", "Next / previous tab"),
    ("h j k l", "Focus left / down / up / right"), ("c", "New tab"),
    ("v", "Split side by side"), ("-", "Split top and bottom"),
    ("z", "Zoom: focused pane fills the tab"), ("t", "Summon the global tree"),
    ("x", "Close the focused pane"),
    ("d", "Detach (panes keep running)"), ("ctrl+b", "Send a literal ctrl+b"),
    ("esc", "Leave prefix mode"), ("Mouse", "Click focus, drag copy, 2× word"),
    ("", "Any key closes this help"),
]


def _cut(text, width):
    """Truncate to a display width (CJK counts as 2 columns). Returns ``(text, width)``.
    Every variable sidebar string must go through this: ``text[:n]`` slices by character,
    so 17 CJK characters are 34 columns and spill through the border into the main area."""
    out, used = "", 0
    for ch in text:
        w = _wcwidth(ch)
        if used + w > width:
            break
        out += ch
        used += w
    return out, used


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _cols(plain, c0, c1):
    """Substring by display-column range [c0, c1) (CJK is 2 columns; a wide character straddling
    the boundary is included). Selections are dragged in screen columns, not character indexes."""
    out, col = "", 0
    for ch in plain:
        if col >= c1:
            break
        w = _wcwidth(ch)
        if col + w > c0:
            out += ch
        col += w
    return out


def _clipboard(text):
    """Copy to the system clipboard: pbcopy on macOS (what herdr does), otherwise OSC 52 and let the terminal handle it."""
    import subprocess
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    _write_all(b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")


def _clamp_row(line, width):
    """ANSI-aware hard clamp of one row: truncate past ``width``, pad with spaces if short.
    Last line of defense at the format_sidebar exit, so a missed _cut cannot break the border."""
    out, used, i = [], 0, 0
    while i < len(line):
        m = _ANSI.match(line, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = _wcwidth(line[i])
        if used + w > width:
            break
        out.append(line[i])
        used += w
        i += 1
    return "".join(out) + "\x1b[0m" + " " * (width - used)


def format_card_menu(card_id, width=46):
    """Card context menu: delete only (destructive). Same layout as the key help. Pure, so testable."""
    rows = [("d", "Delete card + history (cannot undo)"), ("", "Any other key closes")]
    inner = width - 2
    title, tw = _cut(f"─ Card: {card_id} ", inner)
    lines = ["┌" + title + "─" * (inner - tw) + "┐"]
    for key, desc in rows:
        body, bw = _cut(f"  {key}" + " " * max(1, 4 - len(key)) + desc, inner)
        lines.append("│" + body + " " * (inner - bw) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def format_help_lines(width=46):
    """Key help box (a short form of herdr's keybind help): columns padded by display width;
    rows are exactly ``width`` wide and truncate on narrow screens. Pure, so testable."""
    inner = width - 2
    lines = ["┌─ Keys " + "─" * max(0, inner - _wcwidth("─ Keys ")) + "┐"]
    for key, desc in _HELP_ROWS:
        body, bw = _cut(f"  {key}" + " " * max(1, 9 - _wcwidth(key)) + desc, inner)
        lines.append("│" + body + " " * (inner - bw) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


class _Sock:
    """Minimal JSONL client over the daemon's Unix socket (one for requests, one for the event stream)."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.sock.settimeout(15)   # If the daemon hangs, fail with a "disconnected" error instead of freezing the keyboard.
        self.buf = b""

    def send(self, method, params=None):
        self.sock.sendall((json.dumps(
            {"id": "1", "method": method, "params": params or {}},
            ensure_ascii=False) + "\n").encode())

    def request(self, method, params=None):
        self.send(method, params)
        while True:
            line = self.readline(block=True)
            if line is None:
                raise ConnectionError("Daemon connection closed.")
            msg = json.loads(line)
            if "event" in msg:
                continue
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            return msg["result"]

    def readline(self, block=False):
        while b"\n" not in self.buf:
            if not block:
                readable, _, _ = select.select([self.sock], [], [], 0)
                if not readable:
                    return None
            chunk = self.sock.recv(262144)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line


def _write_all(data):
    """Write everything to stdout. os.write to a terminal may write only part of the buffer,
    cutting an escape sequence in half; the terminal then prints the tail as text
    (that is where stray '[' characters come from)."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(1, view)
        except BlockingIOError:
            select.select([], [1], [])
            continue
        view = view[written:]


def _term_size():
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ,
                                                       b"\0" * 8))[:2]
        return (rows or 24), (cols or 80)
    except OSError:
        return 24, 80


def launch():
    # Nesting guard, as in herdr main.rs:469-503: every pane process carries MISAKA_NET_PANE,
    # and opening the panel inside a pane would recurse forever. MISAKA_ALLOW_NESTED=1 is the
    # escape hatch (herdr's experimental.allow_nested).
    if os.environ.get("MISAKA_NET_PANE") and os.environ.get("MISAKA_ALLOW_NESTED") != "1":
        sys.exit(
            "\x1b[1mError:\x1b[0m already inside a pane; panels cannot be nested.\n"
            "Chat with Last Order: misaka chat\n"
            "Chat with a Sister:   misaka chat --as 10032\n"
            "Force it anyway:      MISAKA_ALLOW_NESTED=1 misaka"
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("The panel needs a terminal. From scripts, use `misaka chat` or `misaka net`.")
    net.ensure()
    sock_path = os.path.expanduser(net.CFG["net_sock"])
    control, stream = _Sock(sock_path), _Sock(sock_path)

    def panes():
        return control.request("panes.list")["panes"]

    listing = panes()
    for pane in listing:      # Clean up dead Last Order panes first.
        if pane["title"] == "Last Order" and not pane["alive"]:
            control.request("pane.close", {"id": pane["id"]})
    listing = panes()
    workspace = os.path.realpath(os.getcwd())
    lo = next((p for p in listing
               if p["title"] == "Last Order" and p["alive"]
               and os.path.realpath(p["cwd"]) == workspace), None)
    if lo is None:
        created = control.request("pane.create", {
            "argv": [sys.executable, "-m", "misaka", "chat"],
            "cwd": os.getcwd(), "title": "Last Order",
            "env": {"MISAKA_THEME": hui.theme_variant()}})
        listing = panes()
        lo = next(p for p in listing if p["id"] == created["pane_id"])
    focused = lo["id"]
    zoom = False
    # Cursor state is kept per pane: position and visibility belong to each pane (the engine
    # hides the real cursor and draws its own block; a shell shows it). With one global value,
    # any path that forgot to resync on pane switch would stack the real cursor on top of the
    # engine's fake block, which suddenly looks brighter. (Bitten twice: IME input, click-to-focus.)
    pane_cursor = {}          # pane id -> {"at": [col, row], "hidden": bool}
    slices = []
    # herdr semantics: a tab is a page holding a BSP split tree (layout.rs port; nodes in herdr_ui).
    tabs = []            # One tree per tab; by default every pane gets its own tab.

    def sync_tabs():
        alive = [p["id"] for p in listing if p["alive"]]
        known = {pid for tree in tabs for pid in hui.pane_ids(tree)}
        before = list(tabs)
        for index, tree in enumerate(tabs):
            for pid in hui.pane_ids(tree):
                if pid not in alive:
                    tree = hui.remove_pane(tree, pid)
                    if tree is None:
                        break
            tabs[index] = tree
        tabs[:] = [tree for tree in tabs if tree is not None]
        for pid in alive:
            if pid not in known:
                tabs.append(("pane", pid))
        if tabs != before:
            save_layout()          # The daemon stores the layout, so splits survive a panel restart.

    def save_layout():
        try:
            control.request("layout.set",
                            {"layout": [hui.to_jsonable(t) for t in tabs]})
        except (RuntimeError, ConnectionError):
            pass                   # Saving is best effort; the next change retries.

    def load_layout():
        """Fetch the layout back from the daemon so splits survive a panel restart or a new terminal."""
        try:
            saved = control.request("layout.get")["layout"]
        except (RuntimeError, ConnectionError, KeyError):
            return
        alive = {p["id"] for p in listing if p["alive"]}
        for item in saved or []:
            tree = hui.from_jsonable(item)
            if tree is None:
                continue
            for pid in hui.pane_ids(tree):     # Drop panes that no longer exist.
                if pid not in alive:
                    tree = hui.remove_pane(tree, pid)
                    if tree is None:
                        break
            if tree is not None:
                tabs.append(tree)

    def active_tab():
        return next((i for i, tree in enumerate(tabs)
                     if focused in hui.pane_ids(tree)), 0)

    def tab_label(tree):
        ids = hui.pane_ids(tree)
        first = next((p for p in listing if p["id"] == ids[0]), None)
        name = (first or {}).get("title") or (ids[0] if ids else "?")
        return name + (f" +{len(ids) - 1}" if len(ids) > 1 else "")


    rows, cols = _term_size()
    main_col = SIDEBAR_W + 2      # First column of the main area (herdr: flush against the sidebar border).
    tab_scroll, tab_follow = 0, True   # herdr tab_scroll / tab_scroll_follow_active
    resized = {"hit": True}
    signal.signal(signal.SIGWINCH, lambda *_a: resized.update(hit=True))

    def slice_of(pane_id):
        return next((s for s in slices if s[0] == pane_id), None)

    def cursor_tail():
        """Where the cursor goes once this frame is drawn; appended to the frame so both land in one write.

        Position follows the application in the focused pane (IME composition and candidate
        windows attach to the real terminal cursor). Visibility follows too (herdr
        host_cursor="auto"): the engine draws its own cursor block and hides the real one,
        so showing ours as well would stack into one brighter block."""
        sl = slice_of(focused)
        state = pane_cursor.get(focused)
        if sl is None or state is None:
            return b""
        if (scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom"):
            # Viewing scrollback: the cursor belongs to the live screen, and that coordinate
            # points at different content in history. Hide it rather than show a ghost block
            # (herdr and pi both hide the cursor while scrolled back).
            return b"\x1b[?25l"
        rect = sl[1]
        at = state["at"]
        col = rect.x + 1 + min(at[0], max(0, rect.width - 1))
        row = rect.y + 1 + min(at[1], max(0, rect.height - 1))
        return (f"\x1b[{min(row, rows)};{col}H".encode()
                + (b"\x1b[?25l" if state["hidden"] else b"\x1b[?25h"))

    def paint(data):
        """All drawing goes through here: the frame ends with the cursor back in place, with no gap in between.

        Always paint, never _write_all, on drawing paths. Otherwise the cursor stays on the last
        character drawn and the IME candidate window drifts with it: the sidebar fade repaints
        every 0.12 s, which dragged the cursor to the sidebar's bottom row and dropped the
        candidate window to the bottom of the screen."""
        _write_all(data + cursor_tail())

    chrome_cache = {"rows": []}       # Only changed rows are written (herdr's row-level diffing).
    chrome_state = {"chromed": [], "area": None, "tracks": []}   # Border and scrollbar geometry.
    # Roster and projects hit the filesystem and database, while the fade repaints every
    # 0.12 s; cache them and refresh only with the status poll.
    roster_cache = {"names": ["last-order", *sorted(_sisters_roster())]}
    projects_cache = {"items": []}
    expanded_projects = set()      # Projects whose card list is expanded.
    side_scrolls = {}              # Scroll offset per section (sisters/projects/windows).
    side_selected = [None]         # Last clicked row in Projects: ("proj", id) or ("card", id).

    def refresh_projects():
        try:
            projects_cache["items"] = control.request(
                "projects.list", {"workspace": workspace})["projects"]
        except (RuntimeError, ConnectionError):
            pass
    scroll_state = {}    # pane id -> {"metrics": ..., "alt": bool} (scroll readings from the daemon)

    def draw_borders():
        """herdr panes.rs:412: borders are always drawn last, on top of content, and redrawn when
        focus changes. Scrollbars likewise (render_pane_scrollbar also runs after content)."""
        if not chrome_state["chromed"]:
            return
        out = ["\x1b[?25l\x1b[?2026h",
               draw_pane_borders(chrome_state["chromed"], area=chrome_state["area"])]
        for pane_id, gutter, focused in chrome_state["tracks"]:
            if gutter is None:
                continue
            state = scroll_state.get(pane_id, {})
            metrics = state.get("metrics")
            if hui.should_show_scrollbar(metrics) and not state.get("alt"):
                out.append(draw_scrollbar(metrics, gutter, focused))
            else:
                # History cleared or alternate screen entered: wipe the gutter so no stale scrollbar sticks around.
                out.append("\x1b[0m")
                for y in range(gutter.y, gutter.y + gutter.height):
                    out.append(f"\x1b[{y + 1};{gutter.x + 1}H ")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
    ui_map = {"bar": None, "targets": []}   # Mouse hit areas: tab bar geometry plus sidebar target table.

    def draw_sidebar():
        nonlocal tab_scroll
        sync_tabs()
        names = [tab_label(tab) for tab in tabs]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), SIDEBAR_W + 1, len(tabs))
        # mouse_chrome=True: herdr's "+" new-tab button and overflow scroll buttons.
        bar = hui.compute_tab_bar_view(names, active_tab(), view["tab_bar_rect"],
                                       tab_scroll, tab_follow, True)
        tab_scroll = bar.scroll
        ui_map["bar"] = bar
        tab_line = render_tab_bar(names, active_tab(), bar, view["tab_bar_rect"],
                                  tab_scroll)
        side_lines, ui_map["targets"], ui_map["spans"] = format_sidebar(
            listing, focused, rows, roster=roster_cache["names"],
            projects=projects_cache["items"], expanded=expanded_projects,
            scrolls=side_scrolls, level=breath_level(time.monotonic()),
            selected=side_selected[0])
        wanted = []
        for row, line in enumerate(side_lines, 1):
            cell = (f"\x1b[{row};1H{line}\x1b[0m\x1b[{row};{SIDEBAR_W + 1}H"
                    f"{hui.sgr_fg(hui.OVERLAY0)}│\x1b[0m")
            if row == 1:
                cell += (f"\x1b[1;{view['tab_bar_rect'].x + 1}H\x1b[K"
                         f"\x1b[1;{view['tab_bar_rect'].x + 1}H{tab_line}")
            wanted.append(cell)
        cached = chrome_cache["rows"]
        out = ["\x1b[?25l\x1b[?2026h"]
        for index, chunk in enumerate(wanted):
            if index >= len(cached) or cached[index] != chunk:
                out.append(chunk)
        chrome_cache["rows"] = wanted
        out.append("\x1b[?2026l")
        if len(out) > 2:
            paint("".join(out).encode())

    def draw_prefix_bar():
        # herdr: entering prefix mode pops a mode bar on the bottom row (menus.rs
        # render_prefix_overlay). It spans only the main area; the sidebar is permanent
        # navigation and must not be covered.
        width = max(10, cols - SIDEBAR_W - 1)
        paint(f"\x1b[?2026h\x1b[{rows};{main_col}H\x1b[K"
                   f"{format_prefix_bar(width)}\x1b[?2026l".encode())

    def bottom_note(text):
        """One-line hint on the bottom row, main area only (never spills into the sidebar)."""
        width = max(10, cols - SIDEBAR_W - 2)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        while _wcwidth(plain) > width and plain:
            text, plain = text[:-1], plain[:-1]
        paint(f"\x1b[{rows};{main_col}H\x1b[K{text}\x1b[0m".encode())

    def restore_bottom():
        """Remove the mode bar: clear the main area's bottom row, then restore that row for any
        pane that reaches it (the sidebar was never covered, so it needs nothing)."""
        paint(f"\x1b[?25l\x1b[?2026h\x1b[{rows};{main_col}H\x1b[K".encode())
        for pane_id, rect in slices:
            if rect.y + rect.height < rows:      # This pane does not reach the bottom row.
                continue
            try:
                screen = control.request("pane.screen", {"id": pane_id})
            except (RuntimeError, ConnectionError):
                continue
            if screen["rows"]:
                paint(f"\x1b[{rows};{rect.x + 1}H{screen['rows'][-1]}"
                           "\x1b[?2026l".encode())

    def draw_help_overlay():
        # herdr: "?" shows the full key help (keybind_help), centered in the main area,
        # closed by any key. Width is clamped to the main area so narrow screens do not overflow.
        avail = cols - main_col + 1
        width = max(24, min(46, avail))
        lines = format_help_lines(width=width)
        top = max(2, (rows - len(lines)) // 2)
        left = main_col + max(0, (avail - width) // 2)
        out = ["\x1b[?25l\x1b[?2026h"]
        for index, line in enumerate(lines):
            out.append(f"\x1b[{top + index};{left}H\x1b[0m{line}")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())

    # ── Mouse selection and copy (built into herdr; does not rely on disabling mouse capture) ──
    # herdr keeps capturing the mouse and implements selection itself: dragging paints
    # selection_background, releasing copies (copy_on_select defaults to true), and
    # double-click selects a word. shift+mouse is left alone for the terminal's native selection.
    pane_rows = {}      # pane id -> full rendered rows; selection highlight and text extraction use these.
    sel = {"pane": None, "a": None, "b": None}    # Endpoints as in-pane (col, row).
    last_press = [0.0, -1, -1]                    # Double-click detection: time plus screen coordinates.

    def sel_rows(pane_id=None):
        pane_id = pane_id or sel["pane"]
        if pane_id is None or sel["a"] is None or sel["b"] is None:
            return set()
        return set(range(min(sel["a"][1], sel["b"][1]),
                         max(sel["a"][1], sel["b"][1]) + 1))

    def sel_span(row, width):
        """Column range [c0, c1) the selection covers on ``row``; linear selection, shaped like the terminal's own."""
        if sel["a"] is None or sel["b"] is None:
            return None
        (x0, y0), (x1, y1) = sorted((sel["a"], sel["b"]), key=lambda p: (p[1], p[0]))
        if not y0 <= row <= y1:
            return None
        return (x0 if row == y0 else 0, (x1 + 1) if row == y1 else width)

    def paint_selection(dirty=(), pane_id=None):
        """Repaint affected rows: restore from the cache first, then paint the selected span in the selection background."""
        pane_id = pane_id or sel["pane"]
        sl = slice_of(pane_id) if pane_id else None
        if sl is None:
            return
        rect = sl[1]
        buf = pane_rows.get(pane_id, [])
        out = ["\x1b[?25l\x1b[?2026h"]
        for row in sorted(set(dirty) | sel_rows(pane_id)):
            if not 0 <= row < rect.height:
                continue
            at = f"\x1b[{rect.y + row + 1};{rect.x + 1}H"
            line = buf[row] if row < len(buf) else ""
            out.append(f"{at}\x1b[0m{' ' * rect.width}{at}{line}")
            span = sel_span(row, rect.width)
            if span:
                c0, c1 = span
                text = _cols(_ANSI.sub("", line), c0, c1)
                text += " " * max(0, (c1 - c0) - _wcwidth(text))
                out.append(f"\x1b[{rect.y + row + 1};{rect.x + c0 + 1}H"
                           f"{hui.sgr_bg(hui.SURFACE0)}{hui.sgr_fg(hui.TEXT)}"
                           f"{text}\x1b[0m")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())

    def clear_selection():
        if sel["pane"] is None:
            return
        pane_id, dirty = sel["pane"], sel_rows()
        sel.update(pane=None, a=None, b=None)
        paint_selection(dirty, pane_id)

    def copy_selection():
        sl = slice_of(sel["pane"])
        if sl is None:
            return
        rect = sl[1]
        buf = pane_rows.get(sel["pane"], [])
        lines = []
        for row in sorted(sel_rows()):
            span = sel_span(row, rect.width)
            if span is None or row >= len(buf):
                continue
            lines.append(_cols(_ANSI.sub("", buf[row]), *span).rstrip())
        text = "\n".join(lines).strip("\n")
        if not text.strip():
            return
        _clipboard(text)
        bottom_note(f"{hui.sgr_fg(hui.OVERLAY1)}Copied {len(text)} characters")

    def pane_at(mx, my):
        """Which pane a 1-based screen coordinate falls in: ``(pane_id, col, row)`` in pane-local terms, or None."""
        for pane_id, rect in slices:
            if (rect.x < mx <= rect.x + rect.width
                    and rect.y < my <= rect.y + rect.height):
                return pane_id, mx - rect.x - 1, my - rect.y - 1
        return None

    def select_word(pane_id, col, row):
        """herdr: double-click selects the word under the pointer and copies it (same word-break set)."""
        buf = pane_rows.get(pane_id, [])
        if row >= len(buf):
            return
        plain = _ANSI.sub("", buf[row])
        # Expand to one entry per display column (wide characters take two) so columns index directly.
        cells = []
        for ch in plain:
            cells += [ch] * max(1, _wcwidth(ch))
        if col >= len(cells) or cells[col] in _WORD_BREAK:
            return
        start = end = col
        while start > 0 and cells[start - 1] not in _WORD_BREAK:
            start -= 1
        while end + 1 < len(cells) and cells[end + 1] not in _WORD_BREAK:
            end += 1
        sel.update(pane=pane_id, a=(start, row), b=(end, row))
        paint_selection()
        copy_selection()

    def paint_rows(pane_id, rendered, cur=None, clear=False, hidden=None):
        sl = slice_of(pane_id)
        if sl is None:
            return
        rect = sl[1]
        if cur is not None or hidden is not None:
            state = pane_cursor.setdefault(pane_id, {"at": [0, 0], "hidden": False})
            if cur is not None:
                state["at"] = list(cur)
            if hidden is not None:
                state["hidden"] = bool(hidden)
        buf = pane_rows.setdefault(pane_id, [])
        if clear or len(buf) != rect.height:
            buf[:] = [""] * rect.height
        out = ["\x1b[?25l\x1b[?2026h"]
        if clear:      # Full refresh (scrollback or relayout): wipe our own area first so no old rows linger.
            for row in range(rect.y, rect.y + rect.height):
                out.append(f"\x1b[{row + 1};{rect.x + 1}H" + " " * rect.width)
        for row_str, line in rendered.items():
            row = int(row_str)
            if row < rect.height:                # Only inside our own rectangle (herdr hard-clips).
                out.append(f"\x1b[{rect.y + row + 1};{rect.x + 1}H{line}")
                buf[row] = line
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
        if sel["pane"] == pane_id:       # New content painted over the highlight; put it back.
            paint_selection()

    def relayout():
        nonlocal slices
        sync_tabs()
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), SIDEBAR_W + 1, len(tabs))
        term = view["terminal_area"]
        # herdr: only the active tab is drawn; its layout is the BSP tree cut into rectangles (layout.rs collect_panes).
        tree = tabs[active_tab()] if tabs else None
        if tree is None:
            slices = []
            draw_sidebar()
            return
        if zoom:
            placed = [(focused, term, True)]
        else:
            placed = hui.collect_panes(tree, term, focused)
        # herdr panes.rs: with several panes each gets a full border (adjacent edges shared); content goes inside.
        chromed = hui.apply_pane_chrome(
            [{"id": pid, "rect": rect, "focused": f} for pid, rect, f in placed])
        slices, tracks = [], []
        for item in chromed:
            pane_inner = hui.pane_inner_rect(item["rect"], item["borders"])
            state = scroll_state.get(item["id"], {})
            # Then give up the rightmost column for the scrollbar (herdr stable_scrollbar_gutter: always reserved).
            content, _track = hui.stable_scrollbar_gutter(
                pane_inner, state.get("metrics"), alt_screen=state.get("alt", False))
            slices.append((item["id"], content))
            # The gutter is always reserved (herdr stable gutter); draw_borders decides per frame whether to draw a bar in it.
            gutter = (None if content == pane_inner else
                      hui.Rect(pane_inner.x + pane_inner.width - 1, pane_inner.y,
                               1, pane_inner.height))
            tracks.append((item["id"], gutter, item["focused"]))
        chrome_state["tracks"] = tracks
        paint(b"\x1b[?25l\x1b[?2026h")
        for pane_id, inner in slices:
            if inner.width < 2 or inner.height < 2:
                continue
            try:
                control.request("pane.resize", {"id": pane_id,
                                                "rows": inner.height, "cols": inner.width})
            except RuntimeError:
                continue
        for row in range(term.y + 1, term.y + term.height + 1):     # Clear the main area.
            paint(f"\x1b[{row};{term.x + 1}H\x1b[K".encode())
        paint(b"\x1b[?2026l")
        # Full-screen applications repaint only after SIGWINCH; wait briefly before grabbing
        # the screen or we get the blank one from right after the resize (herdr solves the
        # same problem with a nudge plus a forced full frame).
        time.sleep(0.08)
        for pane_id, _inner in slices:
            try:
                screen = control.request("pane.screen", {"id": pane_id})
            except RuntimeError:
                continue
            scroll_state[pane_id] = {"metrics": screen.get("scroll"),
                                     "alt": screen.get("alt_screen", False)}
            pane_cursor[pane_id] = {"at": list(screen["cursor"]),
                                    "hidden": bool(screen.get("cursor_hidden"))}
            paint_rows(pane_id, {str(i): line for i, line in enumerate(screen["rows"])})
        # herdr panes.rs:412: borders are redrawn after all pane content, otherwise content
        # covers them and the highlight fails to follow focus changes.
        chrome_state["chromed"] = chromed
        chrome_state["area"] = term
        draw_borders()
        draw_sidebar()

    def focus(pane_id, *, force_layout=False):
        nonlocal focused
        changed = focused != pane_id
        focused = pane_id
        try:
            control.request("pane.focused", {"id": pane_id})   # Focusing marks the pane as seen.
        except RuntimeError:
            pass
        # Changing focus while zoomed means the new pane takes over the zoom (herdr zoom follows focus).
        if force_layout or (changed and (zoom or slice_of(pane_id) is None)):
            relayout()
        else:
            sl = slice_of(pane_id)
            if sl is not None:
                try:
                    shot = control.request("pane.screen", {"id": pane_id})
                except RuntimeError:
                    pass
                else:
                    pane_cursor[pane_id] = {
                        "at": list(shot["cursor"]),
                        "hidden": bool(shot.get("cursor_hidden"))}
            if changed:     # Focus moved: the border highlight moves with it (herdr redraws borders every frame; we do it on demand).
                for item in chrome_state["chromed"]:
                    item["focused"] = item["id"] == pane_id
                draw_borders()
            draw_sidebar()

    _FOCUSED = object()          # Default target: split the currently focused pane.

    def new_pane(argv, title, *, split=None, target=_FOCUSED):
        """split=None opens a new tab; split="h"/"v" splits ``target`` in the current tab.
        target=_FOCUSED splits the focused pane (herdr split_focused); target=None wraps the
        whole tree (a new column, used by MISAKA's tiling rule)."""
        nonlocal listing
        out = control.request("pane.create",
                              {"argv": argv, "cwd": os.getcwd(), "title": title,
                               "env": {"MISAKA_THEME": hui.theme_variant()}})
        listing = panes()
        new_id = out["pane_id"]
        if split is not None:
            index = active_tab()
            if index < len(tabs):
                if target is None:
                    tabs[index] = hui.split_root(tabs[index], split, new_id)
                else:
                    # layout.rs:143 split_focused: only the focused leaf becomes a split node.
                    pane_target = focused if target is _FOCUSED else target
                    tabs[index] = hui.split_at(tabs[index], pane_target,
                                               split, new_id, 0.5)
        else:
            tabs.append(("pane", new_id))
        save_layout()
        focus(new_id, force_layout=True)

    def switch_tab(index):
        nonlocal tab_follow, zoom
        if 0 <= index < len(tabs):
            tab_follow = True
            zoom = False           # herdr: zoom is per-tab state; switching tabs leaves it.
            focus(hui.pane_ids(tabs[index])[0], force_layout=True)

    def close_focused():
        """Close the focused pane and move focus to the next live one. Returns True when no panes remain."""
        nonlocal listing
        page_idx = active_tab()
        page_ids = hui.pane_ids(tabs[page_idx]) if page_idx < len(tabs) else []
        survivors = [pid for pid in page_ids if pid != focused]
        control.request("pane.close", {"id": focused})
        listing = panes()
        sync_tabs()
        alive = [p["id"] for p in listing if p["alive"]]
        if not alive:
            return True
        if survivors and survivors[0] in alive:
            focus(survivors[0], force_layout=True)
        else:
            neighbor = tabs[min(page_idx, len(tabs) - 1)] if tabs else None
            focus(hui.pane_ids(neighbor)[0] if neighbor else alive[0],
                  force_layout=True)
        return False

    def on_wheel(x, y, delta):
        """Mouse wheel: scroll the sidebar section or the pane under the pointer."""
        if x <= SIDEBAR_W + 1:
            for span in ui_map.get("spans", []):
                if span["height"] and span["y0"] <= y < span["y0"] + span["height"]:
                    cap = max(0, span["total"] - span["height"])
                    step = 1 if delta > 0 else -1
                    side_scrolls[span["key"]] = max(
                        0, min(span["scroll"] + step, cap))
                    draw_sidebar()
                    return
            return
        for pane_id, rect in slices:
            if not (rect.x < x <= rect.x + rect.width
                    and rect.y < y <= rect.y + rect.height):
                continue
            if scroll_state.get(pane_id, {}).get("alt"):
                return  # Alternate-screen applications handle their own scrolling.
            try:
                out = control.request("pane.scroll", {"id": pane_id, "delta": delta})
            except RuntimeError:
                return
            scroll_state.setdefault(pane_id, {})["metrics"] = out["scroll"]
            paint_rows(pane_id, {str(i): line for i, line in enumerate(out["rows"])},
                       clear=True)
            draw_borders()
            return

    def _popup(lines):
        avail = cols - main_col + 1     # Clamp to the main area so narrow screens do not overflow on the right.
        top = max(2, (rows - len(lines)) // 2)
        left_col = main_col + max(0, (avail - _wcwidth(lines[0])) // 2)
        out = ["\x1b[?25l\x1b[?2026h"]
        for index, line in enumerate(lines):
            out.append(f"\x1b[{top + index};{left_col}H\x1b[0m{line}")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())

    def on_rclick(x, y):
        """Right-click in the sidebar: open the context menu for the card under the pointer."""
        nonlocal menu_card
        if x > SIDEBAR_W:
            return
        target = (ui_map["targets"][y - 1]
                  if 0 <= y - 1 < len(ui_map["targets"]) else None)
        width = max(24, min(46, cols - main_col + 1))
        if target and target[0] == "card":
            menu_card = target[1]
            _popup(format_card_menu(menu_card, width=width))

    def on_click(x, y):
        nonlocal tab_scroll, tab_follow
        bar = ui_map.get("bar")
        if y == 1 and bar is not None:                # Tab row: route by herdr's hit areas.
            cx = x - 1                                # Rects are 0-based.
            for index, rect in enumerate(bar.tab_hit_areas):
                if rect.width and rect.x <= cx < rect.x + rect.width:
                    switch_tab(index)
                    return
            for rect, step in ((bar.scroll_left_hit_area, -1),
                               (bar.scroll_right_hit_area, 1)):
                if rect.width and rect.x <= cx < rect.x + rect.width:
                    tab_follow = False
                    tab_scroll = max(0, tab_scroll + step)
                    draw_sidebar()
                    return
            rect = bar.new_tab_hit_area
            if rect.width and rect.x <= cx < rect.x + rect.width:
                new_pane([os.environ.get("SHELL", "sh")], "shell")
            return
        if x <= SIDEBAR_W:                            # Sidebar: route by the target table.
            target = (ui_map["targets"][y - 1]
                      if 0 <= y - 1 < len(ui_map["targets"]) else None)
            if target is None:
                return
            kind, value = target
            if kind == "pane":
                focus(value)
                return
            if kind == "proj":                        # Click a project: expand/collapse and select.
                expanded_projects.symmetric_difference_update({value})
                side_selected[0] = ("proj", value)
                draw_sidebar()          # Row diffing writes only changed rows; the column does not flash.
                return
            if kind == "card":
                # Running card: focus its pane. Finished card: open its session (--resume only
                # views and chats; it does not resend the contract or touch board state).
                # A card that never ran has no session to open.
                side_selected[0] = ("card", value)
                pane = next((p for p in listing
                             if p["card"] == value and p["alive"]), None)
                if pane:
                    focus(pane["id"])       # Already on screen: just move the highlight.
                    return
                viewer = next((p for p in listing     # A viewer pane is already open: focus it instead of opening another.
                               if p["alive"] and p["title"].endswith(f"·{value}")),
                              None)
                if viewer:
                    focus(viewer["id"])
                    return
                card = next((c for p in projects_cache["items"]
                             for c in p["cards"] if c["id"] == value), None)
                if not card or not card.get("has_session"):
                    draw_sidebar()
                    bottom_note(f"{hui.sgr_fg(hui.OVERLAY1)}Card {value} has no session yet\x1b[0m")
                    return
                argv = [sys.executable, "-m", "misaka", "card-shell",
                        value, "--resume"]
                title = f"{card.get('assignee', '?')}·{value}"
                index = active_tab()
                if index < len(tabs):
                    tile_target, direction = next_tile_placement(tabs[index])
                    new_pane(argv, title, split=direction, target=tile_target)
                else:
                    new_pane(argv, title)
                return
            argv = [sys.executable, "-m", "misaka", "chat"]   # Roster: clicking a name starts her.
            title = "Last Order" if value == "last-order" else value
            if value != "last-order":
                argv += ["--as", value]
            # Open her pane in the current tab using the tiling rule (two per column, then a new column).
            index = active_tab()
            if index < len(tabs):
                target, direction = next_tile_placement(tabs[index])
                new_pane(argv, title, split=direction, target=target)
            else:
                new_pane(argv, title)
            return
        for pane_id, rect in slices:                  # Click a pane: focus it (rectangle hit test).
            if (rect.x < x <= rect.x + rect.width
                    and rect.y < y <= rect.y + rect.height):
                focus(pane_id)
                return

    old_attrs = termios.tcgetattr(0)
    new_attrs = termios.tcgetattr(0)
    new_attrs[0] &= ~(termios.IXON | termios.ICRNL)
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(0, termios.TCSANOW, new_attrs)
    # Alternate screen (standard for herdr and every proper TUI): without it Terminal.app
    # adds a "mark" to every line that gets a carriage return, which renders as a pair of
    # dim brackets. ?1002 reports motion only while a button is held, which is what
    # drag selection needs (?1003 reports all motion, for hover UIs; we have none).
    _write_all(b"\x1b[?1049h\x1b[?7l\x1b[?1000;1002;1006h")
    prefix_pending = 0
    exit_reason = ["detached"]     # detached: user left; closed_all: the last pane was closed.
    help_open = False
    menu_card = None
    try:
        paint(b"\x1b[0m\x1b[2J")
        chrome_cache["rows"] = []          # The row cache must be invalidated after a clear, or the sidebar draws nothing.
        stream.send("pane.attach", {"id": "*"})
        load_layout()          # Pick up the previous split layout (stored in the daemon).
        refresh_projects()
        focus(focused, force_layout=True)
        last_poll = last_blink = 0.0
        repaint_after_typing = 0.0   # After typing pauses, repaint the focused pane once to erase IME leftovers.
        while True:
            if resized.pop("hit", None):
                rows, cols = _term_size()
                paint(b"\x1b[0m\x1b[2J")
                chrome_cache["rows"] = []
                relayout()
            readable, _, _ = select.select([0, stream.sock], [], [], 0.05)
            now = time.monotonic()
            if repaint_after_typing and now > repaint_after_typing:
                # IME pre-edit text is drawn directly by the terminal: the application never sees
                # it and our emulated screen does not have it, so nothing restores that area once
                # it is withdrawn and the content looks shifted. Once typing pauses, repaint the
                # focused pane from its real content to wipe the leftovers.
                repaint_after_typing = 0.0
                try:
                    shot = control.request("pane.screen", {"id": focused})
                    scroll_state[focused] = {"metrics": shot.get("scroll"),
                                             "alt": shot.get("alt_screen", False)}
                    pane_cursor[focused] = {
                        "at": list(shot["cursor"]),
                        "hidden": bool(shot.get("cursor_hidden"))}
                    paint_rows(focused,
                               {str(i): line for i, line in enumerate(shot["rows"])},
                               clear=True)
                    draw_borders()
                except (RuntimeError, ConnectionError):
                    pass

            if 0 in readable:
                chunk = os.read(0, 4096)
                # Mouse: left click focuses, drag selects, release copies, double-click selects a word, wheel scrolls back.
                for m in _MOUSE.finditer(chunk):
                    button, mx, my = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)))
                    press = m.group(4) == b"M"
                    if button in (64, 65):
                        on_wheel(mx, my, -3 if button == 64 else 3)
                    elif button == 32 and press:            # Drag with the left button held.
                        sl = slice_of(sel["pane"]) if sel["pane"] else None
                        if sl and sel["a"]:
                            rect = sl[1]   # Dragging outside the pane clamps to its edge; never select into a neighbor.
                            spot = (min(max(mx - rect.x - 1, 0), rect.width - 1),
                                    min(max(my - rect.y - 1, 0), rect.height - 1))
                            if spot != sel["b"]:
                                dirty = sel_rows()
                                sel["b"] = spot
                                paint_selection(dirty)
                    elif press and button in (0, 1):
                        hit = pane_at(mx, my)
                        double = (hit and now - last_press[0] < 0.4
                                  and last_press[1:] == [mx, my])
                        clear_selection()
                        last_press[:] = [0.0 if double else now, mx, my]
                        if double:
                            on_click(mx, my)
                            select_word(*hit)
                        else:
                            if hit:      # A selection starts only inside a pane; sidebar clicks do not count.
                                sel.update(pane=hit[0], a=hit[1:], b=None)
                            on_click(mx, my)
                    elif press and button == 2:             # Right button.
                        clear_selection()
                        on_rclick(mx, my)
                    elif not press and button == 0 and sel["b"]:
                        copy_selection()    # herdr copy_on_select: releasing copies to the clipboard.
                chunk = _MOUSE.sub(b"", chunk)
                chunk = _MOUSE_X10.sub(b"", chunk)     # Legacy reports are only stripped, so they never reach a pane as input.
                plain = bytearray()
                detach = False
                for offset in range(len(chunk)):       # Byte by byte: prefix and command may arrive in one read.
                    key = chunk[offset:offset + 1]
                    if help_open:                      # Key help is open: any key closes it and redraws.
                        help_open = False
                        relayout()
                        continue
                    if menu_card is not None:          # Card menu: d deletes, any other key closes.
                        card_id, menu_card = menu_card, None
                        if key in (b"d", b"D"):
                            try:
                                control.request("card.delete", {"task_id": card_id})
                            except RuntimeError as error:
                                refresh_projects()
                                relayout()
                                bottom_note(f"\x1b[33m{error}\x1b[0m")
                                continue
                            refresh_projects()
                        relayout()
                        continue
                    if not prefix_pending:
                        if key == PREFIX:
                            prefix_pending = 1
                            draw_prefix_bar()          # herdr: the PREFIX mode bar takes over the bottom row.
                        else:
                            plain += key
                        continue
                    prefix_pending = 0
                    restore_bottom()                   # Mode bar goes away; the bottom row returns to the pane.
                    if key == b"\x1b":                 # esc cancels the prefix (as in herdr).
                        continue
                    if key != PREFIX and 1 <= key[0] <= 26:
                        key = bytes([key[0] + 96])   # ctrl+d counts as d: a slip of the finger still works.
                    if key == PREFIX:
                        plain += PREFIX
                    elif key in b"123456789":          # herdr: digits switch tabs.
                        switch_tab(int(key) - 1)
                    elif key in b"np" and tabs:        # Cycle tabs.
                        switch_tab((active_tab() + (1 if key == b"n" else -1))
                                   % len(tabs))
                    elif key in b"hjkl":              # herdr focus_pane_h/j/k/l (1051-1054).
                        target = hui.find_in_direction(
                            focused,
                            {b"h": "left", b"j": "down",
                             b"k": "up", b"l": "right"}[key],
                            [(pid, rect) for pid, rect in slices])
                        if target:
                            focus(target)
                    elif key == b"z":                 # herdr zoom (1065): the focused pane fills the tab.
                        zoom = not zoom
                        relayout()
                    elif key == b"c":                  # herdr new_tab (model.rs:1039).
                        new_pane([os.environ.get("SHELL", "sh")], "shell")
                    elif key == b"v":                  # split_vertical (1062): side by side.
                        new_pane([os.environ.get("SHELL", "sh")], "shell", split="h")
                    elif key == b"-":                  # split_horizontal (1063): stacked.
                        new_pane([os.environ.get("SHELL", "sh")], "shell", split="v")
                    elif key == b"t":                  # Summon the global tree: focus it if open, else split it off to the right.
                        action, val = tree_summon_action(panes())
                        if action == "focus":
                            focus(val)
                        else:
                            new_pane(val, TREE_TITLE, split="h")
                    elif key == b"d":
                        detach = True
                    elif key == b"x":
                        # herdr prefix+x = ClosePane: one key, no confirmation (actions.rs:2035 only
                        # confirms when closing a worktree group, which MISAKA does not have).
                        # Closing the last pane exits the panel.
                        if close_focused():
                            exit_reason[0] = "closed_all"
                            detach = True
                    elif key == b"?":
                        help_open = True
                        draw_help_overlay()
                if plain:
                    control.request("pane.input", {
                        "id": focused, "data": base64.b64encode(bytes(plain)).decode()})
                    repaint_after_typing = now + 0.9   # Repaint after typing stops to erase IME leftovers.
                if detach:
                    return

            if stream.sock in readable or stream.buf:
                painted = False
                while (line := stream.readline()) is not None:
                    msg = json.loads(line)
                    if msg.get("event") == "screen":
                        if "scroll" in msg:      # Metrics update with every frame (clear drops history).
                            scroll_state[msg["id"]] = {
                                "metrics": msg["scroll"],
                                "alt": msg.get("alt_screen", False)}
                        paint_rows(msg["id"], msg["rows"], msg.get("cursor"),
                                   hidden=msg.get("cursor_hidden"))
                        painted = True
                    elif msg.get("event") == "exited":
                        # The application exited, so the pane is done: close it; when none are left, return to the shell.
                        try:
                            control.request("pane.close", {"id": msg["id"]})
                        except RuntimeError:
                            pass
                        listing = panes()
                        alive = [p for p in listing if p["alive"]]
                        if not alive:
                            exit_reason[0] = "closed_all"
                            return
                        if msg["id"] == focused:
                            focus(alive[0]["id"], force_layout=True)
                        else:
                            relayout()
                        painted = False
                if painted:
                    draw_borders()      # Content paints over borders; herdr redraws them at the end of every frame.

            if now - last_poll > POLL_SECONDS:
                last_poll = now
                roster_cache["names"] = ["last-order", *sorted(_sisters_roster())]
                refresh_projects()
                try:
                    listing = panes()
                except (RuntimeError, ConnectionError):
                    return
                dead = [p for p in listing if not p["alive"] and not p["card"]]
                if dead:      # Exit events can precede the subscription and get missed; the poll cleans up.
                    for pane in dead:
                        try:
                            control.request("pane.close", {"id": pane["id"]})
                        except RuntimeError:
                            pass
                    listing = panes()
                    alive = [p for p in listing if p["alive"]]
                    if not alive:
                        exit_reason[0] = "closed_all"
                        return
                    if not any(p["id"] == focused and p["alive"] for p in listing):
                        focus(alive[0]["id"], force_layout=True)
                    else:
                        relayout()
                draw_sidebar()
            elif now - last_blink > BREATH_FPS:
                # Only the sidebar needs redrawing for the pulse animation.
                last_blink = now
                if any(p.get("busy") for p in listing):
                    draw_sidebar()
    except (RuntimeError, ConnectionError, json.JSONDecodeError) as error:
        sys.exit(f"Panel disconnected: {error}")
    except Exception as error:   # noqa: BLE001
        sys.exit(
            f"Panel crashed with {type(error).__name__}: {error}\n"
            "The panes are still running; run `misaka` again to reconnect."
        )
    finally:
        termios.tcsetattr(0, termios.TCSANOW, old_attrs)
        # Turn off mouse tracking, leave the alternate screen, and show the cursor again.
        _write_all(b"\x1b[?1000;1002;1006l\x1b[?7h\x1b[0m\x1b[2J\x1b[?1049l\x1b[?25h")
        if exit_reason[0] == "closed_all":
            print("All panes closed. Run `misaka` to open the panel again.")
        else:
            print("Detached. The daemon and panes keep running; `misaka net stop` shuts them down.")
