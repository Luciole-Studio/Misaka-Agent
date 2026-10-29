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

# Windows process creation flags; the `subprocess` constants exist only on Windows.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


@dataclass(slots=True)
class ShellConfig:
    shell: str
    args: list[str]
    commandTransport: Literal["argv", "stdin"] | None = None


_LEGACY_WSL_BASH_PATH_RE = re.compile(
    r"[a-z]:\\windows\\(?:system32|sysnative)\\bash\.exe"
)


def getBashShellConfig(shell: str) -> ShellConfig:
    normalized = shell.replace("/", "\\").lower()
    if _LEGACY_WSL_BASH_PATH_RE.fullmatch(normalized):
        return ShellConfig(shell=shell, args=["-s"], commandTransport="stdin")
    return ShellConfig(shell=shell, args=["-c"])


def normalize_command_for_stdin(command: str) -> str:
    """Match Node's UTF-8 replacement semantics for unpaired UTF-16 surrogates."""
    return command.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def findExecutableOnPath(executable: str) -> str | None:
    if os.name == "nt":
        try:
            # pi decodes with Node's lenient UTF-8. `where` prints its "not found" notice in the
            # console code page -- GBK "信息:" is 0xD0 0xC5 -- and a strict decode killed the
            # reader thread with UnicodeDecodeError on Chinese Windows (issue #13, B2).
            result = subprocess.run(
                ["where", executable],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
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
    return findExecutableOnPath("bash.exe" if os.name == "nt" else "bash")


def getShellConfig(custom_shell_path: str | None = None) -> ShellConfig:
    if custom_shell_path:
        if os.path.exists(custom_shell_path):
            return getBashShellConfig(custom_shell_path)
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
                return getBashShellConfig(candidate)
        bash_on_path = find_bash_on_path()
        if bash_on_path:
            return getBashShellConfig(bash_on_path)
        searched = "\n".join(f"  {candidate}" for candidate in candidates)
        raise Error(
            "No bash shell found. Options:\n"
            "  1. Install Git for Windows: https://git-scm.com/download/win\n"
            "  2. Add your bash to PATH (Cygwin, MSYS2, etc.)\n"
            "  3. Set shellPath in settings.json\n\n"
            f"Searched Git Bash in:\n{searched}"
        )

    if os.path.exists("/bin/bash"):
        return getBashShellConfig("/bin/bash")
    bash_on_path = find_bash_on_path()
    if bash_on_path:
        return getBashShellConfig(bash_on_path)
    return ShellConfig(shell="sh", args=["-c"])


POWERSHELL_ARGS = [
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]


def getPowerShellConfig() -> ShellConfig:
    """Resolve PowerShell on Windows, preferring PowerShell 7."""
    if os.name != "nt":
        raise Error("The powershell tool is only available on Windows.")

    shell = findExecutableOnPath("pwsh.exe") or findExecutableOnPath(
        "powershell.exe"
    )
    if shell is None:
        raise Error(
            "No PowerShell executable found. Install PowerShell or add "
            "powershell.exe/pwsh.exe to PATH."
        )
    return ShellConfig(shell=shell, args=list(POWERSHELL_ARGS))


# What a release archive's launcher sets for MISAKA's own Python (scripts/build_release.py): kept
# for the processes MISAKA starts of itself, never for the user's commands -- under PYTHONSAFEPATH a
# skill's `python scripts/fetch.py` could not import the `_common.py` beside it (0.18.9 sweep).
_ARCHIVE_PYTHON_FLAGS = ("PYTHONSAFEPATH", "PYTHONNOUSERSITE")


def getShellEnv() -> dict[str, str]:
    from misaka.config import get_bin_dir
    from misaka.config.engine import bundle_root

    env = dict(os.environ)
    if bundle_root() is not None:
        for name in _ARCHIVE_PYTHON_FLAGS:
            env.pop(name, None)
    bin_dir = get_bin_dir()
    path_key = next((key for key in env if key.lower() == "path"), "PATH")
    current_path = env.get(path_key, "")
    path_entries = [entry for entry in current_path.split(os.pathsep) if entry]
    if bin_dir not in path_entries:
        env[path_key] = os.pathsep.join(filter(None, [bin_dir, current_path]))
    return env


_BINARY_OUTPUT_NOISE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufff9-\ufffb]")


def sanitizeBinaryOutput(value: str) -> str:
    """Sanitize binary output for display/storage.

    Removes characters that crash string-width or cause display issues:
    - Control characters (except tab, newline, carriage return)
    - Unicode interlinear annotation characters U+FFF9..U+FFFB (crash string-width due to a bug)
    """
    return _BINARY_OUTPUT_NOISE.sub("", value)


_tracked_detached_child_pids: set[int] = set()


def trackDetachedChildPid(pid: int) -> None:
    _tracked_detached_child_pids.add(pid)


def untrackDetachedChildPid(pid: int) -> None:
    _tracked_detached_child_pids.discard(pid)


def killTrackedDetachedChildren() -> None:
    for pid in list(_tracked_detached_child_pids):
        killProcessTree(pid)
    _tracked_detached_child_pids.clear()


def killProcessTree(pid: int) -> None:
    if os.name == "nt":
        # Use the trusted System32 executable so cleanup does not depend on PATH.
        system_root = os.environ.get("SystemRoot")
        taskkill = ntpath.normpath(
            ntpath.join(
                r"C:\Windows" if system_root is None else system_root,
                "System32",
                "taskkill.exe",
            )
        )
        try:
            # Node's `{stdio: "ignore", detached: true, windowsHide: true}`: libuv sets DETACHED_PROCESS |
            # CREATE_NEW_PROCESS_GROUP for detached and CREATE_NO_WINDOW when no stdio is inherited.
            subprocess.Popen(
                [taskkill, "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            )
        except OSError:
            # Ignore errors if taskkill fails.
            return
        return

    try:
        os.killpg(pid, signal.SIGKILL)  # windows-footgun: ok - POSIX branch
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)  # windows-footgun: ok - POSIX branch
        except (OSError, ProcessLookupError):
            return


class Error(RuntimeError):
    """Local error type mirroring the upstream tooling surface."""



__all__ = [
    "POWERSHELL_ARGS",
    "ShellConfig",
    "getPowerShellConfig",
    "killTrackedDetachedChildren",
    "normalize_command_for_stdin",
    "sanitizeBinaryOutput",
]
