"""Translated from pi's ``coding-agent/test/suite/regressions/7027-credential-refresh-hang.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthResult,
    ModelAuth,
    ProviderAuth,
)
from misaka.ai.models import ModelsRefreshOptions
from misaka.core.agent_session import AgentSession
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_resolver import defaultModelPerProvider
from misaka.modes.interactive import interactive_mode
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.utils.js import startTask
from tests.fake_timers import FakeTimers
from tests.suite_harness import createHarness
from tests.test_model_runtime import _interaction, _model, _unused, runtimeWithProvider


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def timers(monkeypatch):
    """vitest's fake timers for the 15-second refresh deadline."""
    fake = FakeTimers()
    monkeypatch.setattr(interactive_mode, "setTimeout", fake.setTimeout)
    monkeypatch.setattr(interactive_mode, "clearTimeout", fake.clearTimeout)
    return fake


def abortableRefresh(state: dict):
    def refresh(options):
        future = asyncio.get_running_loop().create_future()

        def resolve(result):
            if not future.done():
                future.set_result(result)

        state["finish"] = lambda: resolve(SimpleNamespace(aborted=False, errors={}))
        signal = options.signal
        if signal is not None:
            signal.addEventListener("abort", lambda: resolve(SimpleNamespace(aborted=True, errors={})), True)
        return future

    return Mock(side_effect=refresh)


def loginContext(session):
    tasks: list[asyncio.Future] = []

    def scheduleTask(work):
        # `void promise`: the work starts at once, as the JavaScript call does.
        if asyncio.iscoroutine(work):
            tasks.append(startTask(work))

    return SimpleNamespace(
        session=session,
        updateAvailableProviderCount=Mock(),
        footer=SimpleNamespace(invalidate=Mock()),
        updateEditorBorderColor=Mock(),
        showStatus=Mock(),
        showError=Mock(),
        showWarning=Mock(),
        maybeWarnAboutAnthropicSubscriptionAuth=Mock(),
        ui=SimpleNamespace(requestRender=Mock()),
        _schedule_task=scheduleTask,
    )


async def test_does_not_hold_login_behind_an_older_stalled_network_catalog_refresh():
    networkStarted = asyncio.Event()

    async def login(_interaction):
        return ApiKeyCredential(key="secret")

    async def check(*, ctx, credential, signal):
        return AuthCheck(type="api_key", source="stored key") if credential is not None and credential.key else None

    async def resolve(*, ctx, credential, signal):
        key = credential.key if credential is not None else None
        return AuthResult(auth=ModelAuth(apiKey=key or "ambient-key"), source="stored key" if key else "ambient key")

    async def refreshModels(context):
        if not context.allowNetwork:
            return
        networkStarted.set()
        await asyncio.get_running_loop().create_future()

    dynamicModel = _model("stalled-login")
    stalledProvider = SimpleNamespace(
        id="stalled-login",
        name="Stalled Login",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="API key", login=login, check=check, resolve=resolve)),
        getModels=lambda: [dynamicModel],
        refreshModels=refreshModels,
        stream=_unused,
        streamSimple=_unused,
    )
    storage = AuthStorage.inMemory()
    credentials = storage
    runtime = await runtimeWithProvider(stalledProvider, storage)

    stalledRefresh = asyncio.ensure_future(
        runtime.refresh(ModelsRefreshOptions(allowNetwork=True, providers=["stalled-login"]))
    )
    await networkStarted.wait()
    assert await runtime.login("stalled-login", "api_key", _interaction()) == ApiKeyCredential(key="secret")

    assert dynamicModel.id in [model.id for model in runtime.getAvailableSnapshot()]
    assert await credentials.read("stalled-login") == ApiKeyCredential(key="secret")
    result = await stalledRefresh
    assert result.aborted is False


async def test_completes_interactive_login_before_its_bounded_background_refresh(monkeypatch, timers):
    async with createHarness() as harness:
        runtime = harness.session.modelRuntime
        refresh = abortableRefresh({})
        monkeypatch.setattr(runtime, "refresh", refresh)
        context = loginContext(harness.session)

        await InteractiveMode.completeProviderAuthentication(
            context, "stalled-login", "Stalled Login", "api_key", harness.getModel()
        )
        options = refresh.call_args.args[0]
        assert options.providers == ["stalled-login"]
        assert options.signal is not None
        context.showWarning.assert_not_called()

        timers.advance(15_000)
        await settle()
        context.showWarning.assert_called_with(
            "Saved API key for Stalled Login, but its model catalog refresh timed out; using cached models."
        )


class PostLogin:
    def __init__(self, harness, monkeypatch, timers) -> None:
        self.harness = harness
        self.timers = timers
        session = harness.session
        model = harness.getModel()
        self.model = model
        self.unknownModel = model.model_copy(update={"id": "unknown", "provider": "unknown", "api": "unknown"})
        self.state: dict = {"model": self.unknownModel, "available": []}
        monkeypatch.setattr(AgentSession, "model", property(lambda _self: self.state["model"]))
        monkeypatch.setattr(session.modelRuntime, "getAvailableSnapshot", lambda: self.state["available"])
        self.refreshState: dict = {}
        monkeypatch.setattr(session.modelRuntime, "refresh", abortableRefresh(self.refreshState))
        self.setModel = Mock(side_effect=lambda *_args, **_kwargs: asyncio.sleep(0))
        monkeypatch.setattr(session, "setModel", self.setModel)
        self.context = loginContext(session)

    async def start(self) -> None:
        await InteractiveMode.completeProviderAuthentication(self.context, "radius", "Radius", "oauth", self.unknownModel)
        assert "Credentials saved" in self.context.showStatus.call_args.args[0]
        self.context.showError.assert_not_called()
        self.setModel.assert_not_called()

    async def discover(self, ids: list[str]) -> None:
        self.state["available"] = [self.model.model_copy(update={"provider": "radius", "id": id}) for id in ids]
        self.refreshState["finish"]()
        await settle()


@pytest.mark.parametrize(
    ("models", "selected"),
    [(["fast", "balanced"], "balanced"), (["fast", "powerful"], "fast")],
)
async def test_selects_the_default_from_the_refreshed_catalog(monkeypatch, timers, models, selected):
    assert defaultModelPerProvider["radius"] == "balanced"
    async with createHarness() as harness:
        login = PostLogin(harness, monkeypatch, timers)
        await login.start()
        await login.discover(models)
        args, _kwargs = login.setModel.call_args
        assert (args[0].provider, args[0].id) == ("radius", selected)
        assert args[1] == {"persist": True}
        login.context.showError.assert_not_called()


async def test_reports_an_empty_catalog_only_after_refresh(monkeypatch, timers):
    async with createHarness() as harness:
        login = PostLogin(harness, monkeypatch, timers)
        await login.start()
        await login.discover([])
        login.setModel.assert_not_called()
        assert "no models are available" in login.context.showError.call_args.args[0]


async def test_preserves_a_model_selected_during_refresh(monkeypatch, timers):
    async with createHarness() as harness:
        login = PostLogin(harness, monkeypatch, timers)
        await login.start()
        login.state["model"] = harness.getModel()
        await login.discover(["fast", "balanced"])
        login.setModel.assert_not_called()
        login.context.showError.assert_not_called()


async def test_bounds_refresh_to_15_seconds(monkeypatch, timers):
    async with createHarness() as harness:
        login = PostLogin(harness, monkeypatch, timers)
        await login.start()
        timers.advance(15_000)
        await settle()
        assert "timed out" in login.context.showWarning.call_args.args[0]
        assert "no models are available" in login.context.showError.call_args.args[0]
        login.setModel.assert_not_called()
