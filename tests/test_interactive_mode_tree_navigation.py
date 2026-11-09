"""Translated from pi's ``coding-agent/test/interactive-mode-tree-navigation.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container

busyMessage = "Wait for the current compaction or tree navigation to finish before navigating the session tree."


def userMsg(text: str) -> dict:
    return {"role": "user", "content": text, "timestamp": 1}


def assistantMsg(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "faux",
        "provider": "faux",
        "model": "faux",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": 2,
    }


def createTreeUI():
    sessionManager = SessionManager.inMemory()
    targetId = sessionManager.appendMessage(userMsg("first"))
    sessionManager.appendMessage(assistantMsg("reply"))
    selectors: list = []
    tasks: list[asyncio.Future] = []
    onEscape = Mock()

    async def abort():
        ui.session.isStreaming = False

    async def navigateTree(*_args, **_kwargs):
        if ui.session.isCompacting:
            raise RuntimeError(busyMessage)
        return {"cancelled": False}

    def showSelector(create):
        selectors.append(create(Mock())["component"])

    ui = SimpleNamespace(
        sessionManager=sessionManager,
        settingsManager=SettingsManager.inMemory(),
        session=SimpleNamespace(
            isStreaming=False,
            isCompacting=False,
            abort=AsyncMock(side_effect=abort),
            abortBranchSummary=Mock(),
            navigateTree=AsyncMock(side_effect=navigateTree),
        ),
        defaultEditor=SimpleNamespace(onEscape=onEscape),
        editor=SimpleNamespace(getText=lambda: "", setText=Mock()),
        chatContainer=Container(),
        isInitialized=True,
        footer=SimpleNamespace(invalidate=Mock()),
        ui=SimpleNamespace(terminal=SimpleNamespace(rows=24, setProgress=Mock()), requestRender=Mock()),
        showSelector=showSelector,
        showExtensionSelector=AsyncMock(return_value="No summary"),
        # Dispose newly created indicators so a failing regression cannot leak spinner timers.
        showStatusIndicator=Mock(side_effect=lambda indicator: indicator.dispose()),
        clearStatusIndicator=Mock(),
        restoreQueuedMessagesToEditor=Mock(),
        renderInitialMessages=Mock(),
        showStatus=Mock(),
        showError=Mock(),
        flushCompactionQueue=AsyncMock(return_value=None),
        # MISAKA fork: `void promise` calls are tasks.
        _schedule_task=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
    )
    InteractiveMode.showTreeSelector(ui)

    async def select():
        assert selectors
        selectors[0].getTreeList().onSelect(targetId)
        while tasks:
            await tasks.pop(0)

    return SimpleNamespace(ui=ui, onEscape=onEscape, targetId=targetId, select=select)


@pytest.fixture(autouse=True)
def darkTheme():
    initTheme("dark")


@pytest.mark.parametrize("choice", ["Summarize", "No summary"])
async def test_preserves_operation_ui_when_choosing_while_busy(choice):
    # Regression for #9178 / PR #9179: rejection must not replace the active operation's UI.
    tree = createTreeUI()
    ui = tree.ui
    originalLeafId = ui.sessionManager.getLeafId()

    async def openDialog(*_args, **_kwargs):
        # Compaction or another navigation can start while the dialog is open.
        ui.session.isCompacting = True
        return choice

    ui.showExtensionSelector.side_effect = openDialog

    await tree.select()

    ui.showError.assert_called_with(busyMessage)
    ui.showStatusIndicator.assert_not_called()
    ui.clearStatusIndicator.assert_not_called()
    assert ui.defaultEditor.onEscape is tree.onEscape
    ui.session.navigateTree.assert_not_called()
    ui.session.abort.assert_not_called()
    assert ui.sessionManager.getLeafId() == originalLeafId


async def test_allows_navigation_when_compaction_finishes_while_the_dialog_is_open():
    tree = createTreeUI()
    ui = tree.ui
    ui.session.isCompacting = True

    async def openDialog(*_args, **_kwargs):
        ui.session.isCompacting = False
        return "No summary"

    ui.showExtensionSelector.side_effect = openDialog

    await tree.select()

    ui.session.navigateTree.assert_awaited_once_with(tree.targetId, {"summarize": False, "customInstructions": None})
    ui.showError.assert_not_called()


async def test_still_aborts_an_active_response_before_navigating():
    tree = createTreeUI()
    ui = tree.ui
    ui.session.isStreaming = True

    async def navigateTree(*_args, **_kwargs):
        assert ui.session.isStreaming is False
        ui.restoreQueuedMessagesToEditor.assert_called_once()
        return {"cancelled": False}

    ui.session.navigateTree.side_effect = navigateTree

    await tree.select()

    ui.session.abort.assert_awaited_once()
    ui.session.navigateTree.assert_awaited_once()
    ui.showError.assert_not_called()


async def test_rechecks_availability_after_the_response_abort_settles():
    tree = createTreeUI()
    ui = tree.ui
    ui.session.isStreaming = True
    ui.showExtensionSelector.return_value = "Summarize"

    async def abort():
        ui.session.isStreaming = False
        ui.session.isCompacting = True

    ui.session.abort.side_effect = abort

    await tree.select()

    ui.session.abort.assert_awaited_once()
    ui.showError.assert_called_with(busyMessage)
    ui.showStatusIndicator.assert_not_called()
    ui.clearStatusIndicator.assert_not_called()
    assert ui.defaultEditor.onEscape is tree.onEscape
    ui.session.navigateTree.assert_not_called()
