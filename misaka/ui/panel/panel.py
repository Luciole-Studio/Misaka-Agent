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
import fcntl
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import termios
import time

from misaka.ui.panel import client as net
from misaka.ui.panel import geometry as hui


def _prefix_key():
    """Prefix key, default ctrl+b. Inside tmux that key is taken, so set
    MISAKA_PANEL_PREFIX=ctrl+g (or similar) to change it."""
    name = os.environ.get("MISAKA_PANEL_PREFIX", "ctrl+b").lower().strip()
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return bytes([ord(name[5]) - 96])
    return b"\x02"


PREFIX = _prefix_key()
POLL_SECONDS = 2.0
GIT_TTL_SECONDS = 10.0     # a branch row is not worth a git subprocess every other second
SIDEBAR_W = 26            # herdr ui.sidebar_width default (config/model.rs:1010); the separator column is the last one.
SIDEBAR_COLLAPSED_W = 4   # herdr ui.rs:229: the collapsed sidebar.
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_MOUSE_X10 = re.compile(rb"\x1b\[M[\x20-\xff]{3}")   # Legacy X10 mouse reports (some terminals lack SGR mode).
# Word-break characters for double-click selection (herdr's embedded set) plus whitespace.
_WORD_BREAK = set(" \t" + "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~")

_wcwidth = hui.display_width


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


def session_lineage(path, limit=10):
    """The files a session was forked from (header ``parentSession``), nearest first.
    One header line read per hop; safe on missing files."""
    from misaka.core.session_manager import read_session_header
    out, current = [], path
    for _ in range(limit):
        parent = (read_session_header(current) or {}).get("parentSession")
        if not parent or parent in out:
            break
        out.append(parent)
        current = parent
    return out


def kin_pane(path, listing):
    """The pane already holding a blood relative of this session (fork lineage, either
    direction): reopening seats the click beside it, so a fork pair always comes up the
    same way it was born -- split in one tab. None when no relative is open."""
    target = os.path.realpath(path)
    ancestors = {os.path.realpath(p) for p in session_lineage(target)}
    for pane in listing:
        if not pane.get("alive"):
            continue
        reported = (pane.get("reported") or {}).get("session") or session_arg(pane.get("argv"))
        if not reported:
            continue
        open_path = os.path.realpath(reported)
        if open_path == target:
            continue                    # the same session: reopen_session focuses it instead
        if (open_path in ancestors
                or target in {os.path.realpath(p) for p in session_lineage(open_path)}):
            return pane["id"]
    return None


def session_key(action):
    """The identity of the session a row points at: the file, or the card that owns one."""
    if action[0] == "sess-card":
        return ("card", action[1])
    return os.path.realpath(action[-1])


def session_arg(argv):
    """The session file a pane was launched on (``--session <path>``). Known the moment the
    pane exists, long before the session inside reports itself (core/network/wiring/panel.py
    needs the engine up, which can take a while): until this, clicking a starting session
    again opened it a second time. Pure, so testable."""
    argv = list(argv or ())
    return argv[argv.index("--session") + 1] if "--session" in argv[:-1] else None


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
        path = (pane.get("reported") or {}).get("session") or session_arg(pane.get("argv"))
        if path:
            live[os.path.realpath(path)] = pane["id"]
    return live


def mark_open_sessions(rows, listing, focused):
    """Give every session row that already lives in a pane that pane's state dot, and mark the
    focused one the way the active space is marked."""
    live = open_session_panes(listing)
    by_id = {pane["id"]: pane for pane in listing}
    out = []
    for row in rows:
        pane_id = live.get(session_key(row["action"])) if row["kind"] == "item" else None
        if pane_id:
            state, seen = pane_state(by_id[pane_id])
            row = {**row, "pane": pane_id, "state": state, "seen": seen,
                   "active": pane_id == focused}
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
            if entry.get("pane"):              # already open: wear that pane's state dot
                glyph, color = hui.state_dot(entry["state"], entry["seen"])
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


def draw_pane_borders(chromed, splits=(), area=None, titles=None):
    """Draw every pane border (output side of herdr panes.rs:484-528 render_pane_borders):
    focused edges in accent, the rest in overlay0; then each pane's title on its top border
    (panes.rs:614-665: from the second column, accent + bold when focused, overlay0 otherwise).
    ``titles`` maps pane id -> label. Called after all pane content has been rendered, as in
    herdr. Pure, so testable."""
    cells = hui.pane_border_cells(chromed, splits=splits, area=area)
    out, last_color = [], None
    for (x, y), (symbol, focused) in sorted(cells.items(), key=lambda kv: kv[0][::-1]):
        color = hui.ACCENT if focused else hui.PALETTE["border"]
        if color != last_color:
            out.append(hui.sgr_fg(color))
            last_color = color
        out.append(f"\x1b[{y + 1};{x + 1}H{symbol}")
    for item in chromed:
        rect = item["rect"]
        if "top" not in item["borders"] or rect.width <= 4:
            continue
        title = hui.pane_border_title((titles or {}).get(item["id"], ""), rect.width)
        if title is None:
            continue
        style = (hui.sgr_fg(hui.ACCENT) + "\x1b[1m") if item["focused"] else hui.sgr_fg(hui.OVERLAY0)
        out.append(f"\x1b[0m{style}\x1b[{rect.y + 1};{rect.x + 2}H{title}")
    if out:
        out.append("\x1b[0m")
    return "".join(out)


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


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clip_row(line, width):
    """Truncate one rendered pane row to ``width`` display columns, keeping its SGR sequences.

    The daemon renders a row at the pane's own ``screen.columns``, which is not always the
    width of the slice the panel gave it: ``relayout`` skips ``pane.resize`` for a pane whose
    inner rect is under 2x2, and again when the resize errors, leaving the daemon on the old
    (120-column) size. Painting that row unclipped runs it straight through the border and
    over the neighbouring pane until the next full redraw (audit 2026-09-02, ui-panel-18)."""
    if width <= 0:
        return ""
    out, col, index = [], 0, 0
    while index < len(line):
        code = _ANSI.match(line, index)
        if code is not None:                      # zero-width: styles pass through untouched
            out.append(code.group(0))
            index = code.end()
            continue
        step = _wcwidth(line[index])
        if col + step > width:
            break
        out.append(line[index])
        col += step
        index += 1
    if index < len(line):     # cut mid-line: close whatever style we were inside of
        out.append("\x1b[0m")
    return "".join(out)


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
        note(f"{hui.sgr_fg(hui.OVERLAY1)}Input not delivered: {error}")


def _restore_bottom_row(paint, control, slices, rows, main_col):
    """Repaint the main area's bottom row from the panes that own it, inside one synchronized
    update. Module level so the escape pairing is testable: the closing ``\\x1b[?2026l`` used to
    hang off the per-pane paint inside the loop, so with borders — where no pane's inner rect
    reaches the bottom row and every iteration hits the `continue` — the opening ``?2026h`` was
    never closed and the terminal froze until its own synchronized-update timeout
    (audit 2026-09-02, ui-panel-17)."""
    paint(f"\x1b[?25l\x1b[?2026h\x1b[{rows};{main_col}H\x1b[K".encode())
    try:
        for pane_id, rect in slices:
            if rect.y + rect.height < rows:      # This pane does not reach the bottom row.
                continue
            try:
                screen = control.request("pane.screen", {"id": pane_id})
            except (RuntimeError, ConnectionError):
                continue
            if screen["rows"]:
                paint(f"\x1b[{rows};{rect.x + 1}H{screen['rows'][-1]}".encode())
    finally:
        paint(b"\x1b[?2026l")


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
    zoom = False
    # Cursor state is kept per pane: position and visibility belong to each pane (the engine
    # hides the real cursor and draws its own block; a shell shows it). With one global value,
    # any path that forgot to resync on pane switch would stack the real cursor on top of the
    # engine's fake block, which suddenly looks brighter. (Bitten twice: IME input, click-to-focus.)
    pane_cursor = {}          # pane id -> {"at": [col, row], "hidden": bool}
    slices = []
    # herdr semantics: a tab is a page holding a BSP split tree (layout.rs port; nodes in geometry.py).
    # The daemon owns the layout and seats every pane (herdr server model); this is a view of it.
    spaces = []          # [{"id", "folder", "name", "tabs": [trees], "tab_names": [str | None]}]
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
        return [(s["id"], list(s["tabs"]), list(s["tab_names"]), s["name"]) for s in spaces]

    def reload_layout():
        """Pull the daemon's layout. Panes the last listing reported dead are left out of the
        view (the poll closes them); see _without_dead. Returns True when anything changed."""
        before = _layout_snapshot()
        dead = {p["id"] for p in listing if not p["alive"]}
        payload = control.request("layout.get")
        layout_rev["n"] = payload.get("revision")
        got = []
        for space in payload["spaces"]:
            trees, names = [], []
            for tab in space["tabs"]:
                tree = _without_dead(hui.from_jsonable(tab["tree"]), dead)
                if tree is not None:
                    trees.append(tree)
                    names.append(tab.get("name"))
            if trees:
                got.append({"id": space["id"], "folder": space["folder"], "name": space.get("name"),
                            "tabs": trees, "tab_names": names})
        spaces[:] = got
        if side["ws"] is not None and side["ws"] not in {space["id"] for space in spaces}:
            # The active space left the layout (its last pane died): `active_space()` was None
            # from here on, no row was highlighted, and the sessions list fell back to the
            # folder the panel was launched in -- for a `~` space that read as zero sessions.
            side["ws"] = space_of(focused) or (spaces[0]["id"] if spaces else None)
            refresh_sessions()
            chrome_cache["rows"] = []
        return before != _layout_snapshot()

    def push_layout():
        """A client-side edit (a dragged divider, a rename) goes back to the daemon, against the
        revision this view was built on; if the daemon moved on meanwhile, its layout wins."""
        try:
            out = control.request("layout.set", {"revision": layout_rev["n"], "spaces": [
                {"id": s["id"], "folder": s["folder"], "name": s["name"],
                 "tabs": [{"name": name, "tree": hui.to_jsonable(tree)}
                          for name, tree in zip(s["tab_names"], s["tabs"])]}
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
        chrome_cache["rows"] = []
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

    cards_cache = {"items": []}       # the board's cards (cards.list): card sessions in the list, and where to seat one
    sessions_cache = {"rows": []}     # the sidebar draws from here: scanning on every frame would stat the disk per keystroke
    sess_folds = set()                # session groups start open; a click folds one
    meta_cache = {}

    def refresh_cards():
        try:
            cards_cache["items"] = control.request("cards.list")["cards"]
        except (RuntimeError, ConnectionError):
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
        """Past sessions: Last Order's, each Sister's direct chats, and card sessions from the
        board, newest first. "here" = the active space's folder; "all" = every folder, grouped.
        Every item carries the folder it worked in: reopening always goes back there."""
        from misaka.config import sisters as roster
        from misaka.core.session_manager import get_session_dir_for_cwd
        raw = effective_space_folder(spaces, listing, focused, side["ws"])   # the folder the row shows
        folder = os.path.realpath(raw)
        everything = side["sess_mode"] == "all"
        from misaka.config import sessions as session_roots
        root = session_roots.sessions_root()

        def files(role):
            if everything:
                try:
                    buckets = [b for b in os.listdir(os.path.join(root, role)) if b.startswith("--")]
                except OSError:
                    buckets = []
                directories = [os.path.join(root, role, bucket) for bucket in buckets]
            else:
                directories = [get_session_dir_for_cwd(raw, os.path.join(root, role))]
            names = []
            for here in directories:
                try:
                    names += [os.path.join(here, n) for n in os.listdir(here) if n.endswith(".jsonl")]
                except OSError:
                    pass
            return sorted(names, key=lambda p: os.path.getmtime(p), reverse=True)

        def item(path, who=None):
            meta = session_meta(path)
            try:
                when, t = session_stamp(os.path.getmtime(path)), os.path.getmtime(path)
            except OSError:
                when, t = "", 0
            return {"label": meta["title"] if who is None else f"{who} · {meta['title']}",
                    "when": when, "folder": os.path.realpath(meta["cwd"] or folder), "_t": t,
                    "path": os.path.realpath(path),
                    "parent": os.path.realpath(meta["parent"]) if meta.get("parent") else None,
                    "action": ("sess-lo", path) if who is None else ("sess-sis", who, path)}

        lo = [item(p) for p in files("last-order")]
        sis = [item(p, who) for who in sorted(roster()) for p in files(who)]
        for card in cards_cache["items"]:
            workspace = os.path.realpath(card["workspace"])
            if card.get("has_session") and (everything or workspace == folder):
                sis.append({"label": f"{card.get('assignee', '?')} · {card['id']} {card.get('title', '')}",
                            "when": card.get("status", ""), "folder": workspace, "_t": 0,
                            "action": ("sess-card", card["id"], card.get("assignee", "?"))})
        sis.sort(key=lambda item: -item.get("_t", 0))
        if everything:
            return session_rows(folder_groups(lo + sis), sess_folds)
        return session_rows([("last-order", "Last Order", lo), ("sisters", "Sisters", sis)], sess_folds)

    def refresh_sessions():
        try:
            sessions_cache["rows"] = gather_sessions()
        except OSError:
            pass

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
        return open_session_panes(listing).get(session_key(action))

    def session_row(action):
        return next((row for row in sessions_cache["rows"]
                     if row["kind"] == "item" and row["action"] == action), None)

    def reopen_session(hit):
        """A click on a past session: go to its open pane, or reopen it where it worked. A fork
        relative already open gets its kin split beside it (the fork rule). Otherwise a Last
        Order conversation opens a space of its own in its folder, named after the conversation;
        a Sister's chat or card session opens a new tab in the space of its folder (the active
        one first). A folder that is gone is refused, never recreated."""
        nonlocal listing
        open_pane = session_pane(hit)
        if open_pane:                                 # already in a tab: go there, never open a second one
            focus(open_pane)
            return
        row = session_row(hit) or {}
        folder = row.get("folder")
        if hit[0] == "sess-card":
            card = next((c for c in cards_cache["items"] if c["id"] == hit[1]), {})
            folder = os.path.realpath(card.get("workspace") or folder or "")
        if not folder or not os.path.isdir(folder):
            show_bottom_bar(f" cannot reopen: folder {folder or '?'} is gone")
            return

        def tab_in(folder):
            """A new tab in the space of this folder (the active one first), else a space of its own."""
            current = active_space()
            space = (current if current and current["folder"] == folder
                     else next((s for s in spaces if s["folder"] == folder), None))
            return {"tab": hui.pane_ids(space["tabs"][0])[0]} if space else {"space": True}

        if hit[0] == "sess-card":
            try:
                out = control.request("pane.open_card_session", {"task_id": hit[1], "place": tab_in(folder)})
            except RuntimeError as error:
                show_bottom_bar(f" {error}")
                return
            listing = panes()
            reload_layout()
            focus(out["pane_id"], force_layout=True)
            return
        if hit[0] == "sess-lo":
            kin = kin_pane(hit[1], listing)     # a fork relative already open: sit beside it
            place = {"split": kin} if kin else {"space": True, "name": row.get("label") or LO_TITLE}
            pane_id = new_pane([sys.executable, "-m", "misaka", "chat", "--session", hit[1]],
                               LO_TITLE, place=place, cwd=folder)
            if kin is None:
                auto_names[pane_id] = hit[1]
        else:
            kin = kin_pane(hit[2], listing)
            new_pane([sys.executable, "-m", "misaka", "chat", "--as", hit[1], "--session", hit[2]],
                     hit[1], place={"split": kin} if kin else tab_in(folder), cwd=folder)

    # The Sister roster popup (herdr's global menu, Mode::GlobalMenu): a modal that eats
    # keys and clicks until it closes.
    menu = {"open": False, "items": [], "hl": 0, "scroll": 0, "rect": None, "hits": []}

    def draw_menu():
        agents_area = (ui_map["sections"].get("agents") or {}).get("rect") or hui.Rect(0, 0, side["w"] - 1, rows)
        launcher = hui.global_launcher_rect(agents_area, SISTERS_LABEL)
        rect = hui.menu_popup_rect(hui.Rect(0, 0, cols, rows), launcher, menu["items"])
        menu["scroll"] = menu_scroll_for(menu["hl"], menu["scroll"], rect.height - 2)
        lines, menu["hits"] = format_menu_popup(menu["items"], menu["hl"], menu["scroll"], rect)
        menu["rect"] = rect
        paint(("\x1b[?25l\x1b[?2026h"
               + "".join(f"\x1b[{y + 1};{x + 1}H{line}" for y, x, line in lines)
               + "\x1b[?2026l").encode())

    def open_menu():
        from misaka.config import sisters
        menu.update(items=sorted(sisters()) or ["no sisters"], hl=0, scroll=0, open=True)
        _write_all(b"\x1b[?1003h")     # herdr highlights on hover: report every mouse move while the popup is up.
        draw_menu()

    def close_menu():
        menu["open"] = False
        _write_all(b"\x1b[?1003l\x1b[?1002h")    # Back to motion-only-while-pressed (drag selection).
        # The popup sat on rows the sidebar cache believes are unchanged: drop the cache so every
        # row is rewritten, or a ghost popup stays on screen (looked like a freeze).
        chrome_cache["rows"] = []
        rect = menu["rect"]
        if rect and rect.x + rect.width > side["w"]:
            relayout()             # It spilled into the main area: repaint the panes under it too.
        else:
            draw_sidebar()

    def menu_hover(x, y):
        """herdr mouse.rs:146-153: the pointer moves the highlight."""
        hit = next((index for rect, index in menu["hits"]
                    if rect.x <= x - 1 < rect.x + rect.width and rect.y == y - 1), None)
        if hit is not None and hit != menu["hl"]:
            menu["hl"] = hit
            draw_menu()

    def menu_move(delta):
        menu["hl"] = max(0, min(menu["hl"] + delta, len(menu["items"]) - 1))   # herdr move_prev/move_next: no wrap
        draw_menu()

    def menu_choose(index):
        name = menu["items"][index]
        close_menu()
        if name != "no sisters":   # A Sister gets a tab of her own in the active space.
            new_pane([sys.executable, "-m", "misaka", "chat", "--as", name], name, place={"tab": focused})

    # The navigator (prefix+g, herdr Mode::Navigator): a modal tree of every space and agent.
    nav = {"open": False, "query": "", "search": False, "filter": None, "selected": 0,
           "scroll": 0, "rows": [], "hits": [], "rect": None, "collapsed": set()}

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
        lines, nav["hits"] = format_navigator(
            nav["rows"], nav["selected"], nav["scroll"], rect, query=nav["query"],
            search_focused=nav["search"], state_filter=nav["filter"], detail=nav_detail(current))
        nav["rect"] = rect
        paint(("\x1b[?25l\x1b[?2026h"
               + "".join(f"\x1b[{y + 1};{x + 1}H{line}" for y, x, line in lines)
               + "\x1b[?2026l").encode())

    def open_nav():
        nav.update(open=True, query="", search=False, filter=None, scroll=0)
        nav["rows"] = nav_rows()
        nav["selected"] = next((i for i, r in enumerate(nav["rows"]) if r["pane"] == focused), 0)
        draw_nav()

    def close_nav():
        nav["open"] = False
        # It covered the sidebar and the panes. Repaint the sidebar first (cache cleared, every
        # row rewritten), then the panes: relayout clears the main area at once but refreshes
        # the sidebar only at its end, after a pause, which left the popup's left half lingering.
        chrome_cache["rows"] = []
        draw_sidebar()
        relayout()

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

    def nav_keys(chunk):
        """modal.rs:162-270 handle_navigator_key, both halves: typing in the search line, and
        the list keys (/, a, b/w/i/d, j/k, space to fold a space, G/End, Home)."""
        text = chunk.decode("utf-8", "ignore")
        index = 0
        while index < len(text) and nav["open"]:
            if text.startswith("\x1b[", index):
                end = index + 2
                while end < len(text) and not "\x40" <= text[end] <= "\x7e":
                    end += 1
                seq, index = text[index:end + 1], end + 1
                if seq == "\x1b[A":
                    nav_move(-1)
                elif seq == "\x1b[B":
                    nav_move(1)
                elif seq in ("\x1b[H", "\x1b[1~"):
                    nav["selected"] = 0
                elif seq in ("\x1b[F", "\x1b[4~"):
                    nav["selected"] = max(0, len(nav["rows"]) - 1)
                continue
            key, index = text[index], index + 1
            if nav["search"]:
                if key == "\x1b":
                    nav["search"] = False
                elif key == "\r":
                    nav_accept()
                elif key in ("\x7f", "\x08"):
                    nav["filter"] = None
                    nav["query"] = nav["query"][:-1]
                    nav["selected"] = 0
                elif key == "\x0e":
                    nav_move(1)
                elif key == "\x10":
                    nav_move(-1)
                elif key == "\x15":
                    nav.update(query="", filter=None)
                elif key >= " " and key != "\x7f":
                    nav["query"] += key
                    nav["selected"] = 0
            else:
                if key == "\x1b":
                    close_nav()
                elif key == "\r":
                    nav_accept()
                elif key == "/":
                    nav.update(search=True, filter=None)
                elif key in ("\x7f", "\x08"):
                    nav["filter"] = None
                elif key == "a":
                    nav.update(query="", filter=None)
                elif key in NAV_FILTERS:
                    nav.update(query="", filter=NAV_FILTERS[key], selected=0)
                elif key in ("j", "\x0e"):
                    nav_move(1)
                elif key in ("k", "\x10"):
                    nav_move(-1)
                elif key == " " and nav["rows"]:
                    space_key = nav["rows"][nav["selected"]]["key"]
                    nav["collapsed"].symmetric_difference_update({space_key})
                elif key == "G":
                    nav["selected"] = max(0, len(nav["rows"]) - 1)
            if nav["open"]:
                draw_nav()

    # ── Resize mode (prefix+r, herdr Mode::Resize: modal.rs:713-735, menus.rs:259-285) ──
    resize_on = [False]

    def current_tree():
        vis = visible_tabs()
        index = active_tab()
        return (index, vis[index]) if vis and index < len(vis) else (None, None)

    def enter_resize():
        resize_on[0] = True
        show_bottom_bar(format_mode_bar("RESIZE", (("h/l", "width"), ("j/k", "height"), ("esc", "done")),
                                        max(10, cols - side["w"])))

    def resize_keys(chunk):
        nonlocal listing
        text = chunk.decode("utf-8", "ignore")
        index = 0
        while index < len(text) and resize_on[0]:
            nav = None
            if text.startswith("\x1b[", index):
                end = index + 2
                while end < len(text) and not "\x40" <= text[end] <= "\x7e":
                    end += 1
                seq, index = text[index:end + 1], end + 1
                nav = {"\x1b[D": "left", "\x1b[C": "right", "\x1b[A": "up", "\x1b[B": "down"}.get(seq)
            else:
                key, index = text[index], index + 1
                if key in ("\x1b", "\r"):
                    resize_on[0] = False
                    restore_bottom()
                    return
                nav = {"h": "left", "l": "right", "k": "up", "j": "down"}.get(key)
            if nav is None:
                continue
            _tab_index, tree = current_tree()
            if tree is None or zoom:
                continue
            new_tree = hui.resize_focused(tree, focused, nav, 0.05, chrome_state["area"])   # actions.rs:1858: 5% steps
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
        draw_borders()

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
        if tree is None or zoom or not chrome_state["area"]:
            return False
        split = hui.divider_at(hui.collect_splits(tree, chrome_state["area"]), x, y)
        if split is None:
            return False
        drag[0] = {"kind": "split", "split": split, "tree": tree}
        return True

    def drag_to(x, y):
        state = drag[0]
        if state["kind"] == "bar":
            metrics = scroll_state.get(state["pane"], {}).get("metrics")
            if metrics:
                scroll_to(state["pane"], hui.scrollbar_offset_from_drag_row(metrics, state["gutter"], y, state["grab"]))
            return
        ratio = hui.drag_ratio(state["split"], x, y)
        tree, trees = state["tree"], tabs_of()
        if tree in trees and hui.get_ratio_at(tree, state["split"]["path"]) != ratio:
            new_tree = hui.set_ratio_at(tree, state["split"]["path"], ratio)
            trees[trees.index(tree)] = new_tree
            push_layout()
            state["tree"] = new_tree
            state["split"] = next((s for s in hui.collect_splits(new_tree, chrome_state["area"])
                                   if s["path"] == state["split"]["path"]), state["split"])
            relayout()

    def drag_end():
        drag[0] = None

    # ── Rename (prefix+T tab, prefix+W space; herdr dialogs.rs:43-110 rename modal) ──
    rename = {"open": False, "kind": "tab", "target": None, "value": "", "hits": [], "rect": None}

    def open_rename(kind):
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
        rename.update(open=True, kind=kind, target=target, value=value)
        draw_rename()

    def draw_rename():
        area = hui.Rect(side["w"], 0, max(1, cols - side["w"]), rows)
        rect = hui.centered_popup_rect(area, 56, 7)
        if rect is None:
            rename["open"] = False
            return
        title = "rename tab" if rename["kind"] == "tab" else "rename space"
        lines, rename["hits"] = format_rename_popup(title, rename["value"], rect)
        rename["rect"] = rect
        paint(("\x1b[?25l\x1b[?2026h"
               + "".join(f"\x1b[{y + 1};{x + 1}H{line}" for y, x, line in lines)
               + "\x1b[?2026l").encode())

    def close_rename(save):
        if save:
            value = rename["value"].strip()
            if rename["kind"] == "tab":
                space, index = active_space(), rename["target"]
                if space is not None and index < len(space["tabs"]):
                    for pid in hui.pane_ids(space["tabs"][index]):
                        auto_names.pop(pid, None)   # a typed name is never auto-refreshed
                    space["tab_names"][index] = value or None
            else:
                space = next((s for s in spaces if s["id"] == rename["target"]), None)
                if space is not None:
                    space["name"] = value or None
            if space is not None:
                push_layout()
        rename["open"] = False
        chrome_cache["rows"] = []
        relayout()

    def rename_keys(chunk):
        text = chunk.decode("utf-8", "ignore")
        index = 0
        while index < len(text) and rename["open"]:
            if text.startswith("\x1b[", index):            # arrows etc.: ignored
                end = index + 2
                while end < len(text) and not "\x40" <= text[end] <= "\x7e":
                    end += 1
                index = end + 1
                continue
            key, index = text[index], index + 1
            if key == "\x1b":
                close_rename(False)
            elif key == "\r":
                close_rename(True)
            elif key == "\x03":
                rename["value"] = ""
            elif key in ("\x7f", "\x08"):
                rename["value"] = rename["value"][:-1]
            elif key >= " ":
                rename["value"] += key
            if rename["open"]:
                draw_rename()

    # ── Copy mode (prefix+[, herdr app/input/copy_mode.rs + menus.rs:63-128) ──
    copy = {"on": False, "pane": None, "row": 0, "col": 0, "anchor": None, "line": False,
            "prompt": None, "query": "", "dir": 1, "status": ""}

    def copy_rect():
        sl = slice_of(copy["pane"])
        return sl[1] if sl else None

    def copy_plain_rows():
        rect = copy_rect()
        buf = pane_rows.get(copy["pane"], [])
        return [_ANSI.sub("", buf[r]) if r < len(buf) else "" for r in range(rect.height if rect else 0)]

    def draw_copy_cursor():
        rect = copy_rect()
        if rect is None or not copy["on"]:
            return
        rows_ = copy_plain_rows()
        line = rows_[copy["row"]] if copy["row"] < len(rows_) else ""
        cells = []
        for ch in line:
            cells += [ch] * max(1, _wcwidth(ch))
        ch = cells[copy["col"]] if copy["col"] < len(cells) else " "
        y, x = rect.y + copy["row"] + 1, rect.x + copy["col"] + 1
        _write_all(f"\x1b[{y};{x}H\x1b[7m{ch if ch.strip() else ' '}\x1b[0m".encode())

    def erase_copy_cursor():
        rect = copy_rect()
        if rect is None:
            return
        buf = pane_rows.get(copy["pane"], [])
        row = copy["row"]
        line = buf[row] if row < len(buf) else ""
        y = rect.y + row + 1
        _write_all(f"\x1b[{y};{rect.x + 1}H\x1b[0m{' ' * rect.width}\x1b[{y};{rect.x + 1}H{line}".encode())
        if sel["pane"] == copy["pane"] and sel["a"] and sel["b"]:
            paint_selection([row])

    def copy_bar():
        width = max(10, cols - side["w"])
        if copy["prompt"] is not None:
            marker = "/" if copy["prompt"]["dir"] > 0 else "?"
            hints = ((f"{marker} {copy['prompt']['query']}█", ""), ("enter", "search"), ("esc", "cancel"))
            return show_bottom_bar(format_mode_bar("COPY", hints, width))
        selecting = copy["anchor"] is not None
        status = f" {copy['status']}" if copy["status"] else ""
        hints = (("h/j/k/l w/b/e { }", "move"), ("/ ?", "search"), ("n/N", f"repeat{status}"),
                 ("v/space", "selecting" if selecting else "select"), ("y/enter", "copy"),
                 ("esc", "clear  q exit") if selecting else ("q/esc", "exit"))
        show_bottom_bar(format_mode_bar("COPY", hints, width))

    def enter_copy():
        rect = slice_of(focused)
        if rect is None:
            return
        rect = rect[1]
        at = pane_cursor.get(focused, {}).get("at", [0, rect.height - 1])
        copy.update(on=True, pane=focused, anchor=None, line=False, prompt=None, status="",
                    row=min(max(at[1], 0), max(0, rect.height - 1)),
                    col=min(max(at[0], 0), max(0, rect.width - 1)))
        copy_bar()
        draw_copy_cursor()

    def exit_copy(yank):
        if yank and sel["pane"] == copy["pane"] and sel["a"] and sel["b"]:
            copy_selection()
        erase_copy_cursor()
        clear_selection()
        copy["on"] = False
        try:
            out = control.request("pane.scroll", {"id": copy["pane"], "to": "bottom"})
            scroll_state.setdefault(copy["pane"], {})["metrics"] = out["scroll"]
            paint_rows(copy["pane"], {str(i): line for i, line in enumerate(out["rows"])}, clear=True)
            draw_borders()
        except (RuntimeError, ConnectionError):
            pass
        restore_bottom()

    def copy_scroll(delta):
        """Scroll the copy-mode pane by ``delta`` lines (negative = back into history)."""
        try:
            out = control.request("pane.scroll", {"id": copy["pane"], "delta": delta})
        except RuntimeError:
            return False
        before = (scroll_state.get(copy["pane"], {}).get("metrics") or {}).get("offset_from_bottom")
        scroll_state.setdefault(copy["pane"], {})["metrics"] = out["scroll"]
        paint_rows(copy["pane"], {str(i): line for i, line in enumerate(out["rows"])}, clear=True)
        draw_borders()
        return out["scroll"]["offset_from_bottom"] != before

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
        if copy["anchor"] is not None:
            sel.update(pane=copy["pane"], a=copy["anchor"], b=(copy["col"], copy["row"]))
            paint_selection()
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

    def copy_keys(chunk):
        text = chunk.decode("utf-8", "ignore")
        index = 0
        while index < len(text) and copy["on"]:
            seq = None
            if text.startswith("\x1b[", index):
                end = index + 2
                while end < len(text) and not "\x40" <= text[end] <= "\x7e":
                    end += 1
                seq, index = text[index:end + 1], end + 1
            else:
                seq, index = text[index], index + 1
            if copy["prompt"] is not None:                # search prompt (copy_mode.rs:133-165)
                if seq == "\x1b":
                    copy["prompt"] = None
                elif seq == "\r":
                    copy["query"], copy["dir"] = copy["prompt"]["query"], copy["prompt"]["dir"]
                    copy["prompt"] = None
                    copy_find(copy["dir"])
                elif seq in ("\x7f", "\x08"):
                    copy["prompt"]["query"] = copy["prompt"]["query"][:-1]
                elif seq == "\x15":
                    copy["prompt"]["query"] = ""
                elif len(seq) == 1 and seq >= " ":
                    copy["prompt"]["query"] += seq
                if copy["on"]:
                    copy_bar()
                continue
            rect = copy_rect()
            page = rect.height if rect else 1
            if seq in ("q",):
                exit_copy(False)
            elif seq == "\x1b":
                if copy["anchor"] is not None:
                    copy["anchor"] = None
                    clear_selection()
                    draw_copy_cursor()
                    copy_bar()
                else:
                    exit_copy(False)
            elif seq in ("y", "\r"):
                exit_copy(True)
            elif seq in ("v", " "):
                copy["anchor"] = (copy["col"], copy["row"])
                sel.update(pane=copy["pane"], a=copy["anchor"], b=copy["anchor"])
                copy_bar()
            elif seq == "V":
                copy["anchor"] = (0, copy["row"])
                copy_move(0, (rect.width - 1) - copy["col"] if rect else 0)
                copy_bar()
            elif seq in ("h", "\x1b[D"):
                copy_move(0, -1)
            elif seq in ("l", "\x1b[C"):
                copy_move(0, 1)
            elif seq in ("j", "\x1b[B"):
                copy_move(1, 0)
            elif seq in ("k", "\x1b[A"):
                copy_move(-1, 0)
            elif seq in ("\x02", "\x1b[5~"):
                copy_move(-page, 0)
            elif seq in ("\x06", "\x1b[6~"):
                copy_move(page, 0)
            elif seq == "\x15":
                copy_move(-(page // 2), 0)
            elif seq == "\x04":
                copy_move(page // 2, 0)
            elif seq == "g":
                metrics = scroll_state.get(copy["pane"], {}).get("metrics") or {}
                erase_copy_cursor()
                copy_scroll(-(metrics.get("max_offset_from_bottom", 0) - metrics.get("offset_from_bottom", 0)))
                copy["row"] = 0
                draw_copy_cursor()
            elif seq == "G":
                metrics = scroll_state.get(copy["pane"], {}).get("metrics") or {}
                erase_copy_cursor()
                copy_scroll(metrics.get("offset_from_bottom", 0))
                copy["row"] = max(0, page - 1)
                draw_copy_cursor()
            elif seq in ("0", "\x1b[H"):
                copy_move(0, -copy["col"])
            elif seq in ("$", "\x1b[F"):
                copy_move(0, (rect.width - 1) - copy["col"] if rect else 0)
            elif seq == "^":
                line = copy_plain_rows()[copy["row"]] if rect else ""
                copy_move(0, (len(line) - len(line.lstrip())) - copy["col"])
            elif seq in ("/", "?"):
                copy["prompt"] = {"dir": 1 if seq == "/" else -1, "query": ""}
                copy_bar()
            elif seq == "n":
                copy_find(copy["dir"])
            elif seq == "N":
                copy_find(-copy["dir"])
            elif seq in ("w", "b", "e"):
                copy_word(seq)
            elif seq == "{":
                copy_paragraph(-1)
            elif seq == "}":
                copy_paragraph(1)

    def menu_keys(chunk):
        """herdr modal.rs:147-160 handle_global_menu_key: esc closes, k/up and j/down move, enter picks."""
        index = 0
        while index < len(chunk) and menu["open"]:
            if chunk.startswith(b"\x1b[", index):          # CSI: arrows arrive as ESC [ A / ESC [ B
                end = index + 2
                while end < len(chunk) and not 0x40 <= chunk[end] <= 0x7e:
                    end += 1
                seq, index = chunk[index:end + 1], end + 1
                if seq == b"\x1b[A":
                    menu_move(-1)
                elif seq == b"\x1b[B":
                    menu_move(1)
                continue
            key, index = chunk[index:index + 1], index + 1
            if key == b"\x1b":
                close_menu()
            elif key == b"k":
                menu_move(-1)
            elif key == b"j":
                menu_move(1)
            elif key == b"\r":
                menu_choose(menu["hl"])

    def main_col():
        """First (1-based) column of the main area: flush against the sidebar separator."""
        return side["w"] + 1
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
        # herdr redraws the mode bar every frame; here any paint (a pane's screen update, the
        # borders, a relayout) can cover the bottom row, so the bar is put back after each one.
        if bottom_bar[0] and not repainting_bar[0]:
            repainting_bar[0] = True
            try:
                _write_all(f"\x1b[{rows};{main_col()}H{bottom_bar[0]}".encode() + cursor_tail())
            finally:
                repainting_bar[0] = False

    repainting_bar = [False]
    bottom_bar = [None]               # The mode bar (prefix / resize / copy) while one is up.

    def show_bottom_bar(line):
        bottom_bar[0] = line
        paint(f"\x1b[?2026h\x1b[{rows};{main_col()}H\x1b[K{line}\x1b[?2026l".encode())
    chrome_cache = {"rows": []}       # Only changed rows are written (herdr's row-level diffing).
    chrome_state = {"chromed": [], "area": None, "tracks": []}   # Border and scrollbar geometry.
    side_scrolls = {}              # Scroll offset (in entries) per sidebar section: spaces / sessions / agents.
    scroll_state = {}    # pane id -> {"metrics": ..., "alt": bool} (scroll readings from the daemon)

    def draw_borders():
        """herdr panes.rs:412: borders are always drawn last, on top of content, and redrawn when
        focus changes. Scrollbars likewise (render_pane_scrollbar also runs after content)."""
        if not chrome_state["chromed"]:
            return
        out = ["\x1b[?25l\x1b[?2026h",
               draw_pane_borders(chrome_state["chromed"], area=chrome_state["area"],
                                 titles={p["id"]: p.get("ally") or p["title"] for p in listing})]
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
    ui_map = {"bar": None, "hits": [], "sections": {}}   # Mouse hit areas: tab bar geometry plus sidebar hit rects.

    def draw_sidebar():
        nonlocal tab_scroll
        names = [tab_label(index) for index in range(len(tabs_of()))]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(names))
        # mouse_chrome=True: herdr's "+" new-tab button and overflow scroll buttons.
        zoomed = {active_tab()} if zoom else ()   # herdr: zoom is per tab; only the active one can be zoomed here
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
        wanted = []
        for row, line in enumerate(side_lines, 1):
            cell = f"\x1b[{row};1H{line}"
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
        if menu["open"]:           # A sidebar refresh under a popup must not erase it.
            draw_menu()
        if nav["open"]:
            draw_nav()
        if rename["open"]:
            draw_rename()

    def draw_prefix_bar():
        # herdr: entering prefix mode pops a mode bar on the bottom row (menus.rs
        # render_prefix_overlay). It spans only the main area; the sidebar is permanent
        # navigation and must not be covered.
        show_bottom_bar(format_prefix_bar(max(10, cols - side["w"])))

    def bottom_note(text):
        """One-line hint on the bottom row, main area only (never spills into the sidebar)."""
        width = max(10, cols - side["w"] - 1)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        while _wcwidth(plain) > width and plain:
            text, plain = text[:-1], plain[:-1]
        paint(f"\x1b[{rows};{main_col()}H\x1b[K{text}\x1b[0m".encode())

    def restore_bottom():
        """Remove the mode bar: clear the main area's bottom row, then restore that row for any
        pane that reaches it (the sidebar was never covered, so it needs nothing)."""
        bottom_bar[0] = None
        _restore_bottom_row(paint, control, slices, rows, main_col())

    def draw_help_overlay():
        # herdr: "?" shows the full key help (keybind_help), centered in the main area,
        # closed by any key. Width is clamped to the main area so narrow screens do not overflow.
        avail = cols - main_col() + 1
        width = max(24, min(46, avail))
        lines = format_help_lines(width=width)
        top = max(2, (rows - len(lines)) // 2)
        left = main_col() + max(0, (avail - width) // 2)
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
                clipped = _clip_row(line, rect.width)   # ... in the column direction too.
                out.append(f"\x1b[{rect.y + row + 1};{rect.x + 1}H{clipped}")
                buf[row] = clipped
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
        if sel["pane"] == pane_id:       # New content painted over the highlight; put it back.
            paint_selection()
        if copy["on"] and copy["pane"] == pane_id:   # ... and the copy-mode cursor.
            draw_copy_cursor()

    def relayout():
        nonlocal slices
        reload_layout()
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs_of()))
        term = view["terminal_area"]
        # herdr: only the active tab is drawn; its layout is the BSP tree cut into rectangles (layout.rs collect_panes).
        vis = visible_tabs()
        tree = vis[active_tab()] if vis else None
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
        any_resized = False
        for pane_id, inner in slices:
            if inner.width < 2 or inner.height < 2:
                continue
            try:
                out = control.request("pane.resize", {"id": pane_id,
                                                      "rows": inner.height, "cols": inner.width})
                any_resized = any_resized or bool(out.get("resized", True))
            except RuntimeError:
                continue
        for row in range(term.y + 1, term.y + term.height + 1):     # Clear the main area.
            paint(f"\x1b[{row};{term.x + 1}H\x1b[K".encode())
        paint(b"\x1b[?2026l")
        if any_resized:
            # Full-screen applications repaint only after SIGWINCH; wait briefly before grabbing
            # the screen or we get the blank one from right after the resize (herdr solves the
            # same problem with a nudge plus a forced full frame). An unchanged layout (the
            # common tab switch) skips both the SIGWINCH and this wait.
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
        key = space_of(pane_id)
        if key is not None and key != side["ws"]:   # herdr: focusing a pane in another workspace activates it
            side["ws"] = key
            force_layout = True
            refresh_sessions()          # the sessions list belongs to the space's folder (as switch_space)
            chrome_cache["rows"] = []
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
        nonlocal tab_follow, zoom
        vis = visible_tabs()
        if 0 <= index < len(vis):
            tab_follow = True
            zoom = False           # herdr: zoom is per-tab state; switching tabs leaves it.
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

    def on_wheel(x, y, delta):
        """Mouse wheel: scroll the roster popup, the navigator, a sidebar section, or the pane under the pointer."""
        if nav["open"]:
            body_h = max(1, nav["rect"].height - 7) if nav["rect"] else 1
            cap = max(0, len(nav["rows"]) - body_h)
            nav["scroll"] = max(0, min(nav["scroll"] + (1 if delta > 0 else -1), cap))
            nav["selected"] = max(nav["scroll"], min(nav["selected"], nav["scroll"] + body_h - 1))
            draw_nav()
            return
        rect = menu["rect"] if menu["open"] else None
        if rect and rect.x <= x - 1 < rect.x + rect.width and rect.y <= y - 1 < rect.y + rect.height:
            cap = max(0, len(menu["items"]) - (rect.height - 2))
            menu["scroll"] = max(0, min(menu["scroll"] + (1 if delta > 0 else -1), cap))
            menu["hl"] = max(menu["scroll"], min(menu["hl"], menu["scroll"] + rect.height - 3))
            draw_menu()
            return
        if x <= side["w"]:
            for key, section in ui_map["sections"].items():   # One entry per notch, clamped like herdr.
                rect = section["rect"]
                if rect.height and rect.y <= y - 1 < rect.y + rect.height:
                    step = 1 if delta > 0 else -1
                    side_scrolls[key] = max(
                        0, min(section["scroll"] + step, section["max_scroll"]))
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

    def on_click(x, y):
        nonlocal tab_scroll, tab_follow, help_open, prefix_pending
        if copy["on"]:                                # A click ends copy mode first (herdr: any mouse press leaves it).
            exit_copy(False)
        if rename["open"]:                            # dialogs.rs buttons: save / clear / cancel; elsewhere cancels.
            hit = next((action for rect, action in rename["hits"]
                        if rect.x <= x - 1 < rect.x + rect.width and rect.y == y - 1), None)
            if hit == "save":
                close_rename(True)
            elif hit == "clear":
                rename["value"] = ""
                draw_rename()
            else:
                close_rename(False)
            return
        if nav["open"]:                               # Navigator: a row jumps, anywhere else closes.
            hit = next((index for rect, index in nav["hits"]
                        if rect.x <= x - 1 < rect.x + rect.width and rect.y == y - 1), None)
            if hit is None:
                close_nav()
            else:
                nav["selected"] = hit
                nav_accept()
            return
        if menu["open"]:                              # herdr mouse.rs:164-172: an item picks, anywhere else closes.
            hit = next((index for rect, index in menu["hits"]
                        if rect.x <= x - 1 < rect.x + rect.width and rect.y == y - 1), None)
            if hit is None:
                close_menu()
            else:
                menu_choose(hit)
            return
        bar = ui_map.get("bar")
        if y == 1 and x > side["w"] and bar is not None:   # Tab row (main area only): herdr's hit areas.
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
                new_pane([os.environ.get("SHELL", "sh")], "shell", place={"tab": focused})
            return
        if x <= side["w"]:                            # Sidebar: hit rects, last drawn wins (the toggle sits over the list).
            hit = next((action for rect, action in reversed(ui_map["hits"])
                        if rect.x <= x - 1 < rect.x + rect.width
                        and rect.y <= y - 1 < rect.y + rect.height), None)
            if hit is None:
                return
            if hit[0] == "pane":
                focus(hit[1])
            elif hit[0] == "space":                   # herdr: click a space = switch workspace.
                switch_space(hit[1])
            elif hit[0] == "new":                     # herdr: new workspace in the same folder.
                new_space_here()
            elif hit[0] == "prefix":                  # a mouse press of the prefix key: next key is a command
                if prefix_pending:                    # pressed again: cancel, like esc
                    prefix_pending = 0
                    restore_bottom()
                else:
                    prefix_pending = 1
                    draw_prefix_bar()
            elif hit[0] == "sisters":                 # agents footer: summon a Sister as a new tab.
                open_menu()
            elif hit[0] == "sessgroup":               # MISAKA: the Last Order / Sisters groups fold.
                sess_folds.symmetric_difference_update({hit[1]})
                refresh_sessions()
                chrome_cache["rows"] = []
                draw_sidebar()
            elif hit[0] == "sessnew":                 # agents footer: a fresh Last Order session, a space of its own here
                space = active_space()
                new_pane([sys.executable, "-m", "misaka", "chat"], LO_TITLE, place={"space": True},
                         cwd=space["folder"] if space else None)
            elif hit[0] == "sessmode":                # sessions header: this folder <-> every folder
                side["sess_mode"] = "all" if side["sess_mode"] == "here" else "here"
                refresh_sessions()
                chrome_cache["rows"] = []
                draw_sidebar()
            elif hit[0] in ("sess-lo", "sess-sis", "sess-card"):
                reopen_session(hit)
            elif hit[0] == "toggle":                  # herdr toggle_sidebar: 26 columns <-> 4.
                side["collapsed"] = not side["collapsed"]
                side["w"] = SIDEBAR_COLLAPSED_W if side["collapsed"] else SIDEBAR_W
                paint(b"\x1b[0m\x1b[2J")
                chrome_cache["rows"] = []
                relayout()
            elif hit[0] == "sort":                    # herdr: click the header label to flip grouped / priority.
                side["sort"] = "priority" if side["sort"] == "grouped" else "grouped"
                draw_sidebar()
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
    try:
        paint(b"\x1b[0m\x1b[2J")
        chrome_cache["rows"] = []          # The row cache must be invalidated after a clear, or the sidebar draws nothing.
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
                rows, cols = _term_size()
                paint(b"\x1b[0m\x1b[2J")
                chrome_cache["rows"] = []
                menu["open"] = nav["open"] = rename["open"] = False   # Geometry changed under a popup; it is simply gone.
                copy["on"], resize_on[0], bottom_bar[0] = False, False, None
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
                    if menu["open"] and press and button in (32, 35):   # Pointer motion over the open popup.
                        menu_hover(mx, my)
                        continue
                    if button in (64, 65):
                        on_wheel(mx, my, -3 if button == 64 else 3)
                    elif drag[0] and press and button == 32:   # Dragging a divider or a scrollbar thumb.
                        drag_to(mx - 1, my - 1)
                    elif drag[0] and not press:
                        drag_end()
                    elif (press and button in (0, 1) and not (menu["open"] or nav["open"] or rename["open"])
                          and press_on_chrome(mx - 1, my - 1)):
                        pass                                # The press started a drag; nothing else to do.
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
                    elif not press and button == 0 and sel["b"]:
                        copy_selection()    # herdr copy_on_select: releasing copies to the clipboard.
                chunk = _MOUSE.sub(b"", chunk)
                chunk = _MOUSE_X10.sub(b"", chunk)     # Legacy reports are only stripped, so they never reach a pane as input.
                if menu["open"]:                       # The roster popup is modal: it takes every key.
                    menu_keys(chunk)
                    chunk = b""
                elif nav["open"]:                      # So are the navigator, the rename box, and the two modes.
                    nav_keys(chunk)
                    chunk = b""
                elif rename["open"]:
                    rename_keys(chunk)
                    chunk = b""
                elif copy["on"]:
                    copy_keys(chunk)
                    chunk = b""
                elif resize_on[0]:
                    resize_keys(chunk)
                    chunk = b""
                plain = bytearray()
                quitting = False   # prefix+d ends the panel, and everything it started
                for offset in range(len(chunk)):       # Byte by byte: prefix and command may arrive in one read.
                    key = chunk[offset:offset + 1]
                    if help_open:                      # Key help is open: any key closes it and redraws.
                        help_open = False
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
                    elif key in b"np" and visible_tabs():   # Cycle the active space's tabs.
                        switch_tab((active_tab() + (1 if key == b"n" else -1))
                                   % len(visible_tabs()))
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
                        new_pane([os.environ.get("SHELL", "sh")], "shell", place={"tab": focused})
                    elif key == b"v":                  # split_vertical (1062): side by side.
                        new_pane([os.environ.get("SHELL", "sh")], "shell", place={"split": focused, "direction": "h"})
                    elif key == b"-":                  # split_horizontal (1063): stacked.
                        new_pane([os.environ.get("SHELL", "sh")], "shell", place={"split": focused, "direction": "v"})
                    elif key == b"g":                  # herdr goto (prefix+g): the navigator.
                        open_nav()
                    elif key == b"[":                  # herdr copy_mode (prefix+[).
                        enter_copy()
                    elif key == b"r":                  # herdr resize_mode (prefix+r).
                        enter_resize()
                    elif key == b"T":                  # herdr rename_tab (prefix+shift+t).
                        open_rename("tab")
                    elif key == b"W":                  # herdr rename_workspace (prefix+shift+w).
                        open_rename("space")
                    elif key == b"d":
                        quitting = True
                    elif key == b"x":
                        # herdr prefix+x = ClosePane: one key, no confirmation (actions.rs:2035 only
                        # confirms when closing a worktree group, which MISAKA does not have).
                        # Closing the last pane exits the panel.
                        if close_focused():
                            exit_reason[0] = "closed_all"
                            quitting = True
                    elif key == b"?":
                        help_open = True
                        draw_help_overlay()
                if plain:
                    _send_pane_input(control, focused, plain, bottom_note)
                    repaint_after_typing = now + 0.9   # Repaint after typing stops to erase IME leftovers.
                if quitting:
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
                        page = active_tab()
                        note_exit(msg["id"], msg.get("exit_code"))
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
                            refocus(page_idx=page)
                        else:
                            relayout()
                        painted = False
                if painted:
                    draw_borders()      # Content paints over borders; herdr redraws them at the end of every frame.

            if now - last_poll > POLL_SECONDS:
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
                    draw_sidebar()
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
        # Turn off mouse tracking, leave the alternate screen, and show the cursor again.
        _write_all(b"\x1b[?1000;1002;1003;1006l\x1b[?7h\x1b[0m\x1b[2J\x1b[?1049l\x1b[?25h")
        if exit_reason[0] == "closed_all":
            print("All panes closed (their last lines are in ~/.misaka/panel-crash.log). "
                  "Run `misaka` to open the panel again.")
        else:
            print("Panel closed. Last Order and the Sisters shut down with it; nothing keeps running in the background.")
