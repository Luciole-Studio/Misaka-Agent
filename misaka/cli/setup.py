"""Translated from pi's ``packages/coding-agent/src/cli/setup.ts`` (v1.1.0).

MISAKA forks:
- The process title is not set: the standard library has no way to, and it is not worth a dependency.
- The agent is named MISAKA: ``MISAKA_CODING_AGENT`` and ``AI_AGENT=misaka``, where pi sets ``PI_CODING_AGENT`` and
  ``AI_AGENT=pi``. Every process the session starts inherits them.
- Python's warnings stand in for ``process.emitWarning``, so none lands on the terminal; warnings asked for with
  ``-W`` or ``PYTHONWARNINGS`` still show.
"""

from __future__ import annotations

import os
import sys
import warnings

from misaka.core.http_dispatcher import configureHttpDispatcher


def setupCli() -> None:
    os.environ["MISAKA_CODING_AGENT"] = "true"
    os.environ["AI_AGENT"] = "misaka"
    if not sys.warnoptions:
        warnings.simplefilter("ignore")

    # Configure the HTTP client before provider SDKs issue requests. Settings are applied
    # once SettingsManager has loaded global/project configuration.
    configureHttpDispatcher()


__all__ = ["setupCli"]
