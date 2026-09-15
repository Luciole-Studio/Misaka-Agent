"""An archive of what the model actually asked for, one JSON per call.

Ported from FrontierAgent's ``_writer_core.py:_archive_intent`` (audit D81). A deliverable
that came out wrong is otherwise unexplainable: the ops that produced it are gone the moment
the call returns, and all anyone has afterwards is the file and a receipt saying it worked.

Best effort throughout -- archiving is a debugging aid, and a full disk must not fail the
write that succeeded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from misaka.config.product import CFG

# Per target file. Past this the oldest go: the archive is for reconstructing how the file
# in front of you came to be, and a document rewritten two hundred times has a longer story
# than anyone will read.
MAX_PER_PATH = 200


def _bucket(path, workspace=None):
    digest = hashlib.sha256(os.path.realpath(str(path)).encode("utf-8")).hexdigest()[:12]
    directory = (Path(workspace).resolve() / ".office-intent" / digest if workspace is not None
                 else Path(os.path.expanduser(CFG["office_intent"]), digest))
    if workspace is not None:
        directory.resolve().relative_to(Path(workspace).resolve())
    return str(directory)


def archive(path, ops, *, workspace=None):
    """Record one call's ops beside the file they targeted. Returns the file written, or "".

    Numbered rather than timestamped, and never overwritten: the sequence is the point, and
    two calls in the same millisecond are exactly the case worth telling apart.
    """
    try:
        directory = _bucket(path, workspace)
        os.makedirs(directory, exist_ok=True)
        numbered = [(int(name.split("_", 1)[0]), name) for name in os.listdir(directory)
                    if name.endswith(".json") and name.split("_", 1)[0].isdigit()]
        numbered.sort()
        sequence = 1 + max((number for number, _name in numbered), default=0)
        first = next(iter(ops[0]), "ops") if ops and isinstance(ops[0], dict) else "ops"
        # Op names are untrusted; they must never become directory components.
        first = "".join(char if char.isalnum() or char in "_-" else "_" for char in str(first))[:80]
        target = os.path.join(directory, f"{sequence:03d}_{first}.json")
        record = {"seq": sequence, "path": os.path.realpath(str(path)), "ops": ops}
        with open(target, "x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=1)
        for _number, name in numbered[:max(0, len(numbered) + 1 - MAX_PER_PATH)]:
            os.remove(os.path.join(directory, name))
        return target
    except (OSError, TypeError, ValueError):
        return ""
