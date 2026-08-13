"""深研轮次：记账与判停（/research 模式内核件，设计见 docs/design/research-mode.md）。

记账在 PROJECT.md「## 深研日志」——**行数即轮数**（文件即真相；驱动器无状态，
崩了重启数行数就知道跑到哪，R9）。判停＝三闸＋手动（R11）：
轮数用尽｜预算触顶｜前沿空｜手动停。饱和读数只展示不进判停（D3：停机是经济决策）。
"""
HEADING = "## 深研日志"


def _section_lines(text):
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == HEADING)
    except StopIteration:
        return []
    out = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip():
            out.append(line.strip())
    return out


def rounds_done(project_md_path):
    """已跑轮数＝日志节的非空行数。文件/节不存在＝0。"""
    try:
        with open(project_md_path, encoding="utf-8") as f:
            return len(_section_lines(f.read()))
    except OSError:
        return 0


def append_round(project_md_path, line):
    """追加一行轮次日志（没有日志节先补节）。一行＝一轮，换行一律压平。
    契约：日志节保持在文件尾——人工在其后加节会使后续行不计轮（见自检末例）。"""
    line = " ".join(str(line).split())
    try:
        with open(project_md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    has_heading = any(l.strip() == HEADING for l in text.splitlines())
    with open(project_md_path, "a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        if not has_heading:
            f.write(f"\n{HEADING}\n")
        f.write(line + "\n")


def should_stop(*, rounds_done, max_rounds, budget_mode, frontier_empty,
                stop_requested=False):
    """满足任一即停。max_rounds=None＝不限（R11，全局预算顶仍由 budget_mode 兜底）。
    返回 (停?, 原因)。"""
    if stop_requested:
        return True, "手动停止"
    if budget_mode == "stop":
        return True, "预算触顶"
    if max_rounds is not None and rounds_done >= max_rounds:
        return True, f"轮数用尽（{rounds_done}/{max_rounds}）"
    if frontier_empty:
        return True, "前沿空（无刺/缺口可展开）"
    return False, None


if __name__ == "__main__":
    import os
    import tempfile

    md = os.path.join(tempfile.mkdtemp(), "PROJECT.md")
    assert rounds_done(md) == 0, "文件不存在＝0 轮"
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 课题\n\n## 目标\n查明真相")   # 尾部无换行，考验补行
    assert rounds_done(md) == 0, "没有日志节＝0 轮"
    append_round(md, "第 1 轮：派 4 张卡\n（换行注入）")
    append_round(md, "第 2 轮：收割 3 发现 2 刺")
    assert rounds_done(md) == 2, rounds_done(md)
    text = open(md, encoding="utf-8").read()
    assert text.count(HEADING) == 1, "日志节只建一次"
    assert "换行注入" in text and "\n（换行注入" not in text, "多行必须压平成一行"
    with open(md, "a", encoding="utf-8") as f:
        f.write("## 后面的节\n这行不算轮次\n")
    append_round(md, "第 3 轮")
    # 日志节被后来的节截断后，追加走文件尾——行数仍按节内计
    assert rounds_done(md) == 2, "节外的行不计轮"

    matrix = [
        (dict(rounds_done=0, max_rounds=None, budget_mode="normal", frontier_empty=False),
         (False, None)),
        (dict(rounds_done=0, max_rounds=None, budget_mode="normal", frontier_empty=True),
         (True, "前沿空（无刺/缺口可展开）")),
        (dict(rounds_done=5, max_rounds=5, budget_mode="normal", frontier_empty=False),
         (True, "轮数用尽（5/5）")),
        (dict(rounds_done=0, max_rounds=5, budget_mode="stop", frontier_empty=False),
         (True, "预算触顶")),
        (dict(rounds_done=0, max_rounds=None, budget_mode="beast", frontier_empty=False),
         (False, None)),   # beast＝强制交卷档，不是停机
        (dict(rounds_done=0, max_rounds=None, budget_mode="normal", frontier_empty=False,
              stop_requested=True), (True, "手动停止")),
    ]
    for kwargs, want in matrix:
        assert should_stop(**kwargs) == want, (kwargs, should_stop(**kwargs))
    print("rounds selfcheck ok — 行数即轮数/压平/单节/判停矩阵 6 例全对")
