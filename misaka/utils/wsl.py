"""Translated from pi's ``coding-agent/src/utils/wsl.ts`` (v1.1.0)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


def isWSL(env: Mapping[str, str] | None = None) -> bool:
    """Windows Subsystem for Linux, where Windows executables are reachable through interop."""
    env = os.environ if env is None else env
    if env.get("WSL_DISTRO_NAME") or env.get("WSLENV"):
        return True
    try:
        release = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"microsoft|wsl", release, re.IGNORECASE) is not None


__all__ = ["isWSL"]
