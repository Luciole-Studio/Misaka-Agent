"""Restore an empty process environment from ``/proc/self/environ`` when possible."""

from __future__ import annotations

import os
from pathlib import Path


def _read_proc_self_environ() -> str:
    return Path("/proc/self/environ").read_text(encoding="utf-8")


def restore_sandbox_env() -> None:
    # Match the TS wrapper's guard: only repair when the runtime environment is empty.
    if os.environ:
        return

    try:
        data = _read_proc_self_environ()
    except Exception:
        return

    for entry in data.split("\0"):
        index = entry.find("=")
        if index > 0:
            os.environ[entry[:index]] = entry[index + 1 :]


restoreSandboxEnv = restore_sandbox_env

__all__ = ["restoreSandboxEnv"]

