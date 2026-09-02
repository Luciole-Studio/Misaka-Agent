"""Current-branch JSONL export without mutating the live session."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from misaka.core.session_manager import (
    CURRENT_SESSION_VERSION,
    SessionManager,
    _dump_json,
)
from misaka.utils import atomic
from misaka.utils.paths import resolve_path

type CreateTrailingEntries = Callable[[str | None, str], Sequence[object] | None]


def export_session_to_jsonl(
    session_manager: SessionManager,
    output_path: str | None = None,
    create_trailing_entries: CreateTrailingEntries | None = None,
) -> str:
    """Write the current branch plus optional export-only trailing entries."""
    resolved_output = output_path or (
        f"session-{datetime.now(UTC).isoformat(timespec='milliseconds').replace(':', '-').replace('.', '-')}.jsonl"
    )
    file_path = resolve_path(resolved_output, os.getcwd())
    timestamp = (
        datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    header = {
        "type": "session",
        "version": CURRENT_SESSION_VERSION,
        "id": session_manager.getSessionId(),
        "timestamp": timestamp,
        "cwd": session_manager.getCwd(),
    }
    lines = [_dump_json(header)]

    parent_id: str | None = None
    for entry in session_manager.getBranch():
        linear_entry = dict(entry)
        linear_entry["parentId"] = parent_id
        lines.append(_dump_json(linear_entry))
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            parent_id = entry_id

    trailing_entries = (
        create_trailing_entries(parent_id, timestamp)
        if create_trailing_entries is not None
        else None
    )
    if trailing_entries is not None:
        for entry in trailing_entries:
            lines.append(_dump_json(entry))

    atomic.write_text(file_path, "\n".join(lines) + "\n")
    return file_path


exportSessionToJsonl = export_session_to_jsonl

__all__ = [
    "CreateTrailingEntries",
    "exportSessionToJsonl",
    "export_session_to_jsonl",
]
