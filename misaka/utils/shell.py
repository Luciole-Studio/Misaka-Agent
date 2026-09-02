"""Shell helpers shared across coding-agent tooling."""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class ShellConfig:
    shell: str
    args: list[str]
    commandTransport: Literal["argv", "stdin"] | None = None


_LEGACY_WSL_BASH_PATH_RE = re.compile(
    r"[a-z]:\\windows\\(?:system32|sysnative)\\bash\.exe"
)


def _get_bash_shell_config(shell: str) -> ShellConfig:
    normalized = shell.replace("/", "\\").lower()
    if _LEGACY_WSL_BASH_PATH_RE.fullmatch(normalized):
        return ShellConfig(shell=shell, args=["-s"], commandTransport="stdin")
    return ShellConfig(shell=shell, args=["-c"])


def normalize_command_for_stdin(command: str) -> str:
    """Match Node's UTF-8 replacement semantics for unpaired UTF-16 surrogates."""
    return command.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def find_executable_on_path(executable: str) -> str | None:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["where", executable],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0 and result.stdout:
            first_match = result.stdout.strip().splitlines()[0]
            if first_match and os.path.exists(first_match):
                return first_match
        return None

    return shutil.which(executable)


def find_bash_on_path() -> str | None:
    return find_executable_on_path("bash.exe" if os.name == "nt" else "bash")


def get_shell_config(custom_shell_path: str | None = None) -> ShellConfig:
    if custom_shell_path:
        if os.path.exists(custom_shell_path):
            return _get_bash_shell_config(custom_shell_path)
        raise Error(f"Custom shell path not found: {custom_shell_path}")

    if os.name == "nt":
        candidates: list[str] = []
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(os.path.join(program_files, "Git", "bin", "bash.exe"))
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            candidates.append(os.path.join(program_files_x86, "Git", "bin", "bash.exe"))
        for candidate in candidates:
            if os.path.exists(candidate):
                return _get_bash_shell_config(candidate)
        bash_on_path = find_bash_on_path()
        if bash_on_path:
            return _get_bash_shell_config(bash_on_path)
        searched = "\n".join(f"  {candidate}" for candidate in candidates)
        raise Error(
            "No bash shell found. Options:\n"
            "  1. Install Git for Windows: https://git-scm.com/download/win\n"
            "  2. Add your bash to PATH (Cygwin, MSYS2, etc.)\n"
            "  3. Set shellPath in settings.json\n\n"
            f"Searched Git Bash in:\n{searched}"
        )

    if os.path.exists("/bin/bash"):
        return _get_bash_shell_config("/bin/bash")
    bash_on_path = find_bash_on_path()
    if bash_on_path:
        return _get_bash_shell_config(bash_on_path)
    return ShellConfig(shell="sh", args=["-c"])


POWERSHELL_ARGS = [
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]


def get_powershell_config() -> ShellConfig:
    """Resolve PowerShell on Windows, preferring PowerShell 7."""
    if os.name != "nt":
        raise Error("The powershell tool is only available on Windows.")

    shell = find_executable_on_path("pwsh.exe") or find_executable_on_path(
        "powershell.exe"
    )
    if shell is None:
        raise Error(
            "No PowerShell executable found. Install PowerShell or add "
            "powershell.exe/pwsh.exe to PATH."
        )
    return ShellConfig(shell=shell, args=list(POWERSHELL_ARGS))


def get_shell_env() -> dict[str, str]:
    from misaka.config import get_bin_dir

    env = dict(os.environ)
    bin_dir = get_bin_dir()
    path_key = next((key for key in env if key.lower() == "path"), "PATH")
    current_path = env.get(path_key, "")
    path_entries = [entry for entry in current_path.split(os.pathsep) if entry]
    if bin_dir not in path_entries:
        env[path_key] = os.pathsep.join(filter(None, [bin_dir, current_path]))
    return env


def sanitize_binary_output(value: str) -> str:
    pieces: list[str] = []
    for char in value:
        code = ord(char)
        if code in {0x09, 0x0A, 0x0D}:
            pieces.append(char)
            continue
        if code <= 0x1F:
            continue
        if 0xFFF9 <= code <= 0xFFFB:
            continue
        pieces.append(char)
    return "".join(pieces)


_tracked_detached_child_pids: set[int] = set()


def track_detached_child_pid(pid: int) -> None:
    _tracked_detached_child_pids.add(pid)


def untrack_detached_child_pid(pid: int) -> None:
    _tracked_detached_child_pids.discard(pid)


def kill_tracked_detached_children() -> None:
    for pid in list(_tracked_detached_child_pids):
        kill_process_tree(pid)
    _tracked_detached_child_pids.clear()


def kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        taskkill = ntpath.normpath(
            ntpath.join(
                r"C:\Windows" if system_root is None else system_root,
                "System32",
                "taskkill.exe",
            )
        )
        try:
            subprocess.Popen(
                [taskkill, "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return
        return

    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return


class Error(RuntimeError):
    """Local error type mirroring the upstream tooling surface."""


sanitizeBinaryOutput = sanitize_binary_output
killTrackedDetachedChildren = kill_tracked_detached_children
getPowerShellConfig = get_powershell_config

__all__ = [
    "POWERSHELL_ARGS",
    "ShellConfig",
    "getPowerShellConfig",
    "get_powershell_config",
    "killTrackedDetachedChildren",
    "normalize_command_for_stdin",
    "sanitizeBinaryOutput",
]
