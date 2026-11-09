"""Translated from pi's ``coding-agent/test/interactive-tui.test.ts`` (v1.1.0).

MISAKA fork (D1): only the main-screen renderer exists, so the fullscreen cases (renderer switching, the reference proxy,
selection copy, the "Copied!" flash, transcript selection reset) are not ported, and the standalone status row case
runs in the regular mode only.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.modes.interactive import interactive_mode
from misaka.modes.interactive.components.status_indicator import (
    BranchSummaryStatusIndicator,
    CompactionStatusIndicator,
    RetryStatusIndicator,
    WorkingStatusIndicator,
)
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.modes.interactive.tui_renderer import (
    InteractiveTuiOptions,
    createInteractiveTui,
)
from misaka.ui.tui import Container, Text
from tests.tui_virtual_terminal import VirtualTerminal


async def test_keeps_the_status_line_confirmation_for_the_copy_shortcut_in_regular_mode(monkeypatch):
    copyToClipboard = AsyncMock()
    monkeypatch.setattr(interactive_mode, "copyToClipboard", copyToClipboard)
    ui = createInteractiveTui(InteractiveTuiOptions(showHardwareCursor=False, logDirectory="/tmp", terminal=VirtualTerminal()))
    context = SimpleNamespace(
        session=SimpleNamespace(getLastAssistantText=lambda: "assistant response"),
        ui=ui,
        showStatus=Mock(),
        showError=Mock(),
    )

    await InteractiveMode.handleCopyCommand(context, {"flashConfirmation": True, "preferSelection": True})

    copyToClipboard.assert_awaited_once_with("assistant response")
    context.showStatus.assert_called_once_with("Copied last agent message to clipboard")
    context.showError.assert_not_called()


# --- clear-on-shrink status spacing --------------------------------------------------------------------------------


def statusContext(*, activeStatusIndicator=None, activeWorkingIndicatorEmbedded=False, defaultEditor, editor):
    context = SimpleNamespace(
        activeStatusIndicator=activeStatusIndicator,
        activeWorkingIndicatorEmbedded=activeWorkingIndicatorEmbedded,
        statusContainer=Container(),
        defaultEditor=defaultEditor,
        editor=editor,
        ui=SimpleNamespace(getClearOnShrink=lambda: True),
        idleStatus=Text("", 0, 0),
    )
    context.setEditorWorkingStatusIndicator = MethodType(InteractiveMode.setEditorWorkingStatusIndicator, context)
    return context


@pytest.mark.parametrize("embedWorkingStatus", [True, False])
def test_routes_every_status_through_the_editor_opt_in(embedWorkingStatus):
    initTheme("dark")
    tui = SimpleNamespace(requestRender=Mock())
    editor = SimpleNamespace(embedWorkingStatus=embedWorkingStatus, setWorkingStatusIndicator=Mock())
    context = statusContext(
        defaultEditor=SimpleNamespace(embedWorkingStatus=True, setWorkingStatusIndicator=Mock()), editor=editor
    )
    indicators = [
        WorkingStatusIndicator(tui, "Working"),
        CompactionStatusIndicator(tui, "manual"),
        CompactionStatusIndicator(tui, "threshold"),
        CompactionStatusIndicator(tui, "overflow"),
        BranchSummaryStatusIndicator(tui),
        RetryStatusIndicator(tui, 1, 3, 1000),
    ]
    try:
        for indicator in indicators:
            InteractiveMode.showStatusIndicator(context, indicator)
            assert context.activeStatusIndicator is indicator
            assert context.activeWorkingIndicatorEmbedded is embedWorkingStatus
            if embedWorkingStatus:
                editor.setWorkingStatusIndicator.assert_called_with(indicator)
                assert len(context.statusContainer.children) == 0
            else:
                assert context.statusContainer.children == [indicator]
    finally:
        for indicator in indicators:
            indicator.dispose()


@pytest.mark.parametrize("kind", ["working", "compaction", "branchSummary", "retry"])
def test_does_not_reserve_separate_status_height_for_an_embedded_indicator(kind):
    dispose = Mock()
    editor = SimpleNamespace(embedWorkingStatus=True, setWorkingStatusIndicator=Mock())
    context = statusContext(
        activeStatusIndicator=SimpleNamespace(kind=kind, dispose=dispose),
        activeWorkingIndicatorEmbedded=True,
        defaultEditor=editor,
        editor=editor,
    )

    InteractiveMode.clearStatusIndicator(context)

    dispose.assert_called_once_with()
    editor.setWorkingStatusIndicator.assert_called_with(None)
    assert len(context.statusContainer.children) == 0


def test_uses_the_standalone_row_for_a_custom_editor_that_has_not_opted_in():
    defaultEditor = SimpleNamespace(embedWorkingStatus=True, setWorkingStatusIndicator=Mock())
    customEditor = SimpleNamespace(embedWorkingStatus=False, setWorkingStatusIndicator=Mock())
    context = statusContext(
        activeStatusIndicator=SimpleNamespace(kind="working", dispose=Mock()),
        activeWorkingIndicatorEmbedded=False,
        defaultEditor=defaultEditor,
        editor=customEditor,
    )

    InteractiveMode.clearStatusIndicator(context)

    defaultEditor.setWorkingStatusIndicator.assert_called_with(None)
    customEditor.setWorkingStatusIndicator.assert_not_called()
    assert len(context.statusContainer.children) == 1
