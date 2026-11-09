"""Translated from pi's ``coding-agent/test/suite/regressions/7829-invalid-settings-warning.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.core.agent_session_services import AgentSessionRuntimeDiagnostic
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container
from tests.suite_harness import createHarness


def render(container: Container) -> str:
    return "\n".join(line for child in container.children for line in child.render(120))


async def test_renders_startup_diagnostics_inside_the_transcript(monkeypatch):
    initTheme("dark")
    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    async with createHarness() as harness:
        chatContainer = Container()
        startupDiagnostics = [
            AgentSessionRuntimeDiagnostic(type="warning", message="Invalid settings file /tmp/settings.json: malformed JSON")
        ]
        tasks: set[asyncio.Task] = set()

        def scheduleTask(coroutine):
            tasks.add(asyncio.ensure_future(coroutine))

        context = SimpleNamespace(
            init=AsyncMock(),
            options=SimpleNamespace(
                startupDiagnostics=startupDiagnostics, modelFallbackMessage=None, initialMessage=None, initialMessages=None
            ),
            shutdownRequested=False,
            chatContainer=chatContainer,
            outputPad=1,
            ui=SimpleNamespace(requestRender=Mock()),
            version="test",
            session=harness.session,
            checkTmuxKeyboardSetup=AsyncMock(return_value=None),
            maybeWarnAboutAnthropicSubscriptionAuth=AsyncMock(),
            getUserInput=lambda: asyncio.get_running_loop().create_future(),
            _schedule_task=scheduleTask,
        )
        for name in ("showWarning", "showError", "showStatus", "_promptInitialMessages", "_runInputLoop"):
            setattr(context, name, MethodType(getattr(InteractiveMode, name), context))
        context.lastStatusSpacer = None
        context.lastStatusText = None
        context.lastStatusMessage = ""

        run = asyncio.ensure_future(InteractiveMode.run(context))
        try:
            for _ in range(100):
                if "Warning: Invalid settings file /tmp/settings.json: malformed JSON" in render(chatContainer):
                    break
                await asyncio.sleep(0)
            assert "Warning: Invalid settings file /tmp/settings.json: malformed JSON" in render(chatContainer)
        finally:
            run.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(run, *tasks, return_exceptions=True)
