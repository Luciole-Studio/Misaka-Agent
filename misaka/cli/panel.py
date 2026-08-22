"""Multi-pane panel: MISAKA's default entry point (herdr-style; geometry from herdr_ui).

Rendering: the daemon keeps a terminal emulator per pane; the panel subscribes
to dirty rows from every pane and lays them out itself: herdr's sidebar on the left
(spaces = folders on top, agents = panes below), a tab row at the top of the
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
SIDEBAR_W = 26            # herdr ui.sidebar_width default (config/model.rs:1010); the separator column is the last one.
SIDEBAR_COLLAPSED_W = 4   # herdr ui.rs:229: the collapsed sidebar.
SIDEBAR_SECTION_SPLIT = 0.5   # herdr app/state.rs:1848 sidebar_section_split.
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_MOUSE_X10 = re.compile(rb"\x1b\[M[\x20-\xff]{3}")   # Legacy X10 mouse reports (some terminals lack SGR mode).
# Word-break characters for double-click selection (herdr's embedded set) plus whitespace.
_WORD_BREAK = set(" \t" + "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~")

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
    """The one bridge from MISAKA pane fields to herdr's (AgentState, pane.seen) pair:
    busy = working, a card waiting for review = blocked, a finished card nobody looked at =
    done (idle + unseen), otherwise idle; a dead pane = unknown. Pure, so testable."""
    if not pane["alive"]:
        return "unknown", True
    if pane.get("busy"):
        return "working", True
    if pane.get("status") == "review":
        return "blocked", True
    if pane.get("unseen"):
        return "idle", False
    return "idle", True


LO_TITLE = "Last Order"      # the panel opens Last Order panes with this title; they are space roots


def space_key(pane):
    """A pane's folder as a real path (herdr's workspace identity cwd, workspace.rs:1161-1173)."""
    return os.path.realpath(pane.get("cwd") or os.getcwd())


def _space_label(folder):
    home = os.path.expanduser("~")
    return "~" if folder == home else (os.path.basename(folder.rstrip(os.sep)) or folder)


def group_spaces(listing):
    """One space per Last Order session: her pane is the root and every pane whose ``parent``
    chain reaches her is a member (herdr workspace = root pane plus everything opened from it).
    Panes with no Last Order above them (opened from the CLI, restored from a snapshot, or
    orphaned when she closed) join the first Last Order space in their folder, else a space of
    their own keyed by the folder. Returns ``(spaces, member_of)``: ordered space dicts
    (key, root, folder, label, state, seen, alive) and {pane id: space key}. Pure, so testable."""
    by_id = {pane["id"]: pane for pane in listing}

    def root_of(pane):
        seen = set()
        while pane.get("parent") in by_id and pane["id"] not in seen:
            seen.add(pane["id"])
            pane = by_id[pane["parent"]]
        return pane

    spaces, by_key, member_of, orphans = [], {}, {}, []
    for pane in listing:
        root = root_of(pane)
        if root["title"] != LO_TITLE:
            orphans.append(pane)
            continue
        if root["id"] not in by_key:
            folder = space_key(root)
            by_key[root["id"]] = {"key": root["id"], "root": root["id"], "folder": folder,
                                  "label": _space_label(folder), "state": "unknown",
                                  "seen": True, "alive": False}
            spaces.append(by_key[root["id"]])
        member_of[pane["id"]] = root["id"]
    for pane in orphans:
        folder = space_key(pane)
        host = next((space for space in spaces
                     if space["root"] is not None and space["folder"] == folder), None)
        if host is None:
            host = by_key.get(folder)
            if host is None:
                host = by_key[folder] = {"key": folder, "root": None, "folder": folder,
                                         "label": _space_label(folder), "state": "unknown",
                                         "seen": True, "alive": False}
                spaces.append(host)
        member_of[pane["id"]] = host["key"]
    for pane in listing:                                  # aggregate.rs:86-99 aggregate_state
        space = by_key[member_of[pane["id"]]]
        state, seen = pane_state(pane)
        space["alive"] = space["alive"] or bool(pane["alive"])
        if (hui.attention_priority(state, seen)
                > hui.attention_priority(space["state"], space["seen"])):
            space["state"], space["seen"] = state, seen
    return spaces, member_of


def tabs_by_space(tabs, space_of_pane):
    """herdr workspace.tabs: every tab belongs to one space, here the space of its first pane.
    Returns ``(groups, tab_of)``: ordered {space: [tree]} and {pane id: tab index within its
    space} (workspace.rs:488-495 tab_display_name numbers tabs per workspace). Pure, so testable."""
    groups, tab_of = {}, {}
    for tree in tabs:
        ids = hui.pane_ids(tree)
        if not ids:
            continue
        key = space_of_pane(ids[0])
        index = len(groups.setdefault(key, []))
        groups[key].append(tree)
        for pane_id in ids:
            tab_of[pane_id] = index
    return groups, tab_of


def sidebar_model(listing, focused_id, active_space=None, tab_of=None, tab_counts=None):
    """Shape the pane listing into herdr's two lists. A space is a workspace = the folder its
    panes run in, labelled by the last path component and marked with its most
    attention-worthy pane (aggregate.rs:86-99); ``active_space`` is herdr's app.active (the
    focused pane's space when None). An agent entry is one pane that runs something other than
    a bare shell (aggregate.rs:29-69 lists only panes with an agent); its tab number shows only
    when its space has several tabs (sidebar.rs:166-171). Pure, so testable."""
    tab_of, tab_counts = tab_of or {}, tab_counts or {}
    spaces, member_of = group_spaces(listing)
    by_key = {space["key"]: space for space in spaces}
    if active_space is None:
        active_space = member_of.get(focused_id)
    for space in spaces:
        space["active"] = space["key"] == active_space
    agents = []
    for pane in listing:
        if pane["title"] == "shell":          # ponytail: MISAKA's own shell panes carry this title
            continue
        state, seen = pane_state(pane)
        key = member_of[pane["id"]]
        tab = tab_of.get(pane["id"])
        agents.append({"pane": pane["id"], "space": by_key[key]["label"],
                       "tab": (str(tab + 1) if (tab is not None and tab_counts.get(key, 1) > 1)
                               else None),
                       "agent": pane["title"] or pane["id"], "state": state, "seen": seen,
                       "active": pane["id"] == focused_id})
    return spaces, agents


def _sorted_agents(agents, sort):
    """app/state.rs AgentPanelSort: "grouped" keeps space order; "priority" puts the entries
    that need attention first (stable, so ties keep the grouped order)."""
    if sort != "priority":
        return list(agents)
    return sorted(agents, key=lambda a: -hui.attention_priority(a["state"], a["seen"]))


def _put_tokens(canvas, x, y, tokens, max_width, clip):
    """sidebar.rs:818-936 resolved_token_spans, the drawing half: separators in overlay0 dim,
    each token in its own style, widths from herdr_ui.fit_tokens.
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


def _render_spaces(canvas, spaces, area, scroll, hits):
    """sidebar.rs:1040-1183 render_workspace_list with the default rows (state icon + name;
    the branch row needs git data MISAKA does not collect, so every entry is one row tall).
    Returns the section's scroll state for the wheel."""
    P = hui.PALETTE
    list_bottom = area.y + max(0, area.height - 1)
    if area.height > 0:
        canvas.put(area.x, area.y, " spaces", fg=hui.OVERLAY0, bold=True,
                   clip=area.x + area.width)
    heights = [1] * len(spaces)
    body = hui.workspace_list_body_rect(area, False)
    metrics = hui.list_scroll_metrics(heights, body.height, scroll)
    scroll = min(scroll, metrics["max_offset_from_bottom"])
    has_bar = hui.should_show_scrollbar(metrics) and body.width > 0 and body.height > 0
    body = hui.workspace_list_body_rect(area, has_bar)
    row_y, body_bottom = body.y, body.y + body.height
    for space in spaces[scroll:]:
        height = min(1, body.height)
        if height == 0 or row_y + height > body_bottom:
            break
        card = hui.Rect(body.x, row_y, body.width, height)
        if space["active"] and row_y < list_bottom:     # 1082-1091: the active space sits on surface_dim
            canvas.fill_bg(card.x, card.x + card.width, row_y, P["surface_dim"])
        name = ({"fg": hui.TEXT, "bold": True} if space["active"]
                else {"fg": P["subtext0"]})                # 1094-1098
        glyph, color = hui.state_dot(space["state"], space["seen"])
        canvas.put(card.x, row_y, " ", clip=card.x + card.width)   # 1137-1139: one-column prefix
        _put_tokens(canvas, card.x + 1, row_y,
                    [("icon", glyph, {"fg": color}), ("text", space["label"], name)],
                    max(0, card.width - 1), card.x + card.width)
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
        menu_rect = hui.global_launcher_rect(area)
        canvas.put(menu_rect.x + max(0, menu_rect.width - 4), menu_rect.y, "menu",
                   fg=hui.OVERLAY0, clip=menu_rect.x + menu_rect.width)
        hits.append((menu_rect, ("menu",)))
    return {"rect": area, "scroll": scroll, "max_scroll": metrics["max_offset_from_bottom"]}


def _render_agents(canvas, agents, area, scroll, sort, hits):
    """sidebar.rs:1189-1318 render_agent_detail with the default rows: state icon + space
    (+ tab number when there are several tabs), then the agent name on a second line."""
    P = hui.PALETTE
    state = {"rect": area, "scroll": 0, "max_scroll": 0}
    if area.height < 3:
        return state
    clip = area.x + area.width
    canvas.put(area.x, area.y, "─" * area.width, fg=P["surface_dim"], clip=clip)
    canvas.put(area.x, area.y + 1, " agents", fg=hui.OVERLAY0, bold=True, clip=clip)
    label = "priority" if sort == "priority" else "grouped"     # 86-91 agent_panel_sort_label
    toggle = hui.agent_panel_header_label_rect(area, label)
    if toggle != hui.RECT_DEFAULT:
        canvas.put(toggle.x, toggle.y, label, fg=hui.OVERLAY0, bold=True,
                   clip=toggle.x + toggle.width)
        hits.append((toggle, ("sort",)))
    heights = [2] * len(agents)
    body = hui.agent_panel_body_rect(area, False)
    metrics = hui.list_scroll_metrics(heights, body.height, scroll)
    scroll = min(scroll, metrics["max_offset_from_bottom"])
    has_bar = hui.should_show_scrollbar(metrics) and body.width > 0 and body.height > 0
    body = hui.agent_panel_body_rect(area, has_bar)
    state.update(scroll=scroll, max_scroll=metrics["max_offset_from_bottom"])
    if body == hui.RECT_DEFAULT:
        return state
    row_y, body_bottom = body.y, body.y + body.height
    agent_style = {"fg": hui.OVERLAY0, "dim": True}
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
    return state


def _render_collapsed(canvas, spaces, agents, area, hits):
    """sidebar.rs:790-904 render_sidebar_collapsed: a numbered space glance on top, the
    divider, then position-numbered agent marks; "»" expands again."""
    P = hui.PALETTE
    ws_area, divider_y, detail_area = hui.collapsed_sidebar_sections(area)
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
        if divider_y is not None:
            canvas.put(ws_area.x, divider_y, "─" * ws_area.width, fg=P["surface_dim"], clip=clip)
        content = hui.Rect(detail_area.x, detail_area.y, detail_area.width,
                           max(0, detail_area.height - 1))
        if content != hui.RECT_DEFAULT:
            for index, entry in enumerate(agents):
                y = content.y + index
                if y >= content.y + content.height:
                    break
                glyph, color = hui.state_dot(entry["state"], entry["seen"])
                canvas.put(content.x, y, f"{index + 1:<2}", fg=hui.OVERLAY0,
                           clip=content.x + content.width)
                canvas.put(content.x + 2, y, glyph, fg=color, clip=content.x + content.width)
                hits.append((hui.Rect(content.x, y, content.width, 1), ("pane", entry["pane"])))
    toggle = hui.collapsed_sidebar_toggle_rect(area)
    canvas.put(toggle.x, toggle.y, "»", fg=hui.OVERLAY0)
    hits.append((toggle, ("toggle",)))


def format_sidebar(spaces, agents, width, rows, *, collapsed=False, scrolls=None,
                   sort="grouped"):
    """src/ui/sidebar.rs render_sidebar (1011-1035) / render_sidebar_collapsed (790-904) on a
    canvas covering the whole sidebar rect, separator column included.
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
        _render_collapsed(canvas, spaces, _sorted_agents(agents, sort), area, hits)
    else:
        ws_area, detail_area = hui.expanded_sidebar_sections(area, SIDEBAR_SECTION_SPLIT)
        sections["spaces"] = _render_spaces(canvas, spaces, ws_area,
                                            scrolls.get("spaces", 0), hits)
        sections["agents"] = _render_agents(canvas, _sorted_agents(agents, sort), detail_area,
                                            scrolls.get("agents", 0), sort, hits)
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

    def space_of(pane_id):
        return group_spaces(listing)[1].get(pane_id)

    def space_info(key):
        return next((space for space in group_spaces(listing)[0] if space["key"] == key), None)

    def visible_tabs():
        """herdr: the tab bar and the main area show only the active workspace's tabs."""
        return tabs_by_space(tabs, space_of)[0].get(side["ws"], [])

    def active_tab():
        return next((i for i, tree in enumerate(visible_tabs())
                     if focused in hui.pane_ids(tree)), 0)

    def tab_label(tree):
        ids = hui.pane_ids(tree)
        first = next((p for p in listing if p["id"] == ids[0]), None)
        name = (first or {}).get("title") or (ids[0] if ids else "?")
        return name + (f" +{len(ids) - 1}" if len(ids) > 1 else "")


    rows, cols = _term_size()
    # Sidebar state (herdr AppState: sidebar_width / sidebar_collapsed / agent_panel_sort / active).
    side = {"w": SIDEBAR_W, "collapsed": False, "sort": "grouped", "ws": lo["id"]}
    last_focus = {}        # space -> the pane focused last time we were there (herdr: per-workspace focus)
    side_order = []        # space keys in sidebar order at the last draw (neighbour lookup when one closes)

    def switch_space(key):
        """herdr switch_workspace: the main area shows that space's tabs, focus returns to its last pane."""
        side["ws"] = key
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
        sync_tabs()
        alive = {p["id"] for p in listing if p["alive"]}
        if prefer in alive and space_of(prefer) == side["ws"]:
            focus(prefer, force_layout=True)
            return
        vis = visible_tabs()
        if vis:
            focus(hui.pane_ids(vis[min(page_idx, len(vis) - 1)])[0], force_layout=True)
            return
        spaces = [space["key"] for space in group_spaces(listing)[0] if space["alive"]]
        if spaces:
            index = side_order.index(side["ws"]) if side["ws"] in side_order else 0
            switch_space(spaces[min(index, len(spaces) - 1)])

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

    chrome_cache = {"rows": []}       # Only changed rows are written (herdr's row-level diffing).
    chrome_state = {"chromed": [], "area": None, "tracks": []}   # Border and scrollbar geometry.
    side_scrolls = {}              # Scroll offset (in entries) per sidebar section: spaces / agents.
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
    ui_map = {"bar": None, "hits": [], "sections": {}}   # Mouse hit areas: tab bar geometry plus sidebar hit rects.

    def draw_sidebar():
        nonlocal tab_scroll
        sync_tabs()
        groups, tab_of = tabs_by_space(tabs, space_of)
        names = [tab_label(tab) for tab in groups.get(side["ws"], [])]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs))
        # mouse_chrome=True: herdr's "+" new-tab button and overflow scroll buttons.
        bar = hui.compute_tab_bar_view(names, active_tab(), view["tab_bar_rect"],
                                       tab_scroll, tab_follow, True)
        tab_scroll = bar.scroll
        ui_map["bar"] = bar
        tab_line = render_tab_bar(names, active_tab(), bar, view["tab_bar_rect"],
                                  tab_scroll)
        spaces, agents = sidebar_model(listing, focused, side["ws"], tab_of,
                                       {key: len(trees) for key, trees in groups.items()})
        side_order[:] = [space["key"] for space in spaces]
        side_lines, ui_map["hits"], ui_map["sections"] = format_sidebar(
            spaces, agents, side["w"], rows, collapsed=side["collapsed"],
            scrolls=side_scrolls, sort=side["sort"])
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

    def draw_prefix_bar():
        # herdr: entering prefix mode pops a mode bar on the bottom row (menus.rs
        # render_prefix_overlay). It spans only the main area; the sidebar is permanent
        # navigation and must not be covered.
        width = max(10, cols - side["w"])
        paint(f"\x1b[?2026h\x1b[{rows};{main_col()}H\x1b[K"
                   f"{format_prefix_bar(width)}\x1b[?2026l".encode())

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
        paint(f"\x1b[?25l\x1b[?2026h\x1b[{rows};{main_col()}H\x1b[K".encode())
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
                out.append(f"\x1b[{rect.y + row + 1};{rect.x + 1}H{line}")
                buf[row] = line
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
        if sel["pane"] == pane_id:       # New content painted over the highlight; put it back.
            paint_selection()

    def relayout():
        nonlocal slices
        sync_tabs()
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), side["w"], len(tabs))
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
        key = space_of(pane_id)
        if key is not None and key != side["ws"]:   # herdr: focusing a pane in another workspace activates it
            side["ws"] = key
            force_layout = True
        last_focus[side["ws"]] = pane_id
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
    _ACTIVE = object()           # Default parent: the active space's Last Order.

    def new_pane(argv, title, *, split=None, target=_FOCUSED, parent=_ACTIVE):
        """split=None opens a new tab; split="h"/"v" splits ``target`` in the current tab.
        target=_FOCUSED splits the focused pane (herdr split_focused); target=None wraps the
        whole tree (a new column). The pane lands in the active space's folder and, unless
        ``parent`` says otherwise, under its Last Order; parent=None makes it a space root."""
        nonlocal listing
        space = space_info(side["ws"])
        out = control.request("pane.create", {
            "argv": argv, "cwd": space["folder"] if space else os.getcwd(), "title": title,
            "parent": (space["root"] if space else None) if parent is _ACTIVE else parent,
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
        """Mouse wheel: scroll the sidebar section or the pane under the pointer."""
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
        nonlocal tab_scroll, tab_follow, help_open
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
            elif hit[0] == "new":                     # herdr: new workspace in the same folder; here a new Last Order session.
                new_pane([sys.executable, "-m", "misaka", "chat"], LO_TITLE, parent=None)
            elif hit[0] == "menu":                    # herdr: the global menu; the key help is the nearest thing here.
                help_open = True
                draw_help_overlay()
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
        load_layout()          # Pick up the previous split layout (stored in the daemon).
        focus(focused, force_layout=True)
        last_poll = 0.0
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
                        page = active_tab()
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
                try:
                    listing = panes()
                except (RuntimeError, ConnectionError):
                    return
                dead = [p for p in listing if not p["alive"] and not p["card"]]
                if dead:      # Exit events can precede the subscription and get missed; the poll cleans up.
                    page = active_tab()
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
                        refocus(page_idx=page)
                    else:
                        relayout()
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
