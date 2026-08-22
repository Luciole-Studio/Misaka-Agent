"""Bun-style wrapper entrypoint for the coding-agent CLI."""

from __future__ import annotations

import importlib
import sys
import warnings
from collections.abc import Callable

from misaka.compat.bun.restore_sandbox_env import restore_sandbox_env
from misaka.config import APP_NAME


def _set_process_title(title: str) -> None:
    # Keep argv visible to Python tooling even if no native process-title setter is installed.
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


def _import_register_bedrock_module() -> None:
    importlib.import_module("misaka.compat.bun.register_bedrock")


def _load_cli_main() -> Callable[[list[str] | None], int]:
    from misaka.cli import main as cli_main

    return cli_main


def main(argv: list[str] | None = None) -> int:
    _set_process_title(APP_NAME)
    _suppress_runtime_warnings()
    restore_sandbox_env()
    _import_register_bedrock_module()
    return _load_cli_main()(argv)




__all__: list[str] = []
