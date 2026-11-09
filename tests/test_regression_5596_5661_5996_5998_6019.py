"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``5596-missing-theme-export.test.ts``,
``5661-uppercase-header-values.test.ts``, ``5996-session-name-newlines.test.ts``,
``5998-blocked-tool-terminate.test.ts`` and ``6019-explicit-provider-retry-message.test.ts`` (v1.1.0).

MISAKA fork: ``5868-rpc-unknown-command-id.test.ts`` is not translated; MISAKA has no RPC mode.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from misaka.agent import Agent
from misaka.agent.types import AgentTool
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import registerFauxProvider, streamSimple
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.messages import convertToLlm
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme.theme import initTheme
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import (
    createInMemoryModelRegistry,
    createModelRegistry,
    getModelRuntime,
)
from tests.suite_harness import (
    createHarness,
    createTestResourceLoader,
    getAssistantTexts,
)

# --- regression #5596: missing configured theme export ------------------------------------------


@pytest.fixture
async def cleanups():
    pending: list[Any] = []
    yield pending
    while pending:
        pending.pop()()
    initTheme("dark")


async def test_exports_with_the_active_fallback_theme_when_the_configured_theme_is_missing(cleanups):
    tempDir = tempfile.mkdtemp(prefix="pi-5596-")
    faux = registerFauxProvider({"models": [{"id": "faux-1", "reasoning": False}]})
    faux.setResponses([fauxAssistantMessage("hello")])

    model = faux.getModel()
    authStorage = AuthStorage.inMemory()

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="faux-key")

    await authStorage.modify(model.provider, apiKey)
    modelRegistry = await createInMemoryModelRegistry(authStorage)
    modelRegistry.registerProvider(model.provider, {
        "baseUrl": model.baseUrl,
        "apiKey": "faux-key",
        "api": faux.api,
        "models": [
            {
                "id": registeredModel.id,
                "name": registeredModel.name,
                "api": registeredModel.api,
                "reasoning": registeredModel.reasoning,
                "input": registeredModel.input,
                "cost": registeredModel.cost,
                "contextWindow": registeredModel.contextWindow,
                "maxTokens": registeredModel.maxTokens,
                "baseUrl": registeredModel.baseUrl,
            }
            for registeredModel in faux.models
        ],
    })

    settingsManager = SettingsManager.inMemory({"theme": "missing-theme"})
    sessionManager = SessionManager.create(tempDir, os.path.join(tempDir, "sessions"))
    agent = Agent(
        getApiKey=lambda *_: "faux-key",
        initialState={"model": model, "systemPrompt": "You are a test assistant.", "tools": []},
        convertToLlm=convertToLlm,
        streamFn=streamSimple,
    )
    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=tempDir,
        modelRuntime=getModelRuntime(modelRegistry),
        resourceLoader=createTestResourceLoader(),
    ))

    def cleanup() -> None:
        session.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

    cleanups.append(cleanup)

    await session.prompt("hi")
    initTheme(settingsManager.getTheme())

    outputPath = os.path.join(tempDir, "export.html")
    assert await session.exportToHtml(outputPath) == outputPath
    assert os.path.exists(outputPath)
    assert settingsManager.getTheme() == "missing-theme"


# --- regression #5661: uppercase models.json header values --------------------------------------


async def test_keeps_uppercase_header_strings_as_literals(monkeypatch: pytest.MonkeyPatch):
    # MISAKA fork: MISAKA has no startup migrations (pi's ``runMigrations``; MISAKA never shipped the layouts they
    # move), so this checks the half that does not depend on them: the registry keeps the uppercase strings as
    # literals, not as names of the variables set below.
    async with createHarness(withConfiguredAuth=False) as harness:
        for key in ["CUSTOM_API_KEY", "BEARER"]:
            monkeypatch.setenv(key, f"env-{key}")

        modelsPath = os.path.join(harness.tempDir, "models.json")
        Path(modelsPath).write_text(
            json.dumps(
                {
                    "providers": {
                        "my-provider": {
                            "baseUrl": "https://example.com/v1",
                            "apiKey": "CUSTOM_API_KEY",
                            "api": "openai-completions",
                            "headers": {"Authorization": "BEARER"},
                            "models": [{"id": "my-model"}],
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        registry = await createModelRegistry(AuthStorage.create(os.path.join(harness.tempDir, "auth.json")), modelsPath)
        model = registry.find("my-provider", "my-model")
        assert model is not None
        auth = await registry.getApiKeyAndHeaders(model)
        assert read_field(auth, "ok") is True
        assert read_field(auth, "apiKey") == "CUSTOM_API_KEY"
        assert read_field(read_field(auth, "headers"), "Authorization") == "BEARER"


# --- regression #5996: session names do not contain newlines ------------------------------------


async def test_filters_newlines_when_agent_session_set_session_name_is_called():
    async with createHarness() as harness:
        harness.session.setSessionName("hello\nworld\r\nagain")

        assert harness.sessionManager.getSessionName() == "hello world again"
        assert [read_field(event, "name") for event in harness.eventsOfType("session_info_changed")] == [
            "hello world again"
        ]


async def test_filters_newlines_when_an_extension_calls_pi_set_session_name():
    api: dict[str, Any] = {}
    async with createHarness(extensionFactories=[lambda pi: api.setdefault("pi", pi)]) as harness:
        api["pi"].setSessionName("from\nextension")

        assert harness.sessionManager.getSessionName() == "from extension"
        assert [read_field(event, "name") for event in harness.eventsOfType("session_info_changed")] == [
            "from extension"
        ]


# --- #5998 blocked tool termination -------------------------------------------------------------


async def test_lets_a_tool_call_handler_terminate_the_run_after_blocking_execution():
    async def execute(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("tool should have been blocked")

    echoTool = AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=execute,
    )

    def extension(pi: Any) -> None:
        async def onToolCall(_event: Any, _ctx: Any) -> Any:
            return {"block": True, "reason": "Blocked by terminating policy", "terminate": True}

        pi.on("tool_call", onToolCall)

    async with createHarness(tools=[echoTool], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("should not run"),
        ])

        await harness.session.prompt("hi")

        assert harness.getPendingResponseCount() == 1
        assert "should not run" not in getAssistantTexts(harness)
        assert read_field(read_field(harness.eventsOfType("tool_execution_end")[0], "result"), "terminate") is True
        assert any(
            read_field(message, "role") == "toolResult" and read_field(message, "isError")
            for message in harness.session.messages
        )


# --- regression: issue 6019 explicit provider retry messages ------------------------------------

openAIExplicitRetryMessage = (
    "An error occurred while processing your request. You can retry your request, or contact us through our help "
    "center at help.openai.com if the error persists. Please include the request ID req_******** in your message."
)
bedrockExplicitRetryMessage = (
    '{"message":"The system encountered an unexpected error during processing. Try your request again."}'
)


@pytest.mark.parametrize(
    ("_provider", "errorMessage"),
    [("openai", openAIExplicitRetryMessage), ("bedrock", bedrockExplicitRetryMessage)],
)
async def test_retries_explicit_retry_guidance(_provider: str, errorMessage: str):
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}) as harness:
        harness.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": errorMessage}),
            fauxAssistantMessage("recovered"),
        ])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 2
        assert [read_field(event, "errorMessage") for event in harness.eventsOfType("auto_retry_start")] == [
            errorMessage
        ]
        assert [read_field(event, "success") for event in harness.eventsOfType("auto_retry_end")] == [True]
