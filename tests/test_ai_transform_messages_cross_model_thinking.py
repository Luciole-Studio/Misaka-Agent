"""Translated from pi's ``packages/ai/test/transform-messages-cross-model-thinking.test.ts`` (v1.3.0).

MISAKA: the last case pins the same-model branch upstream rewrote with it: redacted and signed blocks stay even when
their text is empty, empty readable thinking is dropped.
"""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.types import (
    AssistantMessage,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

kimi = Model.model_validate(
    {
        "id": "moonshotai/Kimi-K3",
        "name": "Kimi K3",
        "api": "openai-completions",
        "provider": "sference",
        "baseUrl": "https://api.sference.com/v1",
        "reasoning": True,
        "input": ["text", "image"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1048576,
        "maxTokens": 32768,
    }
)


def _now() -> int:
    return time.time_ns() // 1_000_000


def assistant(model: str, content: list[Any], stopReason: str = "toolUse") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="sference",
        model=model,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason=stopReason,
        timestamp=_now(),
    )


def _dump(content: list[Any]) -> list[dict[str, Any]]:
    return [block.model_dump(exclude_none=True) for block in content]


# https://github.com/earendil-works/pi/issues/10995
def test_does_not_send_another_models_readable_thinking_as_text():
    messages = [
        UserMessage(content="Why does curl work but node fails?", timestamp=_now()),
        assistant(
            "zai-org/GLM-5.3",
            [
                ThinkingContent(
                    thinking="The user asks about ECONNRESET. Candidates: proxy, IPv6, TLS.",
                    thinkingSignature="reasoning_content",
                ),
                TextContent(text="Let me check the TLS versions."),
                ToolCall(id="call_1", name="bash", arguments={"command": "curl -v"}),
            ],
        ),
        ToolResultMessage(
            toolCallId="call_1",
            toolName="bash",
            content=[TextContent(text="TLSv1.2")],
            isError=False,
            timestamp=_now(),
        ),
    ]

    result = transformMessages(messages, kimi)
    assistantMsg = next(message for message in result if message.role == "assistant")

    assert _dump(assistantMsg.content) == [
        {"type": "text", "text": "Let me check the TLS versions."},
        {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "curl -v"}},
    ]


def test_keeps_thinking_only_from_the_target_model_when_models_alternate():
    def thinking(text: str) -> ThinkingContent:
        return ThinkingContent(thinking=text, thinkingSignature="reasoning_content")

    messages = [
        UserMessage(content="1", timestamp=_now()),
        assistant("moonshotai/Kimi-K3", [thinking("kimi 1"), TextContent(text="a")], "stop"),
        UserMessage(content="2", timestamp=_now()),
        assistant("zai-org/GLM-5.3", [thinking("glm 1"), TextContent(text="b")], "stop"),
        UserMessage(content="3", timestamp=_now()),
        assistant("moonshotai/Kimi-K3", [thinking("kimi 2"), TextContent(text="c")], "stop"),
        UserMessage(content="4", timestamp=_now()),
        assistant("zai-org/GLM-5.3", [thinking("glm 2"), TextContent(text="d")], "stop"),
        UserMessage(content="hi", timestamp=_now()),
    ]

    result = transformMessages(messages, kimi)
    contents = [_dump(message.content) for message in result if message.role == "assistant"]

    def thinkingDump(text: str) -> dict[str, Any]:
        return {"type": "thinking", "thinking": text, "thinkingSignature": "reasoning_content"}

    assert contents == [
        [thinkingDump("kimi 1"), {"type": "text", "text": "a"}],
        [{"type": "text", "text": "b"}],
        [thinkingDump("kimi 2"), {"type": "text", "text": "c"}],
        [{"type": "text", "text": "d"}],
    ]


def test_keeps_the_same_models_redacted_and_signed_thinking_and_drops_its_empty_thinking():
    messages = [
        UserMessage(content="1", timestamp=_now()),
        assistant(
            "moonshotai/Kimi-K3",
            [
                ThinkingContent(thinking="", thinkingSignature="encrypted"),
                ThinkingContent(thinking="[Reasoning redacted]", thinkingSignature="opaque", redacted=True),
                ThinkingContent(thinking="   "),
                TextContent(text="a"),
            ],
            "stop",
        ),
        UserMessage(content="2", timestamp=_now()),
    ]

    result = transformMessages(messages, kimi)
    assistantMsg = next(message for message in result if message.role == "assistant")

    assert _dump(assistantMsg.content) == [
        {"type": "thinking", "thinking": "", "thinkingSignature": "encrypted"},
        {"type": "thinking", "thinking": "[Reasoning redacted]", "thinkingSignature": "opaque", "redacted": True},
        {"type": "text", "text": "a"},
    ]
