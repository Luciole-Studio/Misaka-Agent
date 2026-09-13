"""Small host seams for the pinned Hermes algorithms (no second runtime)."""

import os
import subprocess

from misaka.utils.shell import get_shell_env

IS_WINDOWS = os.name == "nt"


def windows_hide_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def is_termux():
    return bool(
        os.environ.get("TERMUX_VERSION")
        or "com.termux/files/usr" in os.environ.get("PREFIX", "")
    )


def delegated_child_subprocess_env():
    # Use the same child environment as MISAKA's shell, never mutate os.environ.
    from .runtime import _active
    runtime = _active.get()
    return runtime.execution_env(get_shell_env(), scrub=True) if runtime else get_shell_env()
