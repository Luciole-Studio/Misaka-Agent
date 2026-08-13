"""三 schema 强校验（手搓极小校验器——schema 简单到不值得引 jsonschema）。

① 计划书=Last Order 输出的卡片数组  ② 交接单=卡 body（须含「## 验收」节）
③ 验收判词=红队输出 verdict。report.json 的校验在 worker.check_report（keystone）。
"""
import json


def extract_json(text):
    """从模型输出里捞第一个可解析的 JSON 对象/数组。"""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch in "[{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    return None


def validate_plan(obj, sisters):
    """计划书 = {bet, cards}（赌注协议）。兼容裸数组（老格式，无赌注）。

    返回 (bet, cards, errors)。bet 为 None 表示这份计划没下注。
    """
    if isinstance(obj, dict):
        bet = obj.get("bet")
        cards, errors = validate_cards(obj.get("cards"), sisters)
        if not (isinstance(bet, str) and len(bet.strip()) >= 10):
            errors.append("bet 缺失：深研档必须下注一个反直觉判断（≥10 字）")
            bet = None
        return (bet.strip() if bet else None), cards, errors
    cards, errors = validate_cards(obj, sisters)
    return None, cards, errors + ["计划书未含 bet 字段（赌注协议要求下注）"]


def validate_cards(obj, sisters):
    """返回 (cards, errors)。cards 已补默认值。"""
    errors = []
    if not isinstance(obj, list) or not 1 <= len(obj) <= 6:
        return [], [f"须是 1-6 张卡的 JSON 数组，得到 {type(obj).__name__}"]
    cards = []
    for i, c in enumerate(obj):
        if not isinstance(c, dict):
            errors.append(f"卡{i}: 不是对象")
            continue
        title, body, assignee = c.get("title"), c.get("body"), c.get("assignee")
        if not (isinstance(title, str) and title.strip()):
            errors.append(f"卡{i}: title 缺失")
        if not (isinstance(body, str) and "## 验收" in body):
            errors.append(f"卡{i}: body 必须含「## 验收」节（可机检判据）")
        if assignee not in sisters:
            errors.append(f"卡{i}: assignee={assignee!r} 不在名册 {sorted(sisters)}")
        cards.append({
            "title": (title or "").strip(), "body": body or "", "assignee": assignee,
            "model": c.get("model"), "priority": int(c.get("priority") or 0),
            "timeout": min(max(int(c.get("timeout") or 900), 60), 3600),
        })
    return cards, errors


def validate_verdict(obj):
    """返回 errors 列表；空=合法。"""
    if not isinstance(obj, dict):
        return ["verdict 不是对象"]
    errors = []
    if not isinstance(obj.get("pass"), bool):
        errors.append("pass 须是 bool")
    for k in ("reasons", "must_fix"):
        if not (isinstance(obj.get(k), list) and all(isinstance(x, str) for x in obj[k])):
            errors.append(f"{k} 须是字符串数组")
    if obj.get("pass") is False and not obj.get("must_fix"):
        errors.append("不通过时 must_fix 不得为空")
    return errors


if __name__ == "__main__":
    bet, cards, errs = validate_plan(
        {"bet": "赌本题的主流归因方向是错的", "cards": [
            {"title": "x", "body": "## 验收\n- a.md", "assignee": "s1"}]}, {"s1"})
    assert bet and not errs, (bet, errs)
    _, _, e2 = validate_plan([{"title": "x", "body": "## 验收\n- a", "assignee": "s1"}], {"s1"})
    assert any("bet" in x for x in e2), e2
    assert extract_json('废话```json\n{"a":1}\n```尾巴') == {"a": 1}
    assert extract_json("{bad} [1,2]") == [1, 2]
    assert extract_json("没有") is None
    cards, errs = validate_cards(
        [{"title": "x", "body": "## 目标\n…\n## 验收\n- a.md 存在", "assignee": "s1"}], {"s1"})
    assert not errs and cards[0]["timeout"] == 900
    _, errs = validate_cards([{"title": "x", "body": "无验收节", "assignee": "nobody"}], {"s1"})
    assert len(errs) == 2, errs
    assert validate_verdict({"pass": True, "reasons": [], "must_fix": []}) == []
    assert validate_verdict({"pass": False, "reasons": ["r"], "must_fix": []}) != []
    print("validate selfcheck ok")
