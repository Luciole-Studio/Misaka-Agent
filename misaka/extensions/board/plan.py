"""Last Order 批处理规划：一句话目标 → 赌注 + 合规卡片。

CLI 与 chat 工具共用这一份——业务逻辑不长在入口里（简洁宗旨）。
"""
from misaka.extensions.board import db, validate
from misaka.research.kernel import store


def make(cfg, goal, sisters):
    """调 Last Order 出计划书并过 schema。返回 (bet, cards, errors, raw)。"""
    from misaka.extensions.board import worker
    roster = "\n".join(f"- {s}" for s in sorted(sisters))
    prompt = (f"# 研究目标\n{goal}\n\n# Sister 名册（assignee 只能从这里选）\n{roster}\n\n"
              "只输出卡片 JSON 数组。")
    obj, raw, err = worker.run_llm_json(
        f"{cfg['roles_root']}/last_order", prompt,
        cfg["provider"], cfg["default_model"], timeout=300)
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
