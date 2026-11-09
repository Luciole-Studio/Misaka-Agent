"""pi 1.0.0's clipboard paste: ``clipboard-paste-file-paths.test.ts``, the native-error case of
``clipboard-image-native-errors.test.ts`` and the ``readClipboardText`` cases of ``clipboard.test.ts``,
plus MISAKA's own readers behind them (Finder through ``osascript``, Explorer through the Win32 API,
``pbcopy``/``pbpaste`` as the macOS text clipboard)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.ui.tui import native_platform
from misaka.utils import clipboard, clipboard_image


def _result(*, stdout: bytes, ok: bool) -> SimpleNamespace:
    """A clipboard command's answer: its stdout, or a failure."""
    return SimpleNamespace(stdout=stdout, ok=ok)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "SSH_CONNECTION", "SSH_CLIENT", "MOSH_CONNECTION", "WAYLAND_DISPLAY", "DISPLAY", "TERMUX_VERSION",
        "WT_SESSION", "WSL_DISTRO_NAME", "WSLENV",
    ):
        monkeypatch.delenv(name, raising=False)


# -- readClipboardText (clipboard.test.ts) --------------------------------------------------------


@pytest.fixture
def reader(monkeypatch):
    """The text reader on macOS with a native clipboard, and a recorder for the commands it runs."""
    native = SimpleNamespace(getText=AsyncMock(return_value=None))
    commands: list[tuple[str, list[str], int]] = []
    native_loads: list[None] = []
    behaviour = {"run": lambda name, args: _result(stdout=b"", ok=False)}

    async def runClipboardCommand(command, args, options=None):
        commands.append((command, list(args), (options or {}).get("timeoutMs", 3000)))
        result = behaviour["run"](command, args)
        return result.stdout if result.ok else None

    def load_native():
        native_loads.append(None)
        return native

    monkeypatch.setattr(clipboard, "_platform", lambda: "darwin")
    monkeypatch.setattr(native_platform, "getNativeClipboard", load_native)
    monkeypatch.setattr(clipboard, "runClipboardCommand", runClipboardCommand)
    return SimpleNamespace(native=native, commands=commands, native_loads=native_loads, behaviour=behaviour)


async def test_awaits_native_clipboard_text_and_catches_rejected_reads(reader):
    reader.native.getText.return_value = "clipboard text"
    assert await clipboard.readClipboardText() == "clipboard text"
    reader.native.getText.side_effect = RuntimeError("clipboard unavailable")
    assert await clipboard.readClipboardText() is None


@pytest.mark.parametrize("text", ["clipboard text", ""])
@pytest.mark.parametrize(("env", "command", "args", "calls"), [
    ("WAYLAND_DISPLAY", "wl-paste", ["--no-newline", "--type", "text"], ["wl-paste"]),
    ("DISPLAY", "xclip", ["-selection", "clipboard", "-out"], ["xclip"]),
    ("DISPLAY", "xsel", ["--clipboard", "--output"], ["xclip", "xsel"]),
    ("TERMUX_VERSION", "termux-clipboard-get", [], ["termux-clipboard-get"]),
])
async def test_a_linux_command_result_stops_the_fallback(reader, monkeypatch, env, command, args, calls, text):
    # Regression test for #7248: empty Wayland content must not fall through to stale X11.
    monkeypatch.setattr(clipboard, "_platform", lambda: "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv(env, "1")
    reader.behaviour["run"] = lambda name, _args: _result(
        stdout=text.encode(), ok=name == command
    )
    assert await clipboard.readClipboardText() == (text or None)
    assert [name for name, _args, _timeout in reader.commands] == calls
    assert reader.commands[-1] == (command, args, 5000)
    assert reader.native_loads == []


@pytest.mark.parametrize("text", ["native text", "", None])
async def test_uses_native_x11_after_command_failures(reader, monkeypatch, text):
    monkeypatch.setattr(clipboard, "_platform", lambda: "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    reader.native.getText.return_value = text
    assert await clipboard.readClipboardText() == (text or None)
    assert len(reader.native_loads) == 1
    assert [name for name, _args, _timeout in reader.commands] == ["wl-paste", "xclip", "xsel"]


async def test_falls_back_to_x11_tools_when_wl_paste_is_unavailable(reader, monkeypatch):
    monkeypatch.setattr(clipboard, "_platform", lambda: "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    reader.behaviour["run"] = lambda name, _args: _result(stdout=b"X11 text", ok=name != "wl-paste")
    assert await clipboard.readClipboardText() == "X11 text"
    assert reader.native_loads == []


async def test_text_that_is_not_utf8_reads_with_replacement_characters(reader, monkeypatch):
    # Node's `toString("utf8")` replaces the bytes it cannot decode; Python would raise.
    monkeypatch.setattr(clipboard, "_platform", lambda: "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    reader.behaviour["run"] = lambda _name, _args: _result(stdout=b"caf\xe9", ok=True)
    assert await clipboard.readClipboardText() == "caf\ufffd"


async def test_there_is_no_text_without_a_native_clipboard(monkeypatch):
    monkeypatch.setattr(clipboard, "_platform", lambda: "darwin")
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: None)
    assert await clipboard.readClipboardText() is None


# -- readClipboardFilePaths -----------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "expected"), [(["/tmp/a.png", "/tmp/b c.png"], ["/tmp/a.png", "/tmp/b c.png"]), ([], None), (None, None)])
async def test_file_paths_come_from_the_native_clipboard(monkeypatch, answer, expected):
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: SimpleNamespace(getFilePaths=AsyncMock(return_value=answer)))
    assert await clipboard.readClipboardFilePaths() == expected


async def test_a_clipboard_that_cannot_list_files_has_none(monkeypatch):
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: SimpleNamespace())
    assert await clipboard.readClipboardFilePaths() is None
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: None)
    assert await clipboard.readClipboardFilePaths() is None


async def test_a_failing_native_file_read_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        native_platform, "getNativeClipboard",
        lambda: SimpleNamespace(getFilePaths=AsyncMock(side_effect=RuntimeError("Native clipboard file read failed"))),
    )
    with pytest.raises(RuntimeError, match="Native clipboard file read failed"):
        await clipboard.readClipboardFilePaths()


# -- the native readers (MISAKA fork: in-process stand-ins for pi's native addons) ----------------


def test_the_files_copied_in_finder_are_read_through_osascript(monkeypatch):
    seen: list = []

    def run(command, **options):
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout=b'["/Users/a/My Photos/photo.png", "/tmp/b.png", "relative", 3]', stderr=b"")

    monkeypatch.setattr(native_platform.subprocess, "run", run)
    assert native_platform._readFinderPaths() == ["/Users/a/My Photos/photo.png", "/tmp/b.png"]
    command, options = seen[0]
    assert command[:3] == ["osascript", "-l", "JavaScript"] and command[3] == "-e"
    assert "NSPasteboard" in command[4] and options["timeout"] == 5


@pytest.mark.parametrize("failure", [
    OSError("no osascript"),
    subprocess.TimeoutExpired("osascript", 5),
    subprocess.CalledProcessError(1, "osascript"),
])
def test_finder_paths_that_cannot_be_read_are_no_files(monkeypatch, failure):
    def run(*_args, **_options):
        raise failure

    monkeypatch.setattr(native_platform.subprocess, "run", run)
    assert native_platform._readFinderPaths() is None


@pytest.mark.parametrize("stdout", [b"[]", b"undefined", b"", b'{"a": 1}', b'["relative"]'])
def test_finder_answers_without_a_file_path_are_no_files(monkeypatch, stdout):
    monkeypatch.setattr(
        native_platform.subprocess, "run", lambda command, **_options: subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")
    )
    assert native_platform._readFinderPaths() is None


def test_the_linux_clipboard_helper_cannot_tell_which_files_were_copied():
    helper = native_platform._LinuxX11ClipboardHelper(lambda: "")
    assert getattr(helper, "getFilePaths", None) is None
    assert getattr(helper, "setText", None) is None


async def test_the_files_copied_in_explorer_are_read_through_the_win32_module(monkeypatch):
    import misaka.utils

    fake = SimpleNamespace(file_paths=lambda: [r"C:\Users\a\My Photos\photo.png"])
    monkeypatch.delattr(misaka.utils, "win_clipboard", raising=False)
    monkeypatch.setitem(sys.modules, "misaka.utils.win_clipboard", fake)
    assert await native_platform._Win32PlatformHelper().getFilePaths() == [r"C:\Users\a\My Photos\photo.png"]


async def test_the_native_clipboard_lists_files_off_the_event_loop(monkeypatch):
    import threading

    threads: list[bool] = []

    def read():
        threads.append(threading.current_thread() is threading.main_thread())
        return ["/darwin/a.png"]

    monkeypatch.setattr(native_platform, "_readFinderPaths", read)
    assert await native_platform._DarwinPlatformHelper().getFilePaths() == ["/darwin/a.png"]
    assert threads == [False]


async def test_macos_reads_and_writes_text_through_pbpaste_and_pbcopy(monkeypatch):
    runs: list = []

    def run(command, **options):
        runs.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout="héllo".encode(), stderr=b"")

    monkeypatch.setattr(native_platform.subprocess, "run", run)
    helper = native_platform._DarwinPlatformHelper()
    assert await helper.getText() == "héllo"
    await helper.setText("世界")
    assert [command for command, _options in runs] == [["pbpaste"], ["pbcopy"]]
    assert runs[1][1]["input"] == "世界".encode()
    assert all(options["env"]["LC_CTYPE"] == "UTF-8" for _command, options in runs)


def test_linux_without_pyperclip_has_no_x11_clipboard_helper(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyperclip", None)  # `import pyperclip` fails
    assert native_platform.buildNativePlatformHelper("linux", "-x11") is None


# native-platform.test.ts, adapted: the helpers are in-process objects, not addons.


def test_uses_the_native_platform_helper_directly_as_the_clipboard_api(monkeypatch):
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(native_platform.sys, "platform", platform)
        monkeypatch.setattr(native_platform, "helpers", {})
        clipboard = native_platform.getNativeClipboard()
        assert clipboard is not None
        assert callable(clipboard.getText)
        assert callable(clipboard.getImage)
        assert callable(getattr(clipboard, "setText", None))
        assert clipboard is native_platform.getNativePlatformHelper()
        assert clipboard is native_platform.getNativeClipboard()


def test_linux_loads_x11_lazily_and_rechecks_display(monkeypatch):
    monkeypatch.setattr(native_platform.sys, "platform", "linux")
    monkeypatch.setattr(native_platform, "helpers", {})
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert native_platform.getNativeClipboard() is None  # Wayland paste uses wl-paste.
    builds: list[str] = []
    helper = SimpleNamespace(getText=AsyncMock(return_value="X11"), getImage=AsyncMock(return_value=None))

    def build(platform, suffix):
        builds.append(f"{platform}{suffix}")
        return helper

    monkeypatch.setattr(native_platform, "buildNativePlatformHelper", build)
    monkeypatch.setenv("DISPLAY", ":0")
    clipboard = native_platform.getNativeClipboard()
    assert clipboard is helper
    assert helper.getText.await_count == 0
    monkeypatch.delenv("DISPLAY")
    assert native_platform.getNativeClipboard() is None
    monkeypatch.setenv("DISPLAY", ":1")
    assert native_platform.getNativeClipboard() is clipboard
    assert builds == ["linux-x11"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Win32 clipboard API")
class TestWin32Clipboard:
    """The sequencing of the Win32 calls, with the DLLs replaced: the real clipboard is the user's."""

    @staticmethod
    def install(monkeypatch, *, available=(), opens=(True,), paths=(), text=""):
        import ctypes

        from misaka.utils import win_clipboard

        events: list[str] = []
        opened = iter(opens)
        text_buffer = ctypes.create_unicode_buffer(text + "\0padding")

        def drag(_drop, index, buffer, size):
            if index == 0xFFFFFFFF:
                return len(paths)
            if buffer is None:
                return len(paths[index])
            buffer.value = paths[index][: size - 1]
            return len(paths[index])

        user32 = SimpleNamespace(
            IsClipboardFormatAvailable=lambda fmt: fmt in available,
            OpenClipboard=lambda owner: events.append("open") or next(opened, False),
            GetClipboardData=lambda fmt: 1234,
            CloseClipboard=lambda: events.append("close"),
        )
        kernel32 = SimpleNamespace(
            GlobalLock=lambda memory: ctypes.addressof(text_buffer),
            GlobalSize=lambda memory: ctypes.sizeof(text_buffer),
            GlobalUnlock=lambda memory: events.append("unlock"),
            GetConsoleWindow=lambda: 0,
        )
        monkeypatch.setattr(win_clipboard, "_user32", user32)
        monkeypatch.setattr(win_clipboard, "_kernel32", kernel32)
        monkeypatch.setattr(win_clipboard, "_shell32", SimpleNamespace(DragQueryFileW=drag))
        monkeypatch.setattr(win_clipboard.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(win_clipboard, "_failed", lambda what: OSError(f"{what} failed"))
        return win_clipboard, events

    def test_copied_files_are_listed_with_their_full_paths(self, monkeypatch):
        paths = [r"C:\Users\a\My Photos\photo.png", r"C:\tmp\世界.txt"]
        win_clipboard, events = self.install(monkeypatch, available=(15,), paths=paths)
        assert win_clipboard.file_paths() == paths
        assert events == ["open", "close"]

    def test_a_clipboard_without_files_is_not_opened(self, monkeypatch):
        win_clipboard, events = self.install(monkeypatch, available=(13,))
        assert win_clipboard.file_paths() is None
        assert events == []

    def test_a_busy_clipboard_is_retried_and_then_an_error(self, monkeypatch):
        win_clipboard, events = self.install(monkeypatch, available=(15,), opens=(False, False, True), paths=["/a"])
        assert win_clipboard.file_paths() == ["/a"]
        assert events == ["open", "open", "open", "close"]
        win_clipboard, events = self.install(monkeypatch, available=(15,), opens=(), paths=["/a"])
        with pytest.raises(OSError, match="OpenClipboard failed"):
            win_clipboard.file_paths()
        assert events == ["open"] * 10   # win32-platform.c OPEN_CLIPBOARD_ATTEMPTS

    def test_text_ends_at_the_first_nul(self, monkeypatch):
        win_clipboard, events = self.install(monkeypatch, available=(13,), text="héllo 世界 😀\nsecond")
        assert win_clipboard.paste() == "héllo 世界 😀\nsecond"
        assert events == ["open", "unlock", "close"]

    def test_a_clipboard_without_text_pastes_nothing(self, monkeypatch):
        win_clipboard, events = self.install(monkeypatch, available=())
        assert win_clipboard.paste() == ""
        assert events == []


# -- handleClipboardPaste (clipboard-paste-file-paths.test.ts) --------------------------------------


@pytest.fixture
def paste(monkeypatch):
    from misaka.modes.interactive import interactive_mode

    readers = SimpleNamespace(
        file_paths=AsyncMock(return_value=None), image=AsyncMock(return_value=None), text=AsyncMock(return_value=None)
    )
    monkeypatch.setattr(interactive_mode, "readClipboardFilePaths", readers.file_paths)
    monkeypatch.setattr(interactive_mode, "readClipboardImage", readers.image)
    monkeypatch.setattr(interactive_mode, "readClipboardText", readers.text)

    def context(editor, *, bash=False):
        return SimpleNamespace(editor=editor, showError=Mock(), ui=SimpleNamespace(requestRender=Mock()), isBashMode=bash)

    async def run(ctx):
        await interactive_mode.InteractiveMode.handleClipboardPaste(ctx)

    return SimpleNamespace(readers=readers, context=context, run=run, mode=interactive_mode)


def editor_at(text: str, col: int, line: int = 0):
    return SimpleNamespace(insertTextAtCursor=Mock(), getCursor=lambda: {"line": line, "col": col}, getText=lambda: text)


async def test_finder_file_paths_take_precedence_over_their_icon_image(paste):
    # Regression test for #9999.
    file_paths = ["/tmp/screenshot.png", "/tmp/My Photos/photo.png"]
    paste.readers.file_paths.return_value = file_paths
    paste.readers.image.return_value = SimpleNamespace(bytes=b"\x89PNG", mimeType="image/png")
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with("\n".join(file_paths))
    paste.readers.image.assert_not_called()
    ctx.ui.requestRender.assert_called_once_with()


async def test_clipboard_file_paths_containing_terminal_control_characters_are_rejected(paste):
    paste.readers.file_paths.return_value = ["/tmp/photo\x1b]0;unsafe\x07.png"]
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_not_called()
    paste.readers.image.assert_not_called()
    ctx.showError.assert_called_once_with("Failed to paste from clipboard: Clipboard file path contains control characters")


async def test_native_file_path_errors_are_shown_without_falling_through_to_the_icon_image(paste):
    paste.readers.file_paths.side_effect = RuntimeError("Native clipboard file read failed")
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_not_called()
    paste.readers.image.assert_not_called()
    ctx.showError.assert_called_once_with("Failed to paste from clipboard: Native clipboard file read failed")


@pytest.mark.parametrize(("editor_text", "cursor_col"), [("Review:", 7), ("確認", 2)])
async def test_clipboard_file_paths_are_separated_from_preceding_punctuation_or_unicode_text(paste, editor_text, cursor_col):
    paste.readers.file_paths.return_value = ["/tmp/photo.png"]
    ctx = paste.context(editor_at(editor_text, cursor_col))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with(" /tmp/photo.png")
    paste.readers.image.assert_not_called()


async def test_bash_mode_shell_quotes_file_paths_and_inserts_them_as_arguments(paste):
    paste.readers.file_paths.return_value = ["/tmp/My Photos/photo.png", "/tmp/$(touch hacked).png", "/tmp/plain.png"]
    ctx = paste.context(editor_at("catDEST", 3), bash=True)
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with(" '/tmp/My Photos/photo.png' '/tmp/$(touch hacked).png' /tmp/plain.png ")
    paste.readers.image.assert_not_called()


async def test_native_image_errors_abort_paste_without_reading_text_or_changing_the_editor(paste, monkeypatch):
    # clipboard-image-native-errors.test.ts, through the real image reader.
    class Native:
        async def getImage(self):
            raise RuntimeError("Native clipboard operation failed")

    monkeypatch.setattr(clipboard_image, "_platform", lambda: "darwin")
    monkeypatch.setattr(native_platform, "getNativeClipboard", lambda: Native())
    monkeypatch.setattr(paste.mode, "readClipboardImage", clipboard_image.readClipboardImage)
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    paste.readers.text.assert_not_called()
    ctx.editor.insertTextAtCursor.assert_not_called()
    ctx.ui.requestRender.assert_not_called()
    ctx.showError.assert_called_once_with("Failed to paste from clipboard: Native clipboard operation failed")


# -- MISAKA regressions ---------------------------------------------------------------------------


async def test_an_image_is_attached_through_a_temporary_file(paste, tmp_path, monkeypatch):
    monkeypatch.setattr(paste.mode.tempfile, "gettempdir", lambda: str(tmp_path))
    paste.readers.image.return_value = clipboard_image.ClipboardImage(bytes=b"\x89PNG", mimeType="image/png")
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    inserted = ctx.editor.insertTextAtCursor.call_args_list[0].args[0]
    assert inserted.startswith(str(tmp_path)) and inserted.endswith(".png")
    assert Path(inserted).read_bytes() == b"\x89PNG"
    paste.readers.text.assert_not_called()


async def test_text_is_the_last_fallback(paste):
    paste.readers.text.return_value = "plain text"
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with("plain text")
    ctx.ui.requestRender.assert_called_once_with()


async def test_an_empty_clipboard_changes_nothing(paste):
    ctx = paste.context(SimpleNamespace(insertTextAtCursor=Mock()))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_not_called()
    ctx.ui.requestRender.assert_not_called()
    ctx.showError.assert_not_called()


async def test_an_editor_without_insert_text_at_cursor_is_left_alone(paste):
    # pi: `this.editor.insertTextAtCursor?.(text)` -- an editor without the method gets nothing.
    paste.readers.text.return_value = " more"
    editor = SimpleNamespace(getText=lambda: "so far", setText=Mock())
    ctx = paste.context(editor)
    await paste.run(ctx)
    editor.setText.assert_not_called()
    ctx.ui.requestRender.assert_called_once_with()


async def test_the_cursor_on_a_later_line_is_judged_by_that_line(paste):
    paste.readers.file_paths.return_value = ["/tmp/a.png"]
    ctx = paste.context(editor_at("first line\nsecond  ", 8, line=1))
    await paste.run(ctx)
    # "second  " has a space before and after the cursor at column 8.
    ctx.editor.insertTextAtCursor.assert_called_once_with("/tmp/a.png")
    ctx = paste.context(editor_at("first line\nsecond", 6, line=1))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with(" /tmp/a.png")


async def test_a_cursor_past_the_end_of_the_line_adds_no_space_after(paste):
    paste.readers.file_paths.return_value = ["/tmp/a.png"]
    ctx = paste.context(editor_at("x", 5))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with("/tmp/a.png")


@pytest.mark.parametrize(("character", "separated"), [("\u00a0", False), ("\ufeff", False), ("\u3000", False), ("\x85", True), ("\x1f", True), ("a", True)])
async def test_the_space_before_a_path_follows_javascript_whitespace(paste, character, separated):
    # JavaScript's `\s` includes U+00A0, U+FEFF and U+3000, and leaves out U+0085 and U+001F.
    paste.readers.file_paths.return_value = ["/tmp/a.png"]
    ctx = paste.context(editor_at(character, 1))
    await paste.run(ctx)
    ctx.editor.insertTextAtCursor.assert_called_once_with((" " if separated else "") + "/tmp/a.png")


@pytest.mark.parametrize(("value", "quoted"), [
    ("plain/path-1_2.png", "plain/path-1_2.png"),
    ("~/a:b@c.png", "~/a:b@c.png"),
    ("", "''"),
    ("with space", "'with space'"),
    ("it's", "'it'\\''s'"),
    ("$(x)", "'$(x)'"),
    ("世界", "'世界'"),
])
def test_quote_if_needed(paste, value, quoted):
    assert paste.mode.quoteIfNeeded(value) == quoted
