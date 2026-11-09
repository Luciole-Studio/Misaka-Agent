"""pi's ``model-selector.test.ts`` (``components/model_selector.py``)."""

from __future__ import annotations

import asyncio

import pytest

from misaka.ai.models import ModelsRefreshResult
from misaka.ai.types import Model
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.model_selector import (
    DefaultModelReference,
    ModelSelectorComponent,
)
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi

PROVIDER = "fixture"


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


def _model(id: str, name: str, provider: str = PROVIDER) -> Model:
    return Model.model_validate({
        "id": id, "name": name, "api": "openai-completions", "provider": provider,
        "baseUrl": "https://example.test/v1", "reasoning": True, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100,
    })


class _Runtime:
    """The part of ModelRuntime the selector reads."""

    def __init__(self, models: list[Model], result: ModelsRefreshResult | None = None, error: str | None = None):
        self.models = models
        self.result = result or ModelsRefreshResult(aborted=False, errors={})
        self.error = error
        self.refreshes: list = []

    def getAvailableSnapshot(self):
        return list(self.models)

    def getModel(self, provider, id):
        return next((model for model in self.models if model.provider == provider and model.id == id), None)

    def getError(self):
        return self.error

    async def refresh(self, options=None):
        self.refreshes.append(options)
        return self.result


class _Tui:
    def requestRender(self, *_args):
        pass


def _row(selector: ModelSelectorComponent, id: str) -> str | None:
    lines = stripAnsi("\n".join(selector.render(120))).split("\n")
    return next((line.rstrip() for line in lines if f"{id} [" in line), None)


async def test_keeps_the_current_model_marked_while_browsing():
    current = _model("current-model", "Current Model")
    selector = ModelSelectorComponent(
        _Tui(), current, _Runtime([current, _model("browsed-model", "Browsed Model")]), [], lambda _m: None, lambda: None
    )

    assert _row(selector, "current-model") == f"→ ✓ current-model [{PROVIDER}]"
    selector.handleInput("\x1b[B")
    assert _row(selector, "current-model") == f"  ✓ current-model [{PROVIDER}]"
    assert _row(selector, "browsed-model") == f"→   browsed-model [{PROVIDER}]"
    selector.dispose()


async def test_uses_the_configured_save_binding(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager({"app.models.save": "ctrl+r"}))
    current = _model("current-model", "Current Model")
    saved: list = []
    selector = ModelSelectorComponent(
        _Tui(), current, _Runtime([current]), [], lambda _m: None, lambda: None, None, saved.append
    )

    assert "Ctrl+R to set as default" in stripAnsi("\n".join(selector.render(120)))
    selector.handleInput("\x13")
    assert saved == []
    selector.handleInput("\x12")
    assert saved == [current]


async def test_lists_every_catalog_that_failed_to_refresh():
    current = _model("current-model", "Current Model")
    runtime = _Runtime([current], ModelsRefreshResult(
        aborted=False, errors={"openai": RuntimeError("unavailable"), "anthropic": RuntimeError("unavailable")},
    ))
    selector = ModelSelectorComponent(_Tui(), current, runtime, [], lambda _m: None, lambda: None)

    for _ in range(50):
        rendered = stripAnsi("\n".join(selector.render(120)))
        if "Could not refresh 2" in rendered:
            break
        await asyncio.sleep(0.01)
    assert "Could not refresh 2 model catalogs (openai, anthropic); showing cached models." in rendered


# --- MISAKA: what the review found missing against 0.99.1 ------------------------------------


async def test_the_default_model_sorts_second_and_providers_sort_as_locale_compare_does():
    current = _model("c", "C", provider="zeta")
    default = _model("d", "D", provider="omega")
    others = [_model("x", "X", provider="xAI"), _model("y", "Y", provider="Beta")]
    selector = ModelSelectorComponent(
        _Tui(), current, _Runtime([*others, default, current]), [], lambda _m: None, lambda: None,
        defaultModel=DefaultModelReference(provider="omega", id="d"),
    )

    assert [item.provider for item in selector.allModels] == ["zeta", "omega", "Beta", "xAI"]
    selector.dispose()


async def test_the_refresh_status_is_shown_until_the_catalogs_answer():
    current = _model("current-model", "Current Model")
    selector = ModelSelectorComponent(_Tui(), current, _Runtime([current], error="stale error"), [],
                                      lambda _m: None, lambda: None)

    first = stripAnsi("\n".join(selector.render(120)))
    assert "Refreshing model catalogs…" in first and "stale error" not in first
    await asyncio.sleep(0.05)
    assert "stale error" in stripAnsi("\n".join(selector.render(120)))


async def test_closing_the_selector_aborts_its_refresh():
    started = asyncio.Event()

    class Hanging(_Runtime):
        async def refresh(self, options=None):
            self.refreshes.append(options)
            started.set()
            await asyncio.Event().wait()

    runtime = Hanging([_model("m", "M")])
    closed: list = []
    selector = ModelSelectorComponent(_Tui(), None, runtime, [], lambda _m: None, lambda: closed.append(True))
    await asyncio.wait_for(started.wait(), 1)

    selector.handleInput("\x1b")

    assert closed == [True]
    for _ in range(50):
        if runtime.refreshes[0].signal.aborted:
            break
        await asyncio.sleep(0.01)
    assert runtime.refreshes[0].signal.aborted


def test_a_late_done_from_a_closed_selector_leaves_the_newer_one_open():
    # interactive-mode.ts showSelector: each selector's done() is a no-op once another replaced it.
    from misaka.modes.interactive.interactive_mode import InteractiveMode

    class _Container:
        def __init__(self):
            self.children = []

        def clear(self):
            self.children = []

        def addChild(self, child):
            self.children.append(child)

    mode = InteractiveMode.__new__(InteractiveMode)
    mode.editorContainer = _Container()
    mode.editor = object()
    mode.ui = type("UI", (), {"setFocus": lambda self, _c: None, "requestRender": lambda self, *_a: None})()
    mode.activeSelectorToken = None
    mode.activeSelectorDispose = None
    mode._request_render = lambda *_a: None
    disposed: list = []
    dones: list = []

    def build(name):
        def builder(done):
            dones.append(done)
            return {"component": name, "focus": name, "dispose": lambda: disposed.append(name)}

        return builder

    mode.showSelector(build("model"))
    mode.showSelector(build("settings"))
    assert disposed == ["model"]

    dones[0]()  # the model switch finished after /settings opened

    assert mode.editorContainer.children == ["settings"]
    dones[1]()
    assert mode.editorContainer.children == [mode.editor]
    assert disposed == ["model", "model", "settings"]


# --- interactive-mode.ts argument completions ------------------------------------------------


def test_model_completions_match_fuzzily_on_id_and_name():
    from types import SimpleNamespace

    from misaka.modes.interactive.interactive_mode import InteractiveMode

    opus = _model("claude-opus-4-7", "Claude Opus 4.7", provider="anthropic")
    session = SimpleNamespace(
        scopedModels=[],
        modelRuntime=_Runtime([opus, _model("gpt-5", "GPT-5", provider="openai")]),
        promptTemplates=[],
        extensionRunner=SimpleNamespace(getRegisteredCommands=list),
        moments=SimpleNamespace(commands=list),
    )
    mode = SimpleNamespace(session=session, sessionManager=SimpleNamespace(getCwd=lambda: "."), fdPath=None)
    provider = InteractiveMode.createBaseAutocompleteProvider(mode)
    modelCommand = next(command for command in provider.commands if command.name == "model")

    for query in ("opus47", "Claude Opus"):
        items = modelCommand.getArgumentCompletions(query)
        assert [(item.value, item.label, item.description) for item in items] == [
            ("anthropic/claude-opus-4-7", "claude-opus-4-7", "anthropic")
        ]


def test_login_completions_merge_auth_types_and_sort_as_locale_compare_does():
    from misaka.modes.interactive.components.oauth_selector import AuthSelectorProvider
    from misaka.modes.interactive.interactive_mode import (
        createFuzzyAutocompleteItems,
        formatLoginProviderCompletionDescription,
        getLoginProviderCompletionOptions,
        getLoginProviderSearchText,
    )

    options = getLoginProviderCompletionOptions([
        AuthSelectorProvider(id="anthropic", name="Anthropic", authType="api_key"),
        AuthSelectorProvider(id="anthropic", name="Anthropic", authType="oauth"),
        AuthSelectorProvider(id="xai", name="xAI", authType="api_key"),
        AuthSelectorProvider(id="zai", name="Z.AI", authType="api_key"),
    ])

    assert [(option.id, option.authTypes) for option in options] == [
        ("anthropic", ["oauth", "api_key"]), ("xai", ["api_key"]), ("zai", ["api_key"]),
    ]
    items = createFuzzyAutocompleteItems(options, "anth", getLoginProviderSearchText, lambda option: option)
    assert [formatLoginProviderCompletionDescription(item) for item in items] == ["Anthropic · subscription/API key"]

    options = getLoginProviderCompletionOptions([
        AuthSelectorProvider(id="anthropic", name="Anthropic", authType="oauth", subscription=True),
        AuthSelectorProvider(id="anthropic", name="Anthropic", authType="api_key", subscription=True),
        AuthSelectorProvider(id="openai", name="OpenAI", authType="api_key"),
        AuthSelectorProvider(id="radius", name="Radius", authType="oauth", subscription=False),
    ])
    items = createFuzzyAutocompleteItems(options, "anthrop", getLoginProviderSearchText, lambda option: option)
    assert [formatLoginProviderCompletionDescription(item) for item in items] == ["Anthropic · subscription/API key"]
    # OAuth sign-in without a subscription, such as Radius, is an account.
    items = createFuzzyAutocompleteItems(options, "radius", getLoginProviderSearchText, lambda option: option)
    assert [formatLoginProviderCompletionDescription(item) for item in items] == ["Radius · account"]
    assert getLoginProviderSearchText(items[0]) == "radius Radius oauth account"
