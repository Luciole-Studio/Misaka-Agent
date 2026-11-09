"""Translated from pi's ``coding-agent/test/fixtures/fake-external-editor.mjs`` (v1.1.0)."""

from __future__ import annotations

import json
import os
import sys

capturePath = sys.argv[1] if len(sys.argv) > 1 else None
filePath = sys.argv[-1] if len(sys.argv) > 1 else None
if not capturePath or not filePath:
    sys.exit(1)

directory = os.path.dirname(filePath)
with open(filePath, encoding="utf-8") as source:
    content = source.read()
with open(capturePath, "w", encoding="utf-8") as capture:
    json.dump(
        {
            "filePath": filePath,
            "content": content,
            "entries": os.listdir(directory),
            "directoryMode": os.stat(directory).st_mode & 0o777,
        },
        capture,
    )

if "--fail" in sys.argv:
    sys.exit(1)
with open(filePath, "w", encoding="utf-8", newline="") as target:
    target.write("" if "--empty" in sys.argv else "edited\n")
