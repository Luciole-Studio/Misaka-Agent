"""Identify the source snapshot a local GUI process actually loaded."""
from __future__ import annotations

import hashlib
from pathlib import Path


def source_revision(source: Path | None = None) -> str:
    source = source or Path(__file__).resolve().parents[3]
    gui = source / "misaka" / "ui" / "gui"
    paths = [*gui.rglob("*.py"),
             *(gui / "static" / name for name in ("index.html", "app.js", "style.css")),
             source / "misaka" / "core" / "session_control.py",
             source / "misaka" / "core" / "network" / "roster.py"]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(source).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()
