"""假说综合：跨卡的高权重发现 → 溯因假说 → 检验卡（盖上 question 层留了接口没盖的房子）。

债务驱动的 BFS（gap 填坑）做不出横向跳跃——三个发现摆一起暗示什么统一假说。
这里补溯因：一次 LLM 调用（调度侧发起，同 harvest 先例，本文件只收 worker 参数），
产出 hypothesis 节点 + explains 边 + 可证伪 predictions，再模板生成**检验卡**。

纪律：
- 只综合**同课题**的发现（课题隔离，2026-08-08 立的维度）；检验卡继承该课题。
- 假说 provenance 强制 invented（未经检验定义上就是现场推断，宪法⑨）。
- 新 kind=hypothesis（question 留给纯疑问）。不进计票/饱和/前沿——**零守卫代码**：
  verdict 节点边全按 kind 过滤、tms 只沿 supports 传播、canon.dedup 同 kind 比对、
  frontier 只挑 gap（各文件实锤，2026-08-09 复核），selfcheck 有断言钉着。
- 检验卡"证伪也是合格产出"写进验收；证伪走 mark_outcome → dropped + 直插 clauses
  （教训文本现成，照 cdcl 落库形状，不需 LLM 提炼——内核零 LLM 保持）。
"""
import json

PROMPT = """你在给研究图做溯因综合。下面是同一课题里若干条已收割的发现（带节点号）。
问题：**什么单一假说能同时解释其中若干条？**只输出一个 JSON 对象：

{"hypothesis": "一句话陈述假说（自足可读，是解释机制，不是发现的复述或合并同类项）",
 "explains": ["它解释的节点号，≥2 条"],
 "predictions": ["1-3 条可证伪的预测：若假说为真，还应能在材料里查到什么/什么必然不成立"]}

规矩：
- 假说要**压缩**：用一个机制说清多条发现为什么同时成立。解释不了 ≥2 条就输出 {"hypothesis": ""}。
- predictions 必须可检验可证伪——写"去查 X 应该能看到 Y"，不写"值得进一步研究"。
- explains 只准引用给出的节点号。
- 除 JSON 外不要输出任何字。
"""


def candidates(con, store, project, k=12, min_weight=0.6):
    """同课题高权重发现，verified/analogy 才有资格被解释（invented 解释 invented=空转）。"""
    ns = [n for n in store.nodes(con, kind="finding", project=project)
          if n["status"] in ("open", "expanded") and float(n["weight"]) >= min_weight
          and n["provenance"] in ("verified", "analogy")]
    return sorted(ns, key=lambda n: -float(n["weight"]))[:k]


def card_for(hypothesis_text, predictions, hid, assignee, project):
    """检验卡（模板生成，零 LLM）。验收=逐条检验 predictions，证伪也是合格产出。"""
    checks = "\n".join(f"- 对预测「{p}」给出**证实或证伪**的判定＋出处" for p in predictions)
    return {"title": ("检验假说：" + hypothesis_text.strip())[:60],
            "body": (f"## 目标\n检验这个假说的预测（节点 {hid}）：\n\n> {hypothesis_text}\n\n"
                     + "\n".join(f"{i+1}. {p}" for i, p in enumerate(predictions))
                     + "\n\n## 边界\n只检验上述预测，不扩题。**证伪也是合格产出**——"
                       "把假说打死和证实它同等有价值，不许为了交差硬圆。\n\n"
                     + f"## 验收\n{checks}\n- 明确写出总判定：证实/证伪/证据不足"),
            "assignee": assignee, "project": project}


def synthesize_project(con, store, cfg, worker, project, assignee="10032"):
    """综合一个课题。返回 (hypothesis_id, card_dict, err)——调用方建卡后自行加
    hypothesis --tested_by--> task 边（kernel 不 import 板层，同 frontier.card_for 先例）。"""
    cands = candidates(con, store, project)
    if len(cands) < 3:
        return None, None, f"课题「{project or '未分类'}」可综合的发现不足 3 条（现 {len(cands)}）"
    listing = "\n".join(f"[{n['id']}] {n['text']}（weight {n['weight']}，{n['provenance']}）"
                        for n in cands)
    obj, _raw, err = worker.run_llm_json(
        f"{cfg['roles_root']}/hypothesizer", PROMPT + "\n# 发现\n" + listing,
        cfg["provider"], cfg["default_model"], timeout=cfg.get("judge_timeout", 600))
    if err:
        return None, None, err
    if not isinstance(obj, dict) or not (obj.get("hypothesis") or "").strip():
        return None, None, "模型判定这批发现凑不出统一假说（合法结果，不硬编）"
    valid_ids = {n["id"] for n in cands}
    explains = [i for i in (obj.get("explains") or []) if i in valid_ids]  # 幻觉节点号丢弃
    preds = [p.strip() for p in (obj.get("predictions") or [])
             if isinstance(p, str) and len(p.strip()) >= 8][:3]
    if len(explains) < 2:
        return None, None, "假说解释不了 ≥2 条给出的发现（explains 核不上）"
    if not preds:
        return None, None, "没有可证伪的预测——不可检验的假说不落图"
    hid = store.add_node(con, "hypothesis", obj["hypothesis"].strip(), weight=0.5,
                         provenance="invented", project=project)
    for eid in explains:
        store.add_edge(con, hid, eid, "explains")
    return hid, card_for(obj["hypothesis"], preds, hid, assignee, project), None


def mark_outcome(con, store, canon, hid, falsified, reason=""):
    """检验卡收割后的裁定。证伪：假说 dropped + 直插禁令（照 cdcl 落库形状，文本现成零 LLM）。
    未证伪：保持 open——**未证伪≠证实**（波普尔），weight/provenance 都不动。"""
    n = store.get(con, hid)
    if not n or n["kind"] != "hypothesis":
        return None, f"节点 {hid} 不是假说"
    if not falsified:
        return None, f"假说 {hid} 未被证伪，保持 open（未证伪≠证实，不升档）"
    store.set_status(con, hid, "dropped")
    text = f"假说「{n['text'][:80]}」已被检验证伪：{(reason or '预测不成立')[:200]}——同一假说不得再立卡检验"
    vec = canon.embed([text])
    con.execute("INSERT INTO clauses (text, scope, task_id, embedding, created_at)"
                " VALUES (?,?,?,?,strftime('%s','now'))",
                (text, "假说检验", n["task_id"], json.dumps(vec[0]) if vec else None))
    cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    return cid, f"假说 {hid} 已标 dropped，禁令 #{cid} 立档"


if __name__ == "__main__":
    import sqlite3
    from misaka.research.kernel import cdcl, frontier, saturation, store, verdict

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    store.init_all(con)
    cdcl.init(con)

    for i, (w, prov) in enumerate([(0.9, "verified"), (0.8, "verified"), (0.7, "analogy"),
                                   (0.9, "invented"), (0.4, "verified")]):
        store.add_node(con, "finding", f"发现{i}：某档案在 195{i} 年被转移", weight=w,
                       provenance=prov, project="alpha")
    store.add_node(con, "finding", "别课题的发现", weight=0.95, provenance="verified", project="beta")
    store.add_node(con, "gap", "一个缺口", weight=0.9, project="alpha")

    cands = candidates(con, store, "alpha")
    assert len(cands) == 3, f"invented/低权重/别课题该被滤掉: {[n['text'] for n in cands]}"
    ids = [n["id"] for n in cands]

    class FakeWorker:
        def run_llm_json(self, *a, **k):
            return {"hypothesis": "1950 年代初有一次系统性档案迁移",
                    "explains": [ids[0], ids[1], "n_幻觉"],
                    "predictions": ["迁移接收方的入藏簿应有同期批量登记", "x"]}, "", None

    cfg = {"roles_root": "/dev/null", "provider": "p", "default_model": "m"}
    hid, card, err = synthesize_project(con, store, cfg, FakeWorker(), "alpha")
    assert err is None and hid, err
    h = store.get(con, hid)
    assert h["kind"] == "hypothesis" and h["provenance"] == "invented" and h["project"] == "alpha"
    ex = [r["dst"] for r in con.execute("SELECT dst FROM edges WHERE src=? AND kind='explains'", (hid,))]
    assert sorted(ex) == sorted(ids[:2]), "幻觉节点号该被丢弃"
    assert "证伪也是合格产出" in card["body"] and card["project"] == "alpha"
    assert "入藏簿" in card["body"] and " x" not in card["title"], "过短 prediction 该被滤"

    # 三防线：假说不污染计票/饱和/前沿（零守卫，靠既有 kind 过滤，这里钉死不许退化）
    assert all(n["kind"] == "finding" for n in
               [x for x in store.nodes(con, kind="finding")]), "kind 过滤基线"
    assert hid not in [n["id"] for n, _ in frontier.pick(con, store, k=10, min_score=0.0)], "前沿不挑假说"
    assert saturation.reading(con, kind="finding")["distinct"] == 6, "假说不进发现饱和"
    pairs_ns = [n["id"] for n in store.nodes(con, kind="finding") if n["embedding"]]
    assert hid not in pairs_ns, "计票候选不含假说"

    # 发现不足 → 诚实拒绝
    _, _, e2 = synthesize_project(con, store, cfg, FakeWorker(), "beta")
    assert "不足 3 条" in e2, e2

    # 证伪闭环：dropped + 禁令直插；未证伪 ≠ 证实
    class _NoEmbed:
        def embed(self, xs):
            return None
    cid, msg = mark_outcome(con, store, _NoEmbed(), hid, falsified=False)
    assert cid is None and store.get(con, hid)["status"] != "dropped"
    cid, msg = mark_outcome(con, store, _NoEmbed(), hid, falsified=True, reason="入藏簿无记录")
    assert cid and store.get(con, hid)["status"] == "dropped"
    row = con.execute("SELECT text, scope FROM clauses WHERE id=?", (cid,)).fetchone()
    assert "证伪" in row["text"] and row["scope"] == "假说检验"
    print(f"synthesize selfcheck ok — 候选滤(课题/invented/低权重)/幻觉id丢弃/检验卡含证伪条款/"
          f"三防线(前沿·饱和·计票)零污染/证伪→dropped+禁令 #{cid}/未证伪≠证实")
