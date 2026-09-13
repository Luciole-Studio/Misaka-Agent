"""Render a document once, however many times it is paged through.

Both readers page: ``doc_read`` walks a corpus document by node or page range, and ``read``
walks a working-directory file by ``offset``. Each page is a fresh call, and re-parsing a
50-sheet workbook per page -- recalculating its formulas on the way -- is the difference
between paging a large document and not paging it at all. FrontierAgent caches the same
thing for the same reason (``plugins/tools/_reader_core.py:534 _render_cached``).

Two rules make it safe to be wrong:

*Every failure is a miss.* An unwritable cache directory, a home that cannot be resolved,
a half-written entry -- all of them return the freshly rendered text. The cache buys speed
and may never cost a document, so nothing here raises on the caller's behalf. The broad
``except`` is the one ``core/web/cache.py:_cache_dir`` documents: ``expand_tilde_path``
reaches ``Path.home()``, which is a ``RuntimeError`` rather than an ``OSError`` when HOME
is unset and the uid has no passwd entry.

*A refusal is not a rendering.* A renderer that raises is propagated and nothing is
stored, or one transient parse failure would become this file's permanent answer.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from misaka.config import expand_tilde_path
from misaka.config.product import CFG
from misaka.utils import atomic

# A research run renders every document a card touches. The bound is generous -- an entry
# is markdown, tens of KiB -- and exists so a long-lived corpus does not accumulate one
# file per document ever read. Eviction is oldest-first by mtime, which for this store is
# also least-recently-rendered.
MAX_ENTRIES = 256


def _directory(workspace=None):
    """The cache directory, created on demand; ``None`` when it cannot be.

    Read from ``CFG`` on every call rather than expanded once at import: a path frozen at
    import time still points at the developer's own cache after a test moves ``HOME``, and
    ``CFG`` is what the suite redirects.
    """
    try:
        directory = (Path(workspace).resolve() / ".office-cache" if workspace is not None
                     else Path(expand_tilde_path(str(CFG["office_cache"]))))
        if workspace is not None:
            directory.resolve().relative_to(Path(workspace).resolve())
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception:  # noqa: BLE001 - no cache directory is a miss, never a raise
        return None


def _key(path):
    """``(realpath, size, mtime_ns)`` digested, or ``None`` when the file cannot be stat'd.

    Size is in the key as well as the timestamp because a same-second edit that changes a
    document's length is otherwise served the previous rendering -- ``mtime`` has one-second
    resolution on filesystems that still exist, and an ``os.utime`` restore has none at all.
    """
    try:
        real = os.path.realpath(path)
        stat = os.stat(real)
    except OSError:
        return None
    material = f"{real}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _evict(directory):
    """Keep the store under :data:`MAX_ENTRIES`, oldest first. Best effort."""
    try:
        entries = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in entries[: max(0, len(entries) - MAX_ENTRIES)]:
        try:
            stale.unlink()
        except OSError:
            pass


def render_cached(path, render, *, workspace=None):
    """``render()``'s text for ``path``, from the store when it is there.

    ``render`` takes no arguments and returns the whole rendering as one string. It is
    called at most once per (path, size, mtime); anything it raises reaches the caller
    unchanged and is not stored.
    """
    directory, key = _directory(workspace), _key(path)
    if directory is None or key is None:
        return render()
    entry = directory / f"{key}.md"
    try:
        return entry.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        # Absent, unreadable, or not the text it was written as: render, and let the write
        # below replace whatever was there.
        pass
    text = render()
    try:
        atomic.write_text(str(entry), text)
        _evict(directory)
    except (OSError, ValueError):
        pass
    return text
