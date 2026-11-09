"""Translated from pi's ``packages/coding-agent/test/agent-session-dynamic-provider.test.ts`` (v1.1.0)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthResult,
    ModelAuth,
    ProviderAuth,
)
from misaka.ai.compat import getModel
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager


def nativeAnthropicProvider(baseUrl: str) -> Any:
    model = getModel("anthropic", "claude-sonnet-4-5").model_copy(update={"baseUrl": baseUrl})

    async def resolve(**_: Any) -> AuthResult:
        return AuthResult(auth=ModelAuth(apiKey="test-key"), source="test")

    def unused(*_: Any) -> Any:
        raise RuntimeError("unused")

    return SimpleNamespace(
        id="anthropic",
        name="Native Anthropic",
        baseUrl=baseUrl,
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test API key", resolve=resolve)),
        getModels=lambda: [model],
        stream=unused,
        streamSimple=unused,
    )


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    agentDir = tmp_path / "agent"
    agentDir.mkdir(parents=True)
    return SimpleNamespace(tempDir=str(tmp_path), agentDir=agentDir)


async def createSession(dirs: SimpleNamespace, extensionFactories: list[Any]) -> Any:
    settingsManager = SettingsManager.create(dirs.tempDir, str(dirs.agentDir))
    sessionManager = SessionManager.inMemory()
    authStorage = AuthStorage.create(str(dirs.agentDir / "auth.json"))

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="test-key")

    await authStorage.modify("anthropic", apiKey)
    modelRuntime = await ModelRuntime.create({"authStorage": authStorage, "modelsPath": str(dirs.agentDir / "models.json")})
    resourceLoader = DefaultResourceLoader({
        "cwd": dirs.tempDir,
        "agentDir": str(dirs.agentDir),
        "settingsManager": settingsManager,
        "extensionFactories": extensionFactories,
    })
    await resourceLoader.reload()

    result = await createAgentSession({
        "cwd": dirs.tempDir,
        "agentDir": str(dirs.agentDir),
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "modelRuntime": modelRuntime,
        "resourceLoader": resourceLoader,
    })
    return result["session"]


async def capturePromptBaseUrl(session: Any) -> str | None:
    captured: dict[str, Any] = {}

    async def streamFunction(model: Any, *_: Any) -> Any:
        captured["baseUrl"] = model.baseUrl
        raise RuntimeError("stop")

    session.agent.streamFunction = streamFunction
    await session.prompt("hello")
    return captured.get("baseUrl")


async def test_applies_top_level_register_provider_overrides_to_the_active_model(dirs: SimpleNamespace):
    session = await createSession(
        dirs, [lambda pi: pi.registerProvider("anthropic", {"baseUrl": "http://localhost:8080/top-level"})]
    )
    try:
        assert session.model.baseUrl == "http://localhost:8080/top-level"
        assert await capturePromptBaseUrl(session) == "http://localhost:8080/top-level"
    finally:
        session.dispose()


async def test_applies_session_start_register_provider_overrides_to_the_active_model(dirs: SimpleNamespace):
    def extension(pi: Any) -> None:
        pi.on(
            "session_start",
            lambda _event, _ctx: pi.registerProvider("anthropic", {"baseUrl": "http://localhost:8080/session-start"}),
        )

    session = await createSession(dirs, [extension])
    try:
        await session.bindExtensions({})

        assert session.model.baseUrl == "http://localhost:8080/session-start"
        assert await capturePromptBaseUrl(session) == "http://localhost:8080/session-start"
    finally:
        session.dispose()


async def test_registers_native_pi_ai_providers_during_extension_loading(dirs: SimpleNamespace):
    session = await createSession(
        dirs, [lambda pi: pi.registerProvider(nativeAnthropicProvider("http://localhost:8080/native-top-level"))]
    )
    try:
        assert session.model.baseUrl == "http://localhost:8080/native-top-level"
        assert await capturePromptBaseUrl(session) == "http://localhost:8080/native-top-level"
    finally:
        session.dispose()


async def test_applies_command_time_register_provider_overrides_without_reload(dirs: SimpleNamespace):
    def extension(pi: Any) -> None:
        async def handler(_args: str, _ctx: Any) -> None:
            pi.registerProvider("anthropic", {"baseUrl": "http://localhost:8080/command"})

        pi.registerCommand("use-proxy", {"description": "Use proxy", "handler": handler})

    session = await createSession(dirs, [extension])
    try:
        await session.bindExtensions({})
        await session.prompt("/use-proxy")

        assert session.model.baseUrl == "http://localhost:8080/command"
        assert await capturePromptBaseUrl(session) == "http://localhost:8080/command"
    finally:
        session.dispose()


async def test_registers_native_pi_ai_providers_at_command_time(dirs: SimpleNamespace):
    def extension(pi: Any) -> None:
        async def handler(_args: str, _ctx: Any) -> None:
            pi.registerProvider(nativeAnthropicProvider("http://localhost:8080/native-command"))

        pi.registerCommand("use-native", {"description": "Use native provider", "handler": handler})

    session = await createSession(dirs, [extension])
    try:
        await session.bindExtensions({})
        await session.prompt("/use-native")

        assert session.model.baseUrl == "http://localhost:8080/native-command"
        assert await capturePromptBaseUrl(session) == "http://localhost:8080/native-command"
    finally:
        session.dispose()
