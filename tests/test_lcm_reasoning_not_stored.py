"""A model's thinking is not content: it does not enter the LCM archive.

2026-09-18 (B5): pi's assistant messages carry ``thinking`` blocks with a ``thinkingSignature``,
an opaque provider token that can run to 100 KB. Projected whole into the LCM store, the
signature was being externalized as a "large output". Hermes keeps reasoning parts out of
``content`` entirely, and the provider replay is built from pi's live messages, never from
the store, so the archive has no use for either the text or the token."""
from misaka.ai.types import AssistantMessage
from misaka.extensions.misaka_lcm.host import ingest

_USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
          "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0}}


def _assistant(content):
    return AssistantMessage.model_validate({
        "role": "assistant", "content": content, "stopReason": "stop", "timestamp": 1000,
        "api": "anthropic-messages", "provider": "test", "model": "test-model", "usage": _USAGE})


def test_thinking_blocks_and_their_signatures_stay_out_of_the_archive():
    message = _assistant([
        {"type": "thinking", "thinking": "let me weigh the two editions", "thinkingSignature": "sig" * 40000},
        {"type": "text", "text": "The 1982 edition is the one to cite."},
    ])
    projected = ingest.to_upstream(message)
    assert projected["content"] == [{"type": "text", "text": "The 1982 edition is the one to cite."}]
    assert "thinkingSignature" not in str(projected)


def test_a_thinking_only_turn_projects_to_an_empty_content_list():
    projected = ingest.to_upstream(_assistant([{"type": "thinking", "thinking": "hmm", "redacted": True}]))
    assert projected["content"] == [] and projected["tool_calls"] == []


def test_tool_calls_and_text_are_still_carried():
    message = _assistant([
        {"type": "thinking", "thinking": "first look it up"},
        {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "notes.md"}},
        {"type": "text", "text": "reading"},
    ])
    projected = ingest.to_upstream(message)
    assert [block["type"] for block in projected["content"]] == ["text"]
    assert projected["tool_calls"][0]["function"]["name"] == "read"
