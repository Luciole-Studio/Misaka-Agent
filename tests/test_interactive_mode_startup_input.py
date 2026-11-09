"""Translated from pi's ``coding-agent/test/interactive-mode-startup-input.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode


def createSubmitContext():
    tasks: list[asyncio.Future] = []
    context = SimpleNamespace(
        defaultEditor=SimpleNamespace(),
        editor=SimpleNamespace(addToHistory=Mock(), setText=Mock()),
        session=SimpleNamespace(isCompacting=False, isStreaming=False, isBashRunning=False, prompt=AsyncMock()),
        flushPendingBashComponents=Mock(),
        onInputCallback=None,
        pendingUserInputs=[],
        # MISAKA fork: the editor calls `onSubmit` synchronously, so the handler runs as a task.
        _schedule_task=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
    )
    return context, tasks


def test_restores_a_prompt_submitted_while_startup_is_running():
    context = SimpleNamespace(editor=SimpleNamespace(setText=Mock()), showStatus=Mock())

    InteractiveMode.handleStartupSubmit(context, "early prompt")

    context.editor.setText.assert_called_once_with("early prompt")
    context.showStatus.assert_called_once_with("Startup is still in progress")


async def test_queues_a_normal_prompt_submitted_before_the_input_callback_is_installed():
    context, tasks = createSubmitContext()
    InteractiveMode.setupEditorSubmitHandler(context)

    context.defaultEditor.onSubmit(" early prompt ")
    await asyncio.gather(*tasks)

    assert context.pendingUserInputs == ["early prompt"]
    assert context.flushPendingBashComponents.call_count == 1
    context.editor.addToHistory.assert_called_once_with("early prompt")


async def test_returns_queued_startup_input_before_installing_a_new_input_callback():
    context = SimpleNamespace(pendingUserInputs=["queued prompt"], onInputCallback=None)

    assert await InteractiveMode.getUserInput(context) == "queued prompt"
    assert context.onInputCallback is None
    assert context.pendingUserInputs == []
