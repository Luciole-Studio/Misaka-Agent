"""Translated from pi's ``packages/coding-agent/src/core/sdk.ts`` (v1.1.0).

MISAKA forks (marked where they act): the home's credential and session layout, role-bound settings and a role's pinned model and thinking level, the context engine and core
parts in the context transform, and a session catalog for sessions without a product assembly.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from misaka.agent.agent import Agent
from misaka.agent.stream_fn import setDefaultStreamFn
from misaka.agent.types import AgentMessage, ThinkingLevel
from misaka.ai.compat import streamSimple
from misaka.ai.models import clampThinkingLevel
from misaka.ai.types import (
    Model,
    TextContent,
    validate_message,
)
from misaka.config import get_agent_dir
from misaka.core.agent_session import AgentSession
from misaka.core.auth_guidance import formatNoModelsAvailableMessage
from misaka.core.cache_warmer import CacheWarmer, CacheWarmRequest
from misaka.core.defaults import DEFAULT_THINKING_LEVEL
from misaka.core.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionFactory,
    LoadExtensionsResult,
    SessionStartEvent,
    SlashCommandInfo,
    SlashCommandSource,
    ToolDefinition,
)
from misaka.core.mcp_servers import createToolNameMatcher
from misaka.core.messages import convertToLlm
from misaka.core.model_resolver import findInitialModel
from misaka.core.model_runtime import ModelRuntime
from misaka.core.prompt_templates import PromptTemplate
from misaka.core.provider_attribution import mergeProviderAttributionHeaders
from misaka.core.resource_loader import DefaultResourceLoader, ResourceLoader
from misaka.core.session_manager import SessionManager, getDefaultSessionDir
from misaka.core.settings_manager import (
    DEFAULT_TOOL_NAMES,
    SettingsManager,
    applyToolModifiers,
    getToolListError,
    isToolModifier,
)
from misaka.core.timings import time
from misaka.core.tools import (
    Tool,
    createBashTool,
    createCodingTools,
    createEditTool,
    createFindTool,
    createGrepTool,
    createLsTool,
    createPowerShellTool,
    createReadOnlyTools,
    createReadTool,
    createWriteTool,
    withFileMutationQueue,
)
from misaka.core.virtual_models import getBranchSelection
from misaka.utils.paths import resolvePath
from misaka.utils.values import read_field

# Preserve the pre-0.81 fallback for extensions that construct Agent instances
# or invoke low-level agent loops without supplying streamFn. Agent core remains
# provider-agnostic and does not import pi-ai/compat itself.
setDefaultStreamFn(streamSimple)

if TYPE_CHECKING:
    from misaka.core.agent_session_runtime import (
        AgentSessionRuntime,
        AgentSessionRuntimeDiagnostic,
        AgentSessionServices,
        CreateAgentSessionFromServicesOptions,
        CreateAgentSessionRuntimeFactory,
        CreateAgentSessionRuntimeResult,
        CreateAgentSessionServicesOptions,
        SessionImportFileNotFoundError,
        createAgentSessionFromServices,
        createAgentSessionRuntime,
        createAgentSessionServices,
    )


_AGENT_SESSION_RUNTIME_EXPORTS = {
    "AgentSessionRuntime",
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionRuntimeFactory",
    "CreateAgentSessionRuntimeResult",
    "CreateAgentSessionServicesOptions",
    "SessionImportFileNotFoundError",
    "createAgentSessionFromServices",
    "createAgentSessionRuntime",
    "createAgentSessionServices",
}


class ScopedModel(TypedDict):
    model: Model[Any]
    thinkingLevel: NotRequired[ThinkingLevel]


class CreateAgentSessionOptions(TypedDict, total=False):
    cwd: str
    agentDir: str
    modelRuntime: ModelRuntime
    model: Model[Any]
    thinkingLevel: ThinkingLevel
    scopedModels: list[ScopedModel]
    noTools: Literal["all", "builtin"]
    # Optional allowlist of tool names or patterns, where `*` matches any characters.
    #
    # When omitted, the resolved `defaultTools` setting gives the initial selection when configured.
    # Otherwise the default built-in tools are enabled. Extension/custom tools remain enabled unless
    # `noTools` changes that default. When provided, only matching tools are enabled. MCP tools stay
    # registered for codemode and tool search unless an entry starts with `mcp__`; then only matching
    # MCP tools are kept. An empty list, like `noTools: "all"`, disables MCP tools too.
    #
    # A list of only `+name` and `-name` entries is not an allowlist: it adds tools to or removes them
    # from the default selection, like the `defaultTools` setting. These entries take exact names.
    # Mixing them with plain entries throws.
    tools: list[str]
    # Optional denylist of tool names or patterns to disable. Applies after `tools` when both are
    # provided, MCP tools included.
    excludeTools: list[str]
    customTools: list[ToolDefinition[Any, Any]]
    parts: list[Any]
    resourceLoader: ResourceLoader
    sessionManager: SessionManager
    settingsManager: SettingsManager
    modelProfile: str
    sessionStartEvent: SessionStartEvent


class _CreateAgentSessionResultRequired(TypedDict):
    session: AgentSession
    extensionsResult: LoadExtensionsResult


class CreateAgentSessionResult(_CreateAgentSessionResultRequired, total=False):
    modelFallbackMessage: str


# Helper Functions


def getDefaultAgentDir() -> str:
    """Get the default agent directory."""
    return get_agent_dir()


async def createAgentSession(options: CreateAgentSessionOptions | None = None) -> CreateAgentSessionResult:
    """Create an AgentSession with the specified options.

    ```python
    # Minimal - uses defaults
    session = (await createAgentSession())["session"]

    # With explicit model
    result = await createAgentSession({"model": getModel("anthropic", "claude-opus-4-5"), "thinkingLevel": "high"})

    # Full control
    loader = DefaultResourceLoader({"cwd": os.getcwd(), "agentDir": getAgentDir(), "settingsManager": SettingsManager.create()})
    await loader.reload()
    result = await createAgentSession({
        "model": myModel,
        "tools": ["read", "bash"],
        "resourceLoader": loader,
        "sessionManager": SessionManager.inMemory(),
    })
    ```
    """
    options = options or {}
    explicitSessionManager = options.get("sessionManager")
    cwd = resolvePath(
        options["cwd"] if options.get("cwd") is not None
        else explicitSessionManager.getCwd() if explicitSessionManager else os.getcwd()
    )
    agentDir = resolvePath(options["agentDir"]) if options.get("agentDir") else getDefaultAgentDir()
    resourceLoader = options.get("resourceLoader")

    # MISAKA fork: a directory keeps credentials and models the way the home lays them out (config.home.path).
    from misaka.config import home

    authPath = str(home.path("auth", agentDir)) if options.get("agentDir") else None
    modelsPath = str(home.path("models", agentDir if options.get("agentDir") else None))
    modelRuntime = options.get("modelRuntime") or await ModelRuntime.create({"authPath": authPath, "modelsPath": modelsPath})

    settingsManager = options.get("settingsManager") or SettingsManager.create(cwd, agentDir)
    # MISAKA fork: a session that runs as a role reads that role's settings.
    if options.get("modelProfile"):
        settingsManager.bindModelProfile(options["modelProfile"], modelRuntime)
    # MISAKA fork: the agent directory decides the session store only when the caller named one; otherwise
    # sessions go to the product tree (config.sessions), where /resume and the panel look.
    sessionManager = explicitSessionManager or SessionManager.create(
        cwd, getDefaultSessionDir(cwd, options.get("agentDir") and agentDir)
    )

    if resourceLoader is None:
        resourceLoader = DefaultResourceLoader({"cwd": cwd, "agentDir": agentDir, "settingsManager": settingsManager})
        await resourceLoader.reload()
        time("resourceLoader.reload")

    # Check if session has existing data to restore
    existingSession = sessionManager.buildSessionContext()
    hasExistingSession = len(existingSession.messages) > 0
    hasThinkingEntry = any(entry.get("type") == "thinking_level_change" for entry in sessionManager.getBranch())

    model = options.get("model")
    modelFallbackMessage: str | None = None

    # Assistant messages name the physical model that answered, so a virtual selection is only in
    # model_change entries.
    sessionModel = getBranchSelection(sessionManager.getBranch(), modelRuntime.getModel)

    # If session has data, try to restore model from it
    if model is None and hasExistingSession and sessionModel:
        restoredModel = modelRuntime.getModel(sessionModel["provider"], sessionModel["modelId"])
        if restoredModel is not None and modelRuntime.hasConfiguredAuth(restoredModel.provider):
            model = restoredModel
        if model is None:
            modelFallbackMessage = f"Could not restore model {sessionModel['provider']}/{sessionModel['modelId']}"

    # MISAKA fork: a role that pins its model starts on it, and says so when that provider has no credentials.
    if model is None and getattr(settingsManager, "getModelProfile", lambda: None)():
        from misaka.config import profiles

        pin = profiles.pinned_model(settingsManager.getModelProfile(), strict=True)
        if pin and pin != "inherit":
            provider, modelId = settingsManager.getDefaultProvider(), settingsManager.getDefaultModel()
            model = modelRuntime.getModel(provider, modelId)
            if model is None:
                from misaka.core.model_runtime import personalizedCatalogHint

                hint = personalizedCatalogHint(provider)
                raise RuntimeError(f"Role default model is not available: {provider}/{modelId}" + (f".{hint}" if hint else ""))
            if not modelRuntime.hasConfiguredAuth(model.provider):
                modelFallbackMessage = (
                    f"No configured authentication for role default {provider}/{modelId}. "
                    "Use /login or misaka setup; the selected provider has not been changed."
                )

    # If still no model, use findInitialModel (checks settings default, then provider defaults)
    if model is None:
        result = await findInitialModel(
            {
                "scopedModels": [],
                "isContinuing": hasExistingSession,
                "defaultProvider": settingsManager.getDefaultProvider(),
                "defaultModelId": settingsManager.getDefaultModel(),
                "defaultThinkingLevel": settingsManager.getDefaultThinkingLevel(),
                "modelThinkingLevels": settingsManager.getAllModelThinkingLevels(),
                "modelRuntime": modelRuntime,
            }
        )
        model = result.model
        if model is None:
            modelFallbackMessage = formatNoModelsAvailableMessage()
        elif modelFallbackMessage:
            modelFallbackMessage += f". Using {model.provider}/{model.id}"

    thinkingLevel = options.get("thinkingLevel")

    # If session has data, restore thinking level from it
    if thinkingLevel is None and hasExistingSession:
        thinkingLevel = (
            existingSession.thinkingLevel
            if hasThinkingEntry
            else settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
        )

    # MISAKA fork: the agent's own default (her role's defaultThinkingLevel) comes before the shared per-model table.
    if thinkingLevel is None:
        thinkingLevel = settingsManager.getRoleThinkingLevel()

    # Fall back to per-model override, then global default
    if thinkingLevel is None and model is not None:
        perModel = settingsManager.getModelThinkingLevel(model.provider, model.id)
        if perModel:
            thinkingLevel = perModel
    if thinkingLevel is None:
        thinkingLevel = settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL

    # Clamp to model capabilities
    thinkingLevel = "off" if model is None else clampThinkingLevel(model, thinkingLevel)

    tools = options.get("tools")
    toolListError = getToolListError(tools) if tools is not None else None
    if toolListError:
        raise ValueError(f"Invalid tools option: {toolListError}")
    configuredDefaultTools = settingsManager.getDefaultTools()
    defaultToolNames = (
        [] if options.get("noTools") else list(DEFAULT_TOOL_NAMES if configuredDefaultTools is None else configuredDefaultTools)
    )
    # A `tools` list of only `+name`/`-name` entries changes the default selection instead of
    # replacing it, like the `defaultTools` setting.
    toolModifiers = list(tools) if tools is not None and any(map(isToolModifier, tools)) else None
    selectedToolNames = applyToolModifiers(defaultToolNames, toolModifiers) if toolModifiers else tools
    if toolModifiers:
        allowedToolNames = selectedToolNames if options.get("noTools") == "all" else None
    else:
        allowedToolNames = list(tools) if tools is not None else ([] if options.get("noTools") == "all" else None)
    excludedToolNames = options.get("excludeTools")
    isExcludedTool = createToolNameMatcher(excludedToolNames) if excludedToolNames is not None else None
    initialActiveToolNames = [
        name
        for name in (selectedToolNames if selectedToolNames is not None else defaultToolNames)
        if not (isExcludedTool is not None and isExcludedTool(name))
    ]

    # Create convertToLlm wrapper that filters images if blockImages is enabled (defense-in-depth)
    def convertToLlmWithBlockImages(messages: list[AgentMessage]) -> list[Any]:
        converted = convertToLlm(messages)
        # Check setting dynamically so mid-session changes take effect
        if not settingsManager.getBlockImages():
            return converted
        # Filter out ImageContent from all messages, replacing with text placeholder
        filtered: list[Any] = []
        for msg in converted:
            content = read_field(msg, "content")
            if read_field(msg, "role") in ("user", "toolResult") and isinstance(content, list):
                hasImages = any(_contentType(c) == "image" for c in content)
                if hasImages:

                    def isDisabledText(c: Any) -> bool:
                        return _contentType(c) == "text" and _contentText(c) == "Image reading is disabled."

                    mapped = [TextContent(text="Image reading is disabled.") if _contentType(c) == "image" else c for c in content]
                    # Dedupe consecutive "Image reading is disabled." texts
                    filteredContent = [
                        c for i, c in enumerate(mapped) if not (isDisabledText(c) and i > 0 and isDisabledText(mapped[i - 1]))
                    ]
                    payload = _messageDump(msg)
                    payload["content"] = [_messageDump(c) for c in filteredContent]
                    filtered.append(validate_message(payload))
                    continue
            filtered.append(msg)
        return filtered

    extensionRunnerRef: dict[str, Any] = {}

    async def decideCacheWarming(event: dict[str, Any]) -> Any:
        runner = extensionRunnerRef.get("current")
        decision = await runner.emitCacheWarmingDecision(event) if runner is not None else None
        return decision if decision is not None else event["action"]

    cacheWarmer = CacheWarmer(modelRuntime, sessionManager, lambda: settingsManager.getCacheWarmingMode(), decideCacheWarming)

    def buildRequestOptions(requestModel: Model[Any], options: Any = None) -> dict[str, Any]:
        options = _toDict(options)
        providerRetrySettings = settingsManager.getProviderRetrySettings()
        httpIdleTimeoutMs = settingsManager.getHttpIdleTimeoutMs()
        effectiveTimeoutMs = 2147483647 if httpIdleTimeoutMs == 0 else httpIdleTimeoutMs
        headerRunner = extensionRunnerRef.get("current")

        def nullish(*values: Any) -> Any:
            return next((value for value in values if value is not None), None)

        async def transformHeaders(requestHeaders: dict[str, Any]) -> dict[str, Any]:
            headers = mergeProviderAttributionHeaders(requestModel, settingsManager, options.get("sessionId"), requestHeaders)
            if headerRunner is not None and headerRunner.hasHandlers("before_provider_headers"):
                return await headerRunner.emitBeforeProviderHeaders(dict(headers or {}))
            return headers or {}

        return {
            **options,
            "timeoutMs": nullish(options.get("timeoutMs"), providerRetrySettings.get("timeoutMs"), effectiveTimeoutMs),
            "websocketConnectTimeoutMs": nullish(
                options.get("websocketConnectTimeoutMs"), settingsManager.getWebSocketConnectTimeoutMs()
            ),
            "maxRetries": nullish(options.get("maxRetries"), providerRetrySettings.get("maxRetries")),
            "maxRetryDelayMs": nullish(options.get("maxRetryDelayMs"), providerRetrySettings.get("maxRetryDelayMs")),
            "transformHeaders": transformHeaders,
        }

    # Warm only requests for the selected model. Requests a virtual selection routed, or that an
    # extension redirected, may not be repeated by the next request, so warming them could be wasted.
    def cacheContextIsCurrent(requestModel: Model[Any]) -> Callable[[], bool]:
        messages = agent.state.messages

        def isCurrent() -> bool:
            currentModel = agent.state.model
            currentMessages = agent.state.messages
            return (
                currentModel is not None
                and currentModel.provider == requestModel.provider
                and currentModel.id == requestModel.id
                and len(messages) <= len(currentMessages)
                and all(message is currentMessages[index] for index, message in enumerate(messages))
            )

        return isCurrent

    async def transformProviderPayload(payload: Any, _model: Model[Any]) -> Any:
        runner = extensionRunnerRef.get("current")
        if runner is None or not runner.hasHandlers("before_provider_request"):
            return payload
        return await runner.emitBeforeProviderRequest(payload)

    async def handleProviderResponse(response: Any, _model: Model[Any]) -> None:
        runner = extensionRunnerRef.get("current")
        if runner is None or not runner.hasHandlers("after_provider_response"):
            return
        await runner.emit(
            {"type": "after_provider_response", "status": read_field(response, "status"), "headers": read_field(response, "headers")}
        )

    async def handleProviderStreamEvent(data: Any, model: Model[Any]) -> None:
        runner = extensionRunnerRef.get("current")
        if runner is None or not runner.hasHandlers("provider_stream_event"):
            return
        await runner.emit(
            {"data": data, "type": "provider_stream_event", "provider": model.provider, "api": model.api, "model": model.id}
        )

    async def streamFn(model: Model[Any], context: Any, options: Any = None) -> Any:
        requestOptions = buildRequestOptions(model, options)
        # Compaction and summaries use their own routing ids; only session requests
        # replace the cache entry, so warming restarts from them. Keep warming while
        # the current transcript still extends the request's prefix. Agent state may
        # shallow-copy the messages array or refresh the model object without changing
        # the provider request, so top-level object identity is not a valid cache key.
        if requestOptions.get("sessionId") == sessionManager.getSessionId():
            cacheWarmer.start(
                CacheWarmRequest(model=model, context=context, options=requestOptions), cacheContextIsCurrent(model)
            )
        return modelRuntime.streamSimple(model, context, requestOptions)

    async def transformContext(messages: list[AgentMessage], signal: Any | None = None) -> list[AgentMessage]:
        # MISAKA fork: the context engine prepares the messages, and the session's parts see its durable view.
        session = extensionRunnerRef.get("session")
        if session is not None:
            messages = await session.prepareContextMessages(messages, signal)
            messages = await session.moments.context(messages)
        runner = extensionRunnerRef.get("current")
        if runner is None:
            return messages
        return await runner.emitContext(messages)

    agent = Agent(
        {
            "initialState": {
                "systemPrompt": "",
                # MISAKA fork: Agent's state needs a model; a session without one starts on a placeholder that is
                # cleared right after.
                "model": model or _unknownModel(),
                "thinkingLevel": thinkingLevel,
                "tools": [],
                "messages": existingSession.messages,
            },
            "convertToLlm": convertToLlmWithBlockImages,
            "streamFn": streamFn,
            "onPayload": transformProviderPayload,
            "onResponse": handleProviderResponse,
            "onProviderStreamEvent": handleProviderStreamEvent,
            "sessionId": sessionManager.getSessionId(),
            "transformContext": transformContext,
            "steeringMode": settingsManager.getSteeringMode(),
            "followUpMode": settingsManager.getFollowUpMode(),
            "transport": settingsManager.getTransport(),
            "thinkingBudgets": settingsManager.getThinkingBudgets(),
            "maxRetryDelayMs": settingsManager.getProviderRetrySettings().get("maxRetryDelayMs"),
        }
    )
    if model is None:
        agent.state.model = None

    # Restore missing settings metadata for older sessions.
    if hasExistingSession:
        if not hasThinkingEntry:
            sessionManager.appendThinkingLevelChange(thinkingLevel)
    else:
        # Save initial model and thinking level for new sessions so they can be restored on resume
        if model is not None:
            sessionManager.appendModelChange(model.provider, model.id)
        sessionManager.appendThinkingLevelChange(thinkingLevel)

    # MISAKA fork: core parts; a raw SDK or helper session has no product assembly, so it gets the session catalog.
    from misaka.core.session_catalog import CatalogPart

    parts = list(options.get("parts") or [])
    if not any(isinstance(part, CatalogPart) for part in parts):
        parts.insert(0, CatalogPart())
    session = AgentSession(
        {
            "agent": agent,
            "sessionManager": sessionManager,
            "settingsManager": settingsManager,
            "cwd": cwd,
            "scopedModels": options.get("scopedModels") or [],
            "resourceLoader": resourceLoader,
            "customTools": options.get("customTools") or [],
            "parts": parts,
            "modelRuntime": modelRuntime,
            "cacheWarmer": cacheWarmer,
            "initialActiveToolNames": initialActiveToolNames,
            "usesDefaultTools": (tools is None or toolModifiers is not None) and not options.get("noTools"),
            "defaultToolModifiers": toolModifiers,
            "allowedToolNames": allowedToolNames,
            "excludedToolNames": excludedToolNames,
            "extensionRunnerRef": extensionRunnerRef,
            "sessionStartEvent": options.get("sessionStartEvent"),
        }
    )

    extensionsResult = resourceLoader.getExtensions()

    return {
        "session": session,
        "extensionsResult": extensionsResult,
        "modelFallbackMessage": modelFallbackMessage,
    }


# Helper Functions


def _toDict(value: Any) -> dict[str, Any]:
    """An options value as a plain dict: a dict, a Pydantic model, or a namespace (``StreamOptionsNamespace``)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _unknownModel() -> Model[Any]:
    return Model(
        id="unknown",
        name="unknown",
        api="unknown",
        provider="unknown",
        baseUrl="",
        reasoning=False,
        input=[],
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        contextWindow=0,
        maxTokens=0,
    )


def _messageDump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _messageDump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_messageDump(item) for item in value]
    return value


def _contentType(block: Any) -> str | None:
    value = read_field(block, "type")
    return value if isinstance(value, str) else None


def _contentText(block: Any) -> str | None:
    value = read_field(block, "text")
    return value if isinstance(value, str) else None


def __getattr__(name: str) -> Any:
    if name in _AGENT_SESSION_RUNTIME_EXPORTS:
        from misaka.core import agent_session_runtime as runtime_module

        return getattr(runtime_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AgentSessionRuntime",
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionOptions",
    "CreateAgentSessionResult",
    "CreateAgentSessionRuntimeFactory",
    "CreateAgentSessionRuntimeResult",
    "CreateAgentSessionServicesOptions",
    "ExtensionAPI",
    "ExtensionCommandContext",
    "ExtensionContext",
    "ExtensionFactory",
    "PromptTemplate",
    "SessionImportFileNotFoundError",
    "SlashCommandInfo",
    "SlashCommandSource",
    "Tool",
    "ToolDefinition",
    "createAgentSessionFromServices",
    "createAgentSessionRuntime",
    "createAgentSessionServices",
    "createBashTool",
    "createCodingTools",
    "createEditTool",
    "createFindTool",
    "createGrepTool",
    "createLsTool",
    "createPowerShellTool",
    "createReadOnlyTools",
    "createReadTool",
    "createWriteTool",
    "withFileMutationQueue",
]
