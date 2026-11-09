"""Translated from pi's ``packages/ai/test/text.test.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.types import ImageContent, TextContent, ThinkingContent, ToolCall
from misaka.ai.utils.text import contentText

content = [
    ThinkingContent(thinking="reasoning"),
    TextContent(text="first"),
    ToolCall(id="1", name="read", arguments={}),
    TextContent(text="second"),
]


def test_extracts_assistant_text_blocks():
    assert contentText(content) == "first\nsecond"


def test_supports_custom_separators():
    assert contentText(content, "") == "firstsecond"


def test_passes_string_content_through():
    assert contentText("hello") == "hello"


def test_extracts_text_from_tool_result_content():
    toolResultContent = [
        TextContent(text="first"),
        ImageContent(data="...", mimeType="image/png"),
        TextContent(text="second"),
    ]

    assert contentText(toolResultContent, "") == "firstsecond"
