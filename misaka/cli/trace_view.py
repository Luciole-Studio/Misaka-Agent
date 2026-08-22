"""Terminal "maze" view of a session trace (data comes from observability.trace).

Visual language: the main path is a row of duration bars colored by verdict
(width proportional to time, joined by ─); detours are swimlane rows above and
below (↳ leaves, bar, label, ↩ returns); ┊ separates turns, ⏸ marks a collapsed
idle gap, and the bottom row is a wall-clock scale.
Keys: ←/→ (or h/l) select a step (details stay on screen), f shows only
failures and retries, / searches commands and results, +/- zooms around the
selected step, 0 resets, q quits. ``--watch`` re-parses and redraws when the
session file changes, so a running card grows live.

``render_frame`` is a pure function; the interactive loop only handles keys and
redraws.
"""
import os
import sys

from misaka.observability.trace import (
    fmt_det_diff, fmt_t, turn_detour_stats, turn_end_nodes, union_turns,
    wall_clock)

V_COLOR = {"ok": "\x1b[32m", "answer": "\x1b[32m", "error": "\x1b[31m",
           "deadend": "\x1b[90m", "retry": "\x1b[90m"}
V_GLYPH = {"ok": "✓", "answer": "●", "error": "✗", "deadend": "·", "retry": "↻"}
DIM, INV, RESET = "\x1b[2m", "\x1b[7m", "\x1b[0m"
X0 = 2                      # Left margin; column 0 holds the ↑/↓ lane markers.
MIN_SPAN = 0.5


def node_matches(n, query):
    """Case-insensitive search over tool args, full tool results and thinking text."""
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
    """Fixed-width character canvas; text past the last column is clipped and ANSI colors are applied per cell."""

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
    """Greedy row packing for detour bars: non-overlapping items share a row, overlapping ones get a new one.

    ``items`` is ``[(start_col, end_col, node)]``; returns ``[(row_idx, start_col, node)]``."""
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
    """Render one lane's maze frame.

    ``view`` is ``[t0, t1]`` in compressed time, or None for the whole trace.
    ``t_cursor`` is the playback cursor (compressed time): nodes not yet reached
    are dimmed. Returns a Frame."""
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
    tok = (f"output {st['outTok']:,} tok" if st["outTok"] is not None
           else f"{st['rz']} reasoning blocks")
    turns = len({n["turn"] for n in lane["main"] + lane["detours"]})
    head = f.line()
    f.put(
        head,
        0,
        f"{title + ' · ' if title else ''}{lane['model'] or 'unknown model'} · "
        f"{f'{turns} turns · ' if turns > 1 else ''}"
        f"{st['steps']} steps (main {st['main']} / detours {st['detours']}) · "
        f"{st['tools']} tools · {tok} · total {fmt_t(st['T'])}",
    )

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
    """Render the turn-alignment row between two lanes.

    Only turns present on both sides are linked: ``⚑N Δ<time diff> detours a↔b``,
    centered between the two answer nodes."""
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
        label = f"⚑{turn} Δ{fmt_t(abs(wb - wa))}" + (f" detours {da}↔{db}" if da + db else "")
        col = x((na["e"] + nb["e"]) / 2) - len(label) // 2
        f.put(row, max(0, min(col, width - len(label))), label, "\x1b[36m")
    return f


def render_inventory(data, sel_idx, width):
    """Render the detour inventory table: one row per turn, columns for each lane plus the difference.

    Returns ``(lines, turns)``. ``sel_idx`` is the highlighted row (Enter zooms to that turn)."""
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
        return f"{g['n']} steps · {fmt_t(g['T'])}" + (f" {seg}" if seg else "")

    from misaka.cli.herdr_ui import display_width

    def pad(s, w):   # Width-aware padding; plain f-string padding misaligns CJK text.
        return s + " " * max(1, w - display_width(s))

    w1, w2 = 10, 26
    out = ["Detours by turn — ↑/↓ select  Enter zoom to turn  i back", ""]
    out.append(pad("Turn", w1) + pad("Session 1", w2) + pad("Session 2", w2) + "Difference")
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
        line = pad(f"Turn {turn}", w1) + pad(cell(a), w2) + pad(cell(b), w2) + diff
        out.append(f"{INV}{line}{RESET}" if i == sel_idx else line)
    total_diff = (fmt_det_diff(tb["T"] - ta["T"], True)
                  if ta["n"] + tb["n"] > 0 else "no detours on either side")
    out.append(pad("Total", w1) + pad(f"{ta['n']} steps · {fmt_t(ta['T'])}", w2)
               + pad(f"{tb['n']} steps · {fmt_t(tb['T'])}", w2) + total_diff)
    out.append("")
    out.append(DIM + "Detour time is wall-clock. ✗ failed  ↻ wasted retry  · dead end.  — means no detours that turn on that side (absence is a signal too)." + RESET)
    return out, turns


def detail_lines(n, time_map, width, is_main):
    """Text detail panel for the selected step."""
    kind = ("final answer" if n["v"] == "answer" else "main path") if is_main else "detour"
    lab = {"error": "failed ✗", "deadend": "dead end ·", "retry": "retry ↻",
           "ok": "succeeded", "answer": "answer"}[n["v"]]
    out = [f"S{n['step']} · turn {n['turn']} · {kind} · {lab}"
           + (f" · branched from S{n['attach']} and returned" if not is_main else "")]
    if n.get("why"):
        out.append(f"Verdict: {n['why']}")
    rz = f"{n['rzTok']:,} reasoning tokens" if n.get("rzTok") is not None else f"{n['rz']} reasoning blocks"
    out.append(
        f"Time {fmt_t(wall_clock(n['s'], time_map))} → "
        f"{fmt_t(wall_clock(n['e'], time_map))} "
        f"({fmt_t(max(n['e'] - n['s'], 0))}) · {rz}"
    )
    for t in n["tools"]:
        mark = V_GLYPH.get(t["v"], "") if t["e"] is not None else "… running"
        out.append(f"  {t['name']} ({fmt_t(t['dur']) if t['e'] is not None else '?'})"
                   f" {mark}  {str(t['args'])[: width - 20]}")
        if t.get("why"):
            out.append(f"    Verdict: {t['why'][:width - 12]}")
        res = t.get("resFull") or t.get("res") or ""
        if res:
            out.append(f"    Result: {res[:width - 10]}")
    if n.get("rzTxt"):
        out.append(f"  Thinking: {n['rzTxt'][:width - 10]}")
    return out


def locate_session(con, target):
    """Resolve a target (session file path, card ID, or role name) to a session file.

    Returns ``(path, None)`` on success or ``(None, error_message)``."""
    from misaka.core.session_manager import find_most_recent_session
    if os.path.isfile(os.path.expanduser(target)):
        return os.path.expanduser(target), None
    from misaka.platform import tasks as db
    row = db.get(con, target)
    if row is not None:
        sf = row["session_file"]
        if sf and os.path.isfile(sf):
            return sf, None
        sf = find_most_recent_session(os.path.join(db.task_state_dir(target), "session"))
        if sf:
            return sf, None
        return None, f"Card {target} has no session yet."
    role_dir = os.path.expanduser(f"~/.misaka/sessions/{target.replace('_', '-')}")
    if os.path.isdir(role_dir):
        sf = find_most_recent_session(role_dir)
        if sf:
            return sf, None
        return None, f"Session directory for {target} is empty."
    return None, f"Unknown target {target!r}. Give a card ID, a role name, or a session file path."


def _load(paths):
    from misaka.observability.trace import build_data
    texts = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
    return build_data(texts)


def _nodes_in_order(lane):
    return sorted(lane["main"] + lane["detours"], key=lambda n: n["step"])


def _lane_title(data, i):
    return None if len(data["lanes"]) == 1 else f"Session {i + 1}"


def run(paths, *, watch=False, plain=False):
    """Entry point. One path shows a single session; two paths compare them on a shared axis.

    ``plain`` (or a non-TTY) prints one frame and exits."""
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
    mode = "maze"          # maze | inv (inventory table, comparison mode only)
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
                status = "↑/↓ select turn  Enter zoom  i back  q quit"
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
                    hits = f" · matches {vis}/{total}"
                play = (f" ▶{fmt_t(wall_clock(t_cursor, data['timeMap']))}"
                        f" {SPEEDS[speed_i]}×" if t_cursor is not None else "")
                lane_hint = f" (lane {cur + 1}; ↑↓ switch)" if two else ""
                status = (
                    f"←→ select step{lane_hint}  f failures only "
                    f"{'●' if filt_fail else '○'}  / search"
                    f"{f' [{query}]' if query else ''}  +/- zoom  0 fit  p play{play}  "
                    f"[] speed{'  i inventory' if two else ''}  q quit{hits}"
                    f"{' · watch' if watch else ''}"
                )
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
                sys.stdout.write("\x1b[2K\rSearch: ")
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
    except KeyboardInterrupt:   # Ctrl-C (cbreak keeps ISIG): exit cleanly without a traceback.
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
