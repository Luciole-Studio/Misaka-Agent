"""迷宫判定（dsh-trace-compare verdict.js@7b8a28c 严格移植，MIT）。

上游是两条渲染链路共用的判定单一真相源；misaka 侧同职：trace 的 TUI 各档都吃
这一份。逐行对应翻译，阈值与文案逐字保留；与上游的对拍测试在
tests/contract/test_trace.py（node 在场时跑 fixtures/dsh_verdict.js 同输入比对）。

翻译对齐点（语义必须与 JS 一致的地方）：
- JS 无 u 标志的 \\w 只含 [A-Za-z0-9_]；Python \\w 默认含 CJK——argTokens 的
  字符类改写为显式 [A-Za-z0-9_一-鿿./-]，行为与上游逐字符一致。
- JS 正则无 m 标志：^/$ 锚整串；Python 同（不加 MULTILINE）。
- test/exec 均为「搜索」语义 → Python re.search。
"""
import re

VERDICT_RULES = {
    # 强失败特征（扫开头＋末尾两个窗口）：包装器/运行时的硬标记。真失败的标记要么在
    # 短输出里（命令直接死掉），要么贴着末尾（stderr 段是包装器追加在最后的）；而转储/
    # 引用别的日志时，这些标记悬在长文本中部，两个窗口都够不着。
    "ERROR_PATTERNS_STRONG": re.compile(
        r'\[stderr\].*(Error|Traceback|File ")|\[status=Failed\]|__EXIT__=[1-9]',
        re.IGNORECASE),
    # 弱失败特征（只扫开头窗口）：真实报错从开头开始说，而 git log / grep / 文档类输出
    # 在正文深处**引用**别人的报错不该算这条命令失败（上游 2026-08-19 实测误报案例）。
    "ERROR_PATTERNS_WEAK": re.compile(
        r'Traceback \(most recent|command not found|Permission denied|No such file'
        r'|HTTP 40\d|HTTP 50\d|^Error:',
        re.IGNORECASE),
    "ERROR_HEAD_SCAN": 300,
    "ERROR_TAIL_SCAN": 1000,
    # 写入类工具：成功确认天然很短，无错误即成功，永不按输出判扑空。
    "WRITE_TOOLS": ("write", "edit", "todo_write"),
    # 检索类工具：空结果=扑空；有返回（哪怕一行命中）即成功。
    "SEARCH_TOOLS": ("grep", "read", "web_search", "read_image"),
    # 空结果/无命中特征（只扫开头窗口）。
    "NO_RESULT_PATTERNS": re.compile(
        r"^(---)?$|no matches|no results|not found in", re.IGNORECASE),
    # 盲目重试：相邻同工具调用的参数 token Jaccard 相似度门槛／最小连续调用数。
    "RETRY_SIMILARITY": 0.6,
    "RETRY_MIN_CLUSTER": 2,
}

# 步级聚合的严重度序：取最坏工具判定作为步判定。
SEV = {"error": 4, "retry": 3, "deadend": 2, "ok": 0, "answer": 0}


def tool_verdict(ev):
    """单工具判定：错误标志 → 失败特征 → 按工具分类；返回 {v, why}。
    ev['res'] 必须传**未截断**的返回全文（上游契约：两条链路统一在同一份文本上判定）。"""
    if ev.get("err"):
        return {"v": "error", "why": "工具返回错误标志（isError）"}
    txt = str(ev.get("res") or "").strip()
    head = txt[: VERDICT_RULES["ERROR_HEAD_SCAN"]]
    tail = txt[-VERDICT_RULES["ERROR_TAIL_SCAN"]:] if txt else ""
    strong = (VERDICT_RULES["ERROR_PATTERNS_STRONG"].search(head)
              or VERDICT_RULES["ERROR_PATTERNS_STRONG"].search(tail))
    if strong is not None:
        return {"v": "error", "why": "输出命中失败特征「" + strong.group(0)[:48] + "」"}
    weak = VERDICT_RULES["ERROR_PATTERNS_WEAK"].search(head)
    if weak is not None:
        return {"v": "error", "why": "输出开头命中失败特征「" + weak.group(0)[:48] + "」"}
    name = ev.get("name")
    if name in VERDICT_RULES["WRITE_TOOLS"]:
        return {"v": "ok", "why": "写入类工具，无错误即成功"}
    if name in VERDICT_RULES["SEARCH_TOOLS"]:
        if VERDICT_RULES["NO_RESULT_PATTERNS"].search(head):
            return {"v": "deadend",
                    "why": "检索返回为空，判为扑空" if txt == "" else "检索开头命中无结果特征，判为扑空"}
        return {"v": "ok", "why": "检索有返回"}
    if VERDICT_RULES["NO_RESULT_PATTERNS"].search(head):
        return {"v": "deadend", "why": "退出正常但无输出，判为扑空"}
    return {"v": "ok", "why": "退出正常且有输出"}


def step_verdict(tools):
    """步级判定：返回该步最坏判定的工具（其 v/why 即步判定与依据）；无参与投票的工具时 None。"""
    worst = None
    for t in tools:
        if worst is None or SEV.get(t["v"], 0) > SEV.get(worst["v"], 0):
            worst = t
    return worst


_ARG_SPLIT = re.compile(r"[^A-Za-z0-9_一-鿿./-]+")   # JS 无 u 标志 \w 的显式等价


def _arg_tokens(s):
    return {w for w in _ARG_SPLIT.split(str(s)) if len(w) > 2}


def arg_similarity(a, b):
    """参数相似度：token 集 Jaccard，用于识别「几乎相同的重复调用」。"""
    ta, tb = _arg_tokens(a), _arg_tokens(b)
    if not ta or not tb:
        return 0
    inter = len(ta & tb)
    return inter / (len(ta) + len(tb) - inter)


def mark_retry_clusters(calls):
    """盲目重试簇标注（上游借 AgentLens 的确定性检测）：时间序上连续的
    「同工具＋参数相似」调用簇，且簇内至少一次失败，才算盲目重试。就地改判
    簇内非失败调用 v='retry'；失败调用保持 error、依据追加簇上下文。返回命中簇数。
    calls 必须按时间序传入，且只传已有结果的调用。"""
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
                        c["why"] = (c.get("why") or "") + f"；处于连续重试簇（同一操作共 {length} 次）"
                    else:
                        c["v"] = "retry"
                        c["why"] = f"同一操作连续重试 {length} 次（其中 {fails} 次失败），判为盲目重试"
        start = i
    return clusters


if __name__ == "__main__":
    # 三层判定
    assert tool_verdict({"name": "bash", "res": "x", "err": True})["v"] == "error"
    assert tool_verdict({"name": "bash", "res": "[stderr] ValueError: bad\n", "err": False})["v"] == "error"
    # 「病历≠发病」：引用悬在长文本中部，头尾窗口都够不着
    quoted = "commit log " * 60 + " upstream returns HTTP 400 when sound=true " + "more " * 300
    assert tool_verdict({"name": "bash", "res": quoted, "err": False})["v"] == "ok"
    tail_crash = "build output " * 200 + " [stderr] Traceback (most recent call last): boom"
    assert tool_verdict({"name": "bash", "res": tail_crash, "err": False})["v"] == "error", "末尾窗口"
    assert tool_verdict({"name": "bash", "res": "command not found: foobar", "err": False})["v"] == "error", "开头弱特征"
    # 工具分类
    assert tool_verdict({"name": "write", "res": "", "err": False})["v"] == "ok", "写入类不判扑空"
    assert tool_verdict({"name": "grep", "res": "", "err": False})["v"] == "deadend"
    assert tool_verdict({"name": "grep", "res": "hit: 1 line", "err": False})["v"] == "ok"
    assert tool_verdict({"name": "unknown_tool", "res": "", "err": False})["v"] == "deadend"
    assert tool_verdict({"name": "unknown_tool", "res": "did stuff", "err": False})["v"] == "ok"
    # 步级=最坏
    sv = step_verdict([{"v": "ok", "why": "a"}, {"v": "error", "why": "b"}, {"v": "deadend", "why": "c"}])
    assert sv["v"] == "error"
    assert step_verdict([]) is None
    # Jaccard 与簇
    assert arg_similarity("pattern=foo path=src", "pattern=foo path=lib") > 0
    assert arg_similarity("", "x") == 0
    calls = [
        {"name": "bash", "args": "python3 gen_timeline.py --check", "v": "error", "why": "w"},
        {"name": "bash", "args": "python3 gen_timeline.py --check --fast", "v": "ok", "why": "w"},
    ]
    assert mark_retry_clusters(calls) == 1
    assert calls[1]["v"] == "retry" and "盲目重试" in calls[1]["why"]
    assert "重试簇" in calls[0]["why"]
    # 无失败的连续编辑不冤枉
    okcalls = [{"name": "edit", "args": "f.py", "v": "ok", "why": ""},
               {"name": "edit", "args": "f.py", "v": "ok", "why": ""}]
    assert mark_retry_clusters(okcalls) == 0 and okcalls[0]["v"] == "ok"
    # 不同工具打断簇
    mixed = [{"name": "bash", "args": "same thing here", "v": "error", "why": ""},
             {"name": "grep", "args": "same thing here", "v": "ok", "why": ""}]
    assert mark_retry_clusters(mixed) == 0
    print("trace_verdict selfcheck ok — 三层/头尾窗口/病历不发病/分类/最坏聚合/Jaccard 簇")
