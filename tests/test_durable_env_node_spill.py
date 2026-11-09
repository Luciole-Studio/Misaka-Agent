"""pi durable's ``test/env-node-spill.test.ts``.

Upstream mocks ``node:fs.createWriteStream`` so the spill stream is slow and backpressured at once. The Python spill stream
writes in a thread behind a high-water mark, so the same two conditions come from patching those two names.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import getOrThrow
from misaka.durable.env import node as envNode
from misaka.durable.env.node import NodeExecutionEnv

# Longer than the shell's post-exit stdio grace period plus the descendant's delayed write.
SPILL_WRITE_DELAY_S = 0.6


@pytest.fixture
def root() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-env-spill-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="the command needs a POSIX shell with tr and a background job")
async def test_keeps_inherited_stdio_open_past_the_exit_grace_period_while_a_spill_write_is_pending(root, monkeypatch):
    spill = {"rejectedWrites": 0}
    writeChunk = envNode._writeSpillChunk
    write = envNode._SpillStream.write

    def slowWrite(file: Any, chunk: bytes) -> None:
        time.sleep(SPILL_WRITE_DELAY_S)
        writeChunk(file, chunk)

    def countingWrite(self: Any, chunk: bytes) -> bool:
        accepted = write(self, chunk)
        if not accepted:
            spill["rejectedWrites"] += 1
        return accepted

    # Make the spill stream deterministically slow and immediately backpressured.
    monkeypatch.setattr(envNode, "SPILL_HIGH_WATER_MARK", 1)
    monkeypatch.setattr(envNode, "_writeSpillChunk", slowWrite)
    monkeypatch.setattr(envNode._SpillStream, "write", countingWrite)

    env = NodeExecutionEnv({"cwd": root})
    # The shell exits after the first chunk crosses the spill threshold and backpressures the spill. A background
    # descendant retains stdout and writes after the post-exit grace period. Without the pending-spill guard,
    # settlement destroys stdout before that descendant output is read.
    command = "printf '%020d' 0 | tr 0 a; (sleep 0.2; printf '%01000d' 0 | tr 0 b) &"

    output: list[str] = []
    result = getOrThrow(
        await env.exec(
            command,
            {"spill": {"afterBytes": 10, "afterLines": 10}, "onOutput": lambda text, *_: output.append(text)},
            BACKGROUND_CONTEXT,
        )
    )

    assert spill["rejectedWrites"] > 0
    assert len("".join(output)) == 1020
    assert result.get("spillPath") is not None
    assert getOrThrow(await env.readTextFile(result["spillPath"], BACKGROUND_CONTEXT)) == "a" * 20 + "b" * 1000
    shutil.rmtree(result["spillPath"].rsplit("/", 1)[0], ignore_errors=True)
