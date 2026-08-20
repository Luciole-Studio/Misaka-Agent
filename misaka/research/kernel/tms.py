"""真值维护：证据塌了，连坐的结论自动找出来重查——只重查最小脏集，不全局重跑。

节点靠 from_task 边挂在卡上（正当化），靠 supports 边相互依赖
（contradicts 不传播——反驳者塌了不该连坐被反驳者）。
撤回一张卡 → 它的节点 stale → 沿 supports 边传播 → 生成复核卡。
"""


def dependents(con, node_ids):
    """沿 supports 边找下游（谁靠这些节点撑着）。广度优先，防环。"""
    seen, frontier = set(node_ids), list(node_ids)
    while frontier:
        qs = ",".join("?" * len(frontier))
        rows = con.execute(
            f"SELECT dst FROM edges WHERE kind='supports' AND src IN ({qs})", frontier).fetchall()
        frontier = [r[0] for r in rows if r[0] not in seen]
        seen.update(frontier)
    return seen - set(node_ids)


def retract(con, store, task_id, reason=""):
    """撤回一张卡的证据基础。返回 (直接节点, 连坐节点)。"""
    direct = [r[0] for r in con.execute(
        "SELECT id FROM nodes WHERE task_id=? AND status IN ('open','expanded')", (task_id,))]
    if not direct:
        return [], []
    downstream = dependents(con, direct)
    qs = ",".join("?" * (len(direct) + len(downstream)))
    con.execute(f"UPDATE nodes SET status='stale' WHERE id IN ({qs})", direct + list(downstream))
    return direct, sorted(downstream)


def recheck_card(stale_nodes, task_title, reason, assignee):
    """脏集 → 一张复核卡（模板，零 LLM）。"""
    items = "\n".join(f"- [{n['id']}] {n['text']}" for n in stale_nodes[:12])
    return {
        "title": f"复核：{task_title}"[:60],
        "assignee": assignee,
        "body": (f"## 目标\n以下结论的证据基础已被撤回（原因：{reason or '未注明'}），"
                 f"逐条重新核实并写 recheck.md：\n\n{items}\n\n"
                 "每条给出三态之一：**仍成立**（附新证据出处）／**须修正**（写出正确表述）／**撤销**（说明为何不成立）。\n\n"
                 "## 边界\n只核这些条目，不扩展新问题。查不到就写「无法核实」，不许拿旧结论顶数。\n\n"
                 "## 验收\n- recheck.md 存在\n- 上述每个节点 id 都在文中出现且有三态裁定之一\n- 仍成立的条目附新出处"),
    }


if __name__ == "__main__":
    import sqlite3
    import sys
    sys.path.insert(0, ".")
    from misaka.research.kernel import store
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    store.init(con)
    a = store.add_node(con, "finding", "根基结论", task_id="t_base")
    b = store.add_node(con, "finding", "靠 a 撑着", task_id="t_other")
    c = store.add_node(con, "finding", "靠 b 撑着", task_id="t_other")
    d = store.add_node(con, "finding", "无关结论", task_id="t_other")
    store.add_edge(con, a, b, "supports")
    store.add_edge(con, b, c, "supports")
    store.add_edge(con, c, a, "supports")  # 造个环，验证不死循环
    direct, down = retract(con, store, "t_base", reason="档案证伪")
    assert direct == [a] and set(down) == {b, c}, (direct, down)
    assert store.get(con, d)["status"] == "open", "无关结论不该被连坐"
    assert store.get(con, c)["status"] == "stale"
    card = recheck_card([store.get(con, x) for x in [a] + list(down)], "某卡", "档案证伪", "10032")
    assert a in card["body"] and "## 验收" in card["body"]
    print(f"tms selfcheck ok — 撤回 {len(direct)} 直接 + {len(down)} 连坐，无关节点未受影响，环不死循环")
