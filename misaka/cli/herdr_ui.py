"""herdr 客户端布局的逐函数移植（Rust→Python，对照其 v0.8.0 源码）。

每个函数注明出处 `文件:行`。语义照搬：Rect 即 ratatui::layout::Rect
（x/y 从 0 起算，width/height），u16 饱和运算对应 max(0,·)。
不在此发明任何布局——改这里必须先改注释里的出处。
"""
import os
import unicodedata
from collections import namedtuple

Rect = namedtuple("Rect", "x y width height")
RECT_DEFAULT = Rect(0, 0, 0, 0)

# 调色板：**与引擎同源**——读同一份内置主题（misaka 的暗/亮变体），
# 按终端背景自适应。不用 herdr 的 Catppuccin，只借它的角色划分
# （accent/overlay/surface…）。variant=None 时跟随终端背景检测。
def theme_variant():
    """当前该用哪个变体（dark/light）——与引擎 get_default_theme 同一判定。"""
    pinned = os.environ.get("MISAKA_THEME")
    if pinned in ("dark", "light"):
        return pinned
    try:
        from misaka.modes.interactive.theme.theme import get_default_theme
        return get_default_theme()
    except Exception:  # noqa: BLE001 - 检测不了就当暗色（misaka 主色系）
        return "dark"


def _load_palette(variant=None):
    import json
    variant = variant or theme_variant()
    theme_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "modes", "interactive", "theme")
    path = os.path.join(theme_dir, f"{variant}.json")
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)["vars"]
    except (OSError, ValueError, KeyError):
        v = {}

    def rgb(name, fallback):
        value = v.get(name, fallback).lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    return {
        "accent": rgb("accent", "#cb3862"),        # 聚焦高亮：御坂玫红
        "overlay0": rgb("darkGray", "#4a3d42"),    # 非聚焦边框
        "overlay1": rgb("gray", "#9b8288"),        # 非活动标签文字
        "surface0": rgb("selectedBg", "#3d2029"),  # 非活动标签底/侧栏选中底
        "surface1": rgb("userMsgBg", "#33212a"),
        "text": rgb("text", "#e2d6da"),
        "subtext0": rgb("gray", "#9b8288"),
        "panel_bg": rgb("toolPendingBg", "#2a1e23"),
        "green": rgb("green", "#7fa87f"),
        "yellow": rgb("yellow", "#e8c15a"),
        "red": rgb("red", "#e05252"),
        "plum": rgb("plum", "#a8557a"),
        "rose_lite": rgb("roseLite", "#e8698c"),
        "rose_deep": rgb("roseDeep", "#8f2545"),
        "amber": rgb("amber", "#d99a4e"),
    }


def blend(color_a, color_b, level):
    """两色线性插值（level 0→a，1→b）。给呼吸渐变用。"""
    level = min(1.0, max(0.0, level))
    return tuple(round(a + (b - a) * level) for a, b in zip(color_a, color_b))


# 模块加载时按终端背景定好变体（面板进程一次性，无需运行时切换）
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
    """src/ui/widgets.rs panel_contrast_fg：在有色底上取可读前景色。"""
    bg = ACCENT if bg is None else bg
    luminance = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) / 255
    return PANEL_BG if luminance > 0.5 else TEXT

# src/ui/tabs.rs:12-15
MIN_TAB_WIDTH = 8
NEW_TAB_WIDTH = 3
TAB_SCROLL_BUTTON_WIDTH = 3
# src/ui/tabs.rs:16-19
MIN_TAB_STRIP_WIDTH = MIN_TAB_WIDTH + NEW_TAB_WIDTH + TAB_SCROLL_BUTTON_WIDTH * 2

TabBarView = namedtuple(
    "TabBarView",
    "scroll tab_hit_areas scroll_left_hit_area scroll_right_hit_area new_tab_hit_area")


def display_width(text):
    """ui/text.rs display_width_u16 的对应：宽字符按 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


# ── src/ui.rs ─────────────────────────────────────────────────────────

def desktop_tab_bar_and_terminal_area(main_area, tab_count,
                                      hide_tab_bar_when_single_tab=False):
    """src/ui.rs:191-210；tab_bar_position 默认 Top（config/model.rs:1111）。"""
    hide_single = hide_tab_bar_when_single_tab and tab_count == 1
    if not hide_single and main_area.height > 1:
        tab_bar = Rect(main_area.x, main_area.y, main_area.width, 1)
        terminal = Rect(main_area.x, main_area.y + 1,
                        main_area.width, main_area.height - 1)
        return tab_bar, terminal
    return RECT_DEFAULT, main_area


def compute_view(area, sidebar_width, tab_count,
                 hide_tab_bar_when_single_tab=False):
    """src/ui.rs:215-244（compute_view_internal 桌面主干）：
    先横切 [侧栏, 主区]，再从主区头上切标签行。"""
    sidebar_w = min(sidebar_width, area.width)
    sidebar_area = Rect(area.x, area.y, sidebar_w, area.height)
    main_area = Rect(area.x + sidebar_w, area.y,
                     max(1, area.width - sidebar_w), area.height)
    tab_bar_rect, terminal_area = desktop_tab_bar_and_terminal_area(
        main_area, tab_count, hide_tab_bar_when_single_tab)
    return {"sidebar_rect": sidebar_area, "main_area": main_area,
            "tab_bar_rect": tab_bar_rect, "terminal_area": terminal_area}


# ── src/ui/sidebar.rs ─────────────────────────────────────────────────

def sidebar_section_heights(total_h, split_ratio):
    """src/ui/sidebar.rs:42-57。"""
    if total_h == 0:
        return 0, 0
    if total_h < 6:
        ws_h = -(-total_h // 2)               # div_ceil
        return ws_h, max(0, total_h - ws_h)
    ratio = min(0.9, max(0.1, split_ratio))
    ws_h = round(total_h * ratio)
    ws_h = min(max(ws_h, 3), max(0, total_h - 3))
    return ws_h, max(0, total_h - ws_h)


def expanded_sidebar_sections(area, split_ratio):
    """src/ui/sidebar.rs:59-70：内容宽=侧栏宽-1（末列是边框线）。"""
    content = Rect(area.x, area.y, max(0, area.width - 1), area.height)
    if content.width == 0 or content.height == 0:
        return RECT_DEFAULT, RECT_DEFAULT
    ws_h, detail_h = sidebar_section_heights(content.height, split_ratio)
    ws_area = Rect(content.x, content.y, content.width, ws_h)
    detail_area = Rect(content.x, content.y + ws_h, content.width, detail_h)
    return ws_area, detail_area


def sidebar_section_divider_rect(area, split_ratio):
    """src/ui/sidebar.rs:71-80：矮于 6 行没有分隔线。"""
    content = Rect(area.x, area.y, max(0, area.width - 1), area.height)
    if content.width == 0 or content.height < 6:
        return RECT_DEFAULT
    ws_h, _ = sidebar_section_heights(content.height, split_ratio)
    return Rect(content.x, content.y + ws_h, content.width, 1)


# ── src/ui/tabs.rs ────────────────────────────────────────────────────

def tab_chrome_label(tab_names, tab_idx, zoomed=()):
    """src/ui/tabs.rs:36-45：显示名或序号+1；缩放加 " Z"。"""
    name = tab_names[tab_idx] if tab_names[tab_idx] else str(tab_idx + 1)
    return f"{name} Z" if tab_idx in zoomed else name


def tab_width(tab_names, tab_idx, zoomed=()):
    """src/ui/tabs.rs:30-34：标签宽=名宽+4，下限 MIN_TAB_WIDTH。"""
    return max(display_width(tab_chrome_label(tab_names, tab_idx, zoomed)) + 4,
               MIN_TAB_WIDTH)


def layout_tab_hit_areas(tab_names, area, scroll, zoomed=()):
    """src/ui/tabs.rs:107-127：从 scroll 起逐个排，间隔 1 列，放不下的宽 0。"""
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
    """src/ui/tabs.rs:129-158：让活动标签尽量居中。"""
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
    """src/ui/tabs.rs:160-166。"""
    for rect in reversed(tab_hit_areas):
        if rect.width > 0:
            return rect.x + rect.width
    return fallback_x


def max_tab_scroll(tab_names, area, zoomed=()):
    """src/ui/tabs.rs:168-177：最小的能让最后一个标签露头的 scroll。"""
    for scroll in range(len(tab_names)):
        rects = layout_tab_hit_areas(tab_names, area, scroll, zoomed)
        if rects and rects[-1].width > 0:
            return scroll
    return 0


def compute_tab_bar_view(tab_names, active_tab, area, current_scroll,
                         follow_active, mouse_chrome, zoomed=()):
    """src/ui/tabs.rs:179-268：不溢出=全排＋新建钮；溢出=左右滚动钮夹标签区。"""
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


# ── src/layout.rs：BSP 分屏树 ─────────────────────────────────────────
# 节点用 tuple 表示，照搬 layout.rs:72 的 enum Node：
#   ("pane", pane_id)                         叶子
#   ("split", "h"|"v", ratio, first, second)  内节点（h=左右 Horizontal，v=上下 Vertical）


def split_at(node, target, direction, new_id, ratio):
    """src/layout.rs:590-617：把 target 叶子替换成一个分割节点（原格子＋新格子）。"""
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
    """src/layout.rs:619-625。"""
    if ratio != ratio or ratio in (float("inf"), float("-inf")):   # NaN/inf
        return 0.5
    return min(0.9, max(0.1, ratio))


def split_root(node, direction, new_id, ratio=0.5):
    """在整棵树外面包一层分割，新格子在 second 位（＝在右/下开一整列/行）。
    ⚠️ 非 herdr 原文：herdr 只劈聚焦格子；这是 misaka 自动平铺策略要用的原语。"""
    return ("split", direction, valid_split_ratio(ratio), node, ("pane", new_id))


def remove_pane(node, target):
    """src/layout.rs:627-655：删叶子；分割的一个孩子没了就把另一个孩子提上来。"""
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
    """src/layout.rs:691-710：按 ratio 把一个矩形切成两块（h=左右，v=上下）。"""
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
    """src/layout.rs:483-507：递归算出每个叶子的矩形。返回 [(pane_id, Rect, 是否聚焦)]。"""
    if node[0] == "pane":
        return [(node[1], area, node[1] == focus)]
    _, d, r, first, second = node
    a, b = split_rect(area, d, r)
    return collect_panes(first, a, focus) + collect_panes(second, b, focus)


def to_jsonable(node):
    """分屏树 → 可 JSON 化（tuple→list）。布局要交给守护进程持久化。"""
    if node[0] == "pane":
        return ["pane", node[1]]
    return ["split", node[1], node[2], to_jsonable(node[3]), to_jsonable(node[4])]


def from_jsonable(data):
    """JSON → 分屏树（list→tuple）。形状不对就返回 None，让调用方丢弃。"""
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
    """树里所有叶子 id（顺序＝从左到右深度优先，与 collect_panes 一致）。"""
    if node[0] == "pane":
        return [node[1]]
    return pane_ids(node[3]) + pane_ids(node[4])


def collect_splits(node, area, path=()):
    """src/layout.rs:509-536：所有分割边界（给鼠标拖拽调比例用）。
    返回 [{pos, direction, ratio, area, path}]。"""
    if node[0] != "split":
        return []
    _, d, r, first, second = node
    a, b = split_rect(area, d, r)
    pos = (a.x + a.width) if d == "h" else (a.y + a.height)
    out = [{"pos": pos, "direction": d, "ratio": r, "area": area, "path": tuple(path)}]
    out += collect_splits(first, a, (*path, False))
    out += collect_splits(second, b, (*path, True))
    return out


def _range_overlap_amount(a_start, a_len, b_start, b_len):
    """src/layout.rs:462-466。"""
    return max(0, min(a_start + a_len, b_start + b_len) - max(a_start, b_start))


def _range_center_distance(a_start, a_len, b_start, b_len):
    """src/layout.rs:468-472：中心距（用 2 倍避免小数）。"""
    return abs((a_start * 2 + a_len) - (b_start * 2 + b_len))


def find_in_direction(focused_id, direction, panes):
    """src/layout.rs:350-405：找 direction 方向上最近的格子。
    panes=[(id, Rect)]；direction ∈ left/right/up/down。
    排序键＝(边距, -重叠量, 中心距, 原顺序)——与 Rust 的 min_by_key 逐项对应。"""
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


# ── src/ui/panes.rs：格子边框（herdr 不画「分割线」，是给每格画完整边框）──

def _ranges_overlap(a_start, a_len, b_start, b_len):
    """src/ui/panes.rs:57-59。"""
    return a_start < b_start + b_len and b_start < a_start + a_len


def _pane_to_right(info, panes):
    """src/ui/panes.rs:61-73。"""
    right = info["rect"].x + info["rect"].width
    for other in panes:
        if (other["id"] != info["id"] and other["rect"].x == right
                and _ranges_overlap(info["rect"].y, info["rect"].height,
                                    other["rect"].y, other["rect"].height)):
            return other
    return None


def _pane_below(info, panes):
    """src/ui/panes.rs:75-82。"""
    bottom = info["rect"].y + info["rect"].height
    for other in panes:
        if (other["id"] != info["id"] and other["rect"].y == bottom
                and _ranges_overlap(info["rect"].x, info["rect"].width,
                                    other["rect"].x, other["rect"].width)):
            return other
    return None


def apply_pane_chrome(panes, pane_borders=True, pane_gaps=True,
                      pane_outer_borders=True):
    """src/ui/panes.rs:92-157：给每格定边框集合。
    panes=[{"id","rect","focused"}]；返回同结构，多一个 "borders"=集合，
    元素取自 {"top","bottom","left","right"}（对应 ratatui Borders 位）。
    相邻格子共享边：有右邻居就去掉自己的右边（无缝隙模式），下同。"""
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
    """src/ui/panes.rs:706-725：四向线段 → 制表符（含丁字/十字接头）。"""
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
    """src/ui/panes.rs:629-650：该格点是否属于这个格子的边（无缝隙时含共享边）。"""
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
    """src/ui/panes.rs:588-627 逐行移植。item={"rect","borders"}。"""
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
    """src/ui/panes.rs:531-587 逐行移植：把分割线接到已有格点上（只补，不新建）。"""
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
    """src/ui/panes.rs:484-528 render_pane_borders 逐行移植（几何面）。
    返回 {(x,y): (符号, 是否聚焦)}：先收齐所有格点，再统一定符号与高亮。
    area 给出画布范围时按它裁剪（对应原文的 buf.area 边界检查）。纯函数可测。"""
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


# ── 滚动条（src/ui/scrollbar.rs + src/ui/panes.rs:36-196）───────────────
# ScrollMetrics（src/pane/terminal.rs:49-53）在 Python 侧就是三元组 dict：
#   offset_from_bottom / max_offset_from_bottom / viewport_rows

def should_show_scrollbar(metrics):
    """src/ui/scrollbar.rs:26-28：有历史可回看才显示。"""
    return bool(metrics) and metrics["max_offset_from_bottom"] > 0


def terminal_inner_rect(pane_inner, pane_scrollbars=True, alt_screen=False):
    """src/ui/panes.rs:36-48：给滚动条让出最右一列；太窄或备用屏时不让。"""
    if not pane_scrollbars or pane_inner.width <= 4 or alt_screen:
        return pane_inner
    return Rect(pane_inner.x, pane_inner.y, pane_inner.width - 1, pane_inner.height)


def stable_scrollbar_gutter(pane_inner, metrics, pane_scrollbars=True,
                            alt_screen=False):
    """src/ui/panes.rs:175-196：槽位**恒定保留**（内容宽度不会因滚动条出现而跳），
    但只有真有历史时才返回轨道矩形。返回 (内容区, 轨道 | None)。"""
    inner = terminal_inner_rect(pane_inner, pane_scrollbars, alt_screen)
    if inner == pane_inner:
        return inner, None
    gutter = Rect(pane_inner.x + pane_inner.width - 1, pane_inner.y, 1,
                  pane_inner.height)
    return inner, (gutter if should_show_scrollbar(metrics) else None)


def scrollbar_thumb(metrics, track):
    """src/ui/scrollbar.rs:36-70：算滑块位置与长度。返回 (top, len) | None。"""
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


def scrollbar_style(focused):
    """src/ui/scrollbar.rs:178-182：聚焦格子的滚动条更亮、滑块更粗。
    返回 (轨道色, 滑块色, 滑块字符)；轨道字符恒为 '▕'。"""
    if focused:
        return OVERLAY0, OVERLAY1, "▐"
    return PALETTE["surface1"], OVERLAY0, "▕"


def pane_inner_rect(area, borders):
    """src/ui/panes.rs:49-55：边框吃掉的那一圈，内容画在里面。"""
    if not borders:
        return area
    x = area.x + (1 if "left" in borders else 0)
    y = area.y + (1 if "top" in borders else 0)
    width = area.width - (1 if "left" in borders else 0) - (1 if "right" in borders else 0)
    height = area.height - (1 if "top" in borders else 0) - (1 if "bottom" in borders else 0)
    return Rect(x, y, max(0, width), max(0, height))


if __name__ == "__main__":
    # 对照原文的行为自检（数值即 Rust 侧语义）
    ws, detail = sidebar_section_heights(20, 0.4)
    assert (ws, detail) == (8, 12) and ws + detail == 20
    assert sidebar_section_heights(5, 0.4) == (3, 2), "矮栏 div_ceil 折半"
    assert sidebar_section_heights(20, 0.01) == (3, 17), "ws 下限钳 3"

    area = Rect(0, 0, 25, 20)
    ws_a, dt_a = expanded_sidebar_sections(area, 0.4)
    assert ws_a == Rect(0, 0, 24, 8) and dt_a == Rect(0, 8, 24, 12), "内容宽=侧栏-1"
    assert sidebar_section_divider_rect(area, 0.4) == Rect(0, 8, 24, 1)
    assert sidebar_section_divider_rect(Rect(0, 0, 25, 5), 0.4) == RECT_DEFAULT

    names = ["Last Order", "10032", None]
    assert tab_chrome_label(names, 2) == "3", "无名标签用序号"
    assert tab_chrome_label(names, 0, zoomed={0}) == "Last Order Z"
    assert tab_width(names, 1) == max(len("10032") + 4, MIN_TAB_WIDTH)

    bar = Rect(0, 0, 60, 1)
    hits = layout_tab_hit_areas(names, bar, 0)
    assert hits[0].x == 0 and hits[1].x == hits[0].width + 1, "标签间隔 1 列"
    assert all(r.width > 0 for r in hits)

    view = compute_tab_bar_view(names, 0, bar, 0, True, True)
    assert view.new_tab_hit_area.width == NEW_TAB_WIDTH and view.scroll == 0
    assert view.scroll_left_hit_area == RECT_DEFAULT, "不溢出没有滚动钮"

    many = [f"agent-{i}" for i in range(12)]
    tight = compute_tab_bar_view(many, 11, Rect(0, 0, 40, 1), 0, True, True)
    assert tight.scroll_left_hit_area.width == TAB_SCROLL_BUTTON_WIDTH, "溢出出滚动钮"
    assert tight.scroll > 0, "跟随活动标签滚动"
    assert tight.new_tab_hit_area.width == NEW_TAB_WIDTH

    layout = compute_view(Rect(0, 0, 120, 40), 25, tab_count=3)
    assert layout["sidebar_rect"] == Rect(0, 0, 25, 40), "侧栏整列全高"
    assert layout["tab_bar_rect"] == Rect(25, 0, 95, 1), "标签行只属于主区"
    assert layout["terminal_area"] == Rect(25, 1, 95, 39)
    single = compute_view(Rect(0, 0, 120, 40), 25, 1, hide_tab_bar_when_single_tab=True)
    assert single["tab_bar_rect"] == RECT_DEFAULT and single["terminal_area"].height == 40

    # ── BSP 分屏树（layout.rs 语义对表）──
    tree = ("pane", "A")
    tree = split_at(tree, "A", "h", "B", 0.5)          # A 右边劈出 B：各半
    rects = collect_panes(tree, Rect(0, 0, 100, 40), "B")
    assert rects[0][:2] == ("A", Rect(0, 0, 50, 40))
    assert rects[1][:2] == ("B", Rect(50, 0, 50, 40)) and rects[1][2], "新格子占原格子的一半"
    tree = split_at(tree, "B", "v", "C", 0.5)          # 只劈 B：A 不动
    rects = dict((pid, r) for pid, r, _f in collect_panes(tree, Rect(0, 0, 100, 40), "C"))
    assert rects["A"] == Rect(0, 0, 50, 40), "劈 B 不动 A（BSP 局部性，不是全体重排）"
    assert rects["B"] == Rect(50, 0, 50, 20) and rects["C"] == Rect(50, 20, 50, 20)
    assert pane_ids(tree) == ["A", "B", "C"]
    collapsed = remove_pane(tree, "C")                 # 删 C：B 收回整半边
    rects = dict((pid, r) for pid, r, _f in collect_panes(collapsed, Rect(0, 0, 100, 40), "B"))
    assert rects["B"] == Rect(50, 0, 50, 40), "兄弟没了就把孩子提上来（remove_pane 折叠）"
    assert remove_pane(("pane", "A"), "A") is None
    # 布局往返（守护进程持久化用）：tuple↔list 不能丢结构
    round_trip = from_jsonable(to_jsonable(tree))
    assert round_trip == tree, (round_trip, tree)
    assert from_jsonable(["split", "h", 0.5, ["pane", "A"], ["pane", "B"]]) is not None
    assert from_jsonable(["坏data"]) is None and from_jsonable([]) is None
    assert from_jsonable(["split", "h", 0.5, ["pane", "A"], ["坏"]]) is None
    assert valid_split_ratio(float("nan")) == 0.5 and valid_split_ratio(0.01) == 0.1

    # ── 分割边界与格子边框（panes.rs 语义对表）──
    tree2 = split_at(split_at(("pane", "A"), "A", "h", "B", 0.5), "B", "v", "C", 0.5)
    borders_in = [{"id": pid, "rect": r, "focused": f}
                  for pid, r, f in collect_panes(tree2, Rect(0, 0, 100, 40), "A")]
    # 默认 pane_gaps=true（config/model.rs:1108）→ 每格自己一个完整框，不共享边
    chromed = {p["id"]: p["borders"] for p in apply_pane_chrome(borders_in)}
    every = {"top", "bottom", "left", "right"}
    assert chromed["A"] == every, "每格四边俱全（pane_gaps 默认开＝不移除相邻边）"
    assert chromed["B"] == every and chromed["C"] == every
    shared = {p["id"]: p["borders"] for p in apply_pane_chrome(borders_in, pane_gaps=False)}
    assert "right" not in shared["A"], "只有显式关掉 pane_gaps 才共享边"
    inner = pane_inner_rect(Rect(0, 0, 50, 40), chromed["A"])
    assert inner == Rect(1, 1, 48, 38), "四边框各吃一格"
    assert pane_inner_rect(Rect(0, 0, 50, 40), set()) == Rect(0, 0, 50, 40)
    single = apply_pane_chrome([{"id": "A", "rect": Rect(0, 0, 80, 24), "focused": True}])
    assert single[0]["borders"] == set(), "单格子无边框（multi_pane=false）"

    # ── 方向导航（layout.rs:350-405 对表）：A | (B / C) ──
    nav_panes = [(pid, r) for pid, r, _f in collect_panes(tree2, Rect(0, 0, 100, 40), "A")]
    assert find_in_direction("A", "right", nav_panes) == "B", "A 往右＝上半的 B（中心更近）"
    assert find_in_direction("B", "left", nav_panes) == "A"
    assert find_in_direction("C", "left", nav_panes) == "A"
    assert find_in_direction("B", "down", nav_panes) == "C"
    assert find_in_direction("C", "up", nav_panes) == "B"
    assert find_in_direction("A", "left", nav_panes) is None, "最左边没有左邻居"
    assert find_in_direction("A", "up", nav_panes) is None, "上下不重叠者不算邻居"

    # ── 边框格点与高亮（panes.rs:484-587 / 629-650 / 706-725 对表）──
    # 默认 pane_gaps=true：每格独立整框、无丁字接头、只认自己的边
    chromed_full = apply_pane_chrome(borders_in)
    cells = pane_border_cells(chromed_full)
    assert cells[(0, 0)][0] == "┌" and cells[(49, 0)][0] == "┐", "A 自己的左上/右上角"
    assert cells[(50, 0)][0] == "┌", "B 有自己的左上角（不与 A 共用）"
    assert cells[(0, 0)][1] and cells[(49, 0)][1], "聚焦格子 A 的四边高亮"
    assert not cells[(50, 0)][1] and not cells[(99, 5)][1], "别的格子的边不高亮"
    assert cells[(50, 20)][0] == "┌", f"C 的左上角，不是接头，实得 {cells[(50, 20)][0]}"
    assert ACCENT == (0xcb, 0x38, 0x62), f"accent＝misaka 主题玫红，实得 {ACCENT}"
    assert sgr_fg(ACCENT) == "\x1b[38;2;203;56;98m"
    assert panel_contrast_fg() == TEXT, "深色 accent 底上用亮文字"

    # ── 滚动条（scrollbar.rs / panes.rs:36-196 对表）──
    pane_inner = Rect(0, 0, 40, 10)
    no_hist = {"offset_from_bottom": 0, "max_offset_from_bottom": 0, "viewport_rows": 10}
    content, track = stable_scrollbar_gutter(pane_inner, no_hist)
    assert content == Rect(0, 0, 39, 10), "槽位恒定保留（宽度不因滚动条跳变）"
    assert track is None, "没历史就不画轨道"
    hist = {"offset_from_bottom": 0, "max_offset_from_bottom": 90, "viewport_rows": 10}
    content, track = stable_scrollbar_gutter(pane_inner, hist)
    assert track == Rect(39, 0, 1, 10), "轨道＝内容区最右一列"
    assert stable_scrollbar_gutter(pane_inner, hist, alt_screen=True)[1] is None, \
        "备用屏（全屏应用）不显示滚动条"
    assert stable_scrollbar_gutter(Rect(0, 0, 4, 10), hist)[1] is None, "太窄不让槽"

    top, length = scrollbar_thumb(hist, Rect(39, 0, 1, 10))
    assert length == 1 and top == 9, "在底部时滑块贴底"
    at_top = dict(hist, offset_from_bottom=90)
    assert scrollbar_thumb(at_top, Rect(39, 0, 1, 10)) == (0, 1), "滚到顶时滑块贴顶"
    half = dict(hist, offset_from_bottom=45)
    assert scrollbar_thumb(half, Rect(39, 0, 1, 10))[0] in (4, 5), "滚一半在中间"
    big = {"offset_from_bottom": 0, "max_offset_from_bottom": 10, "viewport_rows": 10}
    assert scrollbar_thumb(big, Rect(39, 0, 1, 10))[1] == 5, "历史少＝滑块长"
    assert scrollbar_thumb(no_hist, Rect(39, 0, 1, 10)) is None
    assert scrollbar_style(True)[2] == "▐" and scrollbar_style(False)[2] == "▕"

    splits = collect_splits(tree2, Rect(0, 0, 100, 40))
    assert splits[0]["direction"] == "h" and splits[0]["pos"] == 50, "竖分割线在 x=50"
    assert splits[1]["direction"] == "v" and splits[1]["pos"] == 20, "横分割线在 y=20"
    assert splits[1]["path"] == (True,) and splits[0]["path"] == ()
    # 主题同源：面板调色板必须直接来自内置 misaka 变体（与引擎读的同一份文件），
    # 不是硬编码——改主题文件面板就得跟着变（用户要求：全局生效含 herdr 移植层）
    import json as _json
    theme_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "modes", "interactive", "theme")
    for variant, key, var in (("dark", "accent", "accent"), ("light", "accent", "accent")):
        pal = _load_palette(variant)
        raw = _json.load(open(os.path.join(theme_dir, f"{variant}.json"), encoding="utf-8"))
        want = raw["vars"][raw["vars"].get(var, var) if raw["vars"].get(var) in raw["vars"]
                           else var]
        want = tuple(int(want.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        assert pal[key] == want, f"{variant} 面板 accent 应＝主题文件的 {want}，实得 {pal[key]}"
    dark_pal, light_pal = _load_palette("dark"), _load_palette("light")
    assert dark_pal["accent"] != light_pal["accent"], "两变体面板配色必须不同"
    assert theme_variant() in ("dark", "light")
    print("herdr_ui selfcheck ok — 布局/分区/标签条/BSP 树/格子边框 + 主题与引擎同源")
