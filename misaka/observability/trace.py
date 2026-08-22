"""Parse engine session events into lanes for execution-trace views."""
import json
import math
from datetime import datetime

from misaka.observability.verdict import (
    mark_retry_clusters, step_verdict, tool_verdict)

IDLE_MIN = 60
RES_TIP = 380
RES_FULL = 5000        # Characters of tool output kept for the details view
RZ_TIP = 240
RZ_FULL = 2000


def _ts(event):
    """Convert an event's ISO timestamp to epoch seconds."""
    raw = str(event.get("timestamp") or "")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def _text_blocks(content):
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            out.append(b["text"])
    return "".join(out)


def _arg_summary(arguments):
    """Pick the one argument worth showing for a tool call (command, path, pattern, or query)."""
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
    """Parse a v3 session JSONL transcript into trace rows and model metadata."""
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
        raise ValueError("The file contains no valid JSONL events.")

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
    """Parse one session into a main execution path and its detours."""
    parsed = parse_lane(text)
    rows, model = parsed["rows"], parsed["model"]
    settled = [t for r in rows for t in r["tools"] if t["e"] is not None]
    mark_retry_clusters(settled)
    main, detours = [], []
    last_main = None
    for r in rows:
        if not r["tools"]:
            r["v"], r["why"] = "answer", "Assistant answer without a tool call."
        else:
            sv = step_verdict([t for t in r["tools"] if t["e"] is not None])
            if sv is not None:
                r["v"], r["why"] = sv["v"], sv["why"]
            else:
                r["why"] = "Tool results were not returned."
        if r["v"] in ("ok", "answer"):
            main.append(r)
            last_main = r
        else:
            detours.append(dict(r, attach=last_main["step"] if last_main else 0))
    if not main and not detours:
        raise ValueError("No assistant execution steps were found in the session.")
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
    """Build a normalized trace model from one or two session transcripts."""
    lanes = [build_lane(t, "l" + str(i + 1)) for i, t in enumerate(texts)]
    data = {"Tmax": max(max(l["stats"]["T"] for l in lanes), 60), "lanes": lanes}
    compress_timeline(data)
    return data


def compress_timeline(data):
    """Collapse idle gaps longer than ``IDLE_MIN`` so the timeline shows only active stretches.

    Node and tool timestamps are rewritten in place; ``data["timeMap"]`` records
    the mapping back to wall-clock time.
    """
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
    """Map compressed trace coordinates back to wall-clock seconds."""
    if not time_map:
        return t
    for seg in reversed(time_map["segments"]):
        if t >= seg["cs"]:
            return min(seg["re"], seg["rs"] + (t - seg["cs"]))
    return t


def turn_detour_stats(lane, time_map):
    """Aggregate detour count, wall-clock time, and verdicts by turn."""
    out = {}
    for d in lane["detours"]:
        t = d.get("turn") or 1
        g = out.setdefault(t, {"n": 0, "T": 0.0, "by": {"error": 0, "retry": 0, "deadend": 0}})
        g["n"] += 1
        g["T"] += max(0.0, wall_clock(d["e"], time_map) - wall_clock(d["s"], time_map))
        g["by"][d["v"]] = g["by"].get(d["v"], 0) + 1
    return out


def turn_end_nodes(lane):
    """Return the last main-path node in each turn."""
    out = {}
    for n in lane["main"]:
        out[n.get("turn") or 1] = n
    return out


def union_turns(lanes):
    """Return the sorted union of turn numbers across trace lanes."""
    turns = set()
    for lane in lanes:
        for n in lane["main"] + lane["detours"]:
            turns.add(n.get("turn") or 1)
    return sorted(turns)


def fmt_det_diff(d_t, any_side):
    """Format the detour-time difference between two sessions."""
    if not any_side:
        return ""
    if abs(d_t) < 1:
        return "≈ equal"
    return f"Session {(2 if d_t > 0 else 1)} is longer by {fmt_t(abs(d_t))}"


def _js_round(v, nd=0):
    """Match JavaScript Math.round rather than Python's bankers' rounding."""
    q = 10 ** nd
    return math.floor(v * q + 0.5) / q


def fmt_t(t):
    """Format seconds as a short duration: 12.5s, 3m, 1.5h."""
    if t >= 3600:
        nd = 0 if t >= 36000 else 1
        return f"{_js_round(t / 3600, nd):.{nd}f}h"
    if t >= 120:
        return f"{_js_round(t / 60):g}m"
    return f"{_js_round(t, 1):g}s"
