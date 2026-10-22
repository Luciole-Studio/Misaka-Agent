"""A child for ``tests/test_chord_manager_fx4.py``'s fork-driver cases. CHILD_MODE: sleep | print | env.

- sleep: sleeps a minute (a long-lived child the test kills).
- print: prints ``héllo`` to stdout and exits.
- env: adopts its control channel (``childControl()``), then prints one JSON line: whether ``CHORD2_IPC_FD`` is still
  in its environment, and what a grandchild it spawns sees of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time


async def adopt() -> None:
    from misaka.chord.manager import childControl

    childControl()


def env() -> None:
    asyncio.run(adopt())
    grandchild = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('CHORD2_IPC_FD'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    print(json.dumps({"inEnv": "CHORD2_IPC_FD" in os.environ, "grandchild": grandchild.stdout.strip()}), flush=True)


if __name__ == "__main__":
    mode = os.environ.get("CHILD_MODE", "sleep")
    if mode == "print":
        sys.stdout.buffer.write("héllo\n".encode())
    elif mode == "env":
        env()
    else:
        time.sleep(60)
