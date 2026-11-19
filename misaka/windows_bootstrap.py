"""Process bootstrap for Windows: UTF-8 stdio, an ANSI console, no ``cmd /c ver`` flash.

Ported from Hermes (``hermes_bootstrap.py``: ``apply_windows_utf8_bootstrap``,
``enable_windows_vt``, ``suppress_platform_ver_console``; MIT). Windows binds stdio to the
console code page (cp936, cp1252), so printing Thai or Vietnamese raises
``UnicodeEncodeError``, and Python children inherit the same default unless
``PYTHONUTF8``/``PYTHONIOENCODING`` are set. ``misaka/__main__.py`` imports this module first.
It does NOT re-exec with ``-X utf8``: ``open()`` in the current process still needs an
explicit ``encoding="utf-8"`` (ruff ``PLW1514``). POSIX is left alone deliberately -- users'
``LANG``/``LC_*`` choices are respected.

Stdlib only: it runs before any other misaka module is imported.
"""

from __future__ import annotations

import os
import sys

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """Apply the Windows UTF-8 bootstrap once; True only when it was applied this call."""
    global _bootstrap_applied

    if not _IS_WINDOWS or _bootstrap_applied:
        return False

    # setdefault() so a user can opt out with PYTHONUTF8=0 / PYTHONIOENCODING=...
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # os.environ changes don't rebind streams bound at interpreter startup, so
    # reconfigure them in-process. errors="replace" keeps a non-UTF-8 legacy
    # pipe on stdin from crashing us (U+FFFD instead of an exception).
    # Non-TextIOWrapper streams (BytesIO in tests, embedded hosts) have no
    # reconfigure(): skip -- the env-var fix for children is the bigger win.
    for stream_name in ("stdout", "stderr", "stdin"):
        reconfigure = getattr(getattr(sys, stream_name, None), "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass  # closed, or replaced with something non-reconfigurable

    _bootstrap_applied = True
    return True


_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def enable_windows_vt(streams=None) -> bool:
    """Opt the console behind stdout/stderr in to ANSI escape processing.

    A conhost console (PowerShell 5.1, cmd.exe) prints raw SGR codes as ``←[35m`` until the
    output handle has ENABLE_VIRTUAL_TERMINAL_PROCESSING, and shells hand native children a
    console with it off. The mode belongs to the console buffer, so setting it here also
    covers a child. Handles that are not a console (pipes, files, NUL, a windowless pythonw)
    fail GetConsoleMode and are left alone. A console that refuses VT (pre-Windows 10) gets
    NO_COLOR instead, so it shows plain text rather than garbage. Returns False only in that
    fallback case.
    """
    if not _IS_WINDOWS:
        return True
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enabled = True
    for stream in (sys.stdout, sys.stderr) if streams is None else streams:
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
        except (AttributeError, OSError, ValueError):
            continue  # no fd (None under pythonw, StringIO in embedders)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            continue
        if mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            continue
        if not kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            enabled = False
    if not enabled:
        os.environ.setdefault("NO_COLOR", "1")
    return enabled


def suppress_platform_ver_console() -> None:
    """Stub ``platform._syscmd_ver`` on Windows -- decode-crash + console-flash guard.

    ``platform.win32_ver()`` (reached via ``platform.platform()``, which the OpenAI SDK
    calls) shells out ``cmd /c ver`` with ``shell=True`` and no ``CREATE_NO_WINDOW``: a
    windowless parent flashes a console per call, and Python 3.11.0/3.11.1 (no
    ``encoding="locale"`` fix) strict-utf-8-decodes the OEM code page output under PEP 540
    mode and raises. Returning the inputs makes ``win32_ver()`` fall back to
    ``sys.getwindowsversion()`` -- same data, no subprocess.
    """
    if not _IS_WINDOWS:
        return
    try:
        import platform

        if hasattr(platform, "_syscmd_ver"):
            def _quiet_syscmd_ver(system="", release="", version="",
                                  supported_platforms=("win32", "win16", "dos")):
                return system, release, version

            platform._syscmd_ver = _quiet_syscmd_ver
    except Exception:  # noqa: BLE001, S110 - hardening only: never break an entry point
        pass


def release_launcher() -> None:
    """Take the console-script launcher off ``sys.path``.

    ``misaka.exe`` (uv's launcher, and pip's) runs itself as a zip archive holding only the
    ``__main__.py`` that imported this package, so the ``.exe`` stays on ``sys.path``. The first
    ``importlib.metadata`` lookup -- the version, read at startup -- then opens it as a zip and
    caches it open for the life of the process, without delete sharing, and Windows will neither
    rename nor replace an executable held that way: ``misaka update`` could not set it aside while
    any ``misaka`` ran. Nothing more is imported from the archive once its ``__main__`` runs.
    """
    if _IS_WINDOWS:
        sys.path[:] = [entry for entry in sys.path if not entry.lower().endswith(".exe")]


apply_windows_utf8_bootstrap()
enable_windows_vt()
suppress_platform_ver_console()
release_launcher()
