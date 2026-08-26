"""Function-by-function port of the herdr client layout (Rust to Python, against v0.8.0).

Each function cites its source as ``file:line``. Semantics are copied as-is:
``Rect`` is ``ratatui::layout::Rect`` (x/y zero-based, width/height) and u16
saturating arithmetic becomes ``max(0, ...)``. Nothing here is invented; to
change a layout rule, change the cited source first.
"""
import itertools
import math
import os
import unicodedata
from collections import namedtuple

Rect = namedtuple("Rect", "x y width height")
RECT_DEFAULT = Rect(0, 0, 0, 0)

# Palette: shares the engine's built-in theme (MISAKA dark/light variants) and
# adapts to the terminal background. herdr's Catppuccin colors are not used; only
# its role names (accent/overlay/surface...) are borrowed. variant=None follows
# terminal background detection.
def _query_terminal_background(in_fd=0, out_fd=1, timeout=0.25):
    """Ask the real terminal for its background color (OSC 11) and return "light"/"dark",
    or None when there is no answer. The engine does this inside its TUI loop; the panel
    needs the answer before it starts, synchronously. Must only run while the tty is in
    normal mode -- once the panel is in raw mode the reply would race the input loop."""
    import re as _re
    import select
    import termios
    import time
    import tty
    if not (os.isatty(in_fd) and os.isatty(out_fd)):
        return None
    try:
        old = termios.tcgetattr(in_fd)
    except termios.error:
        return None
    buf = b""
    try:
        tty.setcbreak(in_fd)              # no echo, no line buffering for the reply
        os.write(out_fd, b"\x1b]11;?\x07")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([in_fd], [], [], deadline - time.monotonic())
            if not readable:
                break
            buf += os.read(in_fd, 256)
            if b"\x07" in buf or b"\x1b\\" in buf:
                break
    except OSError:
        return None
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, old)
    match = _re.search(rb"\x1b\]11;[^\x07\x1b]*(?:\x07|\x1b\\)", buf)
    if not match:
        return None
    from misaka.ui.tui.terminal_colors import (
        parse_osc11_background_color,
        theme_for_rgb_color,
    )
    rgb = parse_osc11_background_color(match.group(0).decode("ascii", "replace"))
    return theme_for_rgb_color(rgb) if rgb else None


_VARIANT = None


def theme_variant():
    """Which variant (dark/light) to use. MISAKA_THEME wins (the panel pins it for panes);
    then a live OSC-11 query of the terminal (Terminal.app sets no COLORFGBG, so the
    env-only detection always guessed dark on a white window); then the engine's env
    detection. Memoized: the panel asks once at import, before raw mode."""
    global _VARIANT
    pinned = os.environ.get("MISAKA_THEME")
    if pinned in ("dark", "light"):
        return pinned
    if _VARIANT is None:
        try:
            live = _query_terminal_background()
        except Exception:  # noqa: BLE001 - a broken tty must not stop the panel
            live = None
        if live is None:
            try:
                from misaka.ui.tui.interactive.theme.theme import get_default_theme
                live = get_default_theme()
            except Exception:  # noqa: BLE001 - if detection fails, assume dark (MISAKA's primary palette)
                live = "dark"
        _VARIANT = live
    return _VARIANT


def _load_palette(variant=None):
    import json
    variant = variant or theme_variant()
    from misaka.config import get_themes_dir
    theme_dir = get_themes_dir()
    path = os.path.join(theme_dir, f"{variant}.json")
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)["vars"]
    except (OSError, ValueError, KeyError):
        v = {}

    def rgb(name, fallback):
        value = v.get(name, fallback).lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    # The chrome (sidebar, tab bar, popups, borders) takes herdr's *roles* but every colour
    # comes from the chat theme itself, so the greys share the rose hue of the text: the dim
    # labels are the theme's own dimGray / gray, and the backgrounds sit on the straight line
    # between black (white in the light theme) and the theme's text colour. Nothing foreign.
    text = rgb("text", "#e2d6da")
    gray, dim_gray = rgb("gray", "#9b8288"), rgb("dimGray", "#6e5c62")
    anchor = (255, 255, 255) if variant == "light" else (0, 0, 0)

    def tone(level):                       # level 0 = anchor, 1 = the theme's grey: backgrounds keep the rose tint
        return blend(anchor, gray, level)

    # The quiet label colour is the theme's `gray` itself -- the same colour the chat UI uses
    # for its muted hints -- so both halves of the screen agree; the other text roles sit
    # between it and the text colour. Lines take the theme's dimGray.
    return {
        "accent": rgb("accent", "#cb3862"),        # focus highlight, borders, selection: Misaka rose
        "overlay0": gray,                          # quiet labels: headers, footer, agent names
        "overlay1": blend(gray, text, 0.3),        # inactive tab text, scrollbar thumb
        "subtext0": blend(gray, text, 0.7),        # inactive sidebar names
        "text": text,
        "border": dim_gray,                        # unfocused pane borders
        "surface_dim": tone(0.30),                 # active row background
        "surface0": tone(0.40),                    # inactive tab / selected row background
        "surface1": tone(0.50),
        "panel_bg": tone(0.13),                    # tab bar and popup background
        "dim_gray": dim_gray,                      # the theme's own dim grey, kept for callers
        "green": rgb("green", "#7fa87f"),
        "yellow": rgb("yellow", "#e8c15a"),
        "red": rgb("red", "#e05252"),
        "teal": rgb("plum", "#a8557a"),            # finished, nobody looked yet (herdr: teal)
        "plum": rgb("plum", "#a8557a"),
        "rose_lite": rgb("roseLite", "#e8698c"),
        "rose_deep": rgb("roseDeep", "#8f2545"),
        "amber": rgb("amber", "#d99a4e"),
    }


def blend(color_a, color_b, level):
    """Linear interpolation between two colors (level 0 = a, 1 = b); used for breathing effects."""
    level = min(1.0, max(0.0, level))
    return tuple(round(a + (b - a) * level) for a, b in zip(color_a, color_b))


# Resolved once at import from the terminal background; a panel process never switches variants.
PALETTE = _load_palette()
ACCENT = PALETTE["accent"]
OVERLAY0 = PALETTE["overlay0"]
OVERLAY1 = PALETTE["overlay1"]
SURFACE0 = PALETTE["surface0"]
TEXT = PALETTE["text"]
PANEL_BG = PALETTE["panel_bg"]


def sgr_fg(rgb):
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def sgr_bg(rgb):
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def panel_contrast_fg(bg=None):
    """src/ui/widgets.rs panel_contrast_fg: a readable foreground for a colored background.
    Picks the lighter of the theme's text/panel tones for a dark badge and the darker for a
    light one -- returning TEXT outright painted dark-on-rose in the light theme (its TEXT
    is dark; badge text must stay light on the accent in both variants)."""
    bg = ACCENT if bg is None else bg

    def luminance(color):
        return (0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]) / 255

    light_tone, dark_tone = sorted((TEXT, PANEL_BG), key=luminance, reverse=True)
    return dark_tone if luminance(bg) > 0.5 else light_tone

# src/ui/tabs.rs:12-15
MIN_TAB_WIDTH = 8
NEW_TAB_WIDTH = 3
TAB_SCROLL_BUTTON_WIDTH = 3
# src/ui/tabs.rs:16-19
TabBarView = namedtuple(
    "TabBarView",
    "scroll tab_hit_areas scroll_left_hit_area scroll_right_hit_area new_tab_hit_area")


def display_width(text):
    """ui/text.rs display_width_u16: wide characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


# ── src/ui.rs ─────────────────────────────────────────────────────────

def desktop_tab_bar_and_terminal_area(main_area, tab_count,
                                      hide_tab_bar_when_single_tab=False):
    """src/ui.rs:191-210; tab_bar_position defaults to Top (config/model.rs:1111)."""
    hide_single = hide_tab_bar_when_single_tab and tab_count == 1
    if not hide_single and main_area.height > 1:
        tab_bar = Rect(main_area.x, main_area.y, main_area.width, 1)
        terminal = Rect(main_area.x, main_area.y + 1,
                        main_area.width, main_area.height - 1)
        return tab_bar, terminal
    return RECT_DEFAULT, main_area


def compute_view(area, sidebar_width, tab_count,
                 hide_tab_bar_when_single_tab=False):
    """src/ui.rs:215-244 (compute_view_internal, desktop path):
    split horizontally into [sidebar, main], then take the tab row off the top of main."""
    sidebar_w = min(sidebar_width, area.width)
    sidebar_area = Rect(area.x, area.y, sidebar_w, area.height)
    main_area = Rect(area.x + sidebar_w, area.y,
                     max(1, area.width - sidebar_w), area.height)
    tab_bar_rect, terminal_area = desktop_tab_bar_and_terminal_area(
        main_area, tab_count, hide_tab_bar_when_single_tab)
    return {"sidebar_rect": sidebar_area, "main_area": main_area,
            "tab_bar_rect": tab_bar_rect, "terminal_area": terminal_area}


# ── src/ui/sidebar.rs ─────────────────────────────────────────────────

WORKSPACE_SECTION_HEADER_ROWS = 2     # src/ui/sidebar.rs:20
AGENT_PANEL_HEADER_ROWS = 3           # src/ui/sidebar.rs:21


def allocate_sections(total_h, specs):
    """MISAKA addition (herdr has exactly two sections, sidebar.rs:42-57): stack any number of
    sidebar sections. ``specs`` = [(key, weight)]; they share the height by weight, at least 3
    rows each while the height allows, the last one absorbing the rounding. Returns
    [(key, y, height)] in order; a section that does not fit gets height 0."""
    out, y, given = [], 0, 0
    total_w = sum(w for _k, w in specs) or 1
    for index, (key, weight) in enumerate(specs):
        if index == len(specs) - 1:
            height = max(0, total_h - given)
        else:
            height = max(min(3, total_h - given), round(total_h * weight / total_w))
        given += height
        height = max(0, min(height, total_h - y))
        out.append((key, y, height))
        y += height
    return out


def collapsed_sidebar_sections(area, weights):
    """src/ui/sidebar.rs:765-787 generalized to MISAKA's section list (herdr stacks exactly
    spaces + agents; the expanded sidebar grew a sessions section, so the collapsed glance
    mirrors the same ``weights``): one compact section per entry with a divider row between
    neighbours; under 7 rows the whole content is the first (spaces) list.
    Returns ([(key, Rect)], [divider_y])."""
    content = Rect(area.x, area.y, max(0, area.width - 1), area.height)
    if content.width == 0 or content.height == 0:
        return [], []
    if content.height < 7:
        return [(weights[0][0], content)], []
    sections, dividers = [], []
    inner = content.height - (len(weights) - 1)         # divider rows come off the top
    for index, (key, sec_y, height) in enumerate(allocate_sections(inner, weights)):
        y = content.y + sec_y + index                    # + one divider per earlier section
        if index > 0 and height > 0:
            dividers.append(y - 1)
        sections.append((key, Rect(content.x, y, content.width, height)))
    return sections, dividers


def workspace_list_body_rect(area, has_scrollbar):
    """src/ui/sidebar.rs:430-441: the header rows come off the top, the footer row off the bottom."""
    if area.width == 0 or area.height <= WORKSPACE_SECTION_HEADER_ROWS:
        return RECT_DEFAULT
    body_y = area.y + WORKSPACE_SECTION_HEADER_ROWS
    footer_y = area.y + max(0, area.height - 1)
    return Rect(area.x, body_y, max(0, area.width - int(has_scrollbar)),
                max(0, footer_y - body_y))


def agent_panel_body_rect(area, has_scrollbar):
    """src/ui/sidebar.rs:543-553."""
    if area.width == 0 or area.height <= AGENT_PANEL_HEADER_ROWS:
        return RECT_DEFAULT
    body_y = area.y + AGENT_PANEL_HEADER_ROWS
    return Rect(area.x, body_y, max(0, area.width - int(has_scrollbar)),
                max(0, area.y + area.height - body_y))


def sidebar_footer_rect(ws_area):
    """src/app/input/sidebar.rs:168-175: the last row of the spaces section."""
    if ws_area == RECT_DEFAULT:
        return RECT_DEFAULT
    return Rect(ws_area.x, ws_area.y + max(0, ws_area.height - 1), ws_area.width, 1)


def sidebar_new_button_rect(ws_area):
    """src/app/input/sidebar.rs:177-181: " new", five columns at the left of the footer."""
    footer = sidebar_footer_rect(ws_area)
    return Rect(footer.x, footer.y, min(5, max(footer.width, 1)), footer.height)


def global_launcher_rect(ws_area, label="menu"):
    """src/app/input/sidebar.rs:183-197: the launcher label right-aligned on the footer, two
    columns wider than its text ("menu" = 6, "● menu" = 8)."""
    footer = sidebar_footer_rect(ws_area)
    width = min(display_width(label) + 2, max(footer.width, 1))
    return Rect(footer.x + max(0, footer.width - width), footer.y, width, footer.height)


def menu_popup_rect(screen, launcher, labels):
    """src/app/input/sidebar.rs:210-232 global_menu_rect: width = longest label + 4 (padding and
    border), left edge on the launcher's left edge (clamped to the screen), bottom edge on the
    row above it. MISAKA addition: the height is capped at the rows above the launcher, so a
    long list scrolls instead of spilling over the footer."""
    content = max([display_width(label) for label in labels] or [8]) + 2
    menu_w = min(content + 2, max(screen.width, 1))
    menu_h = min(len(labels) + 2, max(screen.height, 1), max(launcher.y - screen.y, 2))
    max_x = screen.x + max(0, screen.width - menu_w)
    x = min(launcher.x + max(0, launcher.width - menu_w), max_x)
    return Rect(x, max(0, launcher.y - menu_h), menu_w, menu_h)


def agent_panel_header_label_rect(area, label):
    """src/ui/sidebar.rs:99-111: the sort toggle, right-aligned on the second header row."""
    if area.width == 0 or area.height < 2:
        return RECT_DEFAULT
    width = min(display_width(label), area.width)
    return Rect(area.x + max(0, area.width - width), area.y + 1, width, 1)


def expanded_sidebar_toggle_rect(area):
    """src/ui/sidebar.rs:1520-1529: "«" one column left of the separator, on the bottom row."""
    if area.width <= 1 or area.height == 0:
        return RECT_DEFAULT
    return Rect(area.x + area.width - 2, area.y + area.height - 1, 1, 1)


def collapsed_sidebar_toggle_rect(area):
    """src/ui/sidebar.rs:1510-1518: "»" in the middle of the collapsed content, bottom row."""
    content_w = max(0, area.width - 1)
    if content_w == 0 or area.height == 0:
        return RECT_DEFAULT
    return Rect(area.x + content_w // 2, area.y + area.height - 1, 1, 1)


def list_bottom_start(heights, body_h):
    """src/ui/sidebar.rs:468-487 / 590-606: the first entry from which everything below still
    fits the body (row_gap is 0, the default)."""
    used, start = 0, len(heights)
    for idx in range(len(heights) - 1, -1, -1):
        needed = min(heights[idx], body_h)
        if used + needed > body_h:
            break
        used += needed
        start = idx
    return min(start, max(0, len(heights) - 1))


def list_visible_count(heights, body_h, scroll):
    """src/ui/sidebar.rs:443-466 / 565-588: entries that fit from ``scroll`` downward."""
    used, visible = 0, 0
    for height in heights[scroll:]:
        height = min(height, body_h)
        if used + height > body_h:
            break
        used += height
        visible += 1
    return visible


def list_scroll_metrics(heights, body_h, scroll):
    """src/ui/sidebar.rs:489-501 / 608-620: scrollbar metrics for an entry list."""
    max_scroll = list_bottom_start(heights, body_h) if heights else 0
    scroll = min(scroll, max_scroll)
    return {"offset_from_bottom": max_scroll - scroll,
            "max_offset_from_bottom": max_scroll,
            "viewport_rows": list_visible_count(heights, body_h, scroll)}


def fit_tokens(tokens, max_width):
    """src/ui/sidebar.rs:818-936 resolved_token_spans, the width fitting: every text token keeps
    at least one column; if even that overflows, text tokens are dropped from the left and
    re-admitted from the right while they fit; leftover width then grows them round-robin.
    ``tokens`` = [(kind, text)] with kind "icon" (fixed width), "text", or "git" (a git-status
    count). Separator: one space after an icon or before a git count, " · " otherwise
    (sidebar/tokens.rs:144-152). Returns [(index, separator, shown)]."""
    widths = [display_width(text) for _kind, text in tokens]
    fixed = [w if kind == "icon" else 0 for (kind, _t), w in zip(tokens, widths)]
    flex = [0 if kind == "icon" else w for (kind, _t), w in zip(tokens, widths)]

    def sep(prev, cur):
        return " " if tokens[prev][0] == "icon" or tokens[cur][0] == "git" else " · "

    def minimum(active):
        idx = [i for i, on in enumerate(active) if on]
        return (sum(fixed[i] + (1 if flex[i] > 0 else 0) for i in idx)
                + sum(display_width(sep(a, b)) for a, b in itertools.pairwise(idx)))

    active = [True] * len(tokens)
    if minimum(active) > max_width:
        for i, width in enumerate(flex):
            if width > 0:
                active[i] = False
        for i in range(len(tokens) - 1, -1, -1):
            if flex[i] == 0:
                continue
            active[i] = True
            if minimum(active) > max_width:
                active[i] = False
    idx = [i for i, on in enumerate(active) if on]
    sep_w = sum(display_width(sep(a, b)) for a, b in itertools.pairwise(idx))
    fixed_w = sum(fixed[i] for i in idx)
    budgets = [1 if (active[i] and flex[i] > 0) else 0 for i in range(len(tokens))]
    remaining = max(0, max(0, max_width - (sep_w + fixed_w)) - sum(budgets))
    while remaining > 0:
        grew = False
        for i in range(len(tokens)):
            if 0 < budgets[i] < flex[i]:
                budgets[i] += 1
                remaining -= 1
                grew = True
                if remaining == 0:
                    break
        if not grew:
            break
    out = []
    for pos, i in enumerate(idx):
        kind, text = tokens[i]
        out.append((i, sep(idx[pos - 1], i) if pos else "",
                    text if kind == "icon" else truncate_end(text, budgets[i])))
    return out


# ── src/ui/text.rs ────────────────────────────────────────────────────

def truncate_end(text, max_width):
    """src/ui/text.rs:12-25: cut to a display width, ending in an ellipsis."""
    if display_width(text) <= max_width:
        return text
    if max_width == 0:
        return ""
    if max_width == 1:
        return "…"
    out, used = "", 0
    for ch in text:
        width = display_width(ch)
        if used + width > max_width - 1:
            break
        out += ch
        used += width
    return out + "…"


# ── src/ui/status.rs ──────────────────────────────────────────────────

def state_dot(state, seen):
    """src/ui/status.rs:196-204 (colors double as state_label_color, 216-224):
    ``state`` is herdr's AgentState name, ``seen`` its pane.seen flag."""
    if state == "blocked":
        return "●", PALETTE["red"]
    if state == "working":
        return "●", PALETTE["yellow"]
    if state == "idle" and not seen:
        return "●", PALETTE["teal"]
    if state == "idle":
        return "○", PALETTE["green"]
    return "·", OVERLAY0


def state_label(state, seen):
    """src/ui/status.rs:206-214."""
    if state in ("blocked", "working"):
        return state
    return "done" if (state == "idle" and not seen) else "idle"


def attention_priority(state, seen):
    """src/workspace/aggregate.rs:75-83: Blocked > done (idle, unseen) > Working > Idle > Unknown."""
    if state == "blocked":
        return 4
    if state == "working":
        return 2
    if state == "idle":
        return 1 if seen else 3
    return 0


# ── src/ui/tabs.rs ────────────────────────────────────────────────────

def tab_chrome_label(tab_names, tab_idx, zoomed=()):
    """src/ui/tabs.rs:36-45: display name or index+1; zoomed tabs get a " Z" suffix."""
    name = tab_names[tab_idx] if tab_names[tab_idx] else str(tab_idx + 1)
    return f"{name} Z" if tab_idx in zoomed else name


def tab_width(tab_names, tab_idx, zoomed=()):
    """src/ui/tabs.rs:30-34: tab width = label width + 4, at least MIN_TAB_WIDTH."""
    return max(display_width(tab_chrome_label(tab_names, tab_idx, zoomed)) + 4,
               MIN_TAB_WIDTH)


def layout_tab_hit_areas(tab_names, area, scroll, zoomed=()):
    """src/ui/tabs.rs:107-127: lay tabs out from scroll with a 1-column gap; tabs that do not fit get width 0."""
    rects = [RECT_DEFAULT] * len(tab_names)
    if area.width == 0 or area.height == 0:
        return rects
    x = area.x
    right = area.x + area.width
    for idx in range(scroll, len(tab_names)):
        if x >= right:
            break
        desired = tab_width(tab_names, idx, zoomed)
        width = max(1, min(desired, right - x))
        rects[idx] = Rect(x, area.y, width, 1)
        x += width + 1
    return rects


def centered_tab_scroll(tab_names, active_tab, area, zoomed=()):
    """src/ui/tabs.rs:129-158: center the active tab as well as possible."""
    best_scroll, best_distance = active_tab, float("inf")
    viewport_center = area.x * 2 + area.width
    for scroll in range(active_tab + 1):
        rects = layout_tab_hit_areas(tab_names, area, scroll, zoomed)
        active_rect = rects[active_tab] if active_tab < len(rects) else None
        if not active_rect or active_rect.width == 0:
            continue
        active_center = active_rect.x * 2 + active_rect.width
        distance = abs(active_center - viewport_center)
        if distance <= best_distance:
            best_distance, best_scroll = distance, scroll
    return best_scroll


def trailing_tab_controls_x(tab_hit_areas, fallback_x):
    """src/ui/tabs.rs:160-166."""
    for rect in reversed(tab_hit_areas):
        if rect.width > 0:
            return rect.x + rect.width
    return fallback_x


def max_tab_scroll(tab_names, area, zoomed=()):
    """src/ui/tabs.rs:168-177: smallest scroll at which the last tab is visible."""
    for scroll in range(len(tab_names)):
        rects = layout_tab_hit_areas(tab_names, area, scroll, zoomed)
        if rects and rects[-1].width > 0:
            return scroll
    return 0


def compute_tab_bar_view(tab_names, active_tab, area, current_scroll,
                         follow_active, mouse_chrome, zoomed=()):
    """src/ui/tabs.rs:179-268: if everything fits, all tabs plus the new-tab button;
    otherwise scroll buttons on both sides of the tab area."""
    if area.width == 0 or area.height == 0:
        return TabBarView(0, [RECT_DEFAULT] * len(tab_names),
                          RECT_DEFAULT, RECT_DEFAULT, RECT_DEFAULT)
    if not mouse_chrome:
        cap = max_tab_scroll(tab_names, area, zoomed)
        scroll = (min(centered_tab_scroll(tab_names, active_tab, area, zoomed), cap)
                  if follow_active else min(current_scroll, cap))
        return TabBarView(scroll, layout_tab_hit_areas(tab_names, area, scroll, zoomed),
                          RECT_DEFAULT, RECT_DEFAULT, RECT_DEFAULT)

    area_right = area.x + area.width
    all_tabs_area = Rect(area.x, area.y, max(0, area.width - NEW_TAB_WIDTH), area.height)
    all_tabs = layout_tab_hit_areas(tab_names, all_tabs_area, 0, zoomed)
    overflow = any(rect.width == 0 for rect in all_tabs)
    if not overflow:
        new_tab_x = trailing_tab_controls_x(all_tabs, area.x)
        new_tab = Rect(new_tab_x, area.y,
                       min(max(0, area_right - new_tab_x), NEW_TAB_WIDTH), 1)
        return TabBarView(0, all_tabs, RECT_DEFAULT, RECT_DEFAULT, new_tab)

    left = Rect(area.x, area.y, min(TAB_SCROLL_BUTTON_WIDTH, area.width), 1)
    tab_area_x = left.x + left.width
    tab_area_right = max(0, area_right - (NEW_TAB_WIDTH + TAB_SCROLL_BUTTON_WIDTH))
    tab_area = Rect(tab_area_x, area.y, max(0, tab_area_right - tab_area_x), area.height)
    cap = max_tab_scroll(tab_names, tab_area, zoomed)
    scroll = (min(centered_tab_scroll(tab_names, active_tab, tab_area, zoomed), cap)
              if follow_active else min(current_scroll, cap))
    tab_hits = layout_tab_hit_areas(tab_names, tab_area, scroll, zoomed)
    trailing_x = min(trailing_tab_controls_x(tab_hits, tab_area_x), tab_area_right)
    right = Rect(trailing_x, area.y,
                 min(max(0, area_right - trailing_x), TAB_SCROLL_BUTTON_WIDTH), 1)
    new_tab_x = right.x + right.width
    new_tab = Rect(new_tab_x, area.y,
                   min(max(0, area_right - new_tab_x), NEW_TAB_WIDTH), 1)
    return TabBarView(scroll, tab_hits, left, right, new_tab)


# ── src/layout.rs: BSP split tree ────────────────────────────────────
# Nodes are tuples mirroring the enum Node at layout.rs:72:
#   ("pane", pane_id)                         leaf
#   ("split", "h"|"v", ratio, first, second)  inner node (h = side by side, v = stacked)


def split_at(node, target, direction, new_id, ratio):
    """src/layout.rs:590-617: replace the target leaf with a split of (original pane, new pane)."""
    kind = node[0]
    if kind == "pane":
        if node[1] == target:
            return ("split", direction, valid_split_ratio(ratio),
                    ("pane", node[1]), ("pane", new_id))
        return node
    _, d, r, first, second = node
    return ("split", d, r,
            split_at(first, target, direction, new_id, ratio),
            split_at(second, target, direction, new_id, ratio))


def valid_split_ratio(ratio):
    """src/layout.rs:619-625."""
    if not math.isfinite(ratio):                                   # NaN/inf
        return 0.5
    return min(0.9, max(0.1, ratio))


def remove_pane(node, target):
    """src/layout.rs:627-655: delete a leaf; a split left with one child is replaced by that child."""
    kind = node[0]
    if kind == "pane":
        return None if node[1] == target else node
    _, d, r, first, second = node
    f, s = remove_pane(first, target), remove_pane(second, target)
    if f is None and s is not None:
        return s
    if f is not None and s is None:
        return f
    if f is not None and s is not None:
        return ("split", d, r, f, s)
    return None


def split_rect(area, direction, ratio):
    """src/layout.rs:691-710: cut a rectangle in two by ratio (h = side by side, v = stacked)."""
    if direction == "h":
        first_w = round(area.width * ratio)
        second_w = max(0, area.width - first_w)
        return (Rect(area.x, area.y, first_w, area.height),
                Rect(area.x + first_w, area.y, second_w, area.height))
    first_h = round(area.height * ratio)
    second_h = max(0, area.height - first_h)
    return (Rect(area.x, area.y, area.width, first_h),
            Rect(area.x, area.y + first_h, area.width, second_h))


def collect_panes(node, area, focus):
    """src/layout.rs:483-507: recursively compute each leaf's rectangle. Returns [(pane_id, Rect, focused)]."""
    if node[0] == "pane":
        return [(node[1], area, node[1] == focus)]
    _, d, r, first, second = node
    a, b = split_rect(area, d, r)
    return collect_panes(first, a, focus) + collect_panes(second, b, focus)


def to_jsonable(node):
    """Layout tree to JSON-compatible lists (tuple to list); the daemon persists it."""
    if node[0] == "pane":
        return ["pane", node[1]]
    return ["split", node[1], node[2], to_jsonable(node[3]), to_jsonable(node[4])]


def from_jsonable(data):
    """JSON back to a layout tree (list to tuple). Returns None on a malformed shape so the caller can discard it."""
    try:
        if data[0] == "pane":
            return ("pane", data[1])
        if data[0] == "split":
            first, second = from_jsonable(data[3]), from_jsonable(data[4])
            if first is None or second is None:
                return None
            return ("split", data[1], float(data[2]), first, second)
    except (TypeError, IndexError, ValueError):
        pass
    return None


def pane_ids(node):
    """All leaf ids, left-to-right depth-first (same order as collect_panes)."""
    if node[0] == "pane":
        return [node[1]]
    return pane_ids(node[3]) + pane_ids(node[4])


def collect_splits(node, area, path=()):
    """src/layout.rs:509-536: every split boundary, for mouse-drag resizing.
    Returns [{pos, direction, ratio, area, path}]."""
    if node[0] != "split":
        return []
    _, d, r, first, second = node
    a, b = split_rect(area, d, r)
    pos = (a.x + a.width) if d == "h" else (a.y + a.height)
    out = [{"pos": pos, "direction": d, "ratio": r, "area": area, "path": tuple(path)}]
    out += collect_splits(first, a, (*path, False))
    out += collect_splits(second, b, (*path, True))
    return out


def get_ratio_at(node, path):
    """src/layout.rs get_ratio_at: the ratio of the split reached by ``path`` (False = first, True = second)."""
    for step in path:
        if node[0] != "split":
            return None
        node = node[4] if step else node[3]
    return node[2] if node[0] == "split" else None


def set_ratio_at(node, path, ratio):
    """src/layout.rs set_ratio_at: a copy of the tree with that split's ratio replaced (clamped)."""
    if node[0] != "split":
        return node
    if not path:
        return ("split", node[1], valid_split_ratio(ratio), node[3], node[4])
    step, rest = path[0], path[1:]
    first, second = node[3], node[4]
    if step:
        second = set_ratio_at(second, rest, ratio)
    else:
        first = set_ratio_at(first, rest, ratio)
    return ("split", node[1], node[2], first, second)


def _split_edge_distance(split, focused, nav):
    """src/layout.rs:379-390."""
    if nav == "left":
        return abs(split["pos"] - focused.x)
    if nav == "right":
        return abs(split["pos"] - (focused.x + focused.width))
    if nav == "up":
        return abs(split["pos"] - focused.y)
    return abs(split["pos"] - (focused.y + focused.height))


def _nearest_resize_split(splits, target_dir, focused, nav):
    """src/layout.rs:341-368: the closest split of the right orientation that touches the
    focused pane's requested edge (within one cell) and overlaps it on the other axis."""
    area_overlaps = (lambda s: _ranges_overlap(s["area"].y, s["area"].height, focused.y, focused.height)
                     if nav in ("left", "right")
                     else _ranges_overlap(s["area"].x, s["area"].width, focused.x, focused.width))
    candidates = [s for s in splits
                  if s["direction"] == target_dir and area_overlaps(s)
                  and _split_edge_distance(s, focused, nav) <= 1]
    return min(candidates, key=lambda s: _split_edge_distance(s, focused, nav), default=None)


def resize_focused(node, focused_id, nav, delta, area):
    """src/layout.rs:215-239 resize_focused: grow/shrink the focused pane by moving the nearest
    split on its requested edge (falling back to the opposite edge); right/down grow the
    ratio, left/up shrink it. Returns the new tree (unchanged when nothing applies)."""
    focused = next((rect for pid, rect, _f in collect_panes(node, area, focused_id) if pid == focused_id), None)
    if focused is None:
        return node
    splits = collect_splits(node, area)
    target_dir = "h" if nav in ("left", "right") else "v"
    opposite = {"left": "right", "right": "left", "up": "down", "down": "up"}[nav]
    best = (_nearest_resize_split(splits, target_dir, focused, nav)
            or _nearest_resize_split(splits, target_dir, focused, opposite))
    if best is None:
        return node
    grows = nav in ("right", "down")
    current = get_ratio_at(node, best["path"]) or 0.5
    return set_ratio_at(node, best["path"], current + (delta if grows else -delta))


def divider_at(splits, x, y):
    """The split whose divider line holds the cell (x, y), for mouse-drag resizing (app/input/mouse.rs)."""
    for split in splits:
        area = split["area"]
        if split["direction"] == "h":
            if x == split["pos"] and area.y <= y < area.y + area.height:
                return split
        elif y == split["pos"] and area.x <= x < area.x + area.width:
            return split
    return None


def drag_ratio(split, x, y):
    """Ratio for a divider dragged to (x, y) inside its split's area (mouse.rs drag state machine)."""
    area = split["area"]
    if split["direction"] == "h":
        return valid_split_ratio((x - area.x) / max(1, area.width))
    return valid_split_ratio((y - area.y) / max(1, area.height))


def _range_overlap_amount(a_start, a_len, b_start, b_len):
    """src/layout.rs:462-466."""
    return max(0, min(a_start + a_len, b_start + b_len) - max(a_start, b_start))


def _range_center_distance(a_start, a_len, b_start, b_len):
    """src/layout.rs:468-472: distance between centers (doubled to stay in integers)."""
    return abs((a_start * 2 + a_len) - (b_start * 2 + b_len))


def find_in_direction(focused_id, direction, panes):
    """src/layout.rs:350-405: nearest pane in the given direction.
    panes=[(id, Rect)]; direction is left/right/up/down.
    Sort key is (edge gap, -overlap, center distance, original order), matching Rust's min_by_key."""
    focused = next((r for pid, r in panes if pid == focused_id), None)
    if focused is None:
        return None
    fr = focused
    candidates = []
    for index, (pid, r) in enumerate(panes):
        if pid == focused_id:
            continue
        if direction == "left":
            ok = (r.x + r.width <= fr.x
                  and _ranges_overlap(r.y, r.height, fr.y, fr.height))
            edge = max(0, fr.x - (r.x + r.width))
        elif direction == "right":
            ok = (r.x >= fr.x + fr.width
                  and _ranges_overlap(r.y, r.height, fr.y, fr.height))
            edge = max(0, r.x - (fr.x + fr.width))
        elif direction == "up":
            ok = (r.y + r.height <= fr.y
                  and _ranges_overlap(r.x, r.width, fr.x, fr.width))
            edge = max(0, fr.y - (r.y + r.height))
        else:                                    # down
            ok = (r.y >= fr.y + fr.height
                  and _ranges_overlap(r.x, r.width, fr.x, fr.width))
            edge = max(0, r.y - (fr.y + fr.height))
        if not ok:
            continue
        if direction in ("left", "right"):
            overlap = _range_overlap_amount(r.y, r.height, fr.y, fr.height)
            center = _range_center_distance(r.y, r.height, fr.y, fr.height)
        else:
            overlap = _range_overlap_amount(r.x, r.width, fr.x, fr.width)
            center = _range_center_distance(r.x, r.width, fr.x, fr.width)
        candidates.append(((edge, -overlap, center, index), pid))
    if not candidates:
        return None
    return min(candidates)[1]


# ── src/ui/panes.rs: pane borders (herdr draws a full frame around every pane, not split lines) ──

def _ranges_overlap(a_start, a_len, b_start, b_len):
    """src/ui/panes.rs:57-59."""
    return a_start < b_start + b_len and b_start < a_start + a_len


def _pane_to_right(info, panes):
    """src/ui/panes.rs:61-73."""
    right = info["rect"].x + info["rect"].width
    for other in panes:
        if (other["id"] != info["id"] and other["rect"].x == right
                and _ranges_overlap(info["rect"].y, info["rect"].height,
                                    other["rect"].y, other["rect"].height)):
            return other
    return None


def _pane_below(info, panes):
    """src/ui/panes.rs:75-82."""
    bottom = info["rect"].y + info["rect"].height
    for other in panes:
        if (other["id"] != info["id"] and other["rect"].y == bottom
                and _ranges_overlap(info["rect"].x, info["rect"].width,
                                    other["rect"].x, other["rect"].width)):
            return other
    return None


def apply_pane_chrome(panes, pane_borders=True, pane_gaps=True,
                      pane_outer_borders=True):
    """src/ui/panes.rs:92-157: decide the border set for each pane.
    panes=[{"id","rect","focused"}]; returns the same structure plus "borders", a set
    drawn from {"top","bottom","left","right"} (the ratatui Borders bits).
    Adjacent panes share an edge: with a right neighbor, drop our own right border
    (no-gap mode); likewise below."""
    multi = len(panes) > 1
    if not panes:
        return []
    outer_left = min(p["rect"].x for p in panes)
    outer_top = min(p["rect"].y for p in panes)
    outer_right = max(p["rect"].x + p["rect"].width for p in panes)
    outer_bottom = max(p["rect"].y + p["rect"].height for p in panes)
    out = []
    for info in panes:
        info = dict(info)
        right_n = _pane_to_right(info, panes) if multi else None
        below_n = _pane_below(info, panes) if multi else None
        if multi and pane_gaps and not pane_borders:
            rect = info["rect"]
            width = rect.width - 1 if right_n and rect.width > 1 else rect.width
            height = rect.height - 1 if below_n and rect.height > 1 else rect.height
            info["rect"] = Rect(rect.x, rect.y, width, height)
        if not multi or not pane_borders:
            info["borders"] = set()
        else:
            borders = {"top", "bottom", "left", "right"}
            if not pane_gaps:
                if right_n:
                    borders.discard("right")
                if below_n:
                    borders.discard("bottom")
            if not pane_outer_borders:
                if info["rect"].x == outer_left:
                    borders.discard("left")
                if info["rect"].y == outer_top:
                    borders.discard("top")
                if info["rect"].x + info["rect"].width == outer_right:
                    borders.discard("right")
                if info["rect"].y + info["rect"].height == outer_bottom:
                    borders.discard("bottom")
            info["borders"] = borders
        out.append(info)
    return out


def line_cell_symbol(up, down, left, right):
    """src/ui/panes.rs:706-725: four line directions to a box-drawing glyph (including T and cross joints)."""
    return {
        (True, True, True, True): "┼",
        (True, True, True, False): "┤",
        (True, True, False, True): "├",
        (True, False, True, True): "┴",
        (False, True, True, True): "┬",
        (True, True, False, False): "│",
        (True, False, False, False): "│",
        (False, True, False, False): "│",
        (False, False, True, True): "─",
        (False, False, True, False): "─",
        (False, False, False, True): "─",
        (False, True, False, True): "┌",
        (False, True, True, False): "┐",
        (True, False, False, True): "└",
        (True, False, True, False): "┘",
    }.get((up, down, left, right), "")


def line_touches_pane(x, y, rect, pane_gaps=True):
    """src/ui/panes.rs:629-650: whether the cell lies on this pane's border (shared edges included in no-gap mode)."""
    if rect.width == 0 or rect.height == 0:
        return False
    right = rect.x + rect.width - 1
    bottom = rect.y + rect.height - 1
    in_rows = rect.y <= y <= bottom
    in_cols = rect.x <= x <= right
    own = (in_rows and x in (rect.x, right)) or (in_cols and y in (rect.y, bottom))
    if pane_gaps:
        return own
    return (own
            or (in_rows and x == rect.x + rect.width)
            or (in_cols and y == rect.y + rect.height))


def _new_line_cell():
    return {"up": False, "down": False, "left": False, "right": False}


def add_pane_border_cells(cells, item):
    """src/ui/panes.rs:588-627, line by line. item={"rect","borders"}."""
    rect, borders = item["rect"], item["borders"]
    if rect.width == 0 or rect.height == 0:
        return
    right = rect.x + rect.width - 1
    bottom = rect.y + rect.height - 1
    if "top" in borders:
        for x in range(rect.x, right + 1):
            cell = cells.setdefault((x, rect.y), _new_line_cell())
            cell["left"] |= x > rect.x
            cell["right"] |= x < right
    if "bottom" in borders:
        for x in range(rect.x, right + 1):
            cell = cells.setdefault((x, bottom), _new_line_cell())
            cell["left"] |= x > rect.x
            cell["right"] |= x < right
    if "left" in borders:
        for y in range(rect.y, bottom + 1):
            cell = cells.setdefault((rect.x, y), _new_line_cell())
            cell["up"] |= y > rect.y
            cell["down"] |= y < bottom
    if "right" in borders:
        for y in range(rect.y, bottom + 1):
            cell = cells.setdefault((right, y), _new_line_cell())
            cell["up"] |= y > rect.y
            cell["down"] |= y < bottom


def add_split_border_cells(pane_gaps, splits, cells):
    """src/ui/panes.rs:531-587, line by line: join split lines onto existing cells (only extends, never creates)."""
    if pane_gaps:
        return
    for split in splits:
        area, pos = split["area"], split["pos"]
        if split["direction"] == "h":
            end = area.y + area.height
            for y in range(area.y, end + 1):
                if (pos, y) not in cells:
                    continue
                left_cell = cells.get((pos - 1, y)) if pos >= 1 else None
                right_cell = cells.get((pos + 1, y))
                left = bool(left_cell and (left_cell["left"] or left_cell["right"]))
                right = bool(right_cell and (right_cell["left"] or right_cell["right"]))
                cell = cells.setdefault((pos, y), _new_line_cell())
                cell["up"] |= y > area.y
                cell["down"] |= y + 1 < end
                cell["left"] |= left
                cell["right"] |= right
        else:
            end = area.x + area.width
            for x in range(area.x, end + 1):
                if (x, pos) not in cells:
                    continue
                up_cell = cells.get((x, pos - 1)) if pos >= 1 else None
                down_cell = cells.get((x, pos + 1))
                up = bool(up_cell and (up_cell["up"] or up_cell["down"]))
                down = bool(down_cell and (down_cell["up"] or down_cell["down"]))
                cell = cells.setdefault((x, pos), _new_line_cell())
                cell["left"] |= x > area.x
                cell["right"] |= x + 1 < end
                cell["up"] |= up
                cell["down"] |= down


def pane_border_cells(chromed, pane_gaps=True, splits=(), area=None):
    """src/ui/panes.rs:484-528 render_pane_borders, geometry only.
    Returns {(x, y): (glyph, focused)}: collect every cell first, then pick glyphs and highlight.
    When ``area`` is given, clip to it (the original's buf.area bounds check). Pure, so testable."""
    if all(not item["borders"] for item in chromed):
        return {}
    cells = {}
    for item in chromed:
        add_pane_border_cells(cells, item)
    add_split_border_cells(pane_gaps, splits, cells)

    out = {}
    for (x, y), line in cells.items():
        if area is not None and not (area.x <= x < area.x + area.width
                                     and area.y <= y < area.y + area.height):
            continue
        symbol = line_cell_symbol(line["up"], line["down"], line["left"], line["right"])
        if not symbol:
            continue
        focused = any(item["focused"] and line_touches_pane(x, y, item["rect"], pane_gaps)
                      for item in chromed)
        out[(x, y)] = (symbol, focused)
    return out


# ── Scrollbar (src/ui/scrollbar.rs + src/ui/panes.rs:36-196) ───────────────
# ScrollMetrics (src/pane/terminal.rs:49-53) is a three-key dict on the Python side:
#   offset_from_bottom / max_offset_from_bottom / viewport_rows

def should_show_scrollbar(metrics):
    """src/ui/scrollbar.rs:26-28: only shown when there is history to scroll back to."""
    return bool(metrics) and metrics["max_offset_from_bottom"] > 0


def terminal_inner_rect(pane_inner, pane_scrollbars=True, alt_screen=False):
    """src/ui/panes.rs:36-48: give up the rightmost column for the scrollbar, unless too narrow or on the alternate screen."""
    if not pane_scrollbars or pane_inner.width <= 4 or alt_screen:
        return pane_inner
    return Rect(pane_inner.x, pane_inner.y, pane_inner.width - 1, pane_inner.height)


def stable_scrollbar_gutter(pane_inner, metrics, pane_scrollbars=True,
                            alt_screen=False):
    """src/ui/panes.rs:175-196: the gutter is always reserved (content width never jumps
    when a scrollbar appears), but the track rect is only returned when there is history.
    Returns (content rect, track rect | None)."""
    inner = terminal_inner_rect(pane_inner, pane_scrollbars, alt_screen)
    if inner == pane_inner:
        return inner, None
    gutter = Rect(pane_inner.x + pane_inner.width - 1, pane_inner.y, 1,
                  pane_inner.height)
    return inner, (gutter if should_show_scrollbar(metrics) else None)


def scrollbar_thumb(metrics, track):
    """src/ui/scrollbar.rs:36-70: thumb position and length. Returns (top, len) | None."""
    if not metrics or metrics["max_offset_from_bottom"] == 0 or track.height == 0:
        return None
    track_height = track.height
    total_rows = metrics["max_offset_from_bottom"] + metrics["viewport_rows"]
    if total_rows == 0:
        return None
    thumb_len = int(min(max(round(metrics["viewport_rows"] * track_height / total_rows),
                            1), track_height))
    max_thumb_top = max(0, track_height - thumb_len)
    scrolled_from_top = max(0, metrics["max_offset_from_bottom"]
                            - metrics["offset_from_bottom"])
    if max_thumb_top == 0:
        thumb_top = 0
    else:
        thumb_top = int(min(max(round(scrolled_from_top * max_thumb_top
                                      / metrics["max_offset_from_bottom"]), 0),
                            max_thumb_top))
    return track.y + thumb_top, thumb_len


def scrollbar_thumb_grab_offset(metrics, track, row):
    """src/ui/scrollbar.rs:72-79: rows into the thumb when a press lands on it, else None."""
    thumb = scrollbar_thumb(metrics, track)
    if thumb is None:
        return None
    top, length = thumb
    return row - top if top <= row < top + length else None


def _scrollbar_offset_from_thumb_top(metrics, track, thumb_top):
    """src/ui/scrollbar.rs:81-101."""
    if metrics["max_offset_from_bottom"] == 0:
        return 0
    thumb = scrollbar_thumb(metrics, track)
    thumb_len = thumb[1] if thumb else 1
    max_thumb_top = track.height - min(thumb_len, track.height)
    if max_thumb_top == 0:
        return 0
    desired_top = min(thumb_top, max_thumb_top)
    scrolled_from_top = round(desired_top * metrics["max_offset_from_bottom"] / max_thumb_top)
    return max(0, metrics["max_offset_from_bottom"] - scrolled_from_top)


def scrollbar_offset_from_row(metrics, track, row):
    """src/ui/scrollbar.rs:103-117: a click on the track centres the thumb on that row."""
    thumb = scrollbar_thumb(metrics, track)
    if thumb is None:
        return 0
    clamped = min(max(row, track.y), track.y + max(0, track.height - 1))
    desired_top = max(0, (clamped - track.y) - thumb[1] // 2)
    return _scrollbar_offset_from_thumb_top(metrics, track, desired_top)


def scrollbar_offset_from_drag_row(metrics, track, row, grab_row_offset):
    """src/ui/scrollbar.rs:119-129: dragging keeps the grabbed thumb row under the pointer."""
    clamped = min(max(row, track.y), track.y + max(0, track.height - 1))
    desired_top = max(0, (clamped - track.y) - grab_row_offset)
    return _scrollbar_offset_from_thumb_top(metrics, track, desired_top)


def centered_popup_rect(area, popup_w, popup_h):
    """src/ui/widgets.rs:39-49: a popup centred in ``area``, clamped to area-4 / area-2."""
    popup_w = min(popup_w, max(0, area.width - 4))
    popup_h = min(popup_h, max(0, area.height - 2))
    if popup_w < 2 or popup_h < 2:
        return None
    return Rect(area.x + (area.width - popup_w) // 2, area.y + (area.height - popup_h) // 2,
                popup_w, popup_h)


def scrollbar_style(focused):
    """src/ui/scrollbar.rs:178-182: the focused pane gets a brighter, thicker thumb.
    Returns (track color, thumb color, thumb glyph); the track glyph is always '▕'."""
    if focused:
        return OVERLAY0, OVERLAY1, "▐"
    return PALETTE["surface1"], OVERLAY0, "▕"


def pane_border_title(label, pane_width):
    """src/ui/panes.rs:25-32: " label " for the top border, cut to pane width - 4 with an
    ellipsis; nothing for an empty label or a pane 4 columns wide or narrower."""
    label = (label or "").strip()
    if not label or pane_width <= 4:
        return None
    return f" {truncate_end(label, pane_width - 4)} "


def pane_inner_rect(area, borders):
    """src/ui/panes.rs:49-55: the ring eaten by borders; content is drawn inside it."""
    if not borders:
        return area
    x = area.x + (1 if "left" in borders else 0)
    y = area.y + (1 if "top" in borders else 0)
    width = area.width - (1 if "left" in borders else 0) - (1 if "right" in borders else 0)
    height = area.height - (1 if "top" in borders else 0) - (1 if "bottom" in borders else 0)
    return Rect(x, y, max(0, width), max(0, height))
