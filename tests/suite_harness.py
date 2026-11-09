"""pi coding-agent's ``test/suite/harness.ts``, with ``createTestExtensionsResult`` and
``createTestResourceLoader`` from ``test/utilities.ts``.

MISAKA fork: ``createHarness`` is an async context manager, so the cleanup runs on the way out;
``cwd`` is the temp directory as upstream, and the temp directory comes from ``tempfile``.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from misaka.agent import Agent
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import registerFauxProvider, streamSimple
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    loadExtensionFromFactory,
)
from misaka.core.extensions.types import LoadExtensionsResult
from misaka.core.messages import convertToLlm
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme.theme import theme
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import (
    createInMemoryModelRegistry,
    createModelRegistry,
    getModelRuntime,
)


def getMessageText(message: Any) -> str:
    content = read_field(message, "content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(
        read_field(part, "text") for part in content if read_field(part, "type") == "text"
    )


def getUserTexts(harness: Harness) -> list[str]:
    return [getMessageText(message) for message in harness.session.messages if read_field(message, "role") == "user"]


def getAssistantTexts(harness: Harness) -> list[str]:
    return [
        getMessageText(message) for message in harness.session.messages if read_field(message, "role") == "assistant"
    ]


def getToolResult(harness: Harness, toolName: str) -> Any:
    """The latest result of ``toolName`` in the session transcript."""
    result = next(
        (
            message
            for message in reversed(harness.session.messages)
            if read_field(message, "role") == "toolResult" and read_field(message, "toolName") == toolName
        ),
        None,
    )
    if result is None:
        raise RuntimeError(f"No {toolName} tool result")
    return result


class _TestUiContext:
    """An extension UI context that does nothing."""

    theme = theme

    async def select(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def confirm(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    async def input(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def notify(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def onTerminalInput(self, *_args: Any, **_kwargs: Any) -> Any:
        return lambda: None

    def setStatus(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setWorkingMessage(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setWorkingVisible(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setWorkingIndicator(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setHiddenThinkingLabel(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setWidget(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setFooter(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setHeader(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setTitle(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def custom(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def pasteToEditor(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setEditorText(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getEditorText(self) -> str:
        return ""

    async def editor(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def addAutocompleteProvider(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setEditorComponent(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getEditorComponent(self) -> Any:
        return None

    def getAllThemes(self) -> list[Any]:
        return []

    def getTheme(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def setTheme(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": False, "error": "Theme switching not available in tests"}

    def getToolsExpanded(self) -> bool:
        return False

    def setToolsExpanded(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def createTestUiContext(**overrides: Any) -> Any:
    """An extension UI context that does nothing, with ``overrides`` applied."""
    context = _TestUiContext()
    for name, value in overrides.items():
        setattr(context, name, value)
    return context


async def createTestExtensionsResult(inputs: list[Any], cwd: str) -> LoadExtensionsResult:
    runtime = createExtensionRuntime()
    eventBus = createEventBus()
    extensions = []
    for index, input in enumerate(inputs):
        isObject = not callable(input)
        hasName = isObject and "name" in input
        hasPath = isObject and isinstance(input.get("path"), str) and input["path"] != ""
        factory = input["factory"] if isObject else input
        extensionPath = f"<inline:{input['name']}>" if hasName else (input["path"] if hasPath else f"<inline:{index + 1}>")
        extensions.append(await loadExtensionFromFactory(factory, cwd, eventBus, runtime, extensionPath))
    return LoadExtensionsResult(extensions=extensions, errors=[], runtime=runtime)


class _TestResourceLoader:
    def __init__(self, extensionsResult: LoadExtensionsResult) -> None:
        self._extensionsResult = extensionsResult

    def getExtensions(self) -> LoadExtensionsResult:
        return self._extensionsResult

    def getExtensionSkillPaths(self) -> list[str]:
        return []

    def getExtensionSkillResources(self) -> list[Any]:
        return []

    def getSkills(self) -> dict[str, Any]:
        return {"skills": [], "diagnostics": []}

    def getPrompts(self) -> dict[str, Any]:
        return {"prompts": [], "diagnostics": []}

    def getThemes(self) -> dict[str, Any]:
        return {"themes": [], "diagnostics": []}

    def getAgentsFiles(self) -> dict[str, Any]:
        return {"agentsFiles": []}

    def getSystemPrompt(self) -> str | None:
        return None

    def getSystemPromptSource(self) -> Any:
        return None

    def getAppendSystemPrompt(self) -> list[str]:
        return []

    def getAppendSystemPromptSources(self) -> list[Any]:
        return []

    def extendResources(self, *_args: Any) -> None:
        return None

    async def reload(self, *_args: Any) -> None:
        return None


def createTestResourceLoader(extensionsResult: LoadExtensionsResult | None = None) -> Any:
    return _TestResourceLoader(
        extensionsResult
        if extensionsResult is not None
        else LoadExtensionsResult(extensions=[], errors=[], runtime=createExtensionRuntime())
    )


@dataclass
class Harness:
    session: AgentSession
    sessionManager: SessionManager
    settingsManager: SettingsManager
    authStorage: AuthStorage
    faux: Any
    models: list[Any]
    tempDir: str
    events: list[Any] = field(default_factory=list)

    def getModel(self, modelId: str | None = None) -> Any:
        return self.faux.getModel(modelId)

    def setResponses(self, responses: list[Any]) -> None:
        self.faux.setResponses(responses)

    def appendResponses(self, responses: list[Any]) -> None:
        self.faux.appendResponses(responses)

    def getPendingResponseCount(self) -> int:
        return self.faux.getPendingResponseCount()

    def eventsOfType(self, type: str) -> list[Any]:
        return [event for event in self.events if read_field(event, "type") == type]


@asynccontextmanager
async def createHarness(
    *,
    models: list[Any] | None = None,
    settings: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
    initialActiveToolNames: list[str] | None = None,
    allowedToolNames: list[str] | None = None,
    excludedToolNames: list[str] | None = None,
    resourceLoader: Any = None,
    extensionFactories: list[Any] | None = None,
    withConfiguredAuth: bool = True,
    modelsJson: dict[str, Any] | None = None,
    sessionManager: SessionManager | None = None,
) -> AsyncIterator[Harness]:
    tempDir = tempfile.mkdtemp(prefix="pi-suite-")
    fauxProvider = registerFauxProvider({"models": models} if models is not None else {})
    session: AgentSession | None = None
    try:
        fauxProvider.setResponses([])
        model = fauxProvider.getModel()
        toolMap = {tool.name: tool for tool in tools} if tools is not None else None
        extensionRunnerRef: dict[str, Any] = {}

        resolvedSessionManager = sessionManager if sessionManager is not None else SessionManager.inMemory()
        settingsManager = SettingsManager.inMemory(settings)

        authStorage = AuthStorage.inMemory()
        if withConfiguredAuth:

            async def apiKey(_current: Any) -> ApiKeyCredential:
                return ApiKeyCredential(type="api_key", key="faux-key")

            await authStorage.modify(model.provider, apiKey)
        modelsPath = None if modelsJson is None else str(Path(tempDir) / "models.json")
        if modelsPath:
            Path(modelsPath).write_text(jsonStringify(modelsJson), encoding="utf-8")
        modelRegistry = (
            await createModelRegistry(authStorage, modelsPath) if modelsPath else await createInMemoryModelRegistry(authStorage)
        )
        if withConfiguredAuth:
            modelRegistry.registerProvider(model.provider, {
                "baseUrl": model.baseUrl,
                "apiKey": "faux-key",
                "api": fauxProvider.api,
                "models": [
                    {
                        "id": registeredModel.id,
                        "name": registeredModel.name,
                        "api": registeredModel.api,
                        "reasoning": registeredModel.reasoning,
                        "input": registeredModel.input,
                        "inputLimits": registeredModel.inputLimits,
                        "cost": registeredModel.cost,
                        "contextWindow": registeredModel.contextWindow,
                        "maxTokens": registeredModel.maxTokens,
                        "baseUrl": registeredModel.baseUrl,
                    }
                    for registeredModel in fauxProvider.models
                ],
            })

        async def onPayload(payload: Any, _model: Any = None) -> Any:
            runner = extensionRunnerRef.get("current")
            if runner is None or not runner.hasHandlers("before_provider_request"):
                return payload
            return await runner.emitBeforeProviderRequest(payload)

        async def onResponse(response: Any, _model: Any = None) -> None:
            runner = extensionRunnerRef.get("current")
            if runner is None or not runner.hasHandlers("after_provider_response"):
                return
            await runner.emit({
                "type": "after_provider_response",
                "status": read_field(response, "status"),
                "headers": read_field(response, "headers"),
            })

        async def transformContext(messages: list[Any], _signal: Any = None) -> list[Any]:
            runner = extensionRunnerRef.get("current")
            if runner is None:
                return messages
            return await runner.emitContext(messages)

        agent = Agent(
            getApiKey=lambda _provider: "faux-key" if withConfiguredAuth else None,
            streamFn=streamSimple,
            initialState={"model": model, "systemPrompt": "", "tools": []},
            convertToLlm=convertToLlm,
            onPayload=onPayload,
            onResponse=onResponse,
            transformContext=transformContext,
        )
        extensionsResult = (
            await createTestExtensionsResult(extensionFactories, tempDir) if extensionFactories is not None else None
        )
        loader = resourceLoader if resourceLoader is not None else createTestResourceLoader(extensionsResult)

        session = AgentSession(AgentSessionConfig(
            agent=agent,
            sessionManager=resolvedSessionManager,
            settingsManager=settingsManager,
            cwd=tempDir,
            modelRuntime=getModelRuntime(modelRegistry),
            resourceLoader=loader,
            baseToolsOverride=toolMap,
            initialActiveToolNames=initialActiveToolNames,
            usesDefaultTools=initialActiveToolNames is None,
            allowedToolNames=allowedToolNames,
            excludedToolNames=excludedToolNames,
            extensionRunnerRef=extensionRunnerRef,
        ))

        harness = Harness(
            session=session,
            sessionManager=resolvedSessionManager,
            settingsManager=settingsManager,
            authStorage=authStorage,
            faux=fauxProvider,
            models=fauxProvider.models,
            tempDir=tempDir,
        )
        session.subscribe(harness.events.append)
        yield harness
    finally:
        if session is not None:
            session.dispose()
        fauxProvider.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)


__all__ = [
    "Harness",
    "createHarness",
    "createTestExtensionsResult",
    "createTestResourceLoader",
    "createTestUiContext",
    "getAssistantTexts",
    "getMessageText",
    "getToolResult",
    "getUserTexts",
]
