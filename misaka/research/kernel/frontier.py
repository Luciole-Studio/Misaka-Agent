"""前沿调度：从 gap 节点里挑下一批该挖的，直接生成卡片（零 LLM）。

打分 v2：score = weight × 新鲜度衰减 × 校准修正。
修正来自 calibration（harvester 预估 vs 实际回报的收缩式对账）——这就是本文件
初版 docstring 预注册的"等有了历史数据再上"的目标态，2026-08-09 兑现。
零样本时修正恒 1.0，行为=初版纯先验。
"""
import time

from misaka.research.kernel import calibration

HALF_LIFE_DAYS = 7.0  # 老 gap 慢慢降权，避免前沿被陈年问题占死


def score(node, now=None, corrections=None):
    now = now or int(time.time())
    age_days = max(0.0, (now - node["created_at"]) / 86400.0)
    base = float(node["weight"]) * (0.5 ** (age_days / HALF_LIFE_DAYS))
    if corrections:
        base *= corrections.get(calibration.bucket_of(float(node["weight"])), 1.0)
    return base


def pick(con, store, k=2, min_score=0.15, project=None):
    """挑 top-k 未挖的 gap。返回 [(node, score)]。project=None 全局，传了只挑该课题的缺口。"""
    now = int(time.time())
    corr = calibration.corrections(con, project=project)
    ranked = sorted(((n, score(n, now, corr)) for n in store.nodes(con, kind="gap", status="open", project=project)),
                    key=lambda x: -x[1])
    return [(n, s) for n, s in ranked[:k] if s >= min_score]


def card_for(node, assignee):
    """gap 节点 → 卡片（title/body/assignee）。模板生成，不花 LLM。"""
    text = node["text"].strip().rstrip("？?。.")
    title = ("补缺：" + text)[:60]
    body = (f"## 目标\n回答这个研究缺口，并把结论写成 markdown 文件：\n\n> {node['text']}\n\n"
            f"（缺口来自前一轮研究的收割，节点 {node['id']}）\n\n"
            "## 边界\n只回答这一个缺口；不重复已有结论；查不到就在产物里明写「未能确证」，不许编。\n\n"
            "## 验收\n- 产出一个 .md 文件，正面回答上述缺口\n"
            "- 每条结论带出处（文献/网页/档号级别）\n"
            "- 确实查不到的部分明确标注「未能确证」而非编造")
    return {"title": title, "body": body, "assignee": assignee}


if __name__ == "__main__":
    now = int(time.time())
    fresh = {"weight": 0.8, "created_at": now}
    old = {"weight": 0.8, "created_at": now - 14 * 86400}
    assert score(fresh, now) > score(old, now) * 3.5, (score(fresh, now), score(old, now))
    n = {"id": "n_x", "text": "1943 年该档案馆是否已成立？", "weight": 0.9, "created_at": now}
    c = card_for(n, "10032")
    assert "## 验收" in c["body"] and c["assignee"] == "10032"
    print(f"frontier selfcheck ok — 新鲜 {score(fresh, now):.3f} vs 两周前 {score(old, now):.3f}")
