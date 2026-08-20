"""协力者执行：在格子里跑第三方 agent 的非交互模式，完事把回话投进信箱。

为什么用非交互模式（`codex exec` / `claude -p` / `gemini -p`）而不是刮屏：
**进程退出＝确定的完成信号**，stdout 是干净文本，不用判闲、不用消毒 ANSI。
（herdr 只能刮屏是因为它是终端复用器，除了屏幕拿不到东西；我们能直接起进程。）
"""
import os
import shlex

TAIL_CAP = 20000        # 投信箱的回话上限，超了掐头留尾（信箱不是日志仓）


def build_argv(argv, prompt):
    """命令行＝LO 给的 argv ＋ 末位追加提示词。空 argv 直接报错，别跑出个空进程。"""
    if not argv:
        raise ValueError("argv 不能为空——要跑哪个 CLI 得说清楚")
    return [*argv, prompt]


def label_for(argv, label=None):
    """信箱里的发信人名：LO 给了就用，没给取命令名（codex/claude/…）。"""
    return label or (os.path.basename(argv[0]) if argv else "ally")


def summarize(text, cap=TAIL_CAP):
    """回话太长时掐头留尾——尾部通常是结论，开头交代上下文。"""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    head, tail = text[: cap // 3], text[-(cap // 3 * 2):]
    return f"{head}\n\n……（中间省略 {len(text) - len(head) - len(tail)} 字）……\n\n{tail}"


def notify(task_id, text, *, sender, to_addr="last-order"):
    """替协力者给 LO 发一封信——御坂会自己 SendMessage 上报，协力者不会，
    守护进程替它发，于是 LO 两边收信路一致（体感相同）。
    失败也照发：没登录/命令错要让 LO 看见，不无声吞掉。"""
    from misaka.extensions import messages
    con = messages.connect()
    try:
        messages.send(con, to_addr, text, summary=f"协力者 {sender}·卡 {task_id}",
                      sender=sender, task_id=task_id)
    finally:
        con.close()


CONTRACT = """（以下是任务合同。做完把产物写进当前目录；你的最后一段输出会作为交卷摘要。

中途要联系编排官就在当前目录运行：
    misaka tell "你要说的话"
——卡住了、发现前提被推翻、需要人点头，都当场说，别憋到最后。）

{body}
"""


def card_prompt(row):
    """卡 → 交给协力者的提示词：合同原文（与御坂拿到的是同一份 body）。"""
    body = (row["body"] or "").strip() or row["title"]
    return CONTRACT.format(body=body).strip()


def write_report(workspace, exit_code, output, *, assignee):
    """协力者不会写 report.json——**守护进程替它写**，于是看板那套
    「检测交卷 → mark_verifying → 红队验收」一行都不用改（宪法⑥不变：
    只有红队能标 done，协力者与御坂一视同仁）。返回 (交卷了吗, 摘要)。"""
    import json as _json
    if exit_code != 0:
        return False, f"协力者 {assignee} 退出码 {exit_code}：{summarize(output, 500)}"
    tail = summarize(output, 2000).strip()
    if not tail:
        return False, f"协力者 {assignee} 没有任何输出"
    artifacts = []
    try:                     # 它落在工作区的文件就是产物（report.json 自己除外；
        # 目录不进清单——check_report 对每个 artifact 要求 S_ISREG，混入即整份拒收）
        artifacts = sorted(f for f in os.listdir(workspace)
                           if not f.startswith(".") and f != "report.json"
                           and os.path.isfile(os.path.join(workspace, f)))
    except OSError:
        pass
    report = {"schema_version": 1, "status": "done",
              "summary": tail[-1500:],
              "artifacts": artifacts,
              "uncertain": [f"产出由协力者 {assignee} 生成，未经御坂复核"]}
    with open(os.path.join(workspace, "report.json"), "w", encoding="utf-8") as f:
        _json.dump(report, f, ensure_ascii=False)
    return True, report["summary"]


def finish(workspace, exit_code, output, *, assignee, task_id):
    """协力者进程退出后的收尾（守护进程一处调用）：
    ① 替它写 report.json → 看板那套交卷/验收逻辑零改动
    ② 替它给 LO 发信 → 与御坂主动上报同一条收信路
    返回 (交卷了吗, 摘要)。"""
    ok, summary = write_report(workspace, exit_code, output, assignee=assignee)
    head = "干完交卷了" if ok else "没能交卷"
    try:
        notify(task_id, f"协力者 {assignee} {head}（卡 {task_id}）：\n\n{summary}",
               sender=assignee)
    except Exception:  # noqa: BLE001 - 发信失败不拖累交卷判定
        pass
    return ok, summary


def describe(argv, prompt):
    """给人看的一行：起了什么命令（面板标题/日志用）。"""
    shown = " ".join(shlex.quote(a) for a in argv)
    head = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return f"{shown} ⟨{head[:40]}⟩" if head else shown


if __name__ == "__main__":
    assert build_argv(["codex", "exec"], "看看") == ["codex", "exec", "看看"]
    try:
        build_argv([], "x")
        raise AssertionError("空 argv 该报错")
    except ValueError:
        pass
    assert label_for(["/usr/bin/codex", "exec"]) == "codex"
    assert label_for(["codex"], "审查员") == "审查员"
    long = "头" * 100 + "腰" * 30000 + "尾" * 100
    cut = summarize(long)
    assert len(cut) < len(long) and cut.startswith("头") and cut.endswith("尾"), "掐头留尾"
    assert "省略" in cut
    assert summarize("短的") == "短的"
    assert "codex exec" in describe(["codex", "exec"], "查个 bug")
    assert "⟨查个 bug⟩" in describe(["codex", "exec"], "查个 bug")

    # 合同：协力者拿到的与御坂是同一份 body
    assert "扫档案" in card_prompt({"body": "扫档案", "title": "x"})
    assert "只有标题" in card_prompt({"body": "", "title": "只有标题"})

    # 替协力者交卷：看板状态机因此零改动
    import json as _j
    import tempfile
    ws = tempfile.mkdtemp()
    open(os.path.join(ws, "out.md"), "w").write("产物")
    ok, summary = write_report(ws, 0, "分析完毕：三个疑点", assignee="codex")
    assert ok and "三个疑点" in summary
    rep = _j.load(open(os.path.join(ws, "report.json"), encoding="utf-8"))
    assert rep["status"] == "done" and "out.md" in rep["artifacts"]
    assert rep["uncertain"] and "未经御坂复核" in rep["uncertain"][0], "协力者产出要标出处"
    ws2 = tempfile.mkdtemp()
    assert not write_report(ws2, 1, "boom", assignee="codex")[0], "非零退出＝没交卷"
    assert not os.path.exists(os.path.join(ws2, "report.json"))
    assert not write_report(tempfile.mkdtemp(), 0, "   ", assignee="c")[0], "空输出＝没交卷"
    print("ally.runner selfcheck ok — 命令拼装/发信人/掐头留尾/合同/替交卷 均正确")
