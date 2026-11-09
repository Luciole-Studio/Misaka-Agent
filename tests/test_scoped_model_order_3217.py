"""Translated from pi's ``coding-agent/test/suite/regressions/3217-scoped-model-order.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.model_selector import (
    ModelSelectorComponent,
    ScopedModelItem,
)
from misaka.modes.interactive.components.scoped_models_selector import (
    ModelsCallbacks,
    ModelsConfig,
    ScopedModelsSelectorComponent,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi
from tests.suite_harness import createHarness


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
    # Ensure test isolation: keybindings are a global singleton
    setKeybindings(KeybindingsManager())


MODELS = [
    {"id": "faux-1", "name": "One", "reasoning": True},
    {"id": "faux-2", "name": "Two", "reasoning": True},
    {"id": "faux-3", "name": "Three", "reasoning": True},
]


async def test_propagates_reordered_scoped_models_back_to_the_session_state():
    async with createHarness(models=MODELS) as harness:
        orderedIds = [f"{model.provider}/{model.id}" for model in harness.models]
        changes: list[list[str] | None] = []
        selector = ScopedModelsSelectorComponent(
            ModelsConfig(allModels=[*harness.models], enabledModelIds=orderedIds),
            ModelsCallbacks(onChange=changes.append, onPersist=lambda _ids: None, onCancel=lambda: None),
        )

        selector.handleInput("\x1b[1;3B")

        assert changes == [[orderedIds[1], orderedIds[0], orderedIds[2]]]


async def test_preserves_scoped_model_order_in_the_model_scoped_tab():
    async with createHarness(models=MODELS) as harness:
        modelOne = harness.getModel("faux-1")
        modelTwo = harness.getModel("faux-2")
        modelThree = harness.getModel("faux-3")
        selector = ModelSelectorComponent(
            SimpleNamespace(requestRender=lambda *_args: None),
            modelOne,
            harness.session.modelRuntime,
            [ScopedModelItem(model=modelTwo), ScopedModelItem(model=modelOne), ScopedModelItem(model=modelThree)],
            lambda *_: None,
            lambda: None,
        )

        def refreshed() -> None:
            rendered = stripAnsi("\n".join(selector.render(120)))
            assert f"[{modelOne.provider}]" in rendered
            assert "Model catalogs refreshed." in rendered

        await waitFor(refreshed)

        renderedLines = [
            line for line in stripAnsi("\n".join(selector.render(120))).split("\n") if f"[{modelOne.provider}]" in line
        ]
        orderedIds = [
            line.strip().removeprefix("→").lstrip().split(" [")[0].removeprefix("✓").strip() for line in renderedLines[:3]
        ]

        assert orderedIds == [modelTwo.id, modelOne.id, modelThree.id]
        selector.dispose()
