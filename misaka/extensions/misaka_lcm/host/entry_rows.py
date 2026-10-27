"""Which session entry each stored row came from, recorded as upstream writes it.

Upstream's store knows a row by content; a session knows an entry by id. The host used to
recover one from the other by matching content (``reconcile._get_store_id_map_for_messages``),
which upstream itself calls best effort: identical rows are dealt out by count, so an extra or
missing copy of an empty assistant reply re-deals every later one. A checkpoint's source then
mapped to no row, and the turn failed (2026-10-25 card X1, "LCM checkpoint source entries are
absent"; 2026-10-26 Last Order, "frontier contains sources outside the transcript archive").

Here the binding is recorded when the row is written: every message the host hands upstream
carries ``KEY`` = (entry id, position within the entry), upstream leaves the key alone, and
the store write upstream discards the ids of is wrapped to keep them. The bindings live in a
file of the host's own beside the store, and go with it. A row with no binding -- written by a
process that predates this, or lost between the write and the record -- is matched by content
as before.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

KEY = "_misaka_entry"
_LOCK = threading.Lock()
_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def tag(entry_id, messages):
    """``messages`` of one entry, each carrying its binding key."""
    return [{**message, KEY: (str(entry_id), position)} for position, message in enumerate(messages)]


def _connection(store):
    path = str(getattr(store, "db_path", "") or "")
    key = path if path and path != ":memory:" else f"memory:{id(store)}"
    with _LOCK:
        con = _CONNECTIONS.get(key)
        if con is None:
            target = ":memory:" if key.startswith("memory:") else str(Path(path).with_name("entry-rows.db"))
            con = sqlite3.connect(target, timeout=10, check_same_thread=False, isolation_level=None)
            if target != ":memory:":
                con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS rows (store_id INTEGER PRIMARY KEY, entry_id TEXT NOT NULL, "
                        "position INTEGER NOT NULL)")
            _CONNECTIONS[key] = con
        return con


def forget(store) -> None:
    """Close the bindings of a store that is going away (its directory is about to be removed)."""
    path = str(getattr(store, "db_path", "") or "")
    key = path if path and path != ":memory:" else f"memory:{id(store)}"
    with _LOCK:
        con = _CONNECTIONS.pop(key, None)
    if con is not None:
        con.close()


def record(store, store_ids, messages) -> None:
    """Keep the binding of each row just written. A failure costs the binding, not the row."""
    rows = [(int(store_id), str(message[KEY][0]), int(message[KEY][1]))
            for store_id, message in zip(store_ids, messages) if isinstance(message.get(KEY), tuple)]
    if not rows:
        return
    try:
        con = _connection(store)
        with _LOCK:
            con.executemany("INSERT OR REPLACE INTO rows (store_id, entry_id, position) VALUES (?, ?, ?)", rows)
    except sqlite3.Error:
        logger.warning("LCM could not record which entries %d rows came from; they are matched by content.",
                       len(rows), exc_info=True)


def bound(store, store_ids):
    """``({entry id: {position: store id}}, every bound id)`` for ``store_ids``; at one position
    the earliest row, which a later duplicate write does not displace."""
    ids = sorted({int(store_id) for store_id in store_ids})
    if not ids:
        return {}, set()
    con = _connection(store)
    found = {}
    with _LOCK:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update((row[0], (row[1], row[2])) for row in con.execute(
                f"SELECT store_id, entry_id, position FROM rows WHERE store_id IN ({','.join('?' * len(chunk))})",
                chunk))
    result: dict[str, dict[int, int]] = {}
    for store_id in ids:                                     # ascending: the first row at a position wins
        if store_id in found:
            entry_id, position = found[store_id]
            result.setdefault(entry_id, {}).setdefault(position, store_id)
    return result, set(found)
