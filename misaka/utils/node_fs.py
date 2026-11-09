"""What MISAKA needs of Node's ``fs`` module beyond the standard library: directory listings in Node's order."""

from __future__ import annotations

import os
import sys


def readdirEntries(path: str) -> list[os.DirEntry[str]]:
    """``fs.readdirSync(path, { withFileTypes: true })``: libuv sorts the names by their bytes on POSIX, while on
    Windows it keeps the file system's order, which is what ``os.scandir`` gives. Raises as ``os.scandir`` does."""
    with os.scandir(path) as iterator:
        entries = list(iterator)
    if sys.platform != "win32":
        entries.sort(key=lambda entry: os.fsencode(entry.name))
    return entries


__all__ = ["readdirEntries"]
