"""Translated from pi's ``coding-agent/test/interactive-mode-clone-command.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode


def cloneContext(leafId: str | None, fork: AsyncMock):
    return SimpleNamespace(
        sessionManager=SimpleNamespace(getLeafId=lambda: leafId),
        runtimeHost=SimpleNamespace(fork=fork),
        renderCurrentSessionState=Mock(),
        editor=SimpleNamespace(setText=Mock()),
        showStatus=Mock(),
        showError=Mock(),
        ui=SimpleNamespace(requestRender=Mock()),
    )


async def test_clones_the_current_leaf_into_a_new_session():
    fork = AsyncMock(return_value={"cancelled": False})
    context = cloneContext("leaf-123", fork)

    await InteractiveMode.handleCloneCommand(context)

    fork.assert_awaited_once_with("leaf-123", {"position": "at"})
    context.renderCurrentSessionState.assert_not_called()
    context.editor.setText.assert_called_once_with("")
    context.showStatus.assert_called_once_with("Cloned to new session")
    context.showError.assert_not_called()
    context.ui.requestRender.assert_not_called()


async def test_shows_a_status_message_when_there_is_nothing_to_clone():
    fork = AsyncMock(return_value={"cancelled": False})
    context = cloneContext(None, fork)

    await InteractiveMode.handleCloneCommand(context)

    fork.assert_not_called()
    context.showStatus.assert_called_once_with("Nothing to clone yet")
    context.showError.assert_not_called()
