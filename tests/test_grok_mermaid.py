"""grok-mermaid's Python port draws what the npm package draws: the fixture holds the real package's output (node,
grok-mermaid 0.2.3) for handwritten and generated diagrams of every supported kind."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from misaka.ui.tui.vendor.grok_mermaid import diagramKind, render, sourceBox, toAnsi

CASES = json.loads((Path(__file__).parent / "fixtures" / "grok_mermaid_oracle.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[str(i) for i in range(len(CASES))])
def test_renders_byte_for_byte_like_the_npm_package(case):
    art = render(case["src"])
    got = None if art is None else {
        "plain": art.plain,
        "styled": [[asdict(span) for span in row] for row in art.styled],
        "width": art.width,
        "warnings": art.warnings,
    }
    assert got == case["art"]
    box = sourceBox(case["src"], 30)
    assert {"plain": box.plain, "width": box.width} == case["box"]


def test_unsupported_and_blank_sources_draw_nothing():
    assert render("   ") is None
    assert render("pie title Pets") is None and diagramKind("pie title Pets") is None
    assert diagramKind("graph TD\n  A -->") == "flowchart"


def test_ansi_output_colours_by_class():
    art = render("graph LR\n  a --> b")
    assert art is not None
    assert any("\x1b[36m" in line for line in toAnsi(art))
