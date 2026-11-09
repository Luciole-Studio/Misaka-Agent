"""Ported from pi's ``coding-agent/test/mermaid.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any, cast

from misaka.modes.interactive.components.mermaid import (
    MermaidTransformerOptions,
    createMermaidMarkdownTransformer,
)


def transformMermaid(markdown: str, **options: Any) -> str:
    transformer = createMermaidMarkdownTransformer(
        MermaidTransformerOptions(getMode=lambda: options.get("mode", "streaming"), theme=options.get("theme"))
    )
    return transformer(
        markdown,
        {
            "availableWidth": options.get("maxWidth", 100),
            "isStreaming": options.get("isStreaming", False),
            "messageType": options.get("messageType", "assistant"),
        },
    )


def test_replaces_mermaid_code_blocks_with_unicode_diagrams():
    markdown = "Before\n\n```mermaid\nflowchart LR\n  A[Start] --> B[Done]\n```\nAfter"
    rendered = transformMermaid(markdown)

    assert "Before" in rendered
    assert "┌───────┐" in rendered
    assert "│ Start ├───▶│ Done │" in rendered
    assert "└───────┘    └──────┘`\nAfter" in rendered
    assert "```mermaid" not in rendered
    assert "After" in rendered


def test_leaves_unsupported_and_oversized_diagrams_unchanged():
    unsupported = '```mermaid\npie\n  title Pets\n  "Dogs" : 4\n```'
    oversized = "```mermaid\nflowchart LR\n  A[Start] --> B[Done]\n```"

    assert transformMermaid(unsupported) == unsupported
    assert transformMermaid(oversized, maxWidth=10) == oversized


def test_maps_semantic_spans_through_the_pi_theme():
    class FakeTheme:
        def fg(self, color: str, text: str) -> str:
            return f"<{color}>{text}</{color}>"

        def bold(self, text: str) -> str:
            return f"<bold>{text}</bold>"

    rendered = transformMermaid("```mermaid\nflowchart LR\n  A --> B\n```", theme=cast(Any, FakeTheme()))

    assert "<borderMuted>" in rendered
    assert "<accent>" in rendered


def test_renders_incomplete_mermaid_blocks_during_streaming():
    partialMarkdown = "```mermaid\nflowchart LR\n  A --> B"

    assert "───▶" in transformMermaid(partialMarkdown, isStreaming=True)


def test_renders_diagrams_with_class_assignments():
    markdown = "```mermaid\nflowchart LR\n  A[Foo]:::highlight --> B[Bar]\n```"
    rendered = transformMermaid(markdown)

    assert "```mermaid" not in rendered
    assert "Mermaid diagram not rendered" not in rendered
    assert "│ Foo ├───▶│ Bar │" in rendered


def test_falls_back_to_the_code_block_with_a_warning_after_streaming():
    markdown = "```mermaid\nflowchart LR\n  A[Foo] invalid\n```"
    final = transformMermaid(markdown)
    followedByText = transformMermaid(f"{markdown}\nFollowing text")
    streaming = transformMermaid(markdown, isStreaming=True)

    assert markdown in final
    assert "```\n`Mermaid diagram not rendered" in final
    assert 'dropped, expected a link: "invalid"' in final
    assert "more)" not in final
    assert "  \nFollowing text" in followedByText
    assert "Mermaid diagram not rendered" not in streaming
    assert "```mermaid" not in streaming
    assert "│ Foo │" in streaming


def test_summarizes_additional_partial_render_warnings():
    markdown = "```mermaid\nflowchart LR\n  A[Foo] invalid\n  B[Bar] also-invalid\n```"
    rendered = transformMermaid(markdown)

    assert markdown in rendered
    assert 'dropped, expected a link: "invalid"' in rendered
    assert "(+1 more)" in rendered
    assert 'dropped, expected a link: "also-invalid"' not in rendered


def test_respects_rendering_modes_and_skips_thinking_blocks():
    markdown = "```mermaid\nflowchart LR\n  A --> B\n```"

    assert transformMermaid(markdown, mode="off") == markdown
    assert transformMermaid(markdown, mode="final", isStreaming=True) == markdown
    assert "```mermaid" not in transformMermaid(markdown, mode="final")
    assert transformMermaid(markdown, messageType="assistant-thinking") == markdown
