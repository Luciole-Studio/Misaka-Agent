"""Regression tests for the round-2 check of the durable fixes (VERIFY2-R3): the macOS watcher shares one FSEvents
stream per event loop as libuv does (R3-N1..N5), a wait abandoned on the Session line registers nothing (R3-N6), and
the image decoder refuses exactly what Photon refuses (the IMG-B04 dispute)."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import zlib
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import MemoryStorage
from tests.chat_support import chatSetup, openChat
from tests.durable_support import context
from tests.harness_support import addTool
from tests.test_durable_harness_tools import DONE, calls, tool

macOS = pytest.mark.skipif(sys.platform != "darwin", reason="FSEvents is macOS only")


def writeFile(path: str, content: str, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as file:
        file.write(content)


async def until(predicate: Any, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return bool(predicate())


def fsevents() -> Any:
    from misaka.durable.env.node_watch import _FSEvents

    return _FSEvents.get()


# --- R3-N1: one stream per loop; no process-wide cap of 512 streams ------------------------------------------------


@macOS
async def test_r3_n1_two_hundred_concurrent_watchers_share_one_stream_and_all_report_a_change() -> None:
    from misaka.durable.env.node_watch import NodeFileWatcher

    base = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r3-many-"))
    watchers: list[tuple[Any, list[dict[str, Any]], str]] = []
    try:
        for index in range(200):
            directory = os.path.join(base, f"d{index}")
            os.mkdir(directory)
            target = os.path.join(directory, "AGENTS.md")
            changes: list[dict[str, Any]] = []
            watcher = await NodeFileWatcher.open([{"path": target}], lambda path: path, changes.append, {})
            watchers.append((watcher, changes, target))
        assert [watcher.mode for watcher, _, _ in watchers] == ["native"] * 200
        assert not any(change.get("overflow") for _, changes, _ in watchers for change in changes)
        assert await until(lambda: fsevents().openStreams() == 1)
        # Past macOS's settle rescans: only the stream's own events can report the writes now.
        await asyncio.sleep(1)
        for _, changes, target in watchers:
            changes.clear()
            writeFile(target, "x")
        assert await until(lambda: all(any(target in c.get("paths", []) for c in changes) for _, changes, target in watchers))
    finally:
        for watcher, _, _ in watchers:
            await watcher.close(BACKGROUND_CONTEXT)
    assert await until(lambda: fsevents().openStreams() == 0)


# --- R3-N2: a forked child does not touch the parent's streams --------------------------------------------------


@macOS
async def test_r3_n2_a_forked_child_exits_normally_with_a_stream_open_in_its_parent() -> None:
    from misaka.durable.env.node_watch import _FSEvents, _fsWatch

    directory = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r3-fork-"))
    watcher = _fsWatch(directory, False, lambda name: None, lambda: None)
    try:
        # The stream starts on the dispatch queue.
        await asyncio.sleep(0.3)
        import warnings

        parent = _FSEvents.instance
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a multi-threaded process
            pid = os.fork()  # windows-footgun: ok - the test runs on macOS only
        if pid == 0:
            code = 3
            try:
                # What a normal exit runs for the watcher (not the other exit handlers: they would clean up the
                # parent's files). It must not reach the parent's CoreFoundation objects, which crashed the child.
                assert parent is not None
                parent._closeAll()
                # libuv watches a directory with kqueue in such a child; nothing reaches CoreFoundation again.
                code = 0 if _FSEvents.forked and _FSEvents.instance is None else 4
            finally:
                os._exit(code)
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
    finally:
        watcher.close()


# --- R3-N3: a loop closed without closing its watchers stops watching -------------------------------------------


@macOS
def test_r3_n3_a_watch_left_open_when_its_loop_closed_releases_the_stream() -> None:
    from misaka.durable.env.node_watch import _fsWatch

    directory = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r3-leak-"))

    async def leave() -> None:
        _fsWatch(directory, True, lambda name: None, lambda: None)
        await asyncio.sleep(0.3)

    before = fsevents().openStreams()
    asyncio.run(leave())
    # The next event for it finds its loop closed.
    writeFile(os.path.join(directory, "f"), "x")
    deadline = time.monotonic() + 5
    while fsevents().openStreams() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fsevents().openStreams() == before


# --- R3-N4 / R3-N5: names as libuv gives them ----------------------------------------------------------------------


@macOS
async def test_r3_n4_an_event_about_the_watched_directory_itself_names_its_base_name() -> None:
    from misaka.durable.env.node_watch import _fsWatch

    directory = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r3-self-"))
    names: list[str | None] = []
    watcher = _fsWatch(directory, False, names.append, lambda: None)
    try:
        await asyncio.sleep(0.4)
        os.utime(directory)
        assert await until(lambda: os.path.basename(directory) in names, 3)
    finally:
        watcher.close()


def _caseInsensitive(directory: str) -> bool:
    probe = os.path.join(directory, "CaseProbe")
    os.mkdir(probe)
    try:
        return os.path.exists(os.path.join(directory, "caseprobe"))
    finally:
        os.rmdir(probe)


@macOS
@pytest.mark.parametrize("form", ["case", "unicode"])
async def test_r3_n5_a_watched_path_spelled_unlike_the_disk_still_names_entries(form: str) -> None:
    from misaka.durable.env.node_watch import _fsWatch

    base = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r3-name-"))
    if form == "case":
        if not _caseInsensitive(base):
            pytest.skip("case-sensitive volume")
        onDisk, watched = "CaseDir", "casedir"
    else:
        onDisk, watched = unicodedata.normalize("NFD", "café"), unicodedata.normalize("NFC", "café")
    os.mkdir(os.path.join(base, onDisk))
    if not os.path.isdir(os.path.join(base, watched)):
        pytest.skip("the volume does not equate these spellings")
    names: list[str | None] = []
    watcher = _fsWatch(os.path.join(base, watched), False, names.append, lambda: None)
    try:
        await asyncio.sleep(0.4)
        writeFile(os.path.join(base, onDisk, "f.txt"), "1")
        assert await until(lambda: "f.txt" in names, 3)
        assert None not in names
    finally:
        watcher.close()


# --- R3-N6: a wait given up while queued on the line registers nothing ---------------------------------------------


async def test_r3_n6_polling_wait_for_task_with_a_zero_timeout_holds_no_waiter() -> None:
    setup = chatSetup()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_args: Any, _api: Any, _context: Any) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"output": [{"type": "text", "text": "ok"}]}

    addTool(setup.registry, tool("slow", slow))
    setup.faux.setResponses([calls(("slow", {}, "c1")), DONE])
    storage = MemoryStorage()
    opened = await openChat(storage, setup)
    submission = await opened.root.submit({"type": "input", "content": "go"}, context)
    await asyncio.wait_for(started.wait(), 10)
    taskId = (await storage.scanTasks({"kind": "pi.tool"}, 10, None, context))["items"][0]["id"]
    waiters = opened.harness._tasks._taskWaiters
    for _ in range(100):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(opened.harness.waitForTask(taskId, context), 0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(submission.wait(context), 0)
    await asyncio.sleep(0.05)
    assert taskId not in waiters.keys()  # noqa: SIM118 - `Waiters.keys()` is its own method
    assert opened.harness._submissions._waiters.keys() == []
    release.set()
    await asyncio.wait_for(submission.wait(context), 10)
    await opened.harness.close(context)


# --- IMG-B04: Photon's own bound (image 0.24.9's 512 MiB decode limit, then its WebAssembly memory) -----------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _pngHeader(width: int, height: int, depth: int, colorType: int, *extra: bytes) -> bytes:
    """IHDR and the chunks before image data; enough to open, never decoded."""
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colorType, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"".join(extra) + _chunk(b"IDAT", zlib.compress(b"\0")) + _chunk(b"IEND", b"")


def _jpegHeader(mode: str, width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new(mode, (16, 16)).save(buffer, "JPEG")
    data = bytearray(buffer.getvalue())
    offset = 2
    while data[offset + 1] not in (0xC0, 0xC1, 0xC2):
        offset += 2 + struct.unpack(">H", data[offset + 2 : offset + 4])[0]
    data[offset + 5 : offset + 9] = struct.pack(">HH", height, width)
    return bytes(data)


def _gifHeader(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("P", (4, 4)).save(buffer, "GIF")
    data = bytearray(buffer.getvalue())
    data[6:10] = struct.pack("<HH", width, height)
    return bytes(data)


PALETTE, TRANSPARENT = _chunk(b"PLTE", b"\0\0\0"), _chunk(b"tRNS", b"\0")


BOUNDS = [
        # Measured on @silvia-odwyer/photon 0.3.3 under node 26.
        ("rgba16 at 512 MiB", _pngHeader(8192, 8192, 16, 6), True),
        ("rgba16 over", _pngHeader(8192, 8193, 16, 6), False),
        ("rgb8 over", _pngHeader(13378, 13377, 8, 2), False),
        ("palette, no tRNS: RGB", _pngHeader(11586, 11585, 8, 3, PALETTE), True),
        ("palette with tRNS: RGBA, over", _pngHeader(11586, 11585, 8, 3, PALETTE, TRANSPARENT), False),
        ("grey with tRNS: grey and alpha, over", _pngHeader(16385, 16384, 8, 0, _chunk(b"tRNS", b"\0\0")), False),
        ("16-bit grey with tRNS, over", _pngHeader(11586, 11585, 16, 0, _chunk(b"tRNS", b"\0\0")), False),
        # Derived, not measured (more than 4 GB): 8-bit grey passes the decode limit, then must fit 4 GiB of WebAssembly
        # memory with its RGBA copy and that copy's copy.
        ("rgb8 at the limit", _pngHeader(13377, 13377, 8, 2), True),
        ("grey8 that fits WebAssembly memory", _pngHeader(20000, 20000, 8, 0), True),
        ("grey8 under the decode limit but over the memory", _pngHeader(23170, 23170, 8, 0), False),
        ("jpeg rgb over", _jpegHeader("RGB", 13378, 13377), False),
        ("jpeg rgb under", _jpegHeader("RGB", 9000, 9000), True),
        ("jpeg grey over", _jpegHeader("L", 23171, 23171), False),
        ("jpeg cmyk over (decoded as RGB)", _jpegHeader("CMYK", 13378, 13377), False),
        ("gif over (RGBA screen)", _gifHeader(11586, 11585), False),
]


@pytest.mark.parametrize(("label", "data", "decodes"), BOUNDS, ids=[case[0] for case in BOUNDS])
def test_img_b04_photon_decodes_exactly_what_image_0_24_and_its_memory_allow(label: str, data: bytes, decodes: bool) -> None:
    from misaka.durable import images

    images._photonDecoding.active = True
    try:
        with Image.open(BytesIO(data)) as opened:
            assert images._photonDecodes(data, opened) is decodes, label
    finally:
        images._photonDecoding.active = False


async def test_img_b04_a_palette_png_with_transparency_over_photons_limit_is_not_prepared() -> None:
    from misaka.durable.images import createPhotonImages

    # 11586 x 11585 decodes to RGBA (4 bytes a pixel) because of tRNS: 537 MB, over image 0.24's 512 MiB.
    compressor = zlib.compressobj(9)
    row = b"\0" * (11586 + 1)
    pixels = b"".join(compressor.compress(row) for _ in range(11585)) + compressor.flush()
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 11586, 11585, 8, 3, 0, 0, 0))
    data = b"\x89PNG\r\n\x1a\n" + ihdr + PALETTE + TRANSPARENT + _chunk(b"IDAT", pixels) + _chunk(b"IEND", b"")
    started = time.perf_counter()
    limits = {"maxWidth": 2000, "maxHeight": 2000, "maxBytes": 4_718_592}
    assert await createPhotonImages().prepare(data, "image/png", limits) is None  # type: ignore[arg-type]
    # Refused from the header, before any pixel is decoded.
    assert time.perf_counter() - started < 2


def test_img_b04_other_pillow_users_keep_the_guard() -> None:
    import misaka.durable.images  # noqa: F401 - installs the gated check

    events: list[str] = []

    def other() -> None:
        try:
            Image.open(BytesIO(_pngHeader(20000, 20000, 8, 0)))
            events.append("opened")
        except Image.DecompressionBombError:
            events.append("refused")

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    assert events == ["refused"]
