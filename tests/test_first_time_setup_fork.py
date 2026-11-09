"""Translated from pi's ``packages/coding-agent/test/first-time-setup-fork.test.ts`` (v1.1.0).

pi mocks ``PACKAGE_NAME`` to a forked distribution's; MISAKA is one, so ``shouldRunFirstTimeSetup`` is called as is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from misaka.cli.startup_ui import shouldRunFirstTimeSetup


def test_returns_false_for_a_forked_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PI_EXPERIMENTAL", "1")
    settingsPath = str(tmp_path / "settings.json")

    assert shouldRunFirstTimeSetup(settingsPath) is False
