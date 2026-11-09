"""Translated from pi's ``packages/coding-agent/test/sdk-openrouter-attribution.test.ts`` (v1.1.0).

MISAKA fork: the attribution values are MISAKA's (``CLIENT_NAME``/``CLIENT_URL`` in ``core/provider_attribution.py``)
where pi sends "pi", "Pi" and https://pi.dev; the telemetry switch is ``MISAKA_TELEMETRY`` (pi: ``PI_TELEMETRY``)
and install telemetry defaults off, so the default-state cases turn it on first.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import Model
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext
from misaka.core.auth_storage import AuthStorage
from misaka.core.provider_attribution import CLIENT_NAME, CLIENT_URL
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import NewSessionOptions, SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry, getModelRuntime


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cwd = tmp_path / "project"
    agentDir = tmp_path / "agent"
    cwd.mkdir(parents=True)
    agentDir.mkdir(parents=True)
    monkeypatch.delenv("MISAKA_TELEMETRY", raising=False)
    return SimpleNamespace(cwd=str(cwd), agentDir=str(agentDir))


def createModel(provider: str, baseUrl: str, id: str | None = None) -> Model:
    return Model.model_validate({
        "id": id if id is not None else f"{provider}-test-model",
        "name": f"{provider} Test Model",
        "api": "openai-completions",
        "provider": provider,
        "baseUrl": baseUrl,
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000,
        "maxTokens": 4096,
    })


def createDoneStream() -> Any:
    stream = createAssistantMessageEventStream()
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "api": "openai-completions",
        "provider": "capture-provider",
        "model": "capture-model",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    }
    stream.end(message)
    return stream


async def captureHeaders(
    dirs: SimpleNamespace,
    model: Model,
    *,
    telemetryEnabled: bool | None = None,
    providerHeaders: dict[str, str] | None = None,
    requestHeaders: dict[str, str] | None = None,
    sessionId: str | None = None,
) -> dict[str, str] | None:
    settingsManager = SettingsManager.create(dirs.cwd, dirs.agentDir)
    if telemetryEnabled is False:
        settingsManager.setEnableInstallTelemetry(False)
    else:
        # MISAKA fork: install telemetry defaults off (pi: on), so pi's default state is set explicitly.
        settingsManager.setEnableInstallTelemetry(True)

    authStorage = AuthStorage.inMemory({model.provider: {"type": "api_key", "key": "test-api-key"}})
    modelRegistry = await createInMemoryModelRegistry(authStorage)
    captured: dict[str, Any] = {}

    def streamSimple(_model: Any, _context: Any, providerOptions: Any = None) -> Any:
        captured["options"] = providerOptions
        return createDoneStream()

    modelRegistry.registerProvider(
        model.provider,
        {"api": model.api, "headers": providerHeaders, "streamSimple": streamSimple},
    )

    modelRuntime = getModelRuntime(modelRegistry)
    sessionManager = SessionManager.inMemory(dirs.cwd)
    if sessionId:
        sessionManager.newSession(NewSessionOptions(id=sessionId))

    session = (
        await createAgentSession({
            "cwd": dirs.cwd,
            "agentDir": dirs.agentDir,
            "model": model,
            "modelRuntime": modelRuntime,
            "settingsManager": settingsManager,
            "sessionManager": sessionManager,
        })
    )["session"]

    try:
        stream = await session.agent.streamFunction(
            model,
            normalizeContext({"messages": []}),
            {"sessionId": session.sessionId, **({"headers": requestHeaders} if requestHeaders else {})},
        )
        await stream.result()
        return read_field(captured.get("options"), "headers")
    finally:
        session.dispose()
        modelRegistry.unregisterProvider(model.provider)


def header(headers: dict[str, str] | None, name: str) -> str | None:
    return (headers or {}).get(name)


async def test_adds_default_attribution_headers_for_openrouter_models(dirs: SimpleNamespace):
    headers = await captureHeaders(dirs, createModel("openrouter", "https://openrouter.ai/api/v1"))

    assert header(headers, "HTTP-Referer") == CLIENT_URL
    assert header(headers, "X-OpenRouter-Title") == CLIENT_NAME
    assert header(headers, "X-OpenRouter-Categories") == "cli-agent"


async def test_does_not_add_attribution_headers_when_telemetry_is_disabled(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs, createModel("openrouter", "https://openrouter.ai/api/v1"), telemetryEnabled=False
    )

    assert header(headers, "HTTP-Referer") is None
    assert header(headers, "X-OpenRouter-Title") is None
    assert header(headers, "X-OpenRouter-Categories") is None


async def test_adds_attribution_headers_for_custom_providers_routed_through_openrouter(dirs: SimpleNamespace):
    headers = await captureHeaders(dirs, createModel("custom-openrouter", "https://openrouter.ai/api/v1"))

    assert header(headers, "HTTP-Referer") == CLIENT_URL
    assert header(headers, "X-OpenRouter-Title") == CLIENT_NAME
    assert header(headers, "X-OpenRouter-Categories") == "cli-agent"


async def test_preserves_legacy_openrouter_base_url_substring_attribution_matching(dirs: SimpleNamespace):
    headers = await captureHeaders(dirs, createModel("custom-openrouter", "not-a-url-openrouter.ai"))

    assert header(headers, "HTTP-Referer") == CLIENT_URL
    assert header(headers, "X-OpenRouter-Title") == CLIENT_NAME
    assert header(headers, "X-OpenRouter-Categories") == "cli-agent"


async def test_lets_provider_and_request_headers_override_the_defaults(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs,
        createModel("openrouter", "https://openrouter.ai/api/v1"),
        providerHeaders={
            "HTTP-Referer": "https://provider.example",
            "X-OpenRouter-Categories": "provider-category",
        },
        requestHeaders={"X-OpenRouter-Title": "request-title"},
    )

    assert header(headers, "HTTP-Referer") == "https://provider.example"
    assert header(headers, "X-OpenRouter-Title") == "request-title"
    assert header(headers, "X-OpenRouter-Categories") == "provider-category"


async def test_adds_default_attribution_headers_for_direct_nvidia_nim_endpoints(dirs: SimpleNamespace):
    headers = await captureHeaders(dirs, createModel("custom-nim", "https://integrate.api.nvidia.com/v1"))

    assert header(headers, "X-BILLING-INVOKE-ORIGIN") == CLIENT_NAME


async def test_adds_default_attribution_headers_for_the_nvidia_provider(dirs: SimpleNamespace):
    headers = await captureHeaders(dirs, createModel("nvidia", "https://example.test/v1"))

    assert header(headers, "X-BILLING-INVOKE-ORIGIN") == CLIENT_NAME


async def test_does_not_add_nvidia_nim_attribution_headers_when_telemetry_is_disabled(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs, createModel("nvidia", "https://integrate.api.nvidia.com/v1"), telemetryEnabled=False
    )

    assert header(headers, "X-BILLING-INVOKE-ORIGIN") is None


async def test_lets_provider_and_request_headers_override_nvidia_nim_defaults(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs,
        createModel("nvidia", "https://integrate.api.nvidia.com/v1"),
        providerHeaders={"X-BILLING-INVOKE-ORIGIN": "Provider"},
        requestHeaders={"X-BILLING-INVOKE-ORIGIN": "Request"},
    )

    assert header(headers, "X-BILLING-INVOKE-ORIGIN") == "Request"


async def test_does_not_add_nvidia_nim_attribution_headers_for_nvidia_models_routed_through_openrouter(
    dirs: SimpleNamespace,
):
    headers = await captureHeaders(
        dirs, createModel("openrouter", "https://openrouter.ai/api/v1", "nvidia/nemotron-3-super-120b-a12b")
    )

    assert header(headers, "HTTP-Referer") == CLIENT_URL
    assert header(headers, "X-BILLING-INVOKE-ORIGIN") is None


async def test_does_not_add_nvidia_nim_attribution_headers_for_nvidia_models_routed_through_vercel_ai_gateway(
    dirs: SimpleNamespace,
):
    headers = await captureHeaders(
        dirs,
        createModel("vercel-ai-gateway", "https://ai-gateway.vercel.sh/v1", "nvidia/nemotron-3-super-120b-a12b"),
    )

    assert header(headers, "X-BILLING-INVOKE-ORIGIN") is None


async def test_adds_opencode_session_headers(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs, createModel("opencode", "https://opencode.ai/zen/v1"), sessionId="opencode-session"
    )

    assert header(headers, "x-opencode-session") == "opencode-session"
    assert header(headers, "x-opencode-client") == CLIENT_NAME


async def test_lets_configured_opencode_headers_override_the_defaults(dirs: SimpleNamespace):
    headers = await captureHeaders(
        dirs,
        createModel("opencode", "https://opencode.ai/zen/v1"),
        sessionId="opencode-session",
        providerHeaders={
            "x-opencode-session": "configured-session",
            "x-opencode-client": "configured-client",
        },
    )

    assert header(headers, "x-opencode-session") == "configured-session"
    assert header(headers, "x-opencode-client") == "configured-client"
