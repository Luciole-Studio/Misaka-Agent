"""Last Order 批处理规划：一句话目标 → 赌注 + 合规卡片。

CLI 与 chat 工具共用这一份——业务逻辑不长在入口里（简洁宗旨）。
拆卡合同是**调用方的任务要求**，随 prompt 走（REPORT_INSTRUCTIONS 同形制）；
2026-08-20 从 last_order/SOUL.md 迁入——LO 的 profile 从此只有一份人格档，
批处理轮跳过人格加载（soul=False，对话档的工具清单进不来）。
"""
from misaka.extensions.board import db, validate
from misaka.research.kernel import store

# 原 last_order/SOUL.md 逐字（已退役为 SOUL-plan-retired.md 留档）
PLAN_CONTRACT = """\
# Last Order（最终指令）——御坂网络编排官

你只拆解，不执行。你没有任何工具，产出只有一样：**一个 JSON 对象**，除此之外一个字都不要输出：

```
{"bet": "本研究赌哪个反直觉判断成立（一句话）", "cards": [ ...卡片数组... ]}
```

**赌注协议（进攻翼，不可省）**：`bet` 是这份计划敢赌的那个反常识判断——
"赌主流归因搞反了因果""赌这个数字被系统性高估"。它决定研究有没有野心。
规矩：①下注可以豪赌，但兑现必须诚实——赌注只管命题的野心，证据等级由红队说了算，
红队可以降级证据，**不许削你的赌注**；②赌注必须可被证伪（说得出什么证据会推翻它）；
③想不出反直觉判断时，宁可老实写"赌某条被忽视的材料能改写既有叙述"，也不许写空话。

卡片规则：
1. 每卡 = {"title": "...", "body": "...", "assignee": "名册内的 Sister 名", "priority": 0}
2. body 必须用此模板（三节都必填）：
   ## 目标
   （这张卡到底交付什么文件、什么内容）
   ## 边界
   （什么**不归**这张卡做——防止越界与重复劳动）
   ## 验收
   （可机检的判据清单：哪些文件必须存在、必须包含什么。红队会拿这节逐条核。）
3. 卡与卡相互独立，不许有依赖（依赖图 M2 才有）。
4. 宁少勿滥：1-4 张。拆不动的目标就 1 张。
5. assignee 只能从给你的名册里选；不许发明名字。
6. 诚实拆解：目标做不到的部分，写进某张卡的「边界」明说不做，不许假装覆盖。
"""


def build_prompt(goal, sisters):
    """拆卡合同＋目标＋名册（纯函数可测）。"""
    roster = "\n".join(f"- {s}" for s in sorted(sisters))
    return (f"{PLAN_CONTRACT}\n# 研究目标\n{goal}\n\n"
            f"# Sister 名册（assignee 只能从这里选）\n{roster}\n\n"
            "只输出卡片 JSON 数组。")


def make(cfg, goal, sisters):
    """调 Last Order 出计划书并过 schema。返回 (bet, cards, errors, raw)。"""
    from misaka.extensions.board import worker
    obj, raw, err = worker.run_llm_json(
        f"{cfg['roles_root']}/last_order", build_prompt(goal, sisters),
        cfg["provider"], cfg["default_model"], timeout=300, soul=False)
    if err:
        return None, [], [f"Last Order 失败: {err}"], raw
    bet, cards, errors = validate.validate_plan(obj, sisters)
    return bet, cards, errors, raw


def submit(con, bet, cards):
    """赌注入图（红队不许削它，只能降证据等级）＋卡片上板。返回 [tid]。"""
    if bet:
        store.add_node(con, "bet", bet, weight=1.0)
    return [db.create_task(con, c["title"], body=c["body"], assignee=c["assignee"],
                           model=c["model"], priority=c["priority"], timeout_seconds=c["timeout"])
            for c in cards]


if __name__ == "__main__":
    p = build_prompt("考证御坂网络", {"10033", "10032"})
    assert p.startswith("# Last Order"), "合同在最前（原 SOUL.md 系统提示位的等价）"
    assert "你只拆解，不执行" in p and "赌注协议" in p and "## 验收" in p, "合同逐字在场"
    assert "# 研究目标\n考证御坂网络" in p
    assert p.index("- 10032") < p.index("- 10033"), "名册排序稳定"
    assert p.rstrip().endswith("只输出卡片 JSON 数组。")
    print("plan selfcheck ok — 合同前置/逐字/目标/名册/收尾")
