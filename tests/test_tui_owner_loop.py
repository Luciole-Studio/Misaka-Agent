"""Render scheduling and terminal cleanup must share the owning event-loop thread."""
import asyncio
import io
import signal
import sys
import threading
from types import SimpleNamespace

import pytest

from misaka.ui.tui.terminal import ProcessTerminal
from misaka.ui.tui.tui import TUI


class Terminal(ProcessTerminal):
    """In-memory output; no raw mode, reader, subprocess or replacement signal handler."""
    def __init__(self):
        super().__init__()
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stop_threads = []

    @property
    def columns(self):
        return 93

    @property
    def rows(self):
        return 40

    def start(self, onInput, onResize):
        self.loop = asyncio.get_running_loop()
        self.inputHandler = onInput
        self.resizeHandler = onResize
        self._previousSigwinchHandler = signal.getsignal(signal.SIGWINCH)

    def stop(self):
        self.stop_threads.append(threading.get_ident())
        super().stop()


class Content:
    def __init__(self, loop):
        self.text = "Initial frame"
        self.loop = loop
        self.rendered = asyncio.Event()
        self.threads = []

    def render(self, _width):
        self.threads.append(threading.get_ident())
        self.loop.call_soon_threadsafe(self.rendered.set)
        return [self.text]


@pytest.mark.asyncio
async def test_normal_forced_and_worker_requests_render_on_owner():
    owner = threading.get_ident()
    terminal = Terminal()
    ui = TUI(terminal)
    content = Content(asyncio.get_running_loop())
    ui.addChild(content)
    try:
        ui.start()
        await asyncio.wait_for(content.rendered.wait(), 1)
        for force in (False, True):
            content.rendered.clear()
            content.text += " next"
            await asyncio.to_thread(ui.requestRender, force)
            await asyncio.wait_for(content.rendered.wait(), 1)
        assert content.threads and set(content.threads) == {owner}
        # Stop cancels a throttled frame, and late animation requests stay inert.
        ui.MIN_RENDER_INTERVAL_MS = 1000
        content.rendered.clear()
        ui.requestRender()
        await asyncio.sleep(0)
        ui.stop()
        before = len(content.threads)
        await asyncio.to_thread(ui.requestRender, True)
        await asyncio.sleep(0.04)
        assert len(content.threads) == before
        assert terminal.stop_threads == [owner]
    finally:
        if not ui.stopped:
            ui.stop()


@pytest.mark.asyncio
async def test_forced_render_preempts_throttled_frame():
    ui = TUI(Terminal())
    content = Content(asyncio.get_running_loop())
    ui.addChild(content)
    try:
        ui.start()
        await asyncio.wait_for(content.rendered.wait(), 1)
        ui.MIN_RENDER_INTERVAL_MS = 1000
        content.rendered.clear()
        ui.requestRender()
        await asyncio.sleep(0)
        ui.requestRender(True)
        await asyncio.wait_for(content.rendered.wait(), 0.3)
        assert ui.renderTimer is None
    finally:
        ui.stop()


@pytest.mark.asyncio
async def test_overflow_preserves_primary_error_and_cleans_up_on_owner(tmp_path, monkeypatch):
    from misaka.config import home

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    crashed = asyncio.Event()
    errors = []

    def record(error):
        errors.append((threading.get_ident(), error))
        loop.call_soon_threadsafe(crashed.set)

    monkeypatch.setattr(sys, "excepthook", lambda _type, error, _tb: record(error))
    monkeypatch.setattr(threading, "excepthook", lambda args: record(args.exc_value))
    terminal = Terminal()
    ui = TUI(terminal)
    content = Content(loop)
    ui.addChild(content)
    try:
        ui.start()
        await asyncio.wait_for(content.rendered.wait(), 1)
        content.text = "x" * 105
        ui.requestRender()
        await asyncio.wait_for(crashed.wait(), 1)
        assert len(errors) == 1
        thread, error = errors[0]
        assert thread == owner
        assert isinstance(error, RuntimeError) and "105 > 93" in str(error)
        assert "signal only works" not in str(error)
        assert terminal.stop_threads == [owner]
        assert home.path("crash_log").exists()
    finally:
        if not ui.stopped:
            ui.stop()


@pytest.mark.asyncio
async def test_background_crash_handler_returns_to_owner(monkeypatch):
    from misaka.ui.tui.interactive.interactive_mode import InteractiveMode

    owner = threading.get_ident()
    loop = asyncio.get_running_loop()
    handled = asyncio.Event()
    observed = []
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    mode = object.__new__(InteractiveMode)
    mode.signalCleanupHandlers = []

    def crash(error):
        observed.append((threading.get_ident(), error))
        loop.call_soon_threadsafe(handled.set)

    mode.uncaughtCrash = crash
    mode.registerSignalHandlers()
    try:
        error = RuntimeError("background fixture error")
        await asyncio.to_thread(threading.excepthook, SimpleNamespace(exc_value=error))
        await asyncio.wait_for(handled.wait(), 1)
        assert observed == [(owner, error)]
    finally:
        mode.unregisterSignalHandlers()
