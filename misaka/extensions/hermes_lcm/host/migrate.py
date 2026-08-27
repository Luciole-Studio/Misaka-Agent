"""Rebuild a pre-port LCM database in upstream's schema.

The mini implementation and upstream both call their tables ``messages`` and
``summary_nodes`` while meaning different things by them, so there is no in-place
upgrade to attempt and none is attempted: the original file is backed up, a *new*
database is built beside it, the originals are replayed into it through upstream's own
ingest, the row counts are checked session by session, and only then does the new file
take the old one's name.

Only the original messages move. The old summaries stay in the backup: they were
produced by a different summariser against a different DAG, and the ported engine
re-derives its own from the originals the first time it compacts.

Giving a rebuilt file the old one's name has one hazard worth spelling out, because
SQLite loses quietly rather than loudly: a write-ahead log is found by *name*, and its
frames carry no proof of which database they came from. Leave the pre-port
``lcm.db-wal`` beside a freshly rebuilt ``lcm.db`` and the next process to open it
replays the old database over the new one -- integrity_check says ``ok`` and the
migration has silently undone itself. So the log is folded into the file before the
rebuild and the sidecars are removed after the rename, and a database another process
still has open is refused outright: that process would recreate the log under the new
database a moment later, and its own writes are going to an inode that no longer has a
name.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

_LEGACY_COLUMNS = "store_id, session_id, source, role, content, tool_call_id, tool_calls, tool_name, timestamp, observed_at"


def _rows(db_path: str):
    """Every original message in the pre-port database, oldest first."""
    # Not `with sqlite3.connect(...)`: that context manager commits a transaction, it
    # does not close the connection, and a reader left open is a warning under -W error.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield from conn.execute(f"SELECT {_LEGACY_COLUMNS} FROM messages ORDER BY store_id")
    finally:
        conn.close()


def _message(row) -> dict:
    """One legacy row in the shape upstream's store ingests."""
    raw_calls = row["tool_calls"]
    return {
        "role": row["role"] or "unknown",
        "content": row["content"],
        "tool_calls": json.loads(raw_calls) if raw_calls else None,
        "tool_call_id": row["tool_call_id"],
        "tool_name": row["tool_name"],
        "timestamp": row["observed_at"] or row["timestamp"],
    }


def sessions(db_path: str) -> dict[str, int]:
    """Original-message counts per session. Both schemas answer this one the same way."""
    counted: dict[str, int] = {}
    for row in _rows(db_path):
        counted[row["session_id"]] = counted.get(row["session_id"], 0) + 1
    return counted


def _quiesce(db_path: str) -> str:
    """Fold the write-ahead log into the file. Returns why the database is not free.

    The checkpoint is what makes the file self-contained; closing the connection then
    deletes the log and its shared-memory index -- unless someone else still has the
    database open, which is exactly the condition that makes migrating it unsafe. So a
    surviving ``-shm`` is the probe: no polling, no lock file, just SQLite's own
    bookkeeping answering the only question that matters.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as exc:
        return f"the database could not be opened for checkpointing: {exc}"
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        return f"the write-ahead log could not be checkpointed: {exc}"
    finally:
        conn.close()
    if Path(f"{db_path}-shm").exists():
        return ("another process still has this database open; stop every misaka "
                "session (and any panel daemon) before migrating")
    return ""


def plan(db_path: str) -> dict:
    """What a migration would move: per-session message counts, without writing."""
    from .switch import schema

    path = Path(db_path)
    if not path.is_file():
        return {"database": str(path), "legacy": False, "sessions": {}, "messages": 0,
                "note": "no LCM database exists yet; nothing to migrate"}
    if schema(str(path)) != "mini":
        return {"database": str(path), "legacy": False, "sessions": {}, "messages": 0,
                "note": "this database is already in the ported engine's schema"}
    counted = sessions(str(path))
    return {"database": str(path), "legacy": True, "sessions": counted,
            "messages": sum(counted.values()), "note": ""}


def run(db_path: str) -> dict:
    """Migrate for real. Returns the plan plus the backup path and the verified counts."""
    from ..maintenance import backup
    from ..vendor.store import MessageStore

    result = plan(db_path)
    if not result["legacy"]:
        return {**result, "applied": False, "backup": None}

    snapshot, error = backup(db_path)
    if error:
        return {**result, "applied": False, "backup": None, "note": error}
    busy = _quiesce(db_path)
    if busy:
        return {**result, "applied": False, "backup": snapshot, "note": busy}

    # Nanoseconds, not a second-resolution stamp: an attempt killed outright leaves its
    # half-built file behind, and a retry that reused the name would append to it and
    # then reject itself for counting double.
    rebuilt = Path(f"{db_path}.rebuilt-{time.time_ns()}")
    try:
        store = MessageStore(rebuilt)
        try:
            batch: list[dict] = []
            current: tuple[str, str] | None = None
            for row in _rows(db_path):
                key = (row["session_id"], row["source"] or "")
                if key != current and batch:
                    store.append_batch(current[0], batch, source=current[1])
                    batch = []
                current = key
                batch.append(_message(row))
            if batch and current is not None:
                store.append_batch(current[0], batch, source=current[1])
            migrated = {session: store.get_session_count(session) for session in result["sessions"]}
        finally:
            store.close()

        if migrated != result["sessions"]:
            return {**result, "applied": False, "backup": snapshot, "migrated": migrated,
                    "note": "per-session counts did not match; the original database is untouched"}

        os.replace(rebuilt, db_path)
        # The rename moved one file; the pre-port log and index still answer to the new
        # database's name, and SQLite would replay them over it. Nothing in them is lost:
        # the backup read through them, and the checkpoint folded them into the file the
        # rebuild then read.
        for sidecar in ("-wal", "-shm"):
            Path(f"{db_path}{sidecar}").unlink(missing_ok=True)
    finally:
        # A no-op once the rename has consumed it; on any failure, the half-built file.
        rebuilt.unlink(missing_ok=True)
    return {**result, "applied": True, "backup": snapshot, "migrated": migrated}
