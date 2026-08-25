"""CLI helpers and the installed console entry point for the coding-agent package."""

from __future__ import annotations

import asyncio
import os
import sys
import warnings

from misaka.config import APP_NAME


def _set_process_title(title: str) -> None:
    sys.argv[0] = title

    try:
        import setproctitle  # type: ignore[import-not-found]
    except Exception:
        return

    try:
        setproctitle.setproctitle(title)
    except Exception:
        return


def _suppress_runtime_warnings() -> None:
    warnings.showwarning = lambda *args, **kwargs: None


async def _invoke_main(argv: list[str]) -> int:
    from misaka.cli.engine import main as async_main

    return await async_main(argv)


def main(argv: list[str] | None = None) -> int:
    # Imported here, not at module level: this package __init__ runs for every
    # `misaka.cli.*` submodule (panel, app, ...), and http_dispatcher drags the whole
    # engine (core -> agent_session -> ai) into processes that never run one.
    from misaka.core.http_dispatcher import configureHttpDispatcher

    _set_process_title(APP_NAME)
    os.environ["MISAKA_CODING_AGENT"] = "true"
    _suppress_runtime_warnings()
    configureHttpDispatcher()
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_invoke_main(resolved_argv))

__all__ = ["main"]
