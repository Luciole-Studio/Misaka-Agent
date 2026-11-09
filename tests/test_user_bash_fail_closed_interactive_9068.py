"""Translated from the interactive rows of pi's ``coding-agent/test/suite/regressions/9068-user-bash-fail-closed.test.ts``
(v1.1.0). MISAKA has no RPC mode, so the RPC rows are not ported."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.utils.js import startTask
from tests.suite_harness import createHarness

localResult = {"output": "local output", "exitCode": 0, "cancelled": False, "truncated": False}


@pytest.mark.parametrize(("input", "excludeFromContext"), [("!pwd", False), ("!!pwd", True)])
async def test_fails_closed_when_a_handler_returns_an_empty_result(monkeypatch, input, excludeFromContext):
    events: list = []

    def factory(pi):
        async def onUserBash(event, _ctx):
            events.append(event)
            return {}

        pi.on("user_bash", onUserBash)

    async with createHarness(extensionFactories=[factory]) as harness:
        executeBash = AsyncMock(return_value=localResult)
        monkeypatch.setattr(harness.session, "executeBash", executeBash)
        tasks: list[asyncio.Future] = []
        context = SimpleNamespace(
            defaultEditor=SimpleNamespace(),
            editor=SimpleNamespace(addToHistory=Mock(), setText=Mock()),
            session=harness.session,
            sessionManager=harness.sessionManager,
            ui=SimpleNamespace(requestRender=Mock()),
            chatContainer=SimpleNamespace(addChild=Mock()),
            pendingMessagesContainer=SimpleNamespace(addChild=Mock()),
            pendingBashComponents=[],
            isBashMode=True,
            outputPad=1,
            showError=Mock(),
            updateEditorBorderColor=Mock(),
            # MISAKA fork: the editor calls `onSubmit` synchronously, so the handler runs as a task.
            _schedule_task=lambda coroutine: tasks.append(startTask(coroutine)),
        )
        context.handleBashCommand = MethodType(InteractiveMode.handleBashCommand, context)
        InteractiveMode.setupEditorSubmitHandler(context)

        context.defaultEditor.onSubmit(input)
        await asyncio.gather(*tasks)

        assert events == [
            {"type": "user_bash", "command": "pwd", "excludeFromContext": excludeFromContext, "cwd": harness.sessionManager.getCwd()}
        ]
        executeBash.assert_not_called()
