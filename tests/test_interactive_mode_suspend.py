"""Translated from pi's ``coding-agent/test/interactive-mode-suspend.test.ts`` (v1.1.0).

MISAKA fork: Python keeps running while suspended, so there is no keep-alive interval to check; the SIGINT and SIGCONT
handlers are installed with ``signal.signal`` and the previous handlers are put back.
"""

from __future__ import annotations

import signal
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.modes.interactive import interactive_mode
from misaka.modes.interactive.interactive_mode import InteractiveMode

posixOnly = pytest.mark.skipif(sys.platform == "win32", reason="SIGTSTP and SIGCONT exist on POSIX only")


def fakeUi():
    return SimpleNamespace(start=Mock(), stop=Mock(), requestRender=Mock())


@pytest.fixture
def signals(monkeypatch):
    installed: dict[int, list] = {}
    killed: list = []

    def fakeSignal(signum, handler):
        installed.setdefault(signum, []).append(handler)

    monkeypatch.setattr(interactive_mode.signal, "signal", fakeSignal)
    monkeypatch.setattr(interactive_mode.signal, "getsignal", lambda signum: f"previous-{signum}")
    monkeypatch.setattr(interactive_mode.os, "kill", lambda pid, signum: killed.append((pid, signum)))
    monkeypatch.setattr(interactive_mode.loop_watchdog, "suspend", lambda: None)
    return SimpleNamespace(installed=installed, killed=killed)


def test_shows_a_status_message_and_skips_suspend_on_windows(monkeypatch, signals):
    ui = fakeUi()
    context = SimpleNamespace(ui=ui, showStatus=Mock())
    monkeypatch.setattr(interactive_mode.sys, "platform", "win32")

    InteractiveMode.handleCtrlZ(context)

    context.showStatus.assert_called_once_with("Suspend to background is not supported on Windows")
    ui.stop.assert_not_called()
    assert signals.installed == {}
    assert signals.killed == []


@posixOnly
def test_restores_the_tui_on_sigcont(signals):
    ui = fakeUi()
    context = SimpleNamespace(ui=ui)

    InteractiveMode.handleCtrlZ(context)

    sigintHandler = signals.installed[signal.SIGINT][0]
    sigcontHandler = signals.installed[signal.SIGCONT][0]
    ui.stop.assert_called_once_with()
    assert signals.killed == [(0, signal.SIGTSTP)]

    sigcontHandler(signal.SIGCONT, None)

    assert signals.installed[signal.SIGINT][-1] == f"previous-{signal.SIGINT}"
    assert signals.installed[signal.SIGCONT][-1] == f"previous-{signal.SIGCONT}"
    assert sigintHandler is not signals.installed[signal.SIGINT][-1]
    ui.start.assert_called_once_with()
    ui.requestRender.assert_called_once_with(True)


@posixOnly
def test_cleans_up_the_temporary_handlers_if_suspension_fails(monkeypatch, signals):
    ui = fakeUi()
    context = SimpleNamespace(ui=ui)
    suspendError = RuntimeError("suspend failed")

    def failKill(_pid, _signum):
        raise suspendError

    monkeypatch.setattr(interactive_mode.os, "kill", failKill)

    with pytest.raises(RuntimeError, match="suspend failed"):
        InteractiveMode.handleCtrlZ(context)

    ui.stop.assert_called_once_with()
    assert signals.installed[signal.SIGINT][-1] == f"previous-{signal.SIGINT}"
    assert signals.installed[signal.SIGCONT][-1] == f"previous-{signal.SIGCONT}"
    ui.start.assert_not_called()
    ui.requestRender.assert_not_called()
