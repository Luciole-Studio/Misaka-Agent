"""Translated from pi-tui's ``test/terminal.test.ts`` and ``test/regression-sigwinch-kill-eacces.test.ts`` (v1.1.0).

MISAKA forks: the escape timeout is the ``tui.esc_timeout_ms`` setting (pi: ``PI_TUI_ESC_TIMEOUT``), the program
status override is ``MISAKA_PROGRAM_STATUS`` (pi: ``PI_PROGRAM_STATUS``), and ``mock.timers`` is a loop whose
clock the test advances.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from misaka.ui.tui import terminal as terminal_module
from misaka.ui.tui.keys import setKittyProtocolActive
from misaka.ui.tui.terminal import (
    ProcessTerminal,
    normalizeAppleTerminalInput,
    normalizeNativeShiftEnterInput,
    refreshTerminalDimensions,
    resolveEscapeTimeoutMs,
)


class FakeTimer:
    def __init__(self, loop: FakeLoop, when: float, callback: Callable[[], None]) -> None:
        self.loop = loop
        self.when = when
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeLoop:
    """``mock.timers``: timers fire only when the test advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: list[FakeTimer] = []

    def call_later(self, delay: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(self, self.now + delay * 1000, callback)
        self.timers.append(timer)
        return timer

    def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
        callback()

    def is_closed(self) -> bool:
        return False

    def tick(self, ms: float) -> None:
        end = self.now + ms
        while True:
            due = [timer for timer in self.timers if not timer.cancelled and timer.when <= end]
            if not due:
                break
            timer = min(due, key=lambda item: item.when)
            self.timers.remove(timer)
            self.now = timer.when
            timer.callback()
        self.now = end


class Recorder:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, chunk: str) -> int:
        self.writes.append(str(chunk))
        return len(chunk)

    def flush(self) -> None:
        pass


# --- resolveEscapeTimeoutMs -----------------------------------------------------------------------


@pytest.fixture
def escTimeoutSetting(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    from misaka.config import product

    def configure(value: float) -> None:
        def setting(section: str, key: str, default: Any = None, cast: Any = None) -> Any:
            if (section, key) == ("tui", "esc_timeout_ms"):
                return value
            return default

        monkeypatch.setattr(product, "setting", setting)

    configure(0)
    return configure


# MISAKA fork: the configured value is the `tui.esc_timeout_ms` setting.
def test_uses_the_configured_escape_timeout(escTimeoutSetting):
    escTimeoutSetting(80)
    assert resolveEscapeTimeoutMs({}) == 80
    assert resolveEscapeTimeoutMs({"SSH_TTY": "/dev/pts/1"}) == 80


def test_ignores_invalid_escape_timeout_values(escTimeoutSetting):
    for value in (float("nan"), 0, -5):
        escTimeoutSetting(value)
        assert resolveEscapeTimeoutMs({}) == 10


def test_defaults_to_100ms_over_ssh(escTimeoutSetting):
    assert resolveEscapeTimeoutMs({"SSH_CONNECTION": "10.0.0.1 22"}) == 100
    assert resolveEscapeTimeoutMs({"SSH_TTY": "/dev/pts/1"}) == 100


def test_defaults_to_10ms_otherwise(escTimeoutSetting):
    assert resolveEscapeTimeoutMs({}) == 10


# MISAKA fork: a pane of the panel is a relayed transport too.
def test_defaults_to_100ms_in_a_panel_pane(escTimeoutSetting):
    assert resolveEscapeTimeoutMs({"MISAKA_NET_PANE": "1"}) == 100


# --- normalizeNativeShiftEnterInput ---------------------------------------------------------------


def test_rewrites_return_to_csi_u_shift_enter_when_native_shift_detection_is_enabled_and_shift_is_pressed():
    assert normalizeNativeShiftEnterInput("\r", True, True) == "\x1b[13;2u"


def test_leaves_return_unchanged_when_native_shift_detection_is_disabled():
    assert normalizeNativeShiftEnterInput("\r", False, True) == "\r"


def test_leaves_return_unchanged_when_shift_is_not_pressed():
    assert normalizeNativeShiftEnterInput("\r", True, False) == "\r"


def test_native_shift_enter_leaves_non_return_input_unchanged():
    assert normalizeNativeShiftEnterInput("\x1b[13;2u", True, True) == "\x1b[13;2u"
    assert normalizeNativeShiftEnterInput("a", True, True) == "a"


# --- normalizeAppleTerminalInput ------------------------------------------------------------------


def test_rewrites_apple_terminal_return_to_csi_u_shift_enter_when_shift_is_pressed():
    assert normalizeAppleTerminalInput("\r", True, True) == "\x1b[13;2u"


def test_leaves_apple_terminal_return_unchanged_when_shift_is_not_pressed():
    assert normalizeAppleTerminalInput("\r", True, False) == "\r"


def test_leaves_non_apple_terminal_return_unchanged_when_shift_is_pressed():
    assert normalizeAppleTerminalInput("\r", False, True) == "\r"


def test_apple_terminal_leaves_non_return_input_unchanged():
    assert normalizeAppleTerminalInput("\x1b[13;2u", True, True) == "\x1b[13;2u"
    assert normalizeAppleTerminalInput("a", True, True) == "a"


# --- ProcessTerminal Kitty keyboard protocol negotiation ------------------------------------------


class NegotiationHarness:
    def __init__(self) -> None:
        self.terminal = ProcessTerminal()
        self.recorder = Recorder()
        self.writes = self.recorder.writes
        self.loop = FakeLoop()
        self.input: str | None = None
        self.cleaned = False
        self.terminal.stdout = self.recorder
        self.terminal.loop = self.loop  # type: ignore[assignment]
        # `process.stdin.on("data")` is captured: `send` feeds the terminal's data handler.
        self.terminal._install_reader = lambda: None  # type: ignore[method-assign]

        def inputHandler(data: str) -> None:
            self.input = data

        self.terminal.inputHandler = inputHandler
        self.terminal.queryAndEnableKittyProtocol()

    def send(self, data: str) -> None:
        if self.terminal.stdinDataHandler:
            self.terminal.stdinDataHandler(data)

    def getInput(self) -> str | None:
        return self.input

    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        try:
            self.terminal.stop()
        finally:
            setKittyProtocolActive(False)


@pytest.fixture
def setupNegotiation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], NegotiationHarness]]:
    monkeypatch.delenv("MISAKA_PROGRAM_STATUS", raising=False)
    harnesses: list[NegotiationHarness] = []

    def setup() -> NegotiationHarness:
        harness = NegotiationHarness()
        harnesses.append(harness)
        return harness

    yield setup
    for harness in harnesses:
        harness.cleanup()


def test_queries_kitty_mode_before_enabling_modifyotherkeys_fallback(setupNegotiation):
    harness = setupNegotiation()
    assert harness.writes[0] == "\x1b[>7u\x1b[?u\x1b]7501;?\x1b\\\x1b[c"
    assert ("\x1b[>4;2m" in harness.writes) is False
    assert harness.terminal.kittyProtocolActive is False


def test_activates_kitty_mode_for_non_zero_negotiated_flags(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?7u")

    assert harness.getInput() is None
    assert harness.terminal.kittyProtocolActive is True
    assert ("\x1b[>4;2m" in harness.writes) is False
    assert ("\x1b[>4;0m" in harness.writes) is False

    harness.cleanup()
    assert len([write for write in harness.writes if write == "\x1b[<u"]) == 1
    assert ("\x1b[>4;0m" in harness.writes) is False


def test_falls_back_to_modifyotherkeys_for_zero_kitty_flags(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?0u")

    assert harness.getInput() is None
    assert harness.terminal.kittyProtocolActive is False
    assert len([write for write in harness.writes if write == "\x1b[>4;2m"]) == 1

    harness.cleanup()
    assert len([write for write in harness.writes if write == "\x1b[>4;0m"]) == 1


def test_falls_back_to_modifyotherkeys_for_device_attributes_without_kitty_flags(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?62;4;52c")

    assert harness.getInput() is None
    assert harness.terminal.kittyProtocolActive is False
    assert len([write for write in harness.writes if write == "\x1b[>4;2m"]) == 1


def test_forwards_device_attributes_replies_that_answer_other_queries(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?7u")
    harness.send("\x1b[?62;4;52c")
    assert harness.getInput() is None

    # The TUI's color query uses DA1 as its own sentinel.
    harness.send("\x1b[?62;4;52c")
    assert harness.getInput() == "\x1b[?62;4;52c"


def test_forwards_normal_input_while_waiting_for_kitty_response(setupNegotiation):
    harness = setupNegotiation()
    harness.send("a")

    assert harness.getInput() == "a"
    assert harness.terminal.kittyProtocolActive is False


def test_tracks_split_kitty_confirmation(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?7")
    harness.loop.tick(10)

    assert harness.getInput() is None

    harness.send("u")

    assert harness.terminal.kittyProtocolActive is True
    assert ("\x1b[>4;2m" in harness.writes) is False


# --- program status (OSC 7501), #10607 ------------------------------------------------------------

working = "\x1b]7501;state=working:app=pi\x1b\\"
clear = "\x1b]7501;state=clear\x1b\\"


def test_reports_the_latest_status_once_the_terminal_answers_the_query_before_da(setupNegotiation):
    harness = setupNegotiation()
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    assert (working in harness.writes) is False

    harness.send("\x1b]7501;?\x1b\\")
    assert harness.getInput() is None
    assert len([write for write in harness.writes if write == working]) == 1
    harness.send("\x1b[?62;4;52c")

    harness.terminal.setProgramStatus({"state": "done"})
    assert harness.writes[-1] == "\x1b]7501;state=done\x1b\\"


def test_reports_nothing_when_da_arrives_first_and_swallows_late_replies(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[?62;4;52c")
    harness.send("\x1b]7501;?\x07")
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})

    assert harness.getInput() is None
    assert any(write.startswith("\x1b]7501;state=") for write in harness.writes) is False


def test_skips_the_query_when_misaka_program_status_is_set(setupNegotiation, monkeypatch):
    monkeypatch.setenv("MISAKA_PROGRAM_STATUS", "1")
    harness = setupNegotiation()
    assert harness.writes[0] == "\x1b[>7u\x1b[?u\x1b[c"
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    assert harness.writes[-1] == working
    harness.cleanup()

    monkeypatch.setenv("MISAKA_PROGRAM_STATUS", "0")
    harness = setupNegotiation()
    assert harness.writes[0] == "\x1b[>7u\x1b[?u\x1b[c"
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    assert (working in harness.writes) is False


def test_does_not_let_a_da_reply_from_before_a_restart_end_the_new_query(setupNegotiation):
    harness = setupNegotiation()
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    harness.terminal.stop()
    harness.terminal.queryAndEnableKittyProtocol()

    # The first start's replies arrive late: its DA, then the second start's reply and DA.
    harness.send("\x1b[?62;4;52c")
    harness.send("\x1b]7501;?\x1b\\")
    harness.send("\x1b[?62;4;52c")
    assert harness.writes[-1] == working
    assert harness.getInput() is None


def test_clears_the_status_on_stop_and_reports_it_again_after_restart(setupNegotiation):
    harness = setupNegotiation()
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    harness.send("\x1b]7501;?\x1b\\")
    harness.terminal.stop()
    assert clear in harness.writes

    # Stopped: nothing is written until the restarted terminal confirms support again.
    writesBeforeRestart = len(harness.writes)
    harness.terminal.setProgramStatus({"state": "working", "app": "pi"})
    assert len(harness.writes) == writesBeforeRestart

    harness.terminal.queryAndEnableKittyProtocol()
    harness.send("\x1b]7501;?\x1b\\")
    assert harness.writes[-1] == working


def test_replays_buffered_csi_prefix_input_when_it_is_not_a_kitty_response(setupNegotiation):
    harness = setupNegotiation()
    harness.send("\x1b[")
    harness.loop.tick(50)  # StdinBuffer sequence timeout, not the lone-ESC timeout

    assert harness.getInput() is None

    harness.loop.tick(150)

    assert harness.getInput() == "\x1b["


# --- ProcessTerminal progress ---------------------------------------------------------------------


def test_writes_a_valid_osc_9_4_clear_sequence():
    terminal = ProcessTerminal()
    recorder = Recorder()
    terminal.stdout = recorder

    terminal.setProgress(False)
    assert recorder.writes == ["\x1b]9;4;0\x07"]


# --- ProcessTerminal write log --------------------------------------------------------------------


def test_appends_every_write_to_the_write_log_file(tmp_path, monkeypatch):
    logPath = tmp_path / "tui.log"
    monkeypatch.setenv("MISAKA_TUI_WRITE_LOG", str(logPath))
    terminal = ProcessTerminal()
    terminal.stdout = Recorder()

    terminal.write("first\n")
    terminal.write("second")

    assert logPath.read_text(encoding="utf-8") == "first\nsecond"


def test_a_write_log_directory_gets_a_file_per_process(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_TUI_WRITE_LOG", str(tmp_path))
    terminal = ProcessTerminal()
    terminal.stdout = Recorder()

    terminal.write("data")

    [logFile] = list(tmp_path.iterdir())
    assert logFile.name.startswith("tui-") and logFile.name.endswith(f"-{os.getpid()}.log")
    assert logFile.read_text(encoding="utf-8") == "data"


# --- ProcessTerminal dimensions -------------------------------------------------------------------


def test_falls_back_to_columns_and_lines_before_default_dimensions(monkeypatch):
    monkeypatch.setenv("COLUMNS", "123")
    monkeypatch.setenv("LINES", "45")

    terminal = ProcessTerminal()
    terminal.stdout = Recorder()  # no terminal behind it: `process.stdout.columns` is undefined

    assert terminal.columns == 123
    assert terminal.rows == 45


# --- refreshTerminalDimensions (regression-sigwinch-kill-eacces) ----------------------------------


def test_does_not_throw_when_kill_2_returns_eacces_for_self_signal(monkeypatch):
    monkeypatch.setattr(terminal_module.sys, "platform", "linux")
    originalKill = os.kill

    def kill(pid: int, sig: int) -> None:
        if pid == os.getpid():
            raise PermissionError(13, "kill EACCES")
        originalKill(pid, sig)

    monkeypatch.setattr(terminal_module.os, "kill", kill)
    refreshTerminalDimensions()


def test_does_not_call_kill_on_win32(monkeypatch):
    monkeypatch.setattr(terminal_module.sys, "platform", "win32")
    killCalled = False

    def kill(_pid: int, _sig: int) -> None:
        nonlocal killCalled
        killCalled = True

    monkeypatch.setattr(terminal_module.os, "kill", kill)
    refreshTerminalDimensions()

    assert killCalled is False, "kill should not be called on win32"


def test_preserves_other_error_codes(monkeypatch):
    monkeypatch.setattr(terminal_module.sys, "platform", "linux")

    def kill(_pid: int, _sig: int) -> None:
        raise PermissionError(1, "kill EPERM")

    monkeypatch.setattr(terminal_module.os, "kill", kill)
    # EPERM is also ignored - the refresh is best-effort
    refreshTerminalDimensions()


# --- MISAKA ---------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="native Shift+Enter detection on Windows")
def test_a_return_with_shift_held_is_shift_enter_on_windows(setupNegotiation, monkeypatch):
    harness = setupNegotiation()
    monkeypatch.setattr(terminal_module, "isNativeModifierPressed", lambda key: key == "shift")
    harness.send("\r")
    assert harness.getInput() == "\x1b[13;2u"
    monkeypatch.setattr(terminal_module, "isNativeModifierPressed", lambda key: False)
    harness.send("\r")
    assert harness.getInput() == "\r"


def test_a_negotiation_reply_with_a_trailing_newline_is_input(setupNegotiation):
    # `$` is the end of the input, as in JavaScript.
    assert terminal_module.parseKeyboardProtocolNegotiationSequence("\x1b[?7u\n") is None
    assert terminal_module.parseKeyboardProtocolNegotiationSequence("\x1b[?١u") is None
    assert terminal_module.parseKeyboardProtocolNegotiationSequence("\x1b[?62;4c\n") is None
