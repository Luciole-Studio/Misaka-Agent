"""思辨红队（critic）：认知偏差猎手——审产物与图节点，产出刺清单（R8）。

与验收红队分开：验收＝照合同收敛核对；找刺＝主动发散怀疑。两种相反姿态
不共用人格（设计 docs/design/research-mode.md §七）。不常驻：一次性进程内调用，
与 harvester 同款生命周期。核验与入图在内核（kernel/spike.py，R14 硬闸）。
"""
import json
import os

from misaka.research.kernel import guard, spike

SOUL_TEMPLATE = """# 思辨红队（critic）

你是御坂网络的思辨红队：认知偏差猎手。不评文笔、不管格式，只干一件事：
找出研究材料里**站不稳的推理**——臆想、偏见、孤证、过度概括、内部矛盾、覆盖缺口。

## 姿态
- 主动怀疑，但宁缺毋滥：制造伪刺与放过真刺同罪。
- 每根刺都要能落成下一步研究行动，不是文学批评。
- 你只提出疑点，不下结论——证实或证伪是后续卡片的事。
"""

PROMPT = """审读靶清单里的研究材料，找出不稳点（刺）。只输出一个 JSON 对象：

{"spikes": [{"target": "节点id 或 产物相对路径",
  "quote": "被刺原文的逐字引用（10-80 字）",
  "kind": "臆想|偏见|出处弱|过度概括|矛盾|覆盖缺口",
  "why": "一句话说明为什么不稳",
  "suggest": "值得展开的研究问题（自足可读，能直接派卡）",
  "weight": 0.1-1.0}]}

规矩：
- **quote 必须逐字复制**靶文原文（可跨行，字要对得上）——系统会核验，核不上整条丢弃。
- target 只准从靶清单里选（节点写它的 id，产物写相对路径）。
- kind 六档：臆想＝无据推断被当作事实｜偏见＝视角或来源单一导致的倾向｜
  出处弱＝孤证或二手转引｜过度概括＝个案外推｜矛盾＝与其他材料冲突｜
  覆盖缺口＝该考虑而未考虑的要素、领域或耦合（可以刺研究设计本身）。
- suggest 写成能直接开工的研究问题（「核对 X 的 Y」），不是评论。
- weight 是"值不值得优先展开"的估计：要害 0.7-1.0，边角 0.1-0.4。
- 找不到真刺就输出 {"spikes": []}——**宁缺毋滥，不许为了显得尽职而制造伪刺**。
- 除 JSON 外不要输出任何字。
"""


def ensure_profile(roles_root):
    """首跑落骨架人格（可手改，绝不覆盖已有）。返回 profile 目录。"""
    profile = os.path.join(os.path.expanduser(roles_root), "critic")
    soul = os.path.join(profile, "SOUL.md")
    if not os.path.exists(soul):
        os.makedirs(profile, exist_ok=True)
        with open(soul, "w", encoding="utf-8") as f:
            f.write(SOUL_TEMPLATE)
    return profile


def _task_nodes(con, task_id):
    """该卡入图且仍在场的节点（发现/假说）——图侧的可刺靶。"""
    return con.execute(
        "SELECT id, text FROM nodes WHERE task_id=? AND kind IN ('finding','hypothesis')"
        " AND status IN ('open','expanded')", (task_id,)).fetchall()


def _artifacts(workspace):
    if not workspace:
        return []   # 没工作区就没产物——别让相对路径逃逸到进程 CWD
    try:
        with open(os.path.join(workspace or "", "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        return []
    arts = report.get("artifacts")
    return [a for a in arts if isinstance(a, str)] if isinstance(arts, list) else []


def critique_task(con, task, cfg, worker, extra_nodes=None, *,
                  usage_db=None, usage_generation=None, usage_token_cap=None,
                  on_event=None):
    """审一张已收割卡：产物＋它入图的节点（extra_nodes 可加设计层等图靶）。
    返回 (gap_ids, dropped, err)——err 是设施故障；伪刺丢弃记在 dropped，不算故障。"""
    from misaka.research.kernel import store  # 延迟导入避免包初始化环（照 worker 先例）

    profile = ensure_profile(cfg["roles_root"])
    ws = task["workspace"] or ""
    nodes = list(_task_nodes(con, task["id"])) + list(extra_nodes or [])
    artifacts = _artifacts(ws)
    if not nodes and not artifacts:
        return [], [], "无靶可审（卡无产物且未入图）"

    lines = [f"[{n['id']}] {n['text']}" for n in nodes]
    lines += [f"[{a}]（产物文件，先用 read 读原文再下刺）" for a in artifacts]
    prompt = (
        f"{PROMPT}\n# 卡片合同（背景）\n标题：{task['title']}\n\n{(task['body'] or '')[:800]}\n\n"
        f"# 靶清单（target 只准从这里选）\n{guard.untrusted('targets', chr(10).join(lines))}\n"
        f"工作目录就是该卡工作区。最多 {spike.MAX_SPIKES} 条，挑最要害的。"
    )
    obj, _raw, err = worker.run_llm_json(
        profile, prompt, cfg["provider"], cfg["default_model"],
        cwd=ws or None, tools=["read"], timeout=cfg.get("judge_timeout", 600),
        usage_db=usage_db, usage_task_id=task["id"],
        usage_generation=usage_generation, usage_token_cap=usage_token_cap,
        on_event=on_event)
    if err:
        return [], [], err
    proj = task["project"] if "project" in task.keys() else None
    gap_ids, dropped = spike.ingest(con, store, obj, workspace=ws or None,
                                    project=proj, task_id=task["id"])
    return gap_ids, dropped, None
