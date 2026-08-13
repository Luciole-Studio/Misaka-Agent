"""综合卡：把已验收卡的产物汇成一张 REPORT.md 合同（宪法③：综合一枪成稿）。"""
import json
import os

from misaka.extensions.board import db, validate
from misaka.research.kernel import guard


def gather(con, ids=None):
    """可综合的 done 卡（综合器自己的卡除外，防自我吞噬）。"""
    rows = [db.get(con, i) for i in ids] if ids else db.by_status(con, "done")
    return [r for r in rows if r and r["status"] == "done" and r["assignee"] != "synthesizer"]


def card_body(rows, title):
    """各卡 report 摘要（不可信包裹）＋产物路径 → 综合合同正文。"""
    parts = []
    for r in rows:
        rep = {}
        try:
            with open(os.path.join(r["workspace"], "report.json"), encoding="utf-8") as f:
                rep = json.load(f)
        except OSError:
            pass
        arts = "\n".join(f"  - {os.path.join(r['workspace'], a)}" for a in rep.get("artifacts", []))
        parts.append(f"### [{r['id']}] {r['title']}\n"
                     + guard.untrusted(f"{r['id']}-summary", rep.get("summary", ""))
                     + f"产物（绝对路径，用 read 读）：\n{arts}")
    return (f"## 目标\n把以下 {len(rows)} 张已验收卡片的产物综合成《{title}》，写 REPORT.md。\n\n"
            + "\n\n".join(parts)
            + "\n\n## 边界\n不引入来源之外的新事实。\n\n## 验收\n- REPORT.md 存在，含标题与「材料清单」节\n"
              "- 每节论断带 [卡id/文件] 来源标注\n- 覆盖以上每一张卡（材料清单里逐卡出现）")


def create(con, rows, title):
    """建综合卡上板。返回 tid。"""
    return db.create_task(con, f"综合：{title}", body=card_body(rows, title),
                          assignee="synthesizer", timeout_seconds=1200)
