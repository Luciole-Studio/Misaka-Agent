"""单会话迷宫 TUI（dsh-trace-compare 迷宫视图的终端化，数据内核 trace_lanes）。

视觉语言对应上游：主干=判定色时长条（宽∝耗时，条间 ─ 连接），支路=上下泳道行
（↳出程＋条＋标注＋↩折返），┊=换轮分隔，⏸=空闲折叠缝，底行=墙钟刻度。
上游的悬停/点击在终端换成键盘：←→（或 h/l）选步（详情区常驻显示选中步），
f 只看失败/重试，/ 搜索命令与返回，+/- 以选中步为中心缩放，0 复位，q 退出。
--watch＝现场文件变了自动重析重画（正在跑的卡实时生长）。

帧渲染是纯函数（render_frame），交互循环只管键与重画——selfcheck 直接断言帧。
"""
import os
import sys

from misaka.orchestration.trace_lanes import (
    fmt_det_diff, fmt_t, turn_detour_stats, turn_end_nodes, union_turns,
    wall_clock)

V_COLOR = {"ok": "\x1b[32m", "answer": "\x1b[32m", "error": "\x1b[31m",
           "deadend": "\x1b[90m", "retry": "\x1b[90m"}
V_GLYPH = {"ok": "✓", "answer": "●", "error": "✗", "deadend": "·", "retry": "↻"}
DIM, INV, RESET = "\x1b[2m", "\x1b[7m", "\x1b[0m"
X0 = 2                      # 左边距（泳道行首的 ↑↓ 区标记位）
MIN_SPAN = 0.5


def node_matches(n, query):
    """上游 nodeMatches 的搜索段：命令/返回全文/思考摘要包含（小写）。"""
    q = query.lower()
    for t in n["tools"]:
        if q in str(t.get("args") or "").lower():
            return True
        if q in str(t.get("resFull") or t.get("res") or "").lower():
            return True
    return q in str(n.get("rzTxtFull") or n.get("rzTxt") or "").lower()


def _visible(n, filt_fail, query):
    if filt_fail and n["v"] in ("ok", "answer"):
        return False
    if query and not node_matches(n, query):
        return False
    return True


class Frame:
    """一帧的字符画布：末列裁剪、ANSI 色在格子级拼接。"""

    def __init__(self, width):
        self.width = width
        self.rows = []

    def line(self):
        self.rows.append([" "] * self.width)
        return self.rows[-1]

    def put(self, row, col, text, color=""):
        for i, ch in enumerate(str(text)):
            c = col + i
            if 0 <= c < self.width:
                row[c] = f"{color}{ch}{RESET}" if color else ch

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.rows)


def _pack(items):
    """支路行贪心装箱：不重叠同行、重叠加行。items=[(s_col, end_col, node)]，
    返回 [(row_idx, s_col, node)]。"""
    rows_end = []
    out = []
    for s, e, n in sorted(items, key=lambda x: x[0]):
        for i, last in enumerate(rows_end):
            if s > last + 1:
                rows_end[i] = e
                out.append((i, s, n))
                break
        else:
            rows_end.append(e)
            out.append((len(rows_end) - 1, s, n))
    return out


def _make_x(view, tmax, width):
    t0 = view[0] if view else 0.0
    span = max((view[1] - view[0]) if view else tmax, MIN_SPAN)
    usable = width - X0 - 1
    return (lambda t: X0 + int((t - t0) / span * usable)), t0, span, usable


def render_frame(lane, time_map, tmax, width, *, view=None, sel_step=None,
                 filt_fail=False, query="", with_ticks=True, t_cursor=None,
                 title=None):
    """一条泳道的迷宫帧。view=[t0,t1]（压缩坐标）或 None=整图。
    t_cursor＝播放光标（压缩坐标）：未到的节点变暗。返回 Frame。"""
    x, t0, span, usable = _make_x(view, tmax, width)

    active = filt_fail or bool(query)

    def shade(n, base_color):
        if t_cursor is not None and n["s"] > t_cursor:
            return DIM
        if sel_step is not None and n["step"] == sel_step:
            return INV + base_color
        if active and not _visible(n, filt_fail, query):
            return DIM
        return base_color

    f = Frame(width)
    st = lane["stats"]
    tok = (f"输出 {st['outTok']:,}tok" if st["outTok"] is not None
           else f"{st['rz']} 段推理")
    turns = len({n["turn"] for n in lane["main"] + lane["detours"]})
    head = f.line()
    f.put(head, 0,
          f"{title + ' · ' if title else ''}{lane['model'] or '未知模型'} · "
          f"{'%d 轮 · ' % turns if turns > 1 else ''}{st['steps']} 步"
          f"（主干 {st['main']} / 支路 {st['detours']}）· {st['tools']} 工具 · "
          f"{tok} · 总 {fmt_t(st['T'])}")

    ups, downs = [], []
    for i, d in enumerate(lane["detours"]):
        label = f"↳{V_GLYPH[d['v']]}{(d['tools'][0]['name'] if d['tools'] else '?')}" \
                f" {fmt_t(max(d['e'] - d['s'], 0))}↩"
        s_col, e_col = x(d["s"]), x(d["e"])
        if e_col < X0 or s_col > width - 1:
            continue
        (ups if i % 2 == 0 else downs).append(
            (s_col, max(e_col, s_col + 1) + len(label), (d, label)))
    up_rows = _pack(ups)
    down_rows = _pack(downs)

    def draw_detour_rows(packed, count):
        grid = [f.line() for _ in range(count)]
        for row_i, s_col, (d, label) in packed:
            color = shade(d, V_COLOR[d["v"]])
            bar_w = max(x(d["e"]) - s_col, 1)
            f.put(grid[row_i], s_col, "▓" * bar_w, color)
            f.put(grid[row_i], s_col + bar_w, label, color)
        return grid

    n_up = max((r for r, _, _ in up_rows), default=-1) + 1
    draw_detour_rows(up_rows, n_up)

    mainrow = f.line()
    for c in range(X0, width - 1):
        f.put(mainrow, c, "─", DIM)
    seams = []
    if time_map:
        for gp in time_map["gaps"]:
            c = x(gp["c"])
            if X0 <= c < width:
                seams.append((c, gp["skipped"]))
    prev = None
    for n in lane["main"]:
        s_col, e_col = x(n["s"]), x(n["e"])
        if e_col < X0 or s_col > width - 1:
            prev = n
            continue
        if prev is not None and prev["turn"] != n["turn"]:
            f.put(mainrow, max(s_col - 1, X0), "┊", DIM)
        bar_w = max(e_col - s_col, 1)
        color = shade(n, V_COLOR[n["v"]])
        ch = "●" if n["v"] == "answer" else "█"
        f.put(mainrow, s_col, ch * bar_w, color)
        prev = n
    for c, _skipped in seams:
        f.put(mainrow, c, "⏸", "\x1b[33m")

    n_down = max((r for r, _, _ in down_rows), default=-1) + 1
    draw_detour_rows(down_rows, n_down)

    if with_ticks:
        tick = f.line()
        n_ticks = max(2, usable // 14)
        for i in range(n_ticks + 1):
            t = t0 + span * i / n_ticks
            label = fmt_t(wall_clock(t, time_map))
            col = x(t)
            f.put(tick, min(col, width - len(label) - 1), label, DIM)
        for c, skipped in seams:
            f.put(tick, c, f"⏸{fmt_t(skipped)}", "\x1b[33m")
    return f


def render_align(lane_a, lane_b, time_map, tmax, width, view=None):
    """双泳道间的轮次对齐行（上游对齐线的一行化）：只连两边都有的轮，
    ⚑N Δ时差 支路a↔b 落在两边回答节点的中点列。"""
    x, _, _, _ = _make_x(view, tmax, width)
    ends_a, ends_b = turn_end_nodes(lane_a), turn_end_nodes(lane_b)
    det_a = turn_detour_stats(lane_a, time_map)
    det_b = turn_detour_stats(lane_b, time_map)
    f = Frame(width)
    row = f.line()
    for turn in union_turns([lane_a, lane_b]):
        na, nb = ends_a.get(turn), ends_b.get(turn)
        if na is None or nb is None:
            continue
        wa = wall_clock(na["e"], time_map)
        wb = wall_clock(nb["e"], time_map)
        da = det_a.get(turn, {}).get("n", 0)
        db = det_b.get(turn, {}).get("n", 0)
        label = f"⚑{turn} Δ{fmt_t(abs(wb - wa))}" + (f" 支路{da}↔{db}" if da + db else "")
        col = x((na["e"] + nb["e"]) / 2) - len(label) // 2
        f.put(row, max(0, min(col, width - len(label))), label, "\x1b[36m")
    return f


def render_inventory(data, sel_idx, width):
    """支路盘点表（上游 renderInventory 的终端化）：行=轮，列=两泳道＋差额。
    返回 (行列表, 轮次列表)。sel_idx 高亮行（Enter 缩放到该轮）。"""
    lanes = data["lanes"]
    tm = data["timeMap"]
    det_a = turn_detour_stats(lanes[0], tm)
    det_b = turn_detour_stats(lanes[1], tm)
    turns = union_turns(lanes)

    def cell(g):
        if not g:
            return "—"
        by = g["by"]
        seg = "".join(f"{V_GLYPH[k]}{v}" for k, v in
                      (("error", by["error"]), ("retry", by["retry"]),
                       ("deadend", by["deadend"])) if v)
        return f"{g['n']} 步·{fmt_t(g['T'])}" + (f" {seg}" if seg else "")

    from misaka.cli.herdr_ui import display_width

    def pad(s, w):   # CJK 感知补白（f-string 按字符数补会让含全宽字的列错位）
        return s + " " * max(1, w - display_width(s))

    w1, w2 = 10, 26
    out = ["支路盘点（按轮次）——↑↓选轮 Enter缩放到该轮 i返回", ""]
    out.append(pad("轮次", w1) + pad("第 1 会话", w2) + pad("第 2 会话", w2) + "差额")
    ta = {"n": 0, "T": 0.0}
    tb = {"n": 0, "T": 0.0}
    for i, turn in enumerate(turns):
        a, b = det_a.get(turn), det_b.get(turn)
        if a:
            ta["n"] += a["n"]
            ta["T"] += a["T"]
        if b:
            tb["n"] += b["n"]
            tb["T"] += b["T"]
        diff = fmt_det_diff((b["T"] if b else 0) - (a["T"] if a else 0), bool(a or b))
        line = pad(f"第 {turn} 轮", w1) + pad(cell(a), w2) + pad(cell(b), w2) + diff
        out.append(f"{INV}{line}{RESET}" if i == sel_idx else line)
    total_diff = (fmt_det_diff(tb["T"] - ta["T"], True)
                  if ta["n"] + tb["n"] > 0 else "两边都无支路")
    out.append(pad("合计", w1) + pad(f"{ta['n']} 步·{fmt_t(ta['T'])}", w2)
               + pad(f"{tb['n']} 步·{fmt_t(tb['T'])}", w2) + total_diff)
    out.append("")
    out.append(DIM + "支路耗时为墙钟；✗失败 ↻无效重试 ·扑空；只一边有的轮显 —（缺席本身是信号）" + RESET)
    return out, turns


def detail_lines(n, time_map, width, is_main):
    """选中步的详情（上游详情面板的文本化）。"""
    kind = ("最终回答" if n["v"] == "answer" else "主干推进") if is_main else "探索支路"
    lab = {"error": "失败 ✗", "deadend": "扑空 ·", "retry": "无效重试 ↻",
           "ok": "成功", "answer": "回答"}[n["v"]]
    out = [f"S{n['step']} · 第 {n['turn']} 轮 · {kind} · {lab}"
           + (f" · 自 S{n['attach']} 分叉并折返" if not is_main else "")]
    if n.get("why"):
        out.append(f"判定依据：{n['why']}")
    rz = f"{n['rzTok']:,}tok 推理" if n.get("rzTok") is not None else f"{n['rz']} 段推理"
    out.append(f"时段 {fmt_t(wall_clock(n['s'], time_map))} → "
               f"{fmt_t(wall_clock(n['e'], time_map))}"
               f"（{fmt_t(max(n['e'] - n['s'], 0))}）· {rz}")
    for t in n["tools"]:
        mark = V_GLYPH.get(t["v"], "") if t["e"] is not None else "…跑着"
        out.append(f"  {t['name']} ({fmt_t(t['dur']) if t['e'] is not None else '?'})"
                   f" {mark}  {str(t['args'])[: width - 20]}")
        if t.get("why"):
            out.append(f"    判定：{t['why'][: width - 8]}")
        res = t.get("resFull") or t.get("res") or ""
        if res:
            out.append(f"    返回：{res[: width - 8]}")
    if n.get("rzTxt"):
        out.append(f"  思考：{n['rzTxt'][: width - 6]}")
    return out


def locate_session(con, target):
    """目标 → 会话文件路径：直接路径｜卡号｜角色名（last-order/编号）。"""
    from misaka.core.session_manager import find_most_recent_session
    if os.path.isfile(os.path.expanduser(target)):
        return os.path.expanduser(target), None
    from misaka.extensions.board import db
    row = db.get(con, target)
    if row is not None:
        sf = row["session_file"]
        if sf and os.path.isfile(sf):
            return sf, None
        if row["workspace"]:
            sf = find_most_recent_session(os.path.join(row["workspace"], "session"))
            if sf:
                return sf, None
        return None, f"卡 {target} 还没有会话现场"
    role_dir = os.path.expanduser(f"~/.misaka/sessions/{target.replace('_', '-')}")
    if os.path.isdir(role_dir):
        sf = find_most_recent_session(role_dir)
        if sf:
            return sf, None
        return None, f"{target} 的会话目录是空的"
    return None, f"不认识的目标：{target}（可给卡号／角色名／会话文件路径）"


def _load(paths):
    from misaka.orchestration.trace_lanes import build_data
    texts = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
    return build_data(texts)


def _nodes_in_order(lane):
    return sorted(lane["main"] + lane["detours"], key=lambda n: n["step"])


def _lane_title(data, i):
    return None if len(data["lanes"]) == 1 else f"第 {i + 1} 会话"


def run(paths, *, watch=False, plain=False):
    """迷宫入口：paths 1 个=单会话，2 个=同轴对比。plain（或非 TTY）＝打一帧退出。"""
    data = _load(paths)
    cols = os.get_terminal_size().columns if sys.stdout.isatty() else 120
    if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
        parts = []
        for i, lane in enumerate(data["lanes"]):
            parts.append(render_frame(
                lane, data["timeMap"], data["Tmax"], cols,
                with_ticks=(i == len(data["lanes"]) - 1),
                title=_lane_title(data, i)).text())
            if len(data["lanes"]) == 2 and i == 0:
                parts.append(render_align(*data["lanes"], data["timeMap"],
                                          data["Tmax"], cols).text())
        print("\n".join(parts))
        lane = data["lanes"][0]
        nodes = _nodes_in_order(lane)
        if len(data["lanes"]) == 1 and nodes:
            print("\n" + "\n".join(detail_lines(
                nodes[-1], data["timeMap"], cols, nodes[-1]["v"] in ("ok", "answer"))))
        return 0
    return _interactive(paths, data, watch)


SPEEDS = (5, 25, 100, 300)


def _interactive(paths, data, watch):
    import select
    import termios
    import tty

    lanes = data["lanes"]
    per_lane = [_nodes_in_order(l) for l in lanes]
    two = len(lanes) == 2
    cur = 0
    sels = [len(ns) - 1 for ns in per_lane]
    view = None
    filt_fail = False
    query = ""
    mode = "maze"          # maze | inv（盘点表，仅对比档）
    inv_sel = 0
    playing = False
    speed_i = 1            # SPEEDS[1]=25×
    t_cursor = None
    mtimes = [os.stat(p).st_mtime_ns for p in paths]

    def turn_extent(turn):
        s, e = float("inf"), float("-inf")
        for lane in lanes:
            for n in lane["main"] + lane["detours"]:
                if (n.get("turn") or 1) != turn:
                    continue
                s, e = min(s, n["s"]), max(e, n["e"])
        if s >= e:
            return None
        pad = max((e - s) * 0.06, 2)
        return [s - pad, e + pad]

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while True:
            size = os.get_terminal_size()
            nodes = per_lane[cur]
            node = nodes[sels[cur]] if nodes else None
            if mode == "inv":
                inv_lines, inv_turns = render_inventory(data, inv_sel, size.columns)
                body = "\n".join(inv_lines[: size.lines - 2])
                status = "↑↓选轮 Enter缩放该轮 i返回 q退出"
            else:
                parts = []
                for i, lane in enumerate(lanes):
                    parts.append(render_frame(
                        lane, data["timeMap"], data["Tmax"], size.columns,
                        view=view, with_ticks=(i == len(lanes) - 1),
                        sel_step=(node["step"] if node is not None and i == cur else None),
                        filt_fail=filt_fail, query=query, t_cursor=t_cursor,
                        title=_lane_title(data, i)).text())
                    if two and i == 0:
                        parts.append(render_align(
                            *lanes, data["timeMap"], data["Tmax"],
                            size.columns, view=view).text())
                out = ["\n".join(parts), ""]
                if node is not None and t_cursor is None:
                    is_main = any(n is node for n in lanes[cur]["main"])
                    out += detail_lines(node, data["timeMap"], size.columns, is_main)
                hits = ""
                if filt_fail or query:
                    vis = sum(1 for ns in per_lane for n in ns
                              if _visible(n, filt_fail, query))
                    total = sum(len(ns) for ns in per_lane)
                    hits = f" · 命中 {vis}/{total}"
                play = (f" ▶{fmt_t(wall_clock(t_cursor, data['timeMap']))}"
                        f" {SPEEDS[speed_i]}×" if t_cursor is not None else "")
                status = (f"←→选步{'（泳道' + str(cur + 1) + '，↑↓切）' if two else ''}"
                          f" f失败{'●' if filt_fail else '○'} /搜索"
                          f"{('「' + query + '」') if query else ''}"
                          f" +-缩放 0整图 p播放{play} []调速"
                          f"{' i盘点' if two else ''} q退出"
                          f"{hits}{' · watch' if watch else ''}")
                body = "\n".join(out)
            lines = body.splitlines()[: size.lines - 2]
            sys.stdout.write("\x1b[H" + "\n".join(l + "\x1b[K" for l in lines)
                             + "\n" + DIM + status + RESET + "\x1b[K\x1b[J")
            sys.stdout.flush()

            timeout = 0.04 if playing else (2 if watch else None)
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                if playing and t_cursor is not None:
                    t_cursor += 0.04 * SPEEDS[speed_i]
                    if t_cursor >= data["Tmax"]:
                        t_cursor = data["Tmax"]
                        playing = False
                if watch and not playing:
                    fresh = [os.stat(p).st_mtime_ns for p in paths]
                    if fresh != mtimes:
                        mtimes = fresh
                        data = _load(paths)
                        lanes = data["lanes"]
                        per_lane = [_nodes_in_order(l) for l in lanes]
                        sels = [min(s, len(ns) - 1) if ns else 0
                                for s, ns in zip(sels, per_lane)]
                continue
            key = os.read(fd, 8).decode("utf-8", errors="replace")
            if key in ("q", "\x03"):
                return 0
            if mode == "inv":
                if key == "\x1b[A" and inv_sel > 0:
                    inv_sel -= 1
                elif key == "\x1b[B" and inv_sel < len(inv_turns) - 1:
                    inv_sel += 1
                elif key in ("\r", "\n"):
                    ext = turn_extent(inv_turns[inv_sel])
                    if ext:
                        view = ext
                    mode = "maze"
                elif key == "i":
                    mode = "maze"
                continue
            if key in ("\x1b[C", "l") and sels[cur] < len(nodes) - 1:
                sels[cur] += 1
            elif key in ("\x1b[D", "h") and sels[cur] > 0:
                sels[cur] -= 1
            elif two and key in ("\x1b[A", "\x1b[B", "\t"):
                cur = 1 - cur
            elif key == "f":
                filt_fail = not filt_fail
            elif key == "/":
                sys.stdout.write("\x1b[2K\r搜索：")
                sys.stdout.flush()
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                try:
                    query = input().strip()
                finally:
                    tty.setcbreak(fd)
            elif key == "i" and two:
                mode, inv_sel = "inv", 0
            elif key == "p":
                if t_cursor is not None and t_cursor >= data["Tmax"]:
                    t_cursor = 0.0
                if t_cursor is None:
                    t_cursor = 0.0
                playing = not playing
                if not playing and t_cursor >= data["Tmax"]:
                    t_cursor = None
            elif key == "]":
                speed_i = min(speed_i + 1, len(SPEEDS) - 1)
            elif key == "[":
                speed_i = max(speed_i - 1, 0)
            elif key in ("+", "="):
                if node is not None:
                    center = (node["s"] + node["e"]) / 2
                    span = ((view[1] - view[0]) if view else data["Tmax"]) / 2
                    view = [center - span / 2, center + span / 2]
            elif key == "-":
                if view is not None:
                    center = (view[0] + view[1]) / 2
                    span = (view[1] - view[0]) * 2
                    view = None if span >= data["Tmax"] else \
                        [center - span / 2, center + span / 2]
            elif key == "0":
                view = None
                if not playing:
                    t_cursor = None
    except KeyboardInterrupt:   # Ctrl-C（cbreak 保留 ISIG）＝干净退出，不甩栈
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")


if __name__ == "__main__":
    lane = {
        "model": "claude-sonnet-5",
        "main": [
            {"step": 1, "turn": 1, "s": 0, "e": 10, "v": "ok", "why": "w",
             "tools": [{"name": "bash", "s": 0, "e": 8, "dur": 8.0, "args": "ls",
                        "res": "ok", "resFull": "ok", "err": False, "v": "ok",
                        "why": "退出正常且有输出"}],
             "rz": 1, "rzTxt": "想", "rzTxtFull": "想", "rzTok": None, "outTok": 5},
            {"step": 3, "turn": 2, "s": 40, "e": 41, "v": "answer",
             "why": "无工具调用，输出回答", "tools": [], "rz": 0, "rzTxt": "",
             "rzTxtFull": "", "rzTok": None, "outTok": 2},
        ],
        "detours": [
            {"step": 2, "turn": 1, "s": 12, "e": 30, "v": "error", "why": "boom",
             "attach": 1,
             "tools": [{"name": "grep", "s": 12, "e": 30, "dur": 18.0,
                        "args": "pattern=秘密", "res": "no matches",
                        "resFull": "no matches", "err": False, "v": "error",
                        "why": "w"}],
             "rz": 0, "rzTxt": "", "rzTxtFull": "", "rzTok": None, "outTok": None},
        ],
        "stats": {"steps": 3, "tools": 2, "rz": 1, "rzTok": None, "outTok": 7,
                  "T": 41, "main": 2, "detours": 1},
    }
    fr = render_frame(lane, None, 41, 80)
    txt = fr.text()
    plain = txt
    for code in (INV, DIM, RESET, "\x1b[33m", *V_COLOR.values()):
        plain = plain.replace(code, "")
    lines = plain.splitlines()
    assert "3 步（主干 2 / 支路 1）" in lines[0] and "输出 7tok" in lines[0]
    mainline = next(l for l in lines if "█" in l)
    assert "●" in mainline, "answer 节点"
    assert "┊" in mainline, "换轮分隔"
    det = next(l for l in lines if "↳" in l)
    assert "↳✗grep 18s↩" in det and "▓" in det, det
    # 条位置∝时间：步1 起点在左缘附近
    assert mainline.index("█") <= X0 + 1
    # 选中反色＋过滤 dim
    fr2 = render_frame(lane, None, 41, 80, sel_step=2, filt_fail=True)
    assert INV in fr2.text() and DIM in fr2.text()
    # 搜索命中语义（resFull 全文）
    assert node_matches(lane["detours"][0], "matches")
    assert not node_matches(lane["main"][1], "matches")
    # 缩放窗口：只看 [35,41]，步1 应被裁掉
    fr3 = render_frame(lane, None, 41, 80, view=[35, 41])
    p3 = fr3.text()
    for code in (INV, DIM, RESET, "\x1b[33m", *V_COLOR.values()):
        p3 = p3.replace(code, "")
    assert "↳" not in p3, "窗口外支路被裁"
    # 详情
    d = detail_lines(lane["detours"][0], None, 100, False)
    assert d[0].startswith("S2 · 第 1 轮 · 探索支路 · 失败 ✗ · 自 S1 分叉") and \
        any("判定依据" in x for x in d) and any("返回：no matches" in x for x in d)
    # pack：重叠支路分两行
    packed = _pack([(0, 20, "a"), (5, 25, "b"), (30, 40, "c")])
    assert [r for r, _, _ in packed] == [0, 1, 0]
    # 播放光标：未到的节点变暗（t_cursor=5 时步3 s=40 未到）
    frp = render_frame(lane, None, 41, 80, t_cursor=5.0)
    assert DIM + "●" in frp.text() or f"{DIM}●" in frp.text(), "未到 answer 该暗"
    # 对齐行：两泳道同数据 → 每共有轮 ⚑N Δ0s＋支路计数
    al = render_align(lane, lane, None, 41, 100).text()
    plain_al = al
    for code in (INV, DIM, RESET, "\x1b[36m", "\x1b[33m", *V_COLOR.values()):
        plain_al = plain_al.replace(code, "")
    assert "⚑1 Δ0s 支路1↔1" in plain_al and "⚑2 Δ0s" in plain_al, plain_al
    # 盘点表：同数据两边对称=持平；title 行＋合计行
    inv, turns = render_inventory({"lanes": [lane, lane], "timeMap": None}, 0, 100)
    plain_inv = "\n".join(inv)
    for code in (INV, DIM, RESET):
        plain_inv = plain_inv.replace(code, "")
    assert turns == [1, 2] and "第 1 轮" in plain_inv and "≈持平" in plain_inv
    assert "1 步·18s ✗1" in plain_inv and "合计" in plain_inv, plain_inv
    # 只一边有轮：另一边显 —
    lane_b = dict(lane, detours=[])
    inv2, _ = render_inventory({"lanes": [lane, lane_b], "timeMap": None}, 0, 100)
    assert "—" in "\n".join(inv2) and "第 1 会话多耗" in "\n".join(inv2)
    print("trace_view selfcheck ok — 帧/泳道/换轮/缝/选中/过滤/搜索/缩放裁剪/详情/装箱/播放/对齐/盘点")
