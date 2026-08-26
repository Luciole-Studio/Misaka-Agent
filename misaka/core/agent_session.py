"""Foundational coding-agent session abstraction."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from misaka.agent.agent import AbortController, Agent
from misaka.agent.types import AgentMessage, AgentState, AgentTool, ThinkingLevel
from misaka.ai.models import (
    clamp_thinking_level,
    get_supported_thinking_levels,
    models_are_equal,
)
from misaka.ai.providers.register_builtins import reset_api_providers
from misaka.ai.session_resources import cleanup_session_resources
from misaka.ai.stream import stream_simple
from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    validate_message,
)
from misaka.ai.utils.overflow import is_context_overflow, is_recoverable_length
from misaka.core.auth_guidance import (
    format_no_api_key_found_message,
    format_no_model_selected_message,
)
from misaka.core.bash_executor import BashResult, execute_bash_with_operations
from misaka.core.compaction import (
    CompactionResult as SessionCompactionResult,
)
from misaka.core.compaction import (
    CompactionSettings,
)
from misaka.core.compaction import (
    calculateContextTokens as calculate_compaction_context_tokens,
)
from misaka.core.compaction import compact as run_compaction
from misaka.core.compaction import (
    estimateContextTokens as estimate_compaction_context_tokens,
)
from misaka.core.compaction import (
    prepareCompaction as prepare_compaction,
)
from misaka.core.compaction import (
    shouldCompact as should_compact,
)
from misaka.core.compaction.branch_summarization import (
    GenerateBranchSummaryOptions,
    collect_entries_for_branch_summary,
    generate_branch_summary,
)
from misaka.core.defaults import DEFAULT_THINKING_LEVEL
from misaka.core.export_html import export_session_to_html
from misaka.core.export_html.tool_renderer import create_tool_html_renderer
from misaka.core.extensions.runner import ExtensionRunner, emit_session_shutdown_event
from misaka.core.extensions.types import (
    ExtensionCommandContextActions,
    ExtensionError,
    ExtensionErrorListener,
    ExtensionUIContext,
    ToolDefinition,
    ToolInfo,
)
from misaka.core.messages import BashExecutionMessage
from misaka.core.model_registry import ModelRegistry
from misaka.core.prompt_templates import PromptTemplate, expand_prompt_template
from misaka.core.resource_loader import ResourceLoaderLike
from misaka.core.session_manager import (
    CURRENT_SESSION_VERSION,
    SessionManager,
    _dump_json,
    get_latest_compaction_entry,
)
from misaka.core.settings_manager import SettingsManager
from misaka.core.slash_commands import SlashCommandInfo, _make_slash_command_info
from misaka.core.source_info import SourceInfo, create_synthetic_source_info
from misaka.core.system_prompt import BuildSystemPromptOptions, build_system_prompt
from misaka.core.tools import create_all_tool_definitions
from misaka.core.tools.bash import create_local_bash_operations
from misaka.core.tools.tool_definition_wrapper import (
    create_tool_definition_from_agent_tool,
    wrap_tool_definition,
)
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.paths import resolve_path
from misaka.utils.values import read_field

_SKILL_BLOCK_PATTERN = re.compile(
    r'^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n</skill>(?:\n\n([\s\S]+))?$'
)
_STALE_CONTEXT_MESSAGE = (
    "This extension ctx is stale after session replacement or reload. Do not use a captured extension or command ctx "
    "after ctx.newSession(), ctx.fork(), ctx.switchSession(), or ctx.reload(). For newSession, fork, and "
    "switchSession, move post-replacement work into withSession and use the ctx passed to withSession. For "
    "reload, do not use the old ctx after await ctx.reload()."
)
_THINKING_LEVELS: tuple[ThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")
_RETRYABLE_ERROR_PATTERN = re.compile(
    r"overloaded|provider.?returned.?error|exceeded request buffer limit while retrying upstream|"  # pi fe10558
    r"rate.?limit|too many requests|429|500|502|503|504|"
    r"service.?unavailable|server.?error|internal.?error|network.?error|connection.?error|"
    r"connection.?refused|connection.?lost|websocket.?closed|websocket.?error|other side closed|"
    r"fetch failed|upstream.?connect|reset before headers|socket hang up|ended without|"
    r"stream ended before message_stop|http2 request did not get a response|timed? out|timeout|"
    r"terminated|retry delay",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedSkillBlock:
    name: str
    location: str
    content: str
    userMessage: str | None = None


AgentSessionEvent = Any
AgentSessionEventListener = Callable[[AgentSessionEvent], None]


@dataclass(slots=True)
class AgentSessionConfig:
    agent: Agent
    sessionManager: SessionManager
    settingsManager: SettingsManager
    cwd: str
    resourceLoader: ResourceLoaderLike
    modelRegistry: ModelRegistry
    scopedModels: list[dict[str, Any]] = field(default_factory=list)
    customTools: list[Any] = field(default_factory=list)
    initialActiveToolNames: list[str] | None = None
    allowedToolNames: list[str] | None = None
    baseToolsOverride: dict[str, AgentTool] | None = None
    extensionRunnerRef: dict[str, Any] | None = None
    sessionStartEvent: dict[str, Any] | None = None


@dataclass(slots=True)
class ExtensionBindings:
    uiContext: ExtensionUIContext | None = None
    commandContextActions: ExtensionCommandContextActions | None = None
    abortHandler: Callable[[], None] | None = None
    shutdownHandler: Callable[[], None] | None = None
    onError: ExtensionErrorListener | None = None


@dataclass(slots=True)
class SessionTokenStats:
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    total: int


@dataclass(slots=True)
class SessionStats:
    sessionFile: str | None
    sessionId: str
    userMessages: int
    assistantMessages: int
    toolCalls: int
    toolResults: int
    totalMessages: int
    tokens: SessionTokenStats
    cost: float
    contextUsage: dict[str, float | int | None] | None = None


@dataclass(slots=True)
class PromptOptions:
    expandPromptTemplates: bool = True
    images: list[ImageContent] | None = None
    streamingBehavior: str | None = None
    source: str = "interactive"
    preflightResult: Callable[[bool], None] | None = None


@dataclass(slots=True)
class ModelCycleResult:
    model: Model[Any]
    thinkingLevel: ThinkingLevel
    isScoped: bool


@dataclass(slots=True)
class _ToolDefinitionEntry:
    definition: Any
    sourceInfo: SourceInfo
    promptSnippet: str | None = None
    promptGuidelines: list[str] = field(default_factory=list)


def parse_skill_block(text: str) -> ParsedSkillBlock | None:
    match = _SKILL_BLOCK_PATTERN.match(text)
    if not match:
        return None
    return ParsedSkillBlock(
        name=match.group(1),
        location=match.group(2),
        content=match.group(3),
        userMessage=match.group(4).strip() if match.group(4) else None,
    )


class AgentSession:
    def __init__(self, config: AgentSessionConfig | dict[str, Any]) -> None:
        resolved = config if isinstance(config, AgentSessionConfig) else AgentSessionConfig(**config)
        self.agent = resolved.agent
        self.sessionManager = resolved.sessionManager
        self.settingsManager = resolved.settingsManager
        self._cwd = resolved.cwd
        self._resourceLoader = resolved.resourceLoader
        self._modelRegistry = resolved.modelRegistry
        self._scopedModels = list(resolved.scopedModels)
        self._customTools = list(resolved.customTools)
        self._initialActiveToolNames = (
            list(resolved.initialActiveToolNames) if resolved.initialActiveToolNames is not None else None
        )
        self._allowedToolNames = None if resolved.allowedToolNames is None else set(resolved.allowedToolNames)
        self._disallowedToolNames: set[str] = set()
        self._alwaysAllowedToolNames: set[str] = set()
        self._baseToolsOverride = dict(resolved.baseToolsOverride or {})
        self._extensionRunnerRef = resolved.extensionRunnerRef
        self._sessionStartEvent = resolved.sessionStartEvent or {"type": "session_start", "reason": "startup"}

        self._eventListeners: list[AgentSessionEventListener] = []
        self._unsubscribeAgent = self.agent.subscribe(self._handle_agent_event)
        self._extensionErrorUnsubscriber: Callable[[], None] | None = None
        self._extensionAbortHandler: Callable[[], None] | None = None
        self._extensionShutdownHandler: Callable[[], None] | None = None
        self._extensionBindings: ExtensionBindings | None = None
        self._extensionUIContext: ExtensionUIContext | None = None
        self._extensionCommandContextActions: ExtensionCommandContextActions | None = None
        self._extensionErrorListener: ExtensionErrorListener | None = None
        self._steeringMessages: list[str] = []
        self._followUpMessages: list[str] = []
        self._pendingNextTurnMessages: list[dict[str, Any]] = []
        self._pendingBashMessages: list[BashExecutionMessage] = []
        self._bashAbortController: AbortController | None = None
        self._auto_compaction_abort_controller: AbortController | None = None
        self._compactionAbortController: AbortController | None = None
        self._branchSummaryAbortController: AbortController | None = None
        self._overflow_recovery_attempted = False
        self._retryAbortController: AbortController | None = None
        self._retryAttempt = 0
        self._turnIndex = 0
        self._lastAssistantMessage: AssistantMessage | None = None
        self._stopHookContinuationPending = False
        self._baseToolDefinitions: dict[str, Any] = {}
        self._toolRegistry: dict[str, AgentTool] = {}
        self._toolDefinitions: dict[str, _ToolDefinitionEntry] = {}
        self._baseSystemPrompt = ""
        self._baseSystemPromptOptions: BuildSystemPromptOptions = {"cwd": self._cwd}

        self._install_agent_tool_hooks()
        self._build_runtime(
            {
                "activeToolNames": self._initialActiveToolNames,
                "includeAllExtensionTools": True,
            }
        )

    @property
    def extensionRunner(self) -> ExtensionRunner:
        return self._extensionRunner

    @property
    def modelRegistry(self) -> ModelRegistry:
        return self._modelRegistry

    @property
    def resourceLoader(self) -> ResourceLoaderLike:
        return self._resourceLoader

    @property
    def state(self) -> AgentState:
        return self.agent.state

    @property
    def model(self) -> Model[Any] | None:
        return self.agent.state.model

    @property
    def thinkingLevel(self) -> ThinkingLevel:
        return self.agent.state.thinkingLevel

    @property
    def isStreaming(self) -> bool:
        return self.agent.state.isStreaming

    @property
    def systemPrompt(self) -> str:
        return self.agent.state.systemPrompt

    @property
    def isCompacting(self) -> bool:
        return any(
            controller is not None
            for controller in (
                self._auto_compaction_abort_controller,
                self._compactionAbortController,
                self._branchSummaryAbortController,
            )
        )

    @property
    def messages(self) -> list[AgentMessage]:
        return self.agent.state.messages

    @property
    def steeringMode(self) -> str:
        return self.agent.steeringMode

    @property
    def followUpMode(self) -> str:
        return self.agent.followUpMode

    @property
    def sessionFile(self) -> str | None:
        return self.sessionManager.getSessionFile()

    @property
    def sessionId(self) -> str:
        return self.sessionManager.getSessionId()

    @property
    def sessionName(self) -> str | None:
        return self.sessionManager.getSessionName()

    @property
    def scopedModels(self) -> list[dict[str, Any]]:
        return list(self._scopedModels)

    @property
    def promptTemplates(self) -> list[PromptTemplate]:
        return list(self._resourceLoader.getPrompts()["prompts"])

    @property
    def autoCompactionEnabled(self) -> bool:
        return self.settingsManager.getCompactionEnabled()

    @property
    def autoRetryEnabled(self) -> bool:
        return self.settingsManager.getRetryEnabled()

    @property
    def isRetrying(self) -> bool:
        return self._retryAbortController is not None

    @property
    def retryAttempt(self) -> int:
        return self._retryAttempt

    @property
    def pendingMessageCount(self) -> int:
        return len(self._steeringMessages) + len(self._followUpMessages)

    @property
    def isBashRunning(self) -> bool:
        return self._bashAbortController is not None

    @property
    def hasPendingBashMessages(self) -> bool:
        return len(self._pendingBashMessages) > 0

    def setScopedModels(self, scopedModels: list[dict[str, Any]]) -> None:
        self._scopedModels = list(scopedModels)

    def subscribe(self, listener: AgentSessionEventListener) -> Callable[[], None]:
        self._eventListeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._eventListeners:
                self._eventListeners.remove(listener)

        return unsubscribe

    def dispose(self) -> None:
        self._extensionRunner.invalidate(_STALE_CONTEXT_MESSAGE)
        self._disconnect_from_agent()
        self._eventListeners = []
        cleanup_session_resources(self.sessionId)

    def _disconnect_from_agent(self) -> None:
        if self._unsubscribeAgent is not None:
            self._unsubscribeAgent()
            self._unsubscribeAgent = None

    async def abort(self) -> None:
        self.abortRetry()
        self.agent.abort()
        await self.agent.waitForIdle()

    async def _get_required_request_auth(self, model: Model[Any]) -> dict[str, Any]:
        result = await self._modelRegistry.getApiKeyAndHeaders(model)
        if not result.get("ok"):
            error = str(result.get("error") or "")
            if error.startswith("No API key found"):
                raise RuntimeError(format_no_api_key_found_message(model.provider))
            raise RuntimeError(error)

        api_key = result.get("apiKey")
        if isinstance(api_key, str) and api_key:
            return {"apiKey": api_key, "headers": result.get("headers")}

        if self._modelRegistry.isUsingOAuth(model):
            raise RuntimeError(
                f'Authentication failed for "{model.provider}". '
                "Credentials may have expired or network is unavailable. "
                f"Run '/login {model.provider}' to re-authenticate."
            )
        raise RuntimeError(format_no_api_key_found_message(model.provider))

    async def _get_compaction_request_auth(self, model: Model[Any]) -> dict[str, Any]:
        if self.agent.streamFn == stream_simple:
            return await self._get_required_request_auth(model)

        result = await self._modelRegistry.getApiKeyAndHeaders(model)
        if not result.get("ok"):
            return {}
        return {"apiKey": result.get("apiKey"), "headers": result.get("headers")}

    async def prompt(
        self,
        text: str,
        options: PromptOptions | dict[str, Any] | None = None,
    ) -> None:
        resolved = options if isinstance(options, PromptOptions) else PromptOptions(**dict(options or {}))
        preflight_reported = False

        def report_preflight(success: bool) -> None:
            nonlocal preflight_reported
            if preflight_reported or resolved.preflightResult is None:
                return
            preflight_reported = True
            resolved.preflightResult(success)

        try:
            if (
                resolved.expandPromptTemplates
                and text.startswith("/")
                and await self._try_execute_extension_command(text)
            ):
                report_preflight(True)
                return

            current_text = text
            current_images = None if resolved.images is None else list(resolved.images)
            if self._extensionRunner.has_handlers("input"):
                input_result = await self._extensionRunner.emit_input(
                    current_text,
                    current_images,
                    resolved.source,
                )
                action = _event_field(input_result, "action", "continue")
                if action == "handled":
                    report_preflight(True)
                    return
                if action == "transform":
                    current_text = _event_field(input_result, "text", current_text)
                    transformed_images = _event_field(input_result, "images", None)
                    if transformed_images is not None:
                        current_images = list(transformed_images)

            if resolved.expandPromptTemplates:
                current_text = expand_prompt_template(current_text, self.promptTemplates)

            if self.isStreaming:
                if resolved.streamingBehavior == "followUp":
                    await self._queue_follow_up(current_text, current_images)
                    report_preflight(True)
                    return
                if resolved.streamingBehavior == "steer":
                    await self._queue_steer(current_text, current_images)
                    report_preflight(True)
                    return
                raise RuntimeError(
                    "Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the "
                    "message."
                )

            self._flush_pending_bash_messages()

            if self.model is None:
                raise RuntimeError(format_no_model_selected_message())
            if not self._modelRegistry.hasConfiguredAuth(self.model):
                if self._modelRegistry.isUsingOAuth(self.model):
                    raise RuntimeError(
                        f'Authentication failed for "{self.model.provider}". '
                        "Credentials may have expired or network is unavailable. "
                        f"Run '/login {self.model.provider}' to re-authenticate."
                    )
                raise RuntimeError(format_no_api_key_found_message(self.model.provider))

            last_assistant = self._find_last_assistant_message()
            if last_assistant is not None and await self._check_compaction(last_assistant, False):
                try:
                    await self.agent.continue_()
                    while await self._handle_post_agent_run():
                        await self.agent.continue_()
                finally:
                    self._flush_pending_bash_messages()

            messages: list[Any] = []
            messages.append(self._build_user_message(current_text, current_images))
            messages.extend(self._pendingNextTurnMessages)
            self._pendingNextTurnMessages = []
            if self._extensionRunner.has_handlers("before_agent_start"):
                before_result = await self._extensionRunner.emit_before_agent_start(
                    current_text,
                    current_images,
                    self._baseSystemPrompt,
                    self._baseSystemPromptOptions,
                )
                if before_result:
                    if _event_field(before_result, "block", False):
                        raise RuntimeError(
                            str(
                                _event_field(
                                    before_result,
                                    "reason",
                                    "before_agent_start hook blocked the turn",
                                )
                            )
                        )
                    extension_messages = _event_field(before_result, "messages") or []
                    for message in extension_messages:
                        normalized_message = _message_dict(message)
                        messages.append(
                            {
                                "role": "custom",
                                "customType": read_field(normalized_message, "customType"),
                                "content": _message_content(normalized_message),
                                "display": bool(read_field(normalized_message, "display")),
                                "details": read_field(normalized_message, "details"),
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                    system_prompt = _event_field(before_result, "systemPrompt")
                    if system_prompt:
                        self.agent.state.systemPrompt = system_prompt
                    else:
                        self.agent.state.systemPrompt = self._baseSystemPrompt
            else:
                self.agent.state.systemPrompt = self._baseSystemPrompt
            report_preflight(True)
            await self._run_agent_prompt(messages)
        except Exception:
            report_preflight(False)
            raise

    async def steer(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        if text.startswith("/"):
            self._throw_if_extension_command(text)
        expanded_text = expand_prompt_template(text, self.promptTemplates)
        await self._queue_steer(expanded_text, images)

    async def followUp(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        if text.startswith("/"):
            self._throw_if_extension_command(text)
        expanded_text = expand_prompt_template(text, self.promptTemplates)
        await self._queue_follow_up(expanded_text, images)

    def _emit_queue_update(self) -> None:
        self._emit(
            {
                "type": "queue_update",
                "steering": list(self._steeringMessages),
                "followUp": list(self._followUpMessages),
            }
        )

    async def _queue_steer(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        self._steeringMessages.append(text)
        self._emit_queue_update()
        self.agent.steer(self._build_user_message(text, images))

    async def _queue_follow_up(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        self._followUpMessages.append(text)
        self._emit_queue_update()
        self.agent.followUp(self._build_user_message(text, images))

    def _throw_if_extension_command(self, text: str) -> None:
        command_text = text.removeprefix("/")
        command_name = command_text.split(" ", 1)[0]
        if self._extensionRunner.get_command(command_name) is not None:
            raise RuntimeError(
                f'Extension command "/{command_name}" cannot be queued. '
                "Use prompt() or execute the command when not streaming."
            )

    def setSessionName(self, name: str) -> None:
        self.sessionManager.appendSessionInfo(name)
        self._emit({"type": "session_info_changed", "name": self.sessionManager.getSessionName()})

    def clearQueue(self) -> dict[str, list[str]]:
        steering = list(self._steeringMessages)
        follow_up = list(self._followUpMessages)
        self._steeringMessages = []
        self._followUpMessages = []
        self.agent.clearAllQueues()
        self._emit_queue_update()
        return {"steering": steering, "followUp": follow_up}

    def getSteeringMessages(self) -> list[str]:
        return list(self._steeringMessages)

    def getFollowUpMessages(self) -> list[str]:
        return list(self._followUpMessages)

    async def setModel(self, model: Model[Any]) -> None:
        if not self._modelRegistry.hasConfiguredAuth(model):
            raise RuntimeError(f"No API key for {model.provider}/{model.id}")
        current = self.model
        thinking_level = self._get_thinking_level_for_model_switch()
        self.agent.state.model = model
        self.sessionManager.appendModelChange(model.provider, model.id)
        self.settingsManager.setDefaultModelAndProvider(model.provider, model.id)
        self.setThinkingLevel(thinking_level)
        if not models_are_equal(current, model):
            await self._extensionRunner.emit(
                {
                    "type": "model_select",
                    "model": model,
                    "previousModel": current,
                    "source": "set",
                }
            )

    def setThinkingLevel(self, level: ThinkingLevel) -> None:
        model = self.model
        clamped = level if model is None else clamp_thinking_level(model, level)
        previous_level = self.agent.state.thinkingLevel
        self.agent.state.thinkingLevel = clamped
        if clamped == previous_level:
            return
        self.sessionManager.appendThinkingLevelChange(clamped)
        if self.supportsThinking() or clamped != "off":
            self.settingsManager.setDefaultThinkingLevel(clamped)
        self._emit({"type": "thinking_level_changed", "level": clamped})
        if self._extensionRunner.has_handlers("thinking_level_select"):
            async def emit_change() -> None:
                await self._extensionRunner.emit(
                    {
                        "type": "thinking_level_select",
                        "level": clamped,
                        "previousLevel": previous_level,
                    }
                )

            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(emit_change())
            except RuntimeError:
                pass

    def getAvailableThinkingLevels(self) -> list[ThinkingLevel]:
        if self.model is None:
            return list(_THINKING_LEVELS)
        return list(get_supported_thinking_levels(self.model))

    def supportsThinking(self) -> bool:
        return bool(self.model and self.model.reasoning)

    def cycleThinkingLevel(self) -> ThinkingLevel | None:
        if not self.supportsThinking():
            return None
        levels = self.getAvailableThinkingLevels()
        current_index = levels.index(self.thinkingLevel) if self.thinkingLevel in levels else 0
        next_level = levels[(current_index + 1) % len(levels)]
        self.setThinkingLevel(next_level)
        return next_level

    async def cycleModel(self, direction: str = "forward") -> ModelCycleResult | None:
        if self._scopedModels:
            return await self._cycle_scoped_model(direction)
        return await self._cycle_available_model(direction)

    async def _cycle_scoped_model(self, direction: str) -> ModelCycleResult | None:
        scoped_models = [item for item in self._scopedModels if self._modelRegistry.hasConfiguredAuth(item["model"])]
        if len(scoped_models) <= 1:
            return None
        current_model = self.model
        current_index = next(
            (index for index, item in enumerate(scoped_models) if models_are_equal(item["model"], current_model)),
            -1,
        )
        current_index = max(current_index, 0)
        next_index = (current_index + (1 if direction != "backward" else -1)) % len(scoped_models)
        next_model = scoped_models[next_index]["model"]
        thinking_level = self._get_thinking_level_for_model_switch(scoped_models[next_index].get("thinkingLevel"))
        self.agent.state.model = next_model
        self.sessionManager.appendModelChange(next_model.provider, next_model.id)
        self.settingsManager.setDefaultModelAndProvider(next_model.provider, next_model.id)
        self.setThinkingLevel(thinking_level)
        await self._extensionRunner.emit(
            {
                "type": "model_select",
                "model": next_model,
                "previousModel": current_model,
                "source": "cycle",
            }
        )
        return ModelCycleResult(model=next_model, thinkingLevel=self.thinkingLevel, isScoped=True)

    async def _cycle_available_model(self, direction: str) -> ModelCycleResult | None:
        available_models = self._modelRegistry.getAvailable()
        if len(available_models) <= 1:
            return None
        current_model = self.model
        current_index = next(
            (index for index, model in enumerate(available_models) if models_are_equal(model, current_model)),
            -1,
        )
        current_index = max(current_index, 0)
        next_index = (current_index + (1 if direction != "backward" else -1)) % len(available_models)
        next_model = available_models[next_index]
        thinking_level = self._get_thinking_level_for_model_switch()
        self.agent.state.model = next_model
        self.sessionManager.appendModelChange(next_model.provider, next_model.id)
        self.settingsManager.setDefaultModelAndProvider(next_model.provider, next_model.id)
        self.setThinkingLevel(thinking_level)
        await self._extensionRunner.emit(
            {
                "type": "model_select",
                "model": next_model,
                "previousModel": current_model,
                "source": "cycle",
            }
        )
        return ModelCycleResult(model=next_model, thinkingLevel=self.thinkingLevel, isScoped=False)

    def _get_thinking_level_for_model_switch(self, explicitLevel: ThinkingLevel | None = None) -> ThinkingLevel:
        if explicitLevel is not None:
            return explicitLevel
        if not self.supportsThinking():
            return self.settingsManager.getDefaultThinkingLevel() or DEFAULT_THINKING_LEVEL
        return self.thinkingLevel

    def setSteeringMode(self, mode: str) -> None:
        self.agent.steeringMode = mode
        self.settingsManager.setSteeringMode(mode)

    def setFollowUpMode(self, mode: str) -> None:
        self.agent.followUpMode = mode
        self.settingsManager.setFollowUpMode(mode)

    def getActiveToolNames(self) -> list[str]:
        return [tool.name for tool in self.agent.state.tools]

    def getAllTools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=entry.definition.name,
                description=entry.definition.description,
                parameters=entry.definition.parameters,
                sourceInfo=entry.sourceInfo,
            )
            for entry in self._toolDefinitions.values()
        ]

    def getToolDefinition(self, name: str) -> Any | None:
        entry = self._toolDefinitions.get(name)
        return entry.definition if entry is not None else None

    def getSlashCommands(self) -> list[SlashCommandInfo]:
        commands: list[SlashCommandInfo] = []
        for command in self._extensionRunner.get_registered_commands():
            invocation_name = str(read_field(command, "invocationName", read_field(command, "name", ""))).strip()
            if not invocation_name:
                continue
            commands.append(
                _make_slash_command_info(
                    invocation_name,
                    "extension",
                    read_field(command, "sourceInfo")
                    or create_synthetic_source_info(
                        f"<extension-command:{invocation_name}>",
                        {"source": "inline", "scope": "temporary", "origin": "extension"},
                    ),
                    read_field(command, "description"),
                )
            )

        for template in self.promptTemplates:
            commands.append(
                _make_slash_command_info(
                    str(template.name),
                    "prompt",
                    template.sourceInfo
                    or create_synthetic_source_info(
                        f"<prompt:{template.name}>",
                        {"source": "auto", "scope": "project", "origin": "prompt"},
                    ),
                    template.description,
                )
            )


        return commands

    def setActiveToolsByName(self, toolNames: list[str]) -> None:
        active = [self._toolRegistry[name] for name in toolNames if name in self._toolRegistry]
        valid_names = [tool.name for tool in active]
        self.agent.state.tools = active
        self._baseSystemPrompt = self._rebuild_system_prompt(valid_names)
        self.agent.state.systemPrompt = self._baseSystemPrompt

    def setDisallowedToolsByName(
        self,
        toolNames: list[str],
        alwaysAllowed: list[str] | None = None,
    ) -> None:
        """Apply a case-insensitive deny-list to current and future tools."""

        self._disallowedToolNames = {
            name.casefold() for name in toolNames if isinstance(name, str) and name
        }
        self._alwaysAllowedToolNames = {
            name
            for name in (alwaysAllowed or [])
            if isinstance(name, str) and name
        }
        self._refresh_tool_registry({"includeAllExtensionTools": True})

    async def bindExtensions(self, bindings: ExtensionBindings | dict[str, Any] | None = None) -> None:
        resolved = bindings if isinstance(bindings, ExtensionBindings) else ExtensionBindings(**(bindings or {}))
        self._extensionBindings = resolved
        if resolved.uiContext is not None:
            self._extensionUIContext = resolved.uiContext
        if resolved.commandContextActions is not None:
            self._extensionCommandContextActions = resolved.commandContextActions
        if resolved.abortHandler is not None:
            self._extensionAbortHandler = resolved.abortHandler
        if resolved.shutdownHandler is not None:
            self._extensionShutdownHandler = resolved.shutdownHandler
        if resolved.onError is not None:
            self._extensionErrorListener = resolved.onError
        self._apply_extension_bindings(self._extensionRunner)
        await self._extensionRunner.emit(dict(self._sessionStartEvent))
        await self._extend_resources_from_extensions(
            "reload" if _event_field(self._sessionStartEvent, "reason") == "reload" else "startup"
        )

    async def reload(self) -> None:
        previous_flag_values = self._extensionRunner.get_flag_values()
        await emit_session_shutdown_event(self._extensionRunner, {"type": "session_shutdown", "reason": "reload"})
        # The old runner must unsubscribe from the shared event bus, or every
        # /reload leaks one more layer of handlers (pi #7656/6ca423447).
        self._extensionRunner.invalidate(_STALE_CONTEXT_MESSAGE)
        await self.settingsManager.reload()
        reset_api_providers()
        await self._resourceLoader.reload()
        self._build_runtime(
            {
                "activeToolNames": self.getActiveToolNames(),
                "flagValues": previous_flag_values,
                "includeAllExtensionTools": True,
            }
        )

        has_bindings = any(
            value is not None
            for value in (
                self._extensionUIContext,
                self._extensionCommandContextActions,
                self._extensionShutdownHandler,
                self._extensionErrorListener,
            )
        )
        if has_bindings:
            await self._extensionRunner.emit({"type": "session_start", "reason": "reload"})
            await self._extend_resources_from_extensions("reload")

    def createReplacedSessionContext(self) -> Any:
        context = self._extensionRunner.create_command_context()
        replaced_context = object.__new__(type(context))
        replaced_context.__dict__.update(context.__dict__)
        replaced_context.sendMessage = self.sendMessage
        replaced_context.sendUserMessage = self.sendUserMessage
        return replaced_context

    def hasExtensionHandlers(self, eventType: str) -> bool:
        return self._extensionRunner.has_handlers(eventType)

    async def sendCustomMessage(
        self,
        message: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        resolved_options = dict(options or {})
        normalized = _message_dict(message)
        app_message = {
            "role": "custom",
            "customType": read_field(normalized, "customType"),
            "content": _message_content(normalized),
            "display": bool(read_field(normalized, "display")),
            "details": read_field(normalized, "details"),
            "timestamp": int(time.time() * 1000),
        }
        deliver_as = resolved_options.get("deliverAs")
        if deliver_as == "nextTurn":
            self._pendingNextTurnMessages.append(app_message)
        elif self.isStreaming and resolved_options.get("triggerTurn") is not False:
            # triggerTurn=False: record the message without interrupting the running turn (pi #8022/47b5119d0)
            if deliver_as == "followUp":
                self.agent.followUp(app_message)
            else:
                self.agent.steer(app_message)
        elif resolved_options.get("triggerTurn"):
            await self._run_agent_prompt(app_message)
        else:
            self.agent.state.messages.append(app_message)
            self.sessionManager.appendCustomMessageEntry(
                str(app_message["customType"]),
                app_message["content"],
                bool(app_message["display"]),
                app_message["details"],
            )
            self._emit({"type": "message_start", "message": app_message})
            self._emit({"type": "message_end", "message": app_message})

    async def sendMessage(self, message: Any, options: dict[str, Any] | None = None) -> None:
        await self.sendCustomMessage(message, options)

    async def _send_user_message(
        self,
        content: str | list[TextContent | ImageContent | dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> None:
        resolved_options = dict(options or {})
        if isinstance(content, str):
            text = content
            images: list[ImageContent] | None = None
        else:
            text_parts: list[str] = []
            images = []
            for item in content:
                if _content_type(item) == "text":
                    text_parts.append(str(read_field(item, "text", "")))
                else:
                    images.append(item if isinstance(item, ImageContent) else ImageContent.model_validate(item))
            text = "\n".join(text_parts)
            if not images:
                images = None
        await self.prompt(
            text,
            {
                # Extensions may opt in to command dispatch and skill/template expansion (pi #7857/b987ead35); default stays False
                "expandPromptTemplates": bool(resolved_options.get("expandPromptTemplates", False)),
                "streamingBehavior": resolved_options.get("deliverAs"),
                "images": images,
                "source": "extension",
            },
        )

    async def sendUserMessage(
        self,
        content: str | list[TextContent | ImageContent | dict[str, Any]],
        options: dict[str, Any] | None = None,
    ) -> None:
        await self._send_user_message(content, options)

    def getUserMessagesForForking(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for entry in self.sessionManager.getEntries():
            if entry.get("type") != "message":
                continue
            message = entry.get("message")
            if _message_role(message) != "user":
                continue
            text = self._extract_user_message_text(_message_content(message))
            if text:
                result.append({"entryId": str(entry["id"]), "text": text})
        return result

    async def executeBash(
        self,
        command: str,
        onChunk: Callable[[str], None] | None = None,
        options: dict[str, Any] | None = None,
    ) -> BashResult:
        resolved_options = dict(options or {})
        self._bashAbortController = AbortController()
        prefix = self.settingsManager.getShellCommandPrefix()
        shell_path = self.settingsManager.getShellPath()
        resolved_command = f"{prefix}\n{command}" if prefix else command
        try:
            result = await execute_bash_with_operations(
                resolved_command,
                self.sessionManager.getCwd(),
                resolved_options.get("operations") or create_local_bash_operations({"shellPath": shell_path}),
                {
                    "onChunk": onChunk,
                    "signal": self._bashAbortController.signal,
                },
            )
            self.recordBashResult(command, result, resolved_options)
            return result
        finally:
            self._bashAbortController = None

    def recordBashResult(self, command: str, result: BashResult, options: dict[str, Any] | None = None) -> None:
        resolved_options = dict(options or {})
        bash_message = BashExecutionMessage(
            command=command,
            output=result.output,
            exitCode=result.exitCode,
            cancelled=result.cancelled,
            truncated=result.truncated,
            fullOutputPath=result.fullOutputPath,
            timestamp=int(time.time() * 1000),
            excludeFromContext=resolved_options.get("excludeFromContext"),
        )
        if self.isStreaming:
            self._pendingBashMessages.append(bash_message)
            return
        self.agent.state.messages.append(bash_message)
        self.sessionManager.appendMessage(bash_message)

    def abortBash(self) -> None:
        if self._bashAbortController is not None:
            self._bashAbortController.abort()

    def abortBranchSummary(self) -> None:
        if self._branchSummaryAbortController is not None:
            self._branchSummaryAbortController.abort()

    async def compact(self, customInstructions: str | None = None) -> SessionCompactionResult:
        # Keep the agent subscription live across compaction (pi #7370/e56893f4c):
        # unsubscribing here would drop the aborted partial message's
        # message_end/agent_end, so it would never be persisted or applied to state.
        await self.abort()
        self._compactionAbortController = AbortController()
        self._emit({"type": "compaction_start", "reason": "manual"})

        try:
            if self.model is None:
                raise RuntimeError(format_no_model_selected_message())

            auth = await self._get_compaction_request_auth(self.model)
            api_key = auth.get("apiKey")

            settings = CompactionSettings(**self.settingsManager.getCompactionSettings())
            branch_entries = self.sessionManager.getBranch()
            preparation = prepare_compaction(branch_entries, settings)
            if preparation is None:
                last_entry = branch_entries[-1] if branch_entries else None
                if isinstance(last_entry, dict) and last_entry.get("type") == "compaction":
                    raise RuntimeError("Already compacted")
                raise RuntimeError("Nothing to compact (session too small)")

            hook_result = None
            from_hook = False
            if self._extensionRunner.has_handlers("session_before_compact"):
                hook_result = await self._extensionRunner.emit(
                    {
                        "type": "session_before_compact",
                        "preparation": preparation,
                        "branchEntries": branch_entries,
                        "customInstructions": customInstructions,
                        "signal": self._compactionAbortController.signal,
                    }
                )
                if _result_flag(hook_result, "cancel", False):
                    raise RuntimeError("Compaction cancelled")

            provided = _result_flag(hook_result, "compaction")
            if provided is not None:
                result = SessionCompactionResult(
                    summary=_event_field(provided, "summary"),
                    firstKeptEntryId=_event_field(provided, "firstKeptEntryId"),
                    tokensBefore=int(_event_field(provided, "tokensBefore", 0)),
                    details=_event_field(provided, "details"),
                )
                from_hook = True
            else:
                result = await run_compaction(
                    preparation,
                    self.model,
                    api_key,
                    auth.get("headers"),
                    customInstructions,
                    self._compactionAbortController.signal,
                    self.thinkingLevel,
                    self.agent.streamFn,
                )

            if self._compactionAbortController.signal.aborted:
                raise RuntimeError("Compaction cancelled")

            self.sessionManager.appendCompaction(
                result.summary,
                result.firstKeptEntryId,
                result.tokensBefore,
                result.details,
                from_hook,
            )
            self.agent.state.messages = self.sessionManager.buildSessionContext().messages

            saved_entry = next(
                (
                    entry
                    for entry in reversed(self.sessionManager.getEntries())
                    if entry.get("type") == "compaction" and entry.get("summary") == result.summary
                ),
                None,
            )
            if saved_entry is not None:
                await self._extensionRunner.emit(
                    {
                        "type": "session_compact",
                        "compactionEntry": saved_entry,
                        "fromExtension": from_hook,
                    }
                )

            # compaction_end listeners may submit queued prompts, so expose idle state
            # before notifying them.  (pi 3852cb2)
            self._compactionAbortController = None
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "result": result,
                    "aborted": False,
                    "willRetry": False,
                }
            )
            return result
        except Exception as error:
            message = str(error)
            aborted = message == "Compaction cancelled" or getattr(error, "name", None) == "AbortError"
            self._compactionAbortController = None
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "result": None,
                    "aborted": aborted,
                    "willRetry": False,
                    "errorMessage": None if aborted else f"Compaction failed: {message}",
                }
            )
            await self._emit_session_compact_failed(
                reason="manual", aborted=aborted, will_retry=False,
                error_message=None if aborted else f"Compaction failed: {message}")
            raise
        finally:
            self._compactionAbortController = None

    async def _emit_session_compact_failed(self, *, reason: str, aborted: bool,
                                           will_retry: bool,
                                           error_message: str | None = None,
                                           from_extension: bool = False) -> None:
        """Emit the terminal event for a failed or aborted compaction (pi #8175/a6b1dbceb).

        Lets extensions such as telemetry or LCM pair a session_before_compact
        attempt with its outcome; previously failures only reached the UI.
        """
        if not self._extensionRunner.has_handlers("session_compact_failed"):
            return
        await self._extensionRunner.emit(
            {
                "type": "session_compact_failed",
                "reason": reason,
                "errorMessage": error_message,
                "aborted": aborted,
                "willRetry": will_retry,
                "fromExtension": from_extension,
            }
        )

    def abortCompaction(self) -> None:
        if self._auto_compaction_abort_controller is not None:
            self._auto_compaction_abort_controller.abort()
        if self._compactionAbortController is not None:
            self._compactionAbortController.abort()

    async def navigateTree(
        self,
        targetId: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_options = dict(options or {})
        old_leaf_id = self.sessionManager.getLeafId()

        if targetId == old_leaf_id:
            return {"cancelled": False}

        wants_summary = bool(resolved_options.get("summarize"))
        if wants_summary and self.model is None:
            raise RuntimeError("No model available for summarization")

        target_entry = self.sessionManager.getEntry(targetId)
        if target_entry is None:
            raise RuntimeError(f"Entry {targetId} not found")

        collected = collect_entries_for_branch_summary(self.sessionManager, old_leaf_id, targetId)
        custom_instructions = resolved_options.get("customInstructions")
        replace_instructions = resolved_options.get("replaceInstructions")
        label = resolved_options.get("label")
        preparation = {
            "targetId": targetId,
            "oldLeafId": old_leaf_id,
            "commonAncestorId": collected.commonAncestorId,
            "entriesToSummarize": collected.entries,
            "userWantsSummary": wants_summary,
            "customInstructions": custom_instructions,
            "replaceInstructions": replace_instructions,
            "label": label,
        }

        self._branchSummaryAbortController = AbortController()

        try:
            extension_summary: dict[str, Any] | None = None
            from_extension = False
            if self._extensionRunner.has_handlers("session_before_tree"):
                hook_result = await self._extensionRunner.emit(
                    {
                        "type": "session_before_tree",
                        "preparation": preparation,
                        "signal": self._branchSummaryAbortController.signal,
                    }
                )
                if _result_flag(hook_result, "cancel", False):
                    return {"cancelled": True}
                if wants_summary:
                    provided_summary = _result_flag(hook_result, "summary")
                    if provided_summary is not None:
                        extension_summary = _message_dict(provided_summary)
                        from_extension = True
                if _event_field(hook_result, "customInstructions") is not None:
                    custom_instructions = _event_field(hook_result, "customInstructions")
                if _event_field(hook_result, "replaceInstructions") is not None:
                    replace_instructions = _event_field(hook_result, "replaceInstructions")
                if _event_field(hook_result, "label") is not None:
                    label = _event_field(hook_result, "label")

            summary_text: str | None = None
            summary_details: dict[str, Any] | None = None
            if wants_summary and collected.entries and extension_summary is None:
                auth = await self._get_required_request_auth(self.model)
                branch_summary_settings = self.settingsManager.getBranchSummarySettings()
                summary_result = await generate_branch_summary(
                    collected.entries,
                    GenerateBranchSummaryOptions(
                        model=self.model,
                        apiKey=auth["apiKey"],
                        headers=auth.get("headers"),
                        signal=self._branchSummaryAbortController.signal,
                        customInstructions=custom_instructions,
                        replaceInstructions=replace_instructions,
                        reserveTokens=int(branch_summary_settings.get("reserveTokens", 16384)),
                    ),
                )
                if summary_result.aborted:
                    return {"cancelled": True, "aborted": True}
                if summary_result.error:
                    raise RuntimeError(summary_result.error)
                summary_text = summary_result.summary
                summary_details = {
                    "readFiles": list(summary_result.readFiles or []),
                    "modifiedFiles": list(summary_result.modifiedFiles or []),
                }
            elif extension_summary is not None:
                summary_text = _event_field(extension_summary, "summary")
                details = _event_field(extension_summary, "details")
                summary_details = _message_dict(details) if details is not None else None

            new_leaf_id: str | None
            editor_text: str | None = None
            entry_type = target_entry.get("type")
            if entry_type == "message" and _message_role(target_entry.get("message")) == "user":
                parent_id = target_entry.get("parentId")
                new_leaf_id = parent_id if isinstance(parent_id, str) else None
                editor_text = self._extract_user_message_text(_message_content(target_entry.get("message")))
            elif entry_type == "custom_message":
                parent_id = target_entry.get("parentId")
                new_leaf_id = parent_id if isinstance(parent_id, str) else None
                custom_content = target_entry.get("content")
                if isinstance(custom_content, str):
                    editor_text = custom_content
                elif isinstance(custom_content, list):
                    editor_text = "".join(
                        str(read_field(block, "text", ""))
                        for block in custom_content
                        if _content_type(block) == "text"
                    )
            else:
                new_leaf_id = targetId

            summary_entry = None
            if summary_text:
                summary_id = self.sessionManager.branchWithSummary(
                    new_leaf_id,
                    summary_text,
                    summary_details,
                    from_extension,
                )
                summary_entry = self.sessionManager.getEntry(summary_id)
                if label:
                    self.sessionManager.appendLabelChange(summary_id, str(label))
            elif new_leaf_id is None:
                self.sessionManager.resetLeaf()
            else:
                self.sessionManager.branch(new_leaf_id)

            if label and not summary_text:
                self.sessionManager.appendLabelChange(targetId, str(label))

            self.agent.state.messages = self.sessionManager.buildSessionContext().messages

            await self._extensionRunner.emit(
                {
                    "type": "session_tree",
                    "newLeafId": self.sessionManager.getLeafId(),
                    "oldLeafId": old_leaf_id,
                    "summaryEntry": summary_entry,
                    "fromExtension": from_extension if summary_text else None,
                }
            )

            result: dict[str, Any] = {"cancelled": False}
            if editor_text:
                result["editorText"] = editor_text
            if summary_entry is not None:
                result["summaryEntry"] = summary_entry
            return result
        finally:
            self._branchSummaryAbortController = None

    def setAutoCompactionEnabled(self, enabled: bool) -> None:
        self.settingsManager.setCompactionEnabled(enabled)

    def setAutoRetryEnabled(self, enabled: bool) -> None:
        self.settingsManager.setRetryEnabled(enabled)

    def abortRetry(self) -> None:
        if self._retryAbortController is not None:
            self._retryAbortController.abort()

    def getSessionStats(self) -> SessionStats:
        state = self.state
        user_messages = sum(1 for message in state.messages if _message_role(message) == "user")
        assistant_messages = sum(1 for message in state.messages if _message_role(message) == "assistant")
        tool_results = sum(1 for message in state.messages if _message_role(message) == "toolResult")
        tool_calls = 0
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_cost = 0.0

        for message in state.messages:
            if _message_role(message) != "assistant":
                continue
            usage = read_field(message, "usage") or {}
            content = _message_content(message)
            if isinstance(content, list):
                tool_calls += sum(1 for block in content if _content_type(block) == "toolCall")
            # usage may be a dict or a Usage object (providers return objects);
            # a bare .get() would crash /session once the session has usage.
            def _u(field, src=usage, default=0):
                if isinstance(src, dict):
                    return src.get(field, default)
                return getattr(src, field, default) or default

            total_input += int(_u("input"))
            total_output += int(_u("output"))
            total_cache_read += int(_u("cacheRead"))
            total_cache_write += int(_u("cacheWrite"))
            cost = _u("cost", default=None) or {}
            total_cost += float(_u("total", cost, 0) or 0)

        return SessionStats(
            sessionFile=self.sessionFile,
            sessionId=self.sessionId,
            userMessages=user_messages,
            assistantMessages=assistant_messages,
            toolCalls=tool_calls,
            toolResults=tool_results,
            totalMessages=len(state.messages),
            tokens=SessionTokenStats(
                input=total_input,
                output=total_output,
                cacheRead=total_cache_read,
                cacheWrite=total_cache_write,
                total=total_input + total_output + total_cache_read + total_cache_write,
            ),
            cost=total_cost,
            contextUsage=self.getContextUsage(),
        )

    async def exportToHtml(self, outputPath: str | None = None) -> str:
        return await export_session_to_html(
            self.sessionManager,
            self.state,
            {
                "outputPath": outputPath,
                "themeName": self.settingsManager.getTheme(),
                "toolRenderer": create_tool_html_renderer(
                    {
                        "getToolDefinition": self.getToolDefinition,
                        "theme": theme,
                        "cwd": self.sessionManager.getCwd(),
                    }
                ),
            },
        )

    def exportToJsonl(self, outputPath: str | None = None) -> str:
        resolved_output = outputPath or (
            f"session-{datetime.now(UTC).isoformat(timespec='milliseconds').replace(':', '-').replace('.', '-')}.jsonl"
        )
        file_path = resolve_path(resolved_output, os.getcwd())
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        header = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": self.sessionManager.getSessionId(),
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "cwd": self.sessionManager.getCwd(),
        }

        branch_entries = self.sessionManager.getBranch()
        lines = [_dump_json(header)]
        previous_id: str | None = None
        for entry in branch_entries:
            linear_entry = dict(entry)
            linear_entry["parentId"] = previous_id
            lines.append(_dump_json(linear_entry))
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                previous_id = entry_id

        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return file_path

    def getLastAssistantText(self) -> str | None:
        for message in reversed(self.messages):
            if _message_role(message) != "assistant":
                continue
            if read_field(message, "stopReason") == "aborted" and not _message_content(message):
                continue
            content = _message_content(message)
            if not isinstance(content, list):
                continue
            text = "".join(
                str(read_field(block, "text", ""))
                for block in content
                if _content_type(block) == "text"
            )
            if text:
                return text.strip() or None
        return None

    def getContextUsage(self) -> dict[str, float | int | None] | None:
        model = self.model
        if model is None or model.contextWindow <= 0:
            return None

        branch_entries = self.sessionManager.getBranch()
        latest_compaction = get_latest_compaction_entry(branch_entries)
        if latest_compaction is not None:
            compaction_index = next(
                (
                    index
                    for index, entry in enumerate(branch_entries)
                    if entry.get("type") == "compaction" and entry.get("id") == latest_compaction.get("id")
                ),
                -1,
            )
            has_post_compaction_usage = False
            for entry in reversed(branch_entries[compaction_index + 1 :]):
                if entry.get("type") != "message":
                    continue
                message = entry.get("message")
                if _message_role(message) != "assistant":
                    continue
                if read_field(message, "stopReason") in {"aborted", "error"}:
                    continue
                if _calculate_context_tokens(read_field(message, "usage") or {}) > 0:
                    has_post_compaction_usage = True
                break
            if not has_post_compaction_usage:
                return {"tokens": None, "contextWindow": model.contextWindow, "percent": None}

        estimate = estimate_compaction_context_tokens(list(self.messages))
        percent = (estimate.tokens / model.contextWindow) * 100 if model.contextWindow else None
        return {"tokens": estimate.tokens, "contextWindow": model.contextWindow, "percent": percent}

    async def _set_model_if_configured(self, model: Model[Any]) -> bool:
        if not self._modelRegistry.hasConfiguredAuth(model):
            return False
        await self.setModel(model)
        return True

    async def _handle_agent_event(self, event: Any, _signal: Any | None = None) -> None:
        event_type = _event_type(event)
        message = _event_field(event, "message")
        if event_type == "message_start" and _message_role(message) == "user":
            self._overflow_recovery_attempted = False
            message_text = self._extract_user_message_text(_message_content(message))
            if message_text:
                if message_text in self._steeringMessages:
                    self._steeringMessages.remove(message_text)
                    self._emit_queue_update()
                elif message_text in self._followUpMessages:
                    self._followUpMessages.remove(message_text)
                    self._emit_queue_update()

        await self._emit_extension_event(event)

        if event_type == "agent_end":
            self._emit(_decorate_agent_end_event(event, self._will_retry_after_agent_end(event)))
        else:
            self._emit(event)

        if event_type == "message_end" and message is not None:
            self._persist_message(message)
            assistant_message = _as_assistant_message(message)
            if assistant_message is not None:
                self._lastAssistantMessage = assistant_message
                # "length" is also a mid-recovery failure state: resetting here would
                # zero the truncation retry counter and loop forever (#7540)
                if assistant_message.stopReason not in ("error", "length"):
                    self._overflow_recovery_attempted = False
                if assistant_message.stopReason != "error" and self._retryAttempt > 0:
                    self._emit(
                        {
                            "type": "auto_retry_end",
                            "success": True,
                            "attempt": self._retryAttempt,
                        }
                    )
                    self._retryAttempt = 0

    def _emit(self, event: Any) -> None:
        for listener in self._eventListeners:
            listener(event)

    async def _try_execute_extension_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        command_text = text[1:]
        command_name, _, raw_args = command_text.partition(" ")
        resolved = self._extensionRunner.get_command(command_name)
        if resolved is None or resolved.handler is None:
            return False

        try:
            result = resolved.handler(raw_args, self._extensionRunner.create_command_context())
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001 - extension code: any failure is reported as an extension error
            self._extensionRunner.emit_error(
                ExtensionError(
                    extensionPath=f"command:{command_name}",
                    event="command",
                    error=str(error),
                )
            )
        return True

    def _persist_message(self, message: Any) -> None:
        role = _message_role(message)
        if role == "custom":
            self.sessionManager.appendCustomMessageEntry(
                str(read_field(message, "customType")),
                _message_content(message),
                bool(read_field(message, "display")),
                read_field(message, "details"),
            )
        elif role in {"user", "assistant", "toolResult"}:
            self.sessionManager.appendMessage(_message_dict(message))

    def _apply_extension_bindings(self, runner: ExtensionRunner) -> None:
        runner.set_ui_context(self._extensionUIContext)
        runner.bind_command_context(self._extensionCommandContextActions)
        if self._extensionErrorUnsubscriber is not None:
            self._extensionErrorUnsubscriber()
            self._extensionErrorUnsubscriber = None
        self._extensionErrorUnsubscriber = (
            runner.on_error(self._extensionErrorListener) if self._extensionErrorListener is not None else None
        )

    async def _extend_resources_from_extensions(self, reason: str) -> None:
        if not self._extensionRunner.has_handlers("resources_discover"):
            return

        discovered = await self._extensionRunner.emit_resources_discover(self._cwd, reason)
        if not discovered["promptPaths"] and not discovered["themePaths"]:
            return

        self._resourceLoader.extendResources(
            {
                "promptPaths": self._build_extension_resource_paths(discovered["promptPaths"]),
                "themePaths": self._build_extension_resource_paths(discovered["themePaths"]),
            }
        )
        self._baseSystemPrompt = self._rebuild_system_prompt(self.getActiveToolNames())
        self.agent.state.systemPrompt = self._baseSystemPrompt

    def _build_extension_resource_paths(self, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in entries:
            extension_path = entry["extensionPath"]
            base_dir = None if extension_path.startswith("<") else os.path.dirname(extension_path)
            result.append(
                {
                    "path": entry["path"],
                    "metadata": {
                        "source": self._get_extension_source_label(extension_path),
                        "scope": "temporary",
                        "origin": "top-level",
                        "baseDir": base_dir,
                    },
                }
            )
        return result

    def _get_extension_source_label(self, extension_path: str) -> str:
        if extension_path.startswith("<"):
            return f"extension:{extension_path.replace('<', '').replace('>', '')}"
        base = os.path.basename(extension_path)
        return f"extension:{re.sub(r'[.](ts|js)$', '', base)}"

    async def _emit_extension_event(self, event: Any) -> None:
        event_type = _event_type(event)
        if event_type == "agent_start":
            self._turnIndex = 0
            await self._extensionRunner.emit({"type": "agent_start"})
        elif event_type == "agent_end":
            hook_result = await self._extensionRunner.emit_agent_end(
                {
                    "type": "agent_end",
                    "messages": list(_event_field(event, "messages", []) or []),
                }
            )
            if _event_field(hook_result, "block", False):
                reason = str(
                    _event_field(
                        hook_result,
                        "reason",
                        "Stop hook blocked completion",
                    )
                )
                self._stopHookContinuationPending = True
                await self._queue_follow_up(f"Stop hook feedback:\n{reason}")
        elif event_type == "turn_start":
            await self._extensionRunner.emit(
                {
                    "type": "turn_start",
                    "turnIndex": self._turnIndex,
                    "timestamp": int(time.time() * 1000),
                }
            )
        elif event_type == "turn_end":
            await self._extensionRunner.emit(
                {
                    "type": "turn_end",
                    "turnIndex": self._turnIndex,
                    "message": _event_field(event, "message"),
                    "toolResults": _event_field(event, "toolResults"),
                }
            )
            self._turnIndex += 1
        elif event_type == "message_start":
            await self._extensionRunner.emit({"type": "message_start", "message": _event_field(event, "message")})
        elif event_type == "message_update":
            await self._extensionRunner.emit(
                {
                    "type": "message_update",
                    "message": _event_field(event, "message"),
                    "assistantMessageEvent": _event_field(event, "assistantMessageEvent"),
                }
            )
        elif event_type == "message_end":
            message = _event_field(event, "message")
            replacement = await self._extensionRunner.emit_message_end({"type": "message_end", "message": message})
            if replacement is not None and message is not None:
                self._replace_message_in_place(message, replacement)
        elif event_type == "tool_execution_start":
            await self._extensionRunner.emit(
                {
                    "type": "tool_execution_start",
                    "toolCallId": _event_field(event, "toolCallId"),
                    "toolName": _event_field(event, "toolName"),
                    "args": _event_field(event, "args"),
                }
            )
        elif event_type == "tool_execution_update":
            await self._extensionRunner.emit(
                {
                    "type": "tool_execution_update",
                    "toolCallId": _event_field(event, "toolCallId"),
                    "toolName": _event_field(event, "toolName"),
                    "args": _event_field(event, "args"),
                    "partialResult": _event_field(event, "partialResult"),
                }
            )
        elif event_type == "tool_execution_end":
            await self._extensionRunner.emit(
                {
                    "type": "tool_execution_end",
                    "toolCallId": _event_field(event, "toolCallId"),
                    "toolName": _event_field(event, "toolName"),
                    "result": _event_field(event, "result"),
                    "isError": _event_field(event, "isError"),
                }
            )

    def _replace_message_in_place(self, target: Any, replacement: Any) -> None:
        if target is replacement:
            return
        replacement_dict = _message_dict(replacement)
        if isinstance(target, dict):
            target.clear()
            target.update(replacement_dict)
            return
        target_dict = getattr(target, "__dict__", None)
        if isinstance(target_dict, dict):
            target_dict.clear()
            target_dict.update(replacement_dict)
            fields_set = getattr(target, "__pydantic_fields_set__", None)
            if isinstance(fields_set, set):
                fields_set.clear()
                fields_set.update(replacement_dict.keys())
            return
        for key, value in replacement_dict.items():
            setattr(target, key, value)

    def _bind_extension_core(self, runner: ExtensionRunner) -> None:
        def _emit_runtime_error(event: str, error: Exception) -> None:
            runner.emit_error(
                ExtensionError(
                    extensionPath="<runtime>",
                    event=event,
                    error=str(error),
                )
            )

        def _send_message_from_runtime(message: Any, options: dict[str, Any] | None = None) -> None:
            async def _run() -> None:
                try:
                    await self.sendCustomMessage(message, options)
                except Exception as error:  # noqa: BLE001
                    _emit_runtime_error(
                        "send_message",
                        error if isinstance(error, Exception) else RuntimeError(str(error)),
                    )

            self._spawn_background(_run())

        def _send_user_message_from_runtime(
            content: str | list[TextContent | ImageContent | dict[str, Any]],
            options: dict[str, Any] | None = None,
        ) -> None:
            async def _run() -> None:
                try:
                    await self._send_user_message(content, options)
                except Exception as error:  # noqa: BLE001
                    _emit_runtime_error(
                        "send_user_message",
                        error if isinstance(error, Exception) else RuntimeError(str(error)),
                    )

            self._spawn_background(_run())

        def _compact_from_extension(options: dict[str, Any] | None = None) -> None:
            async def _run() -> None:
                try:
                    result = await self.compact(_event_field(options, "customInstructions"))
                    on_complete = _event_field(options, "onComplete")
                    if callable(on_complete):
                        on_complete(result)
                except Exception as error:  # noqa: BLE001
                    on_error = _event_field(options, "onError")
                    if callable(on_error):
                        on_error(error if isinstance(error, Exception) else RuntimeError(str(error)))

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_run())
                return
            loop.create_task(_run())

        runner.bind_core(
            {
                "sendMessage": _send_message_from_runtime,
                "sendUserMessage": _send_user_message_from_runtime,
                "appendEntry": lambda customType, data=None: self.sessionManager.appendCustomEntry(customType, data),
                "setSessionName": self.setSessionName,
                "getSessionName": self.sessionManager.getSessionName,
                "setLabel": lambda entryId, label: self.sessionManager.appendLabelChange(entryId, label),
                "getActiveTools": self.getActiveToolNames,
                "getAllTools": self.getAllTools,
                "setActiveTools": self.setActiveToolsByName,
                "refreshTools": lambda: self._refresh_tool_registry(),
                "getCommands": self.getSlashCommands,
                "setModel": self._set_model_if_configured,
                "getThinkingLevel": lambda: self.thinkingLevel,
                "setThinkingLevel": self.setThinkingLevel,
            },
            {
                "getModel": lambda: self.model,
                "isIdle": lambda: not self.isStreaming,
                "getSignal": lambda: self.agent.signal,
                "abort": lambda: self._extensionAbortHandler() if self._extensionAbortHandler else self._spawn_background(self.abort()),
                "hasPendingMessages": lambda: self.pendingMessageCount > 0,
                "shutdown": lambda: self._extensionShutdownHandler() if self._extensionShutdownHandler else None,
                "getContextUsage": self.getContextUsage,
                "compact": _compact_from_extension,
                "getSystemPrompt": lambda: self.systemPrompt,
            },
            {
                "registerProvider": self._register_provider,
                "unregisterProvider": self._unregister_provider,
            },
        )

    def _create_extension_runner(self, flag_values: dict[str, bool | str] | None = None) -> ExtensionRunner:
        extensions_result = self._resourceLoader.getExtensions()
        if flag_values:
            for name, value in flag_values.items():
                extensions_result.runtime.flagValues[name] = value
        runner = ExtensionRunner(
            extensions=list(extensions_result.extensions),
            runtime=extensions_result.runtime,
            cwd=self._cwd,
            sessionManager=self.sessionManager,
            modelRegistry=self._modelRegistry,
        )
        self._bind_extension_core(runner)
        if self._extensionRunnerRef is not None:
            self._extensionRunnerRef["current"] = runner
        return runner

    def _register_provider(self, name: str, config: dict[str, Any]) -> None:
        self._modelRegistry.registerProvider(name, config)
        self._refresh_current_model_from_registry()

    def _unregister_provider(self, name: str) -> None:
        self._modelRegistry.unregisterProvider(name)
        self._refresh_current_model_from_registry()

    def _refresh_current_model_from_registry(self) -> None:
        current_model = self.model
        if current_model is None:
            return
        refreshed_model = self._modelRegistry.find(current_model.provider, current_model.id)
        if refreshed_model is None or refreshed_model == current_model:
            return
        self.agent.state.model = refreshed_model

    def _refresh_tool_registry(self, options: dict[str, Any] | None = None) -> None:
        resolved_options = dict(options or {})
        previous_registry_names = set(self._toolRegistry)
        previous_active_tool_names = self.getActiveToolNames()

        def is_allowed_tool(name: str) -> bool:
            normalized = name.casefold()
            if name in self._alwaysAllowedToolNames:
                return True
            denied = (
                "*" in self._disallowedToolNames
                or normalized in self._disallowedToolNames
            )
            return not denied and (
                self._allowedToolNames is None or name in self._allowedToolNames
            )

        registered_tools = self._extensionRunner.get_all_registered_tools()
        all_custom_tools: list[tuple[Any, SourceInfo]] = [
            *( (tool.definition, tool.sourceInfo) for tool in registered_tools ),
            *(
                (
                    definition
                    if isinstance(definition, ToolDefinition)
                    else create_tool_definition_from_agent_tool(definition),
                    create_synthetic_source_info(f"<sdk:{definition.name}>", {"source": "sdk"}),
                )
                for definition in self._customTools
            ),
        ]
        all_custom_tools = [
            (definition, source_info)
            for definition, source_info in all_custom_tools
            if is_allowed_tool(definition.name)
        ]

        definition_registry: dict[str, _ToolDefinitionEntry] = {}
        for name, definition in self._baseToolDefinitions.items():
            if not is_allowed_tool(name):
                continue
            definition_registry[name] = _ToolDefinitionEntry(
                definition=definition,
                sourceInfo=create_synthetic_source_info(f"<builtin:{name}>", {"source": "builtin"}),
                promptSnippet=self._normalize_prompt_snippet(_definition_attr(definition, "promptSnippet")),
                promptGuidelines=self._normalize_prompt_guidelines(_definition_attr(definition, "promptGuidelines")),
            )
        for definition, source_info in all_custom_tools:
            definition_registry[definition.name] = _ToolDefinitionEntry(
                definition=definition,
                sourceInfo=source_info,
                promptSnippet=self._normalize_prompt_snippet(_definition_attr(definition, "promptSnippet")),
                promptGuidelines=self._normalize_prompt_guidelines(_definition_attr(definition, "promptGuidelines")),
            )
        self._toolDefinitions = definition_registry

        tool_registry: dict[str, AgentTool] = {}
        for name, definition in self._baseToolDefinitions.items():
            if is_allowed_tool(name):
                tool_registry[name] = wrap_tool_definition(definition, ctx_factory=self._extensionRunner.create_context)
        for definition, _source_info in all_custom_tools:
            tool_registry[definition.name] = wrap_tool_definition(
                definition,
                ctx_factory=self._extensionRunner.create_context,
            )
        self._toolRegistry = tool_registry

        if "activeToolNames" in resolved_options:
            next_active_tool_names = list(resolved_options["activeToolNames"] or [])
        else:
            next_active_tool_names = list(previous_active_tool_names)
        next_active_tool_names = [name for name in next_active_tool_names if is_allowed_tool(name)]

        if self._allowedToolNames is not None:
            for tool_name in self._toolRegistry:
                if tool_name in self._allowedToolNames:
                    next_active_tool_names.append(tool_name)
        elif resolved_options.get("includeAllExtensionTools"):
            for definition, _source_info in all_custom_tools:
                next_active_tool_names.append(definition.name)
        elif "activeToolNames" not in resolved_options:
            for tool_name in self._toolRegistry:
                if tool_name not in previous_registry_names:
                    next_active_tool_names.append(tool_name)

        active_tool_names: list[str] = []
        seen: set[str] = set()
        for name in next_active_tool_names:
            if name in self._toolRegistry and name not in seen:
                seen.add(name)
                active_tool_names.append(name)
        self.setActiveToolsByName(active_tool_names)

    def _build_runtime(self, options: dict[str, Any] | None = None) -> None:
        resolved_options = dict(options or {})
        auto_resize_images = self.settingsManager.getImageAutoResize()
        shell_command_prefix = self.settingsManager.getShellCommandPrefix()
        shell_path = self.settingsManager.getShellPath()

        if self._baseToolsOverride:
            base_tool_definitions = {
                name: create_tool_definition_from_agent_tool(tool)
                for name, tool in self._baseToolsOverride.items()
            }
        else:
            base_tool_definitions = create_all_tool_definitions(
                self._cwd,
                {
                    "read": {"autoResizeImages": auto_resize_images},
                    "bash": {"commandPrefix": shell_command_prefix, "shellPath": shell_path},
                },
            )

        self._baseToolDefinitions = dict(base_tool_definitions)
        self._extensionRunner = self._create_extension_runner(resolved_options.get("flagValues"))
        self._apply_extension_bindings(self._extensionRunner)

        default_active_tool_names = (
            list(self._baseToolsOverride)
            if self._baseToolsOverride
            else ["read", "bash", "edit", "write"]
        )
        base_active_tool_names = (
            list(resolved_options["activeToolNames"])
            if "activeToolNames" in resolved_options and resolved_options["activeToolNames"] is not None
            else default_active_tool_names
        )
        self._refresh_tool_registry(
            {
                "activeToolNames": base_active_tool_names,
                "includeAllExtensionTools": resolved_options.get("includeAllExtensionTools"),
            }
        )

    def _normalize_prompt_snippet(self, text: str | None) -> str | None:
        if not text:
            return None
        one_line = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
        return one_line or None

    def _normalize_prompt_guidelines(self, guidelines: Any) -> list[str]:
        if not guidelines:
            return []
        unique: list[str] = []
        for guideline in list(guidelines):
            normalized = str(guideline).strip()
            if normalized and normalized not in unique:
                unique.append(normalized)
        return unique

    def _rebuild_system_prompt(self, toolNames: list[str]) -> str:
        valid_tool_names = [name for name in toolNames if name in self._toolRegistry]
        tool_snippets: dict[str, str] = {}
        prompt_guidelines: list[str] = []
        for name in valid_tool_names:
            entry = self._toolDefinitions.get(name)
            if entry is None:
                continue
            if entry.promptSnippet:
                tool_snippets[name] = entry.promptSnippet
            prompt_guidelines.extend(entry.promptGuidelines)

        loader_system_prompt = self._resourceLoader.getSystemPrompt()
        loader_append_system_prompt = self._resourceLoader.getAppendSystemPrompt()
        append_system_prompt = "\n\n".join(loader_append_system_prompt) if loader_append_system_prompt else None
        self._baseSystemPromptOptions = {
            "cwd": self._cwd,
            "contextFiles": self._resourceLoader.getAgentsFiles()["agentsFiles"],
            "customPrompt": loader_system_prompt,
            "appendSystemPrompt": append_system_prompt,
            "selectedTools": valid_tool_names,
            "toolSnippets": tool_snippets,
            "promptGuidelines": prompt_guidelines,
        }
        return build_system_prompt(self._baseSystemPromptOptions)

    def _build_user_message(
        self,
        text: str,
        images: Sequence[ImageContent] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [TextContent(text=text).model_dump()]
        if images:
            content.extend(image.model_dump() if hasattr(image, "model_dump") else image for image in images)
        return {"role": "user", "content": content, "timestamp": int(time.time() * 1000)}

    def _extract_user_message_text(self, content: str | list[dict[str, Any]]) -> str:
        if isinstance(content, str):
            return content
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    async def _normalize_tool_result_images(self, result: Any) -> None:
        """Normalize image blocks in a tool result in place (pi #7330). No-op when autoResizeImages is off."""
        content = _event_field(result, "content")
        if not isinstance(content, list) or not self.settingsManager.getImageAutoResize():
            return
        from misaka.ai.types import ImageContent
        from misaka.utils.image_resize import resize_image
        for index, block in enumerate(list(content)):
            if read_field(block, "type") != "image":
                continue
            img = block if isinstance(block, ImageContent) else ImageContent(
                data=str(read_field(block, "data") or ""),
                mimeType=str(read_field(block, "mimeType") or ""))
            try:
                resized = await resize_image(img)
            except Exception:  # noqa: BLE001, S112 - keep the original block if the image backend is unavailable (matches upstream)
                continue
            if resized is None or not resized.wasResized:
                continue
            replacement = dict(block) if isinstance(block, dict) else {"type": "image"}
            replacement.update({"type": "image", "data": resized.data,
                                "mimeType": resized.mimeType})
            content[index] = replacement

    def _install_agent_tool_hooks(self) -> None:
        async def before_tool_call(payload: Any, _signal: Any | None = None) -> Any:
            runner = self._extensionRunner
            if not runner.has_handlers("tool_call"):
                return None
            tool_call = _event_field(payload, "toolCall")
            args = _event_field(payload, "args")
            return await runner.emit_tool_call(  # type: ignore[attr-defined]
                {
                    "type": "tool_call",
                    "toolName": read_field(tool_call, "name"),
                    "toolCallId": read_field(tool_call, "id"),
                    "input": args,
                }
            )

        async def after_tool_call(payload: Any, _signal: Any | None = None) -> Any:
            runner = self._extensionRunner
            result = _event_field(payload, "result")
            # Images produced by tools (extensions/MCP/screenshots) bypass read's resizing,
            # and an oversized image makes the provider reject the whole conversation, so
            # normalize once before they enter history (pi #7330/b0e05b442). On failure keep
            # the original block: an unavailable image backend must not silently drop output.
            await self._normalize_tool_result_images(result)
            if not runner.has_handlers("tool_result"):
                return None
            tool_call = _event_field(payload, "toolCall")
            hook_result = await runner.emit_tool_result(  # type: ignore[attr-defined]
                {
                    "type": "tool_result",
                    "toolName": read_field(tool_call, "name"),
                    "toolCallId": read_field(tool_call, "id"),
                    "input": _event_field(payload, "args"),
                    "content": _event_field(result, "content"),
                    "details": _event_field(result, "details"),
                    "isError": bool(_event_field(payload, "isError")),
                }
            )
            if not hook_result:
                return None
            return {
                "content": _event_field(hook_result, "content"),
                "details": _event_field(hook_result, "details"),
                "isError": _event_field(hook_result, "isError", _event_field(payload, "isError")),
            }

        self.agent.beforeToolCall = before_tool_call
        self.agent.afterToolCall = after_tool_call

    async def _run_agent_prompt(self, messages: AgentMessage | list[AgentMessage]) -> None:
        try:
            await self.agent.prompt(messages)
            while await self._handle_post_agent_run():
                await self.agent.continue_()
        finally:
            self._flush_pending_bash_messages()

    async def _handle_post_agent_run(self) -> bool:
        message = self._lastAssistantMessage
        self._lastAssistantMessage = None
        if message is None:
            return False

        if self._stopHookContinuationPending:
            self._stopHookContinuationPending = False
            return True

        if self._is_retryable_error(message) and await self._prepare_retry(message):
            return True

        if message.stopReason == "error" and self._retryAttempt > 0:
            self._emit(
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "attempt": self._retryAttempt,
                    "finalError": message.errorMessage,
                }
            )
            self._retryAttempt = 0

        return await self._check_compaction(message)

    def _will_retry_after_agent_end(self, event: Any) -> bool:
        if self._stopHookContinuationPending:
            return True
        settings = self.settingsManager.getRetrySettings()
        if not bool(settings.get("enabled")) or self._retryAttempt >= int(settings.get("maxRetries", 0) or 0):
            return False

        for message in reversed(list(_event_field(event, "messages", []) or [])):
            assistant_message = _as_assistant_message(message)
            if assistant_message is not None:
                return self._is_retryable_error(assistant_message)
        return False

    def _is_retryable_error(self, message: AssistantMessage) -> bool:
        if message.stopReason != "error" or not message.errorMessage:
            return False

        context_window = self.model.contextWindow if self.model is not None else 0
        if is_context_overflow(message, context_window):
            return False

        return bool(_RETRYABLE_ERROR_PATTERN.search(message.errorMessage))

    async def _prepare_retry(self, message: AssistantMessage) -> bool:
        settings = self.settingsManager.getRetrySettings()
        if not bool(settings.get("enabled")):
            return False

        self._retryAttempt += 1
        max_retries = int(settings.get("maxRetries", 0) or 0)
        if self._retryAttempt > max_retries:
            self._retryAttempt -= 1
            return False

        delay_ms = int(settings.get("baseDelayMs", 0) or 0) * (2 ** (self._retryAttempt - 1))
        self._emit(
            {
                "type": "auto_retry_start",
                "attempt": self._retryAttempt,
                "maxAttempts": max_retries,
                "delayMs": delay_ms,
                "errorMessage": message.errorMessage or "Unknown error",
            }
        )

        messages = self.agent.state.messages
        if messages and _message_role(messages[-1]) == "assistant":
            self.agent.state.messages = messages[:-1]

        self._retryAbortController = AbortController()
        try:
            should_continue = await _sleep_with_abort(delay_ms, self._retryAbortController.signal)
            if not should_continue:
                attempt = self._retryAttempt
                self._retryAttempt = 0
                self._emit(
                    {
                        "type": "auto_retry_end",
                        "success": False,
                        "attempt": attempt,
                        "finalError": "Retry cancelled",
                    }
                )
                return False
        finally:
            self._retryAbortController = None

        return True

    def _find_last_assistant_message(self) -> AssistantMessage | None:
        for message in reversed(self.agent.state.messages):
            assistant_message = _as_assistant_message(message)
            if assistant_message is not None:
                return assistant_message
        return None

    def _flush_pending_bash_messages(self) -> None:
        if not self._pendingBashMessages:
            return
        for bash_message in self._pendingBashMessages:
            self.agent.state.messages.append(bash_message)
            self.sessionManager.appendMessage(bash_message)
        self._pendingBashMessages = []

    async def _check_compaction(
        self,
        assistant_message: AssistantMessage,
        skip_aborted_check: bool = True,
    ) -> bool:
        settings_data = self.settingsManager.getCompactionSettings()
        if not bool(settings_data.get("enabled")):
            return False

        if skip_aborted_check and assistant_message.stopReason == "aborted":
            return False

        current_model = self.model
        context_window = current_model.contextWindow if current_model is not None else 0
        same_model = (
            current_model is not None
            and assistant_message.provider == current_model.provider
            and assistant_message.model == current_model.id
        )

        branch_entries = self.sessionManager.getBranch()
        latest_compaction = get_latest_compaction_entry(branch_entries)
        latest_compaction_timestamp = _event_timestamp_ms(_event_field(latest_compaction, "timestamp"))
        assistant_timestamp = _event_timestamp_ms(assistant_message.timestamp)
        if (
            latest_compaction is not None
            and assistant_timestamp > 0
            and assistant_timestamp <= latest_compaction_timestamp
        ):
            return False

        # A length stop below the output cap is a recoverable failure: run the same
        # compact-and-retry once (pi #7540/32850ef7c). A "stop" answer that overflowed the
        # window is only compacted, not retried: it is complete, and agent.continue()
        # cannot resume from an assistant message.
        recoverable_length = same_model and is_recoverable_length(
            assistant_message, getattr(self.model, "maxTokens", 0) or 0)
        if same_model and (is_context_overflow(assistant_message, context_window)
                           or recoverable_length):
            will_retry = assistant_message.stopReason != "stop"
            if not will_retry:
                return await self._run_auto_compaction("overflow", False)
            if self._overflow_recovery_attempted:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": "overflow",
                        "result": None,
                        "aborted": False,
                        "willRetry": False,
                        # Report truncation and overflow separately (pi #8130/c7c763f5c): labeling
                        # truncation as overflow would steer the user toward a larger-context
                        # model when the real cause is early-ended output.
                        "errorMessage": (
                            "Truncated-response recovery failed after one compact-and-retry "
                            "attempt. The provider repeatedly ended output early."
                            if recoverable_length
                            and not is_context_overflow(assistant_message, context_window)
                            else "Context overflow recovery failed after one compact-and-retry "
                                 "attempt. Try reducing context or switching to a "
                                 "larger-context model."
                        ),
                    }
                )
                return False

            self._overflow_recovery_attempted = True
            messages = self.agent.state.messages
            if messages and _message_role(messages[-1]) == "assistant":
                self.agent.state.messages = messages[:-1]
            return await self._run_auto_compaction("overflow", True)

        settings = CompactionSettings(**settings_data)
        direct_context_tokens = calculate_compaction_context_tokens(assistant_message.usage)
        # Without provider usage direct=0 and threshold compaction would never fire,
        # so fall back to a message-size estimate (pi #8328/4495469a5).
        if assistant_message.stopReason == "error" or direct_context_tokens == 0:
            estimate = estimate_compaction_context_tokens(list(self.agent.state.messages))
            # With no usage at all, estimate.tokens is a pure message-size estimate. Only a
            # usage-backed estimate needs the stale pre-compaction check: a kept old message's
            # usage reflects the large pre-compaction context and would re-trigger compaction
            # right after one finished.
            if estimate.lastUsageIndex is not None:
                usage_message = self.agent.state.messages[estimate.lastUsageIndex]
                usage_timestamp = _event_timestamp_ms(read_field(usage_message, "timestamp"))
                if (
                    latest_compaction is not None
                    and _message_role(usage_message) == "assistant"
                    and usage_timestamp > 0
                    and usage_timestamp <= latest_compaction_timestamp
                ):
                    return False
            context_tokens = estimate.tokens
        else:
            context_tokens = direct_context_tokens

        if should_compact(context_tokens, context_window, settings):
            return await self._run_auto_compaction("threshold", False)
        return False

    async def _run_auto_compaction(self, reason: str, will_retry: bool) -> bool:
        self._emit({"type": "compaction_start", "reason": reason})
        self._auto_compaction_abort_controller = AbortController()

        try:
            if self.model is None:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": reason,
                        "result": None,
                        "aborted": False,
                        "willRetry": False,
                    }
                )
                return False

            api_key: str | None = None
            headers: dict[str, str] | None = None
            if self.agent.streamFn == stream_simple:
                auth_result = await self._modelRegistry.getApiKeyAndHeaders(self.model)
                if not auth_result.get("ok") or not auth_result.get("apiKey"):
                    self._emit(
                        {
                            "type": "compaction_end",
                            "reason": reason,
                            "result": None,
                            "aborted": False,
                            "willRetry": False,
                        }
                    )
                    return False
                api_key = str(auth_result["apiKey"])
                headers = auth_result.get("headers")
            else:
                auth = await self._get_compaction_request_auth(self.model)
                api_key = auth.get("apiKey")
                headers = auth.get("headers")

            settings = CompactionSettings(**self.settingsManager.getCompactionSettings())
            branch_entries = self.sessionManager.getBranch()
            preparation = prepare_compaction(branch_entries, settings)
            if preparation is None:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": reason,
                        "result": None,
                        "aborted": False,
                        "willRetry": False,
                    }
                )
                return False

            hook_result = None
            from_hook = False
            if self._extensionRunner.has_handlers("session_before_compact"):
                hook_result = await self._extensionRunner.emit(
                    {
                        "type": "session_before_compact",
                        "preparation": preparation,
                        "branchEntries": branch_entries,
                        "customInstructions": None,
                        "signal": self._auto_compaction_abort_controller.signal,
                    }
                )
                if _result_flag(hook_result, "cancel", False):
                    self._emit(
                        {
                            "type": "compaction_end",
                            "reason": reason,
                            "result": None,
                            "aborted": True,
                            "willRetry": False,
                        }
                    )
                    await self._emit_session_compact_failed(
                        reason=reason, aborted=True, will_retry=False)
                    return False

            provided = _result_flag(hook_result, "compaction")
            if provided is not None:
                result = SessionCompactionResult(
                    summary=_event_field(provided, "summary"),
                    firstKeptEntryId=_event_field(provided, "firstKeptEntryId"),
                    tokensBefore=int(_event_field(provided, "tokensBefore", 0)),
                    details=_event_field(provided, "details"),
                )
                from_hook = True
            else:
                result = await run_compaction(
                    preparation,
                    self.model,
                    api_key,
                    headers,
                    None,
                    self._auto_compaction_abort_controller.signal,
                    self.thinkingLevel,
                    self.agent.streamFn,
                )

            if self._auto_compaction_abort_controller.signal.aborted:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": reason,
                        "result": None,
                        "aborted": True,
                        "willRetry": False,
                    }
                )
                await self._emit_session_compact_failed(
                    reason=reason, aborted=True, will_retry=False,
                    from_extension=from_hook)
                return False

            self.sessionManager.appendCompaction(
                result.summary,
                result.firstKeptEntryId,
                result.tokensBefore,
                result.details,
                from_hook,
            )
            self.agent.state.messages = self.sessionManager.buildSessionContext().messages

            saved_entry = next(
                (
                    entry
                    for entry in reversed(self.sessionManager.getEntries())
                    if entry.get("type") == "compaction" and entry.get("summary") == result.summary
                ),
                None,
            )
            if saved_entry is not None:
                await self._extensionRunner.emit(
                    {
                        "type": "session_compact",
                        "compactionEntry": saved_entry,
                        "fromExtension": from_hook,
                    }
                )

            self._emit(
                {
                    "type": "compaction_end",
                    "reason": reason,
                    "result": result,
                    "aborted": False,
                    "willRetry": will_retry,
                }
            )

            if will_retry:
                messages = self.agent.state.messages
                last_message = messages[-1] if messages else None
                if (_message_role(last_message) == "assistant"
                        and read_field(last_message, "stopReason") in ("error", "length")):
                    self.agent.state.messages = messages[:-1]   # drop the truncated tail too (#7540)
                return True

            return self.agent.hasQueuedMessages()
        except Exception as error:  # noqa: BLE001 - compaction failure is reported to the user, never fatal
            error_message = str(error) if str(error) else "compaction failed"
            formatted_error = (
                f"Context overflow recovery failed: {error_message}"
                if reason == "overflow"
                else f"Auto-compaction failed: {error_message}"
            )
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": reason,
                    "result": None,
                    "aborted": False,
                    "willRetry": False,
                    "errorMessage": formatted_error,
                }
            )
            await self._emit_session_compact_failed(
                reason=reason, aborted=False, will_retry=False,
                error_message=formatted_error)
            return False
        finally:
            self._auto_compaction_abort_controller = None

    def _spawn_background(self, awaitable: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(awaitable)
            return
        loop.create_task(awaitable)


def _definition_attr(definition: Any, name: str) -> Any:
    if isinstance(definition, dict):
        return definition.get(name)
    return getattr(definition, name, None)


def _event_type(event: Any) -> str:
    return str(_event_field(event, "type"))


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _event_timestamp_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _result_flag(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _message_role(message: Any) -> str | None:
    role = read_field(message, "role")
    return role if isinstance(role, str) else None


def _message_content(message: Any) -> Any:
    return read_field(message, "content")


def _content_type(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("type")
    else:
        value = getattr(block, "type", None)
    return value if isinstance(value, str) else None


def _message_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _message_dict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_message_dict(item) for item in value]
    return value


def _as_assistant_message(message: Any) -> AssistantMessage | None:
    if _message_role(message) != "assistant":
        return None
    if isinstance(message, AssistantMessage):
        return message
    try:
        validated = validate_message(_message_dict(message))
    except Exception:  # noqa: BLE001 - validation errors of any shape mean 'not an assistant message'
        return None
    return validated if isinstance(validated, AssistantMessage) else None


def _decorate_agent_end_event(event: Any, will_retry: bool) -> dict[str, Any]:
    return {
        "type": "agent_end",
        "messages": list(_event_field(event, "messages", []) or []),
        "willRetry": will_retry,
    }


async def _sleep_with_abort(delay_ms: int, signal: Any) -> bool:
    if bool(getattr(signal, "aborted", False)):
        return False
    if delay_ms <= 0:
        await asyncio.sleep(0)
        return not bool(getattr(signal, "aborted", False))
    try:
        await asyncio.wait_for(signal.wait(), timeout=delay_ms / 1000)
        return False
    except TimeoutError:
        return True


def _calculate_context_tokens(usage: dict[str, Any]) -> int:
    total_tokens = int(usage.get("totalTokens", 0) or 0)
    if total_tokens:
        return total_tokens
    return (
        int(usage.get("input", 0) or 0)
        + int(usage.get("output", 0) or 0)
        + int(usage.get("cacheRead", 0) or 0)
        + int(usage.get("cacheWrite", 0) or 0)
    )


__all__ = [
    "AgentSession",
    "AgentSessionConfig",
    "AgentSessionEvent",
    "AgentSessionEventListener",
    "ExtensionBindings",
    "ModelCycleResult",
    "ParsedSkillBlock",
    "PromptOptions",
    "SessionStats",
    "SessionTokenStats",
    "parse_skill_block",
]
