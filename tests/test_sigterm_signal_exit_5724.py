"""Translated from pi's ``coding-agent/test/suite/regressions/5724-sigterm-signal-exit.test.ts`` (v1.1.0).

The signal handlers stay registered until the asynchronous terminal cleanup of a signal-triggered shutdown is done.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode


async def test_keeps_signal_handlers_registered_while_signal_triggered_cleanup_is_pending():
    order: list[str] = []
    dispose: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def startDispose():
        order.append("dispose")
        return dispose

    async def drainInput(_ms):
        order.append("drainInput")

    context = SimpleNamespace(
        isShuttingDown=False,
        unregisterSignalHandlers=Mock(side_effect=lambda: order.append("unregister")),
        runtimeHost=SimpleNamespace(dispose=Mock(side_effect=startDispose)),
        ui=SimpleNamespace(terminal=SimpleNamespace(drainInput=AsyncMock(side_effect=drainInput))),
        stop=Mock(side_effect=lambda: order.append("stop")),
        _pendingUserInputFuture=None,
        onInputCallback=None,
        _shutdownFuture=None,
        _finishShutdown=lambda: 0,
    )

    shutdownPromise = asyncio.ensure_future(InteractiveMode.shutdown(context, {"fromSignal": True}))
    await asyncio.sleep(0)

    assert order == ["dispose"]
    context.unregisterSignalHandlers.assert_not_called()

    dispose.set_result(None)
    await shutdownPromise

    assert order == ["dispose", "drainInput", "stop"]
