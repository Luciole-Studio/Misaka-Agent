"""Clipboard helpers for text copy operations."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from typing import Any, Protocol

from misaka.utils.clipboard_native import get_clipboard as _get_native_clipboard_backend

MAX_OSC52_ENCODED_LENGTH = 100_000
_DEFAULT_TIMEOUT_MS = 5_000


class _NativeClipboard(Protocol):
    async def set_text(self, text: str) -> None: ...


def is_remote_session(env: dict[str, str] | None = None) -> bool:
    resolved_env = env if env is not None else os.environ
    return bool(
        resolved_env.get("SSH_CONNECTION")
        or resolved_env.get("SSH_CLIENT")
        or resolved_env.get("MOSH_CONNECTION")
    )


def is_wayland_session(env: dict[str, str] | None = None) -> bool:
    resolved_env = env if env is not None else os.environ
    return bool(resolved_env.get("WAYLAND_DISPLAY")) or resolved_env.get("XDG_SESSION_TYPE") == "wayland"


def emit_osc52(text: str, *, writer: Any = None) -> bool:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_OSC52_ENCODED_LENGTH:
        return False
    resolved_writer = writer or sys.stdout
    resolved_writer.write(f"\x1b]52;c;{encoded}\x07")
    return True


async def copy_to_clipboard(text: str) -> None:
    copied = False
    platform_name = _platform()
    native = _get_native_clipboard()

    try:
        if native is not None and platform_name != "linux":
            await native.set_text(text)
            copied = True
    except Exception:  # noqa: BLE001, S110 - clipboard backends are best-effort; the fallback chain continues
        pass

    remote = is_remote_session()
    if copied and not remote:
        return

    if not copied:
        try:
            if platform_name == "darwin":
                _run_command(["pbcopy"], text)
                copied = True
            elif platform_name == "win32":
                _run_command(["clip"], text)
                copied = True
            else:
                copied = _copy_to_linux_clipboard(text)
        except Exception:  # noqa: BLE001, S110 - clipboard backends are best-effort; the fallback chain continues
            pass

    if remote or not copied:
        osc52_copied = emit_osc52(text)
        copied = copied or osc52_copied

    if not copied:
        raise RuntimeError("Failed to copy to clipboard")


def _copy_to_linux_clipboard(text: str) -> bool:
    if os.environ.get("TERMUX_VERSION"):
        try:
            _run_command(["termux-clipboard-set"], text)
            return True
        except Exception:  # noqa: BLE001, S110 - clipboard backends are best-effort; the fallback chain continues
            pass

    has_wayland_display = bool(os.environ.get("WAYLAND_DISPLAY"))
    has_x11_display = bool(os.environ.get("DISPLAY"))

    if is_wayland_session() and has_wayland_display:
        try:
            _spawn_background_command(["wl-copy"], text)
            return True
        except Exception:  # noqa: BLE001 - wl-copy failed: try X11
            if has_x11_display:
                _copy_to_x11_clipboard(text)
                return True
            return False

    if has_x11_display:
        _copy_to_x11_clipboard(text)
        return True

    return False


def _copy_to_x11_clipboard(text: str) -> None:
    try:
        _run_command(["xclip", "-selection", "clipboard"], text)
    except Exception:  # noqa: BLE001 - xclip failed: try xsel
        _run_command(["xsel", "--clipboard", "--input"], text)


def _run_command(command: list[str], text: str) -> None:
    subprocess.run(
        command,
        input=text,
        timeout=_DEFAULT_TIMEOUT_MS / 1000,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _spawn_background_command(command: list[str], text: str) -> None:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        if process.stdin is not None:
            process.stdin.write(text)
            process.stdin.close()
    except OSError:
        pass


def _platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in {"win32", "cygwin"}:
        return "win32"
    return sys.platform


def _get_native_clipboard() -> _NativeClipboard | None:
    return _get_native_clipboard_backend()


copyToClipboard = copy_to_clipboard

__all__ = ["copyToClipboard"]
