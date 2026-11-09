"""pi's ``scoped-models-selector.test.ts`` (``components/scoped_models_selector.py``)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from misaka.ai.types import Model
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.scoped_models_selector import (
    ModelsCallbacks,
    ModelsConfig,
    ScopedModelsSelectorComponent,
    clearAll,
    enableAll,
    toggle,
)
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi

PROVIDER = "fixture"


@dataclass
class ModelState:
    id: str
    name: str
    enabled: bool


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


def _model(id: str, name: str) -> Model:
    return Model.model_validate({
        "id": id, "name": name, "api": "openai-completions", "provider": PROVIDER,
        "baseUrl": "https://example.test/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100,
    })


def _render(selector: ScopedModelsSelectorComponent, width: int = 120) -> str:
    return stripAnsi("\n".join(selector.render(width)))


def getMarkerStates(selector: ScopedModelsSelectorComponent, models: list[ModelState]) -> list[bool]:
    lines = _render(selector).split("\n")
    states = []
    for model in models:
        line = next((candidate for candidate in lines if f"{model.id} [" in candidate), None)
        if line is None:
            raise AssertionError(f"Expected rendered row for {model.id}")
        states.append(line[2:].startswith("✓ "))
    return states


def createSelector(models: list[ModelState]) -> ScopedModelsSelectorComponent:
    def onChange(enabledModelIds):
        for model in models:
            model.enabled = enabledModelIds is None or f"{PROVIDER}/{model.id}" in enabledModelIds

    return ScopedModelsSelectorComponent(
        ModelsConfig(
            allModels=[_model(model.id, model.name) for model in models],
            enabledModelIds=[f"{PROVIDER}/{model.id}" for model in models if model.enabled],
        ),
        ModelsCallbacks(onChange=onChange, onPersist=lambda _ids: None, onCancel=lambda: None),
    )


def _three(*enabled: bool) -> list[ModelState]:
    return [ModelState(f"model-{letter}", f"Model {letter.upper()}", state) for letter, state in zip("abc", enabled, strict=True)]


def test_marks_every_model_after_enabling_all():
    models = _three(True, False, False)
    selector = createSelector(models)

    selector.handleInput("\x01")

    assert [model.enabled for model in models] == [True, True, True]
    assert getMarkerStates(selector, models) == [True, True, True]
    assert "all enabled" in _render(selector)


def test_disables_only_the_selected_model_after_enabling_all():
    models = _three(True, False, False)
    selector = createSelector(models)

    selector.handleInput("\x01")
    selector.handleInput("\r")

    assert [model.enabled for model in models] == [False, True, True]
    assert getMarkerStates(selector, models) == [False, True, True]


def test_enables_only_the_selected_model_after_clearing_all():
    models = _three(True, True, True)
    selector = createSelector(models)

    selector.handleInput("\x18")
    assert [model.enabled for model in models] == [False, False, False]
    assert getMarkerStates(selector, models) == [False, False, False]

    selector.handleInput("\r")
    assert [model.enabled for model in models] == [True, False, False]
    assert getMarkerStates(selector, models) == [True, False, False]


def test_restores_the_all_enabled_state_after_re_enabling_the_last_disabled_model():
    models = _three(True, False, False)
    selector = createSelector(models)

    selector.handleInput("\x01")  # enable all -> None
    selector.handleInput("\r")  # disable model-a; enabled models re-sort first: [b, c, a]
    selector.handleInput("\x1b[B")
    selector.handleInput("\x1b[B")  # move selection back to model-a
    selector.handleInput("\r")  # re-enable model-a

    assert [model.enabled for model in models] == [True, True, True]
    assert getMarkerStates(selector, models) == [True, True, True]
    assert "all enabled" in _render(selector)


# --- MISAKA: the `??` and normalizeEnabled semantics the review found missing ---------------


def test_an_empty_target_list_acts_on_nothing():
    # A search that matches nothing passes [] -- not "every model".
    assert enableAll(["a"], ["a", "b"], []) == ["a"]
    assert clearAll(["a", "b"], ["a", "b", "c"], []) == ["a", "b"]
    assert clearAll(None, ["a", "b"], []) == ["a", "b"]
    assert clearAll(None, ["a", "b"]) == []


def test_all_enabled_needs_every_available_id_not_just_the_count():
    # "x" is a saved pattern that matches nothing; it does not stand in for "c".
    assert enableAll(["a", "x"], ["a", "b", "c"], ["b"]) == ["a", "x", "b"]
    assert toggle(["a", "b"], ["a", "b", "c"], "c") is None
    assert toggle(None, ["a", "b", "c"], "b") == ["a", "c"]


def test_an_unavailable_enabled_id_is_listed_struck_through_and_counted():
    selector = ScopedModelsSelectorComponent(
        ModelsConfig(allModels=[_model("model-a", "Model A")], enabledModelIds=[f"{PROVIDER}/model-a", "gone/model"]),
        ModelsCallbacks(onChange=lambda _ids: None, onPersist=lambda _ids: None, onCancel=lambda: None),
    )

    text = _render(selector, 200)
    assert "gone/model [unavailable]" in text
    assert "1/1 enabled · 1 unavailable" in text


def test_refreshed_models_keep_the_selected_row_and_show_the_status():
    models = [_model("model-a", "Model A"), _model("model-b", "Model B")]
    selector = ScopedModelsSelectorComponent(
        ModelsConfig(allModels=models, enabledModelIds=None, refreshStatus="Refreshing model catalogs…"),
        ModelsCallbacks(onChange=lambda _ids: None, onPersist=lambda _ids: None, onCancel=lambda: None),
    )
    selector.handleInput("\x1b[B")
    assert "Refreshing model catalogs…" in _render(selector)

    selector.updateModels([_model("model-0", "Model 0"), *models])
    selector.setRefreshStatus("Model catalogs refreshed.", "success")

    assert "→ ✓ model-b [fixture]" in _render(selector)
    assert "Model catalogs refreshed." in _render(selector)
