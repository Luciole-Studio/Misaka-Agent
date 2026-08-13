"""端到端干跑：不调 LLM，用假 worker 走完 建卡→认领→交卷→验收→打回→通过→收割→前沿 全链，
外加深研驱动器两整轮（立论→找刺→展开→再找刺→再展开→前沿空→一枪成稿）。

真实链路的每个接缝都在这里被走一遍；LLM 那一格换成可控的桩。
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from misaka.extensions.board import db, dispatch, validate  # noqa: E402
from misaka.research.kernel import canon, evidence, frontier, store  # noqa: E402


class FakeWorker:
    """假 worker：按剧本写产物、按剧本给判词。替掉全部 LLM 调用。"""

    def __init__(self, tmp):
        self.tmp = tmp
        self.judge_calls = 0
        self.card_runs = 0

    def run_card(self, task, workspace, profile_dir, provider, default_model, on_event,
                 **_kwargs):
        self.card_runs += 1
        os.makedirs(workspace, exist_ok=True)
        on_event(json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "usage": {"totalTokens": 1000}}]}))
        # 第一轮故意漏一行（触发打回），第二轮补齐
        body = "结论一\n结论二\n" + ("## 校验通过\n" if self.card_runs > 1 else "")
        with open(os.path.join(workspace, "out.md"), "w", encoding="utf-8") as f:
            f.write(body)
        report = {"schema_version": 1, "status": "done", "summary": "干跑产物",
                  "artifacts": ["out.md"], "uncertain": ["这是干跑，无真实证据"], "notes": ""}
        with open(os.path.join(workspace, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
        from misaka.extensions.board import worker as real
        ok, result = real.check_report(workspace)
        return ({"ok": True, "report": result, "exit_code": 0, "timed_out": False} if ok
                else {"ok": False, "reason": result, "exit_code": 1, "timed_out": False})

    def run_llm_json(self, profile_dir, prompt, provider, default_model,
                     cwd=None, tools=None, timeout=600, model=None, **_kwargs):
        role = os.path.basename(profile_dir)
        if role == "redteam":
            self.judge_calls += 1
            has_footer = "校验通过" in open(os.path.join(cwd, "out.md"), encoding="utf-8").read()
            if has_footer:
                return {"pass": True, "reasons": ["判据齐"], "must_fix": []}, "", None
            return {"pass": False, "reasons": ["缺末行"], "must_fix": ["末尾补一行 '## 校验通过'"]}, "", None
        return None, "", "unexpected role " + role


def main():
    tmp = tempfile.mkdtemp(prefix="misaka-e2e-")
    os.environ["MISAKA_EVIDENCE"] = os.path.join(tmp, "evidence")
    db_path = os.path.join(tmp, "board.db")
    con = db.connect(db_path)
    store.init_all(con)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profiles = os.path.join(tmp, "profiles")   # 干跑自带角色目录，不碰 ~/.misaka
    for d in ("sisters/10032", "redteam", "last_order"):
        os.makedirs(os.path.join(profiles, d))
    cfg = {"db": db_path,
           "profiles_root": os.path.join(profiles, "sisters"),
           "roles_root": profiles,
           "hooks_dir": os.path.join(repo, "hooks"),
           "workspaces_root": os.path.join(tmp, "ws"),
           "provider": "x", "default_model": "y",
           "judge_timeout": 5, "token_cap": 0}

    fake = FakeWorker(tmp)
    real_worker = dispatch.worker
    dispatch.worker = fake
    try:
        body = "## 目标\n干跑\n## 边界\n无\n## 验收\n- out.md 存在\n- 末尾有 '## 校验通过'"
        tid = db.create_task(con, "干跑卡", body=body, assignee="10032")

        n1 = dispatch.dispatch_once(con, cfg)          # 跑 + 判 → 应被打回
        assert n1 >= 1, n1
        t = db.get(con, tid)
        assert t["status"] == "ready" and t["verify_rounds"] == 1, dict(t)
        assert db.latest_payload(con, tid, "verify_fail"), "打回该留 must_fix"

        dispatch.dispatch_once(con, cfg)                # 二轮修复 → 应通过
        t = db.get(con, tid)
        assert t["status"] == "done", dict(t)
        assert fake.card_runs == 2 and fake.judge_calls == 2, (fake.card_runs, fake.judge_calls)

        # 钩子闸真在链路里（空产物必被否决）
        open(os.path.join(t["workspace"], "out.md"), "w").close()
        assert dispatch.run_hooks(cfg, t, t["workspace"]), "空产物该被钩子否决"

        # 证据台账：产物入库 + 引文核验
        sha = evidence.store_artifacts(t["workspace"], ["out.md"])
        assert not sha or len(list(sha.values())[0]) == 64

        # 图层：节点→判重→前沿生卡
        n_gap = store.add_node(con, "gap", "干跑留下的缺口：某某未覆盖", weight=0.9)
        picks = frontier.pick(con, store, k=1)
        assert picks and picks[0][0]["id"] == n_gap, picks
        card = frontier.card_for(picks[0][0], "10032")
        assert "## 验收" in card["body"]
        cards, errs = validate.validate_cards([card], {"10032"})
        assert not errs, errs

        # 预算记账：从事件流读到假 worker 报的用量
        from misaka.orchestration import budget
        assert budget.spent(con) == 2000, budget.spent(con)
        assert budget.status(con, 1000)["mode"] == "stop"

        # blocked 支线：诚实卡住 ≠ 失败——事件记 blocked、不立禁令（状态列仍 failed）
        tid2 = db.create_task(con, "blocked 支线", body="## 目标\nx\n## 边界\ny\n## 验收\n- out.md",
                              assignee="10032")
        real_run_card = fake.run_card

        def blocked_run_card(task, workspace, *a, **k):
            os.makedirs(workspace, exist_ok=True)
            with open(os.path.join(workspace, "report.json"), "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "status": "blocked", "summary": "等输入",
                           "artifacts": [], "uncertain": ["-"], "notes": "缺原始档案，拿不到"}, f)
            from misaka.extensions.board import worker as real
            ok, why = real.check_report(workspace)
            return {"ok": ok, "reason": why, "exit_code": 0, "timed_out": False}

        fake.run_card = blocked_run_card
        dispatch.dispatch_once(con, cfg)
        fake.run_card = real_run_card
        t2 = db.get(con, tid2)
        assert t2["status"] == "failed", dict(t2)
        assert db.latest_payload(con, tid2, "blocked"), "该有 blocked 事件"
        assert not db.latest_payload(con, tid2, "failed"), "不该有 failed 事件"
        assert con.execute("SELECT COUNT(*) FROM clauses").fetchone()[0] == 0, "诚实卡住不许立禁令"
    finally:
        dispatch.worker = real_worker
    print(f"e2e ok — 建卡→执行→打回→修复→通过→钩子→证据→前沿→预算 全链走通"
          f"（{fake.card_runs} 次执行 / {fake.judge_calls} 次判决）")


class ResearchFake:
    """深研剧本假件：立论→找刺(1真1伪)→展开→再找刺(1真)→展开→无刺→综合。"""

    def __init__(self):
        self.critic_calls = 0

    def run_card(self, task, workspace, profile_dir, provider, default_model, on_event,
                 **_kwargs):
        os.makedirs(workspace, exist_ok=True)
        title = task["title"]
        if title.startswith("立论"):
            name, body = "argument.md", ("# 立论\n本稿下注：搬迁实为清洗环节。\n"
                                         "全部依据来自单一县志转引。\n")
        elif title.startswith("综合："):
            name, body = "REPORT.md", "# 报告\n## 材料清单\n- 各卡产物\n"
        elif "入藏簿" in title:
            name, body = "product.md", "入藏簿原件显示 1954 年整体入库。\n"
        else:
            name, body = "product.md", "已核，无新发现。\n"
        with open(os.path.join(workspace, name), "w", encoding="utf-8") as f:
            f.write(body)
        with open(os.path.join(workspace, "report.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "status": "done", "summary": title,
                       "artifacts": [name], "uncertain": ["干跑"], "notes": ""}, f,
                      ensure_ascii=False)
        from misaka.extensions.board import worker as real
        ok, result = real.check_report(workspace)
        return {"ok": ok, "report": result, "exit_code": 0, "timed_out": False}

    def run_llm_json(self, profile_dir, prompt, provider, default_model, **_kwargs):
        role = os.path.basename(profile_dir)
        if role == "redteam":
            return {"pass": True, "reasons": ["判据齐"], "must_fix": []}, "", None
        if role == "harvester":
            if "argument.md" in prompt:
                return {"findings": [{"text": "立论稿下注搬迁实为清洗环节",
                                      "weight": 0.8, "source_file": "argument.md",
                                      "quote": "搬迁实为清洗环节",
                                      "provenance": "verified"}], "gaps": []}, "", None
            return {"findings": [], "gaps": []}, "", None
        if role == "critic":
            self.critic_calls += 1
            if self.critic_calls == 1:      # 审立论：一真刺一伪刺
                return {"spikes": [
                    {"target": "argument.md", "quote": "单一县志转引", "kind": "出处弱",
                     "why": "孤证", "suggest": "核对入藏簿原件确认搬迁年份", "weight": 0.9},
                    {"target": "argument.md", "quote": "查无此句", "kind": "臆想",
                     "why": "x", "suggest": "这条伪刺不该入图不该派卡"},
                ]}, "", None
            if self.critic_calls == 2:      # 审第一张展开卡：再一真刺
                return {"spikes": [
                    {"target": "product.md", "quote": "1954 年整体入库", "kind": "矛盾",
                     "why": "与县志年份冲突", "suggest": "解释县志与入藏簿年份矛盾的成因",
                     "weight": 0.8},
                ]}, "", None
            return {"spikes": []}, "", None   # 宁缺毋滥：无刺可下
        return None, "", f"unexpected role {role}"


class FakeRunner:
    """干跑执行体：launch_ready ＝ 同步跑完 ready＋判完 verifying（dispatch 复用）。"""

    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg

    async def launch_ready(self, *, context=None, tool_call_id=None, on_update=None,
                           task_ids=None):
        dispatch.dispatch_once(self.con, self.cfg)
        return []


def research_dry():
    from misaka.config import CFG
    from misaka.extensions.board import project as project_mod
    from misaka.extensions.board import research_loop
    from misaka.research.kernel import rounds

    tmp = tempfile.mkdtemp(prefix="misaka-research-")
    os.environ["MISAKA_EVIDENCE"] = os.path.join(tmp, "evidence")
    old_projects_root = CFG["projects_root"]
    CFG["projects_root"] = os.path.join(tmp, "projects")
    con = db.connect(os.path.join(tmp, "board.db"))
    store.init_all(con)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profiles = os.path.join(tmp, "profiles")
    for d in ("sisters/10032", "redteam", "harvester", "last_order"):
        os.makedirs(os.path.join(profiles, d))
    cfg = {"db": os.path.join(tmp, "board.db"),
           "profiles_root": os.path.join(profiles, "sisters"),
           "roles_root": profiles,
           "hooks_dir": os.path.join(repo, "hooks"),
           "workspaces_root": os.path.join(tmp, "ws"),
           "provider": "x", "default_model": "y",
           "judge_timeout": 5, "token_cap": 0}
    assert project_mod.create("深研演示")[0]
    md = os.path.join(project_mod.path("深研演示"), "PROJECT.md")

    fake = ResearchFake()
    real_worker = dispatch.worker
    dispatch.worker = fake
    try:
        db.create_task(con, "立论：搬迁性质", assignee="10032", project="深研演示",
                       body="## 目标\n写初稿论证，含可证伪下注\n## 边界\n-\n## 验收\n- argument.md")
        out = asyncio.run(research_loop.run_loop(
            con, cfg, FakeRunner(con, cfg), fake,
            project="深研演示", assignee="10032", beam=4, max_rounds=None,
            poll_seconds=0))

        assert out["reason"].startswith("前沿空"), out
        assert out["rounds"] == 3 == rounds.rounds_done(md), (out, open(md).read())
        assert not out["errors"], out["errors"]
        assert fake.critic_calls == 3, "三张卡各找刺一次（事件幂等）"
        gaps = store.nodes(con, kind="gap")
        assert len(gaps) == 2 and all(g["status"] == "expanded" for g in gaps), \
            [dict(g) for g in gaps]   # 两根真刺都被展开；伪刺没入图
        n_spike_edges = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='spike_of'").fetchone()[0]
        assert n_spike_edges == 2, n_spike_edges
        cov = evidence.coverage(con, store)
        assert cov["backed"] >= 1, "收割的发现要有证据键（宪法⑧）"
        synth = out["synthesis"]
        assert synth and synth["status"] == "done", synth
        srow = db.get(con, synth["task_id"])
        assert os.path.isfile(os.path.join(srow["workspace"], "REPORT.md")), "一枪成稿"
        assert not db.latest_payload(con, synth["task_id"], "research_settled"), \
            "综合卡不回吞进图（防自我消化）"
        # 无状态续跑：再跑一遍＝什么都不用做，立即前沿空收场且不重复消化
        out2 = asyncio.run(research_loop.run_loop(
            con, cfg, FakeRunner(con, cfg), fake,
            project="深研演示", assignee="10032", beam=4, max_rounds=None,
            poll_seconds=0, synthesize=False))
        assert out2["reason"].startswith("前沿空") and fake.critic_calls == 3, \
            "重启续跑不重复收割/找刺（research_settled 幂等）"
    finally:
        dispatch.worker = real_worker
        CFG["projects_root"] = old_projects_root
    print(f"research dry ok — 立论→找刺→展开×2→前沿空→综合 全链走通"
          f"（{out['rounds']} 轮 / critic {fake.critic_calls} 次 / 伪刺零入图 / 重启幂等）")


if __name__ == "__main__":
    main()
    research_dry()
