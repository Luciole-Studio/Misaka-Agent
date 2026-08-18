"""CJK 感知 token 估算（hermes-lcm tokens.py 的估算路径移植，MIT）。

命门（上游实测教训）：平坦 len//4 会把 CJK 低估 3-4 倍——压缩阈值永不触发、
装配溢出。按非 ASCII 占比调除数：≥50% 用 1.5，≥20% 用 2.5，否则 4。
ponytail: 不带 tiktoken——misaka 全中文场景估算器就是主路径，预算台账也全是估算；
要精确计数时再接（升级路径：可选依赖＋惰性加载，照上游）。
"""
import json


def count_tokens(text):
    """一段文本的 token 估算。非字符串先规范化成字符串。"""
    if not isinstance(text, str):
        text = normalize_content_value(text)
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ratio = non_ascii / len(text)
    divisor = 1.5 if ratio >= 0.5 else (2.5 if ratio >= 0.2 else 4.0)
    return int(len(text) / divisor) + 1


def normalize_content_value(content):
    """消息 content → 确定性字符串（结构化块列表按稳定序序列化，上游同约定）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(content)


def count_message_tokens(msg):
    """一条消息的 token 估算：4（角色/框架开销）＋content＋每个工具调用。"""
    total = 4 + count_tokens(normalize_content_value(msg.get("content")))
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            total += count_tokens(str(fn.get("name") or "")) + count_tokens(
                normalize_content_value(fn.get("arguments"))) + 3
    return total


def count_messages_tokens(messages):
    return sum(count_message_tokens(m) for m in messages or [])


if __name__ == "__main__":
    ascii_text = "a" * 400
    cjk_text = "档" * 400
    assert count_tokens(ascii_text) == 101, count_tokens(ascii_text)
    assert count_tokens(cjk_text) == 267, "CJK 除数 1.5：400 字 ≈ 267 token"
    assert count_tokens(cjk_text) > count_tokens(ascii_text) * 2, \
        "平坦 len//4 的 3-4 倍低估必须被修正"
    mixed = "档案资料 archive " * 50   # 非 ASCII 占比 4/14≈0.29 → 除数 2.5
    assert count_tokens(mixed) == int(len(mixed) / 2.5) + 1, count_tokens(mixed)
    assert count_tokens("") == 0 and count_tokens(None) == 0
    msg = {"role": "assistant", "content": "查入藏簿",
           "tool_calls": [{"function": {"name": "read", "arguments": {"path": "a.md"}}}]}
    assert count_message_tokens(msg) > 4 + count_tokens("查入藏簿")
    assert normalize_content_value([{"type": "text", "text": "块"}]) == \
        normalize_content_value([{"text": "块", "type": "text"}]), "结构化内容稳定序列化"
    print("lcm tokens selfcheck ok — CJK 除数/消息计数/稳定序列化 全对")
