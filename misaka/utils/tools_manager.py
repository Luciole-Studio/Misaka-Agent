"""Locating the external search binaries that the find and grep tools shell out to.

This used to fetch prebuilt `fd` and `rg` releases from GitHub on demand -- on every
interactive start, and again whenever the model called grep or find with the binary
absent. There was no checksum: whatever the release URL returned was written to disk,
chmod 0755, and executed, and a model tool call was enough to trigger it. Now nothing
here touches the network; MISAKA uses the tools you installed.

``~/.misaka/agent/bin`` is still searched, because that is where the removed downloader
put things and because dropping a binary in there is a reasonable way to supply one
without touching PATH.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from misaka.config.engine import get_bin_dir

# fd is packaged as `fdfind` on Debian and Ubuntu, where the name `fd` was already taken.
_BINARIES = {"fd": ("fd", "fdfind"), "rg": ("rg",)}

_INSTALL_HINT = {
    "fd": "fd — https://github.com/sharkdp/fd (`brew install fd`, `apt install fd-find`)",
    "rg": "ripgrep — https://github.com/BurntSushi/ripgrep (`brew install ripgrep`, `apt install ripgrep`)",
}


def find_tool(tool: str) -> str | None:
    """The path to `tool`, or None when it is not installed."""
    names = _BINARIES.get(tool, (tool,))
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    suffix = ".exe" if os.name == "nt" else ""
    for name in names:
        candidate = Path(get_bin_dir()) / f"{name}{suffix}"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def missing_tool_message(tool: str) -> str:
    return (
        f"{_INSTALL_HINT.get(tool, tool)} is required for this but was not found on PATH "
        f"or in {get_bin_dir()}."
    )
