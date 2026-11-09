"""Translated from the ``copyToClipboard`` cases of pi's ``coding-agent/test/clipboard.test.ts`` (v1.3.0)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.ui.tui import native_platform
from misaka.utils import clipboard


class _Stdout:
    def __init__(self, writes: list[str]) -> None:
        self._writes = writes

    def write(self, chunk: str) -> int:
        if chunk.startswith("\x1b]52;c;"):
            self._writes.append(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


@pytest.fixture
def mocks(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "MOSH_CONNECTION", "WAYLAND_DISPLAY", "DISPLAY", "TERMUX_VERSION",
                 "WT_SESSION", "WSL_DISTRO_NAME", "WSLENV", "MISAKA_CLIPBOARD_OSC52", "PI_CLIPBOARD_OSC52"):
        monkeypatch.delenv(name, raising=False)
    state = SimpleNamespace(
        platform="darwin",
        clipboard=SimpleNamespace(getText=AsyncMock(return_value=None), setText=AsyncMock(return_value=None)),
        command=AsyncMock(return_value=b""),
        osc52Writes=[],
    )
    state.getNativeClipboard = Mock(side_effect=lambda: state.clipboard)
    monkeypatch.setattr(clipboard, "_platform", lambda: state.platform)
    monkeypatch.setattr(native_platform, "getNativeClipboard", state.getNativeClipboard)
    monkeypatch.setattr(clipboard, "runClipboardCommand", state.command)
    # WSL is what the environment says, not what the machine running the tests is.
    monkeypatch.setattr(clipboard, "isWSL", lambda env: bool(env.get("WSL_DISTRO_NAME") or env.get("WSLENV")))
    # pytest swaps `sys.stdout` between a fixture and its test, so the module's `sys` is replaced instead.
    monkeypatch.setattr(clipboard, "sys", SimpleNamespace(stdout=_Stdout(state.osc52Writes), platform=sys.platform))
    return state


def _names(mocks) -> list[str]:
    return [call.args[0] for call in mocks.command.await_args_list]


async def test_local_native_success_skips_osc52_and_commands(mocks):
    await clipboard.copyToClipboard("hello")
    mocks.clipboard.setText.assert_awaited_once_with("hello")
    assert mocks.osc52Writes == []
    mocks.command.assert_not_called()


async def test_linux_skips_the_native_writer(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("DISPLAY", ":0")
    await clipboard.copyToClipboard("hello")
    mocks.getNativeClipboard.assert_not_called()
    mocks.command.assert_any_await("xclip", ["-selection", "clipboard"], {"input": "hello", "timeoutMs": 5000})


async def test_waits_for_the_native_write_before_emitting_remote_osc52(mocks, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "client server")
    written: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    mocks.clipboard.setText = Mock(return_value=written)
    copy = asyncio.ensure_future(clipboard.copyToClipboard("hello"))
    await asyncio.sleep(0)
    assert mocks.osc52Writes == []
    written.set_result(None)
    await copy
    assert len(mocks.osc52Writes) == 1
    mocks.command.assert_not_called()


@pytest.mark.parametrize(("platform", "display"), [("darwin", None), ("win32", None), ("linux", ":99")])
async def test_misaka_clipboard_osc52_emits_osc52_after_a_successful_copy(mocks, monkeypatch, platform, display):
    # Regression test for #10826, #10835, #10114: remote clients without SSH markers.
    # MISAKA fork: the variable is `MISAKA_CLIPBOARD_OSC52` (pi: `PI_CLIPBOARD_OSC52`).
    mocks.platform = platform
    if display:
        monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("MISAKA_CLIPBOARD_OSC52", "1")
    await clipboard.copyToClipboard("hello")
    if platform == "linux":
        mocks.command.assert_any_await("xclip", ["-selection", "clipboard"], {"input": "hello", "timeoutMs": 5000})
    else:
        mocks.clipboard.setText.assert_awaited_once_with("hello")
    assert mocks.osc52Writes == ["\x1b]52;c;aGVsbG8=\x07"]


async def test_pi_clipboard_osc52_is_not_read(mocks, monkeypatch):
    monkeypatch.setenv("PI_CLIPBOARD_OSC52", "1")
    await clipboard.copyToClipboard("hello")
    assert mocks.osc52Writes == []


async def test_a_rejected_native_write_falls_back_to_pbcopy(mocks):
    mocks.clipboard.setText.side_effect = RuntimeError("native failed")
    await clipboard.copyToClipboard("hello")
    mocks.command.assert_awaited_with("pbcopy", [], {"input": "hello", "timeoutMs": 5000})
    assert mocks.osc52Writes == []


async def test_a_read_only_native_clipboard_uses_the_command_writer(mocks):
    mocks.clipboard = SimpleNamespace(getText=mocks.clipboard.getText)
    await clipboard.copyToClipboard("hello")
    assert mocks.command.await_count == 1


async def test_tries_xclip_and_xsel_after_wl_copy_fails(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    mocks.command.side_effect = lambda name, *_: b"" if name == "xsel" else None
    await clipboard.copyToClipboard("hello")
    assert _names(mocks) == ["wl-copy", "xclip", "xsel"]
    assert mocks.osc52Writes == []


async def test_local_linux_failure_does_not_report_an_unverified_osc52_write_as_success(mocks, monkeypatch):
    # Regression test for #9618.
    mocks.platform = "linux"
    monkeypatch.setenv("DISPLAY", ":0")
    mocks.command.return_value = None
    with pytest.raises(RuntimeError, match=r"Clipboard unavailable: install `xclip` or `xsel`, or check X11 access"):
        await clipboard.copyToClipboard("hello")
    assert _names(mocks) == ["xclip", "xsel"]
    assert mocks.osc52Writes == []


async def test_display_less_linux_falls_back_to_osc52(mocks):
    # Regression test for #9688: containers without X11/Wayland access.
    mocks.platform = "linux"
    await clipboard.copyToClipboard("hello")
    mocks.command.assert_not_called()
    assert len(mocks.osc52Writes) == 1


async def test_wsl_without_a_display_writes_the_windows_clipboard_through_powershell(mocks, monkeypatch):
    # Regression test for #9688: WSL with WSLg disabled.
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    written = {}

    async def command(name, args, options=None):
        if name != "wslpath":
            return b""
        written["text"] = Path(args[1]).read_text(encoding="utf-8")
        return b"\\\\wsl.localhost\\Ubuntu\\tmp\\clip.txt\n"

    mocks.command.side_effect = command
    await clipboard.copyToClipboard("héllo")
    assert _names(mocks) == ["wslpath", "powershell.exe"]
    assert written["text"] == "héllo"
    wslpathArgs = mocks.command.await_args_list[0].args[1]
    assert not os.path.exists(wslpathArgs[1])
    powershellArgs = mocks.command.await_args_list[1].args[1]
    assert "Set-Clipboard" in powershellArgs[2]
    assert "'\\\\wsl.localhost\\Ubuntu\\tmp\\clip.txt'" in powershellArgs[2]
    assert mocks.osc52Writes == []


async def test_wsl_falls_back_to_osc52_when_windows_interop_is_unavailable(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    mocks.command.return_value = None
    await clipboard.copyToClipboard("hello")
    assert _names(mocks) == ["wslpath"]
    assert len(mocks.osc52Writes) == 1


async def test_wsl_in_windows_terminal_prefers_osc52_over_powershell(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("WT_SESSION", "session")
    await clipboard.copyToClipboard("hello")
    mocks.command.assert_not_called()
    assert len(mocks.osc52Writes) == 1


async def test_wsl_in_windows_terminal_emits_osc52_once_in_a_remote_session(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("WT_SESSION", "session")
    monkeypatch.setenv("SSH_CONNECTION", "client server")
    await clipboard.copyToClipboard("hello")
    mocks.command.assert_not_called()
    assert len(mocks.osc52Writes) == 1


async def test_wsl_in_windows_terminal_uses_powershell_for_oversized_osc52_payloads(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("WT_SESSION", "session")
    mocks.command.side_effect = lambda name, *_: b"C:\\clip.txt" if name == "wslpath" else b""
    await clipboard.copyToClipboard("x" * 80_000)
    assert _names(mocks) == ["wslpath", "powershell.exe"]
    assert mocks.osc52Writes == []


async def test_wsl_with_a_display_prefers_the_linux_clipboard_tools(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    await clipboard.copyToClipboard("hello")
    assert _names(mocks) == ["wl-copy"]
    assert mocks.osc52Writes == []


async def test_reports_the_wayland_clipboard_tool_instead_of_the_x11_fallback(mocks, monkeypatch):
    mocks.platform = "linux"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    mocks.command.return_value = None
    with pytest.raises(RuntimeError, match=r"Clipboard unavailable: install `wl-clipboard` \(`wl-copy`\) or check Wayland access"):
        await clipboard.copyToClipboard("hello")
    assert _names(mocks) == ["wl-copy", "xclip", "xsel"]


async def test_termux_on_android_writes_through_termux_clipboard_set(mocks, monkeypatch):
    mocks.platform = "android"
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    mocks.clipboard = None
    await clipboard.copyToClipboard("hello")
    mocks.command.assert_awaited_once_with("termux-clipboard-set", [], {"input": "hello", "timeoutMs": 5000})
    assert mocks.osc52Writes == []


async def test_reports_the_termux_api_requirement_on_android(mocks, monkeypatch):
    # Regression test for #10391: Termux reports platform "android".
    mocks.platform = "android"
    monkeypatch.setenv("TERMUX_VERSION", "0.119")
    mocks.clipboard = None
    mocks.command.return_value = None
    with pytest.raises(RuntimeError, match="Clipboard unavailable: install the Termux:API app and `termux-api` package"):
        await clipboard.copyToClipboard("hello")


async def test_uses_osc52_when_native_and_command_writes_fail_in_a_remote_session(mocks, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "client server")
    mocks.clipboard.setText.side_effect = RuntimeError("native failed")
    mocks.command.return_value = None
    await clipboard.copyToClipboard("hello")
    assert len(mocks.osc52Writes) == 1


async def test_does_not_emit_oversized_osc52_payloads(mocks, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "client server")
    mocks.clipboard.setText.side_effect = RuntimeError("native failed")
    mocks.command.return_value = None
    with pytest.raises(RuntimeError, match="Clipboard unavailable: text exceeds the OSC 52 size limit"):
        await clipboard.copyToClipboard("x" * 80_000)
    assert mocks.osc52Writes == []
