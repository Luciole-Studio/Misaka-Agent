"""pi env's ``test/daemon.ts``: the daemon the tests run, plus helpers the Python tests need.

``PI_ENV_DAEMON`` names another daemon to test (pi's CI points it at release builds); otherwise the zip application
``misaka.env.bundle`` builds from ``misaka/env/daemon``. ``oldDaemonCommand()`` starts pi env's ``test/old-daemon.ts``
(``tests/fixtures/old_env_daemon.py``), a daemon released before ``hello.features``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sys
import time
import zipfile

from misaka.env import Connection, packagedDaemon

daemon = os.environ.get("PI_ENV_DAEMON") or packagedDaemon()
if not os.path.exists(daemon):
    raise RuntimeError(f"No pi-env daemon at {daemon}")


def daemonCommand() -> list[str]:
    """The command that starts the daemon. MISAKA fork: a zip application runs under this interpreter; ``PI_ENV_DAEMON``
    may also name a native daemon, which runs as it is."""
    return [sys.executable, daemon] if zipfile.is_zipfile(daemon) else [daemon]


def oldDaemonCommand() -> list[str]:
    """``[process.execPath, join(import.meta.dirname, "old-daemon.ts")]``: a stand-in for a daemon released before
    ``hello.features``, which refuses ``listFiles`` as an unknown operation."""
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "old_env_daemon.py")]


async def closeConnection(connection: Connection) -> None:
    """``connection.close()``, then wait until the daemon process is gone, so the event loop closes without a child it
    still has to reap."""
    session = connection._session
    connection.close()
    if session is None:
        return
    protocol = session.child.protocol
    deadline = time.monotonic() + 10
    while not protocol.exited and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def removeTempDir(directory: str) -> None:
    """``rmSync(directory, { recursive: true, force: true, maxRetries: 50, retryDelay: 100 })``: on Windows a killed
    command's processes can hold the directory for a moment."""
    attempt = 0
    while True:
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if attempt >= 50 or error.errno not in (errno.EBUSY, errno.EPERM, errno.EACCES, errno.ENOTEMPTY):
                raise
            attempt += 1
            time.sleep(0.1)
