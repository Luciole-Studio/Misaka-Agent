"""深研驱动器：/research 模式的多轮循环（设计 docs/design/research-mode.md §六，R9-R11）。

一轮＝消化新完成卡（收割＋找刺）→ 补派 ready → 束宽挑选前沿 → 派卡 → 记日志。
「轮」只是派卡批次与日志记账单位，**不是执行屏障**——快卡的成果随时入图、随时可被
展开，慢卡不拖住别人（流水线）。判停＝三闸＋手动；触闸先排空在跑的再收场。

无状态：进度全在 板（卡）＋图（节点边）＋PROJECT.md 深研日志（行数即轮数）。
驱动器崩了/会话重开＝重新调用即续跑，不持有任何独占状态。

执行体注入：runner 只需 `launch_ready(context=, tool_call_id=, task_ids=)` 一个口——
生产＝SisterRuntime（LO 会话内，4 期接线），干跑＝dispatch 同步桩（e2e）。
模型调用全走 worker 注入，本文件零 LLM 知识。
"""
import asyncio
import os

from misaka.extensions.board import critic, db, report
from misaka.extensions.board import project as project_mod
from misaka.orchestration import budget
from misaka.research.kernel import canon, evidence, frontier, harvest, rounds, store

POLL_SECONDS = 2.0
ACTIVE_STATUSES = ("running", "verifying", "finalizing")
SYNTH_TITLE_PREFIX = "综合："   # 与 report.create 同一约定；综合卡不回吞进图（防自我消化）
ARGUMENT_TITLE_PREFIX = "立论："

# 模式内纪律（R2 零结论原则）：启动时注入 LO 会话，模式外不生效（R1 模式化）
RESEARCH_DISCIPLINE = """〔研究模式已启动——本模式内纪律〕
1. **零结论**：你（Last Order）在本模式内不做任何实质断言（「X 大概是/应该是…」一律禁止）。
   你的全部输出限于：研究设计（要素/方向/方法/人选/耦合）、派卡决策、进度与仪表读数的转述。
2. 立场与结论只出自 Sisters 的卡产物，并受验收红队＋思辨红队双重审计；
   立论卡里的「下注」也由 Sister 写，不由你替她定。
3. 修订研究设计走 misaka_research_* 工具与建卡；不要在对话里替妹妹们下结论。
（驱动器停止后此纪律自动失效。）"""

ARGUMENT_BODY = """## 目标
就下述研究目标写初稿论证，产出 argument.md：

> {goal}

要求：论点明确；论据链逐条带出处；自标心虚点；**必须含一个「下注」段**——
赌哪个主流说法是错的，并写明何种证据出现即可推翻它（可证伪条件）。

## 边界
只立论，不定案；证实/证伪交给后续卡。不许编造出处。

## 验收
- argument.md 存在
- 含「下注」段，且写明可证伪条件
- 每条论据带出处（查不到的明写「未能确证」）"""


def bootstrap(con, *, goal, project, assignee, elements=(), directions=(), couplings=()):
    """启动装配（幂等）：设计入图（R3：LO 以参数结构化提交，不写文稿）＋立论卡（R4）。
    返回 {"design_added": [...], "argument_task": tid|None(已有)}。"""
    proj = project_mod.require(project)
    # 首启老课题不吃存量：启动前已完成的卡预记 settled，循环只消化此后完成的
    # （否则第一拍就对全部历史卡逐张收割＋找刺，预算惊喜）。日志已有轮次＝续跑，
    # 不预记——上一程崩溃时刚完成、还没消化的卡得留给循环。
    if rounds.rounds_done(os.path.join(project_mod.path(proj), "PROJECT.md")) == 0:
        for t in db.by_status(con, "done"):
            if t["project"] != proj or t["title"].startswith(SYNTH_TITLE_PREFIX):
                continue
            gen = int(t["generation"])
            if not db.latest_payload(con, t["id"], "research_settled", generation=gen):
                db.add_event(con, t["id"], "research_settled", {"seeded": "存量卡"},
                             generation=gen)
    existing = {n["text"] for n in design_nodes(con, proj)}
    added = []
    for label, items in (("要素", elements), ("方向", directions), ("耦合", couplings)):
        for item in items or ():
            text = f"{label}：{str(item).strip()}"
            if str(item).strip() and text not in existing:
                added.append(store.add_node(con, "question", text, weight=0.5,
                                            provenance="design", project=proj))
                existing.add(text)
    argument_task = None
    if con.execute("SELECT 1 FROM tasks WHERE project=? AND title LIKE ? LIMIT 1",
                   (proj, ARGUMENT_TITLE_PREFIX + "%")).fetchone() is None:
        argument_task = db.create_task(
            con, f"{ARGUMENT_TITLE_PREFIX}{goal}"[:60],
            body=ARGUMENT_BODY.format(goal=goal), assignee=assignee, project=proj)
    return {"design_added": added, "argument_task": argument_task}


class DispatchRunner:
    """无头执行体（CLI 用）：launch_ready＝同步跑完 ready＋判完 verifying。
    真烧模型额度；LO 会话内的生产执行体是 SisterRuntime，不用这个。"""

    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg

    async def launch_ready(self, *, context=None, tool_call_id=None, on_update=None,
                           task_ids=None):
        from misaka.extensions.board import dispatch
        await asyncio.to_thread(dispatch.dispatch_once, self.con, self.cfg)
        return []


def design_nodes(con, project):
    """设计层节点（LO 以工具参数结构化提交，4 期启动工具写入）：
    kind='question' ＋ provenance='design'。critic 每轮拿它们当靶查覆盖缺口（R3）。"""
    return con.execute(
        "SELECT * FROM nodes WHERE kind='question' AND provenance='design'"
        " AND project=? AND status='open'", (project,)).fetchall()


def settle_done_cards(con, cfg, worker, *, project):
    """消化新完成的卡：收割一次＋找刺一次（research_settled 事件幂等，按代次隔离）。"""
    stats = {"cards": 0, "findings": 0, "gaps": 0, "spikes": 0, "dropped": 0, "errors": []}
    design = list(design_nodes(con, project))
    for t in db.by_status(con, "done"):
        if t["project"] != project or t["title"].startswith(SYNTH_TITLE_PREFIX):
            continue
        gen = int(t["generation"])
        if db.latest_payload(con, t["id"], "research_settled", generation=gen):
            continue
        fids, gids, herr = harvest.harvest_task(
            con, store, t, cfg, worker, evidence=evidence,
            usage_db=cfg.get("db"), usage_generation=gen,
            usage_token_cap=cfg.get("token_cap"))
        canon.dedup(con, store, fids + gids)
        spike_ids, dropped, cerr = critic.critique_task(
            con, t, cfg, worker, extra_nodes=design,
            usage_db=cfg.get("db"), usage_generation=gen,
            usage_token_cap=cfg.get("token_cap"))
        canon.dedup(con, store, spike_ids)
        db.add_event(con, t["id"], "research_settled",
                     {"findings": len(fids), "gaps": len(gids), "spikes": len(spike_ids),
                      "spike_dropped": len(dropped), "harvest_err": herr,
                      "critic_err": cerr}, generation=gen)
        stats["cards"] += 1
        stats["findings"] += len(fids)
        stats["gaps"] += len(gids)
        stats["spikes"] += len(spike_ids)
        stats["dropped"] += len(dropped)
        if herr and not fids and not gids:
            stats["errors"].append(f"{t['id']} 收割失败: {herr}")
        if cerr:
            stats["errors"].append(f"{t['id']} 找刺失败: {cerr}")
    return stats


def _project_cards(con, status, project):
    return [r for r in db.by_status(con, status) if r["project"] == project]


def _expand_picks(con, picks, assignee, project):
    """前沿挑中的缺口 → 补缺卡（照 misaka_expand 同一手势）。返回新卡 id。"""
    ids = []
    for node, _score in picks:
        card = frontier.card_for(node, assignee)
        tid = db.create_task(con, card["title"], body=card["body"],
                             assignee=card["assignee"], project=project)
        store.set_status(con, node["id"], "expanded")
        store.add_edge(con, node["id"], tid, "expanded_to")
        ids.append(tid)
    return ids


async def run_loop(con, cfg, runner, worker, *, project, assignee,
                   beam=4, max_rounds=None, stop_event=None, context=None,
                   tool_call_id="research-loop", poll_seconds=POLL_SECONDS,
                   synthesize=True):
    """跑到判停为止。返回 {reason, rounds, stats, synthesis?}。"""
    proj = project_mod.require(project)
    if not proj:
        raise ValueError("深研必须归属一个课题（饱和/前沿按课题隔离）")
    md = os.path.join(project_mod.path(proj), "PROJECT.md")
    pending = {"cards": 0, "findings": 0, "gaps": 0, "spikes": 0, "dropped": 0}
    errors = []
    reason = None
    while True:
        settled = await asyncio.to_thread(settle_done_cards, con, cfg, worker, project=proj)
        errors.extend(settled.pop("errors"))
        for key in pending:
            pending[key] += settled[key]

        halt, why = rounds.should_stop(
            rounds_done=rounds.rounds_done(md), max_rounds=max_rounds,
            budget_mode=budget.status(con, cfg.get("token_cap"))["mode"],
            frontier_empty=False,
            stop_requested=bool(stop_event and stop_event.is_set()))
        active = [r for status in ACTIVE_STATUSES for r in _project_cards(con, status, proj)]
        # 在跑的每拍全量回喂执行体：验收悬卡重试、死主 running 回收、健康卡无操作。
        # 只喂 verifying 会饿死死主 running 卡——排空期或前沿干涸期没人救场，循环被钉死。
        stalled = [r["id"] for r in active]
        if halt:
            if active:                      # 触闸先排空在跑的（成果照常入图），再收场
                await runner.launch_ready(context=context, tool_call_id=tool_call_id,
                                          task_ids=stalled)
                await asyncio.sleep(poll_seconds)
                continue
            reason = why
            break

        new_ids = [r["id"] for r in _project_cards(con, "ready", proj)]
        free = max(0, beam - len(active)) - len(new_ids)
        if free > 0:
            new_ids += _expand_picks(
                con, frontier.pick(con, store, k=free, project=proj), assignee, proj)
        if new_ids or stalled:
            await runner.launch_ready(context=context, tool_call_id=tool_call_id,
                                      task_ids=[*new_ids, *stalled])
        if new_ids:
            reading = budget.status(con, cfg.get("token_cap"))
            rounds.append_round(
                md, f"派{len(new_ids)}卡({','.join(new_ids)})"
                    f"｜消化{pending['cards']}卡 +{pending['findings']}发现"
                    f" +{pending['gaps']}缺口 +{pending['spikes']}刺"
                    f"（丢弃伪刺{pending['dropped']}）｜预算{reading['used']}({reading['mode']})")
            pending = dict.fromkeys(pending, 0)
            continue                        # 派了活立刻下一拍
        if not active:
            reason = "前沿空（无刺/缺口可展开）"
            break
        await asyncio.sleep(poll_seconds)

    # 终止不写日志——日志行数＝轮数的契约不许被终止行污染；终止原因在返回值里
    # （4 期由 LO 工具通知给人），板/图状态本身就能看出停在哪。
    result = {"reason": reason, "rounds": rounds.rounds_done(md), "errors": errors}
    if synthesize and reason != "预算触顶":   # 宪法⑦：预算停机不再开新卡
        result["synthesis"] = await _synthesize(
            con, cfg, runner, project=proj, assignee=assignee, context=context,
            tool_call_id=tool_call_id, stop_event=stop_event, poll_seconds=poll_seconds)
    return result


async def _synthesize(con, cfg, runner, *, project, assignee, context,
                      tool_call_id, stop_event, poll_seconds):
    """终止后一枪成稿（宪法③）：本课题全部 done 卡 → 一张综合卡，等它走完验收。"""
    rows = [r for r in _project_cards(con, "done", project)
            if not r["title"].startswith(SYNTH_TITLE_PREFIX)]
    if not rows:
        return None
    tid = db.create_task(con, f"{SYNTH_TITLE_PREFIX}{project}",
                         body=report.card_body(rows, project),
                         assignee=assignee, project=project, timeout_seconds=1200)
    while True:
        row = db.get(con, tid)
        if row["status"] in ("done", "failed", "stopped"):
            return {"task_id": tid, "status": row["status"]}
        if stop_event and stop_event.is_set():
            return {"task_id": tid, "status": row["status"], "note": "手动停止，未等综合完成"}
        await runner.launch_ready(context=context, tool_call_id=tool_call_id,
                                  task_ids=[tid])
        await asyncio.sleep(poll_seconds)
