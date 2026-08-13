"""裁决：找出互相冲突的发现，标出谁站得住。

两段：
① 矛盾预筛（省 LLM 钱的关键）——嵌入相似度落在"像但没同到该合流"的带里才送审：
   同义会被 canon 合流（≥0.78），完全无关的没必要审（<0.55）。夹在中间的才是候选。
② LLM 裁判定性 → contradicts 边 → Dung grounded 标注：
   被"站得住"的对手攻击 = out（被驳倒）；没被攻击或攻击者全 out = in（站得住）；
   互攻不分胜负 = undecided（对峙点——如实登记，不硬裁）。
"""
import json

BAND = (0.55, 0.78)  # 下界=值得一看，上界=canon 的合流线（同上，换模型须重量）

JUDGE_PROMPT = """判断下面两条研究陈述的关系，只输出 JSON：
{"relation": "contradict|compatible|same", "reason": "一句话"}

- contradict：两者不能同时为真（数字冲突、事实互斥、因果相反）。
- same：说的是同一件事（措辞不同而已）。
- compatible：都能成立（互补、不同侧面、无交集）。
拿不准选 compatible——**宁可漏判也不许无中生有制造冲突**。

A: {a}
B: {b}
"""


def candidates(con, store, canon, band=BAND):
    """返回待审对 [(node_a, node_b, sim)]，已排除已有裁决的对。"""
    ns = [n for n in store.nodes(con, kind="finding") if n["status"] in ("open", "expanded") and n["embedding"]]
    judged = {(r[0], r[1]) for r in con.execute(
        "SELECT src, dst FROM edges WHERE kind IN ('contradicts','compatible')")}
    out = []
    for i, a in enumerate(ns):
        va = json.loads(a["embedding"])
        for b in ns[i + 1:]:
            if (a["id"], b["id"]) in judged or (b["id"], a["id"]) in judged:
                continue
            sim = canon.cosine(va, json.loads(b["embedding"]))
            if band[0] <= sim < band[1]:
                out.append((a, b, round(sim, 3)))
    return sorted(out, key=lambda x: -x[2])


def judge_pairs(con, store, pairs, cfg, worker, limit=8):
    """逐对送审，写 contradicts/compatible 边。返回 (冲突数, 已审数)。"""
    conflicts = 0
    for a, b, _sim in pairs[:limit]:
        obj, _raw, err = worker.run_llm_json(
            f"{cfg['roles_root']}/redteam",
            JUDGE_PROMPT.replace("{a}", a["text"]).replace("{b}", b["text"]),
            cfg["provider"], cfg["default_model"], timeout=180)
        rel = (obj or {}).get("relation") if not err else None
        if rel == "contradict":
            store.add_edge(con, a["id"], b["id"], "contradicts")
            store.add_edge(con, b["id"], a["id"], "contradicts")  # 互攻：Dung 的攻击是有向的，冲突是双向的
            conflicts += 1
        elif rel in ("compatible", "same"):
            store.add_edge(con, a["id"], b["id"], "compatible")  # 记账防重审
    return conflicts, min(len(pairs), limit)


def label(con, store):
    """Dung grounded 标注。返回 {node_id: 'in'|'out'|'undecided'}。"""
    ns = [n for n in store.nodes(con, kind="finding") if n["status"] in ("open", "expanded")]
    ids = {n["id"] for n in ns}
    attackers = {i: set() for i in ids}
    for src, dst in con.execute("SELECT src, dst FROM edges WHERE kind='contradicts'"):
        if src in ids and dst in ids:
            attackers[dst].add(src)

    labels = {}
    changed = True
    while changed:  # 最小不动点：无攻击者→in；被 in 攻击→out；反复到稳定
        changed = False
        for i in ids:
            if i in labels:
                continue
            live = [a for a in attackers[i] if labels.get(a) != "out"]
            if not live:
                labels[i] = "in"
                changed = True
            elif any(labels.get(a) == "in" for a in live):
                labels[i] = "out"
                changed = True
    for i in ids:
        labels.setdefault(i, "undecided")  # 互攻僵持＝对峙点，不硬裁
    return labels


if __name__ == "__main__":
    import sqlite3
    import sys
    sys.path.insert(0, ".")
    from misaka.research.kernel import store
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    store.init(con)
    a = store.add_node(con, "finding", "甲：数字是两万")
    b = store.add_node(con, "finding", "乙：数字是三万")
    c = store.add_node(con, "finding", "丙：与数字无关的独立发现")
    store.add_edge(con, a, b, "contradicts")
    store.add_edge(con, b, a, "contradicts")
    lab = label(con, store)
    assert lab[c] == "in", lab                      # 无人攻击 → 站得住
    assert lab[a] == lab[b] == "undecided", lab      # 互攻僵持 → 对峙点
    d = store.add_node(con, "finding", "丁：被丙驳倒")
    store.add_edge(con, c, d, "contradicts")         # 单向：丙攻丁
    lab2 = label(con, store)
    assert lab2[d] == "out" and lab2[c] == "in", lab2
    print("verdict selfcheck ok — 互攻→对峙点；被站得住者攻击→驳倒；孤立→站得住")
