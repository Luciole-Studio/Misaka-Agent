"""Translated from pi's ``coding-agent/test/suite/regressions/7209-model-selector-filter-resets-selection.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.model_selector import (
    ModelSelectorComponent,
    ScopedModelItem,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi
from tests.suite_harness import createHarness


def createFakeTui() -> Any:
    return SimpleNamespace(requestRender=lambda *_args: None)


def selectedModelId(rendered: str) -> str | None:
    """Return the model id of the highlighted (→) row in the rendered selector."""
    line = next((line for line in rendered.split("\n") if line.startswith("→ ")), None)
    if not line:
        return None
    rest = line[1:].lstrip()
    id = rest.split(" [")[0].removeprefix("✓").lstrip()
    return id.strip() or None


async def waitFor(assertion: Callable[[], None], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            assertion()
            return
        except AssertionError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def keybindings():
    setKeybindings(KeybindingsManager())


def _refreshed(selector: ModelSelectorComponent) -> Callable[[], None]:
    def check() -> None:
        assert "Model catalogs refreshed." in stripAnsi("\n".join(selector.render(120)))

    return check


async def test_moves_selection_to_the_first_row_in_the_all_tab_when_typing_a_query():
    async with createHarness(
        models=[
            {"id": "alpha-1", "name": "Alpha One", "reasoning": True},
            {"id": "alpha-2", "name": "Alpha Two", "reasoning": True},
            {"id": "alpha-3", "name": "Alpha Three", "reasoning": True},
            {"id": "beta-1", "name": "Beta One", "reasoning": True},
        ]
    ) as harness:
        current = harness.getModel("alpha-1")
        selector = ModelSelectorComponent(
            createFakeTui(), current, harness.session.modelRuntime, [], lambda *_: None, lambda: None
        )

        await waitFor(_refreshed(selector))

        # Current model (alpha-1) is sorted first, so selection starts on row 0.
        assert selectedModelId(stripAnsi("\n".join(selector.render(120)))) == "alpha-1"

        # Move selection down two rows to alpha-3.
        selector.handleInput("\x1b[B")
        selector.handleInput("\x1b[B")
        assert selectedModelId(stripAnsi("\n".join(selector.render(120)))) == "alpha-3"

        # Type a query that matches the three alpha models. The selection must
        # move back to the top row (alpha-1), not stay clamped at index 2.
        for char in "alpha":
            selector.handleInput(char)

        rendered = stripAnsi("\n".join(selector.render(120)))
        assert selectedModelId(rendered) == "alpha-1"
        # Sanity: the filter actually narrowed the list.
        assert "beta-1" not in rendered
        selector.dispose()


async def test_moves_selection_to_the_first_row_in_the_scoped_tab_when_typing_a_query():
    async with createHarness(
        models=[
            {"id": "alpha-1", "name": "Alpha One", "reasoning": True},
            {"id": "alpha-2", "name": "Alpha Two", "reasoning": True},
            {"id": "alpha-3", "name": "Alpha Three", "reasoning": True},
        ]
    ) as harness:
        alpha1 = harness.getModel("alpha-1")
        alpha2 = harness.getModel("alpha-2")
        alpha3 = harness.getModel("alpha-3")

        # Scoped list is intentionally not in current-model-first order; the
        # current model (alpha-1) sits at index 2.
        selector = ModelSelectorComponent(
            createFakeTui(),
            alpha1,
            harness.session.modelRuntime,
            [ScopedModelItem(model=alpha2), ScopedModelItem(model=alpha3), ScopedModelItem(model=alpha1)],
            lambda *_: None,
            lambda: None,
        )

        await waitFor(_refreshed(selector))

        # Selection starts on the current model (alpha-1), which is row 2 here.
        assert selectedModelId(stripAnsi("\n".join(selector.render(120)))) == "alpha-1"

        # Type a query matching all three scoped models. Selection must move to
        # the top row (alpha-2), not stay clamped at index 2 (alpha-1).
        for char in "alpha":
            selector.handleInput(char)

        assert selectedModelId(stripAnsi("\n".join(selector.render(120)))) == "alpha-2"
        selector.dispose()
