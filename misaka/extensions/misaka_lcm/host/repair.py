"""`misaka lcm repair`: name the summaries of checkpoints written before they were named.

A checkpoint that only replayed its context recorded no ``contextSummaries``, so those summaries
come back on screen and in the input history as things the user typed. The checkpoint itself says
which they are: each record's ``frame`` is the text it drew. A message holding every frame is the
summary; nothing is inferred from how a message reads, and a checkpoint that does not match exactly
is left as it is. Only that field changes. By default this reports what it would do.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from misaka.config import home
from misaka.config.sessions import sessions_root
from misaka.core import session_catalog
from misaka.utils import atomic

from .ingest import _text_of

_ESCAPED = "UNTRUSTED-DATA-ESCAPED"     # what the fence makes of a fence word inside a summary


def _plain(text):
    return text.replace(_ESCAPED, "UNTRUSTED-DATA")


def named(entry):
    """The indexes of the summaries ``entry`` carries, or None when there is nothing to name."""
    if entry.get("type") != "compaction" or entry.get("contextMessages") is None or entry.get("contextSummaries"):
        return None
    lcm = (entry.get("details") or {}).get("lcm")
    frames = [_plain(record.get("frame") or "") for record in (lcm or {}).get("nodes") or []]
    if not frames or not all(frames):
        return None
    found, left = [], set(frames)
    for index, message in enumerate(entry["contextMessages"]):
        if message.get("role") == "user":
            text = _plain(_text_of(message.get("content")))
            shown = {frame for frame in left if frame in text}
            if shown:
                found.append(index)
                left -= shown
    return None if left else found


def _repaired(raw):
    """``raw``, one line of a session file, with its summaries named, or None if it stays."""
    if b'"compaction"' not in raw[:300]:
        return None
    entry = json.loads(raw)
    indexes = named(entry)
    if indexes is None:
        return None
    entry["contextSummaries"] = indexes
    entry["details"]["lcm"]["scaffolds"] = indexes
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def run(*, apply=False, root=None, say=print):
    root = Path(root or sessions_root())
    backup = home.home() / "state" / f"lcm-repair-{time.strftime('%Y%m%d-%H%M%S')}"
    changed = entries = busy = 0
    for path in sorted(root.rglob("*.jsonl")):
        lines = path.read_bytes().splitlines(keepends=True)
        fixed = {index: line for index, raw in enumerate(lines) if (line := _repaired(raw)) is not None}
        if not fixed:
            continue
        header = json.loads(lines[0])
        if session_catalog.live_session(str(header.get("id") or "")) is not None:
            busy += 1
            continue
        changed += 1
        entries += len(fixed)
        if apply:
            target = backup / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            atomic.write_bytes(path, b"".join(fixed.get(index, line) for index, line in enumerate(lines)), mode=0o600)
    verb = "Named" if apply else "Would name"
    say(f"{verb} the summaries of {entries} checkpoints in {changed} session files"
        + (f"; {busy} in use, left alone" if busy else "") + ".")
    if apply and changed:
        say(f"The files as they were: {backup}")
    elif changed:
        say("Nothing was changed. Run `misaka lcm repair --apply` to do it.")
    return 0
