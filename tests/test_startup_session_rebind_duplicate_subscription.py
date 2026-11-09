"""Translated from pi's ``coding-agent/test/suite/regressions/startup-session-rebind-duplicate-subscription.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.utils.js import startTask


async def test_does_not_subscribe_from_the_stale_startup_rebind():
    startupSession = object()
    replacementSession = object()
    loop = asyncio.get_running_loop()
    startupBind: asyncio.Future[None] = loop.create_future()
    replacementBind: asyncio.Future[None] = loop.create_future()

    subscribeToAgent = Mock()
    updateTerminalTitle = Mock()
    bindCount = 0

    def bindCurrentSessionExtensions():
        nonlocal bindCount
        bindCount += 1
        return startupBind if bindCount == 1 else replacementBind

    context = SimpleNamespace(
        session=startupSession,
        unsubscribe=None,
        applyRuntimeSettings=lambda: None,
        programStatus=SimpleNamespace(reset=lambda: None),
        renderCurrentSessionState=lambda: None,
        bindCurrentSessionExtensions=bindCurrentSessionExtensions,
        subscribeToAgent=subscribeToAgent,
        updateAvailableProviderCount=lambda: None,
        updateEditorBorderColor=lambda: None,
        updateTerminalTitle=updateTerminalTitle,
    )

    # MISAKA fork: `startTask` runs the coroutine up to its first suspension, as a JavaScript call does.
    startupRebind = startTask(InteractiveMode.rebindCurrentSession(context))
    assert bindCount == 1

    context.session = replacementSession
    replacementRebind = startTask(InteractiveMode.rebindCurrentSession(context, {"renderBeforeBind": True}))

    assert bindCount == 2
    assert subscribeToAgent.call_count == 1

    startupBind.set_result(None)
    await startupRebind

    assert subscribeToAgent.call_count == 1
    updateTerminalTitle.assert_not_called()

    replacementBind.set_result(None)
    await replacementRebind

    assert subscribeToAgent.call_count == 1
    assert updateTerminalTitle.call_count == 1
