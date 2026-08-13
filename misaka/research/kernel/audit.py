"""审计抽查：判官会不会放水？——蓄水池抽样 + 安慰剂突变测试。

安慰剂格的正确落地：把已判通过的卡产物**复制一份并故意破坏**再送审。
判官应当判 FAIL；若仍判 PASS，就是放水实锤（记 placebo_failed 事件）。
这是对判官本身的变异测试，不是对 Sister 的。
"""
import json
import os
import random
import shutil
import tempfile

from misaka.research.kernel import guard


def reservoir(items, k, rng=None):
    """蓄水池抽样：流式均匀取 k 个，不必先装全量。"""
    rng = rng or random
    out = []
    for i, x in enumerate(items):
        if i < k:
            out.append(x)
        else:
            j = rng.randint(0, i)
            if j < k:
                out[j] = x
    return out


def corrupt(text):
    """制造一处**该被抓到**的破坏：删掉最后一个非空行。返回 (新文本, 被删内容)。"""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            removed = lines[i]
            return "\n".join(lines[:i] + lines[i + 1:]) + "\n", removed
    return text, None


def make_placebo(workspace, artifacts):
    """复制工作区并破坏一个产物。返回 (临时目录, 被删内容) 或 (None, None)。"""
    targets = [a for a in artifacts
               if a.lower().endswith((".md", ".txt", ".json", ".py")) and a != "report.json"]
    if not targets:
        return None, None
    tmp = tempfile.mkdtemp(prefix="misaka-placebo-")
    dst = os.path.join(tmp, "ws")
    shutil.copytree(workspace, dst, ignore=shutil.ignore_patterns("session", ".skills-ro"))
    victim = os.path.join(dst, targets[0])
    with open(victim, encoding="utf-8", errors="replace") as f:
        text = f.read()
    new, removed = corrupt(text)
    if removed is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None
    with open(victim, "w", encoding="utf-8") as f:
        f.write(new)
    return dst, removed


def run_placebo(con, db, task, cfg, worker):
    """对一张已通过的卡跑安慰剂。返回 'caught' | 'leaked' | 'skipped:<原因>'。"""
    ws = task["workspace"] or ""
    try:
        with open(os.path.join(ws, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except OSError:
        return "skipped:no-report"
    fake_ws, removed = make_placebo(ws, report.get("artifacts", []))
    if not fake_ws:
        return "skipped:no-text-artifact"
    try:
        prompt = (f"# 待验收卡片合同\n标题：{task['title']}\n\n{task['body']}\n\n"
                  f"# 交卷 report.json\n{guard.untrusted('report.json', json.dumps(report, ensure_ascii=False))}\n"
                  "工作目录就是该卡工作区。先从合同「## 验收」节列判据，再用 read 工具逐条核对产物实物，"
                  '只输出 verdict JSON：{"pass": true|false, "reasons": ["…"], "must_fix": ["…"]}')
        obj, _raw, err = worker.run_llm_json(
            os.path.join(cfg["roles_root"], "redteam"), prompt,
            cfg["provider"], cfg["default_model"],
            cwd=fake_ws, tools=["read"], timeout=cfg.get("judge_timeout", 600))
    finally:
        shutil.rmtree(os.path.dirname(fake_ws), ignore_errors=True)
    if err or not isinstance(obj, dict) or not isinstance(obj.get("pass"), bool):
        return f"skipped:judge-error({err})"
    if obj["pass"]:
        db.add_event(con, task["id"], "placebo_failed",
                     {"removed": removed[:120], "reasons": obj.get("reasons", [])[:2]})
        return "leaked"
    db.add_event(con, task["id"], "placebo_caught", {"removed": removed[:120]})
    return "caught"


if __name__ == "__main__":
    rng = random.Random(0)
    sample = reservoir(range(1000), 10, rng)
    assert len(sample) == 10 and len(set(sample)) == 10, sample
    counts = [0] * 10
    for _ in range(2000):  # 均匀性粗验：每个位置都该被不同元素占过
        s = reservoir(range(10), 3, random.Random())
        for x in s:
            counts[x] += 1
    assert min(counts) > 300, counts  # 均匀则每个 ≈600，远大于 300
    text = "第一行\n第二行\n\n## 校验通过\n"
    new, removed = corrupt(text)
    assert removed == "## 校验通过" and "校验通过" not in new, (new, removed)
    assert corrupt("   \n\n")[1] is None, "全空白无可破坏，应返回 None"
    print(f"audit selfcheck ok — 蓄水池均匀(min={min(counts)})；破坏器删掉「{removed}」")
