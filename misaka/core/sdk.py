"""Public coding-agent SDK entrypoints."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from misaka.agent.agent import Agent
from misaka.agent.types import AgentMessage, ThinkingLevel
from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.models import clamp_thinking_level
from misaka.ai.types import (
    Model,
    ProviderStreamOptions,
    TextContent,
    validate_message,
)
from misaka.ai.utils.headers import provider_headers_to_record
from misaka.config import get_agent_dir
from misaka.core.agent_session import AgentSession
from misaka.core.auth_guidance import format_no_models_available_message
from misaka.core.auth_storage import AuthStorage
from misaka.core.cache_warmer import CacheWarmer, CacheWarmingRequest
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
from misaka.core.http_dispatcher import applyHttpProxySettings
from misaka.core.messages import convertToLlm
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_resolver import findInitialModel
from misaka.core.prompt_templates import PromptTemplate
from misaka.core.provider_attribution import merge_provider_attribution_headers
from misaka.core.resource_loader import DefaultResourceLoader, ResourceLoader
from misaka.core.session_manager import SessionManager, get_default_session_dir
from misaka.core.settings_manager import SettingsManager
from misaka.core.timings import time
from misaka.core.tools import (
    Tool,
    ToolName,
    create_bash_tool,
    create_coding_tools,
    create_edit_tool,
    create_find_tool,
    create_grep_tool,
    create_ls_tool,
    create_powershell_tool,
    create_read_only_tools,
    create_read_tool,
    create_write_tool,
    with_file_mutation_queue,
)
from misaka.utils.paths import resolve_path
from misaka.utils.values import read_field

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
    authStorage: AuthStorage
    modelRegistry: ModelRegistry
    model: Model[Any]
    thinkingLevel: ThinkingLevel
    scopedModels: list[ScopedModel]
    noTools: Literal["all", "builtin"]
    tools: list[str]
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


def _get_default_agent_dir() -> str:
    return get_agent_dir()


async def create_agent_session(options: CreateAgentSessionOptions | None = None) -> CreateAgentSessionResult:
    resolved_options = dict(options or {})
    explicit_session_manager = resolved_options.get("sessionManager")
    cwd_option = resolved_options.get("cwd", None)
    cwd = resolve_path(cwd_option if cwd_option is not None else (
        explicit_session_manager.getCwd() if explicit_session_manager else os.getcwd()
    ))
    agent_dir = (
        resolve_path(resolved_options["agentDir"])
        if resolved_options.get("agentDir")
        else _get_default_agent_dir()
    )
    resource_loader = resolved_options.get("resourceLoader")

    # The home lays credentials out under credentials/; a custom agent directory keeps the same
    # shape (config.home.path: a directory laying out what it keeps the way the home does).
    from misaka.config import home

    custom_dir = resolved_options.get("agentDir")
    auth_path = str(home.path("auth", agent_dir)) if custom_dir else None
    models_path = str(home.path("models", agent_dir)) if custom_dir else None
    auth_storage = resolved_options.get("authStorage") or AuthStorage.create(auth_path)
    settings_manager = resolved_options.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    applyHttpProxySettings(settings_manager.getGlobalSettings().get("httpProxy"))
    model_registry = resolved_options.get("modelRegistry") or ModelRegistry.create(auth_storage, models_path)
    if resolved_options.get("modelProfile"):
        settings_manager.bindModelProfile(resolved_options["modelProfile"], model_registry)
    # The engine home only decides the session store when the caller named one; otherwise
    # sessions go to the product tree (config.sessions), where /resume and the panel look.
    session_manager = explicit_session_manager or SessionManager.create(
        cwd, get_default_session_dir(cwd, resolved_options.get("agentDir") and agent_dir))

    if resource_loader is None:
        resource_loader = DefaultResourceLoader(
            {"cwd": cwd, "agentDir": agent_dir, "settingsManager": settings_manager}
        )
        await resource_loader.reload()
        time("resourceLoader.reload")

    existing_session = session_manager.buildSessionContext()
    has_existing_session = len(existing_session.messages) > 0
    has_thinking_entry = any(entry.get("type") == "thinking_level_change" for entry in session_manager.getBranch())

    model = resolved_options.get("model")
    model_fallback_message: str | None = None
    if model is None and has_existing_session and existing_session.model:
        restored_model = model_registry.find(existing_session.model["provider"], existing_session.model["modelId"])
        if restored_model is not None and model_registry.hasConfiguredAuth(restored_model):
            model = restored_model
        if model is None:
            model_fallback_message = (
                f'Could not restore model {existing_session.model["provider"]}/{existing_session.model["modelId"]}'
            )

    if model is None and getattr(settings_manager, "getModelProfile", lambda: None)():
        from misaka.config import profiles

        pin = profiles.pinned_model(settings_manager.getModelProfile(), strict=True)
        if pin and pin != "inherit":
            provider, model_id = settings_manager.getDefaultModelPair()
            model = model_registry.find(provider, model_id)
            if model is None:
                raise RuntimeError(f"Role default model is not available: {provider}/{model_id}")
            if not model_registry.hasConfiguredAuth(model):
                model_fallback_message = (
                    f"No configured authentication for role default {provider}/{model_id}. "
                    "Use /login or misaka setup; the selected provider has not been changed."
                )

    if model is None:
        default_provider, default_model = settings_manager.getDefaultModelPair()
        result = await findInitialModel(
            {
                "scopedModels": [],
                "isContinuing": has_existing_session,
                "defaultProvider": default_provider,
                "defaultModelId": default_model,
                "defaultThinkingLevel": settings_manager.getDefaultThinkingLevel(),
                "modelThinkingLevels": settings_manager.getAllModelThinkingLevels(),
                "modelRegistry": model_registry,
            }
        )
        model = result.model
        if model is None:
            model_fallback_message = format_no_models_available_message()
        elif model_fallback_message:
            model_fallback_message += f". Using {model.provider}/{model.id}"

    thinking_level = resolved_options.get("thinkingLevel")
    if thinking_level is None and has_existing_session:
        thinking_level = (
            existing_session.thinkingLevel
            if has_thinking_entry
            else settings_manager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
        )
    if thinking_level is None and model is not None:
        thinking_level = settings_manager.getModelThinkingLevel(model.provider, model.id)
    if thinking_level is None:
        thinking_level = settings_manager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
    thinking_level = "off" if model is None else clamp_thinking_level(model, thinking_level)

    default_active_tool_names: list[ToolName] = ["read", "bash", "edit", "write", "office", "grep", "find", "ls"]
    # defaultTools selects initial built-ins; it does not populate the registry allowlist (pi 541045ae0).
    configured_default_tools = settings_manager.getDefaultTools()
    allowed_tool_names = resolved_options.get("tools")
    if allowed_tool_names is None and resolved_options.get("noTools") == "all":
        allowed_tool_names = []
    excluded_tool_names = (
        list(resolved_options["excludeTools"])
        if resolved_options.get("excludeTools") is not None
        else []
    )
    excluded_tool_name_set = set(excluded_tool_names)
    initial_active_tool_names = (
        list(resolved_options["tools"])
        if resolved_options.get("tools") is not None
        else ([] if resolved_options.get("noTools")
              else (list(configured_default_tools) if configured_default_tools is not None
                    else default_active_tool_names))
    )
    initial_active_tool_names = [
        name for name in initial_active_tool_names if name not in excluded_tool_name_set
    ]

    extension_runner_ref: dict[str, Any] = {}

    def convert_to_llm_with_block_images(messages: list[AgentMessage]) -> list[Any]:
        converted = convertToLlm(messages)
        if not settings_manager.getBlockImages():
            return converted

        filtered_messages: list[Any] = []
        for message in converted:
            role = read_field(message, "role")
            content = read_field(message, "content")
            if role not in {"user", "toolResult"} or not isinstance(content, list):
                filtered_messages.append(message)
                continue
            if not any(_content_type(item) == "image" for item in content):
                filtered_messages.append(message)
                continue

            filtered_content: list[Any] = []
            previous_disabled = False
            for item in content:
                if _content_type(item) == "image":
                    if not previous_disabled:
                        filtered_content.append(TextContent(text="Image reading is disabled."))
                    previous_disabled = True
                    continue
                previous_disabled = (
                    _content_type(item) == "text"
                    and _content_text(item) == "Image reading is disabled."
                )
                filtered_content.append(item)

            payload = _message_dump(message)
            payload["content"] = [_message_dump(item) for item in filtered_content]
            filtered_messages.append(validate_message(payload))

        return filtered_messages

    async def stream_fn(model_value: Model[Any], context: Any, stream_options: Any = None) -> Any:
        resolved_stream_options = _to_dict(stream_options)
        resolution = await model_registry.getAuth(
            model_value,
            AuthResolutionOverrides(signal=resolved_stream_options.get("signal")),
        )
        if resolution is None:
            raise RuntimeError(f"Provider is not configured: {model_value.provider}")
        request_model = (
            model_value.model_copy(update={"baseUrl": resolution.auth.baseUrl})
            if resolution.auth.baseUrl
            else model_value
        )

        provider_retry_settings = settings_manager.getProviderRetrySettings()
        http_idle_timeout_ms = settings_manager.getHttpIdleTimeoutMs()
        effective_timeout_ms = 2_147_483_647 if http_idle_timeout_ms == 0 else http_idle_timeout_ms
        # pi sdk.ts:337 merges attribution over the auth headers before the caller's own,
        # so an explicit header always wins over one of ours.
        headers = merge_provider_attribution_headers(
            model_value,
            settings_manager,
            session_manager.getSessionId(),
            _merge_headers(
                provider_headers_to_record(resolution.auth.headers),
                resolved_stream_options.get("headers"),
            ),
        )
        # pi sdk.ts:330-339 hands the merged headers to `before_provider_headers` handlers right
        # here (models.ts:657, after mergeHeaders and before the provider call). Handlers mutate
        # the mapping in place; a None value deletes that header (pi utils/headers.ts).
        header_runner = extension_runner_ref.get("current")
        if header_runner is not None and header_runner.has_handlers("before_provider_headers"):
            transformed = await header_runner.emit_before_provider_headers(dict(headers or {}))
            headers = {key: str(value) for key, value in transformed.items() if value is not None} or None
        final_options = dict(resolved_stream_options)
        final_options["apiKey"] = resolution.auth.apiKey
        request_env = {
            **(resolution.env or {}),
            **(resolved_stream_options.get("env") or {}),
        }
        if request_env:
            final_options["env"] = request_env
        if final_options.get("timeoutMs") is None:
            provider_timeout_ms = provider_retry_settings.get("timeoutMs")
            final_options["timeoutMs"] = (
                provider_timeout_ms
                if provider_timeout_ms is not None
                else effective_timeout_ms
            )
        if final_options.get("maxRetries") is None:
            final_options["maxRetries"] = provider_retry_settings.get("maxRetries")
        if final_options.get("maxRetryDelayMs") is None:
            final_options["maxRetryDelayMs"] = provider_retry_settings.get("maxRetryDelayMs")
        if headers is not None:
            final_options["headers"] = headers
        request_options = ProviderStreamOptions.model_validate(final_options)
        # Compaction and summaries use their own routing ids; only session requests
        # replace the cache entry, so warming restarts from them. Keep warming while
        # the current transcript still extends the request's prefix. Agent state may
        # shallow-copy the messages array or refresh the model object without changing
        # the provider request, so top-level object identity is not a valid cache key.
        if resolved_stream_options.get("sessionId") == session_manager.getSessionId():
            cache_warmer.start(
                CacheWarmingRequest(model=request_model, context=context, options=request_options),
                cache_context_is_current(request_model),
            )
        return model_registry.streamSimple(
            request_model,
            context,
            request_options,
        )

    def cache_context_is_current(request_model: Model[Any]) -> Callable[[], bool]:
        messages = agent.state.messages

        def is_current() -> bool:
            current_model = agent.state.model
            current_messages = agent.state.messages
            return (
                current_model is not None
                and current_model.provider == request_model.provider
                and current_model.id == request_model.id
                and len(messages) <= len(current_messages)
                and all(message is current_messages[index] for index, message in enumerate(messages))
            )

        return is_current

    async def decide_cache_warming(event: dict[str, Any]) -> Any:
        runner = extension_runner_ref.get("current")
        if runner is None:
            return event["action"]
        return await runner.emit_cache_warming_decision(event)

    cache_warmer = CacheWarmer(
        model_registry, session_manager, settings_manager.getCacheWarmingMode, decide_cache_warming
    )

    async def on_payload(payload: dict[str, Any], _model: Model[Any]) -> Any:
        runner = extension_runner_ref.get("current")
        if runner is None or not runner.has_handlers("before_provider_request"):
            return payload
        return await runner.emit_before_provider_request(payload)

    async def on_response(response: Any, _model: Model[Any]) -> None:
        runner = extension_runner_ref.get("current")
        if runner is None or not runner.has_handlers("after_provider_response"):
            return
        await runner.emit(
            {
                "type": "after_provider_response",
                "status": read_field(response, "status"),
                "headers": read_field(response, "headers"),
            }
        )

    async def transform_context(messages: list[AgentMessage], _signal: Any | None = None) -> list[AgentMessage]:
        session = extension_runner_ref.get("session")
        if session is not None:
            messages = await session.prepareContextMessages(messages, _signal)
            # MISAKA fork: transient parts/extensions see the engine's durable view.
            messages = await session.moments.context(messages)
        runner = extension_runner_ref.get("current")
        if runner is None:
            return messages
        return await runner.emit_context(messages)

    initial_model = model or _unknown_model()
    agent = Agent(
        {
            "initialState": {
                "systemPrompt": "",
                "model": initial_model,
                "thinkingLevel": thinking_level,
                "tools": [],
                "messages": existing_session.messages,
            },
            "convertToLlm": convert_to_llm_with_block_images,
            "streamFn": stream_fn,
            "onPayload": on_payload,
            "onResponse": on_response,
            "sessionId": session_manager.getSessionId(),
            "transformContext": transform_context,
            "steeringMode": settings_manager.getSteeringMode(),
            "followUpMode": settings_manager.getFollowUpMode(),
            "transport": settings_manager.getTransport(),
            "thinkingBudgets": settings_manager.getThinkingBudgets(),
            "maxRetryDelayMs": settings_manager.getProviderRetrySettings().get("maxRetryDelayMs"),
        }
    )
    if model is None:
        agent.state.model = None

    # Restore missing settings metadata for older sessions.
    if has_existing_session:
        if not has_thinking_entry:
            session_manager.appendThinkingLevelChange(thinking_level)
    else:
        if model is not None:
            session_manager.appendModelChange(model.provider, model.id)
        session_manager.appendThinkingLevelChange(thinking_level)

    from misaka.core.session_catalog import CatalogPart
    parts = list(resolved_options.get("parts") or [])
    if not any(isinstance(part, CatalogPart) for part in parts):
        parts.insert(0, CatalogPart())  # raw SDK/helper sessions have no product assembly
    session = AgentSession(
        {
            "agent": agent,
            "sessionManager": session_manager,
            "settingsManager": settings_manager,
            "cwd": cwd,
            "scopedModels": resolved_options.get("scopedModels") or [],
            "resourceLoader": resource_loader,
            "customTools": resolved_options.get("customTools") or [],
            "parts": parts,
            "modelRegistry": model_registry,
            "cacheWarmer": cache_warmer,
            "initialActiveToolNames": initial_active_tool_names,
            "allowedToolNames": allowed_tool_names,
            "excludedToolNames": excluded_tool_names,
            "extensionRunnerRef": extension_runner_ref,
            "sessionStartEvent": resolved_options.get("sessionStartEvent"),
        }
    )
    extensions_result = resource_loader.getExtensions()

    return {
        "session": session,
        "extensionsResult": extensions_result,
        "modelFallbackMessage": model_fallback_message,
    }


def _to_dict(value: Any) -> dict[str, Any]:
    """Convert an options-like value to a plain dict.

    Handles dicts, Pydantic models, SimpleNamespace subclasses (e.g.
    ``StreamOptionsNamespace``), and other objects with ``__dict__``.
    Falls back to an empty dict for ``None``.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    # SimpleNamespace and its subclasses (e.g. StreamOptionsNamespace) store
    # their attributes in __dict__ but are not iterable, so dict() on them
    # raises TypeError.  We read __dict__ directly instead.
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _merge_headers(*header_groups: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for group in header_groups:
        if group:
            merged.update(group)
    return merged or None


def _unknown_model() -> Model[Any]:
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


def _message_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _message_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_message_dump(item) for item in value]
    return value


def _content_type(block: Any) -> str | None:
    value = read_field(block, "type")
    return value if isinstance(value, str) else None


def _content_text(block: Any) -> str | None:
    value = read_field(block, "text")
    return value if isinstance(value, str) else None


createBashTool = create_bash_tool
createCodingTools = create_coding_tools
createEditTool = create_edit_tool
createFindTool = create_find_tool
createGrepTool = create_grep_tool
createLsTool = create_ls_tool
createPowerShellTool = create_powershell_tool
createReadOnlyTools = create_read_only_tools
createReadTool = create_read_tool
createWriteTool = create_write_tool
withFileMutationQueue = with_file_mutation_queue


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
