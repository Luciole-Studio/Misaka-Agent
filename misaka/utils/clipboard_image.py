"""Clipboard image helpers."""

from __future__ import annotations

import os
import subprocess
import tempfile
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
    except Exception:
        return None


def run_command(
    command: str,
    args: list[str],
    *,
    timeout_ms: int = DEFAULT_READ_TIMEOUT_MS,
    max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    env: dict[str, str] | None = None,
) -> _CommandResult:
    try:
        completed = subprocess.run(
            [command, *args],
            check=False,
            timeout=timeout_ms / 1000,
            capture_output=True,
            env=env,
        )
    except OSError:
        return _CommandResult(stdout=b"", ok=False)
    except subprocess.TimeoutExpired:
        return _CommandResult(stdout=b"", ok=False)

    if completed.returncode != 0:
        return _CommandResult(stdout=b"", ok=False)

    stdout = bytes(completed.stdout or b"")
    if len(stdout) > max_buffer_bytes:
        return _CommandResult(stdout=b"", ok=False)
    return _CommandResult(stdout=stdout, ok=True)


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
        return "microsoft" in _read_proc_version().lower() or "wsl" in _read_proc_version().lower()
    except OSError:
        return False


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
            image = read_clipboard_image_via_wl_paste(env=env) or read_clipboard_image_via_xclip(env=env)

        if image is None and wsl:
            image = read_clipboard_image_via_powershell(env=env)

        if image is None and not wayland:
            image = await read_clipboard_image_via_native_clipboard()
    else:
        image = await read_clipboard_image_via_native_clipboard()

    if image is None:
        return None

    if not is_supported_image_mime_type(image.mimeType):
        png_bytes = convert_to_png(image.bytes)
        if png_bytes is None:
            return None
        return ClipboardImage(bytes=png_bytes, mimeType="image/png")

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
    "ClipboardImage",
    ]
