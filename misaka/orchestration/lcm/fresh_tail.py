"""保护尾巴边界（hermes-lcm fresh_tail.py 忠实移植，MIT）。纯函数。

三层，优先级递增：条数界 → token 界（最新一条永远保留，哪怕单条超 cap）→
**组完整性压倒一切**：边界落在 tool 结果上时向前扩到发起它的 assistant，
宁可尾巴超配额也不劈开 assistant/tool-result 组（劈开＝严格供应商 400）。
"""
from dataclasses import dataclass

from misaka.orchestration.lcm.tokens import count_message_tokens


@dataclass
class FreshTailBoundary:
    start: int
    count: int
    tokens: int
    token_limited: bool = False
    tool_group_extended: bool = False


def _assistant_group_start(messages, start):
    """边界在 tool 结果上→向前找发起的 assistant；跨 user/system 立即放弃。"""
    if start <= 0 or start >= len(messages):
        return start
    boundary = messages[start]
    if boundary.get("role") != "tool":
        return start
    result_id = boundary.get("tool_call_id")
    if not result_id:
        return start
    index = start - 1
    while index >= 0:
        msg = messages[index]
        role = msg.get("role")
        if role in ("user", "system"):
            return start          # 绝不跨回合边界
        if role == "assistant":
            ids = {tc.get("id") for tc in msg.get("tool_calls") or []
                   if isinstance(tc, dict)}
            return index if result_id in ids else start
        index -= 1
    return start


def resolve_fresh_tail_boundary(messages, *, fresh_tail_count,
                                fresh_tail_max_tokens=0):
    n = len(messages)
    count_limit = max(1, int(fresh_tail_count)) if fresh_tail_max_tokens > 0 \
        else max(0, int(fresh_tail_count))
    start = max(0, n - count_limit) if count_limit else n
    token_limited = False
    if fresh_tail_max_tokens > 0:
        used = 0
        boundary = n
        for index in range(n - 1, start - 1, -1):
            tokens = count_message_tokens(messages[index])
            if index != n - 1 and used + tokens > fresh_tail_max_tokens:
                token_limited = True
                break
            used += tokens
            boundary = index
        start = boundary
    group_start = _assistant_group_start(messages, start)
    extended = group_start < start
    start = group_start
    tail = messages[start:]
    return FreshTailBoundary(
        start=start, count=len(tail),
        tokens=sum(count_message_tokens(m) for m in tail),
        token_limited=token_limited, tool_group_extended=extended)


if __name__ == "__main__":
    msgs = [{"role": "user", "content": "问题" * 50}]
    for i in range(10):
        msgs.append({"role": "assistant", "content": f"步骤{i}",
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "read"}}]})
        msgs.append({"role": "tool", "content": "结果" * 30, "tool_call_id": f"c{i}"})

    b = resolve_fresh_tail_boundary(msgs, fresh_tail_count=4)
    assert b.count == 4 and b.start == len(msgs) - 4
    # 边界落在 tool 上（count=3 → start 指向 tool）→ 向前扩到 assistant
    b = resolve_fresh_tail_boundary(msgs, fresh_tail_count=3)
    assert msgs[b.start]["role"] == "assistant" and b.tool_group_extended, \
        "组完整性：边界不劈 assistant/tool 组"
    assert b.count == 4 > 3, "尾巴可以超配额（契约明写）"
    # token 界：最新一条永远保留
    b = resolve_fresh_tail_boundary(msgs, fresh_tail_count=6, fresh_tail_max_tokens=1)
    assert b.count >= 1 and b.token_limited
    assert msgs[b.start:][-1] is msgs[-1], "最新一条哪怕超 cap 也保"
    # 孤儿 tool（无 assistant 发起）边界不动
    orphan = [{"role": "user", "content": "q"},
              {"role": "tool", "content": "r", "tool_call_id": "cx"},
              {"role": "assistant", "content": "a"}]
    b = resolve_fresh_tail_boundary(orphan, fresh_tail_count=2)
    assert b.start == 1 and not b.tool_group_extended, "user 挡住向前扩"
    print("lcm fresh_tail selfcheck ok — 配额/组完整性压倒/token 界/孤儿 全对")
