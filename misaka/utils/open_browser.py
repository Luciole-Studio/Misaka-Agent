"""Open a URL or file in the platform browser or default handler.

Translated from pi's ``coding-agent/src/utils/open-browser.ts``.
"""

from __future__ import annotations

import subprocess
import sys


def openBrowser(target: str) -> None:
    """Open a URL or file in the platform browser/default handler.

    This intentionally never invokes a shell. On Windows, do not use ``cmd /c start``: cmd.exe
    re-parses metacharacters (&, |, ^, ...) before ``start`` runs, which would make
    attacker-controlled URLs injectable.
    """
    if sys.platform == "darwin":
        cmd, args = "open", [target]
    elif sys.platform == "win32":
        cmd, args = "rundll32", ["url.dll,FileProtocolHandler", target]
    else:
        cmd, args = "xdg-open", [target]

    # spawn reports launcher failures (for example, missing xdg-open) via an error event. Browser
    # launch is best-effort: callers still present the target to the user, so keep the launcher
    # failure from becoming a process crash.
    detach: dict[str, object] = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        subprocess.Popen(
            [cmd, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach
        )
    except OSError:
        pass


__all__ = ["openBrowser"]
