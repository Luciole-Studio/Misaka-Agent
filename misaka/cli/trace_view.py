"""单会话迷宫 TUI（dsh-trace-compare 迷宫视图的终端化，数据内核 trace_lanes）。

视觉语言对应上游：主干=判定色时长条（宽∝耗时，条间 ─ 连接），支路=上下泳道行
（↳出程＋条＋标注＋↩折返），┊=换轮分隔，⏸=空闲折叠缝，底行=墙钟刻度。
上游的悬停/点击在终端换成键盘：←→ 选步（详情区常驻显示选中步），f 只看失败/
重试，/ 搜索命令与返回，+/- 以选中步为中心缩放，h/l 平移，0 复位，q 退出。
--watch＝现场文件变了自动重析重画（正在跑的卡实时生长）。

帧渲染是纯函数（render_frame），交互循环只管键与重画——selfcheck 直接断言帧。
"""
import os
import sys

from misaka.orchestration.trace_lanes import fmt_t, wall_clock

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


def render_frame(lane, time_map, tmax, width, *, view=None, sel_step=None,
                 filt_fail=False, query=""):
    """一条泳道的迷宫帧。view=[t0,t1]（压缩坐标）或 None=整图。返回 Frame。"""
    t0 = view[0] if view else 0.0
    span = (view[1] - view[0]) if view else tmax
    span = max(span, MIN_SPAN)
    usable = width - X0 - 1

    def x(t):
        return X0 + int((t - t0) / span * usable)

    active = filt_fail or bool(query)

    def shade(n, base_color):
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
          f"{lane['model'] or '未知模型'} · "
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


def _load(path):
    from misaka.orchestration.trace_lanes import build_data
    with open(path, encoding="utf-8", errors="replace") as fh:
        return build_data([fh.read()])


def _nodes_in_order(lane):
    return sorted(lane["main"] + lane["detours"], key=lambda n: n["step"])


def run(path, *, watch=False, plain=False):
    """单会话迷宫入口。plain（或非 TTY）＝打一帧退出；否则进交互循环。"""
    data = _load(path)
    lane = data["lanes"][0]
    cols = os.get_terminal_size().columns if sys.stdout.isatty() else 120
    if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
        frame = render_frame(lane, data["timeMap"], data["Tmax"], cols)
        print(frame.text())
        nodes = _nodes_in_order(lane)
        if nodes:
            print("\n" + "\n".join(detail_lines(
                nodes[-1], data["timeMap"], cols, nodes[-1]["v"] in ("ok", "answer"))))
        return 0
    return _interactive(path, data, watch)


def _interactive(path, data, watch):
    import select
    import termios
    import tty

    lane = data["lanes"][0]
    nodes = _nodes_in_order(lane)
    sel = len(nodes) - 1
    view = None
    filt_fail = False
    query = ""
    mtime = os.stat(path).st_mtime_ns

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while True:
            size = os.get_terminal_size()
            node = nodes[sel] if nodes else None
            frame = render_frame(
                lane, data["timeMap"], data["Tmax"], size.columns, view=view,
                sel_step=node["step"] if node else None,
                filt_fail=filt_fail, query=query)
            out = [frame.text(), ""]
            if node is not None:
                is_main = any(n is node for n in lane["main"])
                out += detail_lines(node, data["timeMap"], size.columns, is_main)
            hits = ""
            if filt_fail or query:
                vis = sum(1 for n in nodes if _visible(n, filt_fail, query))
                hits = f" · 命中 {vis}/{len(nodes)}"
            status = (f"←→选步 f失败{'●' if filt_fail else '○'} /搜索"
                      f"{('「' + query + '」') if query else ''} +-缩放 h l平移 0整图 q退出"
                      f"{hits}{' · watch' if watch else ''}")
            body = "\n".join(out[: size.lines - 2])
            sys.stdout.write("\x1b[2J\x1b[H" + body + "\n" + DIM + status + RESET)
            sys.stdout.flush()

            ready, _, _ = select.select([sys.stdin], [], [], 2 if watch else None)
            if not ready:
                st = os.stat(path)
                if st.st_mtime_ns != mtime:
                    mtime = st.st_mtime_ns
                    data = _load(path)
                    lane = data["lanes"][0]
                    nodes = _nodes_in_order(lane)
                    sel = min(sel, len(nodes) - 1) if nodes else 0
                continue
            key = os.read(fd, 8).decode("utf-8", errors="replace")
            if key in ("q", "\x03"):
                return 0
            if key in ("\x1b[C", "l") and sel < len(nodes) - 1:
                sel += 1
            elif key in ("\x1b[D", "h") and sel > 0:
                sel -= 1
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
            elif key in ("+", "="):
                if node is not None:
                    center = (node["s"] + node["e"]) / 2
                    span = ((view[1] - view[0]) if view else data["Tmax"]) / 2
                    view = [center - span / 2, center + span / 2]
            elif key == "-":
                if view is not None:
                    center = (view[0] + view[1]) / 2
                    span = (view[1] - view[0]) * 2
                    if span >= data["Tmax"]:
                        view = None
                    else:
                        view = [center - span / 2, center + span / 2]
            elif key == "0":
                view = None
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
    print("trace_view selfcheck ok — 帧/泳道/换轮/缝/选中/过滤/搜索/缩放裁剪/详情/装箱")
