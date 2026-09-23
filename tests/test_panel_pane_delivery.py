"""Pane input is delivered through a per-pane write queue that waits for the program.

A terminal's input queue holds about 1 KiB on macOS; anything longer -- a paste of a few
hundred CJK characters, a Last Order note to a Sister -- only enters the pane as fast as the
program reads. The daemon used to write until the queue was full and raise, which cut the
bracketed-paste closer off pastes and the Enter off notes (2026-09-18, B1). herdr blocks a
per-pane writer thread on the pty instead; the daemon drains on writability and lets the
caller wait, with a deadline, for the message to enter the pane."""
import asyncio
import os

import pytest

from misaka.ui.panel import daemon as d


def _pipe():
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    os.set_blocking(writer, False)
    return reader, writer


def _pane(writer):
    pane = d.Pane("p1", "probe", [], "/")
    pane.fd = writer
    return pane


def _daemon(tmp_path):
    return d.Daemon(str(tmp_path / "net.sock"), str(tmp_path / "net.json"))


async def _read_all(reader, expected):
    got = bytearray()
    while len(got) < expected:
        try:
            got += os.read(reader, 65536)
        except BlockingIOError:
            await asyncio.sleep(0.01)
    return bytes(got)


def _read_available(reader):
    got = bytearray()
    while True:
        try:
            got += os.read(reader, 65536)
        except BlockingIOError:
            return bytes(got)


async def test_a_message_larger_than_the_queue_lands_whole_once_the_program_reads(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    data = bytes(range(256)) * 1200     # 300 KiB: several times the pipe buffer, hundreds of pty queues
    try:
        async def slow_program():
            await asyncio.sleep(0.2)     # busy repainting, as a CPython TUI is after a paste
            return await _read_all(reader, len(data))

        program = asyncio.create_task(slow_program())
        assert await daemon._deliver(pane, data, timeout=5, drop_on_timeout=True) == {"sent": True}
        assert await program == data
        assert pane.writes == [] and not pane.writer_armed
    finally:
        os.close(reader)
        os.close(writer)


async def test_pane_send_reports_and_drops_what_the_program_never_read(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    data = b"x" * (200 * 1024)
    try:
        with pytest.raises(RuntimeError) as failure:
            await daemon._deliver(pane, data, timeout=0.2, drop_on_timeout=True)
        text = str(failure.value)
        assert text.startswith("Pane input buffer is full: ")
        assert "bytes entered the pane within 0.2s and the rest was dropped" in text
        assert pane.writes == [] and not pane.writer_armed
        landed = _read_available(reader)
        assert 0 < len(landed) < len(data)          # what entered stays in; nothing follows it
        assert f"{len(landed)} of {len(data)} bytes" in text
    finally:
        os.close(reader)
        os.close(writer)


async def test_pane_input_stays_queued_and_arrives_when_the_program_reads(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    data = bytes(range(256)) * 400      # 100 KiB paste through the panel
    try:
        result = await daemon._deliver(pane, data, timeout=0.1, drop_on_timeout=False)
        assert result["sent"] is True and result["queued"] > 0
        assert pane.writes and pane.writer_armed
        assert await _read_all(reader, len(data)) == data
        await asyncio.sleep(0.05)       # the writer callback retires itself once the queue is empty
        assert pane.writes == [] and not pane.writer_armed
    finally:
        os.close(reader)
        os.close(writer)


async def test_messages_keep_their_order_across_a_full_queue(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    first, second = b"a" * (100 * 1024), b"b" * 10
    try:
        await daemon._deliver(pane, first, timeout=0.05, drop_on_timeout=False)
        assert pane.writer_armed
        later = asyncio.create_task(daemon._deliver(pane, second, timeout=5, drop_on_timeout=True))
        assert await _read_all(reader, len(first) + len(second)) == first + second
        assert await later == {"sent": True}
    finally:
        os.close(reader)
        os.close(writer)


async def test_a_pane_that_exits_fails_what_was_still_queued(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    try:
        await daemon._deliver(pane, b"y" * (200 * 1024), timeout=0.05, drop_on_timeout=False)
        item = pane.writes[0]
        daemon._fail_writes(pane, "the pane's program exited before reading it")
        assert pane.writes == [] and not pane.writer_armed
        with pytest.raises(RuntimeError, match="exited before reading it"):
            item.future.result()
    finally:
        os.close(reader)
        os.close(writer)


async def test_the_queue_has_a_ceiling(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    try:
        await daemon._deliver(pane, b"z" * d.WRITE_QUEUE_LIMIT, timeout=0.05, drop_on_timeout=False)
        queued = sum(len(item.view) for item in pane.writes)   # what the pipe did not take
        assert 0 < queued < d.WRITE_QUEUE_LIMIT
        with pytest.raises(RuntimeError, match="Pane input queue is full"):
            await daemon._deliver(pane, b"m" * (d.WRITE_QUEUE_LIMIT - queued + 1), timeout=0.05,
                                  drop_on_timeout=False)
    finally:
        os.close(reader)
        os.close(writer)


async def test_pane_send_delivers_text_and_enter_as_one_message(tmp_path):
    daemon = _daemon(tmp_path)
    reader, writer = _pipe()
    pane = _pane(writer)
    daemon.panes[pane.id] = pane
    try:
        pending = daemon._api("pane.send", {"id": "p1", "text": "hello", "enter": True})
        assert asyncio.iscoroutine(pending)      # _serve_client awaits delivery
        assert await pending == {"sent": True}
        assert _read_available(reader) == b"hello\r"
    finally:
        os.close(reader)
        os.close(writer)
