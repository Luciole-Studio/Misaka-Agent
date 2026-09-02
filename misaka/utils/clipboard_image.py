"""Clipboard image helpers."""

from __future__ import annotations

import asyncio
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from PIL import Image

from misaka.utils.clipboard_native import get_clipboard as _get_native_clipboard_backend
from misaka.utils.exif_orientation import apply_exif_orientation

SUPPORTED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

DEFAULT_LIST_TIMEOUT_MS = 1000
DEFAULT_READ_TIMEOUT_MS = 3000
DEFAULT_POWERSHELL_TIMEOUT_MS = 5000
DEFAULT_MAX_BUFFER_BYTES = 50 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _NativeClipboard(Protocol):
    def has_image(self) -> bool: ...

    async def get_image_binary(self) -> bytes | bytearray | list[int] | None: ...


class ClipboardImage:
    def __init__(self, *, bytes: bytes, mimeType: str) -> None:
        self.bytes = bytes
        self.mimeType = mimeType


class ReadClipboardImageOptions:
    def __init__(self, *, env: dict[str, str] | None = None, platform: str | None = None) -> None:
        self.env = env
        self.platform = platform


class _CommandResult:
    def __init__(self, *, stdout: bytes, ok: bool) -> None:
        self.stdout = stdout
        self.ok = ok


def is_wayland_session(env: dict[str, str] | None = None) -> bool:
    resolved_env = env or os.environ
    return bool(resolved_env.get("WAYLAND_DISPLAY")) or resolved_env.get("XDG_SESSION_TYPE") == "wayland"


def base_mime_type(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def extension_for_image_mime_type(mime_type: str) -> str | None:
    match base_mime_type(mime_type):
        case "image/png":
            return "png"
        case "image/jpeg":
            return "jpg"
        case "image/webp":
            return "webp"
        case "image/gif":
            return "gif"
        case _:
            return None


def select_preferred_image_mime_type(mime_types: list[str]) -> str | None:
    normalized = [{"raw": value.strip(), "base": base_mime_type(value)} for value in mime_types if value.strip()]
    for preferred in SUPPORTED_IMAGE_MIME_TYPES:
        match = next((item for item in normalized if item["base"] == preferred), None)
        if match is not None:
            return str(match["raw"])
    any_image = next((item for item in normalized if str(item["base"]).startswith("image/")), None)
    return str(any_image["raw"]) if any_image is not None else None


def is_supported_image_mime_type(mime_type: str) -> bool:
    return base_mime_type(mime_type) in SUPPORTED_IMAGE_MIME_TYPES


def convert_to_png(image_bytes: bytes) -> bytes | None:
    try:
        from io import BytesIO

        raw_image = Image.open(BytesIO(image_bytes))
        raw_image.load()
        normalized = apply_exif_orientation(raw_image, image_bytes)
        try:
            output = BytesIO()
            normalized.save(output, format="PNG")
            return output.getvalue()
        finally:
            if normalized is not raw_image:
                normalized.close()
            raw_image.close()
    except Exception:  # noqa: BLE001 - any decode failure means no image
        return None


def run_command(
    command: str,
    args: list[str],
    *,
    timeout_ms: int = DEFAULT_READ_TIMEOUT_MS,
    max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    env: dict[str, str] | None = None,
) -> _CommandResult:
    """Run one clipboard helper and return its stdout, or ``ok=False``.

    ``max_buffer_bytes`` is enforced while reading, the way Node's ``execFile({maxBuffer})``
    that this mirrors does: a `subprocess.run(capture_output=True)` first read the whole of
    stdout into memory and only then compared the length, so a 1 GB image on the clipboard
    was copied into this process in full before being thrown away. Over the limit -- or past
    the deadline -- the child is killed and nothing is returned.

    Every caller is the Linux/Wayland/WSL command cascade below (wl-paste, xclip, wslpath,
    powershell.exe under WSL), so the poll-a-pipe loop only ever runs on POSIX.
    """
    try:
        proc = subprocess.Popen(
            [command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # never a pipe: nothing reads it, and a full one deadlocks
            env=env,
        )
    except OSError:
        return _CommandResult(stdout=b"", ok=False)

    deadline = time.monotonic() + timeout_ms / 1000
    chunks: list[bytes] = []
    total = 0
    failed = False
    stdout_pipe = proc.stdout
    assert stdout_pipe is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stdout_pipe, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    failed = True                       # timed out mid-read
                    break
                chunk = os.read(stdout_pipe.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    break                               # EOF: the child closed stdout
                total += len(chunk)
                if total > max_buffer_bytes:
                    failed = True
                    break
                chunks.append(chunk)
    except (OSError, ValueError):
        # ValueError is how a selector rejects a file object it cannot poll (a pipe on
        # Windows, which no caller here reaches): an unreadable child is a failed read,
        # not an exception out of a clipboard probe.
        failed = True
    finally:
        if failed:
            proc.kill()
        stdout_pipe.close()
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 1.0))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            failed = True

    if failed or proc.returncode != 0:
        return _CommandResult(stdout=b"", ok=False)
    return _CommandResult(stdout=b"".join(chunks), ok=True)


def read_clipboard_image_via_wl_paste(*, env: dict[str, str] | None = None) -> ClipboardImage | None:
    listed = run_command("wl-paste", ["--list-types"], timeout_ms=DEFAULT_LIST_TIMEOUT_MS, env=env)
    if not listed.ok:
        return None

    mime_types = [line.strip() for line in listed.stdout.decode("utf-8", errors="ignore").splitlines() if line.strip()]
    selected_type = select_preferred_image_mime_type(mime_types)
    if selected_type is None:
        return None

    data = run_command("wl-paste", ["--type", selected_type, "--no-newline"], env=env)
    if not data.ok or not data.stdout:
        return None
    return ClipboardImage(bytes=data.stdout, mimeType=base_mime_type(selected_type))


def _read_proc_version() -> str:
    return Path("/proc/version").read_text(encoding="utf-8")


def is_wsl(env: dict[str, str] | None = None) -> bool:
    resolved_env = env or os.environ
    if resolved_env.get("WSL_DISTRO_NAME") or resolved_env.get("WSLENV"):
        return True
    try:
        version = _read_proc_version().lower()   # one read, not one per keyword
    except OSError:
        return False
    return "microsoft" in version or "wsl" in version


def read_clipboard_image_via_powershell(*, env: dict[str, str] | None = None) -> ClipboardImage | None:
    tmp_file = str(Path(tempfile.gettempdir()) / f"harn-wsl-clip-{uuid4()}.png")
    try:
        win_path_result = run_command("wslpath", ["-w", tmp_file], timeout_ms=DEFAULT_LIST_TIMEOUT_MS, env=env)
        if not win_path_result.ok:
            return None

        win_path = win_path_result.stdout.decode("utf-8", errors="ignore").strip()
        if not win_path:
            return None

        quoted_win_path = win_path.replace("'", "''")
        script = "; ".join(
            [
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -AssemblyName System.Drawing",
                f"$path = '{quoted_win_path}'",
                "$img = [System.Windows.Forms.Clipboard]::GetImage()",
                (
                    "if ($img) { $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); "
                    "Write-Output 'ok' } else { Write-Output 'empty' }"
                ),
            ]
        )
        result = run_command(
            "powershell.exe",
            ["-NoProfile", "-Command", script],
            timeout_ms=DEFAULT_POWERSHELL_TIMEOUT_MS,
            env=env,
        )
        if not result.ok or result.stdout.decode("utf-8", errors="ignore").strip() != "ok":
            return None

        bytes_value = Path(tmp_file).read_bytes()
        if not bytes_value:
            return None
        return ClipboardImage(bytes=bytes_value, mimeType="image/png")
    except OSError:
        return None
    finally:
        try:
            Path(tmp_file).unlink()
        except OSError:
            pass


def read_clipboard_image_via_xclip(*, env: dict[str, str] | None = None) -> ClipboardImage | None:
    targets = run_command(
        "xclip",
        ["-selection", "clipboard", "-t", "TARGETS", "-o"],
        timeout_ms=DEFAULT_LIST_TIMEOUT_MS,
        env=env,
    )
    candidate_types: list[str] = []
    if targets.ok:
        candidate_types = [
            line.strip()
            for line in targets.stdout.decode("utf-8", errors="ignore").splitlines()
            if line.strip()
        ]

    preferred = select_preferred_image_mime_type(candidate_types) if candidate_types else None
    try_types = [preferred, *SUPPORTED_IMAGE_MIME_TYPES] if preferred is not None else list(SUPPORTED_IMAGE_MIME_TYPES)
    seen: set[str] = set()
    for mime_type in try_types:
        if mime_type in seen:
            continue
        seen.add(mime_type)
        data = run_command("xclip", ["-selection", "clipboard", "-t", mime_type, "-o"], env=env)
        if data.ok and data.stdout:
            return ClipboardImage(bytes=data.stdout, mimeType=base_mime_type(mime_type))
    return None


async def read_clipboard_image_via_native_clipboard() -> ClipboardImage | None:
    clipboard = _get_native_clipboard()
    if clipboard is None or not clipboard.has_image():
        return None

    image_data = await clipboard.get_image_binary()
    if not image_data:
        return None

    if isinstance(image_data, bytes):
        bytes_value = image_data
    elif isinstance(image_data, bytearray):
        bytes_value = bytes(image_data)
    else:
        bytes_value = bytes(image_data)
    return ClipboardImage(bytes=bytes_value, mimeType="image/png")


async def read_clipboard_image(
    options: ReadClipboardImageOptions | dict[str, Any] | None = None,
) -> ClipboardImage | None:
    resolved_options = _resolve_options(options)
    env = resolved_options.env or dict(os.environ)
    platform = resolved_options.platform or _platform()

    if env.get("TERMUX_VERSION"):
        return None

    image: ClipboardImage | None = None

    if platform == "linux":
        wsl = is_wsl(env)
        wayland = is_wayland_session(env)

        if wayland or wsl:
            image = await asyncio.to_thread(_read_via_external_commands, env=env, wsl=wsl)

        if image is None and not wayland:
            image = await read_clipboard_image_via_native_clipboard()
    else:
        image = await read_clipboard_image_via_native_clipboard()

    if image is None:
        return None

    if not is_supported_image_mime_type(image.mimeType):
        # A decode plus a PNG re-encode of up to DEFAULT_MAX_BUFFER_BYTES, in Python:
        # off the loop for the same reason the cascade above is.
        png_bytes = await asyncio.to_thread(convert_to_png, image.bytes)
        if png_bytes is None:
            return None
        return ClipboardImage(bytes=png_bytes, mimeType="image/png")

    return image


def _read_via_external_commands(
    *, env: dict[str, str] | None, wsl: bool
) -> ClipboardImage | None:
    """The wl-paste/xclip/powershell cascade, as one call off the event loop.

    Three helpers rather than one because each may come back empty, and every step is a
    ``subprocess.run`` with its own timeout: 1s + 3s to list and read through wl-paste,
    the same again through xclip once per candidate MIME type, then 1s + 5s for wslpath
    and powershell. A WSL box with nothing image-shaped on the clipboard can spend the
    better part of half a minute in here, and pasting is a keystroke in the TUI -- on the
    loop it is the whole interface that stops answering, not just the paste.

    Reached only when the caller found Wayland or WSL, which is also the only way the
    powershell step below is reachable, so the sequence matches the cascade it replaced.
    """
    image = read_clipboard_image_via_wl_paste(env=env) or read_clipboard_image_via_xclip(env=env)
    if image is None and wsl:
        image = read_clipboard_image_via_powershell(env=env)
    return image


def _resolve_options(options: ReadClipboardImageOptions | dict[str, Any] | None) -> ReadClipboardImageOptions:
    if isinstance(options, ReadClipboardImageOptions):
        return options
    if isinstance(options, dict):
        return ReadClipboardImageOptions(
            env=options.get("env"),
            platform=options.get("platform"),
        )
    return ReadClipboardImageOptions()


def _platform() -> str:
    import sys

    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in {"win32", "cygwin"}:
        return "win32"
    return sys.platform


def _get_native_clipboard() -> _NativeClipboard | None:
    return _get_native_clipboard_backend()


__all__ = [
    "DEFAULT_LIST_TIMEOUT_MS",
    "DEFAULT_MAX_BUFFER_BYTES",
    "DEFAULT_POWERSHELL_TIMEOUT_MS",
    "DEFAULT_READ_TIMEOUT_MS",
    "SUPPORTED_IMAGE_MIME_TYPES",
    "ClipboardImage",
    "ReadClipboardImageOptions",
    "base_mime_type",
    "convert_to_png",
    "extension_for_image_mime_type",
    "is_supported_image_mime_type",
    "is_wayland_session",
    "is_wsl",
    "read_clipboard_image",
    "read_clipboard_image_via_native_clipboard",
    "read_clipboard_image_via_powershell",
    "read_clipboard_image_via_wl_paste",
    "read_clipboard_image_via_xclip",
    "run_command",
    "select_preferred_image_mime_type",
]
