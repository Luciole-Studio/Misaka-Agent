"""Token estimation helpers adapted from Hermes LCM."""
import json


def count_tokens(text):
    """Estimate token count after normalizing non-string values."""
    if not isinstance(text, str):
        text = normalize_content_value(text)
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ratio = non_ascii / len(text)
    divisor = 1.5 if ratio >= 0.5 else (2.5 if ratio >= 0.2 else 4.0)
    return int(len(text) / divisor) + 1


def normalize_content_value(content):
    """Convert message content to a stable string representation."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(content)


def count_message_tokens(msg):
    """Estimate message tokens, including framing and tool calls."""
    total = 4 + count_tokens(normalize_content_value(msg.get("content")))
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            total += count_tokens(str(fn.get("name") or "")) + count_tokens(
                normalize_content_value(fn.get("arguments"))) + 3
    return total


def count_messages_tokens(messages):
    return sum(count_message_tokens(m) for m in messages or [])
