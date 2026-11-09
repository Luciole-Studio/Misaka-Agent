"""Translated from pi's ``packages/coding-agent/test/suite/regressions/2791-fswatch-error-crash.test.ts`` (v1.1.0).

Regression test for https://github.com/earendil-works/pi/issues/2791: an ``"error"`` event on the custom-theme
watcher must be handled, not crash the process.

MISAKA fork: pi spawns a child that finds the FSWatcher among ``process._getActiveHandles()``; here the watcher is the
theme module's ``themeWatcher`` in process (a ``misaka.utils.fs_watch.FSWatcher``), and the custom themes directory is
the per-test ``MISAKA_HOME``'s.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import misaka.modes.interactive.theme as theme_package
from misaka.config import get_custom_themes_dir
from misaka.modes.interactive.theme import theme as theme_module


def test_process_should_survive_an_error_event_on_the_theme_fswatcher():
    themesDir = Path(get_custom_themes_dir())
    themesDir.mkdir(parents=True, exist_ok=True)
    # Copy dark.json as "custom-test" theme
    darkTheme = json.loads((Path(theme_package.__file__).parent / "dark.json").read_text(encoding="utf-8"))
    darkTheme["name"] = "custom-test"
    (themesDir / "custom-test.json").write_text(json.dumps(darkTheme, indent=2), encoding="utf-8")

    previousTheme = theme_module.currentThemeName
    try:
        assert theme_module.setTheme("custom-test", True)["success"] is True

        fsWatcher = theme_module.themeWatcher
        assert fsWatcher is not None, "no FSWatcher found"
        assert fsWatcher.listenerCount("error") > 0, "BUG: FSWatcher has no error handler (issue #2791)"

        # This simulates an async OS error (e.g. ReadDirectoryChangesW invalidation).
        fsWatcher.emit("error", OSError("simulated OS watcher failure"))
        assert theme_module.themeWatcher is None
    finally:
        theme_module.stopThemeWatcher()
        theme_module.initTheme(previousTheme if previousTheme in ("dark", "light") else None)
        os.remove(themesDir / "custom-test.json")
