"""收割：把一张已验收卡的产物读成图节点（findings + gaps）。

ponytail: 不给 Sister 加交卷字段（改合同代价大且会被忘），收割是调度侧一次 LLM 调用。
"""
import json
import os

PROMPT = """你在给研究图收割节点。读下面这张已验收卡片的产物（用 read 工具读工作目录里的文件），
抽出两类东西，只输出一个 JSON 对象：

{"findings": [{"text": "一句话陈述一个具体发现（自足可读，不依赖上下文）",
               "weight": 0.1-1.0,
               "source_file": "该发现出自哪个产物文件（相对路径）",
               "quote": "支撑它的**逐字原文片段**（10-80 字，必须与文件里一字不差）",
               "provenance": "verified|analogy|invented"}],
 "gaps": [{"text": "一句话陈述这份产物**没能回答**的具体问题", "weight": 0.1-1.0}]}

规矩：
- findings 只写产物里**真有依据**的内容，不许推测、不许总结成空话。每条 3-6 条为宜。
- **quote 必须逐字复制文件里的原文**（可跨行，但字要对得上）——系统会去文件里核，对不上就丢弃该条。
- provenance 三档：verified＝产物里有出处支撑；analogy＝跨域类比未在此域验证；invented＝现场推断。
- gaps 是真正的缺口（材料没覆盖到、出处存疑、下一步该查什么），不是客套话。1-4 条。
- weight 是"值不值得后续深挖"的估计：核心 0.8-1.0，边角 0.1-0.4。
- 除 JSON 外不要输出任何字。
"""


def harvest_task(con, store, task, cfg, worker, evidence=None, *,
                 usage_db=None, usage_generation=None, usage_token_cap=None,
                 on_event=None):
    """返回 (finding_ids, gap_ids, err)。evidence 给了就落 claims 台账（宪法⑧）。
    usage_* 直通预算记账（深研驱动器的收割开销不许漏记，R11）。"""
    ws = task["workspace"] or ""
    if not os.path.isdir(ws):
        return [], [], "no workspace"
    try:
        with open(os.path.join(ws, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except OSError:
        return [], [], "no report.json"
    files = "\n".join(f"- {a}" for a in report.get("artifacts", []))
    shas = evidence.store_artifacts(ws, report.get("artifacts", [])) if evidence else {}
    prompt = (f"{PROMPT}\n# 卡片\n标题：{task['title']}\n\n{task['body'][:1500]}\n\n"
              f"# 交卷摘要\n{report.get('summary','')}\n\n# 产物文件（相对当前目录）\n{files}\n")
    obj, _raw, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "harvester"), prompt,
        cfg["provider"], cfg["default_model"],
        cwd=ws, tools=["read"], timeout=cfg.get("judge_timeout", 600),
        usage_db=usage_db, usage_task_id=task["id"],
        usage_generation=usage_generation, usage_token_cap=usage_token_cap,
        on_event=on_event)
    if err:
        return [], [], err
    if not isinstance(obj, dict):
        return [], [], "harvest 输出不是对象"

    fids, gids, unbacked = [], [], 0
    for kind, key, bucket in (("finding", "findings", fids), ("gap", "gaps", gids)):
        for item in (obj.get(key) or [])[:8]:
            text = (item or {}).get("text") if isinstance(item, dict) else None
            if not isinstance(text, str) or len(text.strip()) < 8:
                continue
            try:
                w = min(max(float(item.get("weight", 0.5)), 0.05), 1.0)
            except (TypeError, ValueError):
                w = 0.5
            prov = item.get("provenance") if item.get("provenance") in ("verified", "analogy", "invented") else None
            proj = task["project"] if "project" in task.keys() else None   # 发现继承卡的课题
            nid = store.add_node(con, kind, text.strip(), weight=w, task_id=task["id"],
                                 provenance=prov, project=proj)
            store.add_edge(con, task["id"], nid, "from_task")
            bucket.append(nid)
            if evidence and kind == "finding":
                sha = shas.get(item.get("source_file"))
                ok = sha and evidence.add_claim(con, nid, task["id"], sha,
                                                item["source_file"], item.get("quote") or "")
                if not ok:
                    unbacked += 1  # 引文核不上＝这条没有证据键，记账不静默
    return fids, gids, (f"{unbacked} 条发现的引文核不上（已收节点但无证据键）" if unbacked else None)
