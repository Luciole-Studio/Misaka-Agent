"""pi chord's ``test/fixtures/worker.ts``: the child host for the manager tests. FIXTURE_MODE: normal |
ignore-shutdown | fatal.

MISAKA adaptations: a Python script ``forkDriver`` runs with ``sys.executable`` (the tests pass ``PYTHONPATH``, so
``misaka`` and ``tests`` import); Node keeps the process alive while its IPC channel is open, so the script runs until
the channel disconnects or a shutdown exits it; ``process.exit(code)`` ends the event loop and exits with ``code``; the
grandchild is a sleeping Python process.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from misaka.chord import createHost, ipcLink
from misaka.chord.manager import childControl, processChannel
from tests.fixtures.chord_echo import Echo


def spawnGrandchild(file: str) -> None:
    """A sleeping grandchild the test watches; its pid goes to ``file``."""
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(file, "w", encoding="utf-8") as output:
        output.write(str(grandchild.pid))


async def main() -> int:
    loop = asyncio.get_running_loop()
    exit: asyncio.Future[int] = loop.create_future()

    def processExit(code: int, after: float = 0) -> None:
        loop.call_later(after, lambda: exit.done() or exit.set_result(code))

    mode = os.environ.get("FIXTURE_MODE", "normal")
    channel = processChannel()
    assert channel is not None, "the worker runs under forkDriver"
    # Node exits once the IPC channel is gone and nothing else keeps it alive.
    channel.on("disconnect", lambda: processExit(0))
    control = childControl()
    if mode == "fatal":
        control.fatal(Exception("fixture refuses to start"))
        processExit(1, 0.2)
        return await exit
    # Before anything that owns data: the kernel lock (if the manager configured one).
    try:
        await control.lock()
    except Exception:  # noqa: BLE001 - the manager has the fatal report; exit as upstream does
        processExit(1, 0.2)
        return await exit
    host = createHost(
        {
            "self": {"kind": "host"},
            "name": os.environ.get("HOST_NAME", "worker"),
            "upstream": {"link": ipcLink(channel), "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    init = await control.init
    host.serve(
        Echo,
        lambda *_: {
            "variant": "main",
            "echo": lambda text, _ctx: f"{init['greeting']} {text} from {os.getpid()}",
            "pid": lambda _ctx: os.getpid(),
        },
    )
    grandchildFile = os.environ.get("GRANDCHILD_FILE")
    if grandchildFile:
        spawnGrandchild(grandchildFile)

    async def onShutdown() -> None:
        if mode == "ignore-shutdown":
            return
        await host.close()
        processExit(0)

    control.onShutdown(onShutdown)
    control.ready()
    return await exit


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
