"""MISAKA fork: the daemon as one Python zip application.

pi ships the Rust daemon in its package's ``bin/``, one native binary per remote system (``npm run build:daemon``). Here one
zip application of ``misaka.env.daemon`` serves every system; the remote runs it with its own Python 3.9 or later. The
archive is the same byte for byte every time (stored entries, fixed timestamps and attributes, LF line endings), so its
SHA-256 names the deployed file as the binary's does upstream.
"""

from __future__ import annotations

import atexit
import io
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

DAEMON_PACKAGE = "pi_env"
SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = b"import sys\n\nfrom pi_env.main import main\n\nmain(sys.argv)\n"

_lock = threading.Lock()
_built: str | None = None


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def daemonArchive() -> bytes:
    """The zip application: ``pi_env/`` (the daemon's modules) and a ``__main__.py`` that runs it, after a shebang line."""
    source = Path(__file__).parent / "daemon"
    buffer = io.BytesIO()
    buffer.write(SHEBANG)
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(source.rglob("*.py"), key=lambda path: path.relative_to(source).as_posix()):
            name = f"{DAEMON_PACKAGE}/{path.relative_to(source).as_posix()}"
            archive.writestr(_entry(name), path.read_bytes().replace(b"\r\n", b"\n"))
        archive.writestr(_entry("__main__.py"), MAIN)
    return buffer.getvalue()


def daemonPath() -> str:
    """The zip application as a file, written once per process into a directory only this user can read."""
    global _built
    with _lock:
        if _built is None or not os.path.exists(_built):
            directory = tempfile.mkdtemp(prefix="misaka-pi-env-")
            atexit.register(shutil.rmtree, directory, True)
            path = os.path.join(directory, "pi-env.pyz")
            with open(path, "wb") as file:
                file.write(daemonArchive())
            _built = path
        return _built


__all__ = ["DAEMON_PACKAGE", "daemonArchive", "daemonPath"]
