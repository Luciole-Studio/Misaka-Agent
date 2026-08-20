"""Public coding-agent SDK entrypoints."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from misaka.agent.agent import Agent
from misaka.agent.types import AgentMessage, ThinkingLevel
from misaka.ai.models import clamp_thinking_level
from misaka.ai.stream import stream_simple
from misaka.ai.types import Model, SimpleStreamOptions, TextContent, validate_message

from misaka.config import get_agent_dir
from misaka.core.agent_session import AgentSession
from misaka.core.auth_guidance import format_no_models_available_message
from misaka.core.auth_storage import AuthStorage
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
from misaka.core.messages import convertToLlm
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_resolver import findInitialModel
from misaka.core.prompt_templates import PromptTemplate
from misaka.core.resource_loader import DefaultResourceLoader, ResourceLoader
from misaka.core.session_manager import SessionManager, get_default_session_dir
from misaka.core.settings_manager import SettingsManager
from misaka.core.skills import Skill
from misaka.core.telemetry import is_install_telemetry_enabled
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
    create_read_only_tools,
    create_read_tool,
    create_write_tool,
    with_file_mutation_queue,
)
from misaka.utils.paths import resolve_path

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
    customTools: list[ToolDefinition[Any, Any]]
    resourceLoader: ResourceLoader
    sessionManager: SessionManager
    settingsManager: SettingsManager
    sessionStartEvent: SessionStartEvent


class _CreateAgentSessionResultRequired(TypedDict):
    session: AgentSession
    extensionsResult: LoadExtensionsResult


class CreateAgentSessionResult(_CreateAgentSessionResultRequired, total=False):
    modelFallbackMessage: str


def _get_default_agent_dir() -> str:
    return get_agent_dir()


def _get_attribution_headers(
    model: Model[Any],
    settings_manager: SettingsManager,
) -> dict[str, str] | None:
    if not is_install_telemetry_enabled(settings_manager):
        return None

    if model.provider == "openrouter" or "openrouter.ai" in model.baseUrl:
        return {
            "HTTP-Referer": "https://harn.dev",
            "X-OpenRouter-Title": "harn",
            "X-OpenRouter-Categories": "cli-agent",
        }

    if (
        model.provider in {"cloudflare-workers-ai", "cloudflare-ai-gateway"}
        or "api.cloudflare.com" in model.baseUrl
        or "gateway.ai.cloudflare.com" in model.baseUrl
    ):
        return {
            "User-Agent": "harn",
        }

    return None


async def create_agent_session(options: CreateAgentSessionOptions | None = None) -> CreateAgentSessionResult:
    resolved_options = dict(options or {})
    explicit_session_manager = resolved_options.get("sessionManager")
    cwd_option = resolved_options["cwd"] if "cwd" in resolved_options else None
    cwd = resolve_path(cwd_option if cwd_option is not None else (
        explicit_session_manager.getCwd() if explicit_session_manager else os.getcwd()
    ))
    agent_dir = (
        resolve_path(resolved_options["agentDir"])
        if resolved_options.get("agentDir")
        else _get_default_agent_dir()
    )
    resource_loader = resolved_options.get("resourceLoader")

    auth_path = os.path.join(agent_dir, "auth.json") if resolved_options.get("agentDir") else None
    models_path = os.path.join(agent_dir, "models.json") if resolved_options.get("agentDir") else None
    auth_storage = resolved_options.get("authStorage") or AuthStorage.create(auth_path)
    model_registry = resolved_options.get("modelRegistry") or ModelRegistry.create(auth_storage, models_path)
    settings_manager = resolved_options.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    session_manager = explicit_session_manager or SessionManager.create(cwd, get_default_session_dir(cwd, agent_dir))

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

    if model is None:
        result = await findInitialModel(
            {
                "scopedModels": [],
                "isContinuing": has_existing_session,
                "defaultProvider": settings_manager.getDefaultProvider(),
                "defaultModelId": settings_manager.getDefaultModel(),
                "defaultThinkingLevel": settings_manager.getDefaultThinkingLevel(),
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
    if thinking_level is None:
        thinking_level = settings_manager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
    thinking_level = "off" if model is None else clamp_thinking_level(model, thinking_level)

    default_active_tool_names: list[ToolName] = ["read", "bash", "edit", "write"]
    # defaultTools 设置＝未显式给 --tools/-nt 时的启动工具白名单（pi 4d9aa837c）
    configured_default_tools = settings_manager.getDefaultTools()
    allowed_tool_names = resolved_options.get("tools")
    if allowed_tool_names is None:
        if resolved_options.get("noTools") == "all":
            allowed_tool_names = []
        elif resolved_options.get("noTools") is None:
            allowed_tool_names = configured_default_tools
    initial_active_tool_names = (
        list(resolved_options["tools"])
        if resolved_options.get("tools") is not None
        else ([] if resolved_options.get("noTools")
              else (list(configured_default_tools) if configured_default_tools is not None
                    else default_active_tool_names))
    )

    extension_runner_ref: dict[str, Any] = {}

    def convert_to_llm_with_block_images(messages: list[AgentMessage]) -> list[Any]:
        converted = convertToLlm(messages)
        if not settings_manager.getBlockImages():
            return converted

        filtered_messages: list[Any] = []
        for message in converted:
            role = _message_field(message, "role")
            content = _message_field(message, "content")
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
        auth = await model_registry.getApiKeyAndHeaders(model_value)
        if not auth.get("ok"):
            raise Exception(auth["error"])

        provider_retry_settings = settings_manager.getProviderRetrySettings()
        attribution_headers = _get_attribution_headers(model_value, settings_manager)
        resolved_stream_options = _to_dict(stream_options)
        headers = _merge_headers(attribution_headers, auth.get("headers"), resolved_stream_options.get("headers"))
        final_options = dict(resolved_stream_options)
        final_options["apiKey"] = auth.get("apiKey")
        if final_options.get("timeoutMs") is None:
            final_options["timeoutMs"] = provider_retry_settings.get("timeoutMs")
        if final_options.get("maxRetries") is None:
            final_options["maxRetries"] = provider_retry_settings.get("maxRetries")
        if final_options.get("maxRetryDelayMs") is None:
            final_options["maxRetryDelayMs"] = provider_retry_settings.get("maxRetryDelayMs")
        if headers is not None:
            final_options["headers"] = headers
        # Filter to only SimpleStreamOptions-known fields before validation.
        # In the TypeScript original, `{ ...options, ... }` creates a plain object
        # with extra AgentLoopConfig keys that are silently ignored by JS runtime.
        # Python's Pydantic extra="forbid" rejects unknown keys, so we must strip them.
        allowed_keys = SimpleStreamOptions.model_fields.keys()
        filtered_options = {k: v for k, v in final_options.items() if k in allowed_keys}
        return stream_simple(model_value, context, SimpleStreamOptions.model_validate(filtered_options))

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
                "status": _message_field(response, "status"),
                "headers": _message_field(response, "headers"),
            }
        )

    async def transform_context(messages: list[AgentMessage], _signal: Any | None = None) -> list[AgentMessage]:
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

    if has_existing_session:
        agent.state.messages = existing_session.messages
        if not has_thinking_entry:
            session_manager.appendThinkingLevelChange(thinking_level)
    else:
        if model is not None:
            session_manager.appendModelChange(model.provider, model.id)
        session_manager.appendThinkingLevelChange(thinking_level)

    session = AgentSession(
        {
            "agent": agent,
            "sessionManager": session_manager,
            "settingsManager": settings_manager,
            "cwd": cwd,
            "scopedModels": resolved_options.get("scopedModels") or [],
            "resourceLoader": resource_loader,
            "customTools": resolved_options.get("customTools") or [],
            "modelRegistry": model_registry,
            "initialActiveToolNames": initial_active_tool_names,
            "allowedToolNames": allowed_tool_names,
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


def _message_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _message_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _message_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_message_dump(item) for item in value]
    return value


def _content_type(block: Any) -> str | None:
    value = _message_field(block, "type")
    return value if isinstance(value, str) else None


def _content_text(block: Any) -> str | None:
    value = _message_field(block, "text")
    return value if isinstance(value, str) else None


createAgentSession = create_agent_session
createBashTool = create_bash_tool
createCodingTools = create_coding_tools
createEditTool = create_edit_tool
createFindTool = create_find_tool
createGrepTool = create_grep_tool
createLsTool = create_ls_tool
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
    "CreateAgentSessionOptions",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionRuntimeFactory",
    "CreateAgentSessionRuntimeResult",
    "CreateAgentSessionResult",
    "CreateAgentSessionServicesOptions",
    "ExtensionAPI",
    "ExtensionCommandContext",
    "ExtensionFactory",
    "ExtensionContext",
    "PromptTemplate",
    "SessionImportFileNotFoundError",
    "Skill",
    "SlashCommandInfo",
    "SlashCommandSource",
    "Tool",
    "ToolDefinition",
    "createAgentSession",
    "createAgentSessionFromServices",
    "createAgentSessionRuntime",
    "createAgentSessionServices",
    "createBashTool",
    "createCodingTools",
    "createEditTool",
    "createFindTool",
    "createGrepTool",
    "createLsTool",
    "createReadOnlyTools",
    "createReadTool",
    "createWriteTool",
    "withFileMutationQueue",
]
