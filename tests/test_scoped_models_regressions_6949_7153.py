"""Translated from pi's ``coding-agent/test/suite/regressions/6949-unavailable-scoped-model.test.ts`` and
``7153-scoped-models-refresh.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.scoped_models_selector import (
    ModelsCallbacks,
    ModelsConfig,
    ScopedModelsSelectorComponent,
)
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme, theme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi
from tests.suite_harness import createHarness


@pytest.fixture(autouse=True)
def setup():
    initTheme("dark")
    setKeybindings(KeybindingsManager())


async def settle(condition, attempts: int = 100) -> None:
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0)
    assert condition()


def createInteractiveContext(*, allModels, enabledModelIds, scopedModels=None):
    selectors: list = []
    setScopedModels = Mock()
    getAvailableSnapshot = Mock(side_effect=lambda: allModels)
    tasks: list[asyncio.Future] = []

    def showSelector(factory):
        selectors.append(factory(lambda: None)["component"])

    context = SimpleNamespace(
        session=SimpleNamespace(
            modelRuntime=SimpleNamespace(
                refresh=AsyncMock(return_value=SimpleNamespace(aborted=False, errors={})),
                getAvailableSnapshot=getAvailableSnapshot,
            ),
            # MISAKA fork: the session keeps its scoped models as dicts.
            scopedModels=scopedModels or [],
            setScopedModels=setScopedModels,
        ),
        settingsManager=SimpleNamespace(getEnabledModels=lambda: enabledModelIds, setEnabledModels=Mock()),
        showStatus=Mock(),
        showSelector=showSelector,
        updateAvailableProviderCount=Mock(),
        ui=SimpleNamespace(requestRender=Mock()),
        _schedule_task=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
    )
    return SimpleNamespace(
        context=context,
        getAvailableSnapshot=getAvailableSnapshot,
        getSelector=lambda: selectors[-1] if selectors else None,
        setScopedModels=setScopedModels,
        tasks=tasks,
    )


# --- issue #6949 unavailable scoped models --------------------------------------------------------------------------


async def test_shows_and_removes_an_enabled_model_without_a_catalog_entry():
    async with createHarness(models=[{"id": "available", "name": "Available"}]) as harness:
        availableId = f"{harness.models[0].provider}/{harness.models[0].id}"
        unavailableId = f"{harness.models[0].provider}/unavailable"
        changes: list = []
        persisted: list = []
        selector = ScopedModelsSelectorComponent(
            ModelsConfig(allModels=[*harness.models], enabledModelIds=[unavailableId, availableId]),
            ModelsCallbacks(onChange=changes.append, onPersist=persisted.append, onCancel=lambda: None),
        )

        rendered = "\n".join(selector.render(100))
        assert f"{unavailableId} [unavailable]" in stripAnsi(rendered)
        assert theme.strikethrough(unavailableId) in rendered
        selector.handleInput("\r")
        assert changes == [[availableId]]
        selector.handleInput("\x13")
        assert persisted == [[availableId]]


async def test_passes_unmatched_settings_patterns_to_the_selector_with_one_combined_resolution():
    async with createHarness(models=[{"id": "available", "name": "Available"}]) as harness:
        unavailableIds = [f"{harness.models[0].provider}/{id}" for id in ("unavailable-one", "unavailable-two")]
        interactive = createInteractiveContext(allModels=[], enabledModelIds=unavailableIds)

        InteractiveMode.showModelsSelector(interactive.context)

        selector = interactive.getSelector()
        assert selector is not None
        rendered = stripAnsi("\n".join(selector.render(100)))
        for unavailableId in unavailableIds:
            assert f"{unavailableId} [unavailable]" in rendered
        interactive.getAvailableSnapshot.assert_called()
        await asyncio.gather(*interactive.tasks)


async def test_opens_when_only_a_session_scoped_model_is_unavailable():
    async with createHarness(models=[{"id": "unavailable", "name": "Unavailable"}]) as harness:
        model = harness.models[0]
        fullId = f"{model.provider}/{model.id}"
        interactive = createInteractiveContext(allModels=[], enabledModelIds=[], scopedModels=[{"model": model}])

        InteractiveMode.showModelsSelector(interactive.context)

        selector = interactive.getSelector()
        assert selector is not None
        assert f"{fullId} [unavailable]" in stripAnsi("\n".join(selector.render(100)))
        await asyncio.gather(*interactive.tasks)


async def test_does_not_clear_a_partial_scope_when_an_enabled_model_is_unavailable():
    async with createHarness(
        models=[{"id": "one", "name": "One"}, {"id": "two", "name": "Two"}, {"id": "three", "name": "Three"}]
    ) as harness:
        one, two, _three = harness.models
        enabledIds = [f"{model.provider}/{model.id}" for model in (one, two)]
        unavailableId = f"{one.provider}/unavailable"
        interactive = createInteractiveContext(
            allModels=[*harness.models],
            enabledModelIds=[*enabledIds, unavailableId],
            scopedModels=[{"model": one}, {"model": two}],
        )

        InteractiveMode.showModelsSelector(interactive.context)
        selector = interactive.getSelector()
        assert selector is not None
        selector.handleInput("\x1b[1;3B")

        expected = [{"model": two, "thinkingLevel": None}, {"model": one, "thinkingLevel": None}]
        await settle(
            lambda: interactive.setScopedModels.call_args is not None
            and interactive.setScopedModels.call_args.args[0] == expected
        )
        await asyncio.gather(*interactive.tasks)


# --- issue #7153 scoped models refresh -----------------------------------------------------------------------------


def openSelector(harness, monkeypatch, initialModels):
    state: dict = {"snapshot": initialModels, "selector": None, "dispose": None}
    done = Mock()
    tasks: list[asyncio.Future] = []

    async def refresh(options):
        future = asyncio.get_running_loop().create_future()
        state["refreshSignal"] = options.signal
        state["finishRefresh"] = future.set_result
        return await future

    monkeypatch.setattr(harness.session.modelRuntime, "getAvailableSnapshot", lambda: state["snapshot"])
    monkeypatch.setattr(harness.session.modelRuntime, "refresh", Mock(side_effect=refresh))

    def showSelector(factory):
        def close():
            if state["dispose"]:
                state["dispose"]()
            done()

        created = factory(close)
        state["selector"] = created["component"]
        state["dispose"] = created.get("dispose")

    context = SimpleNamespace(
        session=harness.session,
        settingsManager=harness.settingsManager,
        showSelector=showSelector,
        updateAvailableProviderCount=Mock(),
        ui=SimpleNamespace(requestRender=Mock()),
        _schedule_task=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
    )

    InteractiveMode.showModelsSelector(context)
    assert state["selector"] is not None

    def complete(models, result):
        state["snapshot"] = models
        state["finishRefresh"](result)

    return SimpleNamespace(done=done, state=state, selector=state["selector"], complete=complete, tasks=tasks)


async def test_renders_cached_models_immediately_and_updates_after_background_refresh(monkeypatch):
    async with createHarness(models=[{"id": "cached", "name": "Cached"}, {"id": "refreshed", "name": "Refreshed"}]) as harness:
        refresh = openSelector(harness, monkeypatch, [harness.models[0]])

        initial = stripAnsi("\n".join(refresh.selector.render(100)))
        assert "cached" in initial
        assert "Refreshing model catalogs…" in initial
        assert "refreshed" not in initial

        await settle(lambda: "finishRefresh" in refresh.state)
        refresh.complete(harness.models, SimpleNamespace(aborted=False, errors={}))

        def updated() -> bool:
            rendered = stripAnsi("\n".join(refresh.selector.render(100)))
            return "refreshed" in rendered and "Model catalogs refreshed." in rendered

        await settle(updated)
        await asyncio.gather(*refresh.tasks)


async def test_cancels_the_background_refresh_when_the_selector_closes(monkeypatch):
    async with createHarness(models=[{"id": "cached", "name": "Cached"}]) as harness:
        refresh = openSelector(harness, monkeypatch, harness.models)

        await settle(lambda: refresh.state.get("refreshSignal") is not None)
        refresh.selector.handleInput("\x1b")
        await settle(lambda: refresh.state["refreshSignal"].aborted is True)
        refresh.done.assert_called_once_with()
        await asyncio.gather(*refresh.tasks, return_exceptions=True)
