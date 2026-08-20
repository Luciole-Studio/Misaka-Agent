"""trace 数据内核：引擎 v3 会话 jsonl → 迷宫 lanes 模型＋空闲折叠。

dsh-trace-compare 的 buildLane/buildData/compressTimeline（maze-upload.html@7b8a28c）
与 live-data.ts 的语义合体移植（MIT）：解析走 v3 事件流（misaka 引擎格式），
判定/分拣/折叠逐段对应上游。与上游的关键映射：

  message(user)            → 分轮＋t0 锚（首条 user）
  message(assistant)       → 一步：thinking 块=推理段，toolCall 块=工具发起，
                             usage.output=outTok（**推理 token 无分项→rzTok=None，
                             渲染层诚实退「N 段推理」——上游同款回退设计**）
  message(toolResult)      → 按 toolCallId 精确配对（live-data 语义；上游上传页的
                             FIFO shift 是因为 dsh log 无 callId，我们有就用准的）
  model_change             → lane.model（取最后一次＝当前模型，live-data 同语义）

时间一律用外层事件 ISO timestamp（内层 toolResult.timestamp 是 epoch 毫秒，不碰）。
判定契约：tool_verdict 吃未截断全文；无结果的调用不投票、不进重试簇（live-data
语义，处理中断现场）；步 e=max(本步消息落地, 最晚工具返回)。
"""
import json
import math
from datetime import datetime

from misaka.orchestration.trace_verdict import (
    mark_retry_clusters, step_verdict, tool_verdict)

IDLE_MIN = 60          # 无活动超过此秒数的区间视为等待，折叠显示（上游同值）
RES_TIP = 380          # 悬停/行内摘要窗（上游同值）
RES_FULL = 5000        # 详情全文窗
RZ_TIP = 240
RZ_FULL = 2000


def _ts(event):
    """外层事件 ISO 时间戳 → epoch 秒。"""
    raw = str(event.get("timestamp") or "")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def _text_blocks(content):
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            out.append(b["text"])
    return "".join(out)


def _arg_summary(arguments):
    """上游 argSummary：command / file_path / pattern(+path) / query / 整体 JSON。
    v3 的 arguments 已是对象，无需 JSON.parse。"""
    a = arguments
    if isinstance(a, dict):
        if isinstance(a.get("command"), str):
            return a["command"]
        if isinstance(a.get("file_path"), str):
            return a["file_path"]
        if isinstance(a.get("pattern"), str):
            s = "pattern=" + a["pattern"]
            if a.get("path"):
                s += " path=" + str(a["path"])
            return s
        if isinstance(a.get("query"), str):
            return a["query"]
        return json.dumps(a, ensure_ascii=False)
    return "" if a is None else str(a)


def parse_lane(text):
    """一份 v3 会话 jsonl 文本 → {rows, model}。行内坏 JSON 跳过（上游同款）。"""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    if not events:
        raise ValueError("文件不是有效的 JSONL（每行一个 JSON 对象）")

    first_user = next((e for e in events
                       if e.get("type") == "message"
                       and (e.get("message") or {}).get("role") == "user"), None)
    t0 = _ts(first_user if first_user is not None else events[0])
    rel = lambda e: round((_ts(e) - t0) * 10) / 10   # noqa: E731

    rows = []
    pending = {}        # toolCallId → (tool dict, row dict)
    model = None
    turn = 0
    prev_t = 0.0
    step_no = 0
    for e in events:
        etype = e.get("type")
        if etype == "model_change":
            model = e.get("modelId") or model
            continue
        if etype != "message":
            continue
        m = e.get("message") or {}
        role = m.get("role")
        tm = rel(e)
        if role == "user":
            turn += 1
            prev_t = tm
        elif role == "assistant":
            step_no += 1
            s = min(prev_t, tm)
            tools, rz, rz_txt = [], 0, ""
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking":
                    rz += 1
                    rz_txt += b.get("thinking") or b.get("text") or ""
                elif b.get("type") == "toolCall":
                    tool = {"name": b.get("name") or "?", "s": tm, "e": None,
                            "args": _arg_summary(b.get("arguments")),
                            "res": "", "err": False, "dur": 0.0,
                            "v": "ok", "why": None}
                    tools.append(tool)
                    if b.get("id"):
                        pending[b["id"]] = tool
            usage = m.get("usage") or {}
            out_tok = usage.get("output") if isinstance(usage.get("output"), int) else None
            row = {"step": step_no, "turn": max(turn, 1), "s": s, "e": tm,
                   "tools": tools, "rz": rz, "rzTxt": "", "rzTxtFull": "",
                   "_rz_raw": rz_txt, "rzTok": None, "outTok": out_tok,
                   "v": "ok", "why": None}
            for tool in tools:
                tool["_row"] = row
            rows.append(row)
            prev_t = tm
        elif role == "toolResult":
            tool = pending.pop(m.get("toolCallId"), None)
            if tool is not None:
                tool["e"] = tm
                tool["res"] = _text_blocks(m.get("content"))
                tool["err"] = m.get("isError") is True
                tool["dur"] = round((tool["e"] - tool["s"]) * 10) / 10
                row = tool.pop("_row")
                row["e"] = max(row["e"], tm)
            prev_t = tm

    for row in rows:
        rz_clean = " ".join(str(row.pop("_rz_raw")).split())
        row["rzTxt"] = rz_clean[:RZ_TIP]
        row["rzTxtFull"] = rz_clean[:RZ_FULL]
        for tool in row["tools"]:
            tool.pop("_row", None)
            if tool["e"] is None:
                continue
            full = " ".join(str(tool["res"]).split())
            verdict = tool_verdict({"name": tool["name"], "res": full, "err": tool["err"]})
            tool["v"] = verdict["v"]
            tool["why"] = verdict["why"]
            tool["resFull"] = full[:RES_FULL]
            tool["res"] = full[:RES_TIP]
    return {"rows": rows, "model": model}


def build_lane(text, key):
    """解析＋重试簇＋步级聚合＋主干/支路分拣＝一条泳道（上游 buildData 单泳道段）。"""
    parsed = parse_lane(text)
    rows, model = parsed["rows"], parsed["model"]
    settled = [t for r in rows for t in r["tools"] if t["e"] is not None]
    mark_retry_clusters(settled)
    main, detours = [], []
    last_main = None
    for r in rows:
        if not r["tools"]:
            r["v"], r["why"] = "answer", "无工具调用，输出回答"
        else:
            sv = step_verdict([t for t in r["tools"] if t["e"] is not None])
            if sv is not None:
                r["v"], r["why"] = sv["v"], sv["why"]
            else:
                r["why"] = "工具结果未返回，暂留主干"
        if r["v"] in ("ok", "answer"):
            main.append(r)
            last_main = r
        else:
            detours.append(dict(r, attach=last_main["step"] if last_main else 0))
    if not main and not detours:
        raise ValueError("没有可解析的有效步骤（会话里没有 assistant 消息？）")
    tools_n = sum(len(r["tools"]) for r in rows)
    rz_n = sum(r["rz"] for r in rows)
    out_tok = (sum(r["outTok"] or 0 for r in rows)
               if any(r["outTok"] is not None for r in rows) else None)
    T = max((r["e"] for r in rows), default=0.1)
    return {"key": key, "model": model, "main": main, "detours": detours,
            "preWindow": 0,
            "stats": {"steps": len(rows), "tools": tools_n, "rz": rz_n,
                      "rzTok": None, "outTok": out_tok, "T": T,
                      "main": len(main), "detours": len(detours)}}


def build_data(texts):
    """1-2 份会话文本 → {Tmax, lanes, timeMap}（折叠后坐标）。"""
    lanes = [build_lane(t, "l" + str(i + 1)) for i, t in enumerate(texts)]
    data = {"Tmax": max(max(l["stats"]["T"] for l in lanes), 60), "lanes": lanes}
    compress_timeline(data)
    return data


def compress_timeline(data):
    """空闲折叠（上游 compressTimeline 逐段移植）：无活动 >IDLE_MIN 的区间压成细缝。
    就地改节点/工具时间为压缩坐标；耗时与统计保墙钟。data['timeMap']=None＝没折。"""
    intervals = []
    for lane in data["lanes"]:
        for arr in (lane["main"], lane["detours"]):
            for n in arr:
                intervals.append([n["s"], max(n["e"], n["s"])])
                for tl in n["tools"]:
                    if tl["s"] is not None:
                        e = tl["e"] if tl["e"] is not None else tl["s"]
                        intervals.append([tl["s"], max(e, tl["s"])])
    data["timeMap"] = None
    if not intervals:
        return
    intervals.sort(key=lambda p: p[0])
    act = []
    for s, e in intervals:
        if act and s <= act[-1][1] + IDLE_MIN:
            act[-1][1] = max(act[-1][1], e)
        else:
            act.append([s, e])
    if len(act) <= 1:
        return
    active = sum(e - s for s, e in act)
    seam = min(30, max(1, round(active * 0.015 * 10) / 10))
    segments, gaps = [], []
    c = min(act[0][0], IDLE_MIN)
    for i, (rs, re_) in enumerate(act):
        segments.append({"rs": rs, "re": re_, "cs": c})
        c += re_ - rs
        if i < len(act) - 1:
            gaps.append({"c": c, "skipped": act[i + 1][0] - re_})
            c += seam

    def cmap(t):
        if t <= segments[0]["rs"]:
            return segments[0]["cs"] - (segments[0]["rs"] - t)
        for i in range(len(segments) - 1, -1, -1):
            g = segments[i]
            if t < g["rs"]:
                continue
            if t <= g["re"]:
                return g["cs"] + (t - g["rs"])
            nxt = segments[i + 1] if i + 1 < len(segments) else None
            gap_len = (nxt["rs"] - g["re"]) if nxt else 1
            return g["cs"] + (g["re"] - g["rs"]) + min(1, (t - g["re"]) / gap_len) * seam
        return t

    seen = set()

    def remap(n):
        if id(n) in seen:
            return
        seen.add(id(n))
        n["s"] = cmap(n["s"])
        n["e"] = cmap(n["e"])
        for tl in n["tools"]:
            if id(tl) in seen:
                continue
            seen.add(id(tl))
            if tl["s"] is not None:
                tl["s"] = cmap(tl["s"])
            if tl["e"] is not None:
                tl["e"] = cmap(tl["e"])

    for lane in data["lanes"]:
        for n in lane["main"]:
            remap(n)
        for n in lane["detours"]:
            remap(n)
    data["Tmax"] = max(c, 60)
    data["timeMap"] = {"segments": segments, "gaps": gaps, "seam": seam}


def wall_clock(t, time_map):
    """压缩坐标 → 墙钟秒（上游 wallClock）。time_map=None 恒等。"""
    if not time_map:
        return t
    for seg in reversed(time_map["segments"]):
        if t >= seg["cs"]:
            return min(seg["re"], seg["rs"] + (t - seg["cs"]))
    return t


def _js_round(v, nd=0):
    """JS Math.round/toFixed 的远离零舍入（Python round 是银行家舍入，.5 会漂）。"""
    q = 10 ** nd
    return math.floor(v * q + 0.5) / q


def fmt_t(t):
    """秒 → 轴/统计标签（上游 fmtT 逐译，舍入按 JS 语义）：340→'6m'，42.5→'42.5s'。
    （上游注释判例 49252→'13.7h' 与其代码不符——代码 ≥36000 走 toFixed(0)＝'14h'，
    忠实以代码为准。）"""
    if t >= 3600:
        nd = 0 if t >= 36000 else 1
        return f"{_js_round(t / 3600, nd):.{nd}f}h"
    if t >= 120:
        return f"{_js_round(t / 60):g}m"
    return f"{_js_round(t, 1):g}s"


if __name__ == "__main__":
    def ev(kind, at, **kw):
        base = {"type": kind, "timestamp":
                datetime.fromtimestamp(1_700_000_000 + at).astimezone().isoformat()}
        base.update(kw)
        return json.dumps(base, ensure_ascii=False)

    def msg(at, role, **m):
        return ev("message", at, message={"role": role, **m})

    lines = [
        ev("session", 0, version=3, id="s1"),
        ev("model_change", 0, provider="p", modelId="claude-sonnet-5"),
        msg(0, "user", content=[{"type": "text", "text": "干活"}]),
        # 步1：两个并行工具，一败一成 → error 支路
        msg(5, "assistant",
            content=[{"type": "thinking", "thinking": "想"},
                     {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "ls"}},
                     {"type": "toolCall", "id": "b", "name": "bash", "arguments": {"command": "cat x"}}],
            usage={"output": 100}),
        msg(8, "toolResult", toolCallId="a", isError=True,
            content=[{"type": "text", "text": "boom"}]),
        msg(9, "toolResult", toolCallId="b", isError=False,
            content=[{"type": "text", "text": "y" * 6000}]),
        # 步2：检索扑空 → deadend 支路
        msg(12, "assistant",
            content=[{"type": "toolCall", "id": "c", "name": "grep",
                      "arguments": {"pattern": "x", "path": "src"}}]),
        msg(13, "toolResult", toolCallId="c", isError=False,
            content=[{"type": "text", "text": ""}]),
        # 步3：无结果的调用（中断现场）→ 不投票留主干
        msg(15, "assistant",
            content=[{"type": "toolCall", "id": "d", "name": "bash",
                      "arguments": {"command": "sleep 99"}}]),
        # 长空闲（>60s）→ 折叠缝
        msg(200, "user", content=[{"type": "text", "text": "第二轮"}]),
        msg(203, "assistant", content=[{"type": "text", "text": "答"}], usage={"output": 7}),
    ]
    data = build_data(["\n".join(lines)])
    lane = data["lanes"][0]
    st = lane["stats"]
    assert (st["steps"], st["main"], st["detours"]) == (4, 2, 2), st
    assert [d["v"] for d in lane["detours"]] == ["error", "deadend"]
    assert lane["detours"][0]["attach"] == 0 and lane["model"] == "claude-sonnet-5"
    assert {n["turn"] for n in lane["main"]} == {1, 2}, "分轮"
    pend = next(n for n in lane["main"] if n["tools"] and n["tools"][0]["e"] is None)
    assert pend["why"] == "工具结果未返回，暂留主干", "无结果不投票"
    ans = lane["main"][-1]
    assert ans["v"] == "answer" and ans["outTok"] == 7
    assert st["rzTok"] is None and st["outTok"] == 107, "推理无分项诚实缺席；输出真值累加"
    b = next(t for n in lane["detours"] for t in n["tools"] if t["args"] == "cat x")
    assert len(t := b["res"]) == RES_TIP and len(b["resFull"]) == RES_FULL, "两级截断"
    assert data["timeMap"] is not None and len(data["timeMap"]["gaps"]) == 1, "折叠缝"
    gap = data["timeMap"]["gaps"][0]
    assert gap["skipped"] > 150, gap
    ans_wall = wall_clock(ans["e"], data["timeMap"])
    assert abs(ans_wall - 203) < 1, f"墙钟反查 {ans_wall}"
    assert st["T"] == 203, "统计保墙钟"
    # 参数摘要族
    assert _arg_summary({"command": "ls -la"}) == "ls -la"
    assert _arg_summary({"pattern": "p", "path": "src"}) == "pattern=p path=src"
    assert _arg_summary({"file_path": "/a/b"}) == "/a/b"
    assert _arg_summary({"other": 1}) == '{"other": 1}'
    # 双泳道
    data2 = build_data(["\n".join(lines)] * 2)
    assert [l["key"] for l in data2["lanes"]] == ["l1", "l2"]
    # fmt_t（判例按上游**代码**行为，其注释判例 49252→13.7h 与代码不符）
    assert fmt_t(49252) == "14h" and fmt_t(340) == "6m" and fmt_t(42.5) == "42.5s"
    assert fmt_t(42) == "42s" and fmt_t(0) == "0s" and fmt_t(36000) == "10h"
    assert fmt_t(35000) == "9.7h" and fmt_t(150) == "3m" and fmt_t(119.96) == "120s"
    print("trace_lanes selfcheck ok — 分轮/配对/判定接线/分拣/attach/折叠/墙钟/截断/诚实缺席/fmt_t")
