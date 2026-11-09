"""Ported from pi's ``packages/coding-agent/test/clipboard-image.test.ts`` and
``clipboard-image-bmp-conversion.test.ts`` (v1.1.0).

``vi.mock`` of ``clipboard-command.ts`` and ``@earendil-works/pi-tui`` become monkeypatches of
``clipboard_image.runClipboardCommand`` and ``native_platform.getNativeClipboard``. MISAKA fork: ``isWSL`` reads
only the environment here, as on the Linux machines pi's suite runs on; under WSL it would also read /proc/version.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ui.tui import native_platform
from misaka.utils import clipboard_image
from misaka.utils.clipboard_image import ClipboardImage, readClipboardImage

png = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52])


def commandResult(stdout: bytes, status: int = 0) -> bytes | None:
    return stdout if status == 0 else None


class _Mock:
    """``vi.fn``: records calls; answers with ``implementation`` (async) or ``value``."""

    def __init__(self, value: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.value = value
        self.implementation: Any = None

    async def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.implementation is not None:
            return await self.implementation(*args)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class _Sync:
    def __init__(self, value: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.value = value

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.value


@pytest.fixture
def mocks(monkeypatch):
    command = _Mock(commandResult(b"", 1))
    getImage = _Mock(png)
    getNativeClipboard = _Sync(SimpleNamespace(getText=None, getImage=getImage))

    async def run(command_name: str, args: list[str], options: Any = None) -> bytes | None:
        return await command(command_name, args, *([options] if options is not None else []))

    monkeypatch.setattr(clipboard_image, "runClipboardCommand", run)
    monkeypatch.setattr(native_platform, "getNativeClipboard", getNativeClipboard)
    monkeypatch.setattr(clipboard_image, "isWSL", lambda env=None: bool((env or {}).get("WSL_DISTRO_NAME") or (env or {}).get("WSLENV")))
    return SimpleNamespace(command=command, getImage=getImage, getNativeClipboard=getNativeClipboard)


@pytest.mark.parametrize(
    ("backend", "command", "env"),
    [("wayland", "wl-paste", {"WAYLAND_DISPLAY": "1", "DISPLAY": ":0"}), ("x11", "xclip", {"DISPLAY": ":0"})],
)
@pytest.mark.parametrize("present", [True, False])
async def test_command_image_present_stops_fallback(mocks, backend, command, env, present):
    async def implementation(name, args, *_options):
        assert name == command
        listing = "--list-types" in args or "TARGETS" in args
        return commandResult((b"text/plain\nimage/png\n" if present else b"text/plain\n") if listing else png)

    mocks.command.implementation = implementation
    assert await readClipboardImage({"platform": "linux", "env": env}) == (
        ClipboardImage(bytes=png, mimeType="image/png") if present else None
    )
    assert len(mocks.command.calls) == (2 if present else 1)
    assert mocks.getNativeClipboard.calls == []


async def test_x11_does_not_probe_image_types_when_targets_fails(mocks):
    # Regression test for #9786.
    mocks.getImage.value = None

    async def implementation(_command, args, *_options):
        return None if "TARGETS" in args else b"hello"

    mocks.command.implementation = implementation
    assert await readClipboardImage({"platform": "linux", "env": {"DISPLAY": ":0"}}) is None
    assert mocks.command.calls == [("xclip", ["-selection", "clipboard", "-t", "TARGETS", "-o"], {"timeoutMs": 1000})]
    assert len(mocks.getImage.calls) == 1


async def test_x11_does_not_probe_unadvertised_image_types(mocks):
    mocks.getImage.value = None

    async def implementation(_command, args, *_options):
        if "TARGETS" in args:
            return b"image/png\n"
        if "image/png" in args:
            return None
        return b"hello"

    mocks.command.implementation = implementation
    assert await readClipboardImage({"platform": "linux", "env": {"DISPLAY": ":0"}}) is None
    assert [call[1][3] for call in mocks.command.calls] == ["TARGETS", "image/png"]
    assert len(mocks.getImage.calls) == 1


@pytest.mark.parametrize("bytes_", [png, None, b""])
async def test_native_x11_result_stops_fallback(mocks, bytes_):
    mocks.getImage.value = bytes_
    assert await readClipboardImage({"platform": "linux", "env": {"DISPLAY": ":0"}}) == (
        ClipboardImage(bytes=bytes_, mimeType="image/png") if bytes_ else None
    )
    assert mocks.getNativeClipboard.calls == [()]
    assert len(mocks.getImage.calls) == 1
    assert [call[0] for call in mocks.command.calls] == ["xclip"]


@pytest.mark.parametrize("failure", ["missing module", "unavailable display"])
async def test_wayland_falls_back_to_x11_after(mocks, failure):
    if failure == "missing module":
        mocks.getNativeClipboard.value = None
    else:
        mocks.getImage.value = None

    async def implementation(command, args, *_options):
        if command == "wl-paste":
            return commandResult(b"", 1)
        return commandResult(b"image/png\n" if "TARGETS" in args else png)

    mocks.command.implementation = implementation
    assert await readClipboardImage({"platform": "linux", "env": {"WAYLAND_DISPLAY": "1"}}) == ClipboardImage(
        bytes=png, mimeType="image/png"
    )
    assert mocks.getNativeClipboard.calls == []


async def test_wsl_tries_powershell_before_a_broken_native_x11_bridge(mocks):
    mocks.getImage.value = RuntimeError("Broken X11 bridge")
    tmpFile: list[str] = []

    async def implementation(command, args, *options):
        if command in ("wl-paste", "xclip"):
            return None
        if command == "wslpath":
            tmpFile.append(args[1])
            return commandResult(b"C:\\Users\\O'Hare\\clip.png\n")
        if command == "powershell.exe":
            spawnOptions = options[0] if options else {}
            assert "env" not in spawnOptions
            assert "$path = 'C:\\Users\\O''Hare\\clip.png'" in args[2]
            if not tmpFile:
                raise RuntimeError("wslpath should be called before powershell.exe")
            with open(tmpFile[0], "wb") as file:  # noqa: ASYNC230 - pi writes it with writeFileSync
                file.write(png)
            return commandResult(b"ok\n")
        raise RuntimeError(f"Unexpected command: {command}")

    mocks.command.implementation = implementation
    assert await readClipboardImage({"platform": "linux", "env": {"WSL_DISTRO_NAME": "Ubuntu"}}) == ClipboardImage(
        bytes=png, mimeType="image/png"
    )
    assert mocks.getNativeClipboard.calls == []


@pytest.mark.parametrize("platform", ["darwin", "win32"])
@pytest.mark.parametrize("bytes_", [png, None, b""])
async def test_reads_native_image_once(mocks, platform, bytes_):
    # pi's `undefined` and `null` are both Python's None.
    mocks.getImage.value = bytes_
    assert await readClipboardImage({"platform": platform, "env": {}}) == (
        ClipboardImage(bytes=bytes_, mimeType="image/png") if bytes_ else None
    )
    assert len(mocks.getImage.calls) == 1
    assert mocks.command.calls == []


async def test_returns_null_without_a_native_helper(mocks):
    mocks.getNativeClipboard.value = None
    assert await readClipboardImage({"platform": "win32", "env": {}}) is None
    assert mocks.getImage.calls == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
async def test_propagates_native_transfer_errors_without_fallback(mocks, platform):
    error = RuntimeError("Native clipboard operation failed")
    mocks.getImage.value = error
    with pytest.raises(RuntimeError) as caught:
        await readClipboardImage({"platform": platform, "env": {"WAYLAND_DISPLAY": "1", "DISPLAY": ":0"}})
    assert caught.value is error
    assert [call[0] for call in mocks.command.calls] == (["wl-paste", "xclip"] if platform == "linux" else [])


async def test_termux_does_not_read_image_clipboards(mocks):
    assert await readClipboardImage({"platform": "linux", "env": {"TERMUX_VERSION": "0.119"}}) is None
    assert mocks.getNativeClipboard.calls == []
    assert mocks.command.calls == []


# clipboard-image-bmp-conversion.test.ts


def createTinyBmp1x1Red24bpp() -> bytes:
    # Minimal 1x1 24bpp BMP (BGR + row padding to 4 bytes)
    # File size = 14 (BMP header) + 40 (DIB header) + 4 (pixel row) = 58
    header = b"BM" + struct.pack("<IHHI", 58, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)
    # Pixel data (B, G, R) + 1 byte padding
    return header + dib + bytes([0x00, 0x00, 0xFF, 0x00])


@pytest.mark.parametrize("platform", ["linux", "win32"])
async def test_converts_command_native_bmp_to_png(monkeypatch, platform):
    async def run(command, args, _options=None):
        if command == "wl-paste" and "--list-types" in args:
            return b"image/bmp\n"
        if command == "wl-paste" and "image/bmp" in args:
            return createTinyBmp1x1Red24bpp()
        return None

    async def getImage():
        return createTinyBmp1x1Red24bpp()

    monkeypatch.setattr(clipboard_image, "runClipboardCommand", run)
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: SimpleNamespace(getImage=getImage))
    monkeypatch.setattr(clipboard_image, "isWSL", lambda env=None: False)

    image = await readClipboardImage({"env": {"WAYLAND_DISPLAY": "wayland-0"}, "platform": platform})

    assert image is not None
    assert image.mimeType == "image/png"
    assert list(image.bytes[:4]) == [0x89, 0x50, 0x4E, 0x47]
