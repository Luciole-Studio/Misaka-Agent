"""Multi-pane panel: MISAKA's default entry point (herdr-style; geometry from geometry.py).

Rendering: the daemon keeps a terminal emulator per pane; the panel subscribes
to dirty rows from every pane and lays them out itself: herdr's sidebar on the left
(spaces = folders on top, agents = panes below), a tab row at the top of the
main area, and a per-tab BSP split tree (splitting only the focused pane; each
pane gets a border and joints are merged). Colors come from the engine's
built-in dark/light theme, adapted to the terminal background; only herdr's
color roles are borrowed.

Controls: click a tab, sidebar entry or pane to focus it. Prefix key ctrl+b,
then: 1-9 switch tab | n/p cycle tabs | hjkl move focus | c new tab |
v / - split side by side / stacked | z zoom | g navigator | [ copy mode | r resize |
T / W rename tab / space |
x close pane | d quit (Last Order and the Sisters shut down with the panel) | ? key help |
ctrl+b again sends a literal ctrl+b.
"""
import base64
import concurrent.futures
import enum
import json
import os
import select
import signal
import socket
import struct
import sys
import time

from misaka.config import home
from misaka.ui.panel import client as net
from misaka.ui.panel import geometry as hui
from misaka.ui.panel import host_input as hin
from misaka.ui.panel import pane_input as pin
from misaka.ui.panel import screen as sc
from misaka.ui.panel import selection as selmod
from misaka.ui.panel.selection import Selection
from misaka.ui.panel.selection import absolute_row as _abs_row
from misaka.ui.panel.text_editor import TextEditor, grapheme_width, graphemes
from misaka.utils import local_socket

# The host terminal is a tty on POSIX and a console on Windows, whose input is read on a thread
# (utils/win_console) and handed to the loop through a socketpair, the one thing select() takes there.
WINDOWS = sys.platform == "win32"
if WINDOWS:
    from misaka.utils import win_console
else:
    import fcntl
    import termios
    import tty


def _prefix_key():
    """Prefix key, default ctrl+b. Inside tmux that key is taken, so set
    ``panel.prefix`` to ctrl+g (or similar) in settings.json to change it."""
    from misaka.config.product import setting

    name = str(setting("panel", "prefix", "ctrl+b", str)).lower().strip()
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return hin.Key(name[5], hin.CTRL)
    return hin.Key("b", hin.CTRL)


PREFIX = _prefix_key()
PREFIX_NAME = f"ctrl+{PREFIX.code}"   # menus.rs render_prefix_overlay: format_key_combo(prefix)
POLL_SECONDS = 2.0
GIT_TTL_SECONDS = 10.0     # a branch row is not worth a git subprocess every other second
SIDEBAR_W = 26            # herdr ui.sidebar_width default (config/model.rs:1010); the separator column is the last one.
SIDEBAR_COLLAPSED_W = 4   # herdr ui.rs:229: the collapsed sidebar.
MOUSE_SCROLL_LINES = 3    # herdr DEFAULT_MOUSE_SCROLL_LINES
SELECTION_AUTOSCROLL_INTERVAL = 0.03   # herdr app/mod.rs:40 SELECTION_AUTOSCROLL_INTERVAL (30 ms, 1 line/tick)


def _selection_edge_scroll_lines(distance):
    """herdr selection.rs selection_edge_scroll_lines: the further past the edge the pointer
    is, the faster the immediate scroll, clamped to a sane band."""
    return min(max(distance * 3, 3), 15)
SPLIT_DRAG_INTERVAL = 0.016  # herdr applies a divider drag at its render pace (MIN_RENDER_INTERVAL 16 ms).
#                            The re-wrap that once forced a slower pace here now happens once, on the daemon,
#                            when the drag settles (daemon RESIZE_COALESCE_SECONDS), so the divider tracks at
#                            render pace again: only the border and the clipped cache move per step.
PANE_DOUBLE_CLICK_WINDOW = 0.35      # herdr app/mod.rs:48
PANE_COPY_HIGHLIGHT_DURATION = 0.5   # herdr app/mod.rs:49: a double-clicked word stays lit this long
COPY_FEEDBACK_DURATION = 2.0         # herdr app/mod.rs:50: "copied to clipboard"
TOAST_DURATION = 4.0                 # a failure notice (herdr NeedsAttention toast)
# Word-break characters for double-click selection (herdr's embedded set) plus whitespace.
_WORD_BREAK = set(" \t" + "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~")

_wcwidth = hui.display_width


class Mode(enum.Enum):
    """What the panel's keys and mouse mean right now (herdr app::Mode). Exactly one is
    in force; every drawing path asks it, so the host cursor, the mode bar and the popups
    can never disagree about which layer owns the screen."""
    TERMINAL = "terminal"
    PREFIX = "prefix"
    COPY = "copy"
    RESIZE = "resize"
    RENAME = "rename"
    GLOBAL_MENU = "global_menu"
    KEYBIND_HELP = "keybind_help"
    NAVIGATOR = "navigator"


def key_text(key):
    """The character a key types (shift applied), for command tables keyed by
    characters: ``T`` for shift+t however the host reported it. None for chords."""
    if not key.is_char or key.kind == "release":
        return None
    mods = key.mods & ~hin.LOCK_MASK
    if mods == 0:
        return key.code
    if mods == hin.SHIFT:
        return pin.encode_key(hin.Key(key.code, key.mods, key.kind, key.shifted), pin.DEFAULT_STATE).decode() or None
    return None


def format_copy_feedback(message, area, offset=0):
    """herdr status.rs render_copy_feedback: a green-bordered box, "● message" bold on
    panel_bg, three rows tall, at the bottom centre of ``area`` (ToastClipboardPosition::
    BottomCenter), ``offset`` rows up. Returns ``(rect, rows)`` with rows = [(y, x, ansi)].
    Pure, so testable."""
    if area.width == 0 or area.height == 0:
        return None, []
    width = min(_wcwidth(message) + 4, area.width)
    height = min(3, area.height)
    rect = hui.Rect(area.x + (area.width - width) // 2, area.y + max(0, area.height - (height + offset)),
                    width, height)
    return rect, _boxed_rows(rect, [[("●", {"fg": hui.PALETTE["green"]}), (" " + message, {"fg": hui.TEXT, "bold": True})]],
                             hui.PALETTE["green"])


def rects_overlap(left, right):
    """ui.rs rectangles_overlap."""
    return (left is not None and right is not None and left.x < right.x + right.width
            and right.x < left.x + left.width and left.y < right.y + right.height
            and right.y < left.y + left.height)


def copy_feedback_offset(message, area, toast_rect):
    """ui.rs copy_feedback_offset_for_toast: the copy box moves up by the toast's height
    when the two would overlap, so both stay readable."""
    rect, _rows = format_copy_feedback(message, area)
    return toast_rect.height if rects_overlap(rect, toast_rect) else 0


MAX_QUEUED_NOTIFICATIONS = 8   # notification_policy.rs: the oldest queued toast gives way


class ToastQueue:
    """notification_policy.rs: one toast shows at a time, the rest wait first in, first out
    (at most eight; a full queue drops its oldest); a toast's time starts when it is shown.
    A new toast about a pane replaces the one showing for that pane and any waiting ones."""

    __slots__ = ("queue", "visible")

    def __init__(self):
        self.visible, self.queue = None, []

    def push(self, toast, now, duration):
        pane = toast.get("pane")
        if pane is not None:
            self.queue = [queued for queued in self.queue if queued.get("pane") != pane]
            if self.visible is not None and self.visible.get("pane") == pane:
                self.visible = None
                self.promote(now, duration)
        if self.visible is None:
            self.visible = {**toast, "deadline": now + duration}
            return
        if len(self.queue) == MAX_QUEUED_NOTIFICATIONS:
            self.queue.pop(0)
        self.queue.append(toast)

    def promote(self, now, duration):
        if not self.queue:
            return False
        self.visible = {**self.queue.pop(0), "deadline": now + duration}
        return True

    def tick(self, now, duration):
        """Retire the toast whose time is up and show the next; True when anything changed."""
        if self.visible is None or now < self.visible["deadline"]:
            return False
        self.visible = None
        self.promote(now, duration)
        return True


def format_toast(title, context, kind, area):
    """herdr status.rs render_toast_notification at ToastHerdrPosition::BottomRight: an
    overlay0 border on panel_bg, "● title" (red dot for needs_attention, green for
    finished) and an indented context line. Returns ``(rect, rows)``. Pure, so testable."""
    if area.width == 0 or area.height == 0:
        return None, []
    content = max(_wcwidth(title), _wcwidth(context)) + 4
    width = min(content + 2, area.width)
    height = min((2 if context else 1) + 2, area.height)
    rect = hui.Rect(area.x + area.width - width, area.y + area.height - height, width, height)
    dot = hui.PALETTE["red"] if kind == "needs_attention" else hui.PALETTE["green"]
    lines = [[("●", {"fg": dot}), (" " + title, {"fg": hui.TEXT, "bold": True})]]
    if context:
        lines.append([("  " + context, {"fg": hui.OVERLAY0})])
    return rect, _boxed_rows(rect, lines, hui.OVERLAY0)


class DragPacer:
    """Coalesces the positions of a divider drag: herdr resizes panes when it renders, at
    most every 16 ms (app/mod.rs MIN_RENDER_INTERVAL), so a burst of motion events costs one
    resize per frame. Applying every motion event here re-wrapped every pane on each one and
    the drag stuttered; the latest position is applied once the interval is up or the drag ends."""

    __slots__ = ("applied_at", "interval", "pending")

    def __init__(self, interval=SPLIT_DRAG_INTERVAL):
        self.interval = interval
        self.applied_at = None
        self.pending = None

    def offer(self, position, now):
        """A new position: returns it when it can be applied now, else keeps it for later."""
        if self.applied_at is not None and now - self.applied_at < self.interval:
            self.pending = position
            return None
        self.applied_at = now
        self.pending = None
        return position

    def due(self):
        """When the pending position should be applied (None when nothing is pending)."""
        return None if self.pending is None else self.applied_at + self.interval

    def take(self, now, final=False):
        """The pending position once its time has come (or at the end of the drag)."""
        if self.pending is None or (not final and now < self.applied_at + self.interval):
            return None
        position, self.pending, self.applied_at = self.pending, None, now
        return position


def _boxed_rows(rect, lines, border):
    """A bordered panel on panel_bg with styled text lines inside (widgets.rs render_panel_shell)."""
    if rect.width < 2 or rect.height < 2:
        return []
    canvas = _Canvas(rect.width, rect.height)
    for y in range(rect.height):
        canvas.fill_bg(0, rect.width, y, hui.PANEL_BG)
    canvas.put(0, 0, "┌" + "─" * (rect.width - 2) + "┐", fg=border)
    for y in range(1, rect.height - 1):
        canvas.put(0, y, "│", fg=border)
        canvas.put(rect.width - 1, y, "│", fg=border)
    canvas.put(0, rect.height - 1, "└" + "─" * (rect.width - 2) + "┘", fg=border)
    for offset, spans in enumerate(lines[:max(0, rect.height - 2)]):
        x = 1
        for text, style in spans:
            canvas.put(x, 1 + offset, text, clip=rect.width - 1, **style)
            x += _wcwidth(text)
    return [(rect.y + y, rect.x, canvas.row(y)) for y in range(rect.height)]


# Random quips shown when someone opens the panel inside a pane (herdr main.rs:13-20, MISAKA edition).


def render_tab_bar(tab_names, active_index, view, area, tab_scroll=0, zoomed=()):
    """Port of herdr client/shell/tabs.rs render_tab_bar, the drawing half (the layout is
    geometry.compute_tab_bar_view). The row is filled with panel_bg; scroll buttons " < " /
    " > " on surface0, overlay1 when they can scroll and overlay0 when they cannot; tab
    labels centered and clipped to their own rect, the active one on accent with a
    contrasting foreground, the rest overlay1 on surface0; a " + " button (overlay1 on
    panel_bg); an overlay0 ellipsis on either side when tabs are cut off, keeping the
    background of the cell it lands on. Returns the whole row as text. Pure, so testable.
    (herdr's is_auto_named is ignored: MISAKA tab names are always agent names, so every
    tab gets the custom-name style.)"""
    width = max(0, area.width)
    if width == 0:
        return ""
    panel_bg = hui.PALETTE["panel_bg"]
    canvas = [[" ", None, panel_bg, False] for _ in range(width)]   # symbol, fg, bg, bold

    def put(x, text, limit, fg, bg, bold=False):
        """render.rs put_text: at most ``limit`` columns from ``x``; a wide character that
        would cross the limit is not drawn."""
        col, end = x - area.x, x - area.x + limit
        for ch in text:
            span = 2 if _wcwidth(ch) == 2 else 1
            if col + span > end:
                break
            if 0 <= col < width:
                canvas[col] = [ch, fg, bg, bold]
                if span == 2 and col + 1 < width:
                    canvas[col + 1] = ["", fg, bg, bold]
            col += span

    visible = [i for i, r in enumerate(view.tab_hit_areas) if r.width > 0]
    first_visible = visible[0] if visible else None
    last_visible = visible[-1] if visible else None
    left, right, new_tab = view.scroll_left_hit_area, view.scroll_right_hit_area, view.new_tab_hit_area
    if left.width:
        put(left.x, " < ", left.width, hui.OVERLAY1 if tab_scroll > 0 else hui.OVERLAY0, hui.SURFACE0)

    for index, rect in enumerate(view.tab_hit_areas):
        if rect.width == 0 or index >= len(tab_names):
            continue
        if index == active_index:
            fg, bg, bold = hui.panel_contrast_fg(), hui.ACCENT, True
        else:
            fg, bg, bold = hui.OVERLAY1, hui.SURFACE0, False
        name = hui.tab_chrome_label(tab_names, index, zoomed)   # tabs.rs tab_label: " Z" while zoomed
        padding = max(0, rect.width - hui.display_width(name))
        left_pad = padding // 2
        put(rect.x, " " * left_pad + name + " " * (padding - left_pad), rect.width, fg, bg, bold)

    if right.width:
        put(right.x, " > ", right.width,
            hui.OVERLAY1 if tab_scroll < view.max_scroll else hui.OVERLAY0, hui.SURFACE0)
    if new_tab.width:
        put(new_tab.x, " + ", new_tab.width, hui.OVERLAY1, panel_bg)

    def ellipsis(x):
        col = x - area.x
        if 0 <= col < width:
            canvas[col][0], canvas[col][1] = "…", hui.OVERLAY0   # foreground only

    content_right = area.x + width
    if first_visible is not None and first_visible > 0:      # tabs cut off on the left
        x = left.x + left.width if left.width else area.x
        if x < content_right:
            ellipsis(x)
    if last_visible is not None and last_visible + 1 < len(tab_names):
        x = right.x - 1 if right.width else content_right - 1
        if area.x <= x < content_right:
            ellipsis(x)

    out, last_style = [], None
    for ch, fg, bg, bold in canvas:
        if ch == "":
            continue
        style = (fg, bg, bold)
        if style != last_style:
            out.append("\x1b[0m" + (hui.sgr_fg(fg) if fg is not None else "") + hui.sgr_bg(bg)
                       + ("\x1b[1m" if bold else ""))
            last_style = style
        out.append(ch)
    out.append("\x1b[0m")
    return "".join(out)


# ── Sidebar: src/ui/sidebar.rs render_sidebar / render_sidebar_collapsed on a cell canvas ──

class _Canvas:
    """A cell grid standing in for ratatui's Buffer: every cell keeps its own symbol and
    style, later writes patch earlier ones (a bg fill survives the text drawn over it, like
    ratatui's Cell::set_style), and each row serializes to one ANSI string."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[[" ", None, None, False, False] for _ in range(width)]
                      for _ in range(height)]

    def put(self, x, y, text, fg=None, bg=None, bold=False, dim=False, clip=None):
        """Write ``text`` at (x, y); ``clip`` is the widget rect's exclusive right edge."""
        if not 0 <= y < self.height:
            return
        limit = self.width if clip is None else min(clip, self.width)
        for ch in text:
            w = _wcwidth(ch)
            if x + w > limit:
                break
            if x >= 0:
                self._patch(x, y, ch, fg, bg, bold, dim)
                if w == 2:                       # A wide character also owns the next cell.
                    self._patch(x + 1, y, "", fg, bg, bold, dim)
            x += w

    def _patch(self, x, y, symbol, fg, bg, bold, dim):
        cell = self.cells[y][x]
        cell[0] = symbol
        if fg is not None:
            cell[1] = fg
        if bg is not None:
            cell[2] = bg
        cell[3] = cell[3] or bold
        cell[4] = cell[4] or dim

    def fill_bg(self, x0, x1, y, bg):
        """Buffer-wide background fill of one row span (sidebar.rs:1082-1091 highlighted rows)."""
        if 0 <= y < self.height:
            for x in range(max(0, x0), min(x1, self.width)):
                self.cells[y][x][2] = bg

    def row(self, y):
        out, last = [], None
        for symbol, fg, bg, bold, dim in self.cells[y]:
            if symbol == "":
                continue
            style = (fg, bg, bold, dim)
            if style != last:
                out.append("\x1b[0m" + (hui.sgr_fg(fg) if fg else "")
                           + (hui.sgr_bg(bg) if bg else "")
                           + ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else ""))
                last = style
            out.append(symbol)
        out.append("\x1b[0m")
        return "".join(out)


def _put_editor(canvas, x, y, width, editor, fg, bg):
    """text_editor.rs render: blank the field, write the part of the text that keeps the
    cursor visible (one cell per grapheme cluster; zero-width clusters are skipped, as
    ratatui's set_stringn does), and return the cursor's column, or None for no field."""
    width = min(width, canvas.width - x)
    if width <= 0 or not 0 <= y < canvas.height:
        return None
    for col in range(x, x + width):
        canvas._patch(col, y, " ", fg, bg, False, False)
    text, caret = editor.viewport(width)
    col = x
    for cluster in graphemes(text):
        cells = grapheme_width(cluster)
        if cells == 0:
            continue
        if col + cells > x + width:
            break
        canvas._patch(col, y, cluster, fg, bg, False, False)
        for extra in range(1, cells):
            canvas._patch(col + extra, y, "", fg, bg, False, False)
        col += cells
    return x + caret


def pane_state(pane):
    """The one bridge from MISAKA pane fields to herdr's (AgentState, pane.seen) pair.
    The session's own report wins (``reported``, herdr's hook authority: the agent_state
    extension says working / idle / blocked), then the detection for a third-party agent
    (``agent_state``, herdr's manifest rules in the daemon), and only then the screen
    heuristic (``busy``), which speaks for plain shells. The board adds the cases where
    someone must act before anything moves: a blocked, triaged, or failed card, or one parked
    at the peer-review gate, is "blocked" too. A finished card nobody looked at is done
    (idle + unseen); a dead pane is unknown. Pure, so testable."""
    if not pane["alive"]:
        return "unknown", True
    reported = (pane.get("reported") or {}).get("state")
    detected = pane.get("agent_state")
    if reported == "blocked" or detected == "blocked" or pane.get("status") in ("blocked", "triage", "failed", "review"):
        return "blocked", True
    if reported == "working" or (reported is None and detected == "working"):
        return "working", True
    if reported is None and detected == "unknown":
        return "unknown", True          # an ally that says nothing: herdr shows unknown, not busy
    if reported is None and detected is None and pane.get("busy"):
        return "working", True
    if pane.get("unseen"):
        return "idle", False
    return "idle", True


LO_TITLE = "Last Order"      # the panel opens Last Order panes with this title; they are space roots
SISTERS_LABEL = "sisters"    # the footer launcher (herdr: "menu"); it opens the Sister roster
def space_key(pane):
    """A pane's folder as a real path (herdr's workspace identity cwd, workspace.rs:1161-1173)."""
    return os.path.realpath(pane.get("cwd") or os.getcwd())


def _space_label(folder):
    user_home = os.path.expanduser("~")
    return "~" if folder == user_home else (os.path.basename(folder.rstrip(os.sep)) or folder)


def sidebar_model(spaces, listing, focused_id, active_id):
    """Shape herdr's two lists from explicit spaces (herdr Workspace: ``{"id", "folder",
    "name", "tabs": [trees]}``) and the pane listing. A space row is labelled by its custom
    name or its folder's last component and marked with its most attention-worthy pane
    (aggregate.rs:86-99); ``active_id`` is herdr's app.active. An agent entry is one pane in
    some tab that runs something other than a bare shell (aggregate.rs:29-69 lists only
    panes with an agent); its tab number shows only when its space has several tabs
    (sidebar.rs:166-171). Pure, so testable."""
    by_id = {pane["id"]: pane for pane in listing}
    rows, agents = [], []
    for space in spaces:
        # workspace.rs:1123-1131: the identity cwd follows the first tab's root pane -- when its
        # foreground job cd'ed somewhere, the label follows it there.
        root = hui.pane_ids(space["tabs"][0])[0] if space["tabs"] else None
        root_pane = by_id.get(root) or {}
        folder = ((root_pane.get("foreground") or {}).get("cwd")
                  or root_pane.get("cwd") or space["folder"])
        row = {"key": space["id"], "label": space.get("name") or _space_label(folder),
               "folder": folder, "active": space["id"] == active_id,
               "state": "unknown", "seen": True, "alive": False, "dormant": not space["tabs"]}
        rows.append(row)
        multi = len(space["tabs"]) > 1
        for index, tree in enumerate(space["tabs"]):
            for pane_id in hui.pane_ids(tree):
                pane = by_id.get(pane_id)
                if pane is None:
                    continue
                state, seen = pane_state(pane)
                row["alive"] = row["alive"] or bool(pane["alive"])
                if (hui.attention_priority(state, seen)
                        > hui.attention_priority(row["state"], row["seen"])):
                    row["state"], row["seen"] = state, seen
                # ponytail: MISAKA's own shell panes carry this title; one running an ally (the
                # daemon recognised codex/claude in its foreground) counts as that agent.
                if pane["title"] == "shell" and not pane.get("ally"):
                    continue
                agents.append({"pane": pane_id, "space": row["label"], "space_key": space["id"],
                               "tab": str(index + 1) if multi else None,
                               "agent": pane.get("ally") or pane["title"] or pane_id,
                               "state": state, "seen": seen,
                               "active": pane_id == focused_id})
    return rows, agents


# Cards in these statuses legitimately own their card/<id> branch; any other card/* branch
# is stray -- a conflict return or a deleted card, waiting for a human.
GIT_OWNED = ("running", "review", "blocked", "triage")


def git_info(folder, active_cards=()):
    """The space's branch row (herdr workspace/git/status.rs, subprocess flavour): branch
    (detached shows @oid), uncommitted-change count, ahead/behind the upstream when one
    exists, and stray card/* branches no in-flight card owns. None outside git repos."""
    import subprocess

    def git(*args):
        try:
            done = subprocess.run(["git", "-C", folder, *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=2, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    # One status call carries branch, upstream distance and the change list; the ref scan is
    # the only other thing needed. This used to be five subprocesses per folder per poll.
    report = git("status", "--porcelain=v2", "--branch")
    if report is None:
        return None
    branch, oid, ahead, behind, dirty = None, None, 0, 0, 0
    for line in report.splitlines():
        if not line.startswith("# "):
            dirty += 1
            continue
        key, _, value = line[2:].partition(" ")
        if key == "branch.head":
            branch = value.strip()
        elif key == "branch.oid":
            oid = value.strip()
        elif key == "branch.ab":
            parts = value.split()
            ahead = int(parts[0]) if parts and parts[0].lstrip("+-").isdigit() else 0
            behind = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("+-").isdigit() else 0
    if not branch:
        return None
    if branch == "(detached)":
        branch = "@" + (oid or "")[:7]
    refs = git("for-each-ref", "refs/heads/card/", "--format=%(refname:short)") or ""
    stray = sum(1 for ref in refs.split()
                if ref.removeprefix("card/") not in active_cards)
    return {"branch": branch, "dirty": dirty, "ahead": abs(ahead), "behind": abs(behind), "stray": stray}


def _poll_git_cache(now, polled, active, cache, jobs, submit):
    """One turn of the sidebar's git bookkeeping: collect finished probes, start stale ones.

    ``git_info`` spawns two subprocesses per folder, each with a two-second timeout, and it
    used to be called straight from the panel's poll: a large repo or a network filesystem
    froze the whole panel -- no keystrokes read, no pane frames drained -- for seconds at a
    time (audit 2026-09-02, ui-panel-16). Nothing here runs git; ``submit`` hands the probe
    to a worker and the next turn picks the answer up. ``git_info`` reads nothing but its two
    arguments, so the worker never touches panel state.
    """
    for folder, job in [(f, j) for f, j in jobs.items() if j.done()]:
        del jobs[folder]
        try:
            cache[folder] = {"at": now, "info": job.result()}
        except Exception:  # noqa: BLE001 - a folder whose probe failed simply has no branch row
            cache[folder] = {"at": now, "info": None}
    for folder in polled:
        entry = cache.get(folder)
        if (entry and now - entry["at"] < GIT_TTL_SECONDS) or folder in jobs:
            continue
        jobs[folder] = submit(git_info, folder, active)
    for folder in [f for f in cache if f not in polled and f not in jobs]:
        del cache[folder]


def _without_dead(tree, dead):
    """Drop the leaves for panes the daemon's last listing reported dead, and only those.

    Pruning against a positive "alive" list instead dropped every pane created between two
    polls -- a Sister Last Order summoned, a fork, a research node -- and the next
    ``push_layout`` handed the daemon a layout that omitted it. The daemon reseats an omitted
    live pane, which means a brand-new space (audit 2026-09-02, ui-panel-15).
    """
    for pid in (hui.pane_ids(tree) if tree else []):
        if pid in dead:
            tree = hui.remove_pane(tree, pid) if tree else None
    return tree


def _sorted_agents(agents, sort):
    """app/state.rs AgentPanelSort: "grouped" keeps space order; "priority" puts the entries
    that need attention first (stable, so ties keep the grouped order)."""
    if sort != "priority":
        return list(agents)
    return sorted(agents, key=lambda a: -hui.attention_priority(a["state"], a["seen"]))


def _put_tokens(canvas, x, y, tokens, max_width, clip):
    """ui/sidebar.rs resolved_token_spans, the drawing half: separators in overlay0 (no DIM
    since herdr #4062: DIM on overlay0 was unreadable on dark themes), each token in its own
    style, widths from geometry.fit_tokens. ``tokens`` = [(kind, text, style kwargs)]."""
    for index, sep, shown in hui.fit_tokens([(k, t) for k, t, _s in tokens], max_width):
        if sep:
            canvas.put(x, y, sep, fg=hui.OVERLAY0, clip=clip)
            x += _wcwidth(sep)
        canvas.put(x, y, shown, clip=clip, **tokens[index][2])
        x += _wcwidth(shown)


def _put_scrollbar(canvas, metrics, track):
    """scrollbar.rs:135-162 render_scrollbar as the sidebar calls it (sidebar.rs:1192, 1315):
    track and thumb are both "▕", surface_dim under overlay0."""
    thumb = hui.scrollbar_thumb(metrics, track)
    if thumb is None:
        return
    for y in range(track.y, track.y + track.height):
        canvas.put(track.x, y, "▕", fg=hui.PALETTE["surface_dim"])
    top, length = thumb
    for y in range(top, min(top + length, track.y + track.height)):
        canvas.put(track.x, y, "▕", fg=hui.OVERLAY0)


def _git_row_tokens(git, active):
    """The branch row's tokens (sidebar.rs default rows Branch + GitStatus, 1311-1315 branch
    style, tokens.rs GitStatus spans: ↑ green, ↓ red). MISAKA additions: ·n uncommitted
    changes beside the branch, ⚑n stray card/* branches in red (cards waiting for a human).
    The active-space highlight is the theme's plum, not herdr's slot (this theme's accent is
    a red and the user read it as an alarm)."""
    P = hui.PALETTE
    branch_style = {"fg": P["plum"] if active else hui.OVERLAY0}   # herdr: mauve / overlay0
    tokens = [("text", git["branch"], branch_style)]
    if git["dirty"]:
        tokens.append(("git", f"·{git['dirty']}", branch_style))
    if git["ahead"]:
        tokens.append(("git", f"↑{git['ahead']}", {"fg": P["green"]}))
    if git["behind"]:
        tokens.append(("git", f"↓{git['behind']}", {"fg": P["red"]}))
    if git["stray"]:
        tokens.append(("git", f"⚑{git['stray']}", {"fg": P["red"]}))
    return tokens


def _render_spaces(canvas, spaces, area, scroll, hits, reveal=False):
    """sidebar.rs:1040-1183 render_workspace_list with the default rows: state icon + name,
    then branch + git status when the space's folder is a repo (``space["git"]``).
    Returns the section's scroll state for the wheel. ``reveal`` (client/shell/sidebar.rs
    reveal_focused_workspace): scroll as little as needed to show the active space; it is
    reported consumed (``revealed``) only when the list had a body to show it in."""
    P = hui.PALETTE
    list_bottom = area.y + max(0, area.height - 1)
    if area.height > 0:
        canvas.put(area.x, area.y, " spaces", fg=hui.OVERLAY0, bold=True,
                   clip=area.x + area.width)
    heights = [2 if space.get("git") else 1 for space in spaces]
    body = hui.workspace_list_body_rect(area, False)
    metrics = hui.list_scroll_metrics(heights, body.height, scroll)
    revealed = False
    if reveal and body.height > 0 and body.width > 0:
        revealed = True
        target = next((index for index, space in enumerate(spaces) if space["active"]), None)
        if target is not None:
            scroll = hui.list_scroll_start_to_reveal(heights, body.height, scroll, target)
            metrics = hui.list_scroll_metrics(heights, body.height, scroll)
    scroll = min(scroll, metrics["max_offset_from_bottom"])
    has_bar = hui.should_show_scrollbar(metrics) and body.width > 0 and body.height > 0
    body = hui.workspace_list_body_rect(area, has_bar)
    row_y, body_bottom = body.y, body.y + body.height
    for space, want in zip(spaces[scroll:], heights[scroll:]):
        height = min(want, body_bottom - row_y)     # herdr clips a partly visible entry
        if height <= 0:
            break
        card = hui.Rect(body.x, row_y, body.width, height)
        if space["active"] and row_y < list_bottom:     # 1082-1091: the active space sits on surface_dim
            for y in range(row_y, min(row_y + height, list_bottom + 1)):
                canvas.fill_bg(card.x, card.x + card.width, y, P["surface_dim"])
        name = ({"fg": hui.TEXT, "bold": True} if space["active"]
                else {"fg": hui.OVERLAY0} if space.get("dormant")    # MISAKA: remembered, no panes
                else {"fg": P["subtext0"]})                # 1094-1098
        glyph, color = hui.state_dot(space["state"], space["seen"])
        canvas.put(card.x, row_y, " ", clip=card.x + card.width)   # 1137-1139: one-column prefix
        _put_tokens(canvas, card.x + 1, row_y,
                    [("icon", glyph, {"fg": color}), ("text", space["label"], name)],
                    max(0, card.width - 1), card.x + card.width)
        if height > 1:                                  # 1351-1356: later rows indent three columns
            _put_tokens(canvas, card.x + 3, row_y + 1,
                        _git_row_tokens(space["git"], space["active"]),
                        max(0, card.width - 3), card.x + card.width)
        hits.append((card, ("space", space["key"])))
        row_y += height
    if has_bar:
        _put_scrollbar(canvas, metrics,
                       hui.Rect(area.x + area.width - 1, body.y, 1, body.height))
    if list_bottom > area.y:                              # 1196-1218 footer (mouse_capture is always on here)
        new_rect = hui.sidebar_new_button_rect(area)
        canvas.put(new_rect.x, new_rect.y, " new", fg=hui.OVERLAY0,
                   clip=new_rect.x + new_rect.width)
        hits.append((new_rect, ("new",)))
        # herdr puts its global menu here (Settings/Keybinds/Reload/Detach) -- all panel-level
        # things MISAKA deliberately lacks. A mouse entry into the prefix layer is useful instead.
        prefix_rect = hui.global_launcher_rect(area, "prefix")
        canvas.put(prefix_rect.x + max(0, prefix_rect.width - 6), prefix_rect.y, "prefix",
                   fg=hui.OVERLAY0, clip=prefix_rect.x + prefix_rect.width)
        hits.append((prefix_rect, ("prefix",)))
    return {"rect": area, "body": hui.workspace_list_body_rect(area, False), "scroll": scroll,
            "max_scroll": metrics["max_offset_from_bottom"], "revealed": revealed}


def _render_agents(canvas, agents, area, scroll, sort, hits):
    """sidebar.rs:1189-1318 render_agent_detail with the default rows: state icon + space
    (+ tab number when there are several tabs), then the agent name on a second line.
    MISAKA addition: the last row is a footer that summons someone into the current tab —
    " new" for a fresh Last Order session, " sisters" for the roster."""
    P = hui.PALETTE
    state = {"rect": area, "scroll": 0, "max_scroll": 0}
    if area.height < 2:
        return state
    clip = area.x + area.width
    canvas.put(area.x, area.y, "─" * area.width, fg=P["surface_dim"], clip=clip)
    canvas.put(area.x, area.y + 1, " agents", fg=hui.OVERLAY0, bold=True, clip=clip)
    if area.height < 3:
        return state
    label = "priority" if sort == "priority" else "grouped"     # 86-91 agent_panel_sort_label
    toggle = hui.agent_panel_header_label_rect(area, label)
    if toggle != hui.RECT_DEFAULT:
        canvas.put(toggle.x, toggle.y, label, fg=hui.OVERLAY0, bold=True,
                   clip=toggle.x + toggle.width)
        hits.append((toggle, ("sort",)))
    heights = [2] * len(agents)
    footer = area.height >= 4                          # MISAKA: the last row holds " sisters"
    body = hui.agent_panel_body_rect(area, False)
    body = hui.Rect(body.x, body.y, body.width, max(0, body.height - int(footer)))
    state["body"] = body                               # client/shell/mouse.rs: the wheel scrolls the body only
    metrics = hui.list_scroll_metrics(heights, body.height, scroll)
    scroll = min(scroll, metrics["max_offset_from_bottom"])
    has_bar = hui.should_show_scrollbar(metrics) and body.width > 0 and body.height > 0
    body = hui.agent_panel_body_rect(area, has_bar)
    body = hui.Rect(body.x, body.y, body.width, max(0, body.height - int(footer)))
    state.update(scroll=scroll, max_scroll=metrics["max_offset_from_bottom"])
    if body == hui.RECT_DEFAULT:
        return state
    row_y, body_bottom = body.y, body.y + body.height
    agent_style = {"fg": hui.OVERLAY0}          # herdr adds DIM here; terminals sink it into the background
    for entry in agents[scroll:]:
        height = min(2, body.height)
        if row_y + height > body_bottom:
            break
        glyph, color = hui.state_dot(entry["state"], entry["seen"])
        if entry["active"]:                                   # Paragraph.style(row_style): the whole row
            for y in range(row_y, row_y + height):
                canvas.fill_bg(body.x, body.x + body.width, y, P["surface_dim"])
        name = ({"fg": hui.TEXT, "bold": True} if entry["active"]
                else {"fg": P["subtext0"], "bold": True})
        first = [("icon", glyph, {"fg": color}), ("text", entry["space"], name)]
        if entry["tab"]:
            first.append(("text", entry["tab"], agent_style))
        lines = [(" ", first), ("   ", [("text", entry["agent"], agent_style)])]
        for offset, (prefix, tokens) in enumerate(lines[:height]):
            canvas.put(body.x, row_y + offset, prefix, clip=body.x + body.width)
            _put_tokens(canvas, body.x + len(prefix), row_y + offset, tokens,
                        max(0, body.width - len(prefix)), body.x + body.width)
        hits.append((hui.Rect(body.x, row_y, body.width, height), ("pane", entry["pane"])))
        row_y += height
    if has_bar:
        _put_scrollbar(canvas, metrics,
                       hui.Rect(area.x + area.width - 1, body.y, 1, body.height))
    if footer:
        new_rect = hui.sidebar_new_button_rect(area)     # a new Last Order session in this folder
        canvas.put(new_rect.x, new_rect.y, " new", fg=hui.OVERLAY0, clip=new_rect.x + new_rect.width)
        hits.append((new_rect, ("sessnew",)))
        launcher = hui.global_launcher_rect(area, SISTERS_LABEL)
        launcher = hui.Rect(max(area.x, launcher.x - 2), launcher.y, launcher.width, launcher.height)  # leave the "«" its cell
        canvas.put(launcher.x + max(0, launcher.width - _wcwidth(SISTERS_LABEL)), launcher.y,
                   SISTERS_LABEL, fg=hui.OVERLAY0, clip=launcher.x + launcher.width)
        hits.append((launcher, ("sisters",)))
    return state


def _render_collapsed(canvas, spaces, sessions, agents, area, hits):
    """sidebar.rs:790-904 render_sidebar_collapsed: a numbered space glance on top, then a
    divider and a numbered glance per further section — MISAKA mirrors the expanded split,
    so sessions sit between spaces and agents; "»" expands again."""
    P = hui.PALETTE
    sections, dividers = hui.collapsed_sidebar_sections(area, SECTION_WEIGHTS)
    areas = dict(sections)
    ws_area = areas.get("spaces", hui.RECT_DEFAULT)
    if ws_area != hui.RECT_DEFAULT:
        clip = ws_area.x + ws_area.width
        for index, space in enumerate(spaces):
            y = ws_area.y + index
            if y >= ws_area.y + ws_area.height:
                break
            glyph, color = hui.state_dot(space["state"], space["seen"])
            if space["active"]:
                canvas.fill_bg(ws_area.x, clip, y, P["surface_dim"])
            number = str(index + 1)
            canvas.put(ws_area.x, y, number,
                       fg=hui.TEXT if space["active"] else hui.OVERLAY0, clip=clip)
            canvas.put(ws_area.x + len(number) + 1, y, glyph, fg=color, clip=clip)
            hits.append((hui.Rect(ws_area.x, y, ws_area.width, 1), ("space", space["key"])))
        for divider_y in dividers:
            canvas.put(ws_area.x, divider_y, "─" * ws_area.width, fg=P["surface_dim"], clip=clip)
    sess_area = areas.get("sessions", hui.RECT_DEFAULT)
    if sess_area != hui.RECT_DEFAULT:
        clip = sess_area.x + sess_area.width
        items = [row for row in sessions if row["kind"] == "item"]
        for index, entry in enumerate(items[:sess_area.height]):
            y = sess_area.y + index
            if entry.get("active"):
                canvas.fill_bg(sess_area.x, clip, y, P["surface_dim"])
            canvas.put(sess_area.x, y, f"{index + 1:<2}",
                       fg=hui.TEXT if entry.get("active") else hui.OVERLAY0, clip=clip)
            if entry.get("pane"):              # open in a pane: its state dot, like expanded
                glyph, color = hui.state_dot(entry["state"], entry["seen"])
            else:
                glyph, color = "·", hui.OVERLAY0
            canvas.put(sess_area.x + 2, y, glyph, fg=color, clip=clip)
            hits.append((hui.Rect(sess_area.x, y, sess_area.width, 1), entry["action"]))
    detail_area = areas.get("agents", hui.RECT_DEFAULT)
    if detail_area != hui.RECT_DEFAULT:
        content = hui.Rect(detail_area.x, detail_area.y, detail_area.width,
                           max(0, detail_area.height - 1))     # the last row keeps the toggle
        for index, entry in enumerate(agents[:content.height]):
            y = content.y + index
            glyph, color = hui.state_dot(entry["state"], entry["seen"])
            canvas.put(content.x, y, f"{index + 1:<2}", fg=hui.OVERLAY0,
                       clip=content.x + content.width)
            canvas.put(content.x + 2, y, glyph, fg=color, clip=content.x + content.width)
            hits.append((hui.Rect(content.x, y, content.width, 1), ("pane", entry["pane"])))
    toggle = hui.collapsed_sidebar_toggle_rect(area)
    canvas.put(toggle.x, toggle.y, "»", fg=hui.OVERLAY0)
    hits.append((toggle, ("toggle",)))


def menu_scroll_for(highlighted, scroll, visible):
    """Keep the highlighted item inside the popup's window of ``visible`` rows (herdr clips its
    menu; scrolling is MISAKA's addition for long rosters). Pure, so testable."""
    if visible <= 0:
        return 0
    if highlighted < scroll:
        return highlighted
    if highlighted >= scroll + visible:
        return highlighted - visible + 1
    return scroll


def format_menu_popup(labels, highlighted, scroll, rect):
    """herdr client/shell/overlays.rs render_global_menu on a panel shell: a plain accent
    border on panel_bg, one " label" per row in text color; the highlighted row is accent
    across the whole inner width, its label in the contrast color, bold. Rows from ``scroll`` fill the inner height.
    Returns ``(rows, hits)``: rows = [(y, x, ansi)] in screen cells, hits = [(Rect, index)].
    Pure, so testable."""
    if rect.width < 2 or rect.height < 2:
        return [], []
    width, height = rect.width, rect.height
    canvas = _Canvas(width, height)
    for y in range(height):
        canvas.fill_bg(0, width, y, hui.PANEL_BG)
    canvas.put(0, 0, "┌" + "─" * (width - 2) + "┐", fg=hui.ACCENT)
    for y in range(1, height - 1):
        canvas.put(0, y, "│", fg=hui.ACCENT)
        canvas.put(width - 1, y, "│", fg=hui.ACCENT)
    canvas.put(0, height - 1, "└" + "─" * (width - 2) + "┘", fg=hui.ACCENT)
    inner_w = width - 2
    hits = []
    for row, index in enumerate(range(scroll, min(len(labels), scroll + height - 2))):
        y = 1 + row
        if index == highlighted:                  # client/shell/overlays.rs: the whole row is lit
            canvas.fill_bg(1, 1 + inner_w, y, hui.ACCENT)
            canvas.put(1, y, f" {labels[index]}", fg=hui.panel_contrast_fg(), bg=hui.ACCENT,
                       bold=True, clip=1 + inner_w)
        else:
            canvas.put(1, y, f" {labels[index]}", fg=hui.TEXT, clip=1 + inner_w)
        hits.append((hui.Rect(rect.x + 1, rect.y + y, inner_w, 1), index))
    return [(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], hits


# ── Navigator: herdr client/shell/aggregate_navigation.rs + overlays.rs (prefix+g) ──

NAV_FILTERS = {"b": "blocked", "w": "working", "i": "idle", "d": "done"}
NAV_PAGE = 8             # overlay_input.rs: ctrl+d / ctrl+u move the selection eight rows
NAV_WHEEL = 3            # mouse.rs: the wheel moves the selection three rows


def navigator_popup_rect(screen):
    """overlays.rs render_navigator_overlay: centred on the whole screen, min(W-4, 116)
    wide and min(H-2, 42) tall; None below 4x9."""
    width, height = min(max(0, screen.width - 4), 116), min(max(0, screen.height - 2), 42)
    if width < 4 or height < 9:
        return None
    return hui.Rect(screen.x + (screen.width - width) // 2, screen.y + (screen.height - height) // 2,
                    width, height)


def _nav_status_text(state, seen):
    """client/shell.rs status_text over herdr's AgentStatus (done = idle and unseen)."""
    if state in ("blocked", "working"):
        return state
    if state == "idle":
        return "idle" if seen else "done"
    return "unknown"


def navigator_rows(spaces, listing, focused_id, *, query="", state_filter=None, git=None):
    """aggregate_navigation.rs navigator_rows: one row per space, then every terminal in
    it, agent or bare shell. The query is split on whitespace and every word must appear
    (case-insensitive) in one field: a space's name or branch keeps all its panes, so does
    a tab's name; a pane matches on its label, cwd, agent kind or id. A state filter keeps
    only panes in that state; a space row stays when it keeps a pane, or when the query hit
    its own name and no filter is on.

    MISAKA mapping of herdr's pane fields: the name is the pane's title (what the session
    or card called it) unless it is the bare-shell marker ``shell``; the agent kind is the
    ally the daemon recognised, or ``misaka`` for MISAKA's own sessions; the status is
    ``pane_state`` (the board and the busy heuristic included). ``git(folder)`` gives the
    branch. Rows: {"target", "depth", "label", "meta", "detail", "agent", "state", "seen",
    "status", "current", "message"}. Pure, so testable."""
    needle = query.strip().lower()
    words = needle.split()

    def text(value):
        if not words:
            return True
        value = (value or "").lower()
        return all(word in value for word in words)

    filtering = state_filter is not None or bool(needle)
    by_id = {pane["id"]: pane for pane in listing}
    space_rows = {row["key"]: row for row in sidebar_model(spaces, listing, focused_id, None)[0]}
    rows = []
    for space in spaces:
        head = space_rows[space["id"]]
        branch = ((git(head["folder"]) if git else None) or {}).get("branch")
        workspace_matches = text(head["label"]) or (branch is not None and text(branch))
        children, multiple_tabs = [], len(space["tabs"]) > 1
        for index, tree in enumerate(space["tabs"]):
            custom = space["tab_names"][index] if index < len(space["tab_names"]) else None
            tab_label = custom or str(index + 1)
            tab_matches = workspace_matches or text(tab_label)
            panes_ = [by_id[pid] for pid in hui.pane_ids(tree) if pid in by_id]
            for position, pane in enumerate(panes_):
                state, seen = pane_state(pane)
                ally = pane.get("ally") or None
                own = pane["title"] and pane["title"] != "shell"
                agent_kind = ally or ("misaka" if own else None)
                name = pane["title"] if own else None
                tab_name = custom if custom is not None else None
                if len(panes_) == 1:
                    if name or tab_name:
                        label = name or tab_name
                    elif multiple_tabs:
                        label = f"{agent_kind or 'terminal'} · {tab_label}"
                    else:
                        label = head["label"]
                else:
                    pane_name = name or agent_kind or "terminal"
                    if tab_name is not None and tab_name != pane_name:
                        label = f"{tab_name} · {pane_name} · {position + 1}"
                    else:
                        label = f"{pane_name} · {position + 1}"
                meta = (pane.get("foreground") or {}).get("cwd") or pane.get("cwd") or ""
                state_hit = state_filter is None or _nav_status_text(state, seen) == state_filter
                if state_hit and (tab_matches or text(label) or text(meta)
                                  or (pane.get("cwd") is not None and text(pane["cwd"]))
                                  or (agent_kind is not None and text(agent_kind))
                                  or text(pane["id"])):
                    children.append({
                        "target": ("pane", pane["id"]), "depth": 1, "label": label, "meta": meta,
                        "detail": f"{head['label']} / {tab_label} / {pane['id']}",
                        "agent": agent_kind, "state": state, "seen": seen, "status": True,
                        "current": pane["id"] == focused_id,
                        "message": (pane.get("reported") or {}).get("message") or ""})
        if not filtering or children or (state_filter is None and needle and workspace_matches):
            rows.append({"target": ("space", space["id"]), "depth": 0, "label": head["label"],
                         "meta": branch or "", "detail": head["folder"], "agent": None,
                         "state": None, "seen": True, "status": False, "current": False,
                         "message": ""})
            rows.extend(children)
    return rows


def navigator_selected_index(rows, selected):
    """aggregate_navigation.rs navigator_selected_index: the row holding the selected target;
    with nothing selected, the first pane row (else the first row). None: nothing to show,
    or the selected target left the list."""
    if selected is not None:
        return next((i for i, row in enumerate(rows) if row["target"] == selected), None)
    first = next((i for i, row in enumerate(rows) if row["target"][0] == "pane"), None)
    return first if first is not None else (0 if rows else None)


def navigator_scroll(rows, selected_index, scroll, body_h):
    """overlays.rs: the list offset a frame draws -- the stored scroll, pulled just far
    enough to show the selection, never past the end. Returns (scroll, max)."""
    top = max(0, len(rows) - body_h)
    return min(max(scroll, selected_index - max(0, body_h - 1)), selected_index, top), top


def format_navigator(rows, selected, scroll, rect, editor, *, search_focused=False, state_filter=None):
    """overlays.rs render_navigator_overlay: an accent panel titled " Go to "; the search
    line (" / " + query, filter or placeholder; the text editor while focused) with the
    terminal count on the right; a surface1 rule; the rows; the selected row's detail
    (subtext0) and meta (overlay0) lines; the overlay0 footer.

    MISAKA: the selected row sits on surface0 in the text colour, bold, where herdr lights
    it accent (this theme's accent is a red the user reads as an alarm; the sidebar's
    active space is plum for the same reason); the meta line carries the session's own
    report after the cwd. Returns ``{"rows", "hits", "caret", "search", "track", "metrics"}``:
    rows = [(y, x, ansi)], hits = [(Rect, target)], screen coordinates. Pure, so testable."""
    P = hui.PALETTE
    width, height = rect.width, rect.height
    canvas = _Canvas(width, height)
    for y in range(height):
        canvas.fill_bg(0, width, y, hui.PANEL_BG)
    canvas.put(0, 0, "┌" + "─" * (width - 2) + "┐", fg=hui.ACCENT)
    for y in range(1, height - 1):
        canvas.put(0, y, "│", fg=hui.ACCENT)
        canvas.put(width - 1, y, "│", fg=hui.ACCENT)
    canvas.put(0, height - 1, "└" + "─" * (width - 2) + "┘", fg=hui.ACCENT)
    canvas.put(2, 0, " Go to ", fg=hui.ACCENT, bg=hui.PANEL_BG, clip=2 + max(0, width - 4))
    ix, iy, iw, ih = 1, 1, width - 2, height - 2
    right = ix + iw
    if search_focused:
        search = " / "
    elif state_filter:
        search = f" / {state_filter}"
    elif not editor.text:
        search = " / search agents and terminals"
    else:
        search = f" / {editor.text}"
    terminals = sum(1 for row in rows if row["target"][0] == "pane")
    count = f"{terminals} {'terminal' if terminals == 1 else 'terminals'}"
    count_w = _wcwidth(count)
    canvas.put(ix, iy, search, fg=hui.TEXT if search_focused else hui.OVERLAY0,
               clip=ix + max(0, iw - (count_w + 1)))
    caret = None
    if search_focused:
        caret = _put_editor(canvas, ix + 3, iy, max(0, iw - (4 + count_w)), editor, hui.TEXT, hui.PANEL_BG)
    canvas.put(right - min(count_w, iw), iy, count, fg=hui.OVERLAY0, clip=right)
    canvas.put(ix, iy + 1, "─" * iw, fg=P["surface1"], clip=right)
    body_y, body_h = iy + 2, max(0, ih - 5)
    index = navigator_selected_index(rows, selected) or 0
    top, most = navigator_scroll(rows, index, scroll, body_h)
    metrics = {"offset_from_bottom": most - top, "max_offset_from_bottom": most, "viewport_rows": body_h}
    track = hui.Rect(right - 1, body_y, 1, body_h) if most > 0 and iw > 1 else None
    row_w = iw - (1 if track else 0)
    hits = []
    if not rows:
        canvas.put(ix, body_y, " No matching agents or terminals", fg=hui.OVERLAY0, clip=right)
    for offset, at in enumerate(range(top, min(len(rows), top + body_h))):
        row, y = rows[at], body_y + offset
        hits.append((hui.Rect(rect.x + ix, rect.y + y, row_w, 1), row["target"]))
        is_selected = at == index
        style = ({"fg": hui.TEXT, "bg": hui.SURFACE0, "bold": True} if is_selected
                 else {"fg": hui.TEXT, "bg": hui.PANEL_BG, "bold": False})
        is_pane = row["target"][0] == "pane"
        if not is_pane:
            connector = ""
        elif at + 1 < len(rows) and rows[at + 1]["target"][0] == "pane":
            connector = "├─ "
        else:
            connector = "└─ "
        padding = max(0, row["depth"] - (1 if is_pane else 0)) * 2 + 1
        indent = " " * padding + connector
        current = "◆ " if row["current"] else ""
        dot, dot_color = hui.state_dot(row["state"], row["seen"]) if row["status"] else ("", None)
        label = f"{indent}{current}{dot}{' ' if dot else ''}{row['label']}"
        bold = style["bold"] or not row["status"]
        canvas.fill_bg(ix, ix + row_w, y, style["bg"])
        for col in range(ix, ix + row_w):
            canvas.cells[y][col][1], canvas.cells[y][col][3] = style["fg"], bold
        columns = (24 if row_w >= 64 else 12 if row_w >= 36 else 0) if row["status"] else 0
        canvas.put(ix, y, label, fg=style["fg"], bg=style["bg"], bold=bold, clip=ix + max(0, row_w - columns))
        dim_fg = style["fg"] if is_selected else hui.OVERLAY0
        if is_pane:
            connector_x = ix + padding
            canvas.put(connector_x, y, connector, fg=dim_fg, bg=style["bg"], bold=bold,
                       clip=connector_x + min(2, max(0, ix + row_w - connector_x)))
        if row["status"]:
            canvas.put(ix + _wcwidth(indent + current), y, dot,
                       fg=style["fg"] if is_selected else dot_color, bg=style["bg"], bold=bold,
                       clip=ix + _wcwidth(indent + current) + _wcwidth(dot))
            if columns > 0:
                x = ix + row_w - columns + 1
                canvas.put(x, y, row["agent"] or "terminal", fg=dim_fg, bg=style["bg"], bold=bold, clip=x + 11)
            if columns == 24:
                x = ix + row_w - 11
                word = _nav_status_text(row["state"], row["seen"]) if row["agent"] else "shell"
                canvas.put(x, y, word, fg=dim_fg, bg=style["bg"], bold=bold, clip=x + 11)
        elif row["meta"]:
            label_w = min(_wcwidth(label), row_w)
            area_x, area_w = ix + label_w + 1, max(0, row_w - (label_w + 1))
            meta_w = min(_wcwidth(row["meta"]), area_w)
            canvas.put(area_x + area_w - meta_w, y, row["meta"], fg=style["fg"], bg=style["bg"],
                       bold=bold, clip=area_x + area_w)
    if track is not None:
        thumb = hui.scrollbar_thumb(metrics, track)
        if thumb is not None:
            for y in range(track.y, track.y + track.height):
                canvas.put(track.x, y, "▕", fg=hui.OVERLAY0)
            for y in range(thumb[0], thumb[0] + thumb[1]):
                canvas.put(track.x, y, "▐", fg=hui.OVERLAY1)
    if rows and index < len(rows):
        chosen = rows[index]
        canvas.put(ix, iy + ih - 3, f" {chosen['detail']}", fg=P["subtext0"], bg=hui.PANEL_BG, clip=right)
        meta = " · ".join(part for part in (chosen["meta"], chosen["message"]) if part)
        canvas.put(ix, iy + ih - 2, f" {meta}", fg=hui.OVERLAY0, bg=hui.PANEL_BG, clip=right)
    footer = (" search type · move ↑↓/ctrl+n/p · open enter · back esc" if search_focused else
              " ↑↓/j/k rows · ←→ workspace · / search · a/b/w/i/d filter · enter open · esc close")
    canvas.put(ix, iy + ih - 1, footer, fg=hui.OVERLAY0, bg=hui.PANEL_BG, clip=right)
    return {"rows": [(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], "hits": hits,
            "caret": None if caret is None else (rect.x + caret, rect.y + iy),
            "search": hui.Rect(rect.x + ix, rect.y + iy, iw, 1),
            "track": None if track is None else hui.Rect(rect.x + track.x, rect.y + track.y, 1, track.height),
            "metrics": metrics}


SECTION_WEIGHTS = (("spaces", 0.9), ("sessions", 1.3), ("agents", 1.1))


STAMP_W = 5              # the right column of a session row; the title keeps the rest


def session_stamp(when, now=None):
    """Age of a session in at most five columns: 12m, 3h, 5d, then the date. A full
    timestamp would eat half of a 26-column sidebar."""
    delta = (now if now is not None else time.time()) - when
    if delta < 3600:
        return f"{max(1, int(delta // 60))}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d"
    return time.strftime("%m-%d", time.localtime(when))


def nest_forks(items):
    """Seat forks under the session they branched from (header ``parentSession``): roots
    keep their newest-first order, a fork chain flattens to one indent level under its
    oldest listed ancestor; a fork whose source is not in the list stays a root.
    Pure, so testable."""
    by_path = {item["path"]: item for item in items if item.get("path")}

    def root_of(item):
        seen, current = set(), item
        while True:
            parent = current.get("parent")
            if not parent or parent not in by_path or parent in seen:
                return current
            seen.add(parent)
            current = by_path[parent]

    children, roots = {}, []
    for item in items:
        root = root_of(item)
        if root is item:
            roots.append(item)
        else:
            children.setdefault(root["path"], []).append(item)
    out = []
    for root in roots:
        out.append(root)
        out += [{**child, "indent": True} for child in children.get(root.get("path"), [])]
    return out


def session_rows(groups, folded_groups=()):
    """Rows for the sessions section: foldable groups of sessions, ``groups`` =
    [(key, label, [{"label", "when", "action", ...}])] newest first within a group. "here"
    shows a Last Order group over a Sisters group; "all" one group per folder. Forks nest
    under their source (nest_forks). Pure, so testable."""
    rows = []
    for key, label, items in groups:
        folded = key in folded_groups
        rows.append({"kind": "group", "key": key, "label": label,
                     "right": str(len(items)), "folded": folded, "action": ("sessgroup", key)})
        if not folded:
            rows.extend({"kind": "item", **item} for item in nest_forks(items))
    return rows


def folder_groups(items):
    """"all" mode: the same session items grouped by the folder they worked in (``item["folder"]``),
    folders ordered by their newest session, items newest first. Pure, so testable."""
    by_folder = {}
    for item in sorted(items, key=lambda item: -item.get("_t", 0)):
        by_folder.setdefault(item["folder"], []).append(item)
    user_home = os.path.expanduser("~")
    return [(folder, folder.replace(user_home, "~", 1) if folder.startswith(user_home) else folder, members)
            for folder, members in by_folder.items()]


def session_key(action):
    """A canonical transcript path (or ephemeral inventory record), independent of kind."""
    return os.path.realpath(action[-1])

def session_arg(argv):
    """The session file a pane was launched on (``--session <path>``). Known the moment the
    pane exists, long before the session inside reports itself (core/network/wiring/panel.py
    needs the engine up, which can take a while): until this, clicking a starting session
    again opened it a second time. Pure, so testable."""
    argv = list(argv or ())
    return next((argv[argv.index(flag) + 1] for flag in ("--session", "--catalog") if flag in argv[:-1]), None)


def open_session_panes(listing):
    """session identity -> the live pane writing it: the card it runs, the session it reports
    (core/network/wiring/panel.py), or failing that the one it was launched on. One session,
    one tab."""
    live = {}
    for pane in listing:
        if not pane.get("alive"):
            continue
        if pane.get("card"):
            live[("card", pane["card"])] = pane["id"]
        argv = list(pane.get("argv") or ())
        path = (pane.get("reported") or {}).get("session") or session_arg(argv)
        if path:
            if {"--read-only", "--attach"} & set(argv):
                live.setdefault(os.path.realpath(path), pane["id"])
            else:
                live[os.path.realpath(path)] = pane["id"]
    return live


def session_pane_id(action, live):
    return live.get(session_key(action))

def mark_open_sessions(rows, listing, focused):
    """Mark open conversations; only a writer pane overrides the session's state dot."""
    live = open_session_panes(listing)
    by_id = {pane["id"]: pane for pane in listing}
    out = []
    for row in rows:
        pane_id = (session_pane_id(row["action"], live) or live.get(tuple(row.get("live_key") or ()))
                   if row["kind"] == "item" else None)
        if pane_id:
            row = {**row, "pane": pane_id, "active": pane_id == focused}
            # An attached/history window owns no model. Window lifetime is not
            # agent lifetime, even when that window can send the original agent input.
            if not ({"--read-only", "--attach"} & set(by_id[pane_id].get("argv") or ())):
                state, seen = pane_state(by_id[pane_id])
                row.update(state=state, seen=seen)
        out.append(row)
    return out


def _render_sessions(canvas, entries, area, scroll, hits, mode="here"):
    """MISAKA section: past sessions, grouped Last Order / Sisters for this space ("here") or
    by folder ("all"); the groups fold, the section does not. The header's right end is the
    here/all toggle, drawn like the agents header's sort toggle. Same chrome as that section."""
    P = hui.PALETTE
    state = {"rect": area, "scroll": 0, "max_scroll": 0}
    if area.height < 2:
        return state
    clip = area.x + area.width
    canvas.put(area.x, area.y, "─" * area.width, fg=P["surface_dim"], clip=clip)
    canvas.put(area.x, area.y + 1, " sessions", fg=hui.OVERLAY0, bold=True, clip=clip)
    toggle = hui.agent_panel_header_label_rect(area, mode)
    if toggle != hui.RECT_DEFAULT:
        canvas.put(toggle.x, toggle.y, mode, fg=hui.OVERLAY0, clip=toggle.x + toggle.width)
        hits.append((toggle, ("sessmode",)))
    if area.height < 3:
        return state
    body = hui.Rect(area.x, area.y + 2, area.width, area.height - 2)   # divider and header above
    state["body"] = body
    metrics = hui.list_scroll_metrics([1] * len(entries), body.height, scroll)
    scroll = min(scroll, metrics["max_offset_from_bottom"])
    has_bar = hui.should_show_scrollbar(metrics) and body.height > 0
    body = hui.Rect(body.x, body.y, body.width - int(has_bar), body.height)
    state.update(scroll=scroll, max_scroll=metrics["max_offset_from_bottom"])
    for offset, entry in enumerate(entries[scroll:scroll + body.height]):
        y = body.y + offset
        if entry["kind"] == "group":
            caret = "▸" if entry["folded"] else "▾"
            canvas.put(body.x, y, f" {caret} {entry['label']}", fg=hui.OVERLAY0, bold=True,
                       clip=body.x + body.width)
            right = entry["right"]
            canvas.put(body.x + body.width - _wcwidth(right) - 1, y, right, fg=hui.OVERLAY0,
                       clip=body.x + body.width)
        else:
            indent = 1 if entry.get("indent") else 0     # a fork sits under its source, "↳"
            when = hui.truncate_end(entry.get("when", ""), STAMP_W)
            label_w = max(0, body.width - 4 - _wcwidth(when) - 1 - indent)
            clip_x = body.x + body.width
            if entry.get("active"):            # the same mark the active space wears (sidebar.rs:1082-1091)
                canvas.fill_bg(body.x, clip_x, y, P["surface_dim"])
            style = ({"fg": hui.TEXT, "bold": True} if entry.get("active")
                     else {"fg": P["subtext0"]})
            if indent:
                canvas.put(body.x + 1, y, "↳", fg=hui.OVERLAY0, clip=clip_x)
            glyph, color = hui.state_dot(entry.get("state", "saved"), entry.get("seen", False))
            canvas.put(body.x + 1 + indent, y, glyph, fg=color, clip=clip_x)
            canvas.put(body.x + 3 + indent, y, hui.truncate_end(entry["label"], label_w),
                       clip=clip_x, **style)
            canvas.put(clip_x - _wcwidth(when) - 1, y, when, fg=hui.OVERLAY0, clip=clip_x)
        hits.append((hui.Rect(body.x, y, body.width, 1), entry["action"]))
    if has_bar:
        _put_scrollbar(canvas, metrics,
                       hui.Rect(area.x + area.width - 1, body.y, 1, body.height))
    return state


def format_sidebar(spaces, agents, width, rows, *, collapsed=False, scrolls=None,
                   sort="grouped", sessions=(), session_mode="here", reveal_space=False):
    """src/ui/sidebar.rs render_sidebar (1011-1035) / render_sidebar_collapsed (790-904) on a
    canvas covering the whole sidebar rect, separator column included. MISAKA addition:
    a sessions section between spaces and agents; the three share the height by weight.
    Returns ``(lines, hits, sections)``: one ANSI string per screen row; ``hits`` =
    [(Rect, action)] in draw order (search it backwards, the last drawn wins); ``sections``
    = per-section scroll state for the wheel. Pure, so testable."""
    scrolls = scrolls or {}
    hits, sections = [], {}
    if width == 0 or rows == 0:
        return [], hits, sections
    canvas = _Canvas(width, rows)
    area = hui.Rect(0, 0, width, rows)
    for y in range(rows):                 # 1023-1027: the separator, surface_dim outside navigate mode
        canvas.put(width - 1, y, "│", fg=hui.PALETTE["surface_dim"])
    if collapsed:
        _render_collapsed(canvas, spaces, sessions, _sorted_agents(agents, sort), area, hits)
    else:
        content_w = max(0, width - 1)
        for key, y, height in hui.allocate_sections(rows, SECTION_WEIGHTS):
            sub = hui.Rect(0, y, content_w, height)
            if key == "spaces":
                sections[key] = _render_spaces(canvas, spaces, sub, scrolls.get(key, 0), hits,
                                               reveal=reveal_space)
            elif key == "sessions":
                sections[key] = _render_sessions(canvas, sessions, sub, scrolls.get(key, 0), hits,
                                                 session_mode)
            else:
                sections[key] = _render_agents(canvas, _sorted_agents(agents, sort), sub,
                                               scrolls.get(key, 0), sort, hits)
        toggle = hui.expanded_sidebar_toggle_rect(area)       # 1531-1553: "«" collapses
        canvas.put(toggle.x, toggle.y, "«", fg=hui.OVERLAY0)
        hits.append((toggle, ("toggle",)))
    return [canvas.row(y) for y in range(rows)], hits, sections


def format_mode_bar(badge_text, hints, width):
    """herdr menus.rs:22-29 render_bottom_bar: the whole row sits on panel_bg (the tab bar's
    colour); a " BADGE " on accent, then key (accent, bold) + description pairs with two
    spaces between them (31-62 prefix, 63-128 copy, 259-285 resize). Descriptions use the
    text colour here (herdr: overlay0) by the user's choice. Pure, so testable."""
    bg = hui.sgr_bg(hui.PANEL_BG)
    key = bg + hui.sgr_fg(hui.ACCENT) + "\x1b[1m"
    word = bg + hui.sgr_fg(hui.TEXT)
    badge = hui.sgr_bg(hui.ACCENT) + hui.sgr_fg(hui.panel_contrast_fg()) + "\x1b[1m"
    parts = [(f"{badge} {badge_text} \x1b[0m{bg} ", _wcwidth(badge_text) + 3)]
    for name, desc in hints:
        parts.append((f"{key}{name}\x1b[0m{word} {desc}  \x1b[0m",
                      _wcwidth(name) + 1 + _wcwidth(desc) + 2))
    out, used = [], 0
    for text, visible in parts:            # Drop hints that do not fit; never overflow the row.
        if used + visible > width:
            break
        out.append(text)
        used += visible
    return bg + "".join(out) + bg + " " * max(0, width - used) + "\x1b[0m"


def format_copy_prompt_bar(direction, editor, width):
    """herdr client/shell/render.rs render_mode_bar, copy mode with the search prompt open:
    " COPY " on the badge, the "/" or "?" marker (key style) at column 7, the text editor
    from column 8 on panel_bg with its cursor cell drawn inverse (text on panel_bg), and the
    "  enter search  esc cancel" footer at the right edge once the row is 50 columns wide.
    The footer uses the text colour like every mode-bar description here (herdr: overlay0)."""
    canvas = _Canvas(width, 1)
    canvas.fill_bg(0, width, 0, hui.PANEL_BG)
    canvas.put(0, 0, " COPY ", fg=hui.panel_contrast_fg(), bg=hui.ACCENT, bold=True)
    if width >= 8:
        canvas.put(7, 0, "/" if direction > 0 else "?", fg=hui.ACCENT, bg=hui.PANEL_BG, bold=True)
    prefix = min(8, width)
    footer = "  enter search  esc cancel"
    footer_width = len(footer) if width >= 50 else 0
    caret = _put_editor(canvas, prefix, 0, max(0, width - prefix - footer_width), editor, hui.TEXT, hui.PANEL_BG)
    if caret is not None:
        cell = canvas.cells[0][caret]
        cell[1], cell[2] = hui.PANEL_BG, hui.TEXT
    if footer_width:
        canvas.put(width - footer_width, 0, footer, fg=hui.TEXT, bg=hui.PANEL_BG)
    return canvas.row(0)


def format_prefix_bar(width, prefix_name="ctrl+b"):
    """herdr menus.rs:31-62 render_prefix_overlay: the PREFIX bar's four hints."""
    return format_mode_bar("PREFIX", (("esc", "cancel"), (prefix_name, "send prefix"),
                                      ("g", "navigator"), ("?", "keybinds")), width)


def format_rename_popup(title, editor, rect):
    """herdr client/shell/overlays.rs render_rename_overlay: a 56x7 modal (accent border,
    panel_bg), the title bold on the first inner row, the input on the third row on surface0
    with the text editor one cell in (text_editor.rs render: the viewport that keeps the
    caret inside), and a centred button row: [↵ save] on accent, [^c clear] and
    [esc cancel] on surface0. Returns ``(rows, hits, caret)``: hits = [(Rect, action)], the
    caret is the screen cell the host cursor goes to."""
    if rect.width < 8 or rect.height < 6:
        return [], [], None
    width, height = rect.width, rect.height
    canvas = _Canvas(width, height)
    for y in range(height):
        canvas.fill_bg(0, width, y, hui.PANEL_BG)
    canvas.put(0, 0, "┌" + "─" * (width - 2) + "┐", fg=hui.ACCENT)
    for y in range(1, height - 1):
        canvas.put(0, y, "│", fg=hui.ACCENT)
        canvas.put(width - 1, y, "│", fg=hui.ACCENT)
    canvas.put(0, height - 1, "└" + "─" * (width - 2) + "┘", fg=hui.ACCENT)
    inner_x, inner_w = 1, width - 2
    canvas.put(inner_x, 1, title, fg=hui.TEXT, bold=True, clip=inner_x + inner_w)
    canvas.fill_bg(inner_x, inner_x + inner_w, 3, hui.SURFACE0)
    caret = _put_editor(canvas, inner_x + 1, 3, inner_w - 1, editor, hui.TEXT, hui.SURFACE0)
    buttons = (("↵", "save", "save"), ("^c", "clear", "clear"), ("esc", "cancel", "cancel"))
    labels = [f" {hint} {label} " for hint, label, _action in buttons]
    total = sum(_wcwidth(t) for t in labels) + 2 * (len(labels) - 1)
    x, y = inner_x + max(0, (inner_w - total) // 2), 4
    hits = []
    for (hint, label, action), text in zip(buttons, labels):
        if action == "save":
            canvas.put(x, y, text, fg=hui.panel_contrast_fg(), bg=hui.ACCENT, bold=True, clip=inner_x + inner_w)
        else:
            canvas.put(x, y, text, fg=hui.TEXT, bg=hui.SURFACE0, bold=True, clip=inner_x + inner_w)
        hits.append((hui.Rect(rect.x + x, rect.y + y, _wcwidth(text), 1), action))
        x += _wcwidth(text) + 2
    return ([(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], hits,
            None if caret is None else (rect.x + caret, rect.y + 3))


_HELP_ROWS = [
    ("1-9", "Switch to tab N"), ("n / p", "Next / previous tab"),
    ("h j k l", "Focus left / down / up / right"), ("c", "New tab"),
    ("v", "Split side by side"), ("-", "Split top and bottom"),
    ("z", "Zoom: focused pane fills the tab"), ("g", "Go to: search every space and pane"),
    ("[", "Copy mode: move, search, select, copy"), ("r", "Resize: h/l width, j/k height"),
    ("T / W", "Rename this tab / this space"),
    ("x", "Close the focused pane"),
    ("d", "Quit (everything shuts down)"), (PREFIX_NAME, f"Send a literal {PREFIX_NAME}"),
    ("esc", "Leave prefix mode"), ("Mouse", "Click focus, drag copy, 2× word"),
    ("", "esc, enter or ? closes this help"),
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


def _shell():
    """The program a new shell pane runs: ``$SHELL``. Windows has none; there it is PowerShell 7
    when ``pwsh.exe`` is on PATH, else the inbox Windows PowerShell (herdr pane.rs
    default_windows_pane_shell)."""
    if WINDOWS:
        import shutil
        return shutil.which("pwsh.exe") or "powershell.exe"
    return os.environ.get("SHELL", "sh")


def _clipboard(text):
    """Copy to the system clipboard: pbcopy on macOS and the Win32 clipboard on Windows (what
    herdr does), otherwise OSC 52 and let the terminal handle it."""
    import subprocess
    try:
        if WINDOWS:
            from misaka.utils import win_clipboard
            win_clipboard.copy(text)
        else:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _write_all(b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")


MAX_CLIPBOARD_TEXT_BYTES = 1024 * 1024
MAX_WINDOW_TITLE_CHARS = 200     # config/window_title.rs
_UNSENT = object()               # no window title written yet


def sanitize_window_title(value):
    """config/window_title.rs sanitize_window_title_text: no ESC, BEL, ST or other control
    characters, at most 200 characters, trimmed; None when nothing is left."""
    kept = "".join(ch for ch in value if ch not in "\x1b\x07\x9c" and not hin.is_control(ch))
    title = kept[:MAX_WINDOW_TITLE_CHARS].strip()
    return title or None


def window_title_bytes(title):
    """terminal_effects.rs write_window_title: OSC 0 with the product name when there is no
    title, terminators stripped."""
    safe = "".join(ch for ch in (title or "misaka") if ch not in "\x1b\x07\x9c")
    return f"\x1b]0;{safe}\x07".encode()


def _clipboard_read_commands(environ=None, platform=None):
    """platform/{macos,linux}.rs read_clipboard_text: pbpaste on macOS; wl-paste (utf-8,
    then plain) under Wayland and xclip / xsel under X11 on Linux; nothing elsewhere."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return [["pbpaste"]]
    commands = []
    if platform.startswith("linux"):
        if environ.get("WAYLAND_DISPLAY"):
            commands += [["wl-paste", "--type", "text/plain;charset=utf-8"], ["wl-paste", "--type", "text/plain"]]
        if environ.get("DISPLAY"):
            commands += [["xclip", "-selection", "clipboard", "-out"], ["xsel", "--clipboard", "--output"]]
    return commands


def _read_clipboard_text():
    """The system clipboard's text, or None: a command that fails, prints nothing, prints
    more than 1 MiB or prints something that is not UTF-8 gives nothing (read_limited_reader)."""
    import subprocess
    for command in _clipboard_read_commands():
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        except OSError:
            continue
        with child:
            try:
                data = child.stdout.read(MAX_CLIPBOARD_TEXT_BYTES + 1)
            except OSError:
                child.kill()
                continue
            if len(data) > MAX_CLIPBOARD_TEXT_BYTES:
                child.kill()
                continue
            if child.wait() != 0 or not data:
                continue
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def is_modal_paste_shortcut(key, macos=None):
    """client/shell/input.rs is_modal_paste_shortcut_for_platform: ctrl+v (cmd+v on macOS),
    Shift allowed, when the host sent no text with it."""
    macos = sys.platform == "darwin" if macos is None else macos
    mods = key.mods & ~hin.LOCK_MASK & ~hin.SHIFT
    return (not key.text and key.code in ("v", "V")
            and (mods == hin.CTRL or (macos and mods == hin.SUPER)))


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
    """Minimal JSONL client over the daemon's local socket (one for requests, one for the event stream).

    Every request carries an id of its own and ``request`` answers only to that id: a reply that
    arrives after its request timed out (the daemon was busy for longer than the socket timeout)
    is skipped when the next request reads the stream, instead of being handed to that next
    request as its result -- which is how ``panes.list`` once received ``cards.list``'s reply and
    the panel died on ``KeyError: 'panes'``."""

    def __init__(self, path):
        self.sock = local_socket.connect(path)
        self.sock.settimeout(15)   # If the daemon hangs, fail with a "disconnected" error instead of freezing the keyboard.
        self.buf = b""
        self.seq = 0

    def send(self, method, params=None):
        self.seq += 1
        request_id = str(self.seq)
        self.sock.sendall((json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            ensure_ascii=False) + "\n").encode())
        return request_id

    def notify(self, method, params=None):
        """Fire-and-forget: the daemon applies it and sends no reply (id=None), so the caller
        never blocks on the daemon. Used for a divider drag's resizes -- herdr's client never
        waits on its server for a resize."""
        self.sock.sendall((json.dumps(
            {"id": None, "method": method, "params": params or {}},
            ensure_ascii=False) + "\n").encode())

    def request(self, method, params=None):
        request_id = self.send(method, params)
        while True:
            line = self.readline(block=True)
            if line is None:
                raise ConnectionError("Daemon connection closed.")
            msg = json.loads(line)
            if "event" in msg:
                continue
            if msg.get("id") != request_id:
                continue               # the late reply to a request that timed out: not this one's
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


def _send_pane_input(control, pane_id, data, note):
    """Forward one keystroke batch to a pane; a refusal costs the batch, not the network.

    An error *reply* means this pane would not take this batch — its PTY queue is full
    because a raw-mode program stopped reading stdin (an ordinary paste is enough), or it
    exited between the keypress and the send. The daemon is still there, but letting the
    error out ends the panel, and the daemon reads the last panel leaving as its cue to
    close every pane: Last Order, every Sister, every running research card. So drop the
    batch and say so, the way pane.close already tolerates a pane that is already gone.
    ConnectionError is not this case — the daemon really is gone — so it propagates.

    "Costs the batch" is the network's view, not the pane's: `_write_pty` writes until the
    queue fills, so a big paste can leave its first bytes in the pane and refuse the rest.
    The daemon's message says which of the two happened, so it is shown as-is rather than
    under a "dropped" heading that would be a lie half the time (audit 2026-09-02, ui-panel-10).
    """
    try:
        control.request("pane.input", {
            "id": pane_id, "data": base64.b64encode(bytes(data)).decode()})
    except RuntimeError as error:
        note(f"Input not delivered: {error}")


def _close_exited_pane(control, pane_id):
    """Leave card exits to the daemon's watcher, including cards not in our last poll."""
    current = control.request("panes.list")["panes"]
    if any(pane["id"] == pane_id and pane["card"] for pane in current):
        return False
    control.request("pane.close", {"id": pane_id})
    return True


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


HOST_MOUSE_MODES = b"\x1b[?1000;1002;1003;1006h"   # crossterm EnableMouseCapture, SGR encoding


def _probe_keyboard_enhancement():
    """terminal_setup.rs query_host_escape_disambiguation: ask for the kitty keyboard flags
    and primary device attributes, and read until DA answers (every terminal answers DA,
    so a host without the kitty protocol costs one round trip). Returns the probe state and
    the other bytes read meanwhile, which the framer receives first. Windows asks nothing
    and pushes no flags (terminal_setup.rs, cfg(windows))."""
    state, buffered = {}, bytearray()
    if WINDOWS:
        return state, b""
    try:
        _write_all(hin.KEYBOARD_PROBE)
    except OSError:
        return state, b""
    deadline = time.monotonic() + hin.KEYBOARD_PROBE_TIMEOUT
    while not state.get("primary_device_attributes") and len(buffered) < hin.MAX_BUFFERED_HOST_INPUT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            readable, _, _ = select.select([0], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            break
        try:
            chunk = os.read(0, min(4096, hin.MAX_BUFFERED_HOST_INPUT - len(buffered)))
        except InterruptedError:
            continue
        except OSError:
            break
        if not chunk:
            break
        buffered += chunk
        hin.consume_keyboard_probe_responses(buffered, state)
    return state, bytes(buffered)


def _term_size():
    """``(rows, cols)`` of the host terminal, or None when it cannot report a grid: the
    ioctl fails or answers zero (platform terminal_grid_size). Guessing 24x80 instead would
    shrink every pane to that size and reflow its whole history (herdr #3519)."""
    if WINDOWS:
        return win_console.size(1)
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ,
                                                       b"\0" * 8))[:2]
    except OSError:
        return None
    if not rows or not cols:
        return None
    return rows, cols


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
    if _term_size() is None:
        sys.exit("The panel needs a terminal that reports its size; this one reports none.")
    net.ensure()
    sock_path = os.path.expanduser(net.CFG["net_sock"])
    control, stream = _Sock(sock_path), _Sock(sock_path)
    if control.request("ping").get("panels"):
        # One panel at a time: two would fight over the layout (herdr shares one view across
        # clients; the cheap answer here is to refuse the second).
        sys.exit("Another MISAKA panel is already open. Use that one, or close it first.")

    # herdr's seen/done rule (api_helpers.rs:99-110): a turn that finished while its pane was
    # out of sight is "done" (teal) until you look at it. Tracked here from the reported state.
    turns = {}              # pane id -> last reported state
    unseen_turn = set()     # panes whose last turn ended out of sight

    def _in_view():
        tab = next((tree for tree in tabs_of() if focused in hui.pane_ids(tree)), None)
        return set(hui.pane_ids(tab)) if tab else {focused}

    def note_turns(raw):
        for pane in raw:
            state = (pane.get("reported") or {}).get("state")
            previous, turns[pane["id"]] = turns.get(pane["id"]), state
            if previous in ("working", "blocked") and state == "idle" and pane["id"] not in _in_view():
                unseen_turn.add(pane["id"])
            if pane["id"] in unseen_turn:
                pane["unseen"] = True
        return raw

    def panes():
        return note_turns(control.request("panes.list")["panes"])

    def note_exit(pane_id, exit_code):
        """A pane's program ended on its own: keep its last lines. When Last Order dies at
        startup the panel follows her out, and this is the only trace of why."""
        try:
            tail = control.request("pane.read", {"id": pane_id, "lines": 40, "strip": True})["text"]
        except (RuntimeError, ConnectionError):
            tail = "(output unavailable)"
        title = next((p["title"] for p in listing if p["id"] == pane_id), pane_id)
        try:
            crash_fd = os.open(home.path("panel_crash_log"), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(crash_fd, "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} pane exited: {title} "
                        f"(exit code {exit_code}) ===\n{tail}\n")
        except OSError:
            pass

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
    # Cursor state is kept per pane: position and visibility belong to each pane (the engine
    # hides the real cursor and draws its own block; a shell shows it). With one global value,
    # any path that forgot to resync on pane switch would stack the real cursor on top of the
    # engine's fake block, which suddenly looks brighter. (Bitten twice: IME input, click-to-focus.)
    pane_cursor = {}          # pane id -> {"at": [col, row], "hidden": bool, "shape": DECSCUSR 0-6}
    slices = []
    # herdr semantics: a tab is a page holding a BSP split tree (layout.rs port; nodes in geometry.py).
    # The daemon owns the layout and seats every pane (herdr server model); this is a view of it.
    spaces = []          # [{"id", "folder", "name", "tabs": [trees], "tab_names": [str | None], "zoomed": [bool], "grid": [bool]}]
    auto_names = {}      # pane -> the session file its tab name came from (renamed when the pane moves on)

    def active_space():
        return next((space for space in spaces if space["id"] == side["ws"]), None)

    def tabs_of():
        """The active space's tab list (the live list object, so callers may mutate it)."""
        space = active_space()
        return space["tabs"] if space else []

    def space_holding(pane_id):
        return next((space for space in spaces
                     if any(pane_id in hui.pane_ids(tree) for tree in space["tabs"])), None)

    layout_rev = {"n": None}          # the daemon revision the current view is based on

    def _layout_snapshot():
        return [(s["id"], list(s["tabs"]), list(s["tab_names"]), list(s["zoomed"]), list(s["grid"]), s["name"])
                for s in spaces]

    def reload_layout():
        """Pull the daemon's layout. Panes the last listing reported dead are left out of the
        view (the poll closes them); see _without_dead. Returns True when anything changed."""
        if drag[0] is not None and drag[0].get("kind") == "split":
            # Mid divider-drag the panel owns the layout: the dragged ratio lives in the local
            # tree and is not pushed until drag_end, so pulling the daemon's (still pre-drag)
            # layout here would overwrite the drag and the divider would snap back. Leave the
            # local tree alone until the drag settles.
            return False
        before = _layout_snapshot()
        dead = {p["id"] for p in listing if not p["alive"]}
        payload = control.request("layout.get")
        layout_rev["n"] = payload.get("revision")
        got = []
        for space in payload["spaces"]:
            trees, names, zoomed, grids = [], [], [], []
            for tab in space["tabs"]:
                tree = _without_dead(hui.from_jsonable(tab["tree"]), dead)
                if tree is not None:
                    trees.append(tree)
                    names.append(tab.get("name"))
                    zoomed.append(bool(tab.get("zoomed")))   # herdr Tab.zoomed: each tab keeps its own
                    grids.append(bool(tab.get("grid")))      # a grid tab re-balances when a pane is added/closed
            if trees:
                got.append({"id": space["id"], "folder": space["folder"], "name": space.get("name"),
                            "tabs": trees, "tab_names": names, "zoomed": zoomed, "grid": grids})
        # Spaces sessions still belong to but no pane holds: listed after the open ones, so a
        # session can be reopened into the space it belongs to.
        opened = {space["id"] for space in got}
        got.extend({"id": space["id"], "folder": space["folder"], "name": space.get("name"),
                    "tabs": [], "tab_names": [], "zoomed": [], "grid": []}
                   for space in payload.get("dormant") or () if space["id"] not in opened)
        was_open = any(space["id"] == side["ws"] and space["tabs"] for space in spaces)
        spaces[:] = got
        now_open = any(space["id"] == side["ws"] and space["tabs"] for space in spaces)
        if side["ws"] is not None and ((was_open and not now_open)
                                       or side["ws"] not in {space["id"] for space in spaces}):
            # The active space left the layout (its last pane died): `active_space()` was None
            # from here on, no row was highlighted, and the sessions list fell back to the
            # folder the panel was launched in -- for a `~` space that read as zero sessions.
            side["ws"] = space_of(focused) or next((space["id"] for space in spaces if space["tabs"]), None)
            side["reveal"] = True
            refresh_sessions()
        return before != _layout_snapshot()

    def push_layout():
        """A client-side edit (a dragged divider, a rename) goes back to the daemon, against the
        revision this view was built on; if the daemon moved on meanwhile, its layout wins."""
        try:
            out = control.request("layout.set", {"revision": layout_rev["n"], "spaces": [
                {"id": s["id"], "folder": s["folder"], "name": s["name"],
                 "tabs": [{"name": name, "tree": hui.to_jsonable(tree), "zoomed": zoomed, "grid": grid}
                          for name, tree, zoomed, grid in
                          zip(s["tab_names"], s["tabs"], s["zoomed"], s["grid"])]}
                for s in spaces]})
            layout_rev["n"] = out.get("revision", layout_rev["n"])
        except RuntimeError:
            reload_layout()

    def space_of(pane_id):
        space = space_holding(pane_id)
        return space["id"] if space else None

    def space_info(key):
        rows_, _agents = sidebar_model(spaces, listing, focused, side["ws"])
        return next((row for row in rows_ if row["key"] == key), None)

    def visible_tabs():
        """herdr: the tab bar and the main area show only the active workspace's tabs."""
        return tabs_of()

    def active_tab():
        return next((i for i, tree in enumerate(visible_tabs())
                     if focused in hui.pane_ids(tree)), 0)

    def tab_label(index):
        """herdr tab_display_name: the custom name, else the 1-based position."""
        space = active_space()
        name = space["tab_names"][index] if space and index < len(space["tab_names"]) else None
        return name or str(index + 1)

    def tab_zoomed(index):
        """herdr Tab.zoomed for a tab of the active space."""
        space = active_space()
        return bool(space and index < len(space["zoomed"]) and space["zoomed"][index])

    def toggle_zoom():
        """herdr apply_pane_zoom (actions.rs:1918-1970): flip the active tab's zoom; a tab
        with one pane has nothing to zoom and stays as it is (no " Z" either)."""
        space = active_space()
        index, tree = current_tree()
        if space is None or tree is None or len(hui.pane_ids(tree)) <= 1:
            return
        space["zoomed"][index] = not space["zoomed"][index]
        push_layout()
        relayout()

    def rename_tab_of(pane_id, name):
        space = space_holding(pane_id)
        if space is None:
            return
        index = next(i for i, tree in enumerate(space["tabs"]) if pane_id in hui.pane_ids(tree))
        space["tab_names"][index] = name or None
        push_layout()


    rows, cols = _term_size() or (24, 80)
    from misaka.ui.tui.native_modifiers import isNativeModifierPressed
    from misaka.ui.tui.terminal import isAppleTerminalSession
    apple_terminal_host = isAppleTerminalSession()   # the panel's own host
    # Sidebar state (herdr AppState: sidebar_width / sidebar_collapsed / agent_panel_sort / active).
    side = {"w": SIDEBAR_W, "collapsed": False, "sort": "grouped", "ws": None, "sess_mode": "here",
            "reveal": True}
    last_focus = {}        # space -> the pane focused last time we were there (herdr: per-workspace focus)
    side_order = []        # space keys in sidebar order at the last draw (neighbour lookup when one closes)

    def switch_space(key):
        """herdr switch_workspace: the main area shows that space's tabs, focus returns to its last pane."""
        side["reveal"] = side["reveal"] or side["ws"] != key
        side["ws"] = key
        refresh_sessions()     # the sessions list belongs to the space
        alive = {p["id"] for p in listing if p["alive"]}
        target = last_focus.get(key)
        if target not in alive or space_of(target) != key:
            vis = visible_tabs()
            target = hui.pane_ids(vis[0])[0] if vis else None
        if target is None:
            relayout()
        else:
            focus(target, force_layout=True)

    def refocus(prefer=None, page_idx=0):
        """After a pane went away: stay in the active space while it has panes (the tab's
        survivor first, then the tab at the same position), else the space is gone and its
        neighbour takes over (herdr actions.rs close_workspace: active = min(idx, len - 1))."""
        reload_layout()
        alive = {p["id"] for p in listing if p["alive"]}
        if prefer in alive and space_of(prefer) == side["ws"]:
            focus(prefer, force_layout=True)
            return
        vis = visible_tabs()
        if vis:
            focus(hui.pane_ids(vis[min(page_idx, len(vis) - 1)])[0], force_layout=True)
            return
        candidates = [space["id"] for space in spaces if space["tabs"]]
        if candidates:
            index = side_order.index(side["ws"]) if side["ws"] in side_order else 0
            switch_space(candidates[min(index, len(candidates) - 1)])

    cards_cache = {"items": [], "sessions": []}       # Board cards and the unified session inventory
    sessions_cache = {"rows": []}     # the sidebar draws from here: scanning on every frame would stat the disk per keystroke
    sess_folds = set()                # session groups start open; a click folds one
    meta_cache = {}

    def refresh_cards():
        try:
            response = control.request("cards.list")
            cards_cache.update(items=response["cards"], sessions=response["sessions"])
        except (RuntimeError, OSError):
            pass

    git_cache = {}      # realpath folder -> {"at": monotonic, "info": git_info() or None}
    git_seen = set()     # folders the sidebar drew since the last poll
    git_jobs = {}        # realpath folder -> in-flight probe; one worker, so probes queue
    git_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="misaka-panel-git")

    def git_view(folder):
        """The sidebar's lookup: cached info now, and a note to the poll to keep it fresh."""
        folder = os.path.realpath(folder)
        git_seen.add(folder)
        entry = git_cache.get(folder)
        return entry["info"] if entry else None

    def refresh_git(now):
        # Only folders the sidebar actually drew since the last poll: this set used to be
        # add-only, so every folder ever shown kept spawning git for the panel's whole life.
        polled = set(git_seen)
        git_seen.clear()
        active = {c["id"] for c in cards_cache["items"] if c["status"] in GIT_OWNED}
        _poll_git_cache(now, polled, active, git_cache, git_jobs, git_pool.submit)

    def session_entry_ids(path):
        """Every entry id in a session file, cached by mtime: the divergence test for forks."""
        try:
            key = ("ids", path)
            stamp = os.path.getmtime(path)
        except OSError:
            return frozenset()
        if meta_cache.get(key, (None,))[0] != stamp:
            ids = set()
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            entry_id = json.loads(line).get("id")
                        except ValueError:
                            continue
                        if isinstance(entry_id, str):
                            ids.add(entry_id)
            except OSError:
                pass
            meta_cache[key] = (stamp, frozenset(ids))
        return meta_cache[key][1]

    def session_meta(path):
        """A session file's title (its first user message; a fork -- header ``parentSession``
        set -- titles itself by its first message NOT in the source, the reason it exists),
        the folder it worked in, and what it was forked from. Cached by mtime."""
        try:
            key = ("meta", path)
            stamp = os.path.getmtime(path)
        except OSError:
            return {"title": os.path.basename(path), "cwd": None, "parent": None}
        if meta_cache.get(key, (None,))[0] != stamp:
            cwd, parent, users = None, None, []
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if obj.get("type") == "session":
                            cwd = obj.get("cwd")
                            parent = obj.get("parentSession")
                        message = obj.get("message") or {}
                        if message.get("role") != "user":
                            continue
                        content = message.get("content")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = next((c.get("text", "") for c in content
                                         if isinstance(c, dict) and c.get("type") == "text"), "")
                        else:
                            text = ""
                        if text.strip():
                            users.append((obj.get("id"), " ".join(text.split())[:60]))
                            if parent is None:
                                break              # a plain session: the first one is the title
            except OSError:
                pass
            title = users[0][1] if users else ""
            if parent and users:
                shared = session_entry_ids(parent)
                title = next((t for i, t in users if i not in shared), title)
            meta_cache[key] = (stamp, {"title": title or os.path.basename(path)[:20],
                               "cwd": cwd, "parent": parent})
        return meta_cache[key][1]

    def gather_sessions():
        """One row per session, whatever its kind or storage layout."""
        from misaka.core import session_catalog
        everything = side["sess_mode"] == "all"
        lo, others = [], []
        for entry in cards_cache["sessions"]:
            # "here" is the active space's own sessions: a session belongs to the space it
            # first ran in, so two spaces on one folder do not show each other's.
            if not session_catalog.available(entry) or (not everything and entry.get("space") != side["ws"]):
                continue
            path = entry["path"]
            meta = session_meta(path) if path else {}
            try:
                stamp = os.path.getmtime(path) if path else 0
            except OSError:
                if not session_catalog.available(entry):
                    continue
                stamp = 0  # A live session can reserve its path before the first saved reply.
            role, kind = entry["role"], entry["kind"]
            title = entry.get("title") or meta.get("title") or entry["id"]
            if kind == "node" and entry.get("node_id"):
                title = f"{entry['node_id']} · {title}"
            label = f"{role} · {title} [{kind}]"
            if entry.get("run_id"):
                label += f" · {entry['run_id']} · depth {entry.get('depth', '?')}"
            if kind == "node":
                label = f"d{entry.get('depth', '?')} {entry.get('node_status', '?')} · {label}"
            elif kind == "child":
                if entry.get("parent_task_status"):
                    label = f"card {entry['parent_task_status']} · {label}"
                if entry.get("child_status"):
                    label = f"{entry['child_status']} · {label}"
            elif entry.get("task_status"):
                label = f"card {entry['task_status']} · {label}"
            if entry.get("paused"):
                label = f"pause requested · {label}"
            row = {**entry, "kind": "item", "label": label, "when": session_stamp(stamp) if stamp else "live · unsaved",
                   "folder": entry["workspace"], "_t": stamp, "session_kind": kind,
                   "parent": meta.get("parent"), "action": ("sess-open", path or entry["catalog_file"])}
            (lo if role == "last-order" else others).append(row)
        lo.sort(key=lambda item: -item["_t"])
        others.sort(key=lambda item: -item["_t"])
        if everything:
            return session_rows(folder_groups(lo + others), sess_folds)
        return session_rows([("last-order", "Last Order", lo), ("sisters", "Sisters / Agents", others)], sess_folds)

    def refresh_sessions():
        try:
            sessions_cache["rows"] = gather_sessions()
        except OSError:
            sessions_cache["rows"] = []  # a failed scan must not keep deleted rows alive

    def prune_meta_cache():
        """Drop cached titles for session files the sidebar no longer lists.

        The cache is keyed by path now (mtime rides along as the validity stamp); before
        that every save of a live session minted a new key and orphaned the old one -- about
        eighteen hundred retained entries an hour, each holding the file's whole id set.
        """
        live = {row.get("path") for row in sessions_cache["rows"] if isinstance(row, dict)}
        for cached in [key for key in meta_cache if os.path.realpath(key[1]) not in live]:
            del meta_cache[cached]      # the rows carry realpaths; the cache is keyed as called

    def session_pane(action):
        """The live pane already writing this session, if any (one session, one tab)."""
        live = open_session_panes(listing)
        row = session_row(action) or {}
        return session_pane_id(action, live) or live.get(tuple(row.get("live_key") or ()))

    def session_row(action):
        return next((row for row in sessions_cache["rows"]
                     if row["kind"] == "item" and row["action"] == action), None)

    def reopen_session(hit):
        """Focus the owner pane or attach to its session; history never starts a worker."""
        from misaka.core import session_catalog
        open_pane = session_pane(hit)
        if open_pane:
            focus(open_pane)
            return
        row = session_row(hit)
        if row is None or not session_catalog.available(row):
            refresh_sessions()
            draw_sidebar()
            show_bottom_bar(" session or its working folder is gone")
            return
        folder, path = row["cwd"], row["path"]
        role, kind = row["role"], row["session_kind"]
        is_fork = bool(session_meta(path).get("parent")) if path else False
        # Only ordinary interactive conversations are resumed as a new interactive runtime.
        # DM/node/child/card sessions belong to their own lifecycle, even when idle.
        from misaka.config import sisters
        resumed = kind == "foreground" and row["state"] == "saved" and (role == "last-order" or role in sisters())
        # A session opens only in the space it belongs to (the daemon reopens a remembered one).
        # One that belongs to none is adopted by the active space -- a tab there -- except a fresh
        # (non-fork) Last Order, a workspace root, which takes a space of its own. A reopened
        # session never rejoins the Last Order it branched from.
        current = active_space()
        if row.get("space"):
            place = {"space": row["space"], "name": row["label"]}
        elif (resumed and role == "last-order" and not is_fork) or current is None:
            place = {"space": True, "name": row["label"]}
        elif current["tabs"]:
            place = {"tab": hui.pane_ids(current["tabs"][0])[0]}
        else:
            place = {"space": current["id"], "name": row["label"]}
        if resumed:
            argv = [sys.executable, "-m", "misaka", "chat"]
            if role != "last-order":
                argv += ["--as", role]
            argv += ["--session", path]
        else:
            argv = [sys.executable, "-m", "misaka", "chat", "--attach" if row.get("control") else "--read-only",
                    "--session" if path else "--catalog", path or row["catalog_file"]]
        pane_id = new_pane(argv, row["label"], place=place, cwd=folder)
        if kind == "foreground" and role == "last-order":
            auto_names[pane_id] = path

    # ── Mode (herdr app::Mode): one value every handler and every drawing path reads ──
    mode = Mode.TERMINAL

    def leave_command_mode():
        """navigate.rs leave_command_mode: back to the copy mode still owning the focused
        pane, else the terminal."""
        nonlocal mode
        mode = Mode.COPY if copy["pane"] is not None and copy["pane"] == focused else Mode.TERMINAL
        if mode is Mode.COPY:
            copy_bar()
            draw_copy_cursor()
        else:
            restore_bottom()

    def redraw_overlay():
        """After a resize or a repaint underneath: put the current mode's layer back
        (herdr simply re-renders the whole frame from the mode)."""
        if mode is Mode.GLOBAL_MENU:
            draw_menu()
        elif mode is Mode.NAVIGATOR:
            draw_nav()
        elif mode is Mode.RENAME:
            draw_rename()
        elif mode is Mode.KEYBIND_HELP:
            draw_help_overlay()
        elif mode is Mode.PREFIX:
            draw_prefix_bar()
        elif mode is Mode.RESIZE:
            draw_resize_bar()
        elif mode is Mode.COPY:
            copy_clamp()
            copy_bar()

    # The Sister roster popup (herdr's global menu, Mode::GlobalMenu): a modal that eats
    # keys and clicks until it closes.
    menu = {"items": [], "hl": 0, "scroll": 0, "rect": None, "hits": [], "lines": []}

    def draw_menu():
        agents_area = (ui_map["sections"].get("agents") or {}).get("rect") or hui.Rect(0, 0, side["w"] - 1, rows)
        launcher = hui.global_launcher_rect(agents_area, SISTERS_LABEL)
        rect = hui.menu_popup_rect(hui.Rect(0, 0, cols, rows), launcher, menu["items"])
        menu["scroll"] = menu_scroll_for(menu["hl"], menu["scroll"], rect.height - 2)
        menu["lines"], menu["hits"] = format_menu_popup(menu["items"], menu["hl"], menu["scroll"], rect)
        menu["rect"] = rect
        request_render()

    def open_menu():
        nonlocal mode
        from misaka.config import sisters
        menu.update(items=sorted(sisters()) or ["no sisters"], hl=0, scroll=0)
        mode = Mode.GLOBAL_MENU
        draw_menu()

    def close_menu():
        leave_command_mode()      # an overlay leaves the mode under it as it was (copy mode included)
        request_render()

    def menu_hover(x, y):
        """herdr mouse.rs:146-153: the pointer moves the highlight."""
        hit = next((index for rect, index in menu["hits"]
                    if rect.x <= x < rect.x + rect.width and rect.y == y), None)
        if hit is not None and hit != menu["hl"]:
            menu["hl"] = hit
            draw_menu()

    def menu_move(delta):
        menu["hl"] = max(0, min(menu["hl"] + delta, len(menu["items"]) - 1))   # herdr move_prev/move_next: no wrap
        draw_menu()

    def menu_choose(index):
        name = menu["items"][index]
        close_menu()
        if name != "no sisters":   # A Sister joins the focused pane's tab as a grid pane.
            new_pane([sys.executable, "-m", "misaka", "chat", "--as", name], name, place={"grid": focused})

    def menu_key(key):
        """herdr modal.rs:147-160 handle_global_menu_key: esc closes, k/up and j/down move, enter picks."""
        if key.kind == "release":
            return
        if key.matches("esc"):
            close_menu()
        elif key.matches("up") or key.matches("k"):
            menu_move(-1)
        elif key.matches("down") or key.matches("j"):
            menu_move(1)
        elif key.matches("enter"):
            menu_choose(menu["hl"])

    def menu_mouse(event):
        """mouse.rs:146-172: hover highlights, a click picks or closes, the wheel scrolls."""
        rect = menu["rect"]
        inside = rect and rect.x <= event.x < rect.x + rect.width and rect.y <= event.y < rect.y + rect.height
        if event.kind == "move":
            menu_hover(event.x, event.y)
        elif event.kind == "press" and event.button == 0:
            hit = next((index for r, index in menu["hits"]
                        if r.x <= event.x < r.x + r.width and r.y == event.y), None)
            if hit is None:
                close_menu()
            else:
                menu_choose(hit)
        elif event.kind in ("wheel_up", "wheel_down") and inside:
            cap = max(0, len(menu["items"]) - (rect.height - 2))
            menu["scroll"] = max(0, min(menu["scroll"] + (1 if event.kind == "wheel_down" else -1), cap))
            menu["hl"] = max(menu["scroll"], min(menu["hl"], menu["scroll"] + rect.height - 3))
            draw_menu()

    # The navigator (prefix+g, herdr Go to): a modal list of every space and terminal.
    # ``selected`` is a row's target, so the choice survives the list changing underneath;
    # ``scroll`` only moves by the scrollbar (the frame pulls it to the selection).
    nav = {"query": TextEditor(), "search": False, "filter": None, "selected": None,
           "scroll": 0, "rows": [], "hits": [], "rect": None, "lines": [], "caret": None,
           "search_rect": None, "track": None, "metrics": None, "drag": None}

    def nav_rows():
        return navigator_rows(spaces, listing, focused, query=nav["query"].text,
                              state_filter=nav["filter"], git=git_view)

    def draw_nav():
        nav["rows"] = nav_rows()
        rect = navigator_popup_rect(hui.Rect(0, 0, cols, rows))
        nav["rect"] = rect
        if rect is None:
            nav.update(lines=[], hits=[], caret=None, search_rect=None, track=None, metrics=None)
        else:
            out = format_navigator(nav["rows"], nav["selected"], nav["scroll"], rect, nav["query"],
                                   search_focused=nav["search"], state_filter=nav["filter"])
            nav.update(lines=out["rows"], hits=out["hits"], caret=out["caret"],
                       search_rect=out["search"], track=out["track"], metrics=out["metrics"])
        request_render()

    def open_nav():
        """overlay_input.rs open_navigator_overlay: an empty query, and the focused pane's
        row selected (none: the first pane row)."""
        nonlocal mode
        nav.update(query=TextEditor(), search=False, filter=None, selected=None, scroll=0, drag=None)
        nav["rows"] = nav_rows()
        nav["selected"] = next((row["target"] for row in nav["rows"] if row["current"]), None)
        mode = Mode.NAVIGATOR
        draw_nav()

    def close_nav():
        nav["drag"] = None
        leave_command_mode()      # an overlay leaves the mode under it as it was (copy mode included)
        request_render()

    def nav_accept():
        """accept_navigator_selection: the navigator closes only when the target could be
        activated; with nothing to activate it stays open."""
        nav["rows"] = nav_rows()
        index = navigator_selected_index(nav["rows"], nav["selected"])
        if index is None:
            return
        kind, target = nav["rows"][index]["target"]
        if kind == "pane":
            if slice_of(target) is None and space_holding(target) is None:
                return
            close_nav()
            focus(target, force_layout=True)
        else:
            if not any(space["id"] == target for space in spaces):
                return
            close_nav()
            switch_space(target)

    def nav_move(delta):
        """move_navigator_selection: clamp to the list; a selection that left the list
        counts from the top."""
        rows_ = nav_rows()
        if not rows_:
            nav["selected"] = None
            return
        index = navigator_selected_index(rows_, nav["selected"]) or 0
        nav["selected"] = rows_[max(0, min(index + delta, len(rows_) - 1))]["target"]

    def nav_move_workspace(forward):
        """move_navigator_workspace: the first pane of the next (or previous) space that has
        one, from the section holding the selection; no wrap."""
        rows_ = nav_rows()
        index = navigator_selected_index(rows_, nav["selected"])
        if index is None:
            return
        section = next((i for i in range(index, -1, -1) if rows_[i]["target"][0] != "pane"), index)
        starts = [i for i in range(len(rows_) - 1)
                  if rows_[i]["target"][0] == "space" and rows_[i + 1]["target"][0] == "pane"
                  and (i > section if forward else i < section)]
        if starts:
            nav["selected"] = rows_[(starts[0] if forward else starts[-1]) + 1]["target"]

    def nav_scroll_to(scroll, viewport):
        """scroll_navigator_to: set the offset and keep the selection inside it."""
        rows_ = nav_rows()
        viewport = max(1, viewport)
        nav["scroll"] = min(scroll, max(0, len(rows_) - viewport))
        index = navigator_selected_index(rows_, nav["selected"]) or 0
        index = min(max(index, nav["scroll"]), nav["scroll"] + viewport - 1)
        nav["selected"] = rows_[index]["target"] if index < len(rows_) else None

    def nav_key(key):
        """overlay_input.rs navigator keys. Search focused: the text editor first (an edit
        clears the state filter and the selection), then up/down and ctrl+p/n move. The
        list: left/right jump spaces, backspace drops the filter, home/end/G, / searches,
        j/k/up/down, ctrl+d/u move eight, b/w/i/d filter (the query is cleared), a shows all."""
        if key.kind == "release":
            return
        mods, code = key.mods & ~hin.LOCK_MASK, key.code
        if code == "esc":
            if nav["search"]:
                nav["search"] = False
                draw_nav()
            else:
                close_nav()
            return
        if code == "enter":
            nav_accept()
            if mode is Mode.NAVIGATOR:
                draw_nav()
            return
        text = key_text(key)
        if nav["search"]:
            changed = nav["query"].handle_key(key)
            if changed is not None:
                if changed:
                    nav.update(filter=None, selected=None)
            elif code == "up" or (code == "p" and mods == hin.CTRL):
                nav_move(-1)
            elif code == "down" or (code == "n" and mods == hin.CTRL):
                nav_move(1)
            else:
                return
            draw_nav()
            return
        if code in ("left", "right") and mods == 0:
            nav_move_workspace(code == "right")
        elif code == "backspace" and mods == 0:
            if nav["filter"] is not None:
                nav.update(filter=None, selected=None)
        elif code == "home" and mods == 0:
            nav.update(selected=None, scroll=0)
        elif (code == "end" and mods == 0) or text == "G":
            rows_ = nav_rows()
            nav["selected"] = rows_[-1]["target"] if rows_ else None
        elif text == "/":
            nav.update(search=True, filter=None)
        elif (code == "down" and mods == 0) or text == "j":
            nav_move(1)
        elif (code == "up" and mods == 0) or text == "k":
            nav_move(-1)
        elif code == "d" and mods & hin.CTRL:
            nav_move(NAV_PAGE)
        elif code == "u" and mods & hin.CTRL:
            nav_move(-NAV_PAGE)
        elif text in NAV_FILTERS and mods == 0:
            nav["query"].clear()
            nav.update(filter=NAV_FILTERS[text], selected=None)
        elif text == "a" and mods == 0:
            nav["query"].clear()
            nav.update(filter=None, selected=None)
        else:
            return
        draw_nav()

    def nav_paste(text):
        """paste_into_active_text_input: only the focused search line takes a paste."""
        if nav["search"]:
            nav["query"].insert(text)
            draw_nav()

    def nav_mouse(event):
        """mouse.rs navigator: hovering a row selects it; a press on the scrollbar grabs the
        thumb or jumps the track, on the search line focuses search (and drops the filter),
        on a row opens it, outside the popup closes; the wheel moves the selection by three."""
        rect = nav["rect"]
        def at(r):
            return r is not None and r.x <= event.x < r.x + r.width and r.y <= event.y < r.y + r.height

        hit = next((target for r, target in nav["hits"] if at(r)), None)
        if nav["drag"] is not None:
            if event.kind == "drag" and nav["metrics"] and nav["track"]:
                metrics = nav["metrics"]
                offset = hui.scrollbar_offset_from_drag_row(metrics, nav["track"], event.y, nav["drag"])
                nav_scroll_to(metrics["max_offset_from_bottom"] - offset, metrics["viewport_rows"])
                draw_nav()
                return
            if event.kind == "release":
                nav["drag"] = None
                return
        if event.kind == "move":
            if hit is not None:
                nav["selected"] = hit
                draw_nav()
        elif event.kind == "press" and event.button == 0:
            if at(nav["track"]):
                metrics = nav["metrics"]
                grab = hui.scrollbar_thumb_grab_offset(metrics, nav["track"], event.y)
                if grab is not None:
                    nav["drag"] = grab
                else:
                    offset = hui.scrollbar_offset_from_row(metrics, nav["track"], event.y)
                    nav_scroll_to(metrics["max_offset_from_bottom"] - offset, metrics["viewport_rows"])
                    draw_nav()
            elif at(nav["search_rect"]):
                nav.update(search=True, filter=None)
                draw_nav()
            elif hit is not None:
                nav["selected"] = hit
                nav_accept()
                if mode is Mode.NAVIGATOR:
                    draw_nav()
            elif not at(rect):
                close_nav()
        elif event.kind == "wheel_up":
            nav_move(-NAV_WHEEL)
            draw_nav()
        elif event.kind == "wheel_down":
            nav_move(NAV_WHEEL)
            draw_nav()

    # ── Resize mode (prefix+r, herdr Mode::Resize: modal.rs:713-735, menus.rs:259-285) ──

    def current_tree():
        vis = visible_tabs()
        index = active_tab()
        return (index, vis[index]) if vis and index < len(vis) else (None, None)

    def draw_resize_bar():
        show_bottom_bar(format_mode_bar("RESIZE", (("h/l", "width"), ("j/k", "height"), ("esc", "done")),
                                        max(10, cols - side["w"])))

    def enter_resize():
        nonlocal mode
        mode = Mode.RESIZE
        draw_resize_bar()

    def resize_key(key):
        """modal.rs handle_resize_key: esc, enter or the resize binding leave; h/l and j/k
        (or the arrows) move the nearest split by 5% (actions.rs:1858)."""
        if key.kind == "release":
            return
        text = key_text(key)
        if key.matches("esc") or key.matches("enter") or text == "r":
            leave_command_mode()
            return
        nav_dir = ({"h": "left", "l": "right", "k": "up", "j": "down"}.get(text)
                   or {"left": "left", "right": "right", "up": "up", "down": "down"}.get(key.code))
        if nav_dir is None:
            return
        _tab_index, tree = current_tree()
        if tree is None or tab_zoomed(active_tab()):
            return
        new_tree = hui.resize_focused(tree, focused, nav_dir, 0.05, chrome_state["area"])
        if new_tree != tree:
            trees = tabs_of()
            trees[trees.index(tree)] = new_tree
            push_layout()   # relayout() re-pulls the daemon's layout; unpushed, the resize would be discarded
            relayout()

    # ── Mouse drags: split dividers and pane scrollbars (app/input/mouse.rs drag state machine) ──
    drag = [None]

    def scroll_to(pane_id, offset):
        """Put a pane's scrollback at ``offset`` rows from the bottom (the daemon scrolls by delta)."""
        metrics = scroll_state.get(pane_id, {}).get("metrics") or {}
        delta = metrics.get("offset_from_bottom", 0) - offset
        if not delta:
            return
        try:
            out = control.request("pane.scroll", {"id": pane_id, "delta": delta})
        except RuntimeError:
            return
        scroll_state.setdefault(pane_id, {})["metrics"] = out["scroll"]
        paint_rows(pane_id, {str(i): line for i, line in enumerate(out["rows"])}, clear=True)

    def press_on_chrome(x, y):
        """A left press on a scrollbar gutter or a split divider starts a drag; returns True when it did."""
        for pane_id, gutter, _focused in chrome_state["tracks"]:
            metrics = scroll_state.get(pane_id, {}).get("metrics")
            if gutter is None or not hui.should_show_scrollbar(metrics):
                continue
            if gutter.x == x and gutter.y <= y < gutter.y + gutter.height:
                grab = hui.scrollbar_thumb_grab_offset(metrics, gutter, y)
                if grab is None:                              # scrollbar.rs:103-117: a track click jumps there
                    scroll_to(pane_id, hui.scrollbar_offset_from_row(metrics, gutter, y))
                    thumb = hui.scrollbar_thumb(scroll_state[pane_id]["metrics"], gutter)
                    grab = (y - thumb[0]) if thumb and thumb[0] <= y < thumb[0] + thumb[1] else 0
                drag[0] = {"kind": "bar", "pane": pane_id, "gutter": gutter, "grab": grab}
                return True
        _index, tree = current_tree()
        if tree is None or tab_zoomed(active_tab()) or not chrome_state["area"]:
            return False
        split = hui.divider_at(hui.collect_splits(tree, chrome_state["area"]), x, y)
        if split is None:
            return False
        drag[0] = {"kind": "split", "split": split, "tree": tree, "pacer": DragPacer()}
        return True

    def drag_to(x, y):
        state = drag[0]
        if state["kind"] == "bar":
            metrics = scroll_state.get(state["pane"], {}).get("metrics")
            if metrics:
                scroll_to(state["pane"], hui.scrollbar_offset_from_drag_row(metrics, state["gutter"], y, state["grab"]))
            return
        position = state["pacer"].offer((x, y), time.monotonic())
        if position is not None:
            move_split(state, *position)

    def drag_due():
        state = drag[0]
        return state["pacer"].due() if state and state["kind"] == "split" else None

    def drag_tick(now, final=False):
        """Apply a coalesced divider position whose time has come."""
        state = drag[0]
        if state is None or state["kind"] != "split":
            return
        position = state["pacer"].take(now, final)
        if position is not None:
            move_split(state, *position)

    def move_split(state, x, y):
        ratio = hui.drag_ratio(state["split"], x, y)
        tree, trees = state["tree"], tabs_of()
        if tree in trees and hui.get_ratio_at(tree, state["split"]["path"]) != ratio:
            new_tree = hui.set_ratio_at(tree, state["split"]["path"], ratio)
            trees[trees.index(tree)] = new_tree
            state["tree"] = new_tree
            state["split"] = next((s for s in hui.collect_splits(new_tree, chrome_state["area"])
                                   if s["path"] == state["split"]["path"]), state["split"])
            # No push/reload per step: keep the dragged tree local and render from it, so the
            # drag never blocks on the daemon; the final ratio is persisted at drag_end.
            relayout(reload=False)

    def drag_end():
        drag_tick(time.monotonic(), final=True)
        was_split = drag[0] is not None and drag[0].get("kind") == "split"
        drag[0] = None                        # cleared first, so push_layout's conflict path can reload normally
        if was_split:
            push_layout()                     # persist the settled ratio (deferred through the drag)

    # ── Rename (prefix+T tab, prefix+W space; herdr dialogs.rs:43-110 rename modal) ──
    rename = {"kind": "tab", "target": None, "input": TextEditor(), "hits": [], "caret": None, "lines": []}

    def open_rename(kind):
        nonlocal mode
        current = active_space()
        if kind == "tab":
            index, tree = current_tree()
            if tree is None:
                return
            target = index
            value = (current["tab_names"][index] or "") if current else ""
        else:
            target = side["ws"]
            value = (current or {}).get("name") or ""
        rename.update(kind=kind, target=target, input=TextEditor(value))
        mode = Mode.RENAME
        draw_rename()

    def draw_rename():
        nonlocal mode
        area = hui.Rect(side["w"], 0, max(1, cols - side["w"]), rows)
        rect = hui.centered_popup_rect(area, 56, 7)
        if rect is None:
            mode = Mode.TERMINAL
            return
        title = "rename tab" if rename["kind"] == "tab" else "rename space"
        rename["lines"], rename["hits"], rename["caret"] = format_rename_popup(title, rename["input"], rect)
        request_render()

    def close_rename(save):
        nonlocal mode
        if save:
            value = rename["input"].text.strip()
            if rename["kind"] == "tab":
                space, index = active_space(), rename["target"]
                if space is not None and index < len(space["tabs"]):
                    for pid in hui.pane_ids(space["tabs"][index]):
                        auto_names.pop(pid, None)   # a typed name is never auto-refreshed
                    # modal.rs:840-856: a name equal to the automatic one keeps auto naming.
                    space["tab_names"][index] = value if value and value != str(index + 1) else None
            else:
                space = next((s for s in spaces if s["id"] == rename["target"]), None)
                if space is not None:
                    space["name"] = value or None
            if space is not None:
                push_layout()
        leave_command_mode()      # an overlay leaves the mode under it as it was (copy mode included)
        request_render()

    def rename_key(key):
        """overlay_input.rs rename keys: enter saves, esc cancels, host-generated text
        types; ctrl+c and super+backspace clear; everything else is the text editor's
        (cursor movement, word and line kills, ctrl+y yank)."""
        if key.kind == "release":
            return
        if key.code == "enter":
            close_rename(True)
            return
        if key.code == "esc":
            close_rename(False)
            return
        editor = rename["input"]
        if key.text:
            if editor.handle_key(key) is not None:
                draw_rename()
            return
        if key.matches("c", hin.CTRL) or (key.code == "backspace" and key.mods & hin.SUPER):
            editor.clear()
            draw_rename()
            return
        if editor.handle_key(key) is not None:
            draw_rename()

    def rename_mouse(event):
        """dialogs.rs buttons: save / clear / cancel; a press anywhere else cancels."""
        if event.kind != "press" or event.button != 0:
            return
        hit = next((action for rect, action in rename["hits"]
                    if rect.x <= event.x < rect.x + rect.width and rect.y == event.y), None)
        if hit == "save":
            close_rename(True)
        elif hit == "clear":
            rename["input"].clear()
            draw_rename()
        else:
            close_rename(False)

    # ── Copy mode (prefix+[, herdr app/input/copy_mode.rs + menus.rs:63-128) ──
    # ``pane`` stays set while a prefix command runs (copy mode survives focus moves
    # that come back); ``anchor`` is the absolute row/col a v/space selection started at.
    copy = {"pane": None, "abs": 0, "col": 0, "anchor": None, "linewise": None,
            "entry_offset": 0, "prompt": None, "query": "", "dir": 1, "status": "", "match": None}

    def copy_rect():
        sl = slice_of(copy["pane"])
        return sl[1] if sl else None

    def copy_metrics():
        return scroll_state.get(copy["pane"], {}).get("metrics") or {}

    # copy_mode.rs keeps the copy cursor in absolute buffer rows: output that moves the text up
    # moves the cursor (and a selection's end) with it, possibly off screen, where it is not
    # drawn. ``copy["abs"]`` is that row; the viewport row is derived on every read.
    def copy_row():
        return copy["abs"] - selmod.viewport_top_row(copy_metrics())

    def copy_set_row(row):
        copy["abs"] = _abs_row(row, copy_metrics())

    def copy_reveal():
        """A cursor that output or the wheel carried off screen is scrolled back into view
        before a key moves it (herdr's motions reveal the cursor they land on)."""
        rect = copy_rect()
        if rect is None:
            return
        row = copy_row()
        if row < 0:
            copy_scroll(row)
        elif row >= rect.height:
            copy_scroll(row - rect.height + 1)

    def copy_plain_rows():
        """The viewport rows as plain text (one character per cell, a wide one once)."""
        rect = copy_rect()
        return ["".join(symbol for symbol, _s in row) for row in pane_cells.get(copy["pane"], [])][:rect.height if rect else 0]

    def copy_clamp():
        rect = copy_rect()
        if rect is not None:
            copy["col"] = min(copy["col"], max(0, rect.width - 1))

    def draw_copy_cursor():
        request_render()               # the copy cursor is composed into the frame (compose_copy_cursor)

    def erase_copy_cursor():
        request_render()

    def copy_bar():
        width = max(10, cols - side["w"])
        if copy["prompt"] is not None:
            return show_bottom_bar(format_copy_prompt_bar(copy["prompt"]["dir"], copy["prompt"]["input"], width))
        selecting = copy["anchor"] is not None or copy["linewise"] is not None
        status = f" {copy['status']}" if copy["status"] else ""
        clearable = selecting or bool(copy["query"])
        hints = (("h/j/k/l w/b/e { }", "move"), ("/ ?", "search"), ("n/N", f"repeat{status}"),
                 ("v/space", "selecting" if selecting else "select"), ("y/enter", "copy"),
                 ("esc", "clear  q exit") if clearable else ("q/esc", "exit"))
        show_bottom_bar(format_mode_bar("COPY", hints, width))

    def enter_copy():
        """copy_mode.rs enter_copy_mode: the cursor starts where the pane's cursor is (or
        the bottom-left when it is hidden) and the scroll position is remembered for exit."""
        nonlocal mode
        if copy["pane"] is not None and copy["pane"] == focused:
            mode = Mode.COPY
            copy_bar()
            draw_copy_cursor()
            return
        if copy["pane"] is not None:
            exit_copy(False)
        rect = slice_of(focused)
        if rect is None:
            return
        rect = rect[1]
        if rect.width == 0 or rect.height == 0:
            return
        state = pane_cursor.get(focused) or {}
        at = state.get("at", [0, rect.height - 1]) if not state.get("hidden") else [0, rect.height - 1]
        clear_selection()
        copy.update(pane=focused, anchor=None, linewise=None, prompt=None, status="", query="", match=None,
                    entry_offset=(scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom", 0),
                    col=min(max(at[0], 0), max(0, rect.width - 1)))
        copy_set_row(min(max(at[1], 0), max(0, rect.height - 1)))
        mode = Mode.COPY
        copy_bar()
        draw_copy_cursor()

    def exit_copy(yank):
        """copy_mode.rs exit_copy_mode: copy or drop the selection, then put the pane back
        at the scroll position it had when copy mode began."""
        nonlocal mode
        live = sel["it"] is not None and sel["it"].pane == copy["pane"] and sel["it"].visible
        if yank and not live and copy["match"] is not None:
            # copy_mode.rs exit_copy_mode: with nothing selected, y copies the current match
            sel["it"] = Selection(copy["pane"], copy["match"][0], copy["match"][1], "dragging")
        if yank and sel["it"] is not None and sel["it"].pane == copy["pane"]:
            copy_selection()
        else:
            clear_selection()
        copy["match"] = None
        erase_copy_cursor()
        pane_id, offset = copy["pane"], copy["entry_offset"]
        copy["pane"] = None
        mode = Mode.TERMINAL
        scroll_to(pane_id, offset)
        restore_bottom()

    def copy_scroll(delta):
        """Scroll the copy-mode pane by ``delta`` lines (negative = back into history)."""
        try:
            out = control.request("pane.scroll", {"id": copy["pane"], "delta": delta})
        except RuntimeError:
            return False
        before = copy_metrics().get("offset_from_bottom")
        scroll_state.setdefault(copy["pane"], {})["metrics"] = out["scroll"]
        paint_rows(copy["pane"], {str(i): line for i, line in enumerate(out["rows"])}, clear=True)
        return out["scroll"]["offset_from_bottom"] != before

    def copy_sync_selection():
        """copy_mode.rs sync_copy_selection: the selection is rebuilt from the anchor (or the
        line anchor) and the cursor every time, so it also comes back after copy mode parked."""
        rect = copy_rect()
        if rect is None:
            return
        cursor_row = copy["abs"]
        if copy["linewise"] is not None:
            sel["it"] = Selection.lines(copy["pane"], copy["linewise"], cursor_row, max(0, rect.width - 1))
        elif copy["anchor"] is not None:
            sel["it"] = Selection(copy["pane"], copy["anchor"], (cursor_row, copy["col"]), "dragging")
        else:
            return
        paint_selection()

    def copy_move(drow, dcol):
        rect = copy_rect()
        if rect is None:
            return
        erase_copy_cursor()
        row, col = copy_row() + drow, copy["col"] + dcol
        if row < 0:
            copy_scroll(row)                  # past the top edge: pull history down
            row = 0
        elif row >= rect.height:
            copy_scroll(row - rect.height + 1)
            row = rect.height - 1
        copy_set_row(row)
        copy["col"] = min(max(col, 0), max(0, rect.width - 1))
        copy_sync_selection()
        draw_copy_cursor()

    def copy_word(motion):
        """copy_mode.rs word motions on the current row: w next start, b previous start, e next end."""
        line = copy_plain_rows()[copy_row()] if 0 <= copy_row() < len(copy_plain_rows()) else ""
        cells = []
        for ch in line:
            cells += [ch] * max(1, _wcwidth(ch))
        col, n = copy["col"], len(cells)
        is_word = lambda i: 0 <= i < n and cells[i] not in _WORD_BREAK
        if motion == "w":
            i = col
            while i < n and is_word(i): i += 1
            while i < n and not is_word(i): i += 1
        elif motion == "e":
            i = col + 1
            while i < n and not is_word(i): i += 1
            while i + 1 < n and is_word(i + 1): i += 1
        else:
            i = col - 1
            while i > 0 and not is_word(i): i -= 1
            while i > 0 and is_word(i - 1): i -= 1
        copy_move(0, max(0, min(i, max(0, n - 1))) - col)

    def copy_paragraph(direction):
        """copy_mode.rs { / }: the nearest blank row above or below in the whole buffer, at
        most 999 rows away, column 0; with none in reach the cursor stays put."""
        try:
            target = control.request("pane.paragraph", {"id": copy["pane"], "row": copy["abs"],
                                                        "direction": direction})["row"]
        except RuntimeError:
            return
        if target is None:
            return
        erase_copy_cursor()
        copy["abs"], copy["col"] = target, 0
        copy_reveal()
        copy_sync_selection()
        draw_copy_cursor()

    def copy_find(direction, from_cursor=True):
        """Search the visible rows from the cursor, then page through history in that direction."""
        query = copy["query"]
        if not query:
            return
        fold = query == query.lower()                      # smart case, as in tmux/herdr
        for _page in range(200):
            rows_ = copy_plain_rows()
            order = range(copy_row() + (1 if from_cursor else 0), len(rows_)) if direction > 0 \
                else range(copy_row() - (1 if from_cursor else 0), -1, -1)
            for r in order:
                hay = rows_[r].lower() if fold else rows_[r]
                needle = query.lower() if fold else query
                i = hay.find(needle)
                if i >= 0:
                    col = sum(max(1, _wcwidth(ch)) for ch in rows_[r][:i])
                    width = sum(max(1, _wcwidth(ch)) for ch in rows_[r][i:i + len(needle)])
                    erase_copy_cursor()
                    copy_set_row(r)
                    copy["col"] = col
                    # the current match, in absolute cells (copy_mode.rs search_current)
                    copy["match"] = ((copy["abs"], col), (copy["abs"], col + max(1, width) - 1))
                    matches = sum(1 for line in rows_ if (line.lower() if fold else line).count(needle))
                    copy["status"] = f"{matches} on screen"
                    copy_sync_selection()
                    draw_copy_cursor()
                    copy_bar()
                    return
            rect = copy_rect()
            if rect is None or not copy_scroll(-rect.height if direction < 0 else rect.height):
                break
            erase_copy_cursor()
            copy_set_row((len(copy_plain_rows()) - 1) if direction < 0 else 0)
            from_cursor = False
        copy["status"] = "no match"
        draw_copy_cursor()
        copy_bar()

    def copy_begin_selection():
        """copy_mode.rs begin_copy_mode_selection: anchor at the cursor, absolute rows."""
        rect = copy_rect()
        if rect is None:
            return
        copy["linewise"] = None
        copy["anchor"] = (copy["abs"], copy["col"])
        copy_sync_selection()
        copy_bar()

    def copy_select_line():
        """copy_mode.rs select_copy_mode_line: whole rows from here (V)."""
        rect = copy_rect()
        if rect is None:
            return
        copy["anchor"] = None
        copy["linewise"] = copy["abs"]
        copy_sync_selection()
        copy_bar()

    def copy_clear_selection():
        copy.update(anchor=None, linewise=None)
        clear_selection()

    def copy_key(key):
        """copy_mode.rs handle_copy_mode_key. The prefix key opens the prefix layer on top
        of copy mode (copy_mode.rs:17-24); esc clears a selection or search before it exits."""
        nonlocal mode
        if key.kind == "release":
            return
        prompt = copy["prompt"]
        if prompt is not None:                        # copy_mode.rs route_copy_search_prompt_key
            if key.code == "esc":
                copy["prompt"] = None
            elif key.code == "enter":
                copy["prompt"] = None
                if prompt["input"].text:              # request_copy_search: an empty query is no search
                    copy["query"], copy["dir"] = prompt["input"].text, prompt["dir"]
                    copy_find(copy["dir"])
            else:
                prompt["input"].handle_key(key)       # the prefix key too: the prompt owns the keys
            copy_bar()
            return
        if key.matches(PREFIX.code, PREFIX.mods):
            erase_copy_cursor()                       # panes.rs:672: the copy cursor is drawn in Copy mode only
            mode = Mode.PREFIX
            draw_prefix_bar()
            return
        copy_reveal()
        text = key_text(key)
        rect = copy_rect()
        page = max(1, rect.height - 2) if rect else 1     # copy_mode_page_lines
        half = max(1, rect.height // 2) if rect else 1
        if text == "q":
            exit_copy(False)
        elif key.matches("esc"):
            if copy["anchor"] is not None or copy["linewise"] is not None or copy["query"]:
                copy_clear_selection()
                copy.update(query="", status="", match=None)
                draw_copy_cursor()
                copy_bar()
            else:
                exit_copy(False)
        elif text == "y" or key.matches("enter"):
            exit_copy(True)
        elif text in ("v", " "):
            copy_begin_selection()
        elif text == "V":
            copy_select_line()
        elif text == "h" or key.matches("left"):
            copy_move(0, -1)
        elif text == "l" or key.matches("right"):
            copy_move(0, 1)
        elif text == "j" or key.matches("down"):
            copy_move(1, 0)
        elif text == "k" or key.matches("up"):
            copy_move(-1, 0)
        elif key.matches("b", hin.CTRL) or key.matches("pageup"):
            copy_move(-page, 0)
        elif key.matches("f", hin.CTRL) or key.matches("pagedown"):
            copy_move(page, 0)
        elif key.matches("u", hin.CTRL):
            copy_move(-half, 0)
        elif key.matches("d", hin.CTRL):
            copy_move(half, 0)
        elif text == "g":
            metrics = copy_metrics()
            erase_copy_cursor()
            copy_scroll(-(metrics.get("max_offset_from_bottom", 0) - metrics.get("offset_from_bottom", 0)))
            copy_set_row(0)
            copy_sync_selection()
            draw_copy_cursor()
        elif text == "G":
            metrics = copy_metrics()
            erase_copy_cursor()
            copy_scroll(metrics.get("offset_from_bottom", 0))
            copy_set_row(max(0, (rect.height if rect else 1) - 1))
            copy_sync_selection()
            draw_copy_cursor()
        elif text == "0" or key.matches("home"):
            copy_move(0, -copy["col"])
        elif text == "$" or key.matches("end"):
            line = copy_plain_rows()[copy_row()] if rect else ""
            last = sum(max(1, _wcwidth(ch)) for ch in line.rstrip()) - 1   # last_character_col
            copy_move(0, max(0, last) - copy["col"])
        elif text == "^":
            line = copy_plain_rows()[copy_row()] if rect else ""
            copy_move(0, (len(line) - len(line.lstrip())) - copy["col"])
        elif text in ("/", "?"):
            copy["prompt"] = {"dir": 1 if text == "/" else -1, "input": TextEditor()}
            copy_bar()
        elif text == "n":
            copy_find(copy["dir"])
        elif text == "N":
            copy_find(-copy["dir"])
        elif text in ("w", "b", "e"):
            copy_word(text)
        elif text == "{":
            copy_paragraph(-1)
        elif text == "}":
            copy_paragraph(1)

    def copy_paste(text):
        """A paste while the search prompt is open types into it (paste_into_active_text_input)."""
        if copy["prompt"] is not None:
            copy["prompt"]["input"].insert(text)
            copy_bar()

    # ── Key help (prefix+?, herdr Mode::KeybindHelp) ──
    help_state = {"rect": None, "lines": []}

    def help_key(key):
        """modal.rs handle_keybind_help_key: esc, enter or ? close the box."""
        if key.kind == "release":
            return
        if key.matches("esc") or key.matches("enter") or key_text(key) == "?":
            close_help()

    def close_help():
        leave_command_mode()      # an overlay leaves the mode under it as it was (copy mode included)
        request_render()

    def help_mouse(event):
        """overlays.rs:184-236: a press outside the box closes it; the box eats the rest."""
        rect = help_state["rect"]
        if event.kind != "press" or event.button != 0 or rect is None:
            return
        if not (rect.x <= event.x < rect.x + rect.width and rect.y <= event.y < rect.y + rect.height):
            close_help()

    def main_col():
        """First (1-based) column of the main area: flush against the sidebar separator."""
        return side["w"] + 1
    # herdr tab_scroll / reveal_focused_tab: the focused tab is centred once, whenever the tab
    # list, the focused tab or the bar width changed (state.rs tab_layout_changed,
    # composition.rs last_tab_bar_width); a manual scroll lasts until then.
    tab_scroll, tab_reveal, tab_seen = 0, True, None
    resized = {"hit": True}
    if not WINDOWS:     # a console reports a new size through its input records instead
        signal.signal(signal.SIGWINCH, lambda *_a: resized.update(hit=True))

    def slice_of(pane_id):
        return next((s for s in slices if s[0] == pane_id), None)

    pane_input = {}      # pane id -> the daemon's input state (what the program asked its terminal for)

    def input_state_of(pane_id):
        return pane_input.get(pane_id) or pin.DEFAULT_STATE

    def host_cursor():
        """Where the host cursor goes once a frame is written, and its DECSCUSR shape (0 =
        the host's default); sent with every frame (tab_surface.rs tab_surface_cursor,
        composition.rs, render_ansi.rs resolve_host_cursor_state). Returns ``(bytes, shape)``.

        Terminal, prefix and resize modes: the focused pane's cursor in the shape its program
        set, hidden while that pane is scrolled back or while its program hides it (the
        engine draws its own block and hides the real one; showing ours too would stack into
        one brighter block), and in prefix/resize also when it sits on the mode bar's row
        (restore_mode_bar). The rename box and the navigator's focused search line: the caret
        at the text editor's cursor, so an IME composes there (text_editor::render). Copy mode
        and every other overlay: hidden -- the copy cursor or a popup owns the screen, and the
        pane cursor blinking underneath was the IME's cue to open its candidate window in the
        wrong place. A hidden cursor is only hidden, never moved, so the IME keeps its anchor."""
        hide = b"\x1b[?25l"
        if mode in (Mode.RENAME, Mode.NAVIGATOR):
            caret = rename["caret"] if mode is Mode.RENAME else (nav["caret"] if nav["search"] else None)
            return (hide if caret is None else f"\x1b[{caret[1] + 1};{caret[0] + 1}H\x1b[?25h".encode()), 0
        if mode not in (Mode.TERMINAL, Mode.PREFIX, Mode.RESIZE):
            return hide, 0
        sl = slice_of(focused)
        state = pane_cursor.get(focused)
        if sl is None or state is None:
            return hide, 0
        shape = state["shape"]
        if (scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom"):
            return hide, shape
        rect = sl[1]
        at = state["at"]
        col = rect.x + 1 + min(at[0], max(0, rect.width - 1))
        row = rect.y + 1 + min(at[1], max(0, rect.height - 1))
        if mode is not Mode.TERMINAL and bottom_bar[0] and min(row, rows) == rows:
            return hide, 0
        return (f"\x1b[{min(row, rows)};{col}H".encode()
                + (hide if state["hidden"] else b"\x1b[?25h")), shape

    # ── Frames (herdr: ratatui Buffer + Terminal::draw) ──
    # Every drawing path writes cells; one render per loop turn composes the frame from
    # state and writes the diff against the frame before. Nothing on the host is ever
    # cleared and repainted: a closed popup is simply not composed any more.
    frame = {"previous": None, "dirty": True, "cursor": b"", "shape": 0, "title": None, "sent_title": _UNSENT}
    hostname = socket.gethostname()      # configure_window_title: resolved once

    def request_render():
        frame["dirty"] = True

    def invalidate():
        """Next frame is written in full (after a resize)."""
        frame["previous"] = None
        frame["dirty"] = True

    def invalidate_rect(rect):
        """Next frame rewrites this rectangle whatever the buffer believes is there: the
        host drew an IME pre-edit over those cells, which no frame of ours accounts for."""
        previous = frame["previous"]
        if previous is None:
            return
        previous.fill(rect, ("\0", sc.DEFAULT_STYLE))
        frame["dirty"] = True

    draw_sidebar = request_render      # the sidebar is composed every frame; callers only ask for one
    bottom_bar = [None]                # The mode bar (prefix / resize / copy) while one is up.

    def show_bottom_bar(line):
        bottom_bar[0] = line
        request_render()

    def restore_bottom():
        """Take the mode bar down; the pane row under it comes back with the next frame."""
        bottom_bar[0] = None
        request_render()

    def draw_prefix_bar():
        # herdr: entering prefix mode pops a mode bar on the bottom row (menus.rs
        # render_prefix_overlay). It spans only the main area; the sidebar is permanent
        # navigation and must not be covered.
        show_bottom_bar(format_prefix_bar(max(10, cols - side["w"]), PREFIX_NAME))

    chrome_state = {"chromed": [], "area": None, "tracks": []}   # Border and scrollbar geometry.
    side_scrolls = {}              # Scroll offset (in entries) per sidebar section: spaces / sessions / agents.
    scroll_state = {}    # pane id -> {"metrics": ..., "alt": bool} (scroll readings from the daemon)
    ui_map = {"bar": None, "hits": [], "sections": {}}   # Mouse hit areas: tab bar geometry plus sidebar hit rects.

    def compose_sidebar(buf):
        """The sidebar and the tab row into the frame; refreshes the mouse hit map."""
        nonlocal tab_scroll, tab_reveal, tab_seen
        names = [tab_label(index) for index in range(len(tabs_of()))]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(names))
        # mouse_chrome=True: herdr's "+" new-tab button and overflow scroll buttons.
        space = active_space()
        zoomed = {i for i, z in enumerate(space["zoomed"]) if z} if space else ()   # tabs.rs tab_label " Z"
        seen = (space["id"] if space else None, tuple(names), frozenset(zoomed), active_tab(),
                view["tab_bar_rect"].width)
        if seen != tab_seen:
            tab_seen, tab_reveal = seen, True
        bar = hui.compute_tab_bar_view(names, active_tab(), view["tab_bar_rect"],
                                       tab_scroll, tab_reveal, True, zoomed)
        tab_scroll, tab_reveal = bar.scroll, False
        ui_map["bar"] = bar
        tab_line = render_tab_bar(names, active_tab(), bar, view["tab_bar_rect"],
                                  tab_scroll, zoomed)
        rows_, agents = sidebar_model(spaces, listing, focused, side["ws"])
        for row in rows_:                     # the branch row, from the poll's git cache
            row["git"] = git_view(row["folder"])
        side_order[:] = [row["key"] for row in rows_]
        # app/window_title.rs, the default ui.window_title "{hostname}: {workspace}": the host
        # window follows the active space (a pane's own OSC 0/2 stops in its terminal).
        active = next((row for row in rows_ if row["active"]), None)
        frame["title"] = sanitize_window_title(f"{hostname}: {active['label'] if active else ''}")
        side_lines, ui_map["hits"], ui_map["sections"] = format_sidebar(
            rows_, agents, side["w"], rows, collapsed=side["collapsed"],
            scrolls=side_scrolls, sort=side["sort"],
            sessions=mark_open_sessions(sessions_cache["rows"], listing, focused),
            session_mode=side["sess_mode"], reveal_space=side["reveal"])
        if ui_map["sections"].get("spaces", {}).get("revealed"):
            side["reveal"] = False
        for key, section in ui_map["sections"].items():   # herdr ui.rs:247-252: compute_view clamps the scroll.
            side_scrolls[key] = section["scroll"]
        for y, line in enumerate(side_lines):
            buf.put_ansi(0, y, line, clip=side["w"])
        tab_rect = view["tab_bar_rect"]
        if tab_rect.width:
            buf.put_ansi(tab_rect.x, tab_rect.y, tab_line, clip=tab_rect.x + tab_rect.width)

    def compose_panes(buf):
        """Pane content from the row cache, then borders, titles and scrollbars on top
        (panes.rs render_panes: content first, chrome last)."""
        for pane_id, inner in slices:
            cache = pane_cells.get(pane_id, [])
            for row in range(inner.height):
                # The whole row goes in and the buffer clips it: cutting the list first
                # left a wide glyph's head on the last column without its tail, and the
                # terminal then drew it two columns wide, over the scrollbar or the border
                # (and every cell written after it in that run landed one column right).
                buf.put_cells(inner.x, inner.y + row, cache[row] if row < len(cache) else (),
                              clip=inner.x + inner.width)
        chromed, area = chrome_state["chromed"], chrome_state["area"]
        if chromed and area is not None:
            titles = {p["id"]: p.get("ally") or p["title"] for p in listing}
            for (x, y), (symbol, is_focused) in hui.pane_border_cells(chromed, area=area).items():
                buf.put_text(x, y, symbol, sc.style(fg=hui.ACCENT if is_focused else hui.PALETTE["border"]))
            for item in chromed:                          # panes.rs:614-665 titles on the top border
                rect = item["rect"]
                if "top" not in item["borders"] or rect.width <= 4:
                    continue
                title = hui.pane_border_title(titles.get(item["id"], ""), rect.width)
                if title is None:
                    continue
                colour = hui.ACCENT if item["focused"] else hui.OVERLAY0
                buf.put_text(rect.x + 1, rect.y, title, sc.style(fg=colour, bold=item["focused"]),
                             clip=rect.x + rect.width - 1)
        for pane_id, gutter, is_focused in chrome_state["tracks"]:
            if gutter is None:
                continue
            state = scroll_state.get(pane_id, {})
            metrics = state.get("metrics")
            if not hui.should_show_scrollbar(metrics) or state.get("alt"):
                continue
            thumb = hui.scrollbar_thumb(metrics, gutter)
            if thumb is None:
                continue
            track_colour, thumb_colour, thumb_symbol = hui.scrollbar_style(is_focused)
            for y in range(gutter.y, gutter.y + gutter.height):
                buf.put_text(gutter.x, y, "▕", sc.style(fg=track_colour))
            for y in range(thumb[0], min(thumb[0] + thumb[1], gutter.y + gutter.height)):
                buf.put_text(gutter.x, y, thumb_symbol, sc.style(fg=thumb_colour))

    def compose_selection(buf):
        """panes.rs render_selection_highlight: restyle the covered cells."""
        selection = sel["it"]
        if selection is None or not selection.visible:
            return
        sl = slice_of(selection.pane)
        if sl is None:
            return
        rect, metrics = sl[1], sel_metrics(selection.pane)
        highlight = sc.style(fg=hui.TEXT, bg=hui.SURFACE0)
        for row in range(rect.height):
            span = selection.span(row, rect.width, metrics)
            if span and span[1] > span[0]:
                buf.restyle(rect.x + span[0], rect.y + row, span[1] - span[0], lambda _s: highlight)

    def compose_copy_cursor(buf):
        """panes.rs:672-690 render_copy_mode_cursor: accent block, contrast text, bold."""
        if mode is not Mode.COPY:
            return
        rect = copy_rect()
        row = copy_row() if copy["pane"] is not None else -1
        if rect is None or not 0 <= row < rect.height or copy["col"] >= rect.width:
            return                                     # off screen: not drawn (composition.rs)
        x, y = rect.x + copy["col"], rect.y + row
        if buf.get(x, y)[0] == "":
            x -= 1
        buf.restyle(x, y, 1, lambda _s: sc.style(fg=hui.panel_contrast_fg(), bg=hui.ACCENT, bold=True))

    def compose_overlays(buf):
        """The mode bar, notices and the modal layer for the current mode (ui.rs render)."""
        overlay_open = mode in (Mode.GLOBAL_MENU, Mode.NAVIGATOR, Mode.RENAME, Mode.KEYBIND_HELP)
        if bottom_bar[0] and not overlay_open:          # composition.rs: no mode bar under an overlay
            buf.put_ansi(side["w"], rows - 1, bottom_bar[0], clip=cols)
        for y, x, line in toast_view["lines"] + copy_note["lines"]:
            buf.put_ansi(x, y, line)
        layer = {Mode.GLOBAL_MENU: menu, Mode.NAVIGATOR: nav, Mode.RENAME: rename,
                 Mode.KEYBIND_HELP: help_state}.get(mode)
        if layer is not None:
            for y, x, line in layer.get("lines") or ():
                buf.put_ansi(x, y, line)

    def render():
        """Compose the frame from state and write what changed since the last one."""
        if not frame["dirty"]:
            return
        frame["dirty"] = False
        buf = sc.ScreenBuffer(cols, rows)
        compose_sidebar(buf)
        compose_panes(buf)
        compose_selection(buf)
        compose_copy_cursor(buf)
        compose_overlays(buf)
        data = buf.diff(frame["previous"])
        cursor, shape = host_cursor()
        if data or cursor != frame["cursor"] or shape != frame["shape"]:
            if shape != frame["shape"]:       # write_host_cursor_state: the shape after the move, before show/hide
                split = cursor.rfind(b"\x1b[?25")
                cursor_out = cursor[:split] + f"\x1b[{shape} q".encode() + cursor[split:]
            else:
                cursor_out = cursor
            _write_all(data + cursor_out)
        frame["previous"], frame["cursor"], frame["shape"] = buf, cursor, shape
        if frame["sent_title"] is _UNSENT or frame["sent_title"] != frame["title"]:   # sync_window_title
            _write_all(window_title_bytes(frame["title"]))
            frame["sent_title"] = frame["title"]

    # ── Notices (herdr status.rs): the clipboard feedback box and failure toasts, each in
    # its own slot (client/shell/state.rs copy_feedback + visible_notification) ──
    toasts = ToastQueue()
    toast_view = {"rect": None, "lines": []}
    copy_note = {"text": None, "deadline": 0.0, "rect": None, "lines": []}

    def terminal_area():
        return hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs_of()))["terminal_area"]

    def layout_notice():
        """composition.rs: the toast, then the copy box above it when they would meet."""
        shown = toasts.visible
        if shown is None:
            toast_view.update(rect=None, lines=[])
        else:
            rect, lines = format_toast(shown["text"], shown["context"], shown["kind"], hui.Rect(0, 0, cols, rows))
            toast_view.update(rect=rect, lines=lines)
        if copy_note["text"] is None:
            copy_note.update(rect=None, lines=[])
        else:
            area = terminal_area()
            offset = copy_feedback_offset(copy_note["text"], area, toast_view["rect"])
            rect, lines = format_copy_feedback(copy_note["text"], area, offset)
            copy_note.update(rect=rect, lines=lines)

    def show_copy_feedback():
        """state.rs show_copy_feedback: "copied to clipboard" for two seconds."""
        copy_note.update(text="copied to clipboard", deadline=time.monotonic() + COPY_FEEDBACK_DURATION)
        layout_notice()
        request_render()

    def report_failure(text, pane=None):
        """A failure the user must see (herdr: a NeedsAttention toast), e.g. input a pane refused."""
        title, _, context = text.partition(": ")
        toasts.push({"kind": "needs_attention", "text": title, "context": context, "pane": pane},
                    time.monotonic(), TOAST_DURATION)
        layout_notice()
        request_render()

    def tick_notices(now):
        changed = toasts.tick(now, TOAST_DURATION)
        if copy_note["text"] is not None and now >= copy_note["deadline"]:
            copy_note["text"] = None
            changed = True
        if changed:
            layout_notice()
            request_render()

    def draw_help_overlay():
        # herdr: "?" shows the full key help (keybind_help), centered in the main area,
        # closed by esc/enter/?. Width is clamped to the main area so narrow screens do not overflow.
        avail = cols - main_col() + 1
        width = max(24, min(46, avail))
        lines = format_help_lines(width=width)
        top = max(2, (rows - len(lines)) // 2)
        left = main_col() + max(0, (avail - width) // 2)
        help_state["rect"] = hui.Rect(left - 1, top - 1, width, len(lines))
        box = f"{hui.sgr_bg(hui.PANEL_BG)}{hui.sgr_fg(hui.TEXT)}"
        help_state["lines"] = [(top - 1 + index, left - 1, box + line) for index, line in enumerate(lines)]
        request_render()

    def open_help():
        nonlocal mode
        mode = Mode.KEYBIND_HELP
        draw_help_overlay()

    # ── Mouse selection and copy (herdr selection.rs + actions.rs copy_selection) ──
    # herdr keeps capturing the mouse and implements selection itself: dragging paints the
    # selection background, releasing copies (copy_on_select defaults to true), and a
    # double-click selects a word. Rows are absolute (screen-buffer coordinates), so a
    # selection stays on its text while the pane scrolls or its program prints more.
    pane_cells = {}     # pane id -> rows of cells for the viewport (the daemon's frames, parsed)
    sel = {"it": None, "clear_at": None, "autoscroll": None, "autoscroll_at": None}
    last_pane_click = {"pane": None, "row": 0, "col": 0, "at": 0.0}   # PaneClickState (app/mod.rs:82-97)

    def sel_metrics(pane_id):
        return scroll_state.get(pane_id, {}).get("metrics") or {}

    def paint_selection():
        request_render()

    def clear_selection():
        """actions.rs clear_selection: forget it; the next frame shows the rows plain."""
        word["it"] = None
        sel["clear_at"] = None
        sel["autoscroll"] = None
        sel["autoscroll_at"] = None
        if sel["it"] is not None:
            sel["it"] = None
            request_render()

    def selection_text(selection):
        (start, end) = selection.ordered()
        try:
            return control.request("pane.extract", {"id": selection.pane, "start": list(start), "end": list(end)})["text"]
        except RuntimeError:
            return ""

    def copy_selection():
        """actions.rs copy_selection: the text between the endpoints, then the selection is gone."""
        selection = sel["it"]
        if selection is None:
            return
        if not selection.finalized and not selection.finish():
            clear_selection()
            return
        text = selection_text(selection)
        clear_selection()
        if text.strip():
            _clipboard(text)
            show_copy_feedback()

    def pane_at(x, y):
        """Which pane a zero-based screen cell falls in: ``(pane_id, inner rect)`` for a
        cell inside the content, or None (mouse.rs pane_at)."""
        for pane_id, rect in slices:
            if rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height:
                return pane_id, rect
        return None

    def pane_frame_at(x, y):
        """The pane whose outer frame (border included) holds the cell (mouse.rs pane_frame_at)."""
        for item in chrome_state["chromed"]:
            rect = item["rect"]
            if rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height:
                return item["id"]
        return None

    def pane_row_text(pane_id, row):
        """A cached viewport row as plain text, one entry per display column."""
        cache = pane_cells.get(pane_id, [])
        if row >= len(cache):
            return []
        cells = []
        for symbol, _style in cache[row]:
            cells.append(symbol if symbol else cells[-1] if cells else " ")
        return cells

    word = {"it": None}      # the held second press (word_selection.rs ClientWordSelection)
    pane_geom = {}           # pane -> (inner width, inner height, alternate screen) as last laid out
    pane_rev = {}            # pane -> the program-output revision of its last frame

    def note_pane_geometry(pane_id, width, height, alt):
        """state.rs, per pane surface: a selection is a range of the pane's buffer at its
        current size and screen, so a new inner size or a switch between the primary and the
        alternate screen drops it (a word gesture included) and copy mode's v/V anchor."""
        before, pane_geom[pane_id] = pane_geom.get(pane_id), (width, height, alt)
        if before is None or before == pane_geom[pane_id]:
            return
        owner = word["it"]["pane"] if word["it"] is not None else (sel["it"].pane if sel["it"] is not None else None)
        if owner == pane_id:
            clear_selection()
        if copy["pane"] == pane_id and (copy["anchor"] is not None or copy["linewise"] is not None):
            copy.update(anchor=None, linewise=None)
            if mode is Mode.COPY:
                copy_bar()

    def pane_row_string(pane_id, abs_row, width):
        """One absolute row's text, read the way herdr's PaneSelectionRead reads it."""
        try:
            return control.request("pane.extract", {"id": pane_id, "start": [abs_row, 0],
                                                    "end": [abs_row, max(0, width - 1)]})["text"]
        except RuntimeError:
            return None

    def begin_word_selection(pane_id, rect, row, col):
        """word_selection.rs request_word_selection: a second press anchors on the token under
        it; nothing is copied until the press is released, and holding it while dragging (or
        wheeling) extends the selection by whole words. A press on a separator (no token)
        cancels the gesture. A pane whose program reads the mouse keeps its own double-click."""
        if pin.wants_mouse(input_state_of(pane_id)):
            return False
        abs_row = _abs_row(row, sel_metrics(pane_id))
        text = pane_row_string(pane_id, abs_row, rect.width)
        bounds = selmod.word_bounds_at_column(text, col) if text is not None else None
        clear_selection()
        if bounds is None:
            return True
        word["it"] = {"pane": pane_id, "anchor": (abs_row, col), "bounds": bounds,
                      "cursor": (abs_row, col), "width": rect.width, "rows": {abs_row: text},
                      "dragged": False, "revision": pane_rev.get(pane_id)}
        update_word_selection()
        return True

    def update_word_selection():
        """word_selection.rs update_word_selection: the anchor token joined with the token (or,
        on a separator, the single cell) under the pointer, across rows."""
        gesture = word["it"]
        cursor_row, cursor_col = gesture["cursor"]
        text = gesture["rows"].get(cursor_row)
        if text is None:
            text = pane_row_string(gesture["pane"], cursor_row, gesture["width"]) or ""
            gesture["rows"] = {gesture["anchor"][0]: gesture["rows"][gesture["anchor"][0]], cursor_row: text}
        start_col, end_col = selmod.word_bounds_at_column(text, cursor_col) or (cursor_col, cursor_col)
        anchor_row, (anchor_start, anchor_end) = gesture["anchor"][0], gesture["bounds"]
        start = min((anchor_row, anchor_start), (cursor_row, start_col))
        end = max((anchor_row, anchor_end), (cursor_row, end_col))
        sel["it"] = Selection(gesture["pane"], start, end, "dragging")
        paint_selection()

    def drag_word_selection(abs_row, col):
        gesture = word["it"]
        if gesture["cursor"] == (abs_row, col):
            return
        gesture["cursor"], gesture["dragged"] = (abs_row, col), True
        update_word_selection()

    def finish_word_selection():
        """word_selection.rs, on release: copy (copy_on_select); a dragged selection is then
        gone, an undragged double-click stays lit for PANE_COPY_HIGHLIGHT_DURATION."""
        gesture, word["it"] = word["it"], None
        clear_autoscroll()
        selection = sel["it"]
        if selection is None:
            return
        selection.finish()
        text = selection_text(selection)
        if text.strip():
            _clipboard(text)
            show_copy_feedback()
        if gesture["dragged"]:
            clear_selection()
        else:
            sel["clear_at"] = time.monotonic() + PANE_COPY_HIGHLIGHT_DURATION

    def drag_selection_to(x, y, rect, metrics):
        """update_selection_cursor_with_metrics: move the held word gesture or the selection."""
        if word["it"] is not None:
            viewport_row = min(max(y - rect.y, 0), max(0, rect.height - 1))
            col = min(max(x - rect.x, 0), max(0, rect.width - 1))
            drag_word_selection(_abs_row(viewport_row, metrics), col)
        elif sel["it"] is not None:
            sel["it"].drag(x, y, rect, metrics)

    def paint_rows(pane_id, rendered, cur=None, clear=False, hidden=None, shape=None):
        """Take a daemon frame into the pane's row cache (and cursor state). ``clear``
        means a whole new viewport (a scroll, a relayout): rows it does not mention are
        blank. Nothing is written to the host here; the next frame composes it."""
        if cur is not None or hidden is not None or shape is not None:
            state = pane_cursor.setdefault(pane_id, {"at": [0, 0], "hidden": False, "shape": 0})
            if cur is not None:
                state["at"] = list(cur)
            if hidden is not None:
                state["hidden"] = bool(hidden)
            if shape is not None:
                state["shape"] = shape if 0 <= shape <= 6 else 0   # render_ansi.rs normalize_cursor_shape
        sl = slice_of(pane_id)
        height = sl[1].height if sl else 0
        cache = pane_cells.setdefault(pane_id, [])
        if clear or len(cache) != height:
            cache[:] = [[] for _ in range(height)]
        for row_str, line in rendered.items():
            row = int(row_str)
            if row < height:                    # Only inside our own rectangle (herdr hard-clips).
                cache[row] = sc.cells_from_ansi(line)
        request_render()

    def relayout(reload=True):
        """Recompute the pane rectangles for the active tab, resize the panes that changed,
        and refresh their viewports (ui.rs compute_view + panes.rs compute_pane_infos).

        ``reload`` pulls the daemon's layout first; a divider drag passes False so it renders
        from its own in-memory tree instead. Pulling (and pushing) the layout every drag step
        cost two synchronous daemon round-trips, which stalled the drag whenever the daemon was
        busy repainting a pane (a 165 ms freeze was seen mid-drag). During a drag the panel owns
        the layout, so it keeps it local and persists once at drag end."""
        nonlocal slices
        if reload:
            reload_layout()
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs_of()))
        term = view["terminal_area"]
        # herdr: only the active tab is drawn; its layout is the BSP tree cut into rectangles (layout.rs collect_panes).
        vis = visible_tabs()
        tree = vis[active_tab()] if vis else None
        if tree is None:
            slices = []
            chrome_state.update(chromed=[], area=term, tracks=[])
            request_render()
            return
        if tab_zoomed(active_tab()):
            # panes.rs:246-252: a zoomed pane of a multi-pane tab keeps a full frame (and its
            # title) as the visible cue that the others are still there.
            chromed = [{"id": focused, "rect": term, "focused": True,
                        "borders": {"top", "bottom", "left", "right"}
                        if len(hui.pane_ids(tree)) > 1 else set()}]
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
            gutter = (None if content == pane_inner else
                      hui.Rect(pane_inner.x + pane_inner.width - 1, pane_inner.y,
                               1, pane_inner.height))
            tracks.append((item["id"], gutter, item["focused"]))
        chrome_state.update(chromed=chromed, area=term, tracks=tracks)
        for pane_id, inner in slices:
            note_pane_geometry(pane_id, inner.width, inner.height, bool(scroll_state.get(pane_id, {}).get("alt", False)))
        sizes = {pane_id: [inner.height, inner.width] for pane_id, inner in slices
                 if inner.width >= 2 and inner.height >= 2}
        if not reload:
            # A drag: resize fire-and-forget so the divider never blocks on a busy daemon
            # (herdr's client never waits on its server for a resize). The panes' new content
            # arrives as broadcast frames; until then the cache is drawn clipped, so nothing is
            # refetched here.
            if sizes:
                control.notify("panes.resize", {"sizes": sizes})
            request_render()
            return
        refresh = set()
        try:
            resized = control.request("panes.resize", {"sizes": sizes})["resized"] if sizes else {}
        except RuntimeError:
            resized = {}
        for pane_id, inner in slices:
            # A resized pane's rows arrive as a frame once the daemon has re-wrapped them
            # (herdr's client never waits on its server for a resize); until then the cache
            # is drawn clipped. Only a pane seen for the first time, or one the daemon
            # already had at this size while our cache does not match, is fetched here.
            if not resized.get(pane_id, False) and len(pane_cells.get(pane_id, [])) != inner.height:
                refresh.add(pane_id)
            if pane_id not in pane_cells:
                refresh.add(pane_id)
        for pane_id, _inner in slices:
            if pane_id not in refresh:
                continue
            try:
                screen = control.request("pane.screen", {"id": pane_id})
            except RuntimeError:
                continue
            scroll_state[pane_id] = {"metrics": screen.get("scroll"),
                                     "alt": screen.get("alt_screen", False)}
            pane_cursor[pane_id] = {"at": list(screen["cursor"]),
                                    "hidden": bool(screen.get("cursor_hidden")),
                                    "shape": screen["cursor_shape"] if 0 <= screen["cursor_shape"] <= 6 else 0}
            if screen.get("input"):
                pane_input[pane_id] = screen["input"]
            paint_rows(pane_id, {str(i): line for i, line in enumerate(screen["rows"])}, clear=True)
        request_render()

    def focus(pane_id, *, force_layout=False):
        nonlocal focused
        key = space_of(pane_id)
        if key is not None and key != side["ws"]:   # herdr: focusing a pane in another workspace activates it
            side["ws"] = key
            side["reveal"] = True
            force_layout = True
            refresh_sessions()          # the sessions list belongs to the space's folder (as switch_space)
        last_focus[side["ws"]] = pane_id
        changed = focused != pane_id
        focused = pane_id
        if changed:
            focus_moved()
        unseen_turn.difference_update(_in_view())   # herdr switch_tab: everything now on screen counts as seen
        for pane in listing:
            if pane["id"] not in unseen_turn and not pane.get("card"):
                pane.pop("unseen", None)
        try:
            control.request("pane.focused", {"id": pane_id})   # Focusing marks the pane as seen.
        except RuntimeError:
            pass
        # Changing focus while zoomed means the new pane takes over the zoom (herdr zoom follows focus).
        if force_layout or (changed and (tab_zoomed(active_tab()) or slice_of(pane_id) is None)):
            relayout()
            return
        if changed:     # Focus moved: the border highlight moves with it.
            for item in chrome_state["chromed"]:
                item["focused"] = item["id"] == pane_id
            chrome_state["tracks"] = [(pid, gutter, pid == pane_id) for pid, gutter, _f in chrome_state["tracks"]]
        request_render()

    def focus_moved():
        """state.rs, when the focused pane changes: a selection (or held word gesture) whose
        pane lost the focus goes (selection_focus_lost); copy mode parks while its pane is
        unfocused -- its selection hidden, the mode back to terminal -- and resumes, selection
        rebuilt from its anchor, when the focus comes back."""
        nonlocal mode
        owner = word["it"]["pane"] if word["it"] is not None else (sel["it"].pane if sel["it"] is not None else None)
        if owner is not None and owner != focused:
            clear_selection()
        if copy["pane"] is None:
            return
        if copy["pane"] == focused:
            if mode is Mode.TERMINAL:
                mode = Mode.COPY
                copy_bar()
                draw_copy_cursor()
            if sel["it"] is None:
                copy_sync_selection()
        elif mode is Mode.COPY:
            mode = Mode.TERMINAL
            restore_bottom()

    def pane_gone(pane_id):
        """state.rs: a pane that left the layout ends its copy mode, any selection in it and a
        mouse gesture it held."""
        nonlocal mode
        if pane_gesture["it"] is not None and pane_gesture["it"]["pane"] == pane_id:
            pane_gesture["it"] = None
        owner = word["it"]["pane"] if word["it"] is not None else (sel["it"].pane if sel["it"] is not None else None)
        if owner == pane_id:
            clear_selection()
        if copy["pane"] == pane_id:
            copy.update(pane=None, anchor=None, linewise=None, prompt=None)
            if mode is Mode.COPY:
                mode = Mode.TERMINAL
                restore_bottom()

    def new_pane(argv, title, *, place, cwd=None):
        """Ask the daemon for a pane seated at ``place`` (Daemon._seat): {"split": pane},
        {"tab": pane}, {"space": id} or {"space": True}; "name" inside it names a new tab. The pane lands in
        the focused job's folder unless ``cwd`` says otherwise (herdr terminal.new_cwd="current")."""
        nonlocal listing
        space = active_space()
        if space is not None and not space["tabs"] and place.get("space") is not True:
            # A remembered space with no panes is on screen: the focused pane is elsewhere,
            # so a split/tab/grid of it would land in another space.
            place = {"space": space["id"], "name": place.get("name") or title}
            cwd = cwd or space["folder"]
        current = next((p for p in listing if p["id"] == focused), {})
        cwd = cwd or ((current.get("foreground") or {}).get("cwd")
                      or (space["folder"] if space else os.getcwd()))
        out = control.request("pane.create", {
            "argv": argv, "cwd": cwd, "title": title, "place": place,
            "env": {"MISAKA_THEME": hui.theme_variant()}})
        listing = panes()
        reload_layout()
        focus(out["pane_id"], force_layout=True)
        return out["pane_id"]

    def new_space_here():
        """herdr new_workspace (the " new" button): another space in the same folder, its root a shell."""
        space = active_space()
        new_pane([_shell()], "shell", place={"space": True},
                 cwd=space["folder"] if space else os.getcwd())

    def switch_tab(index):
        """herdr switch_tab: the tab's own zoom state comes back with it."""
        vis = visible_tabs()
        if 0 <= index < len(vis):
            focus(hui.pane_ids(vis[index])[0], force_layout=True)

    def close_focused():
        """Close the focused pane and move focus to the next live one. Returns True when no panes remain."""
        nonlocal listing
        vis, page_idx = visible_tabs(), active_tab()
        page_ids = hui.pane_ids(vis[page_idx]) if page_idx < len(vis) else []
        survivors = [pid for pid in page_ids if pid != focused]
        closing = focused
        control.request("pane.close", {"id": closing})
        pane_gone(closing)
        listing = panes()
        if not any(p["alive"] for p in listing):
            return True
        refocus(survivors[0] if survivors else None, page_idx)
        return False

    # ── Mouse (herdr app/input/mouse.rs handle_mouse + input/mod.rs handle_mouse_from_input_source) ──
    # Coordinates are zero-based screen cells; the modal layers take the event first, then
    # chrome (tab bar, sidebar, dividers, scrollbars), then the panes.

    def tab_bar_mouse(event):
        """Tab row: click a tab, a scroll button or "+"; the wheel over a tab or a button cycles
        tabs, over the empty rest of the row it does nothing (client/shell/mouse.rs)."""
        nonlocal tab_scroll
        bar = ui_map.get("bar")
        if bar is None:
            return False

        def over(rect):
            return rect.width > 0 and rect.x <= event.x < rect.x + rect.width

        if event.kind in ("wheel_up", "wheel_down"):
            vis = visible_tabs()
            hit = any(over(rect) for rect in bar.tab_hit_areas) or any(
                over(rect) for rect in (bar.scroll_left_hit_area, bar.scroll_right_hit_area,
                                        bar.new_tab_hit_area))
            if vis and hit:
                switch_tab((active_tab() + (1 if event.kind == "wheel_down" else -1)) % len(vis))
            return True
        if event.kind != "press" or event.button != 0:
            return True
        for index, rect in enumerate(bar.tab_hit_areas):
            if rect.width and rect.x <= event.x < rect.x + rect.width:
                switch_tab(index)
                return True
        if over(bar.scroll_left_hit_area):
            tab_scroll = max(0, tab_scroll - 1)
            draw_sidebar()
            return True
        if over(bar.scroll_right_hit_area):
            tab_scroll = min(tab_scroll + 1, max(0, len(visible_tabs()) - 1))
            draw_sidebar()
            return True
        rect = bar.new_tab_hit_area
        if rect.width and rect.x <= event.x < rect.x + rect.width:
            new_pane([_shell()], "shell", place={"tab": focused})
        return True

    def sidebar_mouse(event):
        """Sidebar: the wheel scrolls the section under the pointer; a click acts on the hit
        rect drawn last (the toggle sits over the list)."""
        if event.kind in ("wheel_up", "wheel_down"):
            # client/shell/mouse.rs: one entry per notch, clamped, and only over a list's body
            # (its header, divider and footer rows do not scroll it).
            for key, section in ui_map["sections"].items():
                rect = section.get("body")
                if rect and rect.height and rect.width and rect.y <= event.y < rect.y + rect.height \
                        and rect.x <= event.x < rect.x + rect.width:
                    step = 1 if event.kind == "wheel_down" else -1
                    side_scrolls[key] = max(0, min(section["scroll"] + step, section["max_scroll"]))
                    draw_sidebar()
                    return
            return
        if event.kind != "press" or event.button != 0:
            return
        hit = next((action for rect, action in reversed(ui_map["hits"])
                    if rect.x <= event.x < rect.x + rect.width
                    and rect.y <= event.y < rect.y + rect.height), None)
        if hit is None:
            return
        sidebar_action(hit)

    def sidebar_action(hit):
        nonlocal mode
        if hit[0] == "pane":
            focus(hit[1])
        elif hit[0] == "space":                   # herdr: click a space = switch workspace.
            switch_space(hit[1])
        elif hit[0] == "new":                     # herdr: new workspace in the same folder.
            new_space_here()
        elif hit[0] == "prefix":                  # a mouse press of the prefix key: next key is a command
            if mode is Mode.PREFIX:               # pressed again: cancel, like esc
                leave_command_mode()
            else:
                mode = Mode.PREFIX
                draw_prefix_bar()
        elif hit[0] == "sisters":                 # agents footer: summon a Sister as a new tab.
            open_menu()
        elif hit[0] == "sessgroup":               # MISAKA: the Last Order / Sisters groups fold.
            sess_folds.symmetric_difference_update({hit[1]})
            refresh_sessions()
            draw_sidebar()
        elif hit[0] == "sessnew":                 # agents footer: a fresh Last Order session, a space of its own here
            space = active_space()
            new_pane([sys.executable, "-m", "misaka", "chat"], LO_TITLE, place={"space": True},
                     cwd=space["folder"] if space else None)
        elif hit[0] == "sessmode":                # sessions header: this space <-> every folder
            side["sess_mode"] = "all" if side["sess_mode"] == "here" else "here"
            refresh_sessions()
            draw_sidebar()
        elif hit[0] == "sess-open":
            reopen_session(hit)
        elif hit[0] == "toggle":                  # herdr toggle_sidebar: 26 columns <-> 4.
            side["collapsed"] = not side["collapsed"]
            side["w"] = SIDEBAR_COLLAPSED_W if side["collapsed"] else SIDEBAR_W
            relayout()
        elif hit[0] == "sort":                    # herdr: click the header label to flip grouped / priority.
            side["sort"] = "priority" if side["sort"] == "grouped" else "grouped"
            draw_sidebar()

    pane_gesture = {"it": None}   # client/shell/mouse.rs pane_mouse_gesture: {pane, rect, button}

    def forward_mouse(pane_id, rect, event):
        """Give the event to the program in the pane when it asked for mouse reports
        (mouse.rs push_pane_mouse_event: coordinates relative to the pane, never below 0).
        True when it went there."""
        state = input_state_of(pane_id)
        data = pin.encode_mouse(event, max(0, event.x - rect.x), max(0, event.y - rect.y), state)
        if data is None:
            return False
        send_to_pane(pane_id, data)
        return True

    def forward_press(pane_id, rect, event):
        """A press the pane's program takes starts a gesture: that button's drags and its
        release go to the same pane, even once the pointer has left it (mouse.rs)."""
        if not forward_mouse(pane_id, rect, event):
            return False
        pane_gesture["it"] = {"pane": pane_id, "rect": rect, "button": event.button, "last": event}
        return True

    def pane_wheel(pane_id, rect, event):
        """mouse.rs handle_terminal_wheel: the pane's own routing first (mouse report or
        alternate-screen arrows), else the host scrolls its history."""
        state = input_state_of(pane_id)
        routing = pin.wheel_routing(state)
        if routing == "mouse_report":
            forward_mouse(pane_id, rect, event)
            return
        if routing == "alternate_scroll":
            data = pin.encode_alternate_scroll(event, state)
            if data:
                send_to_pane(pane_id, data)
            return
        if event.kind not in ("wheel_up", "wheel_down"):
            return
        scroll_pane(pane_id, -MOUSE_SCROLL_LINES if event.kind == "wheel_up" else MOUSE_SCROLL_LINES)

    def scroll_pane(pane_id, delta):
        try:
            out = control.request("pane.scroll", {"id": pane_id, "delta": delta})
        except RuntimeError:
            return
        scroll_state.setdefault(pane_id, {})["metrics"] = out["scroll"]
        paint_rows(pane_id, {str(i): line for i, line in enumerate(out["rows"])}, clear=True)

    def pane_press(pane_id, rect, event):
        """A left press inside a pane (mouse.rs:590-612): leave any command layer, focus
        it, then either hand the press to its program or anchor a selection."""
        nonlocal mode
        if mode is Mode.COPY:
            exit_copy(False)
        elif mode is not Mode.TERMINAL:
            leave_command_mode()
        if focused != pane_id:
            focus(pane_id)
        if forward_press(pane_id, rect, event):
            sel["it"] = None
            return
        sel["it"] = Selection.at(pane_id, event.y - rect.y, event.x - rect.x, sel_metrics(pane_id))

    def pane_double_click(pane_id, rect, event, now):
        """state.rs ClientPaneClick::is_double_click_for: the second unmodified left press
        within 350 ms, in the same pane, at most one row and one column away, starts a word
        selection (begin_word_selection)."""
        if mode is not Mode.TERMINAL or event.mods & ~hin.LOCK_MASK:
            last_pane_click["pane"] = None
            return False
        row, col = event.y - rect.y, event.x - rect.x
        last = last_pane_click
        double = (last["pane"] == pane_id and now - last["at"] <= PANE_DOUBLE_CLICK_WINDOW
                  and abs(last["row"] - row) <= 1 and abs(last["col"] - col) <= 1)
        last_pane_click.update(pane=None if double else pane_id, row=row, col=col, at=now)
        return double and begin_word_selection(pane_id, rect, row, col)

    def handle_mouse(event, now):
        nonlocal mode
        if mode is Mode.GLOBAL_MENU:
            menu_mouse(event)
            return
        if mode is Mode.NAVIGATOR:
            nav_mouse(event)
            return
        if mode is Mode.KEYBIND_HELP:
            help_mouse(event)
            return
        if mode is Mode.RENAME:
            rename_mouse(event)
            return
        x, y = event.x, event.y
        gesture = pane_gesture["it"]
        if gesture is not None and event.kind in ("press", "drag", "release"):
            if event.kind != "press" and event.button == gesture["button"]:
                sl = slice_of(gesture["pane"])
                gesture["last"] = event
                forward_mouse(gesture["pane"], sl[1] if sl is not None else gesture["rect"], event)
                if event.kind == "release":
                    pane_gesture["it"] = None
            return
        in_sidebar = x < side["w"]
        on_tab_bar = not in_sidebar and y == 0
        if event.kind == "press" and event.button == 0:
            hit = None if in_sidebar or on_tab_bar else pane_at(x, y)
            if hit is not None and pane_double_click(hit[0], hit[1], event, now):
                return
            clear_selection()
            if not in_sidebar and not on_tab_bar and press_on_chrome(x, y):
                grabbed = drag[0]
                if grabbed and grabbed["kind"] == "bar" and focused != grabbed["pane"]:
                    focus(grabbed["pane"])          # mouse.rs:422-445: a scrollbar press focuses its pane
                if mode is Mode.COPY:
                    exit_copy(False)
                elif mode is not Mode.TERMINAL:
                    leave_command_mode()
                return
            if on_tab_bar:
                tab_bar_mouse(event)
                return
            if in_sidebar:
                sidebar_mouse(event)
                return
            if hit is not None:
                pane_press(hit[0], hit[1], event)
                return
            frame = pane_frame_at(x, y)
            if frame is not None:                    # a border click focuses (mouse.rs:613-624)
                if mode is Mode.COPY:
                    exit_copy(False)
                elif mode is not Mode.TERMINAL:
                    leave_command_mode()
                if focused != frame:
                    focus(frame)
            return
        if event.kind == "press":                    # middle / right press
            clear_selection()
            hit = None if in_sidebar or on_tab_bar else pane_at(x, y)
            if hit is not None:
                forward_press(hit[0], hit[1], event)
            return
        if event.kind == "drag":
            if event.button == 0 and (word["it"] is not None or sel["it"] is not None):
                update_selection_drag(x, y)
                return
            if drag[0] is not None and event.button == 0:
                drag_to(x, y)
            return                                   # a drag nobody captured goes nowhere (mouse.rs)
        if event.kind == "release":
            if event.button == 0 and word["it"] is not None:
                finish_word_selection()
                return
            if event.button == 0 and sel["it"] is not None:
                selection = sel["it"]
                clear_autoscroll()                   # herdr stops autoscroll on mouse-up
                drag_end()
                if selection.just_click:
                    sel["it"] = None
                elif not selection.finalized:
                    copy_selection()                 # copy_on_select
                return
            if drag[0] is not None:
                drag_end()
            return                                   # a release nobody captured goes nowhere (mouse.rs)
        if event.kind.startswith("wheel"):
            # mouse.rs scroll_in_progress_selection: while a selection (or a held word gesture)
            # is being made, a notch anywhere scrolls its pane and moves only its cursor.
            active = word["it"]["pane"] if word["it"] is not None else (
                sel["it"].pane if sel["it"] is not None and sel["it"].in_progress else None)
            if active is not None and event.kind in ("wheel_up", "wheel_down"):
                sl = slice_of(active)
                if sl is not None:
                    scroll_pane(active, -MOUSE_SCROLL_LINES if event.kind == "wheel_up" else MOUSE_SCROLL_LINES)
                    drag_selection_to(x, y, sl[1], sel_metrics(active))
                    paint_selection()
                return
            if on_tab_bar:
                tab_bar_mouse(event)
                return
            if in_sidebar:
                sidebar_mouse(event)
                return
            # mouse.rs: only a pane's content takes the wheel (a border or gap does nothing);
            # it focuses that pane and leaves a retained selection alone -- the selection goes
            # when the focus leaves its pane (state.rs selection_focus_lost).
            hit = pane_at(x, y)
            if hit is not None:
                if focused != hit[0]:
                    focus(hit[0])
                pane_wheel(hit[0], hit[1], event)
            return
        if event.kind == "move" and mode is Mode.TERMINAL and not in_sidebar:
            hit = pane_at(x, y)
            if hit is not None:
                forward_mouse(hit[0], hit[1], event)

    def clear_autoscroll():
        sel["autoscroll"] = None
        sel["autoscroll_at"] = None

    def update_selection_drag(x, y):
        """selection.rs update_selection_drag: extend the selection to the pointer; when the
        pointer is past (or on) the pane's top/bottom edge, scroll the pane so the selection
        runs beyond the viewport, then keep scrolling on a timer while it is held there
        (selection_autoscroll_tick). The immediate step scales with the distance past the edge."""
        gesture = word["it"]
        selection = sel["it"]
        pane = gesture["pane"] if gesture is not None else selection.pane
        sl = slice_of(pane)
        if sl is None:
            return
        rect = sl[1]
        top = rect.y
        bottom = rect.y + max(0, rect.height - 1)
        if gesture is None:
            anchor_row, anchor_col = selection.anchor_screen_pos(rect, sel_metrics(pane))
            is_dragging = selection.phase == "dragging" or (anchor_row, anchor_col) != (y, x)
        drag_selection_to(x, y, rect, sel_metrics(pane))
        if gesture is not None:
            is_dragging = gesture["dragged"]      # mouse.rs: a word gesture drags once it moved
        elif is_dragging and selection.just_click:
            selection.force_dragging()
        if is_dragging:
            last_pane_click["pane"] = None        # a drag is not the first click of a double-click

        def arm(direction):
            sel["autoscroll"] = {"dir": direction, "x": x, "y": y, "rect": rect}
            sel["autoscroll_at"] = time.monotonic() + SELECTION_AUTOSCROLL_INTERVAL

        if y < top:
            if is_dragging:
                scroll_pane(pane, -_selection_edge_scroll_lines(top - y))
                drag_selection_to(x, y, rect, sel_metrics(pane))   # re-advance onto the revealed rows
                arm(-1)
        elif y > bottom:
            if is_dragging:
                scroll_pane(pane, _selection_edge_scroll_lines(y - bottom))
                drag_selection_to(x, y, rect, sel_metrics(pane))
                arm(1)
        elif y == top and is_dragging:                          # hot zone: hold to keep scrolling up
            arm(-1)
        elif y == bottom and is_dragging:                       # hot zone: hold to keep scrolling down
            arm(1)
        else:                                                   # safe zone (or a plain click): no autoscroll
            clear_autoscroll()
        paint_selection()

    def selection_autoscroll_tick(now):
        """runtime.rs tick_selection_autoscroll: while the pointer is held at a pane edge, keep
        scrolling one line per interval and extend the selection to the last pointer position,
        stopping at the scrollback boundary, when the pane resizes, or when the drag ends."""
        auto = sel["autoscroll"]
        if auto is None:
            return
        selection = sel["it"]
        if word["it"] is None and (selection is None or selection.phase != "dragging"):
            clear_autoscroll()
            return
        pane = word["it"]["pane"] if word["it"] is not None else selection.pane
        sl = slice_of(pane)
        if sl is None or sl[1] != auto["rect"]:                 # the pane moved/resized: stop
            clear_autoscroll()
            return
        metrics = sel_metrics(pane)
        if auto["dir"] < 0:
            if metrics.get("offset_from_bottom", 0) >= metrics.get("max_offset_from_bottom", 0):
                clear_autoscroll()                              # already at the oldest line
                return
            scroll_pane(pane, -1)
        else:
            if metrics.get("offset_from_bottom", 0) == 0:
                clear_autoscroll()                              # already at the newest line
                return
            scroll_pane(pane, 1)
        drag_selection_to(auto["x"], auto["y"], auto["rect"], sel_metrics(pane))
        paint_selection()
        sel["autoscroll_at"] = now + SELECTION_AUTOSCROLL_INTERVAL

    # ── Keys ──
    pane_out = {"pane": None, "data": bytearray()}   # keys for one pane in one read, sent as one request

    def send_to_pane(pane_id, data):
        if pane_out["pane"] not in (None, pane_id):
            flush_pane_out()
        pane_out["pane"] = pane_id
        pane_out["data"] += data

    def flush_pane_out():
        nonlocal repaint_after_typing
        if pane_out["pane"] is None or not pane_out["data"]:
            pane_out.update(pane=None, data=bytearray())
            return
        target = pane_out["pane"]
        _send_pane_input(control, target, bytes(pane_out["data"]), lambda text: report_failure(text, target))
        pane_out.update(pane=None, data=bytearray())
        repaint_after_typing = time.monotonic() + 0.9   # Repaint after typing stops to erase IME leftovers.

    def terminal_key(key):
        """terminal.rs prepare_terminal_key_forward: a key clears a retained selection, the
        prefix key opens the prefix layer, a plain PageUp/PageDown scrolls a shell
        transcript, everything else is encoded for the pane's protocol."""
        nonlocal mode
        if key.matches(PREFIX.code, PREFIX.mods) and key.kind != "release":
            mode = Mode.PREFIX
            draw_prefix_bar()
            return
        if key.code == "modifier" and not input_state_of(focused)["kitty_flags"]:
            return
        state = input_state_of(focused)
        if key.code in ("pageup", "pagedown") and not key.mods & ~hin.LOCK_MASK \
                and pin.plain_page_keys_use_host_scrollback(state):
            if key.kind == "release":
                return
            sl = slice_of(focused)
            lines = max(1, sl[1].height) if sl else 10
            scroll_pane(focused, -lines if key.code == "pageup" else lines)
            return
        if (apple_terminal_host and key.code == "enter" and key.kind == "press"
                and not key.mods & ~hin.LOCK_MASK and isNativeModifierPressed("shift")):
            # MISAKA: Terminal.app sends a bare CR for Shift+Enter. MISAKA's TUI tells them
            # apart by the live modifier state when TERM_PROGRAM is Apple_Terminal; panes
            # carry TERM_PROGRAM=misaka (pane.rs apply_pane_terminal_env), so the panel,
            # which owns the host terminal, makes the call and sends the pane a real Shift+Enter.
            key = hin.Key("enter", hin.SHIFT | (key.mods & hin.LOCK_MASK))
        data = pin.encode_key(key, state)
        if data:
            send_to_pane(focused, data)

    def prefix_key(key):
        """navigate.rs handle_prefix_key: the prefix again sends it through; esc and any
        unbound key leave; bound keys run their command (bindings in §10 of the parity audit)."""
        nonlocal mode, exit_reason
        if key.kind == "release" or key.code == "modifier":
            return
        if key.matches(PREFIX.code, PREFIX.mods):
            send_to_pane(focused, pin.encode_key(key, input_state_of(focused)))
            leave_command_mode()
            return
        text = key_text(key)
        if text is None and key.text and len(key.text) == 1 and not hin.is_control(key.text):
            # keybindings.rs resolve_prefix_binding: no exact chord matched, so the character
            # the host says the key produced (macOS Option, a custom layout) is tried instead.
            text = key.text
        if key.matches("esc") or text is None:
            leave_command_mode()
            return
        mode = Mode.TERMINAL
        restore_bottom()
        # client/shell/input.rs: copy mode survives every prefix action; it is back once the
        # command is done if its pane still has the focus (and when the focus returns).
        if text in "123456789":                  # herdr: digits switch tabs.
            switch_tab(int(text) - 1)
        elif text in ("n", "p") and visible_tabs():   # Cycle the active space's tabs.
            switch_tab((active_tab() + (1 if text == "n" else -1)) % len(visible_tabs()))
        elif text in ("h", "j", "k", "l"):       # herdr focus_pane_h/j/k/l.
            # api/panes.rs directional_pane_target: the tab's whole layout in the terminal
            # area, zoom or not, so a zoomed pane can hand focus (and the zoom) to a neighbour.
            _index, tree = current_tree()
            if tree is not None:
                area = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs_of()))["terminal_area"]
                panes_ = [(pid, rect) for pid, rect, _f in hui.collect_panes(tree, area, focused)]
                target = hui.find_in_direction(
                    focused, {"h": "left", "j": "down", "k": "up", "l": "right"}[text], panes_)
                if target:
                    focus(target)
        elif text == "z":                        # herdr zoom: the focused pane fills the tab.
            toggle_zoom()
        elif text == "c":                        # herdr new_tab.
            new_pane([_shell()], "shell", place={"tab": focused})
        elif text == "v":                        # split_vertical: side by side.
            new_pane([_shell()], "shell", place={"split": focused, "direction": "h"})
        elif text == "-":                        # split_horizontal: stacked.
            new_pane([_shell()], "shell", place={"split": focused, "direction": "v"})
        elif text == "g":                        # herdr goto: the navigator.
            open_nav()
        elif text == "[":                        # herdr copy_mode.
            enter_copy()
        elif text == "r":                        # herdr resize_mode.
            enter_resize()
        elif text == "T":                        # herdr rename_tab (prefix+shift+t).
            open_rename("tab")
        elif text == "W":                        # herdr rename_workspace (prefix+shift+w).
            open_rename("space")
        elif text == "d":
            return "quit"
        elif text == "x":
            # herdr prefix+x = ClosePane: one key, no confirmation (actions.rs:2035 only
            # confirms when closing a worktree group, which MISAKA does not have).
            # Closing the last pane exits the panel.
            if close_focused():
                exit_reason[0] = "closed_all"
                return "quit"
        elif text == "?":
            open_help()
        if mode is Mode.TERMINAL and copy["pane"] is not None:
            leave_command_mode()                 # back to copy mode when its pane has the focus again
        return None

    def modal_paste_target_active():
        """input.rs modal_paste_target_active: a text field has the keyboard."""
        return (mode is Mode.RENAME or (mode is Mode.NAVIGATOR and nav["search"])
                or (mode is Mode.COPY and copy["prompt"] is not None))

    def handle_key(key):
        if key.kind != "release" and is_modal_paste_shortcut(key) and modal_paste_target_active():
            text = _read_clipboard_text()       # handle_modal_paste_shortcut_with: the key is taken either way
            if text:
                handle_paste(text)
            return None
        if (mode in (Mode.TERMINAL, Mode.PREFIX, Mode.RESIZE) and key.kind != "release"
                and key.code != "modifier" and not (copy["pane"] is not None and copy["pane"] == focused)):
            clear_selection()
        if mode is Mode.TERMINAL:
            terminal_key(key)
        elif mode is Mode.PREFIX:
            return prefix_key(key)
        elif mode is Mode.COPY:
            copy_key(key)
        elif mode is Mode.RESIZE:
            resize_key(key)
        elif mode is Mode.RENAME:
            rename_key(key)
        elif mode is Mode.GLOBAL_MENU:
            menu_key(key)
        elif mode is Mode.NAVIGATOR:
            nav_key(key)
        elif mode is Mode.KEYBIND_HELP:
            help_key(key)
        return None

    def handle_paste(text):
        """input/mod.rs handle_paste: text inputs take it; a pane gets it as a paste
        (bracketed when it asked), never as keystrokes."""
        if mode is Mode.RENAME:
            rename["input"].insert(text)
            draw_rename()
        elif mode is Mode.NAVIGATOR:
            nav_paste(text)
        elif mode is Mode.COPY:
            copy_paste(text)
        elif mode is Mode.TERMINAL:
            clear_selection()
            send_to_pane(focused, pin.paste_bytes(text, input_state_of(focused)))

    def handle_focus(gained):
        """The host window's focus, forwarded to the focused pane when it asked (pane.rs
        try_send_focus_event). Regaining focus also re-asserts the host mouse modes: a
        terminal that was re-attached or reconnected may have dropped them (client/mod.rs
        refresh_host_mouse_capture)."""
        if gained:
            _write_all(HOST_MOUSE_MODES)
        else:
            # input.rs release_input_leases: a press the pane still holds is released where
            # the pointer last was, so a program never waits on a button the host let go of.
            gesture, pane_gesture["it"] = pane_gesture["it"], None
            if gesture is not None:
                last = gesture["last"]
                sl = slice_of(gesture["pane"])
                forward_mouse(gesture["pane"], sl[1] if sl is not None else gesture["rect"],
                              hin.Mouse("release", gesture["button"], last.x, last.y, last.mods))
        data = pin.focus_bytes(gained, input_state_of(focused))
        if data:
            send_to_pane(focused, data)

    # crossterm enable_raw_mode (cfmakeraw), as herdr runs its host terminal. IEXTEN must go
    # too: left on, the line discipline eats ctrl+v (VLNEXT) and takes ctrl+o as VDISCARD,
    # so neither reached the panel or a pane.
    if WINDOWS:
        old_attrs = win_console.enter_raw_mode(0, 1, mouse=True)
        keys, feed = socket.socketpair()
        console_input = win_console.ConsoleInput(
            0, lambda text: feed.sendall(text.encode()), lambda: resized.update(hit=True)).start()
    else:
        old_attrs = termios.tcgetattr(0)
        new_attrs = termios.tcgetattr(0)
        tty.cfmakeraw(new_attrs)
        termios.tcsetattr(0, termios.TCSANOW, new_attrs)
        keys = 0
    # Alternate screen (standard for herdr and every proper TUI): without it Terminal.app
    # adds a "mark" to every line that gets a carriage return, which renders as a pair of
    # dim brackets. Mouse: every motion (?1003, as crossterm's EnableMouseCapture) so a pane
    # program that asked for any-motion reports gets them and popups can highlight on hover;
    # SGR encoding. Bracketed paste and focus events come in as their own events; the kitty
    # keyboard flags are herdr's IME-compatible set (a host that lacks the protocol ignores
    # the push and sends legacy sequences, which the parser also reads). Order as herdr's
    # setup_terminal_with_capabilities: push the flags, probe whether the host took them
    # (a host that did sends Escape and Alt chords as CSI u, so the framer may hold a partial
    # escape sequence instead of releasing it as Escape), then turn on mouse, paste, focus.
    _write_all(b"\x1b[?1049h\x1b[?7l" + (b"" if WINDOWS else f"\x1b[>{pin.HOST_KITTY_FLAGS}u".encode()))
    probe, initial_input = _probe_keyboard_enhancement()
    _write_all(HOST_MOUSE_MODES + b"\x1b[?2004h\x1b[?1004h")
    host = hin.HostInput(escape_disambiguation=hin.escape_disambiguation_confirmed(probe))
    exit_reason = ["detached"]     # detached: user left; closed_all: the last pane was closed.
    try:
        _write_all(b"\x1b[0m\x1b[2J")
        stream.send("pane.attach", {"id": "*"})
        refresh_cards()
        reload_layout()        # the daemon seated every pane (Last Order included) in a space
        for space in spaces:               # warm the branch rows so the first frame has them
            git_seen.add(os.path.realpath(space["folder"]))
        refresh_git(time.monotonic())
        refresh_sessions()
        side["ws"] = space_of(focused) or (spaces[0]["id"] if spaces else None)
        focus(focused, force_layout=True)
        last_poll = 0.0
        repaint_after_typing = 0.0   # After typing pauses, repaint the focused pane once to erase IME leftovers.
        while True:
            if resized.pop("hit", None):
                # ui.rs compute_view on a new size: geometry is recomputed, the mode stays;
                # the host mouse modes are re-asserted (client/mod.rs, resize path). A host
                # that can no longer report a grid (its terminal went away) detaches the panel
                # rather than resizing every pane to a guess (client TerminalUnavailable).
                size = _term_size()
                if size is None:
                    return
                rows, cols = size
                _write_all(b"\x1b[0m\x1b[2J" + HOST_MOUSE_MODES)
                invalidate()
                relayout()
                layout_notice()
                redraw_overlay()
            now = time.monotonic()
            wait = 0.05
            pending = host.deadline()
            if pending is not None:
                wait = min(wait, max(0.0, pending - now))
            split_due = drag_due()
            if split_due is not None:
                wait = min(wait, max(0.0, split_due - now))
            if sel["autoscroll_at"] is not None:
                wait = min(wait, max(0.0, sel["autoscroll_at"] - now))
            readable, _, _ = select.select([keys, stream.sock], [], [], wait)
            now = time.monotonic()
            drag_tick(now)
            if sel["autoscroll_at"] is not None and now >= sel["autoscroll_at"]:
                selection_autoscroll_tick(now)
            events = []
            if initial_input:          # typed while the keyboard probe was waiting (client/input.rs)
                events = host.feed(bytes(initial_input), now)
                initial_input = b""
            elif pending is not None and now >= pending and keys not in readable:
                events = host.flush(now)
            if repaint_after_typing and now > repaint_after_typing:
                # IME pre-edit text is drawn directly by the host terminal over our cells: the
                # application never sees it, so no frame restores that area once it is
                # withdrawn. Once typing pauses, write the whole frame again.
                repaint_after_typing = 0.0
                sl = slice_of(focused)
                if sl is not None:
                    invalidate_rect(sl[1])
            tick_notices(now)
            if sel["clear_at"] is not None and now >= sel["clear_at"]:
                clear_selection()

            if keys in readable:
                events = events + host.feed(keys.recv(4096) if WINDOWS else os.read(0, 4096), now)
            quitting = False
            for event in events:
                if isinstance(event, hin.Mouse):
                    flush_pane_out()
                    handle_mouse(event, now)
                elif isinstance(event, hin.Paste):
                    handle_paste(event.text)
                elif isinstance(event, hin.Focus):
                    handle_focus(event.gained)
                else:
                    if handle_key(event) == "quit":
                        quitting = True
                        break
            flush_pane_out()
            if quitting:
                return

            if stream.sock in readable or stream.buf:
                while (line := stream.readline()) is not None:
                    msg = json.loads(line)
                    if msg.get("event") == "screen":
                        if "scroll" in msg:      # Metrics update with every frame (clear drops history).
                            scroll_state[msg["id"]] = {
                                "metrics": msg["scroll"],
                                "alt": msg.get("alt_screen", False)}
                        if msg.get("input"):
                            pane_input[msg["id"]] = msg["input"]
                        if copy["pane"] == msg["id"] and pane_rev.get(msg["id"]) != msg["revision"]:
                            copy["match"] = None      # state.rs: content changed, matches are stale
                        pane_rev[msg["id"]] = msg["revision"]
                        gesture = word["it"]
                        if (gesture is not None and gesture["pane"] == msg["id"]
                                and gesture["revision"] != msg["revision"]):
                            clear_selection()   # output invalidates the gesture's cached word bounds
                        sl_ = slice_of(msg["id"])
                        if sl_ is not None:
                            note_pane_geometry(msg["id"], sl_[1].width, sl_[1].height, bool(msg["alt_screen"]))
                        paint_rows(msg["id"], msg["rows"], msg.get("cursor"),
                                   hidden=msg.get("cursor_hidden"), shape=msg["cursor_shape"])
                    elif msg.get("event") == "clipboard":
                        # A pane program's OSC 52 copy (herdr client/clipboard_forwarding.rs).
                        try:
                            text = base64.b64decode(msg["data"]).decode("utf-8", "replace")
                        except ValueError:
                            text = ""
                        if text:
                            _clipboard(text)
                    elif msg.get("event") == "exited":
                        # Card exits must reach the daemon's watcher before their pane goes.
                        page = active_tab()
                        note_exit(msg["id"], msg.get("exit_code"))
                        try:
                            if not _close_exited_pane(control, msg["id"]):
                                continue
                        except RuntimeError:
                            pass
                        pane_gone(msg["id"])
                        listing = panes()
                        alive = [p for p in listing if p["alive"]]
                        if not alive:
                            exit_reason[0] = "closed_all"
                            return
                        if msg["id"] == focused:
                            refocus(page_idx=page)
                        else:
                            relayout()

            if drag[0] is None and now - last_poll > POLL_SECONDS:
                # Not while dragging: this poll makes blocking daemon requests (cards, panes,
                # sessions) and runs git, any of which stalls a drag if it lands mid-gesture
                # while the daemon is busy repainting a big pane. It resumes the moment the
                # drag ends (last_poll is stale, so the next turn runs it).
                last_poll = now
                refresh_cards()
                refresh_git(now)
                refresh_sessions()
                prune_meta_cache()
                try:
                    listing = panes()
                except (RuntimeError, ConnectionError):
                    return
                open_ids = {pane["id"] for pane in listing}
                for gone in [pane_id for pane_id in pane_cursor if pane_id not in open_ids]:
                    pane_cursor.pop(gone, None)      # per-pane state dies with its pane
                    pane_gone(gone)
                for gone in [pane_id for pane_id in scroll_state if pane_id not in open_ids]:
                    scroll_state.pop(gone, None)
                for gone in [pane_id for pane_id in pane_input if pane_id not in open_ids]:
                    pane_input.pop(gone, None)
                for pane in listing:      # /new and in-place forks swap the file under a named tab
                    source = auto_names.get(pane["id"])
                    reported = (pane.get("reported") or {}).get("session")
                    if source and reported and os.path.realpath(reported) != os.path.realpath(source):
                        auto_names[pane["id"]] = reported
                        rename_tab_of(pane["id"], session_meta(reported)["title"])
                dead = [p for p in listing if not p["alive"] and not p["card"]]
                if dead:      # Exit events can precede the subscription and get missed; the poll cleans up.
                    page = active_tab()
                    for pane in dead:
                        note_exit(pane["id"], pane.get("exit_code"))
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
                        refocus(page_idx=page)
                    else:
                        relayout()
                if reload_layout():    # a pane opened elsewhere (a summoned Sister, a fork): lay the panes out again
                    relayout()
                else:
                    request_render()
            render()
    except Exception as error:   # noqa: BLE001
        import traceback
        log = str(home.path("panel_crash_log"))
        try:
            crash_fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(crash_fd, "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{traceback.format_exc()}")
        except OSError:
            log = "(could not write the crash log)"
        what = ("Panel disconnected" if isinstance(error, (RuntimeError, ConnectionError, json.JSONDecodeError))
                else f"Panel crashed with {type(error).__name__}")
        sys.exit(f"{what}: {error}\nTraceback saved to {log}. Run `misaka` to start again.")
    finally:
        # Nothing waits on an in-flight git probe: it has its own two-second timeout and
        # the panel is on its way out.
        git_pool.shutdown(wait=False, cancel_futures=True)
        if WINDOWS:
            console_input.stop()
            win_console.restore(old_attrs)
            keys.close()
            feed.close()
        else:
            termios.tcsetattr(0, termios.TCSANOW, old_attrs)
        # Pop the keyboard flags, turn off mouse tracking, paste and focus reporting, leave
        # the alternate screen, show the cursor again in the terminal's default shape
        # (terminal_setup.rs: "\x1b[?25h\x1b[0 q").
        _write_all(b"\x1b[<u\x1b[?1000;1002;1003;1006l\x1b[?2004l\x1b[?1004l\x1b[?7h\x1b[0m\x1b[2J\x1b[?1049l\x1b[?25h\x1b[0 q")
        if exit_reason[0] == "closed_all":
            print(f"All panes closed (their last lines are in {home.display(home.path('panel_crash_log'))}). "
                  "Run `misaka` to open the panel again.")
        else:
            print("Panel closed. Last Order and the Sisters shut down with it; nothing keeps running in the background.")
