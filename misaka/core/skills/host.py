"""Small host seams for the pinned Hermes algorithms (no second runtime)."""

import os
import subprocess

from misaka.utils.shell import getShellConfig, getShellEnv

IS_WINDOWS = os.name == "nt"


def _find_bash():
    """Hermes' ``tools.environments.local._find_bash``: the bash the terminal runs commands with (Git Bash first on
    Windows), raising when there is none."""
    return getShellConfig().shell


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
    return runtime.execution_env(getShellEnv(), scrub=True) if runtime else getShellEnv()
