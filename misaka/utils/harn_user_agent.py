"""User-Agent helpers for harn version checks."""

from __future__ import annotations

import platform
import sys


def get_harn_user_agent(version: str) -> str:
    runtime = f"python/{platform.python_version()}"
    return f"harn/{version} ({sys.platform}; {runtime}; {_arch()})"


def _arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x64"
    return machine


getHarnUserAgent = get_harn_user_agent

__all__ = ["getHarnUserAgent", "get_harn_user_agent"]
