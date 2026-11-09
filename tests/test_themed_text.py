"""Translated from pi's ``coding-agent/test/themed-text.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.modes.interactive.components.themed_text import ThemedText
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import initTheme


@pytest.fixture(autouse=True)
def reset_theme():
    yield
    initTheme("dark")


def test_builds_lazily_and_rebuilds_with_the_current_theme_after_invalidation():
    initTheme("dark")
    builds = [0]

    def build() -> str:
        builds[0] += 1
        return theme_module.theme.fg("accent", "hello")

    text = ThemedText(build)
    assert builds[0] == 0
    dark = "".join(text.render(20))

    initTheme("light")
    assert "".join(text.render(20)) == dark
    text.invalidate()
    assert theme_module.theme.getFgAnsi("accent") in "".join(text.render(20))
    assert builds[0] == 2
