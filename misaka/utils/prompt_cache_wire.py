# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/prompt_caching.py; see PROVENANCE.json and LICENSE.
# ruff: noqa: I001
from .prompt_cache_boundary import find_stable_prefix

def _text_part(text: str, cache_marker: dict | None = None) -> dict:
    part: dict = {"type": "text", "text": text}
    if cache_marker is not None:
        part["cache_control"] = cache_marker
    return part


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False,
                        tool_part_markers: bool = True) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool" and not native_anthropic and not tool_part_markers:
        # LiteLLM-style envelope: a part marker → tool_result.content[0] → non-retryable 400.
        return
    if (role == "tool" and native_anthropic) or content is None or content == "":
        # Native role:tool: top-level marker, the adapter moves it inside tool_result. Empty
        # content: no part can carry it, and OpenRouter rejects a top-level marker on role:tool
        # (silent hang) and ignores it on empty assistant turns — skip those on the envelope.
        if not (role in ("tool", "assistant") and not native_anthropic):
            msg["cache_control"] = cache_marker
    elif isinstance(content, str):
        stable_prefix = find_stable_prefix(content) if role == "user" else None
        if stable_prefix is not None and content[len(stable_prefix):].strip():
            # Builder-declared boundary: the scaffold carries the breakpoint and the volatile
            # tail rides unmarked. Request-local only — the stored message stays a string.
            msg["content"] = [_text_part(stable_prefix, cache_marker), _text_part(content[len(stable_prefix):])]
        else:
            msg["content"] = [_text_part(content, cache_marker)]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = cache_marker

