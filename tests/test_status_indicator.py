"""Translated from pi's ``coding-agent/test/status-indicator.test.ts`` (v1.3.0, unchanged since v1.1.0).

``vi.useFakeTimers()`` becomes ``fakeTimers``: the tests run without an event loop, so the Loader and CountdownTimer
schedule through ``threading.Timer``, which the fixture swaps for timers that ``FakeTimers.advance`` runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components import countdown_timer as countdownModule
from misaka.modes.interactive.components.custom_editor import (
    CustomEditor,
    CustomEditorOptions,
)
from misaka.modes.interactive.components.status_indicator import (
    BranchSummaryStatusIndicator,
    CompactionStatusIndicator,
    IdleStatus,
    RetryStatusIndicator,
    WorkingStatusIndicator,
)
from misaka.modes.interactive.theme.theme import getEditorTheme, initTheme, theme
from misaka.ui.tui import visibleWidth
from misaka.ui.tui.components import loader as loaderModule
from misaka.utils.ansi import stripAnsi
from tests.fake_timers import FakeTimers


@pytest.fixture
def fakeTimers(monkeypatch: pytest.MonkeyPatch) -> FakeTimers:
    timers = FakeTimers()

    class _Timer:
        def __init__(self, interval: float, function: Any) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False
            self.handle: Any = None

        def start(self) -> None:
            self.handle = timers.setTimeout(self.function, self.interval * 1000)

        def cancel(self) -> None:
            timers.clearTimeout(self.handle)

    monkeypatch.setattr(loaderModule.threading, "Timer", _Timer)
    monkeypatch.setattr(countdownModule.threading, "Timer", _Timer)
    return timers


def _tui(**fields: Any) -> Any:
    return SimpleNamespace(requestRender=fields.get("requestRender", MagicMock()), terminal=SimpleNamespace(rows=10))


def test_keeps_idle_status_at_the_same_height_as_standalone_status_indicators():
    idleStatus = IdleStatus()

    lines = idleStatus.render(20)
    assert len(lines) == 2
    assert lines == [" " * 20, " " * 20]


def test_keeps_the_top_border_unchanged_unless_the_editor_opts_in(fakeTimers):
    initTheme("dark")
    tui = _tui()
    editor = CustomEditor(tui, getEditorTheme(), KeybindingsManager.create())
    indicator = WorkingStatusIndicator(tui, "Working")
    editor.setWorkingStatusIndicator(indicator)

    assert stripAnsi(editor.render(20)[0]) == "─" * 20
    standaloneLine = indicator.render(20)[1]
    assert theme.getFgAnsi("accent") in standaloneLine
    assert theme.getFgAnsi("muted") in standaloneLine
    indicator.dispose()


def test_embeds_the_working_indicator_when_the_editor_opts_in(fakeTimers):
    initTheme("dark")
    tui = _tui()
    editor = CustomEditor(
        tui,
        getEditorTheme(),
        KeybindingsManager.create(),
        CustomEditorOptions(embedWorkingStatus=True),
    )
    assert editor.embedWorkingStatus is True
    editor.borderColor = theme.getThinkingBorderColor("high")
    indicator = WorkingStatusIndicator(tui, "Working", None, lambda text: editor.borderColor(text))
    editor.setWorkingStatusIndicator(indicator)

    topBorder = editor.render(20)[0]
    assert stripAnsi(topBorder) == "── ⠋ Working ───────"
    assert visibleWidth(topBorder) == 20
    assert len(topBorder.split(theme.getFgAnsi("thinkingHigh"))) == 5
    indicator.dispose()


def test_embeds_compaction_summary_and_retry_labels_within_the_border_width(fakeTimers):
    initTheme("dark")
    tui = _tui()
    editor = CustomEditor(
        tui,
        getEditorTheme(),
        KeybindingsManager.create(),
        CustomEditorOptions(embedWorkingStatus=True),
    )
    indicators = [
        CompactionStatusIndicator(tui, "manual"),
        CompactionStatusIndicator(tui, "threshold"),
        CompactionStatusIndicator(tui, "overflow"),
        BranchSummaryStatusIndicator(tui),
        RetryStatusIndicator(tui, 1, 3, 3000),
    ]
    try:
        for indicator in indicators:
            editor.setWorkingStatusIndicator(indicator)
            label = stripAnsi(indicator.render(120)[1]).strip()
            assert f"── {label} " in stripAnsi(editor.render(120)[0])
            for width in [1, 4, 10, 20, 80, 120]:
                assert visibleWidth(editor.render(width)[0]) == width
        fakeTimers.advance(1000)
        assert "Retrying (1/3) in 2s" in stripAnsi(editor.render(120)[0])
        editor.setWorkingStatusIndicator(None)
        assert stripAnsi(editor.render(120)[0]) == "─" * 120
    finally:
        for indicator in indicators:
            indicator.dispose()


def test_colors_an_embedded_compaction_indicator_like_the_editor_border(fakeTimers):
    # MISAKA: #10809. A compaction indicator given the border color draws spinner and label in it; without one it
    # keeps accent and muted.
    initTheme("dark")
    tui = _tui()
    editor = CustomEditor(
        tui,
        getEditorTheme(),
        KeybindingsManager.create(),
        CustomEditorOptions(embedWorkingStatus=True),
    )
    editor.borderColor = theme.getThinkingBorderColor("high")
    indicator = CompactionStatusIndicator(tui, "manual", lambda text: editor.borderColor(text))
    editor.setWorkingStatusIndicator(indicator)
    topBorder = editor.render(60)[0]
    assert theme.getFgAnsi("accent") not in topBorder
    assert theme.getFgAnsi("muted") not in topBorder
    assert len(topBorder.split(theme.getFgAnsi("thinkingHigh"))) == 5
    indicator.dispose()

    standalone = CompactionStatusIndicator(tui, "manual")
    line = standalone.render(60)[1]
    assert theme.getFgAnsi("accent") in line
    assert theme.getFgAnsi("muted") in line
    standalone.dispose()


def test_disposes_retry_countdown_updates(fakeTimers):
    initTheme("dark")
    requestRender = MagicMock()
    tui = SimpleNamespace(requestRender=requestRender)
    indicator = RetryStatusIndicator(tui, 1, 3, 1000)
    callsBeforeDispose = requestRender.call_count

    indicator.dispose()
    fakeTimers.advance(2000)

    assert requestRender.call_count == callsBeforeDispose
