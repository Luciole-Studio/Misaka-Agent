"""Translated from pi's ``packages/coding-agent/src/utils/tools-manager.ts`` (v1.1.0).

MISAKA fork: nothing here touches the network. pi downloads prebuilt ``fd`` and ``rg`` releases from GitHub when they
are missing, with no checksum, and a model tool call is enough to trigger it; MISAKA uses the tools you installed and
``ensureTool`` reports a missing one with how to install it. ``getBinDir()`` is still searched first, as upstream does,
so a binary dropped there is used without touching PATH. ``find_tool`` is MISAKA's lookup for the other binaries
``misaka setup`` checks, and ``install_command`` the line that installs one on this OS.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.config.engine import get_bin_dir


@dataclass(frozen=True, slots=True)
class ToolConfig:
    name: str
    binaryName: str  # Name of the binary
    systemBinaryNames: tuple[str, ...] | None = None  # Alternative system command names to try
    # MISAKA fork: where it lives, in place of the release download; ``install_command`` has the line to run.
    installHint: str = ""


TOOLS: dict[str, ToolConfig] = {
    "fd": ToolConfig(
        name="fd",
        binaryName="fd",
        # fd is packaged as `fdfind` on Debian and Ubuntu, where the name `fd` was already taken.
        systemBinaryNames=("fd", "fdfind"),
        installHint="https://github.com/sharkdp/fd",
    ),
    "rg": ToolConfig(
        name="ripgrep",
        binaryName="rg",
        installHint="https://github.com/BurntSushi/ripgrep",
    ),
}


# MISAKA fork: one table for ``misaka setup`` and for a tool that finds its program missing. The
# grep tool named `brew` and `apt` on Windows, where the wizard already said `winget`.
_BREW = {"git": "xcode-select --install", "rg": "brew install ripgrep", "fd": "brew install fd",
         "pdftotext": "brew install poppler", "ocrmypdf": "brew install ocrmypdf tesseract-lang",
         "tesseract": "brew install tesseract"}
_APT = {"git": "sudo apt install git", "rg": "sudo apt install ripgrep", "fd": "sudo apt install fd-find",
        "pdftotext": "sudo apt install poppler-utils",
        # The chi_sim and jpn packs: documents.ocr_langs defaults to eng+chi_sim+jpn.
        "ocrmypdf": "sudo apt install ocrmypdf tesseract-ocr-chi-sim tesseract-ocr-jpn",
        "tesseract": "sudo apt install tesseract-ocr"}
# OCRmyPDF has no Windows package: it needs Tesseract and Ghostscript first, then pip.
_WINGET = {"git": "winget install Git.Git", "rg": "winget install BurntSushi.ripgrep.MSVC",
           "fd": "winget install sharkdp.fd", "pdftotext": "winget install oschwartz10612.Poppler",
           "ocrmypdf": "see https://ocrmypdf.readthedocs.io/en/latest/installation.html#installing-on-windows",
           "tesseract": "winget install UB-Mannheim.TesseractOCR"}
# Where an installer wrote the folder it chose, for a program it left off PATH. UB-Mannheim's
# Tesseract, the one `winget install` above brings, does this: a machine that had just installed
# it was told it had none (Windows 11, 2026-11-10).
_WINDOWS_INSTALL_DIRS = {"tesseract": (r"SOFTWARE\Tesseract-OCR", "InstallDir")}


def install_command(binary: str) -> str:
    """MISAKA fork: the package manager line for a missing binary on this OS."""
    table = _BREW if sys.platform == "darwin" else _WINGET if sys.platform == "win32" else _APT
    return {**table, "mineru-kit": "uv tool install mineru"}.get(binary, f"install {binary}")


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
    return (f"{config.name} is not installed: {config.installHint} (`{install_command(tool)}`). "
            f"MISAKA looks for it on PATH and in {get_bin_dir()}.")


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
        onStatus(ToolStatus(type="warning", message=(
            f"{config.name} not found. Install it: {config.installHint} (`{install_command(tool)}`)")))
    return None


def _windows_install(tool: str) -> str | None:
    """MISAKA fork: the program a Windows installer registered and left off PATH, or None."""
    entry = _WINDOWS_INSTALL_DIRS.get(tool)
    if entry is None or sys.platform != "win32":
        return None
    import winreg

    key, value = entry
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as opened:
                    folder = winreg.QueryValueEx(opened, value)[0]
            except OSError:
                continue
            # A key that outlived an uninstall names a folder with no program in it.
            found = shutil.which(tool, path=str(folder))
            if found:
                return found
    return None


def find_tool(tool: str) -> str | None:
    """MISAKA fork: the path to any binary ``misaka setup`` checks, or None when it is not installed."""
    if tool in TOOLS:
        found = getToolPath(tool)  # type: ignore[arg-type]
        return shutil.which(found) or found if found else None
    return shutil.which(tool) or _windows_install(tool)


__all__ = ["ToolStatus", "ensureTool", "find_tool", "getToolPath", "install_command", "missingToolMessage"]
