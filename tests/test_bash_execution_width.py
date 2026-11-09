"""Translated from pi's ``coding-agent/test/bash-execution-width.test.ts`` (v1.1.0).

Test that BashExecutionComponent's collapsed output respects the render-time width,
not a stale captured width. Regression test for #2569.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from misaka.modes.interactive.components.bash_execution import BashExecutionComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import visibleWidth


def createTuiStub(columns: int) -> Any:
    """Minimal TUI stub that only exposes terminal.columns"""
    return SimpleNamespace(
        terminal=SimpleNamespace(columns=columns, rows=24),
        requestRender=lambda: None,
    )


@pytest.fixture(autouse=True, scope="module")
def default_theme():
    initTheme(None, False)


def test_collapsed_preview_lines_respect_render_time_width_not_construction_time_width():
    wideWidth = 200
    narrowWidth = 80

    stub = createTuiStub(wideWidth)
    component = BashExecutionComponent("pwd", stub)

    # Add output with long lines that will wrap differently at different widths
    longLine = "x" * 150
    component.appendOutput(f"{longLine}\n{longLine}\n")

    # Complete the command so it enters collapsed mode
    component.setComplete(0, False)

    # Render at the narrow width (simulating a resize or split pane)
    lines = component.render(narrowWidth)

    # Every rendered line must fit within the narrow width
    for i, line in enumerate(lines):
        w = visibleWidth(line)
        assert w <= narrowWidth, f"Line {i} visibleWidth={w} > {narrowWidth}"


def test_re_computes_lines_when_width_changes_between_renders():
    stub = createTuiStub(200)
    component = BashExecutionComponent("echo hello", stub)

    longLine = "abcdefghij" * 20  # 200 chars
    component.appendOutput(f"{longLine}\n")
    component.setComplete(0, False)

    # First render at width 200
    lines200 = component.render(200)
    for line in lines200:
        assert visibleWidth(line) <= 200

    # Second render at width 60 (split pane scenario)
    lines60 = component.render(60)
    for i, line in enumerate(lines60):
        w = visibleWidth(line)
        assert w <= 60, f"Line {i} visibleWidth={w} > 60"
