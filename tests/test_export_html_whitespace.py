"""Translated from pi's ``packages/coding-agent/test/export-html-whitespace.test.ts`` (v1.1.0)."""

from __future__ import annotations

import re
from pathlib import Path

from misaka.core import export_html
from misaka.core.export_html.ansi_to_html import ansiLinesToHtml
from misaka.core.export_html.tool_renderer import (
    ToolHtmlRendererDeps,
    createToolHtmlRenderer,
)

# --- export HTML tool output whitespace -----------------------------------------------------------


def test_preserves_whitespace_for_plain_text_tool_output_lines_without_preserving_template_whitespace():
    css = (Path(export_html.__file__).parent / "template.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.output-preview > div:not\(\.expand-hint\),\s*\.output-full > div:not\(\.expand-hint\) \{[\s\S]*?white-space:\s*pre-wrap;",
        css,
    )
    assert re.search(r"\.ansi-line\s*\{[\s\S]*?white-space:\s*pre;", css)
    assert not re.search(r"\.output-preview,\s*\.output-full\s*\{[\s\S]*?white-space:\s*pre-wrap;", css)


def test_does_not_insert_source_whitespace_between_ansi_rendered_lines():
    assert ansiLinesToHtml(["one", "two"]) == '<div class="ansi-line">one</div><div class="ansi-line">two</div>'


def test_trims_tui_spacing_lines_from_custom_tool_result_html():
    class Component:
        def render(self, _width: int) -> list[str]:
            return ["", "\u001b[31mone\u001b[0m", "two", ""]

        def invalidate(self) -> None:
            pass

    component = Component()
    tool = {"name": "custom", "label": "custom", "description": "custom", "renderResult": lambda *_args: component}
    renderer = createToolHtmlRenderer(ToolHtmlRendererDeps(getToolRenderers=lambda _name: tool, theme={}, cwd="/tmp"))

    result = renderer.renderResult("id", "custom", [], None, False)
    assert result is not None
    assert result.expanded == (
        '<div class="ansi-line"><span style="color:#800000">one</span></div><div class="ansi-line">two</div>'
    )
