"""多格子面板：misaka 的默认入口（herdr 形态，几何走 herdr_ui 的移植版）。

渲染：守护进程给每个格子养一块终端仿真屏，面板订阅**全部**格子的脏行，
自己排版——左侧边栏（Sisters 名册＋Windows 双区）｜主区顶部标签行＋
**页内 BSP 分屏树**（劈聚焦格子，其余不动；每格带边框，接头自动合并）。
配色与引擎**同源**（内置 misaka 暗/亮变体，按终端背景自适应），只借 herdr 的角色划分。

操作：鼠标点标签/侧栏条目/格子＝聚焦；前缀键 ctrl+b：`1-9` 切标签页｜
`n/p` 轮换页｜`hjkl` 选窗口｜`c` 新页｜`v`/`-` 左右/上下分屏｜`z` 缩放｜
`t` 召唤全局树｜`x` 关格子｜`d` 分离｜`?` 键位面板｜再按一次 ctrl+b 原样发进格子。
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
    """前缀键，默认 ctrl+b；tmux 用户的 ctrl+b 会被 tmux 吃掉——
    用 MISAKA_PANEL_PREFIX=ctrl+g 这类换一个。"""
    name = os.environ.get("MISAKA_PANEL_PREFIX", "ctrl+b").lower().strip()
    if name.startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return bytes([ord(name[5]) - 96])
    return b"\x02"


PREFIX = _prefix_key()
DEBUG_LOG = os.path.expanduser("~/.misaka/panel-debug.log")
POLL_SECONDS = 2.0
SIDEBAR_W = 24            # 侧边栏字符宽；分隔线在 SIDEBAR_W+1 列，主区再空一列
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_MOUSE_X10 = re.compile(rb"\x1b\[M[\x20-\xff]{3}")   # 老式鼠标报文（部分终端不支持 SGR 格式）
# herdr 双击选词的断词字符集（照抄其二进制内嵌的那串）＋空白
_WORD_BREAK = set(" \t" + "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~")

# Windows 区：左侧＝运行指示（真在跑才亮，闪烁圆点），右侧＝卡片状态词
_P = hui.PALETTE
BREATH_SECONDS = 1.6             # 呼吸一个来回（Claude Code 那种渐变节奏）
BREATH_FPS = 0.12                # 渐变重画间隔
# 只有终态/等待类才配状态词；「在跑」用闪烁圆点表示，不写字
_STATUS_WORD = {
    "verifying": ("verify", hui.sgr_fg(_P["plum"])),
    "finalizing": ("verify", hui.sgr_fg(_P["plum"])),
    "ready": ("ready", hui.sgr_fg(hui.OVERLAY0)),
    "failed": ("failed", hui.sgr_fg(_P["red"])),
    "stopped": ("stopped", hui.sgr_fg(_P["plum"])),
    "done": ("done", hui.sgr_fg(hui.OVERLAY0)),
}
_UNSEEN_WORD = {                 # 终态没看过：亮色（herdr 的"完了没人看"）
    "done": ("done", hui.sgr_fg(_P["green"])),
    "failed": ("failed", hui.sgr_fg(_P["red"])),
    "stopped": ("stopped", hui.sgr_fg(_P["plum"])),
}


def breath_level(now, period=BREATH_SECONDS):
    """0..1 的呼吸相位（正弦最平滑，不是二值闪烁）。纯函数可测。"""
    return (math.sin(2 * math.pi * (now % period) / period) + 1) / 2


# 运行指示的呼吸区间：主题绿（#7fa87f）的暗版↔亮版
DOT_DIM = tuple(round(c * 0.42) for c in _P["green"])
DOT_BRIGHT = tuple(min(255, round(c * 1.28)) for c in _P["green"])


def busy_dot(busy, level, dim=None, bright=None):
    """运行指示：在跑＝**呼吸渐变的绿点**（暗绿↔亮绿），没跑＝空白。
    level 由 breath_level() 给。返回的串**不含 reset**——否则会把选中底色清掉。"""
    if not busy:
        return " "
    color = hui.blend(dim or DOT_DIM, bright or DOT_BRIGHT, level)
    return f"{hui.sgr_fg(color)}●"


def _wcwidth(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)



# 防嵌套时的随机吐槽（herdr main.rs:13-20 NESTED_HERDR_MESSAGES 的御坂版）
NESTED_MESSAGES = [
    "御坂在御坂里面开御坂，御坂表示这样下去会有两万个窗口。",
    "递归检测：找不到基线条件，御坂选择中止。",
    "你光顾着想能不能，没停下来想想该不该。",
    "套娃被拒。某处有一个调用栈松了口气。",
    "一号自称是妹妹的妹妹的妹妹——网络已拒绝该请求。",
    "面板套面板？这是妹妹们才玩的把戏，你不是妹妹。",
]


def next_tile_placement(tree):
    """御坂网络的默认平铺策略（**非 herdr**，misaka 自己的需求）：
    每列最多两个（先上下劈），列满就往右开新列。
    返回 (劈谁, 方向)；劈谁=None 表示在整棵树外面包一层（新开一列）。纯函数可测。

        1个 [A]   2个 [A]   3个 [A][C]   4个 [A][C]   5个 [A][C][E]
                      [B]       [B]          [B][D]       [B][D]
    """
    ids = hui.pane_ids(tree)
    if len(ids) % 2 == 0:                      # 每列已满两个 → 右边开新列
        return None, "h"
    placed = hui.collect_panes(tree, hui.Rect(0, 0, 1000, 1000), ids[0])
    rightmost = max(placed, key=lambda item: (item[1].x, -item[1].y))
    return rightmost[0], "v"                   # 最右那列上下劈


def render_tab_bar(tab_names, active_index, view, area, tab_scroll=0):
    """herdr tabs.rs:319-470 render_tab_bar 移植：整行铺 panel_bg；滚动钮
    `" < "`/`" > "`（不可滚动时 overlay0＋DIM）；标签文字**居中**，活动页
    accent 底＋对比字、其余 overlay1 字＋surface0 底；加号 `" + "`（overlay1，
    无底色）；两侧还有标签被截时各画一个 `…`。返回整行文本。纯函数可测。
    （不区分 herdr 的 is_auto_named——misaka 的标签名总是 agent 名。）"""
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
            if _wcwidth(ch) == 2:            # 宽字符吃掉下一格
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

    if view.new_tab_hit_area.width:          # 加号：只有前景色，不铺底
        put(view.new_tab_hit_area.x, " + ", f"{hui.sgr_fg(hui.OVERLAY1)}{base}")

    if first_visible is not None and first_visible > 0:      # 左边还有被截的标签
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


_CARD_GLYPH = {          # 课题展开后卡片行的状态字形：完成＝主题玫红，暗色留给未完成
    "running": ("●", "green"), "verifying": ("◆", "plum"), "finalizing": ("◆", "plum"),
    "ready": ("○", "overlay"), "done": ("✓", "accent"),
    "failed": ("✗", "red"), "stopped": ("■", "plum"),
}


def _row(body, body_w, selected):
    """侧栏整条行成品：左右各留 1 列空隙；选中＝surface0 底罩住整条
    （herdr sidebar.rs:791；reset 后把底色贴回，防色块被打断）。"""
    body += " " * max(0, (SIDEBAR_W - 2) - body_w)
    if selected:
        bg = hui.sgr_bg(hui.SURFACE0)
        body = bg + body.replace("\x1b[0m", "\x1b[0m" + bg) + "\x1b[0m"
    else:
        body += "\x1b[0m"
    return " " + body + " "


def _alloc_sections(rows, wants):
    """把侧栏高度分给三个区（区间各夹 1 行分隔线）：要得少的先喂饱，
    剩余按顺序补给没喂饱的。非 herdr（它只有两区，sidebar.rs:42）。纯函数可测。"""
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
    """侧边栏三区：Sisters 名册｜Projects 课题（点开展开卡列表，置顶在前、
    归档沉底标灰）｜Windows 窗口列表。每区超高出滚动条（末列细轨）。
    返回 (各行, 目标表, 区间表)；目标表与行对齐：
    None｜("roster",名)｜("pane",id)｜("proj",课题名)｜("card",卡号)。纯函数可测。"""
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
    # 临时御坂＝**在 misaka 里跑着的**第三方 agent：★ 主题色标记，
    # 进程一结束就从名册消失（不落名册文件，纯活体列表）
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
        name = proj["name"]
        shown = name if name is not None else "(未分类)"
        arrow = "▾" if (name in expanded) else "▸"
        star = "★" if proj.get("pinned_at") else ""
        live_n = sum(1 for c in proj["cards"]
                     if c["status"] in ("ready", "running", "verifying", "finalizing"))
        label = "归档" if proj["archived"] else (str(live_n) if live_n else "")
        tone = hui.sgr_fg(hui.OVERLAY0) if proj["archived"] else ""
        inner = SIDEBAR_W - 2
        shown, shown_w = _cut(shown,
                              max(4, inner - 5 - _wcwidth(star) - _wcwidth(label)))
        left = f"{tone}{arrow} {star}{shown}"
        left_w = 2 + _wcwidth(star) + shown_w
        gap = max(1, inner - left_w - _wcwidth(label))
        projs.append((_row(f"{left}{' ' * gap}{hui.sgr_fg(hui.OVERLAY0)}{label}\x1b[0m",
                           left_w + gap + _wcwidth(label),
                           selected == ("proj", name)),
                      ("proj", name)))
        if name in expanded:
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
            glyph = busy_dot(busy, level)     # 左侧只表达「此刻在不在跑」
            label, color = "", ""
            if pane["card"]:
                if pane.get("unseen"):
                    label, color = _UNSEEN_WORD.get(status, ("", ""))
                elif status != "running":
                    label, color = _STATUS_WORD.get(status, ("", ""))
        mail = (f"{hui.sgr_fg(_P['red'])}✉{pane['mail']}\x1b[0m"
                if pane.get("mail") else "")
        mail_w = (1 + len(str(pane["mail"]))) if pane.get("mail") else 0
        inner_w = SIDEBAR_W - 2               # 一整条＝左右各留 1 列空隙
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
        if total > vis and vis > 0:          # 超高：末列画细轨＋滑块
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
    # 出口硬钳：任何一行都不许宽过侧栏（上游漏了 _cut 也穿不出边框）
    lines = [_clamp_row(line, SIDEBAR_W) for line in lines]
    return lines[:rows], targets[:rows], spans


def draw_scrollbar(metrics, track, focused):
    """herdr scrollbar.rs:135-162 render_scrollbar：轨道 '▕'＋滑块，聚焦更亮更粗。
    返回转义序列串。纯函数可测。"""
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
    """画所有格子边框（herdr panes.rs:484-528 render_pane_borders 的输出面）：
    逐格点取合并后的制表符，接触聚焦格子的格点用 accent，其余 overlay0。
    调用时机照 herdr：**在所有格子内容渲染之后**。纯函数可测。"""
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
    """herdr 的前缀模式底栏（src/ui/menus.rs render_prefix_overlay 同款）：
    高亮 PREFIX 徽章＋按键提示，占满整个底行。纯函数可测。"""
    key = f"{hui.sgr_fg(hui.ACCENT)}\x1b[1m"      # herdr menus.rs：键名用 accent＋粗体
    dim = hui.sgr_fg(hui.OVERLAY1)
    badge = f"{hui.sgr_bg(hui.ACCENT)}{hui.sgr_fg(hui.panel_contrast_fg())}\x1b[1m"
    parts = [(f"{badge} PREFIX \x1b[0m", 8)]
    for name, desc in ((f"{prefix_name}", "原样发送"), ("1-9", "切页"),
                       ("hjkl", "选窗口"), ("c", "新页"), ("v/-", "分屏"),
                       ("z", "缩放"), ("t", "树"), ("m", "鼠标"), ("x", "关格子"),
                       ("d", "分离"), ("?", "键位"), ("esc", "取消")):
        parts.append((f" {key}{name}\x1b[0m{dim} {desc}\x1b[0m",
                      1 + len(name) + 1 + _wcwidth(desc)))
    out, used = [], 0
    for text, visible in parts:            # 放不下就不放，绝不溢出屏幕
        if used + visible > width:
            break
        out.append(text)
        used += visible
    return "".join(out) + " " * max(0, width - used)


TREE_TITLE = "树"
TREE_ARGV = [sys.executable, "-m", "misaka", "trace", "--watch"]


def tree_summon_action(listing):
    """prefix+t 的决策（纯函数可测）：活着的树格子→聚焦它；没有→新开一页。"""
    existing = next((p["id"] for p in listing
                     if p["alive"] and p["title"] == TREE_TITLE), None)
    return ("focus", existing) if existing else ("create", TREE_ARGV)


_HELP_ROWS = [
    ("1-9", "切到第 N 个标签页"), ("n / p", "下一页 / 上一页"),
    ("h j k l", "选窗口：左 下 上 右"), ("c", "新建标签页"),
    ("v", "分屏：左右劈开"), ("-", "分屏：上下劈开"),
    ("z", "缩放：聚焦格子独占"), ("t", "召唤全局树（当前页右侧劈出）"),
    ("x", "关掉当前格子"),
    ("d", "分离（格子照跑）"), ("ctrl+b", "再按一次＝原样发进格子"),
    ("esc", "取消前缀模式"), ("鼠标", "点＝聚焦，拖＝选中复制，双击＝选词"),
    ("", "任意键关闭本面板"),
]


def _cut(text, width):
    """按显示宽截断（CJK=2 列）。返回 (截后文本, 实际宽)。
    ⚠️ 侧栏一切可变文本必须走这里——`text[:n]` 是按字符切的，
    17 个汉字＝34 列，直接穿透边框喷进主区（踩过，用户截图抓获）。"""
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
    """按**显示列**区间 [c0,c1) 取子串（CJK 占 2 列，跨界的宽字符算进来）。
    选区是照屏幕列拉的，不能按字符下标切。"""
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
    """写系统剪贴板。herdr 在 macOS 上就是走 pbcopy/pbpaste（其二进制内嵌可证）；
    别的平台退回 OSC 52，让终端自己代劳。"""
    import subprocess
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    _write_all(b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")


def _clamp_row(line, width):
    """ANSI 感知的整行硬钳：显示宽超过 width 就截、不足就补空格。
    format_sidebar 出口的最后防线——就算上游哪行忘了 _cut 也穿不出边框。"""
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
    """卡片右键菜单：只有删除（破坏性）。版式同课题菜单。纯函数可测。"""
    rows = [("d", "删除这张卡（连事件与预算，不可恢复）"), ("", "其他键关闭")]
    inner = width - 2
    title, tw = _cut(f"─ 卡：{card_id} ", inner)
    lines = ["┌" + title + "─" * (inner - tw) + "┐"]
    for key, desc in rows:
        body, bw = _cut(f"  {key}" + " " * max(1, 4 - len(key)) + desc, inner)
        lines.append("│" + body + " " * (inner - bw) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def format_project_menu(name, pinned, archived, width=46):
    """课题右键菜单（版式同键位面板）：按当前状态给动词。
    行宽严格＝width（超长名/窄屏都截断，绝不外溢）。纯函数可测。"""
    rows = [("1", "取消置顶" if pinned else "置顶"),
            ("2", "恢复进行中" if archived else "归档"),
            ("3", "删除（有卡挂着会拒；目录软删可反悔）"),
            ("", "其他键关闭")]
    inner = width - 2
    title, tw = _cut(f"─ 课题：{name} ", inner)
    lines = ["┌" + title + "─" * (inner - tw) + "┐"]
    for key, desc in rows:
        body, bw = _cut(f"  {key}" + " " * max(1, 4 - len(key)) + desc, inner)
        lines.append("│" + body + " " * (inner - bw) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def format_help_lines(width=46):
    """键位面板（herdr 的 keybind help 简版）：按字素宽对齐补白，
    行宽严格＝width（窄屏截断不外溢）。纯函数可测。"""
    inner = width - 2
    lines = ["┌─ 键位 " + "─" * max(0, inner - _wcwidth("─ 键位 ")) + "┐"]
    for key, desc in _HELP_ROWS:
        body, bw = _cut(f"  {key}" + " " * max(1, 9 - _wcwidth(key)) + desc, inner)
        lines.append("│" + body + " " * (inner - bw) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


class _Sock:
    """一行一条 JSON 的同步小客户端（面板用两条：控制＋订阅流）。"""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.sock.settimeout(15)   # 守护进程卡死时报「面板断开」，不无声冻住键盘
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
                raise ConnectionError("守护进程断了")
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
    """写满为止。os.write 对终端可能只写出一部分——转义序列被拦腰截断，
    终端就会把后半截当普通字符打出来（越界的 `[` 就是这么来的）。"""
    if os.environ.get("MISAKA_PANEL_DEBUG"):
        with open(DEBUG_LOG, "a", encoding="utf-8") as dbg:
            dbg.write(f"{time.time():.3f} out {bytes(data)!r}\n")
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
    # herdr main.rs:469-503 同款防嵌套：格子里的进程都带 MISAKA_NET_PANE，
    # 在格子里再开面板＝面板套面板，无限递归。逃生阀 MISAKA_ALLOW_NESTED=1
    # （对应它的 experimental.allow_nested）。
    if os.environ.get("MISAKA_NET_PANE") and os.environ.get("MISAKA_ALLOW_NESTED") != "1":
        sys.exit(
            "\x1b[1m错误：\x1b[0m你已经在一个格子里了，面板不能套面板。\n"
            "  跟御坂说话：\x1b[1mmisaka chat\x1b[0m"
            "（找妹妹加 \x1b[1m--as 10032\x1b[0m）\n"
            "  真要套娃：\x1b[1mMISAKA_ALLOW_NESTED=1 misaka\x1b[0m\n\n"
            "\x1b[2m\"" + NESTED_MESSAGES[os.getpid() % len(NESTED_MESSAGES)] + "\"\x1b[0m")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("面板需要终端；脚本里请直接用 misaka chat / misaka net …")
    if os.environ.get("MISAKA_PANEL_DEBUG"):
        with open(DEBUG_LOG, "a", encoding="utf-8") as dbg:
            dbg.write(f"=== {time.time():.3f} 启动 TERM_PROGRAM="
                      f"{os.environ.get('TERM_PROGRAM')!r} TERM={os.environ.get('TERM')!r} "
                      f"TMUX={'有' if os.environ.get('TMUX') else '无'} "
                      f"prefix={PREFIX!r} ===\n")
    net.ensure()
    sock_path = os.path.expanduser(net.CFG["net_sock"])
    control, stream = _Sock(sock_path), _Sock(sock_path)

    def panes():
        return control.request("panes.list")["panes"]

    listing = panes()
    for pane in listing:      # 死掉的编排官格子先收尸
        if pane["title"] == "Last Order" and not pane["alive"]:
            control.request("pane.close", {"id": pane["id"]})
    listing = panes()
    lo = next((p for p in listing if p["title"] == "Last Order" and p["alive"]), None)
    if lo is None:
        control.request("pane.create", {
            "argv": [sys.executable, "-m", "misaka", "chat"],
            "cwd": os.getcwd(), "title": "Last Order",
            "env": {"MISAKA_THEME": hui.theme_variant()}})
        listing = panes()
        lo = next(p for p in listing if p["title"] == "Last Order")
    focused = lo["id"]
    zoom = False
    # 光标状态**按格子存**：位置和显隐都是每格自己的事（引擎藏真光标自己画块，
    # shell 则显示）。存成一个全局值的话，任何一条路径忘了跟着换格子同步，真光标
    # 就会叠到引擎画的假块上——那块看着突然变亮（这坑踩过两次：中文输入法、点击切格）
    pane_cursor = {}          # 格子 → {"at": [列, 行], "hidden": bool}
    slices = []
    # herdr 语义：标签=一页，页内是 BSP 分屏树（layout.rs 移植版，节点见 herdr_ui）
    tabs = []            # 每项是一棵树；默认每个格子自成一页

    def sync_tabs():
        alive = [p["id"] for p in listing if p["alive"]]
        known = {pid for tree in tabs for pid in hui.pane_ids(tree)}
        before = list(tabs)
        for index, tree in enumerate(tabs):
            for pid in hui.pane_ids(tree):
                if pid not in alive:
                    tree = hui.remove_pane(tree, pid)   # 树自己折叠（兄弟提上来）
                    if tree is None:
                        break
            tabs[index] = tree
        tabs[:] = [tree for tree in tabs if tree is not None]
        for pid in alive:
            if pid not in known:
                tabs.append(("pane", pid))
        if tabs != before:
            save_layout()          # 布局归守护进程存，面板退了分屏也不散

    def save_layout():
        try:
            control.request("layout.set",
                            {"layout": [hui.to_jsonable(t) for t in tabs]})
        except (RuntimeError, ConnectionError):
            pass                   # 存不上不影响用，下次改动再试

    def load_layout():
        """从守护进程取回布局（面板重进/换终端都能续上分屏）。"""
        try:
            saved = control.request("layout.get")["layout"]
        except (RuntimeError, ConnectionError, KeyError):
            return
        alive = {p["id"] for p in listing if p["alive"]}
        for item in saved or []:
            tree = hui.from_jsonable(item)
            if tree is None:
                continue
            for pid in hui.pane_ids(tree):     # 丢掉已经没了的格子
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
    main_col = SIDEBAR_W + 2      # 主区起始列（herdr：紧贴侧栏边框，无额外空列）
    tab_scroll, tab_follow = 0, True   # herdr tab_scroll / tab_scroll_follow_active
    resized = {"hit": True}
    signal.signal(signal.SIGWINCH, lambda *_a: resized.update(hit=True))

    def slice_of(pane_id):
        return next((s for s in slices if s[0] == pane_id), None)

    def cursor_tail():
        """这一帧画完，光标该摆哪儿——拼在帧尾，跟内容**同一次写入**。

        位置永远跟随聚焦格子里的应用（中文输入法的拼音和候选窗就贴着终端真光标
        走）；显隐也跟随（herdr host_cursor="auto"：macOS 用原生终端光标）——引擎
        自己画了假光标块并要求藏真光标，我们再亮一个就叠成一块更亮的。"""
        sl = slice_of(focused)
        state = pane_cursor.get(focused)
        if sl is None or state is None:
            return b""
        if (scroll_state.get(focused, {}).get("metrics") or {}).get("offset_from_bottom"):
            # 回看历史：光标是「当前屏」的东西，那个坐标在历史视图上指的是别的
            # 内容——摆上去就成了游荡的幽灵块（herdr/pi 回看时都不显示光标）。
            return b"\x1b[?25l"
        rect = sl[1]
        at = state["at"]
        col = rect.x + 1 + min(at[0], max(0, rect.width - 1))
        row = rect.y + 1 + min(at[1], max(0, rect.height - 1))
        return (f"\x1b[{min(row, rows)};{col}H".encode()
                + (b"\x1b[?25l" if state["hidden"] else b"\x1b[?25h"))

    def paint(data):
        """画什么都走这儿：帧尾自带光标归位，中间不留窗口。

        ⚠️ 绘制路径一律用 paint 而不是 _write_all——画完不归位，光标就留在最后
        画的那个字符上，输入法的候选窗跟着飘过去（侧栏渐变每 0.12 秒重画一遍，
        正好把光标拽到侧栏底行 → 候选窗掉到屏幕最下面，用户截图实证）。"""
        _write_all(data + cursor_tail())

    chrome_cache = {"rows": []}       # 只写变化行（herdr 的差分纪律，行粒度）
    chrome_state = {"chromed": [], "area": None, "tracks": []}   # 边框＋滚动条几何
    # 名册/课题要读目录和库，渐变每 0.12 秒重画一次——缓存，只跟状态轮询一起刷新
    roster_cache = {"names": ["last-order", *sorted(_sisters_roster())]}
    projects_cache = {"items": []}
    expanded_projects = set()      # 点开的课题（展开卡列表）
    side_scrolls = {}              # 每区滚动位（sisters/projects/windows）
    side_selected = [None]         # Projects 区最近点选的行（("proj",名)/("card",卡)）

    def refresh_projects():
        try:
            projects_cache["items"] = control.request("projects.list")["projects"]
        except (RuntimeError, ConnectionError):
            pass
    scroll_state = {}    # 格子 → {"metrics":…, "alt":bool}（守护进程给的滚动读数）

    def draw_borders():
        """herdr panes.rs:412：边框永远最后画（内容之上），聚焦变了就重画高亮。
        滚动条同理（render_pane_scrollbar 也在内容之后）。"""
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
                # 历史没了（clear）或进了备用屏：把槽位擦干净，别让滚动条卡住
                out.append("\x1b[0m")
                for y in range(gutter.y, gutter.y + gutter.height):
                    out.append(f"\x1b[{y + 1};{gutter.x + 1}H ")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
    ui_map = {"bar": None, "targets": []}   # 鼠标热区：标签条几何＋侧栏目标表

    def draw_sidebar(note=None, force=False):
        nonlocal tab_scroll
        sync_tabs()
        names = [tab_label(tab) for tab in tabs]
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), SIDEBAR_W + 1, len(tabs))
        # mouse_chrome=True：herdr 同款「+」新建钮＋溢出滚动钮
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
        # herdr ui.rs：先横切整列（侧栏全高），标签行只从主区头上切一行
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
            if force or index >= len(cached) or cached[index] != chunk:
                out.append(chunk)
        chrome_cache["rows"] = wanted
        out.append("\x1b[?2026l")
        if len(out) > 2:
            paint("".join(out).encode())
        if note:                 # 提示只走主区底行，不占侧栏
            bottom_note(note)

    def draw_prefix_bar():
        # herdr：进前缀模式=底行弹出模式栏（menus.rs render_prefix_overlay）；
        # 只占**主区**宽度——侧栏是常驻导航，不该被模式栏盖掉
        width = max(10, cols - SIDEBAR_W - 1)
        paint(f"\x1b[?2026h\x1b[{rows};{main_col}H\x1b[K"
                   f"{format_prefix_bar(width)}\x1b[?2026l".encode())

    def bottom_note(text):
        """底行提示（只占主区宽度，绝不溢进侧栏）。"""
        width = max(10, cols - SIDEBAR_W - 2)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        while _wcwidth(plain) > width and plain:
            text, plain = text[:-1], plain[:-1]
        paint(f"\x1b[{rows};{main_col}H\x1b[K{text}\x1b[0m".encode())

    def restore_bottom():
        """收掉模式栏：清主区底行，再把该格子的底行内容补回来（侧栏没被盖，不用动）。"""
        paint(f"\x1b[?25l\x1b[?2026h\x1b[{rows};{main_col}H\x1b[K".encode())
        for pane_id, rect in slices:
            if rect.y + rect.height < rows:      # 这格没画到底行，不用补
                continue
            try:
                screen = control.request("pane.screen", {"id": pane_id})
            except (RuntimeError, ConnectionError):
                continue
            if screen["rows"]:
                paint(f"\x1b[{rows};{rect.x + 1}H{screen['rows'][-1]}"
                           "\x1b[?2026l".encode())

    def draw_help_overlay():
        # herdr：? 弹完整键位面板（keybind_help）——画在主区居中，任意键关；
        # 宽度钳到主区，窄屏不外溢
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

    # ── 鼠标选区＋复制（herdr 自带，不靠关鼠标捕获）──────────────────
    # herdr 的做法：鼠标照捕获，选区自己实现——拖拽画 selection_background，
    # 松手即入剪贴板（copy_on_select 默认 true），双击选词同理；
    # 同时不占用 shift+鼠标，留给终端原生选区（其配置注释明写此意）。
    pane_rows = {}      # 格子 → 完整渲染行；选区高亮和取字都照它算
    sel = {"pane": None, "a": None, "b": None}    # 起止＝格内 (列, 行)
    last_press = [0.0, -1, -1]                    # 双击检测：时刻＋屏幕坐标

    def sel_rows(pane_id=None):
        pane_id = pane_id or sel["pane"]
        if pane_id is None or sel["a"] is None or sel["b"] is None:
            return set()
        return set(range(min(sel["a"][1], sel["b"][1]),
                         max(sel["a"][1], sel["b"][1]) + 1))

    def sel_span(row, width):
        """选区在第 row 行覆盖的列区间 [c0,c1)；线性选区，同终端原生的形状。"""
        if sel["a"] is None or sel["b"] is None:
            return None
        (x0, y0), (x1, y1) = sorted((sel["a"], sel["b"]), key=lambda p: (p[1], p[0]))
        if not y0 <= row <= y1:
            return None
        return (x0 if row == y0 else 0, (x1 + 1) if row == y1 else width)

    def paint_selection(dirty=(), pane_id=None):
        """重画受影响的行：先照缓存还原，再把选中段刷上选中底色。"""
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
        bottom_note(f"{hui.sgr_fg(hui.OVERLAY1)}已复制 {len(text)} 字到剪贴板")

    def pane_at(mx, my):
        """屏幕坐标（1 基）落在哪个格子里 → (格子, 格内列, 格内行)。"""
        for pane_id, rect in slices:
            if (rect.x < mx <= rect.x + rect.width
                    and rect.y < my <= rect.y + rect.height):
                return pane_id, mx - rect.x - 1, my - rect.y - 1
        return None

    def select_word(pane_id, col, row):
        """herdr：双击选词并复制（断词字符集照抄它）。"""
        buf = pane_rows.get(pane_id, [])
        if row >= len(buf):
            return
        plain = _ANSI.sub("", buf[row])
        # 按显示列展开成「每列一个字符」，宽字符占两格，好照列定位
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
            # 先更新再画：paint 的帧尾归位要用这一帧的新坐标，晚一步就摆到旧位置
            state = pane_cursor.setdefault(pane_id, {"at": [0, 0], "hidden": False})
            if cur is not None:
                state["at"] = list(cur)
            if hidden is not None:
                state["hidden"] = bool(hidden)
        buf = pane_rows.setdefault(pane_id, [])
        if clear or len(buf) != rect.height:
            buf[:] = [""] * rect.height
        out = ["\x1b[?25l\x1b[?2026h"]
        if clear:      # 整屏刷新（回看/重排）：先清自己的内容区，免得旧行残留
            for row in range(rect.y, rect.y + rect.height):
                out.append(f"\x1b[{row + 1};{rect.x + 1}H" + " " * rect.width)
        for row_str, line in rendered.items():
            row = int(row_str)
            if row < rect.height:                # 只画进自己的矩形（herdr 硬截断）
                out.append(f"\x1b[{rect.y + row + 1};{rect.x + 1}H{line}")
                buf[row] = line
        out.append("\x1b[?2026l")
        paint("".join(out).encode())
        if sel["pane"] == pane_id:       # 新内容盖了高亮，补回来
            paint_selection()

    def relayout():
        nonlocal slices
        sync_tabs()
        # herdr：主区只画活动页；页内布局＝BSP 树递归切矩形（layout.rs collect_panes）
        view = hui.compute_view(hui.Rect(0, 0, cols, rows), SIDEBAR_W + 1, len(tabs))
        term = view["terminal_area"]
        tree = tabs[active_tab()] if tabs else None
        if tree is None:
            slices = []
            draw_sidebar()
            return
        if zoom:
            placed = [(focused, term, True)]
        else:
            placed = hui.collect_panes(tree, term, focused)
        # herdr panes.rs：多格时每格画完整边框（相邻共享边），内容画在边框内侧
        chromed = hui.apply_pane_chrome(
            [{"id": pid, "rect": rect, "focused": f} for pid, rect, f in placed])
        # 再给滚动条让出最右一列（herdr stable_scrollbar_gutter：槽位恒定保留）
        slices, tracks = [], []
        for item in chromed:
            pane_inner = hui.pane_inner_rect(item["rect"], item["borders"])
            state = scroll_state.get(item["id"], {})
            content, _track = hui.stable_scrollbar_gutter(
                pane_inner, state.get("metrics"), alt_screen=state.get("alt", False))
            slices.append((item["id"], content))
            # 槽位恒定（herdr stable gutter）；画不画由 draw_borders 按实时读数决定
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
        for row in range(term.y + 1, term.y + term.height + 1):     # 清主区
            paint(f"\x1b[{row};{term.x + 1}H\x1b[K".encode())
        paint(b"\x1b[?2026l")
        # 尺寸刚变，全屏应用要收到 SIGWINCH 后才重画——给一小会儿再取屏，
        # 否则拿到的是刚 resize 完的空白屏（herdr 靠 nudge+强制整帧解决同一问题）
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
        # herdr panes.rs:412：边框在**所有格子内容渲染之后**重画（否则被内容盖掉，
        # 表现为聚焦切换时高亮不更新）
        chrome_state["chromed"] = chromed
        chrome_state["area"] = term
        draw_borders()
        draw_sidebar()

    def focus(pane_id, *, force_layout=False):
        nonlocal focused
        changed = focused != pane_id
        focused = pane_id
        try:
            control.request("pane.focused", {"id": pane_id})   # 聚焦即已读
        except RuntimeError:
            pass
        # 缩放中换焦点＝换成新格子独占（herdr zoom 跟着聚焦格子走）
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
            if changed:
                # 聚焦变了：边框高亮跟着变（herdr 每帧重画边框，我们按需重画）
                for item in chrome_state["chromed"]:
                    item["focused"] = item["id"] == pane_id
                draw_borders()
            draw_sidebar()

    _FOCUSED = object()          # target 默认值：劈当前聚焦格子

    def new_pane(argv, title, *, split=None, target=_FOCUSED):
        """split=None 开新页；split="h"/"v" 在当前页劈开 target。
        target=_FOCUSED 劈聚焦格子（herdr split_focused）；target=None 在
        整棵树外包一层（新开一列，misaka 平铺策略用）。"""
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
                    # layout.rs:143 split_focused：只把聚焦叶子换成一个分割节点
                    pane_target = focused if target is _FOCUSED else target
                    tabs[index] = hui.split_at(tabs[index], pane_target,
                                               split, new_id, 0.5)
        else:
            tabs.append(("pane", new_id))
        save_layout()          # 分屏结构变了就存，面板退了也不散
        focus(new_id, force_layout=True)

    def switch_tab(index):
        nonlocal tab_follow, zoom
        if 0 <= index < len(tabs):
            tab_follow = True
            zoom = False           # herdr：缩放是页内状态，换页即退出缩放
            focus(hui.pane_ids(tabs[index])[0], force_layout=True)

    def close_focused():
        """关聚焦格子（herdr close_pane）：从当前页移除，页内有剩聚焦同页下一个，
        页空了 sync_tabs 清页、焦点落相邻页。返回是否已无活格子（该退出）。"""
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
        """滚轮：侧栏区内＝滚那个区；格子内＝回看（备用屏交给应用自己）。"""
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
                return          # 备用屏：全屏应用自己管滚动，不拦
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
        avail = cols - main_col + 1     # 宽度钳到主区（曾在窄屏外溢到屏幕右侧）
        top = max(2, (rows - len(lines)) // 2)
        left_col = main_col + max(0, (avail - _wcwidth(lines[0])) // 2)
        out = ["\x1b[?25l\x1b[?2026h"]
        for index, line in enumerate(lines):
            out.append(f"\x1b[{top + index};{left_col}H\x1b[0m{line}")
        out.append("\x1b[?2026l")
        paint("".join(out).encode())

    def on_rclick(x, y):
        """右键课题行＝置顶/归档/删；右键卡片行＝删卡。"""
        nonlocal menu_proj, menu_card
        if x > SIDEBAR_W:
            return
        target = (ui_map["targets"][y - 1]
                  if 0 <= y - 1 < len(ui_map["targets"]) else None)
        width = max(24, min(46, cols - main_col + 1))
        if target and target[0] == "card":
            menu_card = target[1]
            _popup(format_card_menu(menu_card, width=width))
            return
        if not target or target[0] != "proj" or target[1] is None:
            return                      # (未分类) 无实体目录，没有可操作项
        menu_proj = target[1]
        meta = next((p for p in projects_cache["items"]
                     if p["name"] == menu_proj), {})
        _popup(format_project_menu(menu_proj, bool(meta.get("pinned_at")),
                                   bool(meta.get("archived")), width=width))

    def on_click(x, y):
        nonlocal tab_scroll, tab_follow
        bar = ui_map.get("bar")
        if y == 1 and bar is not None:                # 标签行：herdr 热区路由
            cx = x - 1                                # Rect 是 0 起算
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
                new_pane([os.environ.get("SHELL", "sh")], "shell")   # ＋＝新页
            return
        if x <= SIDEBAR_W:                            # 侧栏：按目标表路由
            target = (ui_map["targets"][y - 1]
                      if 0 <= y - 1 < len(ui_map["targets"]) else None)
            if target is None:
                return
            kind, value = target
            if kind == "pane":
                # 不强制重排：格子已在屏上就只换高亮（focus 自己会判断在不在屏上）
                focus(value)
                return
            if kind == "proj":                        # 点课题＝展开/收回＋选中
                expanded_projects.symmetric_difference_update({value})
                side_selected[0] = ("proj", value)
                draw_sidebar()          # 行差分只写变化行，不整栏闪一遍
                return
            if kind == "card":
                # 在跑的聚焦；跑过的展开她的会话现场（--resume 不重发合同，
                # 纯看/手聊，不动看板状态）；从没跑过的没现场可看
                side_selected[0] = ("card", value)
                pane = next((p for p in listing
                             if p["card"] == value and p["alive"]), None)
                if pane:
                    focus(pane["id"])       # 在屏上＝只换高亮，不整区重刷
                    return
                viewer = next((p for p in listing     # 已开过的会话格子：聚焦别重开
                               if p["alive"] and p["title"].endswith(f"·{value}")),
                              None)
                if viewer:
                    focus(viewer["id"])
                    return
                card = next((c for p in projects_cache["items"]
                             for c in p["cards"] if c["id"] == value), None)
                if not card or not card.get("has_session"):
                    draw_sidebar()
                    bottom_note(f"{hui.sgr_fg(hui.OVERLAY1)}"
                                f"卡 {value} 还没跑过，没有会话可展开\x1b[0m")
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
            argv = [sys.executable, "-m", "misaka", "chat"]   # 名册：点开即启动她
            title = "Last Order" if value == "last-order" else value
            if value != "last-order":
                argv += ["--as", value]
            # 在**当前页内**按平铺策略开她的格子（每列两个，列满往右）
            index = active_tab()
            if index < len(tabs):
                target, direction = next_tile_placement(tabs[index])
                new_pane(argv, title, split=direction, target=target)
            else:
                new_pane(argv, title)
            return
        for pane_id, rect in slices:                  # 点格子＝聚焦（矩形命中）
            if (rect.x < x <= rect.x + rect.width
                    and rect.y < y <= rect.y + rect.height):
                focus(pane_id)
                return

    old_attrs = termios.tcgetattr(0)
    new_attrs = termios.tcgetattr(0)
    new_attrs[0] &= ~(termios.IXON | termios.ICRNL)
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    termios.tcsetattr(0, termios.TCSANOW, new_attrs)
    # 备用屏（herdr/一切正经 TUI 的标配，此前漏抄）：不进备用屏的话，
    # Terminal.app 会给每次回车的行自动打「标记」——渲染成行首[行尾]一对暗括号
    # ?1002＝按住时上报移动：herdr 的拖拽选区靠它（?1003 全程上报是给悬停
    # UI 用的，我们没有悬停效果，不开）
    _write_all(b"\x1b[?1049h\x1b[?7l\x1b[?1000;1002;1006h")
    prefix_pending = 0
    exit_reason = ["detached"]     # detached=主动分离；closed_all=格子关完了
    help_open = False
    menu_proj = None
    menu_card = None
    try:
        paint(b"\x1b[0m\x1b[2J")
        chrome_cache["rows"] = []          # 清屏后行缓存必须失效，否则侧栏一行都不画
        stream.send("pane.attach", {"id": "*"})
        load_layout()          # 续上上次的分屏（布局存在守护进程里）
        refresh_projects()
        focus(focused, force_layout=True)
        last_poll = last_blink = 0.0
        repaint_after_typing = 0.0   # 打字停下后补一次整格重画（擦掉输入法残留）
        while True:
            if resized.pop("hit", None):
                rows, cols = _term_size()
                paint(b"\x1b[0m\x1b[2J")
                chrome_cache["rows"] = []
                relayout()
            readable, _, _ = select.select([0, stream.sock], [], [], 0.05)
            now = time.monotonic()
            if repaint_after_typing and now > repaint_after_typing:
                # 中文输入法的拼音预编辑是**终端直接画上去的**：应用不知道、我们的
                # 仿真屏也没有它，撤销后那块没人负责恢复 → 看着像内容漂移。
                # 打字停下后照聚焦格子的真实内容补画一遍，把残留擦掉。
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
                if os.environ.get("MISAKA_PANEL_DEBUG"):
                    with open(DEBUG_LOG, "a", encoding="utf-8") as dbg:
                        dbg.write(f"{time.time():.3f} stdin {chunk!r}\n")
                # 鼠标：左键＝聚焦、拖拽＝选区、松手＝复制、双击＝选词、滚轮＝回看
                for m in _MOUSE.finditer(chunk):
                    button, mx, my = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)))
                    press = m.group(4) == b"M"
                    if button in (64, 65):
                        on_wheel(mx, my, -3 if button == 64 else 3)
                    elif button == 32 and press:            # 按住左键拖动
                        sl = slice_of(sel["pane"]) if sel["pane"] else None
                        if sl and sel["a"]:
                            rect = sl[1]   # 拖出格子外就贴边，别选到别人家去
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
                            if hit:      # 起点落在格子里才起选区，侧栏点击不算
                                sel.update(pane=hit[0], a=hit[1:], b=None)
                            on_click(mx, my)
                    elif press and button == 2:             # 右键
                        clear_selection()
                        on_rclick(mx, my)
                    elif not press and button == 0 and sel["b"]:
                        copy_selection()    # herdr copy_on_select：松手即入剪贴板
                chunk = _MOUSE.sub(b"", chunk)
                chunk = _MOUSE_X10.sub(b"", chunk)     # 老式报文只滤不用，别漏进格子当输入
                plain = bytearray()
                detach = False
                for offset in range(len(chunk)):       # 逐字节：前缀与命令可能同一笔到达
                    key = chunk[offset:offset + 1]
                    if help_open:                      # 键位面板开着：任意键关闭并重画
                        help_open = False
                        relayout()
                        continue
                    if menu_proj is not None:          # 课题菜单：1 置顶 2 归档 3 删
                        name, menu_proj = menu_proj, None
                        meta = next((p for p in projects_cache["items"]
                                     if p["name"] == name), {})
                        try:
                            if key == b"1":
                                control.request("project.set", {
                                    "name": name,
                                    "pinned": not meta.get("pinned_at")})
                            elif key == b"2":
                                control.request("project.set", {
                                    "name": name,
                                    "archived": not meta.get("archived")})
                            elif key == b"3":
                                control.request("project.delete", {"name": name})
                        except RuntimeError as error:
                            refresh_projects()
                            relayout()
                            bottom_note(f"\x1b[33m{error}\x1b[0m")
                            continue
                        refresh_projects()
                        relayout()
                        continue
                    if menu_card is not None:          # 卡菜单：d 删卡，其它键关
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
                            draw_prefix_bar()          # herdr：整条底行弹出 PREFIX 模式栏
                        else:
                            plain += key
                        continue
                    prefix_pending = 0
                    restore_bottom()                   # 模式栏退场，底行还给格子
                    if key == b"\x1b":                 # esc 取消前缀（herdr 同款）
                        continue
                    if key != PREFIX and 1 <= key[0] <= 26:
                        key = bytes([key[0] + 96])   # ctrl+d 等价 d：手滑不白按
                    if key == PREFIX:
                        plain += PREFIX
                    elif key in b"123456789":          # herdr：数字=切标签页
                        switch_tab(int(key) - 1)
                    elif key in b"np" and tabs:        # 轮换标签页
                        switch_tab((active_tab() + (1 if key == b"n" else -1))
                                   % len(tabs))
                    elif key in b"hjkl":              # herdr focus_pane_h/j/k/l（1051-1054）
                        target = hui.find_in_direction(
                            focused,
                            {b"h": "left", b"j": "down",
                             b"k": "up", b"l": "right"}[key],
                            [(pid, rect) for pid, rect in slices])
                        if target:
                            focus(target)
                    elif key == b"z":                 # herdr zoom（1065）：聚焦格子独占
                        zoom = not zoom
                        relayout()
                    elif key == b"c":                  # herdr new_tab（model.rs:1039）：新页
                        new_pane([os.environ.get("SHELL", "sh")], "shell")
                    elif key == b"v":                  # split_vertical（1062）：左右分
                        new_pane([os.environ.get("SHELL", "sh")], "shell", split="h")
                    elif key == b"-":                  # split_horizontal（1063）：上下分
                        new_pane([os.environ.get("SHELL", "sh")], "shell", split="v")
                    elif key == b"t":                  # 召唤全局树：有就聚焦；没有在当前页右侧劈出
                        action, val = tree_summon_action(panes())
                        if action == "focus":
                            focus(val)
                        else:
                            new_pane(val, TREE_TITLE, split="h")
                    elif key == b"d":
                        detach = True
                    elif key == b"x":
                        # herdr prefix+x = ClosePane：一个键直接关，零确认
                        # （actions.rs:2035 close_pane：确认仅在「会关掉 worktree 组」时触发，
                        # misaka 无 worktree 组 → 那条永不成立 → 一路直接关，关到最后一个退面板）
                        if close_focused():
                            exit_reason[0] = "closed_all"
                            detach = True
                    elif key == b"?":
                        help_open = True
                        draw_help_overlay()
                if plain:
                    control.request("pane.input", {
                        "id": focused, "data": base64.b64encode(bytes(plain)).decode()})
                    repaint_after_typing = now + 0.9   # 停手后补画，擦输入法残留
                if detach:
                    return

            if stream.sock in readable or stream.buf:
                painted = False
                while (line := stream.readline()) is not None:
                    msg = json.loads(line)
                    if msg.get("event") == "screen":
                        if "scroll" in msg:      # 读数随每帧更新（clear 会清历史）
                            scroll_state[msg["id"]] = {
                                "metrics": msg["scroll"],
                                "alt": msg.get("alt_screen", False)}
                        paint_rows(msg["id"], msg["rows"], msg.get("cursor"),
                                   hidden=msg.get("cursor_hidden"))
                        painted = True
                    elif msg.get("event") == "exited":
                        # 应用退场＝格子收场（直连同感）：收尸；全空则面板收摊回 shell
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
                    draw_borders()      # 内容盖过边框：herdr 每帧末尾重画

            if now - last_poll > POLL_SECONDS:
                last_poll = now
                roster_cache["names"] = ["last-order", *sorted(_sisters_roster())]
                refresh_projects()
                try:
                    listing = panes()
                except (RuntimeError, ConnectionError):
                    return
                dead = [p for p in listing if not p["alive"] and not p["card"]]
                if dead:      # 退场事件可能比订阅早、会漏——轮询兜底收尸
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
                # 渐变比状态轮询快：只重画侧栏（行差分，只有圆点那行真写字节）
                last_blink = now
                if any(p.get("busy") for p in listing):
                    draw_sidebar()
    except (RuntimeError, ConnectionError, json.JSONDecodeError) as error:
        sys.exit(f"面板断开：{error}")
    except Exception as error:   # noqa: BLE001
        # 别的异常以前直接抛栈＝用户看到的"闪退"。写进排障日志，给人一句人话。
        import traceback
        try:
            with open(DEBUG_LOG, "a", encoding="utf-8") as dbg:
                dbg.write(f"=== {time.time():.3f} 面板崩溃 ===\n"
                          + traceback.format_exc() + "\n")
        except OSError:
            pass
        sys.exit(f"面板出错退出：{type(error).__name__}: {error}\n"
                 f"（堆栈已记到 {DEBUG_LOG}；格子还活着，misaka 可以直接回来）")
    finally:
        termios.tcsetattr(0, termios.TCSANOW, old_attrs)
        # 退场：模式还原＋光标交还宿主终端，绝不能再追加归位（会把它藏回去）
        _write_all(b"\x1b[?1000;1002;1006l\x1b[?7h\x1b[0m\x1b[2J\x1b[?1049l\x1b[?25h")
        if exit_reason[0] == "closed_all":
            print("格子已全部关闭，面板退出（misaka 回来重开）")
        else:
            print("已分离——守护进程与格子照跑（misaka 回来，misaka net stop 全停）")


if __name__ == "__main__":
    fake = [
        {"id": "p1", "title": "Last Order", "card": None, "alive": True, "status": None},
        {"id": "p2", "title": "10032·t_ab", "card": "t_ab", "alive": True,
         "status": "running", "mail": 2},
        {"id": "p3", "title": "10033·t_cd", "card": "t_cd", "alive": True,
         "status": "done", "unseen": True},
        {"id": "p4", "title": "10044·t_ef", "card": "t_ef", "alive": True,
         "status": "done", "unseen": False},
    ]
    demo_projects = [
        {"name": "冷战经济", "archived": False, "pinned_at": 9.0,
         "cards": [{"id": "t_ab", "status": "running", "title": "档案扫一遍"},
                   {"id": "t_zz", "status": "done", "title": "旧结论复核"}]},
        {"name": "苏联档案", "archived": False, "pinned_at": None, "cards": []},
        {"name": "老课题", "archived": True, "pinned_at": None, "cards": []},
    ]
    lines, targets, spans = format_sidebar(
        fake, "p2", rows=24, roster=["last-order", "10032", "10099"],
        projects=demo_projects, expanded={"冷战经济"})
    assert len(lines) == len(targets) == 24, "侧栏全高（herdr：整列属于侧栏）"
    joined = "\n".join(lines)
    assert "Sisters" in joined and "Projects" in joined and "Windows" in joined
    assert targets[1] == ("pane", "p1"), "在跑的 Last Order：名册点击=聚焦她的格子"
    assert targets[2] == ("pane", "p2"), "10032 有活格子（卡）：点击=聚焦"
    assert targets[3] == ("roster", "10099"), "没在跑的：点击=启动"

    # 临时御坂（协力者）：在 misaka 里跑着才进名册，★ 主题色，进程结束即消失
    with_ally = [*fake, {"id": "p9", "title": "codex·协力者", "card": None,
                         "alive": True, "status": None, "busy": True, "ally": "codex"}]
    plines, ptargets, _ps = format_sidebar(with_ally, "p2", rows=24,
                                           roster=["last-order"], projects=[])
    pjoin = "\n".join(plines)
    assert "★" in pjoin and hui.sgr_fg(hui.ACCENT) in pjoin, "在跑的协力者＝主题色★"
    assert ("pane", "p9") in ptargets, "协力者行可点＝聚焦它的格子"
    idle_ally = [dict(with_ally[-1], busy=False)]
    assert "☆" in "\n".join(format_sidebar(idle_ally, "x", rows=10)[0]), "闲着＝空心☆"
    dead_ally = [dict(with_ally[-1], alive=False)]
    gone = "\n".join(format_sidebar(dead_ally, "x", rows=10)[0])
    assert "★" not in gone and "☆" not in gone, "进程结束＝从名册消失"
    # 在 misaka 格子的 shell 里手起的也算（守护进程 _ally_name 已判好，面板只认字段）
    hand_started = [{"id": "p8", "title": "shell", "card": None, "alive": True,
                     "status": None, "busy": True, "ally": "codex"}]
    assert "★" in "\n".join(format_sidebar(hand_started, "x", rows=10)[0]), \
        "格子里手敲起来的第三方 agent 同样进名册"
    plain_shell = [{"id": "p7", "title": "shell", "card": None, "alive": True,
                    "status": None, "busy": False, "ally": None}]
    assert "★" not in "\n".join(format_sidebar(plain_shell, "x", rows=10)[0]), \
        "光是 shell 没跑 agent＝不进名册"
    plain_all = re.sub(r"\x1b\[[0-9;]*m", "", joined)
    assert "▾ ★冷战经济" in plain_all, "置顶星＋展开箭头"
    assert "▸ 苏联档案" in plain_all and "▸ 老课题" in plain_all
    assert "归档" in plain_all, "已归档课题带灰字标"
    assert ("card", "t_ab") in targets, "展开后卡片行可点（在跑的聚焦）"
    assert ("proj", "老课题") in targets, "归档课题也在列表（用户裁定：都显示）"
    card_row = plain_all.splitlines()[targets.index(("card", "t_ab"))]
    assert "● 档案扫一遍" in card_row, "卡片行＝状态字形＋标题"
    done_line = lines[targets.index(("card", "t_zz"))]
    assert hui.sgr_fg(_P["accent"]) + "✓" in done_line, "完成对勾＝主题玫红（暗色留给未完成）"
    ready_demo = [{"name": "x", "archived": False, "pinned_at": None,
                   "cards": [{"id": "t_r", "status": "ready", "title": "没跑的"}]}]
    ready_lines, ready_targets, _sp = format_sidebar(
        [], "none", rows=10, projects=ready_demo, expanded={"x"})
    assert hui.sgr_fg(hui.OVERLAY0) + "○" in ready_lines[ready_targets.index(("card", "t_r"))], \
        "未完成（ready）＝暗色圆圈"

    # Projects 行选中高亮（Windows 同款 surface0 底、罩整条、左右留空）
    sel_lines, sel_targets, _sp2 = format_sidebar(
        fake, "p2", rows=24, roster=["last-order"], projects=demo_projects,
        expanded={"冷战经济"}, selected=("card", "t_ab"))
    sel_row = sel_lines[sel_targets.index(("card", "t_ab"))]
    assert hui.sgr_bg(hui.SURFACE0) in sel_row, "点选的卡行有选中底色"
    sel_plain = re.sub(r"\x1b\[[0-9;]*m", "", sel_row)
    assert sel_plain.startswith(" ") and _wcwidth(sel_plain) == SIDEBAR_W, "罩整条留边距"
    sel_lines2, sel_targets2, _sp3 = format_sidebar(
        fake, "p2", rows=24, roster=["last-order"], projects=demo_projects,
        expanded=set(), selected=("proj", "苏联档案"))
    assert hui.sgr_bg(hui.SURFACE0) in sel_lines2[sel_targets2.index(("proj", "苏联档案"))], \
        "点选的课题行有选中底色"
    assert [s["key"] for s in spans] == ["sisters", "windows", "projects"], \
        "Projects 放最下面（用户裁定）"
    assert plain_all.index("Windows") < plain_all.index("Projects"), "顺序体现在渲染"

    # 弹窗行宽严格＝width：长 CJK 名、窄屏都不外溢
    for menu_w in (46, 30, 24):
        menu = format_project_menu("冷战经济与超长课题名称测试", True, False,
                                   width=menu_w)
        widths = {_wcwidth(_l) for _l in menu}
        assert widths == {menu_w}, f"菜单宽度们 {widths}≠{menu_w}"
    for help_w in (46, 24):
        widths = {_wcwidth(_l) for _l in format_help_lines(width=help_w)}
        assert widths == {help_w}, f"键位面板宽度们 {widths}≠{help_w}"
    assert "取消置顶" in "".join(format_project_menu("x", True, False))
    assert "恢复进行中" in "".join(format_project_menu("x", False, True))
    for menu_w in (46, 30, 24):
        cm = format_card_menu("t_abc123", width=menu_w)
        assert {_wcwidth(_l) for _l in cm} == {menu_w}, "卡菜单不外溢"
    assert "删除" in "".join(format_card_menu("t_abc123")), "卡菜单有删除项"
    assert "✉2" in joined

    # 分区高度分配：小区先喂饱，剩余给大户；滚动时窗口截取＋末列滚动条
    assert _alloc_sections(24, [3, 6, 4]) == [3, 6, 4], "放得下＝按需"
    assert _alloc_sections(10, [3, 30, 4]) == [2, 3, 3], \
        "挤时水位填充：都超额＝近似均分（各自出滚动条）"
    assert _alloc_sections(20, [3, 30, 4]) == [3, 11, 4], \
        "小区喂饱，剩余全给大户"
    small_lines, _t2, small_spans = format_sidebar(
        fake, "p2", rows=12, roster=["last-order", "10032", "10099"],
        projects=demo_projects, expanded={"冷战经济"},
        scrolls={"projects": 1})
    proj_span = next(s for s in small_spans if s["key"] == "projects")
    assert proj_span["total"] > proj_span["height"], "内容超高"
    assert proj_span["scroll"] == 1, "滚动位生效"
    assert "▕" in "\n".join(small_lines), "超高的区末列有滚动条"

    # CJK 溢出回归（用户截图抓获的 bug）：长汉字标题绝不许宽过侧栏
    cjk_projects = [{"name": "冷战经济", "archived": False, "pinned_at": None,
                     "cards": [{"id": "t_c1", "status": "done",
                                "title": "考证御坂网络被第三方大规模克隆体脑利用的两个案例"},
                               {"id": "t_c2", "status": "running",
                                "title": "信息与规模数字全链验证以及更多更多字"}]}]
    cjk_panes = [{"id": "p8", "title": "御坂网络超长汉字标题格子", "card": None,
                  "alive": True, "status": None, "busy": True}]
    cjk_lines, _t3, _s3 = format_sidebar(
        cjk_panes, "p8", rows=14, roster=["超长汉字名妹妹编号一号"],
        projects=cjk_projects, expanded={"冷战经济"})
    for _l in cjk_lines:
        w = _wcwidth(re.sub(r"\x1b\[[0-9;]*m", "", _l))
        assert w == SIDEBAR_W, f"行宽必须恰好 {SIDEBAR_W}，实得 {w}：{_l!r}"
    assert "working" not in joined, "不再无脑写 working（只有真在跑才亮圆点）"
    assert "●" in joined or "○" in joined, "在跑的格子有圆点"
    assert joined.count("done") == 2, "两个终态卡各有状态词（一个亮一个暗）"
    assert hui.sgr_fg(_P["green"]) + "done" in joined, "没看过的完成＝绿（misaka 主题）"
    assert hui.sgr_bg(hui.SURFACE0) in joined, "聚焦项＝selectedBg 底（不是反白）"

    # 运行指示：在跑＝呼吸渐变圆点，没跑＝空白
    assert busy_dot(False, 1.0) == " " and busy_dot(False, 0.0) == " ", "没跑啥都不显示"
    dim, bright = busy_dot(True, 0.0), busy_dot(True, 1.0)
    assert "●" in dim and "●" in bright, "在跑＝圆点（形状不变，靠颜色呼吸）"
    assert dim != bright, "两端颜色不同才是渐变"
    assert hui.sgr_fg(DOT_DIM) in dim and hui.sgr_fg(DOT_BRIGHT) in bright, "绿色呼吸"
    assert DOT_BRIGHT[1] > DOT_BRIGHT[0] and DOT_BRIGHT[1] > DOT_BRIGHT[2], "确实是绿"
    assert "\x1b[0m" not in dim, "圆点不带 reset（否则会打断选中底色）"
    mid = busy_dot(True, 0.5)
    assert mid not in (dim, bright), "中间是插值色，不是二值跳变"
    assert 0.49 < breath_level(0.4, period=1.6) < 1.01 and breath_level(0, 1.6) == 0.5
    assert abs(breath_level(0.4, 1.6) - 1.0) < 1e-9, "四分之一周期到最亮（正弦）"
    idle_only = [{"id": "p9", "title": "shell", "card": None, "alive": True,
                  "status": None, "busy": False}]
    quiet, _t, _s = format_sidebar(idle_only, "p9", rows=10)
    assert "●" not in "".join(quiet), "闲置格子无指示"
    running = [{"id": "p9", "title": "shell", "card": None, "alive": True,
                "status": None, "busy": True}]
    lines_run, _t, _s = format_sidebar(running, "p9", rows=10)
    plain_run = re.sub(r"\x1b\[[0-9;]*m", "", "".join(lines_run))
    assert "1. ●" in plain_run, "序号带点，且与圆点留空"
    focus_line = next(_l for _l in lines_run if hui.sgr_bg(hui.SURFACE0) in _l)
    plain_focus = re.sub(r"\x1b\[[0-9;]*m", "", focus_line)
    assert _wcwidth(plain_focus) == SIDEBAR_W, \
        f"整行宽度＝侧栏宽 {SIDEBAR_W}，实得 {_wcwidth(plain_focus)}"
    assert plain_focus.startswith(" ") and plain_focus.endswith(" "), "左右各留空隙"
    assert focus_line.index(hui.sgr_bg(hui.SURFACE0)) > 0, "选择框从第 2 列才开始"
    assert plain_focus.rstrip().endswith(("Order", "shell")) or "  " in plain_focus, \
        "选择框内补满空格（罩住整条而不只是文字）"
    agent_rows = [t for t in targets if t and t[0] == "pane"]
    assert ("pane", "p4") in agent_rows, "Windows 区每行都有格子目标"

    names = ["Last Order", "10032·t_ab", "10033·t_cd"]
    area = hui.Rect(25, 0, 90, 1)
    bar = hui.compute_tab_bar_view(names, 1, area, 0, True, True)
    line = render_tab_bar(names, 1, bar, area, 0)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
    assert "Last Order" in plain and "10032·t_ab" in plain, "标签=agent 名（herdr 版式）"
    assert hui.sgr_bg(hui.ACCENT) in line, "活动页＝accent 底（misaka 玫红）"
    assert hui.sgr_bg(hui.SURFACE0) in line, "非活动页＝surface0 底"
    assert " + " in plain and bar.new_tab_hit_area.width == hui.NEW_TAB_WIDTH, "加号"
    assert _wcwidth(plain) == area.width, f"整行铺满 {area.width}，实得 {_wcwidth(plain)}"
    assert plain.index("Last Order") > plain.index(" "), "标签文字居中（左侧有留白）"

    many = [f"agent-{i}" for i in range(12)]           # 溢出：滚动钮＋省略号
    narrow = hui.Rect(0, 0, 46, 1)
    bar2 = hui.compute_tab_bar_view(many, 11, narrow, 0, True, True)
    line2 = re.sub(r"\x1b\[[0-9;]*m", "", render_tab_bar(many, 11, bar2, narrow,
                                                         bar2.scroll))
    assert " < " in line2 and " > " in line2, "溢出时出滚动钮"
    assert "…" in line2, "被截的一侧画省略号"
    assert " + " in line2, "加号仍在"

    # 平铺策略：每列两个，列满往右开新列
    tree = ("pane", "A")
    grown = ["A"]
    for step in range(2, 7):
        target, direction = next_tile_placement(tree)
        new_id = chr(ord("A") + step - 1)
        tree = (hui.split_root(tree, direction, new_id) if target is None
                else hui.split_at(tree, target, direction, new_id, 0.5))
        grown.append(new_id)
    rects = {pid: r for pid, r, _f in hui.collect_panes(tree, hui.Rect(0, 0, 90, 40), "A")}
    assert hui.pane_ids(tree) == grown == list("ABCDEF")
    assert rects["A"].x == rects["B"].x and rects["A"].y < rects["B"].y, "1/2 上下同列"
    assert rects["C"].x > rects["A"].x, "第 3 个往右开新列"
    assert rects["C"].x == rects["D"].x and rects["C"].y < rects["D"].y, "3/4 上下同列"
    assert rects["E"].x > rects["C"].x, "第 5 个再开一列"
    assert rects["E"].x == rects["F"].x and rects["E"].y < rects["F"].y, "5/6 上下同列"
    assert len({r.x for r in rects.values()}) == 3, "6 个格子＝3 列"

    metrics = {"offset_from_bottom": 0, "max_offset_from_bottom": 90,
               "viewport_rows": 10}
    sb = draw_scrollbar(metrics, hui.Rect(39, 0, 1, 10), True)
    assert "▕" in sb and "▐" in sb, "轨道 ▕＋聚焦滑块 ▐"
    assert sb.count("▐") == 1, "历史多＝滑块 1 行"
    assert hui.sgr_fg(hui.OVERLAY1) in sb, "聚焦滑块用 overlay1"
    unfocused = draw_scrollbar(metrics, hui.Rect(39, 0, 1, 10), False)
    assert "▐" not in unfocused and unfocused.count("▕") == 11, "非聚焦滑块也是 ▕"
    assert draw_scrollbar({"offset_from_bottom": 0, "max_offset_from_bottom": 0,
                           "viewport_rows": 10}, hui.Rect(39, 0, 1, 10), True) == "", \
        "没历史不画"

    bar = format_prefix_bar(120)
    plain_bar = re.sub(r"\x1b\[[0-9;]*m", "", bar)
    assert " PREFIX " in plain_bar and "原样发送" in plain_bar, "徽章与提示"
    assert "新页" in plain_bar and "分屏" in plain_bar, "键位＝herdr 语义（c 新页 / v 分屏）"
    assert _wcwidth(plain_bar) == 120, f"底栏正好铺满，实得 {_wcwidth(plain_bar)}"
    narrow_bar = re.sub(r"\x1b\[[0-9;]*m", "", format_prefix_bar(40))
    assert _wcwidth(narrow_bar) == 40, "窄屏也不溢出（放不下的键位直接不放）"
    assert " PREFIX " in narrow_bar, "再窄也保留徽章"
    help_lines = format_help_lines()
    widths = {_wcwidth(_l) for _l in help_lines}
    assert len(widths) == 1, f"键位面板边框要对齐：宽度们 {widths}"
    assert any("分屏" in _l for _l in help_lines)

    tree = hui.split_at(("pane", "A"), "A", "h", "B", 0.5)
    placed = [{"id": p, "rect": r, "focused": f}
              for p, r, f in hui.collect_panes(tree, hui.Rect(0, 0, 60, 20), "A")]
    border = draw_pane_borders(hui.apply_pane_chrome(placed))
    plain_border = re.sub(r"\x1b\[[0-9;]*m", "", border)
    assert plain_border.count("┌") == 2 and plain_border.count("┘") == 2, \
        "每格自己一个完整框（pane_gaps 默认开），不共享边不画丁字"
    assert "┼" not in plain_border and "┬" not in plain_border
    assert hui.sgr_fg(hui.ACCENT) in border, "聚焦格子用 accent（Catppuccin 蓝）"
    assert hui.sgr_fg(hui.OVERLAY0) in border, "非聚焦格子用 overlay0（灰）"
    assert draw_pane_borders([{"id": "A", "rect": hui.Rect(0, 0, 60, 20),
                               "focused": True, "borders": set()}]) == "", "单格无边框"
    print("panel selfcheck ok — 侧栏/标签栏 + PREFIX 底栏 + 键位面板（页内布局走 herdr_ui BSP 树）")
