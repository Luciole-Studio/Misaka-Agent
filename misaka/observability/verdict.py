"""Classify tool outcomes and repeated attempts for trace visualization."""
import re

VERDICT_RULES = {
    # Strong signatures are checked at both ends so quoted log text in the middle is ignored.
    "ERROR_PATTERNS_STRONG": re.compile(
        r'\[stderr\].*(Error|Traceback|File ")|\[status=Failed\]|__EXIT__=[1-9]',
        re.IGNORECASE),
    # Weak signatures are checked only at the beginning to avoid false positives in quoted text.
    "ERROR_PATTERNS_WEAK": re.compile(
        r'Traceback \(most recent|command not found|Permission denied|No such file'
        r'|HTTP 40\d|HTTP 50\d|^Error:',
        re.IGNORECASE),
    "ERROR_HEAD_SCAN": 300,
    "ERROR_TAIL_SCAN": 1000,
    "WRITE_TOOLS": ("write", "edit", "todo_write"),
    "SEARCH_TOOLS": ("grep", "read", "web_search", "read_image"),
    # Explicit empty-result signatures, checked at the beginning.
    "NO_RESULT_PATTERNS": re.compile(
        r"^(---)?$|no matches|no results|not found in", re.IGNORECASE),
    # Adjacent similar-call clustering parameters.
    "RETRY_SIMILARITY": 0.6,
    "RETRY_MIN_CLUSTER": 2,
}

# A step inherits the most severe verdict among its tools.
SEV = {"error": 4, "retry": 3, "deadend": 2, "ok": 0, "answer": 0}


def tool_verdict(ev):
    """Classify one completed tool call from its error flag and full result text."""
    if ev.get("err"):
        return {"v": "error", "why": 'Tool returned an error marker (isError).'}
    txt = str(ev.get("res") or "").strip()
    head = txt[: VERDICT_RULES["ERROR_HEAD_SCAN"]]
    tail = txt[-VERDICT_RULES["ERROR_TAIL_SCAN"]:] if txt else ""
    strong = (VERDICT_RULES["ERROR_PATTERNS_STRONG"].search(head)
              or VERDICT_RULES["ERROR_PATTERNS_STRONG"].search(tail))
    if strong is not None:
        return {"v": "error", "why": f'Output contains a strong failure signature: "{strong.group(0)[:48]}"'}
    weak = VERDICT_RULES["ERROR_PATTERNS_WEAK"].search(head)
    if weak is not None:
        return {"v": "error", "why": f'Output begins with a failure signature: "{weak.group(0)[:48]}"'}
    name = ev.get("name")
    if name in VERDICT_RULES["WRITE_TOOLS"]:
        return {"v": "ok", "why": 'Write tool completed without an error.'}
    if name in VERDICT_RULES["SEARCH_TOOLS"]:
        if VERDICT_RULES["NO_RESULT_PATTERNS"].search(head):
            return {"v": "deadend",
                    "why": "Search returned no content." if not txt else "Search returned an explicit no-result response."}
        return {"v": "ok", "why": 'Search returned content.'}
    if VERDICT_RULES["NO_RESULT_PATTERNS"].search(head):
        return {"v": "deadend", "why": "Tool exited normally without output."}
    return {"v": "ok", "why": 'Tool exited normally with output.'}


def step_verdict(tools):
    """Return the most severe tool verdict for a step, or None when empty."""
    worst = None
    for t in tools:
        if worst is None or SEV.get(t["v"], 0) > SEV.get(worst["v"], 0):
            worst = t
    return worst


_ARG_SPLIT = re.compile(r"[^A-Za-z0-9_\u4e00-\u9fff./-]+")


def _arg_tokens(s):
    return {w for w in _ARG_SPLIT.split(str(s)) if len(w) > 2}


def arg_similarity(a, b):
    """Measure tool-argument similarity with token-set Jaccard similarity."""
    ta, tb = _arg_tokens(a), _arg_tokens(b)
    if not ta or not tb:
        return 0
    inter = len(ta & tb)
    return inter / (len(ta) + len(tb) - inter)


def mark_retry_clusters(calls):
    """Mark consecutive similar calls as retries when their cluster contains a failure."""
    clusters = 0
    start = 0
    for i in range(1, len(calls) + 1):
        brk = (i == len(calls)
               or calls[i]["name"] != calls[i - 1]["name"]
               or arg_similarity(calls[i].get("args"), calls[i - 1].get("args"))
               < VERDICT_RULES["RETRY_SIMILARITY"])
        if not brk:
            continue
        length = i - start
        if length >= VERDICT_RULES["RETRY_MIN_CLUSTER"]:
            cluster = calls[start:i]
            fails = sum(1 for c in cluster if c["v"] == "error")
            if fails > 0:
                clusters += 1
                for c in cluster:
                    if c["v"] == "error":
                        c["why"] = (c.get("why") or "") + f"; repeated similar operation {length} times"
                    else:
                        c["v"] = "retry"
                        c["why"] = f"Repeated a similar operation {length} times, including {fails} failure(s)."
        start = i
    return clusters
