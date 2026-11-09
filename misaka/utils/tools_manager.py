"""Translated from pi's ``packages/coding-agent/src/utils/tools-manager.ts`` (v1.1.0).

MISAKA fork: nothing here touches the network. pi downloads prebuilt ``fd`` and ``rg`` releases from GitHub when they
are missing, with no checksum, and a model tool call is enough to trigger it; MISAKA uses the tools you installed and
``ensureTool`` reports a missing one with how to install it. ``getBinDir()`` is still searched first, as upstream does,
so a binary dropped there is used without touching PATH. ``find_tool`` is MISAKA's lookup for the other binaries
``misaka setup`` checks.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.config.engine import get_bin_dir


@dataclass(frozen=True, slots=True)
class ToolConfig:
    name: str
    binaryName: str  # Name of the binary
    systemBinaryNames: tuple[str, ...] | None = None  # Alternative system command names to try
    # MISAKA fork: where to get it, in place of the release download.
    installHint: str = ""


TOOLS: dict[str, ToolConfig] = {
    "fd": ToolConfig(
        name="fd",
        binaryName="fd",
        # fd is packaged as `fdfind` on Debian and Ubuntu, where the name `fd` was already taken.
        systemBinaryNames=("fd", "fdfind"),
        installHint="https://github.com/sharkdp/fd (`brew install fd`, `apt install fd-find`)",
    ),
    "rg": ToolConfig(
        name="ripgrep",
        binaryName="rg",
        installHint="https://github.com/BurntSushi/ripgrep (`brew install ripgrep`, `apt install ripgrep`)",
    ),
}


def commandExists(cmd: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(cmd) is not None


def getToolPath(tool: Literal["fd", "rg"]) -> str | None:
    """Get the path to a tool (system-wide or in our tools dir)."""
    config = TOOLS.get(tool)
    if not config:
        return None

    # Check our tools directory first
    localPath = os.path.join(get_bin_dir(), config.binaryName + (".exe" if os.name == "nt" else ""))
    if os.path.exists(localPath):
        return localPath

    # Check system PATH - if found, just return the command name (it's in PATH)
    systemBinaryNames = config.systemBinaryNames or (config.binaryName,)
    for systemBinaryName in systemBinaryNames:
        if commandExists(systemBinaryName):
            return systemBinaryName

    return None


@dataclass(frozen=True, slots=True)
class ToolStatus:
    type: Literal["info", "warning"]
    message: str


def missingToolMessage(tool: Literal["fd", "rg"]) -> str:
    """MISAKA fork: what a tool reports when ``tool`` is missing (upstream: "... could not be downloaded")."""
    config = TOOLS[tool]
    return f"{config.name} is not installed: {config.installHint}. MISAKA looks for it on PATH and in {get_bin_dir()}."


async def ensureTool(
    tool: Literal["fd", "rg"],
    onStatus: Callable[[ToolStatus], None] | None = None,
) -> str | None:
    """Ensure a tool is available.
    Reports progress through `onStatus`; status messages are otherwise silent.
    Returns the tool path, or None if unavailable.
    """
    existingPath = getToolPath(tool)
    if existingPath:
        return existingPath

    config = TOOLS.get(tool)
    if not config:
        return None

    # MISAKA fork: never downloaded; the status says how to install it.
    if onStatus is not None:
        onStatus(ToolStatus(type="warning", message=f"{config.name} not found. Install it: {config.installHint}"))
    return None


def find_tool(tool: str) -> str | None:
    """MISAKA fork: the path to any binary ``misaka setup`` checks, or None when it is not installed."""
    if tool in TOOLS:
        found = getToolPath(tool)  # type: ignore[arg-type]
        return shutil.which(found) or found if found else None
    return shutil.which(tool)


__all__ = ["ToolStatus", "ensureTool", "find_tool", "getToolPath", "missingToolMessage"]
