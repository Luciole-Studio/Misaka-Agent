"""Translated from pi's ``coding-agent/test/clipboard-command.test.ts`` (v1.1.0). The child is this Python where pi's is
Node."""

from __future__ import annotations

import asyncio
import sys

from misaka.utils.clipboard_command import runClipboardCommand


async def test_preserves_binary_output_and_distinguishes_empty_success_from_failure():
    assert await runClipboardCommand(sys.executable, ["-c", "import sys; sys.stdout.buffer.write(bytes([0, 255, 10]))"]) == bytes(
        [0, 255, 10]
    )
    assert await runClipboardCommand(sys.executable, ["-c", ""]) == b""
    assert await runClipboardCommand(sys.executable, ["-c", "raise SystemExit(1)"]) is None
    assert await runClipboardCommand("pi-clipboard-command-does-not-exist", []) is None


async def test_sends_unicode_input_to_clipboard_writers():
    script = "import sys; text = sys.stdin.buffer.read().decode('utf-8'); raise SystemExit(0 if text == 'café 日本語' else 1)"
    assert await runClipboardCommand(sys.executable, ["-c", script], {"input": "café 日本語"}) == b""


async def test_times_out_without_blocking_the_event_loop():
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.ensure_future(tick())
    try:
        assert await runClipboardCommand(sys.executable, ["-c", "import time; time.sleep(30)"], {"timeoutMs": 200}) is None
        assert ticks > 5
    finally:
        ticker.cancel()


async def test_rejects_output_above_the_buffer_limit():
    assert (
        await runClipboardCommand(sys.executable, ["-c", "import sys; sys.stdout.buffer.write(bytes(1024))"], {"maxBufferBytes": 16})
        is None
    )
