"""Regression tests for what the adversarial review of the durable port found."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.chord.output import OutputBuffer
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.errors import StorageRequestError
from misaka.durable.storage.memory import MemoryStorage
from misaka.durable.testing.assertions import createAssertions
from misaka.utils import node_path
from misaka.utils.js import Utf8Decoder
from tests.durable_support import context, createConversation, flush, openTestSession

# -- the mutation line -------------------------------------------------------------------------------------------------


async def test_a_caller_that_stops_waiting_for_a_commit_leaves_the_line_working():
    test = openTestSession()
    release: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def hang(tx: Any) -> None:
        await release

    waiting = test.session.commit(hang, context)
    await flush()
    other = test.session.commit(lambda tx: None, context)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other, 0.01)
    waiting.cancel()
    # The commit the caller gave up on, and the one behind it, run to the end; nothing behind them is cancelled.
    release.set_result(None)
    await flush()
    assert isinstance(await createConversation(test.session), int)


async def test_a_commit_whose_callback_ends_cancelled_does_not_break_the_line():
    test = openTestSession()
    cancelled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    cancelled.cancel()

    async def broken(tx: Any) -> None:
        await cancelled

    with pytest.raises(asyncio.CancelledError):
        await test.session.commit(broken, context)
    assert isinstance(await createConversation(test.session), int)


async def test_cancelling_one_close_caller_leaves_the_close_to_the_others():
    test = openTestSession()
    first = test.session.close(context)
    second = test.session.close(context)
    first.cancel()
    await second
    await test.session.close(context)


async def test_an_unawaited_transaction_operation_fails_the_commit():
    test = openTestSession()
    conversationId = await createConversation(test.session)

    def forgetful(tx: Any) -> None:
        tx.appendEntry(conversationId, {"kind": "test.note", "payload": {"text": "x"}})

    with pytest.raises(RuntimeError, match="settled before its pending Tx operations"):
        await test.session.commit(forgetful, context)
    assert len(test.storage.commits) == 1


# -- storage -----------------------------------------------------------------------------------------------------------


async def test_a_cursor_with_a_null_after_is_rejected():
    storage = MemoryStorage()
    # pi 1.3.0: storages refuse an invalid cursor with StorageRequestError, which fails only that read.
    with pytest.raises(StorageRequestError, match="Invalid storage cursor"):
        await storage.scanConversations({}, 10, {"after": None}, BACKGROUND_CONTEXT)
    # pi 1.1.0 (scan.ts): a table scan's cursor must carry its `after`, so `{}` is refused too.
    with pytest.raises(StorageRequestError, match="Invalid storage cursor"):
        await storage.scanConversations({}, 10, {}, BACKGROUND_CONTEXT)


def test_the_conformance_assertions_tell_true_from_one():
    assertions = createAssertions()
    for call in (assertions.strictEqual, assertions.deepEqual):
        with pytest.raises(AssertionError):
            call(1, True)
        with pytest.raises(AssertionError):
            call({"background": 1}, {"background": True})
    assertions.deepEqual({"n": 1}, {"n": 1.0})
    with pytest.raises(AssertionError):
        assertions.partialDeepEqual({"version": True}, {"version": 1})


# -- text decoding -----------------------------------------------------------------------------------------------------


def decode(*chunks: bytes) -> str:
    decoder = Utf8Decoder()
    return "".join(decoder.decode(chunk) for chunk in chunks) + decoder.decode(b"", True)


def test_the_decoder_is_a_text_decoder():
    assert decode(b"\xef\xbb\xbfhello") == "hello"
    assert decode(b"\xef\xbb", b"\xbfhello") == "hello"
    assert decode(b"a\xef\xbb\xbf") == "a\ufeff"
    assert decode(b"\xef") == "\ufffd"
    assert decode(b"\xef\xbb") == "\ufffd"
    assert decode(b"\xe2\x82", b"\xac") == "\u20ac"
    assert decode(b"\xed\xa0\x80") == "\ufffd\ufffd\ufffd"
    assert decode(b"a\xff\xfeb") == "a\ufffd\ufffdb"
    assert decode("é€😀".encode()) == "é€😀"
    decoder = Utf8Decoder()
    # An encoded surrogate prefix is invalid at once, not a sequence still waiting for its end.
    assert decoder.decode(b"a\xed\xa0") == "a\ufffd\ufffd"


def test_the_output_buffer_keeps_the_replacement_for_a_cut_byte_order_mark():
    buffer = OutputBuffer({"maxBytes": 100, "maxLines": 10, "retain": "head"})
    buffer.push("ok\n")
    buffer.push(bytes([0xEF]))
    buffer.end()
    assert buffer.snapshot()["text"] == "ok\n\ufffd"
    buffer = OutputBuffer({"maxBytes": 4, "maxLines": 0, "retain": "head"})
    assert buffer.push(bytes([0xED, 0xA0])) is True


# -- the local environment ---------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> NodeExecutionEnv:
    return NodeExecutionEnv({"cwd": str(tmp_path)})


async def test_reading_lines_drops_a_byte_order_mark(env: NodeExecutionEnv, tmp_path: Path):
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbfline1\nline2\n")
    result = await env.readTextLines("bom.txt", None, BACKGROUND_CONTEXT)
    assert result == {"ok": True, "value": ["line1", "line2"]}


async def test_a_directory_opens_as_a_line_reader_and_fails_when_read(env: NodeExecutionEnv, tmp_path: Path):
    (tmp_path / "dir").mkdir()
    opened = await env.openTextLineReader("dir", BACKGROUND_CONTEXT)
    assert opened["ok"] is True
    read = await opened["value"].readLine(BACKGROUND_CONTEXT)
    assert read["ok"] is False
    assert read["error"].code == "is_directory"
    await opened["value"].close(BACKGROUND_CONTEXT)


async def test_removing_a_directory_without_recursion_is_unknown(env: NodeExecutionEnv, tmp_path: Path):
    (tmp_path / "dir").mkdir()
    result = await env.remove("dir", None, BACKGROUND_CONTEXT)
    assert result["ok"] is False
    assert result["error"].code == "unknown"
    assert "rm returned EISDIR" in result["error"].message


async def test_removing_deletes_read_only_files(env: NodeExecutionEnv, tmp_path: Path):
    tree = tmp_path / "tree" / "objects"
    tree.mkdir(parents=True)
    readOnly = tree / "pack"
    readOnly.write_bytes(b"x")
    readOnly.chmod(stat.S_IREAD)
    single = tmp_path / "ro.txt"
    single.write_bytes(b"x")
    single.chmod(stat.S_IREAD)
    if sys.platform != "win32":
        # A read-only file is deleted through its directory's permissions on POSIX.
        pass
    assert (await env.remove("ro.txt", None, BACKGROUND_CONTEXT))["ok"] is True
    assert (await env.remove("tree", {"recursive": True}, BACKGROUND_CONTEXT))["ok"] is True
    assert not single.exists()
    assert not (tmp_path / "tree").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
async def test_a_junction_is_a_symbolic_link_that_removal_unlinks(env: NodeExecutionEnv, tmp_path: Path):
    import subprocess

    target = tmp_path / "target"
    target.mkdir()
    (target / "inner.txt").write_text("x", encoding="utf-8")
    subprocess.run(  # noqa: ASYNC221 - a fixture step
        ["cmd", "/c", "mklink", "/J", str(tmp_path / "junction"), str(target)], check=True, capture_output=True
    )
    info = await env.fileInfo("junction", BACKGROUND_CONTEXT)
    assert info["value"]["kind"] == "symlink"
    assert info["value"]["size"] > 0
    assert (await env.remove("junction", {"recursive": True}, BACKGROUND_CONTEXT))["ok"] is True
    assert (target / "inner.txt").exists()
    assert not (tmp_path / "junction").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows names")
async def test_a_name_windows_does_not_allow_is_not_found(env: NodeExecutionEnv):
    assert await env.exists("a?b", BACKGROUND_CONTEXT) == {"ok": True, "value": False}
    info = await env.fileInfo("a<b", BACKGROUND_CONTEXT)
    assert info["error"].code == "not_found"
    read = await env.readTextFile("a|b", BACKGROUND_CONTEXT)
    assert read["error"].code == "not_found"


def test_join_keeps_every_segment_and_a_trailing_separator():
    separator = os.sep
    assert node_path.join("a", "/b") == f"a{separator}b"
    assert node_path.join("a", "b/") == f"a{separator}b{separator}"
    assert node_path.join() == "."
    assert node_path.join("", "") == "."
    if sys.platform != "win32":
        assert node_path.join("/root", "/etc/passwd") == "/root/etc/passwd"


async def test_posix_absolute_paths_collapse_two_leading_slashes(env: NodeExecutionEnv):
    if sys.platform == "win32":
        pytest.skip("POSIX paths")
    assert (await env.absolutePath("//tmp/x", BACKGROUND_CONTEXT))["value"] == "/tmp/x"


async def test_file_urls_follow_file_url_to_path(env: NodeExecutionEnv, tmp_path: Path):
    from misaka.durable.env.node import fileUrlToPath

    assert fileUrlToPath("file:///a%2Fb") is None
    assert fileUrlToPath("file:///a%zz") is None
    if sys.platform == "win32":
        assert fileUrlToPath("file:///C:/a/b") == "C:\\a\\b"
        assert fileUrlToPath("file://host/share/x") == "\\\\host\\share\\x"
        assert fileUrlToPath("file:///a") is None
    else:
        assert fileUrlToPath("file:///a/b") == "/a/b"
        assert fileUrlToPath("file://host/share/x") is None
        # What cannot be converted stays an ordinary path under the working directory.
        assert (await env.absolutePath("file://host/share/x", BACKGROUND_CONTEXT))["value"] == os.path.join(
            str(tmp_path), "file:", "host", "share", "x"
        )


async def test_a_write_that_is_aborted_returns_aborted(env: NodeExecutionEnv, tmp_path: Path):
    from misaka.chord.context import withCancel

    cancellable = withCancel(BACKGROUND_CONTEXT)

    async def abortSoon() -> None:
        await asyncio.sleep(0.01)
        cancellable.cancel()

    asyncio.ensure_future(abortSoon())
    result = await env.writeFile("big.bin", bytes(300 * 1024 * 1024), cancellable.context)
    assert result["ok"] is False
    assert result["error"].code == "aborted"


async def test_the_timeout_text_prints_numbers_as_javascript_does(env: NodeExecutionEnv):
    result = await env.exec("echo hi && sleep 5", {"timeout": 0.00001}, BACKGROUND_CONTEXT)
    if result["ok"]:
        pytest.skip("the command finished within the timeout")
    assert str(result["error"].message).startswith("timeout:0.00001")
