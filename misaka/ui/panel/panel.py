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
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import time

from misaka.ui.panel import client as net
from misaka.ui.panel import geometry as hui
from misaka.ui.panel import host_input as hin
from misaka.ui.panel import pane_input as pin
from misaka.ui.panel import screen as sc
from misaka.ui.panel.selection import Selection
from misaka.ui.panel.selection import absolute_row as _abs_row


def _prefix_key():
    """Prefix key, default ctrl+b. Inside tmux that key is taken, so set
    MISAKA_PANEL_PREFIX=ctrl+g (or similar) to change it."""
    name = os.environ.get("MISAKA_PANEL_PREFIX", "ctrl+b").lower().strip()
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return hin.Key(name[5], hin.CTRL)
    return hin.Key("b", hin.CTRL)


PREFIX = _prefix_key()
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


def format_copy_feedback(message, area):
    """herdr status.rs render_copy_feedback: a green-bordered box, "● message" bold on
    panel_bg, three rows tall, at the bottom centre of ``area`` (ToastClipboardPosition::
    BottomCenter). Returns ``(rect, rows)`` with rows = [(y, x, ansi)]. Pure, so testable."""
    if area.width == 0 or area.height == 0:
        return None, []
    width = min(_wcwidth(message) + 4, area.width)
    height = min(3, area.height)
    rect = hui.Rect(area.x + (area.width - width) // 2, area.y + area.height - height, width, height)
    return rect, _boxed_rows(rect, [[("●", {"fg": hui.PALETTE["green"]}), (" " + message, {"fg": hui.TEXT, "bold": True})]],
                             hui.PALETTE["green"])


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
        name = hui.tab_chrome_label(tab_names, index, zoomed)   # tabs.rs:36-45: " Z" while zoomed
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
    home = os.path.expanduser("~")
    return "~" if folder == home else (os.path.basename(folder.rstrip(os.sep)) or folder)


def effective_space_folder(spaces, listing, focused_id, active_id):
    """The folder the active space *shows*: its root pane's foreground job's cwd, else that
    pane's cwd, else the folder it was created in -- the identity cwd of ``sidebar_model``.

    The sessions list used the creation folder while the label followed the foreground job,
    so a shell that ``cd ~``'d was labelled ``~`` over a list of the old folder's sessions:
    zero, for a home folder that had several. One folder feeds both. With no active space,
    the focused pane's folder -- never the folder the panel was launched in.
    """
    rows, _agents = sidebar_model(spaces, listing, focused_id, active_id)
    row = next((row for row in rows if row["key"] == active_id), None)
    if row is not None:
        return row["folder"]
    current = next((p for p in listing if p["id"] == focused_id), None)
    return (current or {}).get("cwd") or os.getcwd()


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
               "state": "unknown", "seen": True, "alive": False}
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
            done = subprocess.run(["git", "-C", folder, *args],
                                  capture_output=True, text=True, timeout=2, check=False)
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
    """sidebar.rs:818-936 resolved_token_spans, the drawing half: separators in overlay0 dim,
    each token in its own style, widths from geometry.fit_tokens.
    ``tokens`` = [(kind, text, style kwargs)]."""
    for index, sep, shown in hui.fit_tokens([(k, t) for k, t, _s in tokens], max_width):
        if sep:
            canvas.put(x, y, sep, fg=hui.OVERLAY0, dim=True, clip=clip)
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


def _render_spaces(canvas, spaces, area, scroll, hits):
    """sidebar.rs:1040-1183 render_workspace_list with the default rows: state icon + name,
    then branch + git status when the space's folder is a repo (``space["git"]``).
    Returns the section's scroll state for the wheel."""
    P = hui.PALETTE
    list_bottom = area.y + max(0, area.height - 1)
    if area.height > 0:
        canvas.put(area.x, area.y, " spaces", fg=hui.OVERLAY0, bold=True,
                   clip=area.x + area.width)
    heights = [2 if space.get("git") else 1 for space in spaces]
    body = hui.workspace_list_body_rect(area, False)
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
    return {"rect": area, "scroll": scroll, "max_scroll": metrics["max_offset_from_bottom"]}


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
    """herdr menus.rs:214-258 render_global_launcher_menu on widgets.rs:11-30 render_panel_shell:
    a plain accent border on panel_bg, one " label " per row in text color, the highlighted
    label on accent in the contrast color, bold. Rows from ``scroll`` fill the inner height.
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
        if index == highlighted:
            canvas.put(1, y, f" {labels[index]} ", fg=hui.panel_contrast_fg(), bg=hui.ACCENT,
                       bold=True, clip=1 + inner_w)
        else:
            canvas.put(1, y, f" {labels[index]} ", fg=hui.TEXT, clip=1 + inner_w)
        hits.append((hui.Rect(rect.x + 1, rect.y + y, inner_w, 1), index))
    return [(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], hits


# ── Navigator: src/ui/navigator.rs + app/input/modal.rs handle_navigator_key (prefix+g) ──

NAV_FILTERS = {"b": "blocked", "w": "working", "i": "idle", "d": "done"}


def navigator_popup_rect(screen):
    """app/input/overlays.rs:262-274: margins of width/16 and height/10, at least 2 and 1."""
    margin_x, margin_y = max(screen.width // 16, 2), max(screen.height // 10, 1)
    return hui.Rect(screen.x + margin_x, screen.y + margin_y,
                    max(screen.width - 2 * margin_x, 4), max(screen.height - 2 * margin_y, 4))


def navigator_rows(spaces, agents, *, query="", state_filter=None, collapsed=()):
    """navigator.rs rows: one row per space (▾/▸ caret) with its agents as a tree underneath.
    A query (case-insensitive substring) or a state filter keeps only the matching agents and
    the spaces holding them; a space whose own label matches the query keeps every agent.
    A collapsed space hides its agents unless a query or filter is active. Pure, so testable."""
    rows, needle, filtering = [], query.strip().lower(), bool(query.strip()) or state_filter is not None
    for space in spaces:
        label_hit = bool(needle) and needle in space["label"].lower()
        members = []
        for agent in agents:
            if agent["space_key"] != space["key"]:
                continue
            name_hit = not needle or label_hit or needle in agent["agent"].lower()
            state_hit = state_filter is None or hui.state_label(agent["state"], agent["seen"]) == state_filter
            if name_hit and state_hit:
                members.append(agent)
        if filtering and not members and not (label_hit and state_filter is None):
            continue
        expanded = filtering or space["key"] not in collapsed
        rows.append({"kind": "space", "key": space["key"], "label": space["label"],
                     "state": space["state"], "seen": space["seen"], "pane": None,
                     "expanded": expanded, "last": True})
        if not expanded:
            continue
        for index, agent in enumerate(members):
            rows.append({"kind": "pane", "key": space["key"], "label": agent["agent"],
                         "state": agent["state"], "seen": agent["seen"], "pane": agent["pane"],
                         "expanded": None, "last": index == len(members) - 1})
    return rows


def format_navigator(rows, selected, scroll, rect, *, query="", search_focused=False,
                     state_filter=None, detail=""):
    """navigator.rs render_navigator_overlay: the panel shell, the search line, a separator,
    the tree rows (state dot, label, state word right-aligned; the selected row on surface0),
    a scrollbar when they overflow, a separator, one detail line, and the footer key hints.
    Returns ``(rows, hits)`` like format_menu_popup. Pure, so testable."""
    P = hui.PALETTE
    if rect.width < 6 or rect.height < 9:
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
    x0, inner_w, clip = 1, width - 2, width - 1
    # Search line (navigator.rs:49-107): " / " then the query, a state chip, or the placeholder.
    slash = {"fg": hui.ACCENT, "bold": True} if search_focused else {"fg": hui.OVERLAY0}
    canvas.put(x0, 1, " / ", clip=clip, **slash)
    if state_filter:
        glyph, color = hui.state_dot(state_filter if state_filter != "done" else "idle",
                                     state_filter != "done")
        canvas.put(x0 + 3, 1, f"{glyph} {state_filter}", fg=color, bold=True, clip=clip)
    elif query.strip():
        canvas.put(x0 + 3, 1, query, fg=hui.TEXT, clip=clip)
    else:
        canvas.put(x0 + 3, 1, "search panes", fg=hui.OVERLAY0, clip=clip)
    canvas.put(x0, 2, "─" * inner_w, fg=P["surface_dim"], clip=clip)
    body_y, body_h = 3, height - 7
    hits = []
    metrics = hui.list_scroll_metrics([1] * len(rows), body_h, scroll)
    has_bar = hui.should_show_scrollbar(metrics)
    row_w = inner_w - (1 if has_bar else 0)
    for offset, index in enumerate(range(scroll, min(len(rows), scroll + body_h))):
        row, y = rows[index], body_y + offset
        is_selected = index == selected
        if is_selected:
            canvas.fill_bg(x0, x0 + row_w, y, hui.SURFACE0)
        if row["kind"] == "space":                       # navigator.rs:277-300 tree_prefix
            prefix = "▾ " if row["expanded"] else "▸ "
        else:
            prefix = "  └─ " if row["last"] else "  ├─ "
        glyph, color = hui.state_dot(row["state"], row["seen"])
        word = hui.state_label(row["state"], row["seen"])
        label_w = max(0, row_w - _wcwidth(prefix) - 2 - _wcwidth(word) - 2)
        x = x0
        canvas.put(x, y, prefix, fg=hui.OVERLAY0, clip=x0 + row_w)
        x += _wcwidth(prefix)
        canvas.put(x, y, glyph, fg=color, clip=x0 + row_w)
        x += 2
        shown = hui.truncate_end(row["label"], label_w)
        canvas.put(x, y, shown, fg=hui.TEXT, bold=is_selected, clip=x0 + row_w)
        canvas.put(x0 + row_w - _wcwidth(word) - 1, y, word, fg=color, clip=x0 + row_w)
        hits.append((hui.Rect(rect.x + x0, rect.y + y, row_w, 1), index))
    if has_bar:
        _put_scrollbar(canvas, metrics, hui.Rect(x0 + inner_w - 1, body_y, 1, body_h))
    canvas.put(x0, height - 4, "─" * inner_w, fg=P["surface_dim"], clip=clip)
    canvas.put(x0, height - 3, hui.truncate_end(detail, inner_w - 1), fg=hui.OVERLAY1, clip=clip)
    hints = ((("enter", "switch"), ("↑↓", "move"), ("ctrl+u", "clear"), ("esc", "back"))
             if search_focused else
             (("enter", "switch"), ("/", "search"), ("b/w/i/d/a", "states"),
              ("j/k/↑↓", "move"), ("esc", "close")))
    x = x0
    for key, word in hints:                              # navigator.rs:535-572 render_footer
        canvas.put(x, height - 2, f" {key}", fg=hui.ACCENT, bold=True, clip=clip)
        x += _wcwidth(key) + 1
        canvas.put(x, height - 2, f" {word}  ", fg=hui.OVERLAY0, clip=clip)
        x += _wcwidth(word) + 3
    return [(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], hits


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
    home = os.path.expanduser("~")
    return [(folder, folder.replace(home, "~", 1) if folder.startswith(home) else folder, members)
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
    """MISAKA section: past sessions, grouped Last Order / Sisters for this folder ("here") or
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
                   sort="grouped", sessions=(), session_mode="here"):
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
                sections[key] = _render_spaces(canvas, spaces, sub, scrolls.get(key, 0), hits)
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


def format_prefix_bar(width, prefix_name="ctrl+b"):
    """herdr menus.rs:31-62 render_prefix_overlay: the PREFIX bar's four hints."""
    return format_mode_bar("PREFIX", (("esc", "cancel"), (prefix_name, "send prefix"),
                                      ("g", "navigator"), ("?", "keybinds")), width)


def format_rename_popup(title, value, rect):
    """herdr dialogs.rs:43-110 render_rename_overlay on widgets.rs render_modal_shell: a 56x7
    modal (accent border, panel_bg), the title bold on the first inner row, the input on the
    third row as " value█" on surface0, and a centred button row: [↵ save] on accent,
    [^c clear] and [esc cancel] on surface0. Returns ``(rows, hits)`` with hits = [(Rect, action)]."""
    if rect.width < 8 or rect.height < 6:
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
    inner_x, inner_w = 1, width - 2
    canvas.put(inner_x, 1, title, fg=hui.TEXT, bold=True, clip=inner_x + inner_w)
    shown = hui.truncate_end(f" {value}", inner_w - 1) + "█"
    canvas.fill_bg(inner_x, inner_x + inner_w, 3, hui.SURFACE0)
    canvas.put(inner_x, 3, shown, fg=hui.TEXT, clip=inner_x + inner_w)
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
    return [(rect.y + y, rect.x, canvas.row(y)) for y in range(height)], hits


_HELP_ROWS = [
    ("1-9", "Switch to tab N"), ("n / p", "Next / previous tab"),
    ("h j k l", "Focus left / down / up / right"), ("c", "New tab"),
    ("v", "Split side by side"), ("-", "Split top and bottom"),
    ("z", "Zoom: focused pane fills the tab"), ("g", "Go to: search every space and pane"),
    ("[", "Copy mode: move, search, select, copy"), ("r", "Resize: h/l width, j/k height"),
    ("T / W", "Rename this tab / this space"),
    ("x", "Close the focused pane"),
    ("d", "Quit (everything shuts down)"), ("ctrl+b", "Send a literal ctrl+b"),
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


def _clipboard(text):
    """Copy to the system clipboard: pbcopy on macOS (what herdr does), otherwise OSC 52 and let the terminal handle it."""
    import subprocess
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    _write_all(b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")


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
    """Minimal JSONL client over the daemon's Unix socket (one for requests, one for the event stream).

    Every request carries an id of its own and ``request`` answers only to that id: a reply that
    arrives after its request timed out (the daemon was busy for longer than the socket timeout)
    is skipped when the next request reads the stream, instead of being handed to that next
    request as its result -- which is how ``panes.list`` once received ``cards.list``'s reply and
    the panel died on ``KeyError: 'panes'``."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
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
            crash_fd = os.open(os.path.expanduser("~/.misaka/panel-crash.log"),
                               os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
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
    pane_cursor = {}          # pane id -> {"at": [col, row], "hidden": bool}
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
        spaces[:] = got
        if side["ws"] is not None and side["ws"] not in {space["id"] for space in spaces}:
            # The active space left the layout (its last pane died): `active_space()` was None
            # from here on, no row was highlighted, and the sessions list fell back to the
            # folder the panel was launched in -- for a `~` space that read as zero sessions.
            side["ws"] = space_of(focused) or (spaces[0]["id"] if spaces else None)
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


    rows, cols = _term_size()
    # Sidebar state (herdr AppState: sidebar_width / sidebar_collapsed / agent_panel_sort / active).
    side = {"w": SIDEBAR_W, "collapsed": False, "sort": "grouped", "ws": None, "sess_mode": "here"}
    last_focus = {}        # space -> the pane focused last time we were there (herdr: per-workspace focus)
    side_order = []        # space keys in sidebar order at the last draw (neighbour lookup when one closes)

    def switch_space(key):
        """herdr switch_workspace: the main area shows that space's tabs, focus returns to its last pane."""
        side["ws"] = key
        refresh_sessions()     # the sessions list belongs to the space's folder
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
        raw = effective_space_folder(spaces, listing, focused, side["ws"])
        folder = os.path.realpath(raw)
        everything = side["sess_mode"] == "all"
        lo, others = [], []
        for entry in cards_cache["sessions"]:
            if not session_catalog.available(entry) or (not everything and entry["workspace"] != folder):
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
            elif entry.get("task_status"):
                label = f"{entry['task_status']} · {label}"
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
        current = active_space()
        space = (current if current and current["folder"] == folder
                 else next((s for s in spaces if s["folder"] == folder and s["tabs"]), None))
        # A reopened session stands on its own: closing a pane released it, and reopening never
        # rejoins the Last Order it branched from -- a new fork calling a Sister makes a fresh
        # session anyway. So it opens as a plain tab; only a fresh (non-fork) Last Order takes a
        # space of its own, being a workspace root.
        place = ({"tab": hui.pane_ids(space["tabs"][0])[0]} if space and space["tabs"]
                 else {"space": True, "name": row["label"]})
        # Only ordinary interactive conversations are resumed as a new interactive runtime.
        # DM/node/child/card sessions belong to their own lifecycle, even when idle.
        from misaka.config import sisters
        if kind == "foreground" and row["state"] == "saved" and (role == "last-order" or role in sisters()):
            argv = [sys.executable, "-m", "misaka", "chat"]
            if role != "last-order":
                argv += ["--as", role]
            elif not is_fork:
                place = {"space": True, "name": row["label"]}
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
        nonlocal mode
        mode = Mode.TERMINAL
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

    # The navigator (prefix+g, herdr Mode::Navigator): a modal tree of every space and agent.
    nav = {"query": "", "search": False, "filter": None, "selected": 0,
           "scroll": 0, "rows": [], "hits": [], "rect": None, "collapsed": set(), "lines": []}

    def nav_rows():
        rows_, agents = sidebar_model(spaces, listing, focused, side["ws"])
        return navigator_rows(rows_, agents, query=nav["query"], state_filter=nav["filter"],
                              collapsed=nav["collapsed"])

    def nav_detail(row):
        if row is None:
            return ""
        folder = (space_info(row["key"]) or {}).get("folder", "")
        folder = folder.replace(os.path.expanduser("~"), "~", 1)
        if row["kind"] == "space":
            count = sum(1 for r in nav["rows"] if r["kind"] == "pane" and r["key"] == row["key"])
            return f"{row['label']} · {folder} · {count} pane{'s' if count != 1 else ''}"
        pane = next((p for p in listing if p["id"] == row["pane"]), {})
        message = (pane.get("reported") or {}).get("message") or ""
        pane_folder = ((pane.get("foreground") or {}).get("cwd") or pane.get("cwd") or folder)
        pane_folder = pane_folder.replace(os.path.expanduser("~"), "~", 1)
        return " · ".join(part for part in (row["label"], pane_folder,
                                            hui.state_label(row["state"], row["seen"]), message) if part)

    def draw_nav():
        nav["rows"] = nav_rows()
        nav["selected"] = min(nav["selected"], max(0, len(nav["rows"]) - 1))
        rect = navigator_popup_rect(hui.Rect(0, 0, cols, rows))
        nav["scroll"] = menu_scroll_for(nav["selected"], nav["scroll"], rect.height - 7)
        current = nav["rows"][nav["selected"]] if nav["rows"] else None
        nav["lines"], nav["hits"] = format_navigator(
            nav["rows"], nav["selected"], nav["scroll"], rect, query=nav["query"],
            search_focused=nav["search"], state_filter=nav["filter"], detail=nav_detail(current))
        nav["rect"] = rect
        request_render()

    def open_nav():
        nonlocal mode
        nav.update(query="", search=False, filter=None, scroll=0)
        nav["rows"] = nav_rows()
        nav["selected"] = next((i for i, r in enumerate(nav["rows"]) if r["pane"] == focused), 0)
        mode = Mode.NAVIGATOR
        draw_nav()

    def close_nav():
        nonlocal mode
        mode = Mode.TERMINAL
        request_render()

    def nav_accept():
        row = nav["rows"][nav["selected"]] if nav["rows"] else None
        close_nav()
        if row is None:
            return
        if row["kind"] == "pane":
            focus(row["pane"], force_layout=True)
        else:
            switch_space(row["key"])

    def nav_move(delta):
        nav["selected"] = max(0, min(nav["selected"] + delta, len(nav["rows"]) - 1))

    def nav_key(key):
        """modal.rs:162-270 handle_navigator_key, both halves: typing in the search line, and
        the list keys (/, a, b/w/i/d, j/k, space to fold a space, G/End, Home)."""
        if key.kind == "release":
            return
        text = key_text(key)
        if nav["search"]:
            if key.matches("esc"):
                nav["search"] = False
            elif key.matches("enter"):
                nav_accept()
                return
            elif key.matches("backspace"):
                nav["filter"] = None
                nav["query"] = nav["query"][:-1]
                nav["selected"] = 0
            elif key.matches("up") or key.matches("p", hin.CTRL):
                nav_move(-1)
            elif key.matches("down") or key.matches("n", hin.CTRL):
                nav_move(1)
            elif key.matches("u", hin.CTRL):
                nav.update(query="", filter=None)
            elif text is not None and text >= " ":
                nav["query"] += text
                nav["selected"] = 0
        else:
            if key.matches("esc"):
                close_nav()
                return
            elif key.matches("enter"):
                nav_accept()
                return
            elif text == "/":
                nav.update(search=True, filter=None)
            elif key.matches("backspace"):
                nav["filter"] = None
            elif text == "a":
                nav.update(query="", filter=None)
            elif text in NAV_FILTERS:
                nav.update(query="", filter=NAV_FILTERS[text], selected=0)
            elif text == "j" or key.matches("down"):
                nav_move(1)
            elif text == "k" or key.matches("up"):
                nav_move(-1)
            elif text == " " and nav["rows"]:
                space_key = nav["rows"][nav["selected"]]["key"]
                nav["collapsed"].symmetric_difference_update({space_key})
            elif text == "G" or key.matches("end"):
                nav["selected"] = max(0, len(nav["rows"]) - 1)
            elif key.matches("home"):
                nav["selected"] = 0
        draw_nav()

    def nav_paste(text):
        """input/mod.rs paste_into_active_text_input: only the search box takes a paste."""
        if nav["search"]:
            nav["query"] += "".join(ch for ch in text if ch >= " ")
            nav["selected"] = 0
            draw_nav()

    def nav_mouse(event):
        """overlays.rs:121-180: a row jumps, the wheel scrolls, anywhere else closes."""
        if event.kind == "press" and event.button == 0:
            hit = next((index for rect, index in nav["hits"]
                        if rect.x <= event.x < rect.x + rect.width and rect.y == event.y), None)
            if hit is None:
                close_nav()
            else:
                nav["selected"] = hit
                nav_accept()
        elif event.kind in ("wheel_up", "wheel_down"):
            body_h = max(1, nav["rect"].height - 7) if nav["rect"] else 1
            cap = max(0, len(nav["rows"]) - body_h)
            nav["scroll"] = max(0, min(nav["scroll"] + (1 if event.kind == "wheel_down" else -1), cap))
            nav["selected"] = max(nav["scroll"], min(nav["selected"], nav["scroll"] + body_h - 1))
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
    rename = {"kind": "tab", "target": None, "value": "", "hits": [], "rect": None,
              "replace_on_type": False, "lines": []}

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
        rename.update(kind=kind, target=target, value=value, replace_on_type=False)
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
        rename["lines"], rename["hits"] = format_rename_popup(title, rename["value"], rect)
        rename["rect"] = rect
        request_render()

    def close_rename(save):
        nonlocal mode
        if save:
            value = rename["value"].strip()
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
        mode = Mode.TERMINAL
        request_render()

    def rename_insert(text):
        if rename["replace_on_type"]:
            rename.update(value="", replace_on_type=False)
        rename["value"] += "".join(ch for ch in text if ch >= " ")

    def rename_delete_word():
        """modal.rs delete_rename_input_word: blanks, then one run of word or separator characters."""
        if rename["replace_on_type"]:
            rename.update(value="", replace_on_type=False)
            return
        value = rename["value"].rstrip()
        if not value:
            rename["value"] = value
            return
        word = value[-1].isalnum() or value[-1] == "_"
        while value and not value[-1].isspace() and (value[-1].isalnum() or value[-1] == "_") == word:
            value = value[:-1]
        rename["value"] = value

    def rename_key(key):
        """modal.rs RENAME_ACTIONS + handle_rename_edit_key: enter saves, ctrl+c clears,
        esc cancels; ctrl+u / super+backspace clear, ctrl+w / ctrl+h / alt+backspace
        delete a word, backspace one character, a printable character inserts."""
        if key.kind == "release":
            return
        if key.matches("enter"):
            close_rename(True)
            return
        if key.matches("esc"):
            close_rename(False)
            return
        if key.matches("c", hin.CTRL) or key.matches("u", hin.CTRL) or key.matches("backspace", hin.SUPER):
            rename.update(value="", replace_on_type=False)
        elif (key.matches("backspace", hin.CTRL) or key.matches("backspace", hin.ALT)
              or key.matches("w", hin.CTRL) or key.matches("h", hin.CTRL)):
            rename_delete_word()
        elif key.matches("backspace"):
            if rename["replace_on_type"]:
                rename.update(value="", replace_on_type=False)
            else:
                rename["value"] = rename["value"][:-1]
        else:
            text = key_text(key)
            if text is not None and text >= " ":
                rename_insert(text)
            else:
                return
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
            rename.update(value="", replace_on_type=False)
            draw_rename()
        else:
            close_rename(False)

    # ── Copy mode (prefix+[, herdr app/input/copy_mode.rs + menus.rs:63-128) ──
    # ``pane`` stays set while a prefix command runs (copy mode survives focus moves
    # that come back); ``anchor`` is the absolute row/col a v/space selection started at.
    copy = {"pane": None, "row": 0, "col": 0, "anchor": None, "linewise": None,
            "entry_offset": 0, "prompt": None, "query": "", "dir": 1, "status": ""}

    def copy_rect():
        sl = slice_of(copy["pane"])
        return sl[1] if sl else None

    def copy_metrics():
        return scroll_state.get(copy["pane"], {}).get("metrics") or {}

    def copy_plain_rows():
        """The viewport rows as plain text (one character per cell, a wide one once)."""
        rect = copy_rect()
        return ["".join(symbol for symbol, _s in row) for row in pane_cells.get(copy["pane"], [])][:rect.height if rect else 0]

    def copy_clamp():
        rect = copy_rect()
        if rect is not None:
            copy["row"] = min(copy["row"], max(0, rect.height - 1))
            copy["col"] = min(copy["col"], max(0, rect.width - 1))

    def draw_copy_cursor():
        request_render()               # the copy cursor is composed into the frame (compose_copy_cursor)

    def erase_copy_cursor():
        request_render()

    def copy_bar():
        width = max(10, cols - side["w"])
        if copy["prompt"] is not None:
            marker = "/" if copy["prompt"]["dir"] > 0 else "?"
            hints = ((f"{marker} {copy['prompt']['query']}█", ""), ("enter", "search"), ("esc", "cancel"))
            return show_bottom_bar(format_mode_bar("COPY", hints, width))
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
        rect = slice_of(focused)
        if rect is None:
            return
        rect = rect[1]
        if rect.width == 0 or rect.height == 0:
            return
        state = pane_cursor.get(focused) or {}
        at = state.get("at", [0, rect.height - 1]) if not state.get("hidden") else [0, rect.height - 1]
        clear_selection()
        copy.update(pane=focused, anchor=None, linewise=None, prompt=None, status="", query="",
                    entry_offset=(scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom", 0),
                    row=min(max(at[1], 0), max(0, rect.height - 1)),
                    col=min(max(at[0], 0), max(0, rect.width - 1)))
        mode = Mode.COPY
        copy_bar()
        draw_copy_cursor()

    def exit_copy(yank):
        """copy_mode.rs exit_copy_mode: copy or drop the selection, then put the pane back
        at the scroll position it had when copy mode began."""
        nonlocal mode
        if yank and sel["it"] is not None and sel["it"].pane == copy["pane"]:
            copy_selection()
        else:
            clear_selection()
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
        """copy_mode.rs sync_copy_mode_selection: the selection follows the cursor."""
        rect = copy_rect()
        if rect is None:
            return
        if copy["linewise"] is not None:
            cursor_row = _abs_row(copy["row"], copy_metrics())
            sel["it"] = Selection.lines(copy["pane"], copy["linewise"], cursor_row, max(0, rect.width - 1))
            paint_selection()
        elif copy["anchor"] is not None and sel["it"] is not None:
            sel["it"].drag(rect.x + copy["col"], rect.y + copy["row"], rect, copy_metrics())
            paint_selection()

    def copy_move(drow, dcol):
        rect = copy_rect()
        if rect is None:
            return
        erase_copy_cursor()
        row, col = copy["row"] + drow, copy["col"] + dcol
        if row < 0:
            copy_scroll(row)                  # past the top edge: pull history down
            row = 0
        elif row >= rect.height:
            copy_scroll(row - rect.height + 1)
            row = rect.height - 1
        copy["row"], copy["col"] = row, min(max(col, 0), max(0, rect.width - 1))
        copy_sync_selection()
        draw_copy_cursor()

    def copy_word(motion):
        """copy_mode.rs word motions on the current row: w next start, b previous start, e next end."""
        line = copy_plain_rows()[copy["row"]] if copy["row"] < len(copy_plain_rows()) else ""
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
        rows_ = copy_plain_rows()
        r = copy["row"] + direction
        while 0 <= r < len(rows_) and rows_[r].strip():
            r += direction
        r = max(0, min(r, len(rows_) - 1))
        copy_move(r - copy["row"], 0)

    def copy_find(direction, from_cursor=True):
        """Search the visible rows from the cursor, then page through history in that direction."""
        query = copy["query"]
        if not query:
            return
        fold = query == query.lower()                      # smart case, as in tmux/herdr
        for _page in range(200):
            rows_ = copy_plain_rows()
            order = range(copy["row"] + (1 if from_cursor else 0), len(rows_)) if direction > 0 \
                else range(copy["row"] - (1 if from_cursor else 0), -1, -1)
            for r in order:
                hay = rows_[r].lower() if fold else rows_[r]
                needle = query.lower() if fold else query
                i = hay.find(needle)
                if i >= 0:
                    col = sum(max(1, _wcwidth(ch)) for ch in rows_[r][:i])
                    erase_copy_cursor()
                    copy["row"], copy["col"] = r, col
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
            copy["row"] = (len(copy_plain_rows()) - 1) if direction < 0 else 0
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
        copy["anchor"] = (_abs_row(copy["row"], copy_metrics()), copy["col"])
        sel["it"] = Selection.at(copy["pane"], copy["row"], copy["col"], copy_metrics())
        paint_selection()
        copy_bar()

    def copy_select_line():
        """copy_mode.rs select_copy_mode_line: whole rows from here (V)."""
        rect = copy_rect()
        if rect is None:
            return
        copy["anchor"] = None
        copy["linewise"] = _abs_row(copy["row"], copy_metrics())
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
        if key.matches(PREFIX.code, PREFIX.mods):
            erase_copy_cursor()                       # panes.rs:672: the copy cursor is drawn in Copy mode only
            mode = Mode.PREFIX
            draw_prefix_bar()
            return
        text = key_text(key)
        if copy["prompt"] is not None:                # search prompt (copy_mode.rs:133-165)
            if key.matches("esc"):
                copy["prompt"] = None
            elif key.matches("enter"):
                copy["query"], copy["dir"] = copy["prompt"]["query"], copy["prompt"]["dir"]
                copy["prompt"] = None
                copy_find(copy["dir"])
            elif key.matches("backspace"):
                copy["prompt"]["query"] = copy["prompt"]["query"][:-1]
            elif key.matches("u", hin.CTRL):
                copy["prompt"]["query"] = ""
            elif text is not None and text >= " ":
                copy["prompt"]["query"] += text
            copy_bar()
            return
        rect = copy_rect()
        page = max(1, rect.height - 2) if rect else 1     # copy_mode_page_lines
        half = max(1, rect.height // 2) if rect else 1
        if text == "q":
            exit_copy(False)
        elif key.matches("esc"):
            if copy["anchor"] is not None or copy["linewise"] is not None or copy["query"]:
                copy_clear_selection()
                copy.update(query="", status="")
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
            copy["row"] = 0
            copy_sync_selection()
            draw_copy_cursor()
        elif text == "G":
            metrics = copy_metrics()
            erase_copy_cursor()
            copy_scroll(metrics.get("offset_from_bottom", 0))
            copy["row"] = max(0, (rect.height if rect else 1) - 1)
            copy_sync_selection()
            draw_copy_cursor()
        elif text == "0" or key.matches("home"):
            copy_move(0, -copy["col"])
        elif text == "$" or key.matches("end"):
            line = copy_plain_rows()[copy["row"]] if rect else ""
            last = sum(max(1, _wcwidth(ch)) for ch in line.rstrip()) - 1   # last_character_col
            copy_move(0, max(0, last) - copy["col"])
        elif text == "^":
            line = copy_plain_rows()[copy["row"]] if rect else ""
            copy_move(0, (len(line) - len(line.lstrip())) - copy["col"])
        elif text in ("/", "?"):
            copy["prompt"] = {"dir": 1 if text == "/" else -1, "query": ""}
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
            copy["prompt"]["query"] += "".join(ch for ch in text if ch >= " ")
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
        nonlocal mode
        mode = Mode.TERMINAL
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
    tab_scroll, tab_follow = 0, True   # herdr tab_scroll / tab_scroll_follow_active
    resized = {"hit": True}
    signal.signal(signal.SIGWINCH, lambda *_a: resized.update(hit=True))

    def slice_of(pane_id):
        return next((s for s in slices if s[0] == pane_id), None)

    pane_input = {}      # pane id -> the daemon's input state (what the program asked its terminal for)

    def input_state_of(pane_id):
        return pane_input.get(pane_id) or pin.DEFAULT_STATE

    def rename_caret():
        """dialogs.rs:44-76: the host cursor sits after the typed text (IMEs compose
        there); the text stops one column short so the caret always has a blank cell."""
        rect = rename["rect"]
        if rect is None:
            return None
        x = min(rect.x + 2 + _wcwidth(rename["value"]), rect.x + rect.width - 2)
        return (x, rect.y + 3)

    def host_cursor():
        """Where the host cursor goes once a frame is written; sent with every frame
        (tab_surface.rs tab_surface_cursor + dialogs.rs:44-76).

        Terminal mode: the focused pane's cursor, at its position, hidden while that pane is
        scrolled back or while its program hides it (the engine draws its own block and hides
        the real one; showing ours too would stack into one brighter block). The rename box:
        the caret after the typed text, so an IME composes there. Any other mode: hidden --
        a popup or the copy-mode cursor owns the screen, and the pane cursor blinking
        underneath was the IME's cue to open its candidate window in the wrong place."""
        if mode is Mode.RENAME:
            caret = rename_caret()
            return b"\x1b[?25l" if caret is None else f"\x1b[{caret[1] + 1};{caret[0] + 1}H\x1b[?25h".encode()
        if mode is not Mode.TERMINAL:
            return b"\x1b[?25l"
        sl = slice_of(focused)
        state = pane_cursor.get(focused)
        if sl is None or state is None:
            return b"\x1b[?25l"
        if (scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom"):
            return b"\x1b[?25l"
        rect = sl[1]
        at = state["at"]
        col = rect.x + 1 + min(at[0], max(0, rect.width - 1))
        row = rect.y + 1 + min(at[1], max(0, rect.height - 1))
        return (f"\x1b[{min(row, rows)};{col}H".encode()
                + (b"\x1b[?25l" if state["hidden"] else b"\x1b[?25h"))

    # ── Frames (herdr: ratatui Buffer + Terminal::draw) ──
    # Every drawing path writes cells; one render per loop turn composes the frame from
    # state and writes the diff against the frame before. Nothing on the host is ever
    # cleared and repainted: a closed popup is simply not composed any more.
    frame = {"previous": None, "dirty": True, "cursor": b""}

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
        show_bottom_bar(format_prefix_bar(max(10, cols - side["w"])))

    chrome_state = {"chromed": [], "area": None, "tracks": []}   # Border and scrollbar geometry.
    side_scrolls = {}              # Scroll offset (in entries) per sidebar section: spaces / sessions / agents.
    scroll_state = {}    # pane id -> {"metrics": ..., "alt": bool} (scroll readings from the daemon)
    ui_map = {"bar": None, "hits": [], "sections": {}}   # Mouse hit areas: tab bar geometry plus sidebar hit rects.

    def compose_sidebar(buf):
        """The sidebar and the tab row into the frame; refreshes the mouse hit map."""
        nonlocal tab_scroll
        names = [tab_label(index) for index in range(len(tabs_of()))]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(names))
        # mouse_chrome=True: herdr's "+" new-tab button and overflow scroll buttons.
        space = active_space()
        zoomed = {i for i, z in enumerate(space["zoomed"]) if z} if space else ()   # tabs.rs:36-45 " Z"
        bar = hui.compute_tab_bar_view(names, active_tab(), view["tab_bar_rect"],
                                       tab_scroll, tab_follow, True, zoomed)
        tab_scroll = bar.scroll
        ui_map["bar"] = bar
        tab_line = render_tab_bar(names, active_tab(), bar, view["tab_bar_rect"],
                                  tab_scroll, zoomed)
        rows_, agents = sidebar_model(spaces, listing, focused, side["ws"])
        for row in rows_:                     # the branch row, from the poll's git cache
            row["git"] = git_view(row["folder"])
        side_order[:] = [row["key"] for row in rows_]
        side_lines, ui_map["hits"], ui_map["sections"] = format_sidebar(
            rows_, agents, side["w"], rows, collapsed=side["collapsed"],
            scrolls=side_scrolls, sort=side["sort"],
            sessions=mark_open_sessions(sessions_cache["rows"], listing, focused),
            session_mode=side["sess_mode"])
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
        if rect is None or copy["row"] >= rect.height or copy["col"] >= rect.width:
            return
        x, y = rect.x + copy["col"], rect.y + copy["row"]
        if buf.get(x, y)[0] == "":
            x -= 1
        buf.restyle(x, y, 1, lambda _s: sc.style(fg=hui.panel_contrast_fg(), bg=hui.ACCENT, bold=True))

    def compose_overlays(buf):
        """The mode bar, notices and the modal layer for the current mode (ui.rs render)."""
        if bottom_bar[0]:
            buf.put_ansi(side["w"], rows - 1, bottom_bar[0], clip=cols)
        if notice["kind"] is not None:
            for y, x, line in notice["lines"]:
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
        cursor = host_cursor()
        if data or cursor != frame["cursor"]:
            _write_all(data + cursor)
        frame["previous"], frame["cursor"] = buf, cursor

    # ── Notices (herdr status.rs): the clipboard feedback box and failure toasts ──
    notice = {"kind": None, "text": "", "context": "", "deadline": 0.0, "rect": None, "lines": []}

    def terminal_area():
        return hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs_of()))["terminal_area"]

    def layout_notice():
        if notice["kind"] is None:
            return
        if notice["kind"] == "clipboard":
            rect, lines = format_copy_feedback(notice["text"], terminal_area())
        else:
            rect, lines = format_toast(notice["text"], notice["context"], notice["kind"],
                                       hui.Rect(0, 0, cols, rows))
        notice.update(rect=rect, lines=lines)

    def show_notice(kind, text, context="", duration=COPY_FEEDBACK_DURATION):
        notice.update(kind=kind, text=text, context=context, deadline=time.monotonic() + duration)
        layout_notice()
        request_render()

    def report_failure(text):
        """A failure the user must see (herdr: a NeedsAttention toast), e.g. input a pane refused."""
        title, _, context = text.partition(": ")
        show_notice("needs_attention", title, context, TOAST_DURATION)

    def clear_notice():
        notice.update(kind=None, rect=None, lines=[])
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
            show_notice("clipboard", "copied to clipboard")

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

    def select_word_at(pane_id, row, col):
        """actions.rs select_word_at_pane_cell: the token under a double-click, copied at
        once; a pane whose program reads the mouse keeps its own double-click."""
        if pin.wants_mouse(input_state_of(pane_id)):
            return False
        cells = pane_row_text(pane_id, row)
        if col >= len(cells) or cells[col] in _WORD_BREAK:
            return False
        start = end = col
        while start > 0 and cells[start - 1] not in _WORD_BREAK:
            start -= 1
        while end + 1 < len(cells) and cells[end + 1] not in _WORD_BREAK:
            end += 1
        selection = Selection.range(pane_id, row, start, end, sel_metrics(pane_id))
        selection.finish()
        sel["it"] = selection
        request_render()
        text = selection_text(selection)
        if text.strip():
            _clipboard(text)
            show_notice("clipboard", "copied to clipboard")
        sel["clear_at"] = time.monotonic() + PANE_COPY_HIGHLIGHT_DURATION
        return True

    def paint_rows(pane_id, rendered, cur=None, clear=False, hidden=None):
        """Take a daemon frame into the pane's row cache (and cursor state). ``clear``
        means a whole new viewport (a scroll, a relayout): rows it does not mention are
        blank. Nothing is written to the host here; the next frame composes it."""
        if cur is not None or hidden is not None:
            state = pane_cursor.setdefault(pane_id, {"at": [0, 0], "hidden": False})
            if cur is not None:
                state["at"] = list(cur)
            if hidden is not None:
                state["hidden"] = bool(hidden)
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
                                    "hidden": bool(screen.get("cursor_hidden"))}
            if screen.get("input"):
                pane_input[pane_id] = screen["input"]
            paint_rows(pane_id, {str(i): line for i, line in enumerate(screen["rows"])}, clear=True)
        request_render()

    def focus(pane_id, *, force_layout=False):
        nonlocal focused
        key = space_of(pane_id)
        if key is not None and key != side["ws"]:   # herdr: focusing a pane in another workspace activates it
            side["ws"] = key
            force_layout = True
            refresh_sessions()          # the sessions list belongs to the space's folder (as switch_space)
        last_focus[side["ws"]] = pane_id
        changed = focused != pane_id
        focused = pane_id
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

    def new_pane(argv, title, *, place, cwd=None):
        """Ask the daemon for a pane seated at ``place`` (Daemon._seat): {"split": pane},
        {"tab": pane}, or {"space": True}; "name" inside it names a new tab. The pane lands in
        the focused job's folder unless ``cwd`` says otherwise (herdr terminal.new_cwd="current")."""
        nonlocal listing
        space = active_space()
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
        new_pane([os.environ.get("SHELL", "sh")], "shell", place={"space": True},
                 cwd=space["folder"] if space else os.getcwd())

    def switch_tab(index):
        """herdr switch_tab: the tab's own zoom state comes back with it."""
        nonlocal tab_follow
        vis = visible_tabs()
        if 0 <= index < len(vis):
            tab_follow = True
            focus(hui.pane_ids(vis[index])[0], force_layout=True)

    def close_focused():
        """Close the focused pane and move focus to the next live one. Returns True when no panes remain."""
        nonlocal listing
        vis, page_idx = visible_tabs(), active_tab()
        page_ids = hui.pane_ids(vis[page_idx]) if page_idx < len(vis) else []
        survivors = [pid for pid in page_ids if pid != focused]
        control.request("pane.close", {"id": focused})
        listing = panes()
        if not any(p["alive"] for p in listing):
            return True
        refocus(survivors[0] if survivors else None, page_idx)
        return False

    # ── Mouse (herdr app/input/mouse.rs handle_mouse + input/mod.rs handle_mouse_from_input_source) ──
    # Coordinates are zero-based screen cells; the modal layers take the event first, then
    # chrome (tab bar, sidebar, dividers, scrollbars), then the panes.

    def tab_bar_mouse(event):
        """Tab row: click a tab, a scroll button or "+"; the wheel cycles tabs (mouse.rs:845-870)."""
        nonlocal tab_scroll, tab_follow
        bar = ui_map.get("bar")
        if bar is None:
            return False
        if event.kind in ("wheel_up", "wheel_down"):
            vis = visible_tabs()
            if vis:
                switch_tab((active_tab() + (1 if event.kind == "wheel_down" else -1)) % len(vis))
            return True
        if event.kind != "press" or event.button != 0:
            return True
        for index, rect in enumerate(bar.tab_hit_areas):
            if rect.width and rect.x <= event.x < rect.x + rect.width:
                switch_tab(index)
                return True
        for rect, step in ((bar.scroll_left_hit_area, -1), (bar.scroll_right_hit_area, 1)):
            if rect.width and rect.x <= event.x < rect.x + rect.width:
                tab_follow = False
                tab_scroll = max(0, tab_scroll + step)
                draw_sidebar()
                return True
        rect = bar.new_tab_hit_area
        if rect.width and rect.x <= event.x < rect.x + rect.width:
            new_pane([os.environ.get("SHELL", "sh")], "shell", place={"tab": focused})
        return True

    def sidebar_mouse(event):
        """Sidebar: the wheel scrolls the section under the pointer; a click acts on the hit
        rect drawn last (the toggle sits over the list)."""
        if event.kind in ("wheel_up", "wheel_down"):
            for key, section in ui_map["sections"].items():   # One entry per notch, clamped like herdr.
                rect = section["rect"]
                if rect.height and rect.y <= event.y < rect.y + rect.height:
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
        elif hit[0] == "sessmode":                # sessions header: this folder <-> every folder
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

    def forward_mouse(pane_id, rect, event):
        """Give the event to the program in the pane when it asked for mouse reports
        (mouse.rs forward_pane_mouse_button/motion/wheel). True when it went there."""
        state = input_state_of(pane_id)
        data = pin.encode_mouse(event, event.x - rect.x, event.y - rect.y, state)
        if data is None:
            return False
        send_to_pane(pane_id, data)
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
        if forward_mouse(pane_id, rect, event):
            sel["it"] = None
            return
        sel["it"] = Selection.at(pane_id, event.y - rect.y, event.x - rect.x, sel_metrics(pane_id))

    def pane_double_click(pane_id, rect, event, now):
        """input/mod.rs handle_pane_double_click: the second unmodified left press within
        350 ms, in the same pane, at most one row away, selects the word (and copies it)."""
        if mode is not Mode.TERMINAL or event.mods & ~hin.LOCK_MASK:
            last_pane_click["pane"] = None
            return False
        row, col = event.y - rect.y, event.x - rect.x
        last = last_pane_click
        double = (last["pane"] == pane_id and now - last["at"] <= PANE_DOUBLE_CLICK_WINDOW
                  and abs(last["row"] - row) <= 1)
        last_pane_click.update(pane=None if double else pane_id, row=row, col=col, at=now)
        return double and select_word_at(pane_id, row, col)

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
                forward_mouse(hit[0], hit[1], event)
            return
        if event.kind == "drag":
            if event.button == 0 and sel["it"] is not None:
                update_selection_drag(x, y)
                return
            if drag[0] is not None and event.button == 0:
                drag_to(x, y)
                return
            hit = None if in_sidebar else pane_at(x, y)
            if hit is not None:
                forward_mouse(hit[0], hit[1], event)
            return
        if event.kind == "release":
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
                return
            hit = None if in_sidebar else pane_at(x, y)
            if hit is not None:
                forward_mouse(hit[0], hit[1], event)
            return
        if event.kind.startswith("wheel"):
            if on_tab_bar:
                tab_bar_mouse(event)
                return
            if in_sidebar:
                sidebar_mouse(event)
                return
            if sel["it"] is not None and sel["it"].in_progress and event.kind in ("wheel_up", "wheel_down"):
                scroll_pane(sel["it"].pane, -MOUSE_SCROLL_LINES if event.kind == "wheel_up" else MOUSE_SCROLL_LINES)
                update_selection_drag(x, y)
                return
            clear_selection()
            hit = pane_at(x, y)
            if hit is not None:
                pane_wheel(hit[0], hit[1], event)
                return
            frame = pane_frame_at(x, y)
            if frame is not None and event.kind in ("wheel_up", "wheel_down"):
                sl = slice_of(frame)
                if sl is not None:
                    pane_wheel(frame, sl[1], event)
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
        selection = sel["it"]
        sl = slice_of(selection.pane)
        if sl is None:
            return
        pane, rect = selection.pane, sl[1]
        top = rect.y
        bottom = rect.y + max(0, rect.height - 1)
        anchor_row, anchor_col = selection.anchor_screen_pos(rect, sel_metrics(pane))
        is_dragging = selection.phase == "dragging" or (anchor_row, anchor_col) != (y, x)
        selection.drag(x, y, rect, sel_metrics(pane))
        if is_dragging and selection.just_click:
            selection.force_dragging()

        def arm(direction):
            sel["autoscroll"] = {"dir": direction, "x": x, "y": y, "rect": rect}
            sel["autoscroll_at"] = time.monotonic() + SELECTION_AUTOSCROLL_INTERVAL

        if y < top:
            if is_dragging:
                scroll_pane(pane, -_selection_edge_scroll_lines(top - y))
                selection.drag(x, y, rect, sel_metrics(pane))   # re-advance onto the revealed rows
                arm(-1)
        elif y > bottom:
            if is_dragging:
                scroll_pane(pane, _selection_edge_scroll_lines(y - bottom))
                selection.drag(x, y, rect, sel_metrics(pane))
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
        if selection is None or selection.phase != "dragging":
            clear_autoscroll()
            return
        pane = selection.pane
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
        selection.drag(auto["x"], auto["y"], auto["rect"], sel_metrics(pane))
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
        _send_pane_input(control, pane_out["pane"], bytes(pane_out["data"]), report_failure)
        pane_out.update(pane=None, data=bytearray())
        repaint_after_typing = time.monotonic() + 0.9   # Repaint after typing stops to erase IME leftovers.

    def terminal_key(key):
        """terminal.rs prepare_terminal_key_forward: a key clears a retained selection, the
        prefix key opens the prefix layer, a plain PageUp/PageDown scrolls a shell
        transcript, everything else is encoded for the pane's protocol."""
        nonlocal mode
        if key.kind != "release":
            clear_selection()
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
        if key.matches("esc") or text is None:
            leave_command_mode()
            return
        mode = Mode.TERMINAL
        restore_bottom()
        # navigate.rs copy_mode_survives_prefix_action: only focus moves keep copy mode
        # alive (it comes back if the focus returns to its pane); anything else cancels it.
        if copy["pane"] is not None and text not in ("1", "2", "3", "4", "5", "6", "7", "8", "9",
                                                     "n", "p", "h", "j", "k", "l"):
            exit_copy(False)
            mode = Mode.TERMINAL
        if text in "123456789":                  # herdr: digits switch tabs.
            switch_tab(int(text) - 1)
        elif text in ("n", "p") and visible_tabs():   # Cycle the active space's tabs.
            switch_tab((active_tab() + (1 if text == "n" else -1)) % len(visible_tabs()))
        elif text in ("h", "j", "k", "l"):       # herdr focus_pane_h/j/k/l.
            target = hui.find_in_direction(
                focused, {"h": "left", "j": "down", "k": "up", "l": "right"}[text],
                [(pid, rect) for pid, rect in slices])
            if target:
                focus(target)
        elif text == "z":                        # herdr zoom: the focused pane fills the tab.
            toggle_zoom()
        elif text == "c":                        # herdr new_tab.
            new_pane([os.environ.get("SHELL", "sh")], "shell", place={"tab": focused})
        elif text == "v":                        # split_vertical: side by side.
            new_pane([os.environ.get("SHELL", "sh")], "shell", place={"split": focused, "direction": "h"})
        elif text == "-":                        # split_horizontal: stacked.
            new_pane([os.environ.get("SHELL", "sh")], "shell", place={"split": focused, "direction": "v"})
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

    def handle_key(key):
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
            rename_insert(text)
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
        try_send_focus_event)."""
        data = pin.focus_bytes(gained, input_state_of(focused))
        if data:
            send_to_pane(focused, data)

    old_attrs = termios.tcgetattr(0)
    new_attrs = termios.tcgetattr(0)
    new_attrs[0] &= ~(termios.IXON | termios.ICRNL)
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(0, termios.TCSANOW, new_attrs)
    # Alternate screen (standard for herdr and every proper TUI): without it Terminal.app
    # adds a "mark" to every line that gets a carriage return, which renders as a pair of
    # dim brackets. Mouse: every motion (?1003, as crossterm's EnableMouseCapture) so a pane
    # program that asked for any-motion reports gets them and popups can highlight on hover;
    # SGR encoding. Bracketed paste and focus events come in as their own events; the kitty
    # keyboard flags are herdr's IME-compatible set (a host that lacks the protocol ignores
    # the push and sends legacy sequences, which the parser also reads).
    _write_all(b"\x1b[?1049h\x1b[?7l\x1b[?1000;1002;1003;1006h\x1b[?2004h\x1b[?1004h"
               + f"\x1b[>{pin.HOST_KITTY_FLAGS}u".encode())
    host = hin.HostInput()
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
                # ui.rs compute_view on a new size: geometry is recomputed, the mode stays.
                rows, cols = _term_size()
                _write_all(b"\x1b[0m\x1b[2J")
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
            readable, _, _ = select.select([0, stream.sock], [], [], wait)
            now = time.monotonic()
            drag_tick(now)
            if sel["autoscroll_at"] is not None and now >= sel["autoscroll_at"]:
                selection_autoscroll_tick(now)
            events = []
            if pending is not None and now >= pending and 0 not in readable:
                events = host.flush()
            if repaint_after_typing and now > repaint_after_typing:
                # IME pre-edit text is drawn directly by the host terminal over our cells: the
                # application never sees it, so no frame restores that area once it is
                # withdrawn. Once typing pauses, write the whole frame again.
                repaint_after_typing = 0.0
                sl = slice_of(focused)
                if sl is not None:
                    invalidate_rect(sl[1])
            if notice["kind"] is not None and now >= notice["deadline"]:
                clear_notice()
            if sel["clear_at"] is not None and now >= sel["clear_at"]:
                clear_selection()

            if 0 in readable:
                events = host.feed(os.read(0, 4096), now)
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
                        paint_rows(msg["id"], msg["rows"], msg.get("cursor"),
                                   hidden=msg.get("cursor_hidden"))
                    elif msg.get("event") == "exited":
                        # Card exits must reach the daemon's watcher before their pane goes.
                        page = active_tab()
                        note_exit(msg["id"], msg.get("exit_code"))
                        try:
                            if not _close_exited_pane(control, msg["id"]):
                                continue
                        except RuntimeError:
                            pass
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
        log = os.path.expanduser("~/.misaka/panel-crash.log")
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
        termios.tcsetattr(0, termios.TCSANOW, old_attrs)
        # Pop the keyboard flags, turn off mouse tracking, paste and focus reporting, leave
        # the alternate screen, and show the cursor again.
        _write_all(b"\x1b[<u\x1b[?1000;1002;1003;1006l\x1b[?2004l\x1b[?1004l\x1b[?7h\x1b[0m\x1b[2J\x1b[?1049l\x1b[?25h")
        if exit_reason[0] == "closed_all":
            print("All panes closed (their last lines are in ~/.misaka/panel-crash.log). "
                  "Run `misaka` to open the panel again.")
        else:
            print("Panel closed. Last Order and the Sisters shut down with it; nothing keeps running in the background.")
