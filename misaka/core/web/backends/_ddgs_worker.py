"""DDGS search child-process entrypoint.

Invoked as ``python misaka/core/web/backends/_ddgs_worker.py`` (script path from the
parent provider). Reads one JSON request from stdin, writes one JSON envelope to stdout,
then exits.

Request::

    {"query": str, "safe_limit": int}

Envelope::

    {"ok": true, "results": [...]}
    {"ok": false, "error": str}

It exists because ``ddgs``/``primp`` can block inside native code while holding the GIL:
in that state no in-process timeout can fire, so the only reliable deadline is a process
the parent can kill. See :mod:`misaka.core.web.backends.ddgs`.
"""

from __future__ import annotations

import json
import sys


def _write_envelope(envelope: dict) -> None:
    json.dump(envelope, sys.stdout)
    sys.stdout.flush()


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001 - any malformed request is one message back
        _write_envelope({"ok": False, "error": f"invalid request: {exc}"})
        return 2

    query = str(request.get("query") or "")
    safe_limit = max(1, int(request.get("safe_limit") or 1))
    try:
        # Imported inside main so startup stays light and the parent's module (which the
        # test seam patches) is the one that defines the search.
        from misaka.core.web.backends.ddgs import _run_ddgs_search

        results = _run_ddgs_search(query, safe_limit)
        _write_envelope({"ok": True, "results": results})
        return 0
    except Exception as exc:  # noqa: BLE001 - ddgs raises its own exception types
        _write_envelope({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
