"""The unit tests of pi env's Rust daemon (``#[cfg(test)]`` in ``frame.rs``, ``decode.rs``, ``scan.rs``, ``window.rs``,
``list.rs`` and ``sys/windows.rs``; ``pty.rs`` has none), and MISAKA's checks of the Python daemon: Python 3.9 syntax, standard-library-only imports, the
reproducible zip application, and that only the experimental side reaches ``misaka.env`` (D5)."""

from __future__ import annotations

import ast
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Self

import pytest

from misaka.env.bundle import DAEMON_PACKAGE, SHEBANG, daemonArchive
from misaka.env.daemon.decode import StreamDecoder
from misaka.env.daemon.errors import Failure
from misaka.env.daemon.frame import RESULT, Frame, read_frame, write_frame
from misaka.env.daemon.list import Options, list_files
from misaka.env.daemon.scan import LineScanner
from misaka.env.daemon.window import Pending, Window, newline_count

ROOT = Path(__file__).resolve().parent.parent
DAEMON = ROOT / "misaka" / "env" / "daemon"


# frame.rs


def test_round_trips_json_and_payload():
    buffer = io.BytesIO()
    write_frame(buffer, Frame(RESULT, 7, {"a": 1}, bytes([0, 255, 10])))
    buffer.seek(0)
    frame = read_frame(buffer)
    assert frame is not None
    assert (frame.kind, frame.id) == (RESULT, 7)
    assert frame.json == {"a": 1}
    assert frame.payload == bytes([0, 255, 10])
    assert read_frame(io.BytesIO(b"")) is None


# decode.rs


def test_keeps_a_feff_that_does_not_start_the_stream():
    decoder = StreamDecoder()
    data = [0xE2, 0x82, 0xEF, 0xBB, 0xBF, 0x61]
    text = "".join(decoder.decode(bytes([byte]), False) for byte in data) + decoder.decode(b"", True)
    assert text == "\ufffd\ufeffa"


def test_drops_only_a_leading_mark_and_never_sniffs_utf16():
    decoder = StreamDecoder()
    assert decoder.decode(bytes([0xEF, 0xBB]), False) + decoder.decode(bytes([0xBF, 0x61]), True) == "a"
    decoder = StreamDecoder()
    assert decoder.decode(bytes([0xFF, 0xFE, 0x41, 0x00]), True) == "\ufffd\ufffdA\x00"


def test_replaces_an_incomplete_sequence_once():
    decoder = StreamDecoder()
    assert decoder.decode(bytes([0xE2, 0x82, 0x41]), True) == "\ufffdA"
    decoder = StreamDecoder()
    assert decoder.decode(bytes([0xE2, 0x82]), True) == "\ufffd"


# scan.rs


def scan(file: bytes, start: int, end: int | None, chunk: int) -> dict:
    scanner = LineScanner(start, end)
    size = max(chunk, 1)
    for index in range(0, len(file), size):
        scanner.push(file[index : index + size])
    return scanner.finish()


def test_locates_and_measures_lines_like_whole_file_decoding():
    # BOM, "a", invalid sequence, empty line, a later U+FEFF, and no final newline.
    file = bytes([0xEF, 0xBB, 0xBF, 0x61, 0x0A, 0xE2, 0x82, 0x0A, 0x0A, 0xEF, 0xBB, 0xBF, 0x62, 0x0A, 0xC3, 0xA9])
    for chunk in range(1, 6):
        whole = scan(file, 0, None, chunk)
        assert whole["newlines"] == 4
        # "a\n\ufffd\n\n\ufeffb\né" is 1+1+3+1+1+3+1+1+2 = 14 bytes.
        assert whole["selectedBytes"] == 14
        assert whole["firstLineBytes"] == 1
        middle = scan(file, 1, 3, chunk)
        assert (middle["start"], middle["end"]) == (5, 8)
        assert middle["selectedBytes"] == 4
        assert middle["lastLineStart"] == 8
        past = scan(file, 9, None, chunk)
        assert past["start"] == 16


# window.rs


def window(max_bytes: int, max_lines: int) -> Window:
    return Window(max_bytes, max_lines, 0.0, 1e12)


def total(events: list) -> tuple[int, int, bytes]:
    """Bytes and newlines delivered or skipped, and the text after the last skip."""
    count = 0
    newlines = 0
    after = b""
    for event in events:
        if event.skipped is not None:
            count += event.skipped["bytes"]
            newlines += event.skipped["newlines"]
            after = b""
        count += len(event.text)
        newlines += newline_count(event.text)
        after += event.text
    return count, newlines, after


def test_counts_everything_and_keeps_more_than_the_window_after_a_skip():
    pending = Pending(window(10, 2))
    full = b""
    events = []
    for index in range(500):
        text = f"line {index} é😀\n".encode()
        full += text
        pending.push("stderr" if index % 7 == 0 else "stdout", text)
        if index % 50 == 49:
            events.extend(pending.take())
    events.extend(pending.take())
    count, newlines, after = total(events)
    assert count == len(full)
    assert newlines == newline_count(full)
    assert full.endswith(after)
    assert len(after) > 10 or newline_count(after) > 2
    assert any(event.skipped is not None for event in events)


def test_delivers_streams_apart_without_skips():
    pending = Pending(window(1000, 1000))
    pending.push("stdout", b"a")
    pending.push("stderr", b"b")
    pending.push("stdout", b"c")
    assert [event.stream for event in pending.take()] == ["stdout", "stderr", "stdout"]


# list.rs


class Tree:
    """A directory of files for a test, removed after it."""

    def __init__(self, files: list[str]) -> None:
        self.root = tempfile.mkdtemp(prefix=f"pi-env-list-{os.getpid()}-")
        for file in files:
            self.write(file, "")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path: str, content: str) -> None:
        target = os.path.join(self.root, path)
        if path.endswith("/"):
            os.makedirs(target, exist_ok=True)
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text(content, encoding="utf-8")

    def list(self, options: Options) -> tuple[list[str], bool]:
        listing = list_files(self.root, options, lambda: False)
        entries = [
            f"{chr(record[0])} {record[1:].decode('utf-8', 'replace')}"
            for record in bytes(listing.payload).split(b"\0")
            if record
        ]
        assert len(entries) == listing.count
        return entries, listing.truncated


def options(**fields: Any) -> Options:
    """``Options::from_json(&json!({}))``, with ``fields`` replaced as ``Options { field, ..options() }`` does."""
    parsed = Options.from_json({})
    for key, value in fields.items():
        assert hasattr(parsed, key)
        setattr(parsed, key, value)
    return parsed


def test_lists_files_and_directories_depth_first_in_name_order():
    with Tree(["b.txt", "a/z.txt", "a/b/c.txt", "empty/", ".hidden/x", "A.txt"]) as tree:
        entries, truncated = tree.list(options())
        assert entries == [
            "d .hidden",
            "f .hidden/x",
            "f A.txt",
            "d a",
            "d a/b",
            "f a/b/c.txt",
            "f a/z.txt",
            "f b.txt",
            "d empty",
        ]
        assert not truncated
        entries, _ = tree.list(options(hidden=False))
        assert not any(".hidden" in entry for entry in entries)


def test_skips_what_git_ignores_only_in_work_trees():
    with Tree(["keep.txt", "out/x.js", "sub/keep.txt", "sub/drop.log", "node_modules/m/i.js"]) as tree:
        tree.write(".gitignore", "out/\n*.log\n")
        tree.write("sub/.gitignore", "!keep.txt\n")
        # Not a work tree yet: everything is listed.
        entries, _ = tree.list(options())
        assert "f out/x.js" in entries
        tree.write(".git/HEAD", "ref: refs/heads/main\n")
        tree.write(".git/info/exclude", "keep.txt\n")
        entries, _ = tree.list(options(exclude=["node_modules"]))
        assert entries == ["f .gitignore", "d sub", "f sub/.gitignore", "f sub/keep.txt"]
        entries, _ = tree.list(options(gitignore=False))
        assert "f out/x.js" in entries
        assert not any(".git/" in entry for entry in entries)
        # A subdirectory of a work tree applies the rules of its parents.
        sub = list_files(os.path.join(tree.root, "sub"), options(), lambda: False)
        assert bytes(sub.payload) == b"f.gitignore\0fkeep.txt\0"


@pytest.mark.skipif(sys.platform == "win32", reason="#[cfg(unix)]")
def test_lists_symbolic_links_without_following_them():
    with Tree(["dir/inner.txt"]) as tree:
        os.symlink(os.path.join(tree.root, "dir"), os.path.join(tree.root, "link"))
        os.symlink("missing", os.path.join(tree.root, "dangling"))
        os.symlink(tree.root, os.path.join(tree.root, "dir", "loop"))
        entries, _ = tree.list(options())
        assert entries == ["l dangling", "d dir", "f dir/inner.txt", "l dir/loop", "l link"]


def test_stops_at_bounds_with_truncated():
    with Tree(["a", "b", "c"]) as tree:
        entries, truncated = tree.list(options(max_entries=2))
        assert (len(entries), truncated) == (2, True)
        entries, truncated = tree.list(options(max_entries=3))
        assert (len(entries), truncated) == (3, False)
        entries, truncated = tree.list(options(max_bytes=6))
        assert (entries, truncated) == (["f a", "f b"], True)
        entries, truncated = tree.list(options(timeout=0))
        assert truncated and entries == []


def test_fails_on_missing_roots_files_and_aborts():
    with Tree(["file"]) as tree:
        with pytest.raises(Failure) as missing:
            list_files(os.path.join(tree.root, "missing"), options(), lambda: False)
        assert missing.value.code == "ENOENT"
        with pytest.raises(Failure) as file:
            list_files(os.path.join(tree.root, "file"), options(), lambda: False)
        assert file.value.code == "ENOTDIR"
        with pytest.raises(Failure) as aborted:
            list_files(tree.root, options(), lambda: True)
        assert aborted.value.code == "aborted"


# sys/windows.rs


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows system layer loads only on Windows")
def test_quotes_arguments_like_libuv():
    from misaka.env.daemon.system.windows import quote_argument

    assert quote_argument("") == '""'
    assert quote_argument("plain") == "plain"
    assert quote_argument("a b") == '"a b"'
    assert quote_argument("*.txt") == "*.txt"
    assert quote_argument('say "hi"') == '"say \\"hi\\""'
    assert quote_argument("C:\\dir with space\\") == '"C:\\dir with space\\\\"'
    assert quote_argument("a\\\\b c") == '"a\\\\b c"'


# MISAKA: the Python daemon.


def daemonSources() -> list[Path]:
    return sorted(DAEMON.rglob("*.py"))


@pytest.mark.parametrize("path", daemonSources(), ids=lambda path: path.relative_to(DAEMON).as_posix())
def test_the_daemon_parses_as_python_3_9_and_imports_only_the_standard_library(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        else:
            continue
        top = module.split(".")[0]
        assert top in sys.stdlib_module_names or top == "__future__", f"{path.name} imports {module}"


def test_the_daemon_archive_is_reproducible_and_runs_the_package():
    archive = daemonArchive()
    assert archive == daemonArchive()
    assert archive.startswith(SHEBANG)
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        names = opened.namelist()
        assert "__main__.py" in names
        assert f"{DAEMON_PACKAGE}/main.py" in names
        assert f"{DAEMON_PACKAGE}/system/unix.py" in names
        assert all(not name.endswith(".pyc") for name in names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in opened.infolist())
        assert b"\r\n" not in opened.read(f"{DAEMON_PACKAGE}/main.py")


ALLOWED = ("misaka/env/",)


def test_nothing_outside_misaka_env_imports_it():
    """D5: like pi's package, which nothing in pi's coding agent imports, nothing in misaka imports ``misaka.env``."""
    offenders = []
    for path in sorted((ROOT / "misaka").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(ALLOWED):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            if any(module == "misaka.env" or module.startswith("misaka.env.") for module in modules):
                offenders.append(relative)
    assert offenders == []
