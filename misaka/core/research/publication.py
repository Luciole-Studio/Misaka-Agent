"""Recoverable file/SQLite publication; no new artifact layout or database prose store.

The temporary sibling journal keeps the previous bytes until the outer DB transaction
commits. On rollback or explicit resume after a crash, the registered digest decides which
version survived. All operations are serialized by the Board writer, never a Git lock.
"""

import base64
import hashlib
import json
from pathlib import Path

from misaka.core.platform import tasks
from misaka.utils import atomic


def _journal(path):
    return Path(path).with_name(f".{Path(path).name}.research-publish.json")


def recover(con, path):
    journal = _journal(path)
    if not journal.exists():
        return
    with tasks.write_txn(con):
        if not journal.exists():
            return
        intent = json.loads(journal.read_text(encoding="utf-8"))
        if intent["path"] != str(path):
            raise ValueError("Research publication journal path does not match")
        row = con.execute(
            "SELECT sha256 FROM research_artifacts WHERE id=?", (intent["id"],)
        ).fetchone()
        digest = row["sha256"] if row else None
        current = Path(path).read_bytes() if Path(path).exists() else None
        current_sha = (
            hashlib.sha256(current).hexdigest() if current is not None else None
        )
        if current_sha not in {intent["new_sha"], intent["previous_file_sha"]}:
            raise ValueError(
                f"Research publication changed externally during recovery: {path}"
            )
        if digest == intent["new_sha"]:
            if current_sha != digest:
                raise ValueError(f"Committed research publication is missing: {path}")
        elif digest == intent["previous_registered_sha"]:
            previous = intent["previous"]
            if previous is None:
                Path(path).unlink(missing_ok=True)
            else:
                atomic.write_bytes(path, base64.b64decode(previous, validate=True))
        else:
            raise ValueError(
                f"Research publication ownership changed during recovery: {path}"
            )
        journal.unlink()


def prepare(con, path, aid, new_sha):
    """Keep the oldest rollback version even if this transaction rewrites a path twice."""
    journal = _journal(path)
    previous_file = Path(path).read_bytes() if Path(path).exists() else None
    previous_journal = journal.read_bytes() if journal.exists() else None
    if journal.exists():
        intent = json.loads(journal.read_text(encoding="utf-8"))
        if intent["id"] != aid or intent["path"] != str(path):
            raise ValueError(f"Conflicting research publication: {path}")
        intent["new_sha"] = new_sha
    else:
        previous = previous_file
        row = con.execute(
            "SELECT sha256 FROM research_artifacts WHERE id=?", (aid,)
        ).fetchone()
        intent = {
            "id": aid,
            "path": str(path),
            "new_sha": new_sha,
            "previous_registered_sha": row["sha256"] if row else None,
            "previous_file_sha": hashlib.sha256(previous).hexdigest()
            if previous is not None
            else None,
            "previous": base64.b64encode(previous).decode("ascii")
            if previous is not None
            else None,
        }
    atomic.write_text(journal, json.dumps(intent), mode=0o600)
    return previous_file, previous_journal


def restore_attempt(path, checkpoint):
    """Undo just a failed savepoint, preserving earlier writes in the outer transaction."""
    previous, journal = checkpoint
    if previous is None:
        Path(path).unlink(missing_ok=True)
    else:
        atomic.write_bytes(path, previous)
    if journal is None:
        _journal(path).unlink(missing_ok=True)
    else:
        atomic.write_bytes(_journal(path), journal, mode=0o600)
