"""The original /lcm grammar plus isolated operator CLIs with captured output."""
from __future__ import annotations

import shlex
import subprocess
import sys

from . import execution, operations, operators

USAGE = "/lcm help"


def run(argv: str | list[str], ctx=None) -> str:
    args = shlex.split(argv) if isinstance(argv, str) else argv
    if args and args[0] in operators.COMMANDS:
        # stdout belongs to the terminal owner, so never redirect process-global
        # sys.stdout in a worker thread shared with other sessions.
        with subprocess.Popen([sys.executable, "-m", operators.__name__, *args],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                while True:
                    execution.check_cancelled()
                    try:
                        stdout, stderr = process.communicate(timeout=0.05)
                        return stdout + stderr
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
    return operations.command(" ".join(args), ctx)
