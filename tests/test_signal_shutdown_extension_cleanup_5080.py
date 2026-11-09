"""Translated from pi's ``coding-agent/test/suite/regressions/5080-signal-shutdown-extension-cleanup.test.ts``
(v1.1.0).

On SIGTERM/SIGHUP the graceful shutdown must emit ``session_shutdown`` (``runtimeHost.dispose``) BEFORE touching the
terminal; the interactive quit path keeps the opposite order to preserve the final TUI frame.

MISAKA forks: ``shutdown`` returns the exit code instead of exiting the process; the resume hint names ``misaka chat``;
Python's standard streams have no error events, so pi's "dead terminal errors on stdin" rows have no counterpart (a dead
terminal's ``OSError`` reaches ``uncaughtCrash``, whose rows are ported), and there is no crash record.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.config import APP_NAME
from misaka.modes.interactive import interactive_mode
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.utils.chalk import chalk


def createSessionManager(sessionFile: str | None = None):
    return SimpleNamespace(
        isPersisted=lambda: sessionFile is not None,
        getSessionFile=lambda: sessionFile,
        getSessionId=lambda: "test-session",
        getSessionDir=lambda: "/tmp/pi-sessions",
    )


def createContext(order: list[str], sessionManager=None):
    async def dispose():
        order.append("dispose")

    async def drainInput(_ms):
        order.append("drainInput")

    return SimpleNamespace(
        isShuttingDown=False,
        unregisterSignalHandlers=Mock(),
        runtimeHost=SimpleNamespace(dispose=AsyncMock(side_effect=dispose)),
        ui=SimpleNamespace(terminal=SimpleNamespace(drainInput=AsyncMock(side_effect=drainInput))),
        stop=Mock(side_effect=lambda: order.append("stop")),
        sessionManager=sessionManager or createSessionManager(),
        # MISAKA fork: the input loop's pending read and the exit-code future.
        _pendingUserInputFuture=None,
        onInputCallback=None,
        _shutdownFuture=None,
        _finishShutdown=lambda: 0,
    )


@pytest.fixture
def captureStdout(monkeypatch):
    """Replaces stdout from inside the test body (pytest resets ``sys.stdout`` between setup and call)."""
    monkeypatch.delenv("MISAKA_WHO", raising=False)

    def capture() -> list[str]:
        writes: list[str] = []
        monkeypatch.setattr(interactive_mode.sys, "stdout", SimpleNamespace(isatty=lambda: True, write=writes.append))
        return writes

    return capture


def createTempFile(tmp_path) -> str:
    file = os.path.join(tmp_path, "session.jsonl")
    with open(file, "w", encoding="utf-8") as handle:
        handle.write("\n")
    return file


async def test_signal_triggered_shutdown_emits_session_shutdown_before_terminal_writes():
    order: list[str] = []
    context = createContext(order)

    await InteractiveMode.shutdown(context, {"fromSignal": True})

    assert order == ["dispose", "drainInput", "stop"]
    assert context.isShuttingDown is True


async def test_interactive_quit_stops_the_tui_before_emitting_session_shutdown():
    order: list[str] = []
    context = createContext(order)

    await InteractiveMode.shutdown(context)

    assert order == ["drainInput", "stop", "dispose"]


async def test_interactive_quit_prints_a_resume_hint_for_persisted_sessions(captureStdout, tmp_path):
    stdout = captureStdout()
    order: list[str] = []
    context = createContext(order, createSessionManager(createTempFile(tmp_path)))

    await InteractiveMode.shutdown(context)

    assert order == ["drainInput", "stop", "dispose"]
    assert stdout == [f"{chalk.dim('To resume this session:')} {APP_NAME} chat --session test-session\n"]


async def test_signal_triggered_shutdown_does_not_print_a_resume_hint(captureStdout, tmp_path):
    stdout = captureStdout()
    order: list[str] = []
    context = createContext(order, createSessionManager(createTempFile(tmp_path)))

    await InteractiveMode.shutdown(context, {"fromSignal": True})

    assert all("To resume this session:" not in text for text in stdout)


async def test_re_entrant_shutdown_is_a_no_op():
    order: list[str] = []
    context = createContext(order)
    context.isShuttingDown = True

    await InteractiveMode.shutdown(context, {"fromSignal": True})

    assert order == []
    context.runtimeHost.dispose.assert_not_called()


# --- dead terminal errors -------------------------------------------------------------------------------------------


def createHandlerContext():
    context = SimpleNamespace(
        isShuttingDown=False,
        signalCleanupHandlers=[],
        shutdown=AsyncMock(),
        getCrashExtensionHint=lambda _error: None,
        ui=SimpleNamespace(stop=Mock()),
        _schedule_task=lambda coroutine: coroutine.close(),
    )
    context.unregisterSignalHandlers = lambda: InteractiveMode.unregisterSignalHandlers(context)
    context.emergencyTerminalExit = lambda: InteractiveMode.emergencyTerminalExit(context)
    context.uncaughtCrash = lambda error: InteractiveMode.uncaughtCrash(context, error)
    return context


@pytest.fixture
def handlerContext(monkeypatch):
    monkeypatch.setattr(interactive_mode, "killTrackedDetachedChildren", lambda: None)
    context = createHandlerContext()
    InteractiveMode.registerSignalHandlers(context)
    yield context
    context.unregisterSignalHandlers()


def test_uncaught_dead_terminal_errors_exit_quietly(handlerContext, capsys):
    with pytest.raises(SystemExit) as exit:
        handlerContext.uncaughtCrash(OSError(errno.EIO, "read EIO"))

    assert exit.value.code == 129
    handlerContext.ui.stop.assert_not_called()
    assert capsys.readouterr().err == ""


def test_other_uncaught_errors_still_crash(handlerContext, capsys):
    with pytest.raises(SystemExit) as exit:
        handlerContext.uncaughtCrash(RuntimeError("boom"))

    assert exit.value.code == 1
    handlerContext.ui.stop.assert_called_once_with()
    assert f"{APP_NAME} exiting due to uncaughtException:" in capsys.readouterr().err


def test_handlers_are_removed_on_unregister(monkeypatch):
    monkeypatch.setattr(interactive_mode, "killTrackedDetachedChildren", lambda: None)
    previousExcepthook = sys.excepthook
    previousThreadingExcepthook = threading.excepthook
    context = createHandlerContext()
    InteractiveMode.registerSignalHandlers(context)
    assert sys.excepthook is not previousExcepthook

    context.unregisterSignalHandlers()

    assert sys.excepthook is previousExcepthook
    assert threading.excepthook is previousThreadingExcepthook
    assert context.signalCleanupHandlers == []
