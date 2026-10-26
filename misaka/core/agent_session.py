"""Foundational coding-agent session abstraction."""

from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import logging
import os
import re
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, NoReturn, TypedDict

from misaka.agent.agent import Agent
from misaka.agent.agent_loop import RunToolCallOptions, runToolCall
from misaka.agent.types import (
    AgentContext,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentRequestUpdate,
    AgentState,
    AgentTool,
    AgentToolCallOutcome,
    AgentToolResult,
    AgentTurnContext,
    AgentTurnDecision,
    PrepareNextTurnContext,
    PrepareRequestContext,
    ThinkingLevel,
)
from misaka.ai.compat import resetApiProviders, streamSimple
from misaka.ai.models import (
    clampThinkingLevel,
    getSupportedThinkingLevels,
    modelsAreEqual,
)
from misaka.ai.session_resources import cleanupSessionResources
from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    validate_message,
)
from misaka.ai.utils.headers import providerHeadersToRecord
from misaka.ai.utils.overflow import isContextOverflow, isRecoverableLength
from misaka.ai.utils.retry import (
    RetryCallbacks,
    RetryPolicy,
    isRetryableAssistantError,
    retryDelayMs,
)
from misaka.ai.utils.transcript import (
    getCurrentSystemMessage,
    getCurrentTools,
    getToolStateChanges,
    toToolDeclaration,
)
from misaka.core.auth_guidance import (
    formatNoApiKeyFoundMessage,
    formatNoModelSelectedMessage,
)
from misaka.core.bash_executor import BashResult, executeBashWithOperations
from misaka.core.cache_warmer import CacheWarmer, CacheWarmingStatus
from misaka.core.compaction import (
    CompactionPreparation,
    CompactionSettings,
    calculateContextTokens,
    compact,
    estimateContextTokens,
    prepareCompaction,
    shouldCompact,
)
from misaka.core.compaction import (
    CompactionResult as SessionCompactionResult,
)
from misaka.core.compaction.branch_summarization import (
    GenerateBranchSummaryOptions,
    collectEntriesForBranchSummary,
    generateBranchSummary,
)
from misaka.core.compaction.compaction import (
    estimateProjectedContextTokens,
    estimateTokens,
)
from misaka.core.defaults import DEFAULT_THINKING_LEVEL
from misaka.core.export_html import exportSessionToHtml
from misaka.core.export_html.tool_renderer import (
    ToolHtmlRendererDeps,
    createToolHtmlRenderer,
)
from misaka.core.extensions.runner import (
    BeforeAgentStartTrigger,
    ExtensionRunner,
    emitSessionShutdownEvent,
)
from misaka.core.extensions.types import (
    ExecuteToolOptions,
    ExtensionCommandContextActions,
    ExtensionError,
    ExtensionErrorListener,
    ExtensionMode,
    ExtensionUIContext,
    RegisteredTool,
    ToolDefinition,
    ToolExposure,
    ToolInfo,
    ToolLoadout,
)
from misaka.core.extensions.wrapper import wrapRegisteredTool
from misaka.core.mcp_servers import createToolNameMatcher, isMcpToolName
from misaka.core.messages import BashExecutionMessage, convertToLlm
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_runtime import ModelRuntime, is_rejected_credential_error
from misaka.core.moments import CoreCommand, Moments
from misaka.core.nested_tool_calls import NestedToolCallOptions, NestedToolCallRunner
from misaka.core.prompt_templates import PromptTemplate, expandPromptTemplate
from misaka.core.resource_loader import ResourceLoaderLike
from misaka.core.session_export import exportSessionToJsonl
from misaka.core.session_manager import (
    SessionManager,
    SessionProjection,
    getLatestCompactionEntry,
)
from misaka.core.settings_manager import (
    DEFAULT_TOOL_NAMES,
    SettingsManager,
    applyToolModifiers,
)
from misaka.core.slash_commands import SlashCommandInfo, _make_slash_command_info
from misaka.core.source_info import (
    BUILTIN_PATH_PREFIX,
    SourceInfo,
    createSyntheticSourceInfo,
    isSyntheticPath,
)
from misaka.core.system_prompt import (
    buildSystemPrompt,
    buildSystemPromptSections,
    diffSystemPromptSections,
    normalizeBuildSystemPromptOptions,
)
from misaka.core.tools import createAllToolDefinitions
from misaka.core.tools.bash import createLocalBashOperations
from misaka.core.tools.tool_definition_wrapper import (
    createToolDefinitionFromAgentTool,
)
from misaka.core.usage_totals import addUsageToTotals, combineUsage, createUsageTotals
from misaka.core.virtual_models import (
    VIRTUAL_MODEL_STATE_ENTRY,
    VirtualModelDefinition,
    VirtualModelStateData,
    answersAsOtherModels,
    findLatestResponse,
    getBranchSelection,
    getVirtualModelState,
    isVirtualModel,
)
from misaka.modes.interactive.theme.theme import getThemeByName, theme
from misaka.utils._abort_signal import AnyAbortSignal
from misaka.utils.image_process import ProcessImageOptions, processImage
from misaka.utils.image_resize import ImageResizeOptions
from misaka.utils.js import AbortController
from misaka.utils.tool_result_images import (
    NormalizeToolResultImagesOptions,
    normalizeToolResultImages,
)
from misaka.utils.values import read_field

_SKILL_BLOCK_PATTERN = re.compile(
    r'^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n</skill>(?:\n\n([\s\S]+))?$'
)
_STALE_CONTEXT_MESSAGE = (
    "This extension ctx is stale after session replacement or reload. Do not use a captured pi or command ctx "
    "after ctx.newSession(), ctx.fork(), ctx.switchSession(), or ctx.reload(). For newSession, fork, and "
    "switchSession, move post-replacement work into withSession and use the ctx passed to withSession. For "
    "reload, do not use the old ctx after await ctx.reload()."
)
_THINKING_LEVELS: tuple[ThinkingLevel, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)

# Strong references to fire-and-forget session tasks; see `_holdBackgroundTask`.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


class _CompactionCancelledByExtension(RuntimeError):
    """MISAKA fork: pi's `cancelledByExtension` flag. The `session_before_compact` hook runs inside the
    operation `_prepareCompactionOperation` returns, so the cancellation reaches the caller as this type."""


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
    modelRuntime: ModelRuntime
    scopedModels: list[dict[str, Any]] = field(default_factory=list)
    customTools: list[Any] = field(default_factory=list)
    cacheWarmer: CacheWarmer | None = None
    # MISAKA fork: the session's own subsystems, called from core.moments ahead of extensions.
    parts: list[Any] = field(default_factory=list)
    initialActiveToolNames: list[str] | None = None
    # Whether the initial tools come from the `defaultTools` setting. When true, a resumed session
    # replaces them with the loadout its transcript records, with `defaultToolModifiers` applied, and
    # reload activates tools newly added to the setting. Tools removed from it stay active.
    usesDefaultTools: bool = False
    # `+name`/`-name` entries applied on top of the `defaultTools` setting, from `--tools`. Reload
    # applies them to the reloaded setting too, so a removed tool stays removed.
    defaultToolModifiers: list[str] | None = None
    # Optional allowlist of tool names or patterns (`*` matches any characters). When provided, only
    # matching tools are exposed. A non-empty list without `mcp__` entries also keeps MCP tools
    # registered for codemode and tool_search; only tool_search can declare them. An empty list
    # exposes no tools.
    allowedToolNames: list[str] | None = None
    baseToolsOverride: dict[str, AgentTool] | None = None
    extensionRunnerRef: dict[str, Any] | None = None
    sessionStartEvent: dict[str, Any] | None = None
    # Optional denylist of tool names or patterns. When provided, matching tools are not exposed.
    excludedToolNames: list[str] | None = None


@dataclass(slots=True)
class ExtensionBindings:
    uiContext: ExtensionUIContext | None = None
    # Which run mode is driving the session (pi agent-session.ts:234,2421-2423).
    mode: ExtensionMode | None = None
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


QueuedInputDisposition = Literal["handled", "queued"]
PromptDisposition = Literal["handled", "queued", "started"]


@dataclass(slots=True)
class PromptOptions:
    expandPromptTemplates: bool = True
    images: list[ImageContent] | None = None
    streamingBehavior: str | None = None
    source: str = "interactive"
    # Observes how an accepted prompt was dispatched. Not called if the prompt is rejected.
    preflightResult: Callable[[PromptDisposition], None] | None = None


class ModelMutationOptions(TypedDict, total=False):
    # Persist the new value to global defaults. Defaults to session-only.
    persist: bool


# Result from cycleModel()
@dataclass(slots=True)
class ModelCycleResult:
    model: Model[Any]
    thinkingLevel: ThinkingLevel
    isScoped: bool


@dataclass(frozen=True, slots=True)
class RoutedModel:
    """Physical model and thinking level of the latest successful response under a virtual selection."""

    model: Model[Any]
    thinkingLevel: ThinkingLevel | None = None


@dataclass(slots=True)
class _ToolDefinitionEntry:
    definition: Any
    sourceInfo: SourceInfo
    promptSnippet: str | None = None
    promptGuidelines: list[str] = field(default_factory=list)


def parseSkillBlock(text: str) -> ParsedSkillBlock | None:
    match = _SKILL_BLOCK_PATTERN.match(text)
    if match:
        return ParsedSkillBlock(
            name=match.group(1),
            location=match.group(2),
            content=match.group(3),
            userMessage=match.group(4).strip() if match.group(4) else None,
        )

    # MISAKA keeps Pi's historical transcript renderer, while /skill now emits
    # Hermes' activation scaffold. Parse both so old and new sessions collapse.
    from misaka.core.skills.wiring.skills import parse_skill_invocation_message

    parsed = parse_skill_invocation_message(text)
    if parsed is None:
        return None
    return ParsedSkillBlock(
        name=parsed["name"],
        location=parsed["location"],
        content=parsed["content"],
        userMessage=parsed["user_instruction"],
    )


class AgentSession:
    # MISAKA fork: a session built without __init__ (test doubles) has no parts.
    moments: Moments = Moments(None, [])
    # Class-level defaults for doubles built without __init__, as `moments`.
    _nestedToolCalls: NestedToolCallRunner | None = None
    _hiddenDeclarations: frozenset[str] = frozenset()

    def __init__(self, config: AgentSessionConfig | dict[str, Any]) -> None:
        resolved = config if isinstance(config, AgentSessionConfig) else AgentSessionConfig(**config)
        self.agent = resolved.agent
        self.sessionManager = resolved.sessionManager
        self.settingsManager = resolved.settingsManager
        self._cwd = resolved.cwd
        self._resourceLoader = resolved.resourceLoader
        self._modelRuntime = resolved.modelRuntime
        self._modelRegistry: ModelRegistry | None = None
        self._scopedModels = list(resolved.scopedModels)
        self._customTools = list(resolved.customTools)
        self._cacheWarmer = resolved.cacheWarmer
        if self._cacheWarmer is not None:
            self._cacheWarmer.onWarmed = lambda entry: self._emit({"type": "entry_appended", "entry": entry})
        self.moments = Moments(self, list(resolved.parts))  # MISAKA fork
        self._initialActiveToolNames = (
            list(resolved.initialActiveToolNames) if resolved.initialActiveToolNames is not None else None
        )
        # Tools of the restored or reloaded loadout that are not registered yet, such as tools of MCP
        # servers that are still connecting. They are activated when they are registered, and dropped when
        # `setActiveToolsByName()` deactivates a tool or the next agent run starts.
        # Insertion-ordered, like pi's `Set`: restored tools are redeclared in transcript order.
        self._pendingToolNames: dict[str, None] = {}
        self._usesDefaultTools = bool(resolved.usesDefaultTools)
        self._defaultToolModifiers: list[str] = list(resolved.defaultToolModifiers or [])
        # Matches the `--tools` entries: tool names or patterns.
        self._allowedTools: Callable[[str], bool] | None = None
        # Whether the allowlist filters MCP tools: it is empty (`--no-tools`) or names an MCP tool
        # (`mcp__*`). Otherwise it keeps MCP tools registered for codemode and tool_search.
        self._allowlistFiltersMcp = False
        if resolved.allowedToolNames is not None:
            self._allowedTools = createToolNameMatcher(resolved.allowedToolNames)
            self._allowlistFiltersMcp = len(resolved.allowedToolNames) == 0 or any(
                entry.startswith("mcp__") for entry in resolved.allowedToolNames
            )
        self._excludedTools: Callable[[str], bool] | None = (
            createToolNameMatcher(resolved.excludedToolNames) if resolved.excludedToolNames is not None else None
        )
        self._disallowedToolNames: set[str] = set()
        self._toolAdmission: Callable[[str], bool] | None = None
        self._alwaysAllowedToolNames: set[str] = set()
        self._baseToolsOverride = (
            dict(resolved.baseToolsOverride) if resolved.baseToolsOverride is not None else None
        )
        self._extensionRunnerRef = resolved.extensionRunnerRef
        self._sessionStartEvent = resolved.sessionStartEvent or {"type": "session_start", "reason": "startup"}

        self._eventListeners: list[AgentSessionEventListener] = []
        self._unsubscribeAgent = self.agent.subscribe(self._handleAgentEvent)
        self._extensionErrorUnsubscriber: Callable[[], None] | None = None
        self._extensionAbortHandler: Callable[[], None] | None = None
        self._extensionShutdownHandler: Callable[[], None] | None = None
        self._extensionBindings: ExtensionBindings | None = None
        self._extensionUIContext: ExtensionUIContext | None = None
        self._extensionMode: ExtensionMode = "print"
        self._extensionCommandContextActions: ExtensionCommandContextActions | None = None
        self._extensionErrorListener: ExtensionErrorListener | None = None
        self._steeringMessages: list[str] = []
        self._followUpMessages: list[str] = []
        self._pendingNextTurnMessages: list[dict[str, Any]] = []
        self._pendingBashMessages: list[BashExecutionMessage] = []
        self._pendingCustomMessages: list[dict[str, Any]] = []
        self._customMessageReceipts: dict[str, dict[str, Any]] = {}
        self._bashAbortControllers: set[AbortController] = set()
        self._auto_compaction_abort_controller: AbortController | None = None
        self._compactionAbortController: AbortController | None = None
        self._branchSummaryAbortController: AbortController | None = None
        self._overflow_recovery_attempted = False
        # True for the whole run driven by _startRun, not just the inner agent loop:
        # retries, auto-compaction and every continue_() between them are still "busy"
        # (pi agent-session.ts _isAgentRunActive).
        self._isAgentRunActive = False
        self._agentRunAbortRequested = False
        self._idleWaiters: list[asyncio.Future[None]] = []
        self._retryAbortController: AbortController | None = None
        self._retryAttempt = 0
        # Failed response that the next request repeats, set by auto-retry and overflow recovery. The
        # retry is routed with it as `failed`, since the context no longer contains it.
        self._failedResponse: AssistantMessage | None = None
        # MISAKA fork: the model the current request goes to, routed or not. A context engine sizes
        # itself from it at the provider boundary; under a virtual selection `self.model` has no
        # window of its own.
        self._requestModel: Model[Any] | None = None
        self._turnIndex = 0
        # pi keys these by message object in a WeakMap; pydantic messages are neither
        # hashable nor weak-referenceable, so they are keyed by `id()` and the map is rebuilt
        # from the projection on every refresh, which is where pi's entries would be collected.
        self._entryIdsByMessage: dict[int, tuple[Any, str]] = {}
        self._boundaryDispatchedMessages: set[int] = set()
        self._lastAssistantMessage: AssistantMessage | None = None
        self._lastAssistantToolResults: list[Any] = []
        self._lastActivityOutcome = "completed"
        self._isBeforeSettle = False
        self._abortDuringBeforeSettle = False
        self._isEmittingAgentSettled = False
        self._deferredSettledActions: list[Callable[[], Awaitable[None]]] = []
        self._stopHookContinuationPending = False
        self._baseToolDefinitions: dict[str, Any] = {}
        self._toolRegistry: dict[str, AgentTool] = {}
        self._toolDefinitions: dict[str, _ToolDefinitionEntry] = {}
        # Runs the calls tools make through ctx.executeTool(); created on the first one.
        self._nestedToolCalls = None
        # Declared tools whose declarations prepareLoadout hooks hide from requests.
        self._hiddenDeclarations = frozenset()
        self._baseSystemPromptOptions: dict[str, Any] = normalizeBuildSystemPromptOptions({"cwd": self._cwd})
        # Prompt options after before_agent_start mutations for the active run.
        self._runSystemPromptOptions: dict[str, Any] | None = None
        self._toolScopes: list[dict[str, Any]] = []
        self._unscopedToolNames: list[str] = []
        # MISAKA fork: the last tool scope ended while a run was working; see `toolScope`.
        self._recordLoadoutAfterRun = False

        self._installAgentToolHooks()
        self._installAgentNextTurnRefresh()
        self._installAgentRequestProjection()
        self._installAgentBoundaryHooks()
        self._installHiddenDeclarationsProjection()
        self._installAgentForcedPromptProjection()
        self._buildRuntime(
            {
                "activeToolNames": self._initialActiveToolNames,
                "includeAllExtensionTools": True,
            }
        )
        if self._usesDefaultTools:
            self._restoreToolsFromTranscript(self._defaultToolModifiers)

    @property
    def extensionRunner(self) -> ExtensionRunner:
        return self._extensionRunner

    @property
    def modelRuntime(self) -> ModelRuntime:
        return self._modelRuntime

    @property
    def modelRegistry(self) -> ModelRegistry:
        """MISAKA fork: the extension-facing facade over ``modelRuntime``, one per session and
        shared with the ExtensionRunner, for code that reads the registry (subagents)."""
        if self._modelRegistry is None:
            self._modelRegistry = ModelRegistry(self._modelRuntime)
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
    def routedModel(self) -> RoutedModel | None:
        """Under a virtual selection, the physical model and thinking level of the latest successful response."""
        # MISAKA fork: an MoA preset reports its aggregator the same way.
        if self.model is None or not answersAsOtherModels(self.model):
            return None
        latest = findLatestResponse(self.agent.state.messages)
        model = (
            self._modelRuntime.getPhysicalModel(read_field(latest, "provider"), read_field(latest, "model"))
            if latest is not None
            else None
        )
        return RoutedModel(model=model, thinkingLevel=read_field(latest, "thinkingLevel")) if model is not None else None

    @property
    def isStreaming(self) -> bool:
        """Whether the session is processing an agent run or a post-run continuation.

        This is the run-level flag, not ``agent.state.isStreaming``: the inner loop goes
        idle between two ``continue_()`` calls, during retry backoff and during
        auto-compaction, and pi reports busy for all of those (agent-session.ts:900-908).
        """
        return self._isAgentRunActive

    @property
    def isIdle(self) -> bool:
        """No active agent run, retry, auto-compaction or queued continuation."""
        return not self._isAgentRunActive

    async def waitForIdle(self) -> None:
        if self.isIdle:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._idleWaiters.append(waiter)
        await waiter

    def _modelResizeOptions(self) -> ImageResizeOptions | None:
        """The limits model's `inputLimits.images.resize`, as the resizer's own options."""
        # `model.inputLimits?.images?.resize`, whether the limits are models or plain mappings.
        resize = read_field(read_field(read_field(self._limitsModel(), "inputLimits"), "images"), "resize")
        if resize is None:
            return None
        return ImageResizeOptions(
            maxWidth=read_field(resize, "maxWidth"),
            maxHeight=read_field(resize, "maxHeight"),
            maxBytes=read_field(resize, "maxBytes"),
            jpegQuality=read_field(resize, "jpegQuality"),
        )

    async def _normalizePromptImages(self, images: Sequence[ImageContent] | None) -> tuple[list[ImageContent], list[str]]:
        """Resize prompt attachments to the model's cache-safe profile; a failed image becomes a hint."""
        if not images:
            return [], []
        normalized_images: list[ImageContent] = []
        hints: list[str] = []
        for image in images:
            # Images arrive as models from the TUI and as plain mappings from RPC/skill callers.
            data = read_field(image, "data")
            mime_type = read_field(image, "mimeType")
            processed = await processImage(
                base64.b64decode(data),
                mime_type,
                ProcessImageOptions(
                    autoResizeImages=self.settingsManager.getImageAutoResize(),
                    resizeOptions=self._modelResizeOptions(),
                ),
            )
            if not processed.ok:
                hints.append(processed.message)
                continue
            normalized_images.append(ImageContent(data=processed.data, mimeType=processed.mimeType))
            hints.extend(processed.hints)
        return normalized_images, hints

    def refreshContext(self) -> None:
        """Refresh the public finalized transcript from the canonical session projection."""
        self._refreshFinalizedContext()

    @property
    def cacheWarmingStatus(self) -> CacheWarmingStatus | None:
        """Current cache-warming state and the policy inputs that produced it."""
        return self._cacheWarmer.status if self._cacheWarmer is not None else None

    def setCacheWarmingMode(self, mode: str) -> None:
        """Persist the cache-warming mode and immediately reconcile active warming."""
        self.settingsManager.setCacheWarmingMode(mode)
        if self._cacheWarmer is not None:
            self._cacheWarmer.onModeChanged()

    @property
    def systemPrompt(self) -> str:
        """Current effective system prompt, including changes not yet sent to the model."""
        return buildSystemPrompt(
            self._runSystemPromptOptions if self._runSystemPromptOptions is not None else self._baseSystemPromptOptions
        )

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
        return len(self._bashAbortControllers) > 0

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
        try:
            self.abortRetry()
            self.abortCompaction()
            self.abortBranchSummary()
            self.abortBash()
            self.agent.abort()
        except Exception:  # noqa: BLE001,S110 - Pi disposal ignores abort-hook failures
            pass

        for delivery_id in list(self._customMessageReceipts):
            self._failCustomReceipt(delivery_id, RuntimeError("session closed before persistence"))
        self._extensionRunner.invalidate(_STALE_CONTEXT_MESSAGE)
        self._disconnectFromAgent()
        self._eventListeners = []
        if self._cacheWarmer is not None:
            self._cacheWarmer.onWarmed = None
            self._cacheWarmer.cancel()
        cleanupSessionResources(self.sessionId)

    def _disconnectFromAgent(self) -> None:
        if self._unsubscribeAgent is not None:
            self._unsubscribeAgent()
            self._unsubscribeAgent = None

    async def abort(self) -> None:
        """Abort current operation and wait for agent to become idle."""
        if self._isAgentRunActive:
            self._agentRunAbortRequested = True
        self.abortRetry()
        self.abortCompaction()
        self.abortBranchSummary()
        if self._isBeforeSettle:
            self._abortDuringBeforeSettle = True
        self.agent.abort()
        # Wait for the whole run to settle, not just the inner agent loop: pi
        # (agent-session.ts:1599-1603) returns only once the post-run continuation,
        # retry and auto-compaction windows are done too.
        await self.waitForIdle()

    async def _getRequiredRequestAuth(self, model: Model[Any], signal: Any | None = None) -> dict[str, Any]:
        try:
            result = await self._modelRuntime.getAuth(model, _auth_overrides(signal))
        except Exception as error:
            cause = error.__cause__
            if isinstance(cause, Exception) and str(cause) == "authHeader requires a resolved API key":
                raise RuntimeError(
                    formatNoApiKeyFoundMessage(model.provider)
                ) from error
            raise

        if result and (result.auth.apiKey or result.auth.headers is not None):
            request_model = (
                model.model_copy(update={"baseUrl": result.auth.baseUrl})
                if result.auth.baseUrl
                else model
            )
            return {
                "model": request_model,
                "apiKey": result.auth.apiKey,
                "headers": providerHeadersToRecord(result.auth.headers),
                "env": result.env,
            }

        if self._modelRuntime.isUsingOAuth(model.provider):
            raise RuntimeError(
                f'Authentication failed for "{model.provider}". '
                "Credentials may have expired or network is unavailable. "
                f"Run '/login {model.provider}' to re-authenticate."
            )
        raise RuntimeError(formatNoApiKeyFoundMessage(model.provider))

    async def _getSummarizationRequestAuth(
        self, selected_model: Model[Any], signal: Any | None = None
    ) -> dict[str, Any]:
        # Route a virtual model first: summaries size their input and output from the model they get.
        if isVirtualModel(selected_model):
            route = await self._modelRuntime.resolveModel(selected_model, convertToLlm(self.messages), {"reason": "direct", "thinkingLevel": self.thinkingLevel, "signal": signal})
            model, thinking_level = route.model, route.thinkingLevel
        else:
            model, thinking_level = selected_model, self.thinkingLevel
        if self.agent.streamFunction == streamSimple:
            return {**await self._getRequiredRequestAuth(model, signal), "thinkingLevel": thinking_level}

        try:
            result = await self._modelRuntime.getAuth(model, _auth_overrides(signal))
            if result is None:
                return {"model": model, "thinkingLevel": thinking_level}
            request_model = (
                model.model_copy(update={"baseUrl": result.auth.baseUrl})
                if result.auth.baseUrl
                else model
            )
            return {
                "model": request_model,
                "apiKey": result.auth.apiKey,
                "headers": providerHeadersToRecord(result.auth.headers),
                "env": result.env,
                "thinkingLevel": thinking_level,
            }
        except Exception:  # custom streams may own credentials outside the registry
            if signal is not None and signal.aborted:
                raise
            return {"model": model, "thinkingLevel": thinking_level}

    def _modelForMessage(self, message: AssistantMessage) -> Model[Any] | None:
        """The model whose limits apply to `message`, or None when the message came from another
        model. Under a virtual selection, that is the physical model that produced it."""
        model = self.model
        # MISAKA fork: under an MoA preset the aggregator produced the message.
        if model is not None and answersAsOtherModels(model):
            return self._modelRuntime.getPhysicalModel(message.provider, message.model)
        return model if model is not None and model.provider == message.provider and model.id == message.model else None

    def _recordSelection(self) -> None:
        """Record the selection on the current branch when the branch implies another one, so a resume
        restores it. Tree navigation can leave the latest `model_change` on another branch; responses
        cannot record a virtual selection because they name physical models. Responses do record a
        physical selection unless the branch holds a virtual one; checking a physical selection against
        responses would record it on every prompt while `prepareRequest` redirects to another model."""
        model = self.model
        if model is None:
            return
        get_model = self._modelRuntime.getModel
        recorded = getBranchSelection(self.sessionManager.getBranch(), get_model)
        if recorded is None or (recorded["provider"] == model.provider and recorded["modelId"] == model.id):
            return
        recorded_model = get_model(recorded["provider"], recorded["modelId"])
        # MISAKA fork: MoA presets are recorded like virtual models while they are in the catalog. One
        # that left it still answers through an aggregator, so recording it would repeat every prompt.
        holds = isVirtualModel(model) or (answersAsOtherModels(model) and get_model(model.provider, model.id) is not None)
        if not holds and not (recorded_model is not None and answersAsOtherModels(recorded_model)):
            return
        self.sessionManager.appendModelChange(model.provider, model.id)

    def _limitsModel(self) -> Model[Any] | None:
        """The model whose limits apply to the conversation."""
        routed = self.routedModel
        return routed.model if routed is not None else self.model

    async def prompt(
        self,
        text: str,
        options: PromptOptions | dict[str, Any] | None = None,
    ) -> None:
        if self._isEmittingAgentSettled:
            async def deferred_prompt() -> None:
                await self.prompt(text, options)

            self._deferredSettledActions.append(deferred_prompt)
            return
        resolved = options if isinstance(options, PromptOptions) else PromptOptions(**dict(options or {}))

        def report_preflight(disposition: PromptDisposition) -> None:
            if resolved.preflightResult is not None:
                resolved.preflightResult(disposition)

        def assert_not_compacting() -> None:
            if self._compactionAbortController is not None:
                raise RuntimeError(
                    "Cannot submit a prompt while compaction is in progress. "
                    "Wait for compaction to finish and retry."
                )

        # MISAKA fork: skill commands prepare text for this same submission.
        # Keep its images, delivery mode, preflight and cancellation ownership.
        prompt_command = self.getCorePromptCommand(text) if resolved.expandPromptTemplates else None
        if (
            resolved.expandPromptTemplates
            and prompt_command is None
            and text.startswith("/")
            and (
                await self._tryExecuteCoreCommand(text)  # MISAKA fork: a part's command first
                or await self._tryExecuteExtensionCommand(text)
            )
        ):
            # Extension command executed, no prompt to send
            report_preflight("handled")
            return

        # Compaction replaces agent.state.messages wholesale, so a prompt accepted while
        # it runs would append into a list that is about to be thrown away.  pi refuses
        # instead of racing (agent-session.ts:1155-1159).  The extension-command branch
        # above stays reachable, exactly as in pi.
        assert_not_compacting()

        current_text = await self._expandCorePromptCommand(prompt_command, text) if prompt_command else text
        if current_text is None:
            report_preflight("handled")
            return
        if prompt_command is not None:
            assert_not_compacting()
        # Generated command messages retain sendUserMessage's input origin:
        # /research's pending-question capture must not consume a skill scaffold.
        input_source = "extension" if prompt_command else resolved.source
        current_images = None if resolved.images is None else list(resolved.images)
        processed_input = await self._runInputHandlers(
            current_text,
            current_images,
            input_source,
            resolved.streamingBehavior if self.isStreaming else None,
        )
        if processed_input is None:
            report_preflight("handled")
            return
        current_text, current_images = processed_input

        if resolved.expandPromptTemplates and prompt_command is None:
            current_text = expandPromptTemplate(current_text, self.promptTemplates)

        if self.isStreaming:
            if resolved.streamingBehavior == "followUp":
                await self._queueFollowUp(current_text, current_images)
                report_preflight("queued")
                return
            if resolved.streamingBehavior == "steer":
                await self._queueSteer(current_text, current_images)
                report_preflight("queued")
                return
            raise RuntimeError(
                "Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the "
                "message."
            )

        if self.model is None:
            raise RuntimeError(formatNoModelSelectedMessage())
        has_configured_auth = (
            self._modelRuntime.hasConfiguredAuth(self.model.provider)
            or await self._modelRuntime.checkAuth(self.model.provider) is not None
        )
        if not has_configured_auth:
            if self._modelRuntime.isUsingOAuth(self.model.provider):
                raise RuntimeError(
                    f'Authentication failed for "{self.model.provider}". '
                    "Credentials may have expired or network is unavailable. "
                    f"Run '/login {self.model.provider}' to re-authenticate."
                )
            raise RuntimeError(formatNoApiKeyFoundMessage(self.model.provider))

        await self._startRun(
            {"messageType": "user", "prompt": current_text, "images": current_images},
            lambda: report_preflight("started"),
            # MISAKA fork: a core prompt command's text must not land in a conversation that a
            # compaction started during the hooks is about to replace.
            admit=assert_not_compacting if prompt_command is not None else None,
        )

    async def _emitBeforeAgentStart(self, trigger: BeforeAgentStartTrigger):
        """The before_agent_start hooks: the messages they contribute and the run's prompt options."""
        selected_tools_before = list(self._baseSystemPromptOptions["selectedTools"])
        # MISAKA fork: the session's own parts run first; an extension sees the prompt
        # options and the messages as core left them.
        core = await self.moments.before_agent_start(trigger, self._baseSystemPromptOptions)
        if core.block:
            _raiseRunBlocked(trigger, core.reason or "a core part blocked the turn")
        contributed: list[Any] = list(core.messages)
        options = core.system_prompt_options
        if self._extensionRunner.hasHandlers("before_agent_start"):
            before_result = await self._extensionRunner.emitBeforeAgentStart(trigger, options)
            if before_result and _event_field(before_result, "block", False):
                _raiseRunBlocked(
                    trigger,
                    str(_event_field(before_result, "reason", "before_agent_start hook blocked the turn")),
                )
            contributed.extend(_event_field(before_result, "messages") or [])
            options = _event_field(before_result, "systemPromptOptions", options)
        # Handlers may edit event.systemPromptOptions.selectedTools or call setActiveTools(),
        # which updates the live loadout instead. An explicit edit wins; otherwise the live
        # loadout is authoritative, so a setActiveTools() call is not undone here.
        handler_edited_tools = list(options["selectedTools"]) != selected_tools_before
        if not handler_edited_tools:
            options["selectedTools"] = self.getActiveToolNames()
        return contributed, options

    async def _runInputHandlers(
        self,
        text: str,
        images: list[ImageContent] | None,
        source: Any,
        streaming_behavior: Any,
    ) -> tuple[str, list[ImageContent] | None] | None:
        """Parts first, then extensions (MISAKA fork: pi has only the runner). None when a
        handler took the input."""
        input_result = await self.moments.input(text, images, source, streaming_behavior)
        action = _event_field(input_result, "action", "continue")
        if action == "handled":
            return None
        if action == "transform":
            text = _event_field(input_result, "text", text)
            transformed_images = _event_field(input_result, "images", None)
            if transformed_images is not None:
                images = list(transformed_images)
        if not self._extensionRunner.hasHandlers("input"):
            return text, images
        input_result = await self._extensionRunner.emitInput(text, images, source, streaming_behavior)
        action = _event_field(input_result, "action", "continue")
        if action == "handled":
            return None
        if action == "transform":
            text = _event_field(input_result, "text", text)
            transformed_images = _event_field(input_result, "images", None)
            if transformed_images is not None:
                images = list(transformed_images)
        return text, images

    async def _queueUserInput(
        self, text: str, images: Sequence[ImageContent] | None, behavior: str, source: Any
    ) -> QueuedInputDisposition:
        command = self.getCorePromptCommand(text)  # MISAKA fork: a part's prompt command first
        if command is not None:
            expanded = await self._expandCorePromptCommand(command, text)
            if expanded is None:
                return "handled"
            text = expanded
        elif text.startswith("/"):
            self._throwIfExtensionCommand(text)
        processed_input = await self._runInputHandlers(
            text, None if images is None else list(images), source, behavior if self.isStreaming else None
        )
        if processed_input is None:
            return "handled"
        expanded_text, processed_images = processed_input
        if command is None:
            expanded_text = expandPromptTemplate(expanded_text, self.promptTemplates)
        if behavior == "steer":
            await self._queueSteer(expanded_text, processed_images)
        else:
            await self._queueFollowUp(expanded_text, processed_images)
        return "queued"

    async def steer(
        self, text: str, images: Sequence[ImageContent] | None = None, options: dict[str, Any] | None = None
    ) -> QueuedInputDisposition:
        """Queue a steering message while the agent is running. Delivered after the current
        assistant turn finishes executing its tool calls, before the next LLM call. Runs the
        `input` handlers, expands skill commands and prompt templates; errors on extension commands."""
        return await self._queueUserInput(text, images, "steer", (options or {}).get("source", "interactive"))

    async def followUp(
        self, text: str, images: Sequence[ImageContent] | None = None, options: dict[str, Any] | None = None
    ) -> QueuedInputDisposition:
        """Queue a follow-up message to be processed after the agent finishes. Delivered only
        when agent has no more tool calls or steering messages. Runs the `input` handlers,
        expands skill commands and prompt templates; errors on extension commands."""
        return await self._queueUserInput(text, images, "followUp", (options or {}).get("source", "interactive"))

    def _emitQueueUpdate(self) -> None:
        self._emit(
            {
                "type": "queue_update",
                "steering": list(self._steeringMessages),
                "followUp": list(self._followUpMessages),
            }
        )

    async def _queueSteer(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        self._steeringMessages.append(text)
        self._emitQueueUpdate()
        self.agent.steer(self._buildUserMessage(text, images))

    async def _queueFollowUp(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        self._followUpMessages.append(text)
        self._emitQueueUpdate()
        self.agent.followUp(self._buildUserMessage(text, images))

    def _throwIfExtensionCommand(self, text: str) -> None:
        command_text = text.removeprefix("/")
        command_name = command_text.split(" ", 1)[0]
        if self._extensionRunner.getCommand(command_name) is not None:
            raise RuntimeError(
                f'Extension command "/{command_name}" cannot be queued. '
                "Use prompt() or execute the command when not streaming."
            )

    def setSessionName(self, name: str) -> None:
        self.sessionManager.appendSessionInfo(name)
        event = {"type": "session_info_changed", "name": self.sessionManager.getSessionName()}
        self._emit(event)
        # pi agent-session.ts:3063-3068 sends the same event to the extension runner as well;
        # only the UI half was ported, so `session_info_changed` handlers never ran.
        self._spawnBackground(self._extensionRunner.emit(event), "session_info_changed")

    def clearQueue(self) -> dict[str, list[str]]:
        steering = list(self._steeringMessages)
        follow_up = list(self._followUpMessages)
        self._steeringMessages = []
        self._followUpMessages = []
        self.agent.clearAllQueues()
        retained = {(message.get("details") or {}).get("delivery_id")
                    for message in [*self._pendingCustomMessages, *self._pendingNextTurnMessages]
                    if isinstance(message.get("details"), dict)}
        for delivery_id in set(self._customMessageReceipts) - retained:
            self._failCustomReceipt(delivery_id, RuntimeError("message queue cleared before persistence"))
        self._emitQueueUpdate()
        return {"steering": steering, "followUp": follow_up}

    def getSteeringMessages(self) -> list[str]:
        return list(self._steeringMessages)

    def getFollowUpMessages(self) -> list[str]:
        return list(self._followUpMessages)

    async def _emitModelSelect(
        self,
        nextModel: Model[Any],
        previousModel: Model[Any] | None,
        source: Literal["set", "cycle", "restore"],
    ) -> None:
        if modelsAreEqual(previousModel, nextModel):
            return
        await self._extensionRunner.emit(
            {
                "type": "model_select",
                "model": nextModel,
                "previousModel": previousModel,
                "source": source,
            }
        )

    async def setModel(self, model: Model[Any], options: ModelMutationOptions | None = None) -> None:
        """Set model directly.
        Validates that auth is configured and saves to the session transcript.
        Persists to global defaults only when options.persist is true.
        MISAKA fork: SettingsManager saves to this session's role when bound, otherwise to the global default.
        @throws Error if no auth is configured for the model
        """
        options = options or {}
        if not await self._modelRuntime.checkAuth(model.provider):
            raise RuntimeError(f"No API key for {model.provider}/{model.id}")

        previousModel = self.model
        thinkingLevel = self._getThinkingLevelForModelSwitch(model)
        # MISAKA fork: the settings write goes first; a role whose default cannot be saved here refuses it and the
        # session keeps its model.
        if options.get("persist"):
            self.settingsManager.setDefaultModelAndProvider(model.provider, model.id)
        self.agent.state.model = model
        self.sessionManager.appendModelChange(model.provider, model.id)
        if options.get("persist"):
            self._addPersistedDefaultToNonEmptyScope(model)

        # Apply thinking level for the new model.
        # Per-model thinking level overrides take priority over the global default.
        # Model persistence does not implicitly rewrite the global thinking default.
        self.setThinkingLevel(thinkingLevel)

        await self._emitModelSelect(model, previousModel, "set")

    def _addPersistedDefaultToNonEmptyScope(self, model: Model[Any]) -> None:
        if not self._scopedModels:
            return
        if any(modelsAreEqual(item["model"], model) for item in self._scopedModels):
            return

        self._scopedModels = [*self._scopedModels, {"model": model}]
        if getattr(self.settingsManager, "getModelProfile", lambda: None)():
            return

        enabled_models = self.settingsManager.getEnabledModels()
        if not enabled_models:
            return
        model_reference = f"{model.provider}/{model.id}"
        if any(pattern.lower() == model_reference.lower() for pattern in enabled_models):
            return
        self.settingsManager.setEnabledModels([*enabled_models, model_reference])

    def setThinkingLevel(self, level: ThinkingLevel, options: ModelMutationOptions | None = None) -> None:
        """Set thinking level.
        Clamps to model capabilities based on available thinking levels.
        Saves the clamped level to the session transcript only if the level actually changes.
        Persists the requested level to global defaults only when options.persist is true.
        """
        options = options or {}
        model = self.model
        clamped = level if model is None else clampThinkingLevel(model, level)
        previous_level = self.agent.state.thinkingLevel
        self.agent.state.thinkingLevel = clamped
        if options.get("persist"):
            self.settingsManager.setDefaultThinkingLevel(level)
        if clamped == previous_level:
            return
        self.sessionManager.appendThinkingLevelChange(clamped)
        self._emit({"type": "thinking_level_changed", "level": clamped})
        if self._extensionRunner.hasHandlers("thinking_level_select"):
            async def emit_change() -> None:
                await self._extensionRunner.emit(
                    {
                        "type": "thinking_level_select",
                        "level": clamped,
                        "previousLevel": previous_level,
                    }
                )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass  # no loop to deliver on; the UI half of the event already went out
            else:
                self._holdBackgroundTask(loop.create_task(emit_change()), "thinking_level_select")

    def getAvailableThinkingLevels(self) -> list[ThinkingLevel]:
        if self.model is None:
            return list(_THINKING_LEVELS)
        return list(getSupportedThinkingLevels(self.model))

    def supportsThinking(self) -> bool:
        return bool(self.model and self.model.reasoning)

    def cycleThinkingLevel(self, options: ModelMutationOptions | None = None) -> ThinkingLevel | None:
        """Cycle to next thinking level.
        @returns New level, or undefined if model doesn't support thinking
        """
        if not self.supportsThinking():
            return None

        levels = self.getAvailableThinkingLevels()
        # `indexOf` gives -1 for a level the model lacks, so the cycle starts at the first level.
        currentIndex = levels.index(self.thinkingLevel) if self.thinkingLevel in levels else -1
        nextIndex = (currentIndex + 1) % len(levels)
        nextLevel = levels[nextIndex]

        self.setThinkingLevel(nextLevel, options)
        return nextLevel

    async def cycleModel(
        self, direction: Literal["forward", "backward"] = "forward", options: ModelMutationOptions | None = None
    ) -> ModelCycleResult | None:
        """Cycle to next/previous model.
        Uses scoped models (from --models flag) if available, otherwise all available models.
        @param direction - "forward" (default) or "backward"
        @returns The new model info, or undefined if only one model available
        """
        options = options or {}
        if self._scopedModels:
            return await self._cycleScopedModel(direction, options)
        return await self._cycleAvailableModel(direction, options)

    async def _cycleScopedModel(
        self, direction: Literal["forward", "backward"], options: ModelMutationOptions
    ) -> ModelCycleResult | None:
        available_ids = {(model.provider, model.id) for model in self._modelRuntime.getAvailableSnapshot()}
        scoped_models = [
            item for item in self._scopedModels if (item["model"].provider, item["model"].id) in available_ids
        ]
        if len(scoped_models) <= 1:
            return None
        current_model = self.model
        current_index = next(
            (index for index, item in enumerate(scoped_models) if modelsAreEqual(item["model"], current_model)),
            -1,
        )
        current_index = max(current_index, 0)
        next_index = (current_index + (1 if direction == "forward" else -1)) % len(scoped_models)
        next_model = scoped_models[next_index]["model"]
        thinking_level = self._getThinkingLevelForModelSwitch(
            next_model,
            scoped_models[next_index].get("thinkingLevel"),
        )

        # MISAKA fork: the settings write goes first; a role whose default cannot be saved here refuses it and the
        # session keeps its model.
        if options.get("persist"):
            self.settingsManager.setDefaultModelAndProvider(next_model.provider, next_model.id)
        # Apply model
        self.agent.state.model = next_model
        self.sessionManager.appendModelChange(next_model.provider, next_model.id)
        if options.get("persist"):
            self._addPersistedDefaultToNonEmptyScope(next_model)

        # Apply thinking level for the new model.
        # - Explicit scoped model thinking level overrides defaults
        # - Per-model thinking level overrides take priority over the global default
        # setThinkingLevel clamps to model capabilities.
        # Model persistence does not implicitly rewrite the global thinking default.
        self.setThinkingLevel(thinking_level)

        await self._emitModelSelect(next_model, current_model, "cycle")

        return ModelCycleResult(model=next_model, thinkingLevel=self.thinkingLevel, isScoped=True)

    async def _cycleAvailableModel(
        self, direction: Literal["forward", "backward"], options: ModelMutationOptions
    ) -> ModelCycleResult | None:
        available_models = self._modelRuntime.getAvailableSnapshot()
        if len(available_models) <= 1:
            return None
        current_model = self.model
        current_index = next(
            (index for index, model in enumerate(available_models) if modelsAreEqual(model, current_model)),
            -1,
        )
        current_index = max(current_index, 0)
        next_index = (current_index + (1 if direction == "forward" else -1)) % len(available_models)
        next_model = available_models[next_index]

        thinking_level = self._getThinkingLevelForModelSwitch(next_model)
        # MISAKA fork: the settings write goes first; a role whose default cannot be saved here refuses it and the
        # session keeps its model.
        if options.get("persist"):
            self.settingsManager.setDefaultModelAndProvider(next_model.provider, next_model.id)
        self.agent.state.model = next_model
        self.sessionManager.appendModelChange(next_model.provider, next_model.id)
        if options.get("persist"):
            self._addPersistedDefaultToNonEmptyScope(next_model)

        # Apply thinking level for the new model.
        # Model persistence does not implicitly rewrite the global thinking default.
        self.setThinkingLevel(thinking_level)

        await self._emitModelSelect(next_model, current_model, "cycle")

        return ModelCycleResult(model=next_model, thinkingLevel=self.thinkingLevel, isScoped=False)

    def _getThinkingLevelForModelSwitch(
        self,
        targetModel: Model[Any] | None = None,
        explicitLevel: ThinkingLevel | None = None,
    ) -> ThinkingLevel:
        if explicitLevel is not None:
            return explicitLevel
        # MISAKA fork: the agent's own default comes before the shared per-model table.
        own = self.settingsManager.getRoleThinkingLevel()
        if own is not None:
            return own
        if targetModel is not None:
            per_model = self.settingsManager.getModelThinkingLevel(targetModel.provider, targetModel.id)
            if per_model is not None:
                return per_model
        return self.settingsManager.getDefaultThinkingLevel() or self.thinkingLevel or DEFAULT_THINKING_LEVEL

    def setSteeringMode(self, mode: str) -> None:
        self.agent.steeringMode = mode
        self.settingsManager.setSteeringMode(mode)

    def setFollowUpMode(self, mode: str) -> None:
        self.agent.followUpMode = mode
        self.settingsManager.setFollowUpMode(mode)

    def getPermissionMode(self) -> str | None:
        for part in self.moments.parts:
            getter = getattr(part, "get_permission_mode", None)
            if callable(getter):
                return getter()
        return None

    def setPermissionMode(self, mode: str) -> None:
        for part in self.moments.parts:
            setter = getattr(part, "set_permission_mode", None)
            if callable(setter):
                setter(mode)
                return
        raise ValueError("This session has no agent permission policy part")

    def registerCustomTools(self, definitions: list[Any]) -> None:
        # MISAKA fork: a part adding tools after startup (an MCP server that answered late)
        # takes the door ``customTools`` took, then the refresh an extension's registerTool gets.
        self._customTools.extend(definitions)
        self.refreshTools()

    def unregisterCustomTools(self, definitions: list[Any]) -> None:
        """Remove precisely the temporary definitions the caller installed, not namesakes."""
        identities = {id(definition) for definition in definitions}
        self._customTools = [tool for tool in self._customTools if id(tool) not in identities]
        self.refreshTools()

    def refreshTools(self) -> None:
        # MISAKA fork: the runtime action of the same name, callable by a part that changed a
        # definition it already registered (a description that depends on the session).
        self._refreshToolRegistry()

    def getActiveToolNames(self) -> list[str]:
        """Get the names of currently active tools, which are the tools declared to the model.
        Tools with ``codemode`` or ``deferred`` exposure are callable from other tools without
        being active."""
        return [tool.name for tool in self.agent.state.tools]

    def getCallableToolNames(self) -> list[str]:
        """Get the names of the tools that tools can call through ``ctx.executeTool()``."""
        return [tool.name for tool in self._getCallableTools()]

    def getAllTools(self) -> list[ToolInfo]:
        """Get all configured tools with name, description, parameter schema, prompt guidelines,
        and source metadata."""
        tools: list[ToolInfo] = []
        for entry in self._toolDefinitions.values():
            definition = entry.definition
            namespace = _definition_attr(definition, "namespace")
            annotations = _definition_attr(definition, "annotations")
            reaches_tools = _definition_attr(definition, "reachesTools")
            tools.append(
                ToolInfo(
                    name=definition.name,
                    description=definition.description,
                    parameters=definition.parameters,
                    promptGuidelines=list(getattr(definition, "promptGuidelines", None) or []),
                    exposure=self._getToolExposure(definition.name),
                    namespace=namespace if namespace else None,
                    annotations=dict(annotations) if annotations else None,
                    reachesTools=reaches_tools if reaches_tools else None,
                    sourceInfo=entry.sourceInfo,
                )
            )
        return tools

    def getToolDefinition(self, name: str) -> Any | None:
        entry = self._toolDefinitions.get(name)
        return entry.definition if entry is not None else None

    def getSlashCommands(self) -> list[SlashCommandInfo]:
        commands: list[SlashCommandInfo] = []
        for command in self._extensionRunner.getRegisteredCommands():
            invocation_name = str(read_field(command, "invocationName", read_field(command, "name", ""))).strip()
            if not invocation_name:
                continue
            commands.append(
                _make_slash_command_info(
                    invocation_name,
                    "extension",
                    read_field(command, "sourceInfo")
                    or createSyntheticSourceInfo(
                        f"<extension-command:{invocation_name}>",
                        {"source": "inline", "scope": "temporary", "origin": "extension"},
                    ),
                    read_field(command, "description"),
                )
            )

        for core_command in self.moments.commands():  # MISAKA fork: the parts' commands
            commands.append(
                _make_slash_command_info(
                    core_command.name,
                    "core",
                    createSyntheticSourceInfo(f"<core-command:{core_command.name}>", {"source": "sdk"}),
                    core_command.description,
                )
            )

        for template in self.promptTemplates:
            commands.append(
                _make_slash_command_info(
                    str(template.name),
                    "prompt",
                    template.sourceInfo
                    or createSyntheticSourceInfo(
                        f"<prompt:{template.name}>",
                        {"source": "auto", "scope": "project", "origin": "prompt"},
                    ),
                    template.description,
                )
            )


        return commands

    def setActiveToolsByName(self, toolNames: list[str]) -> None:
        """Set active tools by name.

        Only tools in the registry can be enabled. Unknown and hidden tool names are ignored.
        Also rebuilds the system prompt to reflect the new tool set. Changes take effect on the
        next agent turn.
        """
        previous = self.getActiveToolNames()
        # Keep selection before scopes/projections: a temporary mode must not erase
        # the user's ordinary tools, or revive tools the user explicitly disabled.
        names = list(dict.fromkeys(toolNames))
        scopes = getattr(self, "_toolScopes", ())
        if scopes:
            # MISAKA fork: a caller inside a scope sees only the scoped loadout (`[...active, x]`).
            # Selected tools the scope or a part hides from it stay selected underneath, in place;
            # tools the scope itself granted (visible before the call) do not outlive it.
            visible, chosen = set(self.getActiveToolNames()), set(names)
            names = [
                *(name for name in self._unscopedToolNames if name in chosen or name not in visible),
                *(name for name in names if name not in self._unscopedToolNames and name not in visible),
            ]
            scopes[-1]["active"] = list(toolNames)
        self._unscopedToolNames = names
        self._setActiveTools(toolNames)
        # A loadout that deactivates a tool replaces the restored one, whose pending tools are dropped.
        # One that only adds tools, like activating tool_search, keeps them.
        active = set(self.getActiveToolNames())
        if any(name not in active for name in previous):
            self._pendingToolNames.clear()

    @contextmanager
    def toolScope(self, toolNames: Sequence[str], callable: Sequence[str] = ()):
        """Temporary selection AND ceiling, independent of registry refresh.

        Keep the normal selection live underneath: late registrations, removals,
        reloads and explicit user changes survive leaving the scope. Nested scopes
        intersect; they never change the session's persistent permission settings.
        ``callable`` names tools the scope lets other tools call (codemode scripts
        calling MCP tools) without declaring them to the model; its entries may be ``*``
        patterns, so tools registered after the scope starts can be admitted too.
        """
        scope = {"allowed": createToolNameMatcher([*toolNames, *callable]), "active": list(toolNames)}
        self._toolScopes.append(scope)
        try:
            self._setActiveTools(scope["active"])
            yield
        finally:
            self._toolScopes = [item for item in self._toolScopes if item is not scope]
            names = self._toolScopes[-1]["active"] if self._toolScopes else self._unscopedToolNames
            self._setActiveTools(names)
            if not self._toolScopes:
                # MISAKA fork: a run inside the scope declared the scope's tools in the transcript,
                # which a resume or `/tree` restores as the lasting loadout. Declare the loadout the
                # scope gave back now; a run still working could declare the scope's tools after it.
                if self._isAgentRunActive:
                    self._recordLoadoutAfterRun = True
                else:
                    self._recordUnscopedLoadout()

    def _recordUnscopedLoadout(self) -> None:
        """MISAKA fork: declare the live loadout in the transcript when it differs from the loadout
        the transcript declares, as the tool patch the next run would write. Used when the last
        tool scope ends: pi's invariant that the transcript's declarations are the lasting loadout
        (restored on resume and `/tree`) then holds for the scope's runs too. A transcript that
        declares no loadout is left alone: restoring it keeps the default tools."""
        if self._toolScopes:
            return
        messages = self.sessionManager.buildSessionContext().messages
        if getCurrentSystemMessage(messages) is None:
            return
        changes = getToolStateChanges(
            getCurrentTools(messages), [toToolDeclaration(tool) for tool in self.agent.state.tools]
        )
        if len(changes.toolsAdded) == 0 and len(changes.toolsRemoved) == 0:
            return
        update = SystemMessage(
            content="",
            toolsAdded=changes.toolsAdded or None,
            toolsRemoved=changes.toolsRemoved or None,
            timestamp=int(time.time() * 1000),
        )
        # Recorded as a run records its messages (`message_end`): appended to the conversation and the transcript.
        entry_id = self.sessionManager.appendMessage(_message_dict(update))
        self.agent.state.messages = [*self.agent.state.messages, update]
        self._entryIdsByMessage[id(update)] = (update, entry_id)

    def _setActiveTools(self, toolNames: Sequence[str]) -> None:
        tools = self._applyToolLoadout(list(toolNames))
        for tool in tools:
            self._pendingToolNames.pop(tool.name, None)
        self._rebuildSystemPrompt([tool.name for tool in tools])

    def _getToolExposure(self, name: str) -> ToolExposure:
        entry = self._toolDefinitions.get(name)
        exposure = _definition_attr(entry.definition, "exposure") if entry is not None else None
        return exposure or "direct"

    def _getCallableTools(self, active: AbstractSet[str] | None = None) -> list[AgentTool]:
        """Tools callable through ``ctx.executeTool()``: the active ``direct`` tools and every
        registered ``codemode`` or ``deferred`` tool."""
        if active is None:
            active = set(self.getActiveToolNames())
        tools = [
            tool
            for tool in self._toolRegistry.values()
            if (exposure := self._getToolExposure(tool.name)) in ("codemode", "deferred")
            or (exposure == "direct" and tool.name in active)
        ]
        # MISAKA fork: a nested call answers to the same boundaries as a model-issued one. The
        # registry is already inside the admission, allow and deny lists; a tool scope (a
        # research window) and the parts' projections (driver-owned tools) narrow it further.
        for scope in self._toolScopes:
            tools = [tool for tool in tools if scope["allowed"](tool.name)]
        return self.moments.project_tools(tools)

    def _applyToolLoadout(self, toolNames: list[str]) -> list[AgentTool]:
        """Set the agent's tools for the given active tool names and return them. The active
        tools are the registered, non-hidden ones; they are declared to the model. Active tools
        with a ``prepareLoadout`` hook can change the declared descriptions and hide declarations
        from requests (see ``_installHiddenDeclarationsProjection``)."""
        # MISAKA fork: the tool scopes, the browser_exec/bash pairing and the parts' projections
        # apply to every loadout, so every path that sets the agent's tools honours them.
        for scope in self._toolScopes:
            toolNames = [name for name in toolNames if scope["allowed"](name)]
        tools = [
            self._toolRegistry[name]
            for name in dict.fromkeys(toolNames)
            if name in self._toolRegistry and self._getToolExposure(name) != "hidden" and self._isActivatable(name)
        ]
        if not any(tool.name == "bash" for tool in tools):
            tools = [tool for tool in tools if tool.name != "browser_exec"]
        elif any(tool.name == "browser_exec" for tool in tools):
            from misaka.core.web.browser.settings import BASE_TOOLS
            tools = [tool for tool in tools if tool.name not in BASE_TOOLS]
        tools = self.moments.project_tools(tools)

        hooks = [
            entry
            for tool in tools
            if (entry := self._toolDefinitions.get(tool.name)) is not None
            and _definition_attr(entry.definition, "prepareLoadout")
        ]
        hidden: set[str] = set()
        declared = tools
        if hooks:

            def get_namespace(name: str) -> Any:
                entry = self._toolDefinitions.get(name)
                return _definition_attr(entry.definition, "namespace") if entry is not None else None

            def get_prompt_guidelines(name: str) -> list[str]:
                entry = self._toolDefinitions.get(name)
                return list(entry.promptGuidelines) if entry is not None else []

            loadout = ToolLoadout(
                declared=tuple(tools),
                callable=tuple(self._getCallableTools({tool.name for tool in tools})),
                registered=tuple(self._toolRegistry.values()),
                getExposure=self._getToolExposure,
                getNamespace=get_namespace,
                getPromptGuidelines=get_prompt_guidelines,
            )
            descriptions: dict[str, str] = {}
            for entry in hooks:
                try:
                    changes = _definition_attr(entry.definition, "prepareLoadout")(loadout)
                    descriptions.update(read_field(changes, "descriptions") or {})
                    hidden.update(read_field(changes, "hiddenDeclarations") or [])
                except Exception as error:  # noqa: BLE001 - extension code: reported through the runner
                    self._extensionRunner.emitError(
                        ExtensionError(
                            extensionPath=entry.sourceInfo.path,
                            event="prepare_loadout",
                            error=str(error),
                            stack="".join(traceback.format_exception(error)),
                        )
                    )
            declared = [
                tool if (description := descriptions.get(tool.name)) is None
                else tool.model_copy(update={"description": description})
                for tool in tools
            ]
        self._hiddenDeclarations = frozenset(hidden)
        self.agent.state.tools = declared
        return declared

    def setDisallowedToolsByName(
        self,
        toolNames: list[str],
        alwaysAllowed: list[str] | None = None,
        *,
        admission: Callable[[str], bool] | None = None,
    ) -> None:
        """Apply a case-insensitive deny-list to current and future tools."""

        self._disallowedToolNames = {
            name.casefold() for name in toolNames if isinstance(name, str) and name
        }
        self._toolAdmission = admission
        self._alwaysAllowedToolNames = {
            name
            for name in (alwaysAllowed or [])
            if isinstance(name, str) and name
        }
        # The selection stays; tools the change lets through join as new registrations do.
        self._refreshToolRegistry()

    async def bindExtensions(self, bindings: ExtensionBindings | dict[str, Any] | None = None) -> None:
        resolved = bindings if isinstance(bindings, ExtensionBindings) else ExtensionBindings(**(bindings or {}))
        self._extensionBindings = resolved
        if resolved.uiContext is not None:
            self._extensionUIContext = resolved.uiContext
        if resolved.mode is not None:
            self._extensionMode = resolved.mode
        if resolved.commandContextActions is not None:
            self._extensionCommandContextActions = resolved.commandContextActions
        if resolved.abortHandler is not None:
            self._extensionAbortHandler = resolved.abortHandler
        if resolved.shutdownHandler is not None:
            self._extensionShutdownHandler = resolved.shutdownHandler
        if resolved.onError is not None:
            self._extensionErrorListener = resolved.onError
        self._applyExtensionBindings(self._extensionRunner)
        await self.moments.session_start(dict(self._sessionStartEvent))  # MISAKA fork
        await self._extensionRunner.emit(dict(self._sessionStartEvent))
        self._extensionRunner.reportUnhandledMcpServers()
        await self.extendResourcesFromExtensions(
            "reload" if _event_field(self._sessionStartEvent, "reason") == "reload" else "startup"
        )
        await self.moments.resources_ready({"type": "resources_ready"})

    async def reload(self, options: dict[str, Any] | None = None) -> None:
        previous_flag_values = self._extensionRunner.getFlagValues()
        await self.moments.session_shutdown({"type": "session_shutdown", "reason": "reload"})  # MISAKA fork
        await emitSessionShutdownEvent(self._extensionRunner, {"type": "session_shutdown", "reason": "reload"})
        # The old runner must unsubscribe from the shared event bus, or every
        # /reload leaks one more layer of handlers (pi #7656/6ca423447).
        self._extensionRunner.invalidate(_STALE_CONTEXT_MESSAGE)
        def getDefaultTools() -> list[str]:
            if not self._usesDefaultTools:
                return []
            configured = self.settingsManager.getDefaultTools()
            return applyToolModifiers(DEFAULT_TOOL_NAMES if configured is None else configured, self._defaultToolModifiers)

        previous_default_tools = set(getDefaultTools())
        await self.settingsManager.reload()
        self.agent.steeringMode = self.settingsManager.getSteeringMode()
        self.agent.followUpMode = self.settingsManager.getFollowUpMode()
        resetApiProviders()
        await self._resourceLoader.reload()
        # Activate tools newly added to defaultTools. Removed ones stay active, and tools disabled
        # during the session stay disabled unless the setting newly adds them.
        added_default_tools = [name for name in getDefaultTools() if name not in previous_default_tools]
        # Tools the new extensions register later, such as MCP tools, are pending until then.
        # MISAKA fork: the user's selection, not the loadout a tool scope shows.
        for name in self._unscopedToolNames:
            self._pendingToolNames[name] = None
        self._buildRuntime(
            {
                "activeToolNames": [*self._unscopedToolNames, *added_default_tools],
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
            before_session_start = read_field(options, "beforeSessionStart")
            if before_session_start is not None:
                result = before_session_start()
                if inspect.isawaitable(result):
                    await result
        # Native parts were shut down above even for a bound headless session.
        # Their lifecycle does not depend on UI/error/command bindings.
        if has_bindings or self._extensionBindings is not None:
            await self.moments.session_start({"type": "session_start", "reason": "reload"})
        if has_bindings:
            await self._extensionRunner.emit({"type": "session_start", "reason": "reload"})
            self._extensionRunner.reportUnhandledMcpServers()
        # A started headless session can have no UI/command/error bindings. Refresh
        # its resources too, but do not start discovery on a never-bound session.
        if self._extensionBindings is not None:
            await self.extendResourcesFromExtensions("reload")
            await self.moments.resources_ready({"type": "resources_ready", "reason": "reload"})

    def createReplacedSessionContext(self) -> Any:
        context = self._extensionRunner.createCommandContext()
        replaced_context = object.__new__(type(context))
        replaced_context.__dict__.update(context.__dict__)
        replaced_context.sendMessage = self.sendMessage
        replaced_context.sendUserMessage = self.sendUserMessage
        return replaced_context

    def hasExtensionHandlers(self, eventType: str) -> bool:
        return self._extensionRunner.hasHandlers(eventType)

    async def sendCustomMessage(
        self,
        message: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        resolved_options = dict(options or {})
        normalized = _message_dict(message)
        app_message = _normalize_nullish_message_content(
            {
                "role": "custom",
                "customType": read_field(normalized, "customType"),
                "content": _message_content(normalized),
                "display": bool(read_field(normalized, "display")),
                "details": read_field(normalized, "details"),
                "timestamp": int(time.time() * 1000),
            }
        )
        delivery_id = resolved_options.get("_deliveryId")
        if delivery_id:
            app_message["details"] = {**(app_message["details"] or {}), "delivery_id": delivery_id}
            if self._customMessageRecorded(delivery_id):
                self._customReceiptCallback(resolved_options, "_onPersist")
                return
            already_queued = delivery_id in self._customMessageReceipts
            self._customMessageReceipts[delivery_id] = resolved_options
            if already_queued:
                return
        await self._routeCustomMessage(app_message, resolved_options)

    async def _routeCustomMessage(self, app_message: dict[str, Any], resolved_options: dict[str, Any]) -> None:
        """Deliver a custom message whose receipt `sendCustomMessage` has taken: queue it, start a
        run with it, or append it.

        MISAKA fork: pi's `sendCustomMessage` is one method. Here the receipt bookkeeping for
        `_deliveryId` stays there, so a run deferred past `agent_settled` re-runs this routing
        alone: re-calling `sendCustomMessage` with the same delivery id would meet its own receipt
        and return without delivering the message."""
        try:
            deliver_as = resolved_options.get("deliverAs")
            if deliver_as == "nextTurn":
                self._pendingNextTurnMessages.append(app_message)
            elif self.isStreaming and resolved_options.get("triggerTurn") is False:
                # triggerTurn=False: record the message without interrupting the running turn
                # (pi #8022/47b5119d0). It cannot go into agent.state.messages from here: a
                # turn in flight has already appended the assistant message carrying its
                # toolCalls, so a custom message landing now sits between the calls and their
                # results -- and convertToLlm turns it into a user message, which the
                # Messages API rejects on every later request. Hold it out of the conversation
                # and let the flush put it in at the next turn boundary, where the toolCalls
                # are answered. The UI is told there too, not here: the TUI answers a custom
                # message_end by rebuilding the chat from the transcript, so announcing a
                # message that has not been written yet only draws it long enough for the next
                # toolResult to erase it.
                self._pendingCustomMessages.append(app_message)
            elif self.isStreaming:
                if deliver_as == "followUp":
                    self.agent.followUp(app_message)
                else:
                    self.agent.steer(app_message)
            elif resolved_options.get("triggerTurn"):
                if self._isEmittingAgentSettled:
                    # Re-evaluated when it runs: by then another deferred action may have started a
                    # run, which this message then joins as a queued message.
                    async def deferred_route() -> None:
                        await self._routeCustomMessage(app_message, resolved_options)

                    # MISAKA fork: a route a cancellation keeps from running is handed back (`_emitAgentSettled`).
                    deferred_route.handBack = lambda error: self._keepOrFailCustomMessage(  # type: ignore[attr-defined]
                        app_message, resolved_options, error, keepWithoutReceipt=True
                    )
                    self._deferredSettledActions.append(deferred_route)
                    return
                await self._startRun({"messageType": "custom", "message": app_message})
            else:
                self._appendCustomMessage(app_message)
                self._refreshFinalizedContext()
                self._emit({"type": "message_start", "message": app_message})
                self._emit({"type": "message_end", "message": app_message})

        except BaseException as error:
            self._keepOrFailCustomMessage(app_message, resolved_options, error)
            raise

    def _keepOrFailCustomMessage(
        self, app_message: dict[str, Any], resolved_options: dict[str, Any], error: BaseException,
        *, keepWithoutReceipt: bool = False,
    ) -> None:
        """MISAKA fork: a custom message that could not be delivered now (the run it started failed to start, or a
        cancellation kept its deferred route from running). One whose sender redelivers it (a receipt with
        `_onError`: the Sister outbox, mail) is failed back to that sender. One nobody redelivers (a sub-agent's or
        a background shell's completion notice, `_onPersist` only) keeps its receipt and waits in the follow-up
        queue for the next run, as `_handBackQueuedReceipts` leaves queued ones: failing it would lose it for
        good. A message with no receipt at all fails with its caller, as upstream, unless `keepWithoutReceipt`
        (a deferred route has no caller left to tell)."""
        delivery_id = resolved_options.get("_deliveryId")
        receipt = self._customMessageReceipts.get(delivery_id) if delivery_id else None
        if receipt is not None and receipt.get("_onError") is not None:
            self._failCustomReceipt(delivery_id, error)
        elif receipt is not None or (keepWithoutReceipt and not delivery_id):
            self.agent.followUp(app_message)

    def _customMessageRecorded(self, delivery_id):
        return bool(self.sessionManager.flushed and any(
            entry.get("type") == "custom_message" and isinstance(entry.get("details"), dict)
            and entry["details"].get("delivery_id") == delivery_id
            for entry in self.sessionManager.getEntries()))

    @staticmethod
    def _customReceiptCallback(receipt, name, *args):
        callback = receipt.get(name)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # a failed outbox ACK stays retryable
            logging.getLogger(__name__).warning("custom message receipt failed", exc_info=True)

    def _failCustomReceipt(self, delivery_id, error):
        receipt = self._customMessageReceipts.pop(delivery_id, None)
        if receipt and receipt.get("_onError"):
            if not isinstance(error, Exception):
                # MISAKA fork: a sender never gets the `CancelledError` (or another exit) of the task that was
                # delivering its message: it would take it for its own cancellation (the mail inbox's pump ended
                # with it, and the session read no more mail). It gets an ordinary failure and redelivers.
                logging.getLogger(__name__).warning(
                    "delivery %s was interrupted (%s); handed back to its sender", delivery_id, type(error).__name__)
                error = RuntimeError(f"The delivery was interrupted ({type(error).__name__}); it is to be redelivered")
            self._customReceiptCallback(receipt, "_onError", error)

    def _appendCustomMessage(self, message) -> str | None:
        details = read_field(message, "details")
        delivery_id = details.get("delivery_id") if isinstance(details, dict) else None
        entry_id: str | None = None
        try:
            if not delivery_id or not self._customMessageRecorded(delivery_id):
                entry_id = self.sessionManager.appendCustomMessageEntry(
                    str(read_field(message, "customType")), _message_content(message),
                    bool(read_field(message, "display")), details)
        except BaseException as error:
            self._failCustomReceipt(delivery_id, error)
            raise
        if delivery_id and self.sessionManager.flushed:
            receipt = self._customMessageReceipts.pop(delivery_id, None)
            if receipt:
                # The file has landed. A failed ACK is retried via the delivery id,
                # never by appending a second copy of the notification.
                self._customReceiptCallback(receipt, "_onPersist")
        return entry_id

    async def sendMessage(self, message: Any, options: dict[str, Any] | None = None) -> None:
        await self.sendCustomMessage(message, options)

    async def _sendUserMessage(
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
        await self._sendUserMessage(content, options)

    def getUserMessagesForForking(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for entry in self.sessionManager.getEntries():
            if entry.get("type") != "message":
                continue
            message = entry.get("message")
            if _message_role(message) != "user":
                continue
            text = self._extractUserMessageText(_message_content(message))
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
        abort_controller = AbortController()
        self._bashAbortControllers.add(abort_controller)
        prefix = self.settingsManager.getShellCommandPrefix()
        shell_path = self.settingsManager.getShellPath()
        resolved_command = f"{prefix}\n{command}" if prefix else command

        def handle_chunk(delta: str) -> None:
            if onChunk is not None:
                onChunk(delta)
            event: dict[str, Any] = {"type": "bash_execution_update", "delta": delta}
            bash_id = resolved_options.get("id")
            if bash_id is not None:
                event["id"] = bash_id
            self._emit(event)

        try:
            result = await executeBashWithOperations(
                resolved_command,
                self.sessionManager.getCwd(),
                resolved_options.get("operations") or createLocalBashOperations({"shellPath": shell_path}),
                {
                    "onChunk": handle_chunk,
                    "signal": abort_controller.signal,
                },
            )
            self.recordBashResult(command, result, resolved_options)
            return result
        finally:
            self._bashAbortControllers.discard(abort_controller)

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
        self.sessionManager.appendMessage(bash_message)
        self._refreshFinalizedContext()

    def abortBash(self) -> None:
        for abort_controller in tuple(self._bashAbortControllers):
            abort_controller.abort()

    def abortBranchSummary(self) -> None:
        if self._branchSummaryAbortController is not None:
            self._branchSummaryAbortController.abort()

    def _compactionSource(self) -> tuple[Any, ...]:
        """Owner and immutable branch identity captured before awaiting a summarizer."""
        manager = self.sessionManager
        return (manager, manager.getSessionId(), manager.getSessionFile(),
                tuple(entry.get("id") for entry in manager.getBranch()),
                len(manager.getEntries()), (self.model.model_dump(mode="json")
                                          if isinstance(self.model, Model) else copy.deepcopy(self.model)),
                copy.deepcopy(self.settingsManager.getCompactionSettings()))

    def _checkCompactionSource(self, source: tuple[Any, ...]) -> None:
        current = self._compactionSource()
        fields = ("owner", "session", "session file", "branch", "entries", "model", "settings")
        changed = [name for name, before, after in zip(fields, source, current, strict=True)
                   if before != after]
        appended = self.sessionManager.getEntries()[source[4]:]
        # Bookkeeping may arrive while a context engine or summarizer is awaiting.
        # Only accept a contiguous append to the original leaf, not a new branch,
        # message (including custom_message), model/thinking change or compaction.
        # ``usage`` is pi 1.3.0's cache warmer: a slow summary outlasts its interval
        # (Last Order's 500k-token compaction, 2026-10-26; R10).
        if (current[4] == source[4] + len(appended)
                and current[3] == (*source[3], *(entry.get("id") for entry in appended))
                and all(entry.get("type") in {"custom", "label", "session_info", "usage"} for entry in appended)):
            changed = [name for name in changed if name not in {"branch", "entries"}]
        if changed:
            raise RuntimeError("Session, branch, model or settings changed during compaction "
                               f"({', '.join(changed)})")

    def _publishCompaction(self, result: SessionCompactionResult, from_hook: bool,
                            source: tuple[Any, ...]) -> Any:
        self._checkCompactionSource(source)
        entry_id = self.sessionManager.appendCompaction(
            result.summary, result.firstKeptEntryId, result.tokensBefore,
            result.details, from_hook, result.usage,
            contextMessages=result.contextMessages, contextSummaries=result.contextSummaries,
        )
        # appendCompaction commits before changing either view. A failed write leaves
        # the previous archive, leaf and provider context intact.
        self._refreshFinalizedContext()
        result.estimatedTokensAfter = sum(
            estimateTokens(message) for message in self.sessionManager.buildSessionProjection().messages
        )
        return next((entry for entry in self.sessionManager.getEntries()
                     if entry.get("id") == entry_id), None)

    async def _prepareCompactionOperation(self, reason, signal, custom_instructions=None,
                                            *, preflight=False, current_tokens=None, will_retry=False,
                                            context_window=None):
        """Ask an installed context engine before native auth, threshold or cut points.

        A returned operation owns the whole replay; an explicit empty operation owns
        the no-op too. Only an absent engine goes through Pi's existing summary hook.
        """
        event = {"type": "session_context_prepare", "reason": reason, "signal": signal,
                 "preflight": preflight, "currentTokens": current_tokens,
                 "customInstructions": custom_instructions, "messages": list(self.messages),
                 "systemPrompt": self.systemPrompt,
                 "tools": [{"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                           for tool in self.agent.state.tools],
                 "allowCompression": reason == "manual" or bool(self.settingsManager.getCompactionSettings(self.model).get("enabled")),
                 # MISAKA fork: the model whose window applies, routed under a virtual selection.
                 "model": self._compactionLimitsModel(preflight)}
        decision = await self.moments.session_context_prepare(event)
        if decision is None and self._extensionRunner.hasHandlers("session_context_prepare"):
            decision = await self._extensionRunner.emit(event)
        if decision is not None:
            operation = _event_field(decision, "execute")
            if operation is None:
                return None
            if not callable(operation):
                raise TypeError("Context engine execute must be callable or None")
            async def engine_operation():
                result = await operation()
                if result is not None and _event_field(result, "contextMessages") is None:
                    raise ValueError("Context engine must return its complete contextMessages")
                return result, True
            return engine_operation

        if self.model is None:
            if reason == "manual":
                raise RuntimeError(formatNoModelSelectedMessage())
            return None
        settings = CompactionSettings(**self.settingsManager.getCompactionSettings(self.model))
        if reason == "threshold" and (preflight or current_tokens is not None):
            tokens = (
                estimateProjectedContextTokens(
                    self.sessionManager.buildSessionProjection(), self.sessionManager.getBranch()
                ).tokens
                if current_tokens is None
                else current_tokens
            )
            # MISAKA fork: the caller passes the window of the model that produced the checked
            # response (upstream checks in `_checkCompaction` itself); the preflight uses the model
            # the request goes to, and an unknown window never compacts (`_exceedsCompactionThreshold`).
            limits_model = self._compactionLimitsModel(preflight)
            window = limits_model.contextWindow if context_window is None else context_window
            if (preflight and window <= 0) or not shouldCompact(tokens, window, settings):
                return None
        if signal is not None and signal.aborted:
            raise RuntimeError("Compaction cancelled")
        branch_entries = self.sessionManager.getBranch()
        preparation = prepareCompaction(branch_entries, settings)
        if preparation is None or _is_noop_compaction(preparation):
            if reason == "manual":
                if branch_entries and branch_entries[-1].get("type") == "compaction":
                    raise RuntimeError("Already compacted")
                raise RuntimeError("Nothing to compact (session too small)")
            return None

        async def native_operation():
            event = {"type": "session_before_compact", "preparation": preparation,
                     "branchEntries": branch_entries, "customInstructions": custom_instructions,
                     "reason": reason, "willRetry": will_retry,
                     "signal": signal}
            provided = await self.moments.session_before_compact(event)
            if provided is None and self._extensionRunner.hasHandlers("session_before_compact"):
                provided = await self._extensionRunner.emit(event)
            if _result_flag(provided, "cancel", False):
                raise _CompactionCancelledByExtension("Compaction cancelled")
            provided = _result_flag(provided, "compaction")
            if provided is not None:
                return SessionCompactionResult(
                    summary=_event_field(provided, "summary"),
                    firstKeptEntryId=_event_field(provided, "firstKeptEntryId"),
                    tokensBefore=int(_event_field(provided, "tokensBefore", 0)),
                    details=_event_field(provided, "details"), usage=_event_field(provided, "usage"),
                    contextMessages=_event_field(provided, "contextMessages"),
                    contextSummaries=_event_field(provided, "contextSummaries")), True
            # Upstream `_runAutoCompaction`: `abortController.signal.throwIfAborted()` after the hooks.
            if reason != "manual" and signal is not None and signal.aborted:
                raise RuntimeError("Compaction cancelled")
            return await self._runDefaultCompaction(
                preparation, self.model, custom_instructions, signal, reason), False
        return native_operation

    def _compactionLimitsModel(self, preflight: bool) -> Model[Any] | None:
        """MISAKA fork: the model whose window a compaction check uses. At the provider boundary
        that is the model the request goes to; otherwise the model the conversation's limits
        come from."""
        return (self._requestModel if preflight else None) or self._limitsModel()

    async def _runDefaultCompaction(self, preparation, model, custom_instructions, signal, reason):
        """Generate Pi's built-in compaction summary for manual and automatic compaction."""
        # Resolve the request only when Pi summarizes itself: routing may call models or fail.
        request = await self._getSummarizationRequestAuth(model, signal)
        return await compact(
            preparation, request["model"], request.get("apiKey"), request.get("headers"),
            custom_instructions, signal, request["thinkingLevel"], self.agent.streamFunction,
            retry=self._summarizationRetryPolicy(), env=request.get("env"),
            callbacks=self._summarizationRetryCallbacks({"source": "compaction", "reason": reason}),
            sessionId=None)

    async def compact(self, customInstructions: str | None = None) -> SessionCompactionResult:
        # Keep the agent subscription live across compaction (pi #7370/e56893f4c):
        # unsubscribing here would drop the aborted partial message's
        # message_end/agent_end, so it would never be persisted or applied to state.
        await self.abort()
        self._compactionAbortController = AbortController()
        compaction_abort_controller = self._compactionAbortController
        self._emit({"type": "compaction_start", "reason": "manual"})

        published_result = None
        from_hook = False
        try:
            source = self._compactionSource()
            operation = await self._prepareCompactionOperation(
                "manual", self._compactionAbortController.signal, customInstructions)
            if operation is None:
                raise RuntimeError("Nothing to compact")
            self._checkCompactionSource(source)
            result, from_hook = await operation()
            if result is None:
                raise RuntimeError("Nothing to compact")

            if self._compactionAbortController.signal.aborted:
                raise RuntimeError("Compaction cancelled")

            saved_entry = self._publishCompaction(result, from_hook, source)
            published_result = result
            if saved_entry is not None:
                await self.moments.session_compact({  # MISAKA fork
                    "type": "session_compact", "compactionEntry": saved_entry, "fromExtension": from_hook,
                    "reason": "manual", "willRetry": False,
                })
                await self._extensionRunner.emit(
                    {
                        "type": "session_compact",
                        "compactionEntry": saved_entry,
                        "fromExtension": from_hook,
                        "reason": "manual",
                        "willRetry": False,
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
        except asyncio.CancelledError:
            self._compactionAbortController = None
            self._emit({"type": "compaction_end", "reason": "manual",
                        "result": published_result, "aborted": published_result is None,
                        "willRetry": False})
            if published_result is None:
                await self._emitSessionCompactFailed(
                    reason="manual", aborted=True, will_retry=False, from_extension=from_hook)
            raise
        except Exception as error:
            message = str(error)
            aborted = compaction_abort_controller.signal.aborted or isinstance(error, _CompactionCancelledByExtension)
            self._compactionAbortController = None
            self._emit(
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "result": published_result,
                    "aborted": aborted and published_result is None,
                    "willRetry": False,
                    "errorMessage": (f"Compaction notification failed: {message}" if published_result is not None
                                     else None if aborted else f"Compaction failed: {message}"),
                }
            )
            if published_result is None:
                await self._emitSessionCompactFailed(
                    reason="manual", aborted=aborted, will_retry=False,
                    error_message=None if aborted else f"Compaction failed: {message}",
                    from_extension=from_hook)
            raise
        finally:
            self._compactionAbortController = None

    async def _emitSessionCompactFailed(self, *, reason: str, aborted: bool,
                                           will_retry: bool,
                                           error_message: str | None = None,
                                           from_extension: bool = False) -> None:
        """Emit the terminal event for a failed or aborted compaction (pi #8175/a6b1dbceb).

        Lets extensions such as telemetry or LCM pair a session_before_compact
        attempt with its outcome; previously failures only reached the UI.
        """
        failed_event = {
            "type": "session_compact_failed",
            "reason": reason,
            "errorMessage": error_message,
            "aborted": aborted,
            "willRetry": will_retry,
            "fromExtension": from_extension,
        }
        # MISAKA fork: core parts hear it whether or not an extension is listening.
        await self.moments.session_compact_failed(failed_event)
        if not self._extensionRunner.hasHandlers("session_compact_failed"):
            return
        await self._extensionRunner.emit(failed_event)

    def abortCompaction(self) -> None:
        if self._compactionAbortController is not None:
            self._compactionAbortController.abort()
        if self._auto_compaction_abort_controller is not None:
            self._auto_compaction_abort_controller.abort()

    async def navigateTree(
        self,
        targetId: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.isStreaming:
            raise RuntimeError(
                "Wait for the current response to finish before navigating the session tree."
            )
        if self.isCompacting:
            raise RuntimeError(
                "Wait for the current compaction or tree navigation to finish before navigating the session tree."
            )

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

        collected = collectEntriesForBranchSummary(self.sessionManager, old_leaf_id, targetId)
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
            if self._extensionRunner.hasHandlers("session_before_tree"):
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
            summary_usage: Any | None = None
            if wants_summary and collected.entries and extension_summary is None:
                auth = await self._getSummarizationRequestAuth(self.model, self._branchSummaryAbortController.signal)
                branch_summary_settings = self.settingsManager.getBranchSummarySettings()
                summary_result = await generateBranchSummary(
                    collected.entries,
                    GenerateBranchSummaryOptions(
                        model=auth.get("model", self.model),
                        apiKey=auth.get("apiKey"),
                        headers=auth.get("headers"),
                        env=auth.get("env"),
                        signal=self._branchSummaryAbortController.signal,
                        customInstructions=custom_instructions,
                        replaceInstructions=replace_instructions,
                        reserveTokens=int(branch_summary_settings.get("reserveTokens", 16384)),
                        streamFn=self.agent.streamFunction,
                        retry=self._summarizationRetryPolicy(),
                        callbacks=self._summarizationRetryCallbacks({"source": "branchSummary"}),
                    ),
                )
                if summary_result.aborted:
                    return {"cancelled": True, "aborted": True}
                if summary_result.error:
                    raise RuntimeError(summary_result.error)
                summary_text = summary_result.summary
                summary_usage = summary_result.usage
                summary_details = {
                    "readFiles": list(summary_result.readFiles or []),
                    "modifiedFiles": list(summary_result.modifiedFiles or []),
                }
            elif extension_summary is not None:
                summary_text = _event_field(extension_summary, "summary")
                details = _event_field(extension_summary, "details")
                summary_details = _message_dict(details) if details is not None else None
                summary_usage = _event_field(extension_summary, "usage")

            new_leaf_id: str | None
            editor_text: str | None = None
            entry_type = target_entry.get("type")
            if entry_type == "message" and _message_role(target_entry.get("message")) == "user":
                parent_id = target_entry.get("parentId")
                new_leaf_id = parent_id if isinstance(parent_id, str) else None
                editor_text = self._extractUserMessageText(_message_content(target_entry.get("message")))
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
                    summary_usage,
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

            # Update finalized context from the canonical session projection.
            self._refreshFinalizedContext()
            self._restoreToolsFromTranscript()

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
            if editor_text is not None:
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
        user_messages = 0
        assistant_messages = 0
        tool_results = 0
        total_messages = 0
        tool_calls = 0
        usage_totals = createUsageTotals()

        for entry in self.sessionManager.getEntries():
            entry_type = read_field(entry, "type")
            if entry_type == "usage":
                addUsageToTotals(usage_totals, read_field(entry, "usage"))
            elif entry_type in ("branch_summary", "compaction"):
                usage = read_field(entry, "usage")
                if usage:
                    addUsageToTotals(usage_totals, usage)
            if entry_type != "message":
                continue
            total_messages += 1
            message = read_field(entry, "message")
            role = _message_role(message)
            if role == "user":
                user_messages += 1
            elif role == "toolResult":
                tool_results += 1
                usage = read_field(message, "usage")
                if usage:
                    addUsageToTotals(usage_totals, usage)
            elif role == "assistant":
                assistant_messages += 1
                content = _message_content(message)
                if isinstance(content, list):
                    tool_calls += sum(1 for block in content if _content_type(block) == "toolCall")
                addUsageToTotals(usage_totals, read_field(message, "usage") or {})

        return SessionStats(
            sessionFile=self.sessionFile,
            sessionId=self.sessionId,
            userMessages=user_messages,
            assistantMessages=assistant_messages,
            toolCalls=tool_calls,
            toolResults=tool_results,
            totalMessages=total_messages,
            tokens=SessionTokenStats(
                input=usage_totals.input,
                output=usage_totals.output,
                cacheRead=usage_totals.cacheRead,
                cacheWrite=usage_totals.cacheWrite,
                total=(
                    usage_totals.input
                    + usage_totals.output
                    + usage_totals.cacheRead
                    + usage_totals.cacheWrite
                ),
            ),
            cost=usage_totals.cost,
            contextUsage=self.getContextUsage(),
        )

    async def exportToHtml(
        self,
        outputPath: str | None = None,
        options: dict[str, str | None] | None = None,
    ) -> str:
        requested_theme = (options or {}).get("themeName")
        theme_name = next(
            (
                candidate
                for candidate in (requested_theme, self.settingsManager.getTheme())
                if isinstance(candidate, str)
                and candidate
                and getThemeByName(candidate) is not None
            ),
            None,
        )
        return await exportSessionToHtml(
            self.sessionManager,
            self.state,
            {
                "outputPath": outputPath,
                "themeName": theme_name,
                "toolRenderer": createToolHtmlRenderer(
                    ToolHtmlRendererDeps(
                        getToolRenderers=lambda name: self._extensionRunner.resolveToolRenderers(
                            name, lambda: self.getToolDefinition(name)
                        ),
                        theme=theme,
                        cwd=self.sessionManager.getCwd(),
                    )
                ),
            },
        )

    def exportToJsonl(self, outputPath: str | None = None) -> str:
        return exportSessionToJsonl(self.sessionManager, outputPath)

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
        model = self._limitsModel()
        if model is None or model.contextWindow <= 0:
            return None

        # After compaction, the last assistant usage reflects pre-compaction context size.
        # We can only trust usage from an assistant that responded after the latest compaction.
        # If no such assistant exists, context token count is unknown until the next LLM response.
        projection = self.sessionManager.buildSessionProjection()
        branch = self.sessionManager.getBranch()
        latest_compaction = getLatestCompactionEntry(branch)
        if latest_compaction is not None:
            projected_assistants = {
                str(entry.sourceEntry.get("id"))
                for entry in projection.entries
                if any(
                    _message_role(message) == "assistant"
                    and read_field(message, "stopReason") not in {"aborted", "error"}
                    and calculateContextTokens(read_field(message, "usage") or {}) > 0
                    for message in entry.messages
                )
            }
            compaction_index = next(
                (index for index, entry in enumerate(branch) if entry.get("id") == latest_compaction.get("id")), -1
            )
            has_post_compaction_usage = any(
                str(entry.get("id")) in projected_assistants for entry in branch[compaction_index + 1 :]
            )
            if not has_post_compaction_usage:
                return {"tokens": None, "contextWindow": model.contextWindow, "percent": None}

        estimate = estimateProjectedContextTokens(projection, branch)
        percent = (estimate.tokens / model.contextWindow) * 100 if model.contextWindow else None
        return {"tokens": estimate.tokens, "contextWindow": model.contextWindow, "percent": percent}

    async def _setModelIfConfigured(self, model: Model[Any]) -> bool:
        if not self._modelRuntime.hasConfiguredAuth(model.provider):
            return False
        await self.setModel(model)
        return True

    async def _handleAgentEvent(self, event: Any, _signal: Any | None = None) -> None:
        event_type = _event_type(event)
        message = _event_field(event, "message")
        # An abort requested while the run was starting, e.g. during before_agent_start, stops it
        # here. agent_start is awaited before the first request.
        if event_type == "agent_start" and self._agentRunAbortRequested:
            self.agent.abort()
        # Record the calls a tool made through ctx.executeTool() and their usage on its result message.
        if self._nestedToolCalls is not None:
            if event_type == "message_start" and _message_role(message) == "toolResult":
                summary = self._nestedToolCalls.takeRecord(read_field(message, "toolCallId"))
                if summary is not None and summary.calls is not None:
                    message.nestedCalls = summary.calls
                if summary is not None and summary.usage is not None:
                    message.usage = (
                        combineUsage(message.usage, summary.usage) if message.usage is not None else summary.usage
                    )
            elif event_type == "agent_end":
                self._nestedToolCalls.clear()
        if event_type == "message_start" and _message_role(message) == "user":
            self._overflow_recovery_attempted = False
            message_text = self._extractUserMessageText(_message_content(message))
            if message_text:
                if message_text in self._steeringMessages:
                    self._steeringMessages.remove(message_text)
                    self._emitQueueUpdate()
                elif message_text in self._followUpMessages:
                    self._followUpMessages.remove(message_text)
                    self._emitQueueUpdate()

        # Emit to extensions first, then notify public listeners.
        await self._emitExtensionEvent(event)

        if event_type == "agent_end":
            self._emit(_decorate_agent_end_event(event, self._willRetryAfterAgentEnd(event)))
        else:
            self._emit(event)

        if event_type == "message_end" and message is not None:
            entry_id = self._persistMessage(message)
            if entry_id:
                self._entryIdsByMessage[id(message)] = (message, entry_id)
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

        if event_type == "turn_end":
            self._lastAssistantToolResults = list(_event_field(event, "toolResults") or [])
            # MISAKA fork: a turn boundary is the first place a message queued mid-turn can
            # land safely: every toolCall the turn made has its result by now. Waiting for
            # the end of the run instead would keep a research run's progress off disk and
            # off screen for hours. A turn that errored, aborted or was cut at the output
            # cap is skipped -- its tail is still being rewritten, by _prepareRetry, by
            # the abort itself, or by the truncated-response recovery in _checkCompaction
            # /_runAutoCompaction. A custom message flushed on top of it would hide the
            # tail and leave it in the conversation for the retry to re-send.
            turn_message = _as_assistant_message(_event_field(event, "message"))
            if turn_message is not None and turn_message.stopReason not in ("error", "aborted", "length"):
                self._flushPendingCustomMessages()

    def _emit(self, event: Any) -> None:
        # MISAKA fork: iterate a snapshot so listeners that unsubscribe during dispatch do not
        # cause later listeners to miss this event (pi applies this to its RPC client's listeners).
        for listener in list(self._eventListeners):
            listener(event)

    def getCorePromptCommand(self, text: str) -> CoreCommand | None:
        if not text.startswith("/"):
            return None
        command = self.moments.command(text[1:].partition(" ")[0])
        return command if command is not None and command.is_prompt else None

    async def _expandCorePromptCommand(self, command: CoreCommand, text: str) -> str | None:
        result = command.handler(text[1:].partition(" ")[2], self._extensionRunner.createCommandContext())
        if inspect.isawaitable(result):
            result = await result
        if result is not None and not isinstance(result, str):
            raise TypeError(f"Prompt command /{command.name} must return text or None")
        return result

    async def _tryExecuteCoreCommand(self, text: str) -> bool:
        # MISAKA fork: a part's command runs like an extension command, without the runner.
        if not text.startswith("/"):
            return False
        command_name, _, raw_args = text[1:].partition(" ")
        resolved = self.moments.command(command_name)
        if resolved is None or resolved.is_prompt:
            return False
        try:
            result = resolved.handler(raw_args, self._extensionRunner.createCommandContext())
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001 - reported through the same channel as an extension command
            self._extensionRunner.emitError(
                ExtensionError(
                    extensionPath=f"command:{command_name}",
                    event="command",
                    error=str(error),
                )
            )
        return True

    async def _tryExecuteExtensionCommand(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        command_text = text[1:]
        command_name, _, raw_args = command_text.partition(" ")
        resolved = self._extensionRunner.getCommand(command_name)
        if resolved is None or resolved.handler is None:
            return False

        try:
            result = resolved.handler(raw_args, self._extensionRunner.createCommandContext())
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001 - extension code: any failure is reported as an extension error
            self._extensionRunner.emitError(
                ExtensionError(
                    extensionPath=f"command:{command_name}",
                    event="command",
                    error=str(error),
                )
            )
        return True

    def _persistMessage(self, message: Any) -> str | None:
        role = _message_role(message)
        if role == "custom":
            return self._appendCustomMessage(message)
        if role in {"system", "user", "assistant", "toolResult"}:
            return self.sessionManager.appendMessage(_message_dict(message))
        return None

    def _applyExtensionBindings(self, runner: ExtensionRunner) -> None:
        runner.setUIContext(self._extensionUIContext, self._extensionMode)
        runner.bindCommandContext(self._extensionCommandContextActions)
        if self._extensionErrorUnsubscriber is not None:
            self._extensionErrorUnsubscriber()
            self._extensionErrorUnsubscriber = None
        self._extensionErrorUnsubscriber = (
            runner.onError(self._extensionErrorListener) if self._extensionErrorListener is not None else None
        )

    async def extendResourcesFromExtensions(self, reason: str) -> None:
        if not self._extensionRunner.hasHandlers("resources_discover"):
            return

        discovered = await self._extensionRunner.emitResourcesDiscover(self._cwd, reason)
        if not discovered["promptPaths"] and not discovered["themePaths"] and not discovered.get("skillPaths"):
            return

        self._resourceLoader.extendResources(
            {
                "promptPaths": self.buildExtensionResourcePaths(discovered["promptPaths"]),
                "themePaths": self.buildExtensionResourcePaths(discovered["themePaths"]),
                "skillPaths": self.buildExtensionResourcePaths(discovered.get("skillPaths", [])),
            }
        )
        self._rebuildSystemPrompt(self.getActiveToolNames())

    def buildExtensionResourcePaths(self, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in entries:
            extension_path = entry["extensionPath"]
            base_dir = None if isSyntheticPath(extension_path) else os.path.dirname(extension_path)
            result.append(
                {
                    "path": entry["path"],
                    "metadata": {
                        "source": self.getExtensionSourceLabel(extension_path),
                        "scope": "temporary",
                        "origin": "top-level",
                        "baseDir": base_dir,
                    },
                }
            )
        return result

    def getExtensionSourceLabel(self, extension_path: str) -> str:
        if isSyntheticPath(extension_path):
            return f"extension:{extension_path.replace('<', '').replace('>', '')}"
        base = os.path.basename(extension_path)
        return f"extension:{re.sub(r'[.](ts|js)$', '', base)}"

    async def _emitExtensionEvent(self, event: Any) -> None:
        event_type = _event_type(event)
        if event_type == "agent_start":
            self._turnIndex = 0
            await self.moments.agent_start({"type": "agent_start"})  # MISAKA fork
            await self._extensionRunner.emit({"type": "agent_start"})
        elif event_type == "agent_end":
            agent_end_event = {
                "type": "agent_end",
                "messages": list(_event_field(event, "messages", []) or []),
            }
            # MISAKA fork: a core part may block first; extensions are asked only if none did.
            hook_result = await self.moments.agent_end(agent_end_event)
            if not _event_field(hook_result, "block", False):
                hook_result = await self._extensionRunner.emitAgentEnd(agent_end_event)
            if _event_field(hook_result, "block", False):
                reason = str(
                    _event_field(
                        hook_result,
                        "reason",
                        "Stop hook blocked completion",
                    )
                )
                self._stopHookContinuationPending = True
                await self._queueFollowUp(f"Stop hook feedback:\n{reason}")
        elif event_type == "turn_start":
            await self._extensionRunner.emit(
                {
                    "type": "turn_start",
                    "turnIndex": self._turnIndex,
                    "timestamp": int(time.time() * 1000),
                }
            )
        elif event_type == "turn_end":
            turn_message = _event_field(event, "message")
            dispatched = id(turn_message) in self._boundaryDispatchedMessages
            self._boundaryDispatchedMessages.discard(id(turn_message))
            if _message_role(turn_message) == "assistant" and not dispatched:
                await self._dispatchTurnEndBoundary(turn_message, list(_event_field(event, "toolResults") or []))
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
            # The Agent reducer stores an isolated copy before awaiting listeners. Capture
            # that exact copy before the extension can re-enter and append more state.
            state_message = self.agent.state.messages[-1] if self.agent.state.messages else None
            replacement = await self._extensionRunner.emitMessageEnd({"type": "message_end", "message": message})
            if replacement is not None and message is not None:
                normalized_replacement = _normalize_nullish_message_content(replacement)
                self._replaceMessageInPlace(message, normalized_replacement)
                if state_message is not None:
                    self._replaceMessageInPlace(state_message, message)
        elif event_type in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
            await self._extensionRunner.emit(_tool_execution_extension_event(event))

    def _replaceMessageInPlace(self, target: Any, replacement: Any) -> None:
        if target is replacement:
            return
        replacement_dict = _message_dict(replacement)
        if isinstance(target, dict):
            target.clear()
            target.update(replacement_dict)
            return
        target_dict = getattr(target, "__dict__", None)
        if isinstance(target_dict, dict):
            # The caller keeps using `target` right after this - agent_loop reads
            # block.type on every content block of the message it just ended - so the
            # target has to stay a usable model. Copying model_dump()'s plain dicts
            # into __dict__ would leave .content full of dicts and kill the turn with
            # unanswered toolCalls, so re-validate into the target's own type first.
            validated = _validate_as(type(target), replacement_dict)
            source = replacement_dict if validated is None else validated.__dict__
            target_dict.clear()
            target_dict.update(source)
            fields_set = getattr(target, "__pydantic_fields_set__", None)
            if isinstance(fields_set, set):
                fields_set.clear()
                fields_set.update(
                    replacement_dict.keys()
                    if validated is None
                    else getattr(validated, "__pydantic_fields_set__", replacement_dict.keys())
                )
            return
        for key, value in replacement_dict.items():
            setattr(target, key, value)

    def _bindExtensionCore(self, runner: ExtensionRunner) -> None:
        def _emit_runtime_error(event: str, error: Exception) -> None:
            runner.emitError(
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

            self._spawnExtensionMessage(_run())

        def _send_user_message_from_runtime(
            content: str | list[TextContent | ImageContent | dict[str, Any]],
            options: dict[str, Any] | None = None,
        ) -> None:
            async def _run() -> None:
                try:
                    await self._sendUserMessage(content, options)
                except Exception as error:  # noqa: BLE001
                    _emit_runtime_error(
                        "send_user_message",
                        error if isinstance(error, Exception) else RuntimeError(str(error)),
                    )

            self._spawnExtensionMessage(_run(), "send_user_message")

        def _append_entry_from_runtime(custom_type: str, *data: Any) -> None:
            entry_id = self.sessionManager.appendCustomEntry(custom_type, *data)
            entry = self.sessionManager.getEntry(entry_id)
            if entry is not None:
                self._emit({"type": "entry_appended", "entry": entry})

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

            self._spawnBackground(_run(), "compact")

        runner.bindCore(
            {
                "sendMessage": _send_message_from_runtime,
                "sendUserMessage": _send_user_message_from_runtime,
                "appendEntry": _append_entry_from_runtime,
                "setSessionName": self.setSessionName,
                "getSessionName": self.sessionManager.getSessionName,
                "setLabel": lambda entryId, label: self.sessionManager.appendLabelChange(entryId, label),
                "getActiveTools": self.getActiveToolNames,
                "getAllTools": self.getAllTools,
                "getSettings": self.settingsManager.getSettings,
                "setActiveTools": self.setActiveToolsByName,
                "refreshTools": lambda: self._refreshToolRegistry(),
                "getCommands": self.getSlashCommands,
                "setModel": self._setModelIfConfigured,
                "getThinkingLevel": lambda: self.thinkingLevel,
                "setThinkingLevel": self.setThinkingLevel,
            },
            {
                "getModel": lambda: self.model,
                "getScopedModels": lambda: self.scopedModels,
                "isIdle": lambda: self.isIdle,
                "isProjectTrusted": lambda: self.settingsManager.isProjectTrusted(),
                "getSignal": lambda: self.agent.signal,
                "abort": lambda: self._extensionAbortHandler() if self._extensionAbortHandler else self._spawnBackground(self.abort(), "abort"),
                "hasPendingMessages": lambda: self.pendingMessageCount > 0,
                "shutdown": lambda: self._extensionShutdownHandler() if self._extensionShutdownHandler else None,
                "getContextUsage": self.getContextUsage,
                "compact": _compact_from_extension,
                "getSystemPrompt": lambda: self.systemPrompt,
                "getSystemPromptOptions": lambda: self._baseSystemPromptOptions,
                "executeTool": self._executeNestedToolCall,
                "getCallableTools": lambda: self._getCallableTools(),
            },
            {
                "registerProvider": self._registerProvider,
                "registerNativeProvider": self._registerNativeProvider,
                "unregisterProvider": self._unregisterProvider,
                "registerVirtualModel": self._registerVirtualModel,
                "unregisterVirtualModel": self._unregisterVirtualModel,
            },
        )

    def _createExtensionRunner(self, flag_values: dict[str, bool | str] | None = None) -> ExtensionRunner:
        extensions_result = self._resourceLoader.getExtensions()
        if flag_values:
            for name, value in flag_values.items():
                extensions_result.runtime.flagValues[name] = value
        runner = ExtensionRunner(
            extensions=list(extensions_result.extensions),
            runtime=extensions_result.runtime,
            cwd=self._cwd,
            sessionManager=self.sessionManager,
            modelRegistry=self.modelRegistry,
        )
        self._bindExtensionCore(runner)
        if self._extensionRunnerRef is not None:
            self._extensionRunnerRef["current"] = runner
            self._extensionRunnerRef["session"] = self  # MISAKA fork: sdk.transform_context reaches the parts
            runner.moments = self.moments  # MISAKA fork: the runner's own ui_prompt events reach the parts
        return runner

    def _registerProvider(self, name: str, config: dict[str, Any]) -> None:
        self._modelRuntime.registerProvider(name, config)
        self._refreshCurrentModelFromRegistry()

    def _registerNativeProvider(self, provider: Any) -> None:
        self._modelRuntime.registerNativeProvider(provider)
        self._refreshCurrentModelFromRegistry()

    def _unregisterProvider(self, name: str) -> None:
        self._modelRuntime.unregisterProvider(name)
        self._refreshCurrentModelFromRegistry()

    def _registerVirtualModel(self, definition: VirtualModelDefinition) -> None:
        self._modelRuntime.registerVirtualModel(definition)
        self._refreshCurrentModelFromRegistry()

    def _unregisterVirtualModel(self, provider: str, id: str) -> None:
        self._modelRuntime.unregisterVirtualModel(provider, id)
        self._refreshCurrentModelFromRegistry()

    def _refreshCurrentModelFromRegistry(self) -> None:
        current_model = self.model
        if current_model is None:
            return
        refreshed_model = self._modelRuntime.getModel(current_model.provider, current_model.id)
        if refreshed_model is None or refreshed_model == current_model:
            return
        self.agent.state.model = refreshed_model

    def _refreshToolRegistry(self, options: dict[str, Any] | None = None) -> None:
        resolved_options = dict(options or {})
        if self.moments.parts:
            self._customTools = self.moments.configure_tools(
                self._customTools, self._resourceLoader.getExtensions().extensions
            )
        # Tools that were already activated on registration. A tool whose exposure changes to
        # `direct` or `model-only` (for example from `hidden`) is activated like a new tool.
        previous_activated_on_registration = {
            name for name in self._toolRegistry if self._isActivatedOnRegistration(name)
        }
        previous_active_tool_names = list(self._unscopedToolNames)

        registered_tools = self._extensionRunner.getAllRegisteredTools()
        all_custom_tools: list[tuple[Any, SourceInfo]] = [
            *( (tool.definition, tool.sourceInfo) for tool in registered_tools ),
            *(
                (
                    definition
                    if isinstance(definition, ToolDefinition)
                    else createToolDefinitionFromAgentTool(definition),
                    createSyntheticSourceInfo(f"<sdk:{definition.name}>", {"source": "sdk"}),
                )
                for definition in self._customTools
            ),
        ]
        all_custom_tools = [
            (definition, source_info)
            for definition, source_info in all_custom_tools
            if self._isAllowedTool(definition.name)
        ]

        definition_registry: dict[str, _ToolDefinitionEntry] = {}
        for name, definition in self._baseToolDefinitions.items():
            if not self._isAllowedTool(name):
                continue
            definition_registry[name] = _ToolDefinitionEntry(
                definition=definition,
                sourceInfo=createSyntheticSourceInfo(f"{BUILTIN_PATH_PREFIX}{name}", {"source": "builtin"}),
                promptSnippet=self._normalizePromptSnippet(_definition_attr(definition, "promptSnippet")),
                promptGuidelines=self._normalizePromptGuidelines(_definition_attr(definition, "promptGuidelines")),
            )
        for definition, source_info in all_custom_tools:
            definition_registry[definition.name] = _ToolDefinitionEntry(
                definition=definition,
                sourceInfo=source_info,
                promptSnippet=self._normalizePromptSnippet(_definition_attr(definition, "promptSnippet")),
                promptGuidelines=self._normalizePromptGuidelines(_definition_attr(definition, "promptGuidelines")),
            )
        self._toolDefinitions = definition_registry

        # pi agent-session.ts:2694-2703 sends built-in and extension tools through the same
        # wrapper, so a tool of either kind that turns on more tools while it runs tags its
        # own result with the names that became available (A-15 addedToolNames).
        tool_registry: dict[str, AgentTool] = {}
        for name, definition in self._baseToolDefinitions.items():
            if self._isAllowedTool(name):
                tool_registry[name] = wrapRegisteredTool(
                    RegisteredTool(
                        definition=definition,
                        sourceInfo=createSyntheticSourceInfo(f"{BUILTIN_PATH_PREFIX}{name}", {"source": "builtin"}),
                    ),
                    self._extensionRunner,
                )
        for definition, source_info in all_custom_tools:
            tool_registry[definition.name] = wrapRegisteredTool(
                RegisteredTool(definition=definition, sourceInfo=source_info),
                self._extensionRunner,
            )
        self._toolRegistry = tool_registry

        if "activeToolNames" in resolved_options:
            next_active_tool_names = list(resolved_options["activeToolNames"] or [])
        else:
            next_active_tool_names = list(previous_active_tool_names)
        next_active_tool_names = [name for name in next_active_tool_names if self._isAllowedTool(name)]

        if getattr(self, "_toolScopes", ()):
            # A scope is not permission to revive tools the underlying user
            # selection had turned off. Only genuinely new registrations join it.
            next_active_tool_names.extend(
                name
                for name in self._toolRegistry
                if name not in previous_activated_on_registration and self._isActivatedOnRegistration(name)
            )
        elif self._allowedTools is not None:
            for tool_name in self._toolRegistry:
                # Naming or matching a tool activates it even when it is not active by default. MCP tools
                # kept registered without being named stay inactive.
                if self._allowedTools(tool_name) and self._isDeclarable(tool_name):
                    next_active_tool_names.append(tool_name)
        elif resolved_options.get("includeAllExtensionTools"):
            for definition, _source_info in all_custom_tools:
                if self._isActivatedOnRegistration(definition.name):
                    next_active_tool_names.append(definition.name)
        elif "activeToolNames" not in resolved_options:
            for tool_name in self._toolRegistry:
                if tool_name not in previous_activated_on_registration and self._isActivatedOnRegistration(
                    tool_name
                ):
                    next_active_tool_names.append(tool_name)

        # Pending tools that are registered now become active.
        next_active_tool_names.extend(self._pendingToolNames)

        active_tool_names: list[str] = []
        seen: set[str] = set()
        for name in next_active_tool_names:
            if name in self._toolRegistry and name not in seen:
                seen.add(name)
                active_tool_names.append(name)
        # The selection is set directly: the public `setActiveToolsByName` would drop the pending tools.
        self._unscopedToolNames = active_tool_names
        # MISAKA fork: inside a tool scope the scoped loadout stays active; the selection changes underneath it.
        if getattr(self, "_toolScopes", ()):
            self._setActiveTools(self._toolScopes[-1]["active"])
        else:
            self._setActiveTools(active_tool_names)

    def _isAllowedTool(self, name: str) -> bool:
        """Whether `--tools` and `--exclude-tools` keep the tool registered. MCP tools stay registered
        unless the allowlist filters them (see `_allowlistFiltersMcp`)."""
        # MISAKA fork: misaka also asks the tool admission hook, keeps browser_exec behind bash, and
        # honours the disallowed and always-allowed sets.
        if self._toolAdmission is not None and not self._toolAdmission(name):
            return False
        if name == "browser_exec" and ("bash" not in self._baseToolDefinitions or not self._isAllowedTool("bash")):
            return False
        if self._excludedTools is not None and self._excludedTools(name):
            return False
        normalized = name.casefold()
        if name in self._alwaysAllowedToolNames:
            return True
        if "*" in self._disallowedToolNames or normalized in self._disallowedToolNames:
            return False
        if self._allowedTools is None or self._allowedTools(name):
            return True
        return not self._allowlistFiltersMcp and isMcpToolName(name)

    def _isActivatable(self, name: str) -> bool:
        """Whether the tool may be active, which declares it to the model. MCP tools the allowlist keeps
        without matching them are only for codemode and tool_search: they may be declared only when
        tool_search can load them (non-`direct` exposure and tool_search registered). This also applies
        to tools restored from the transcript or set by extensions."""
        if self._allowedTools is None or self._allowedTools(name) or not isMcpToolName(name):
            return True
        return self._getToolExposure(name) != "direct" and "tool_search" in self._toolRegistry

    def _isDeclarable(self, name: str) -> bool:
        """Whether activating the tool declares it to the model."""
        return self._getToolExposure(name) in ("direct", "model-only")

    def _isActivatedOnRegistration(self, name: str) -> bool:
        """Whether registering the tool activates it, which declares it to the model."""
        entry = self._toolDefinitions.get(name)
        return self._isDeclarable(name) and (
            entry is None or _definition_attr(entry.definition, "defaultActive") is not False
        )

    def _buildRuntime(self, options: dict[str, Any] | None = None) -> None:
        resolved_options = dict(options or {})
        auto_resize_images = self.settingsManager.getImageAutoResize()
        shell_command_prefix = self.settingsManager.getShellCommandPrefix()
        shell_path = self.settingsManager.getShellPath()

        if self._baseToolsOverride is not None:
            base_tool_definitions = {
                name: createToolDefinitionFromAgentTool(tool)
                for name, tool in self._baseToolsOverride.items()
            }
        else:
            tool_options = {
                "read": {"autoResizeImages": auto_resize_images},
                "bash": {"commandPrefix": shell_command_prefix, "shellPath": shell_path},
            }
            self.moments.configure_tool_options(tool_options)
            base_tool_definitions = createAllToolDefinitions(self._cwd, tool_options)

        self._baseToolDefinitions = dict(base_tool_definitions)
        self._extensionRunner = self._createExtensionRunner(resolved_options.get("flagValues"))
        self._applyExtensionBindings(self._extensionRunner)

        # MISAKA fork: MISAKA's default tools (`DEFAULT_TOOL_NAMES`), where pi lists read/bash/edit/write here.
        default_active_tool_names = (
            list(self._baseToolsOverride)
            if self._baseToolsOverride is not None
            else list(DEFAULT_TOOL_NAMES)
        )
        base_active_tool_names = (
            list(resolved_options["activeToolNames"])
            if "activeToolNames" in resolved_options and resolved_options["activeToolNames"] is not None
            else default_active_tool_names
        )
        self._refreshToolRegistry(
            {
                "activeToolNames": base_active_tool_names,
                "includeAllExtensionTools": resolved_options.get("includeAllExtensionTools"),
            }
        )

    def _normalizePromptSnippet(self, text: str | None) -> str | None:
        if not text:
            return None
        one_line = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
        return one_line or None

    def _normalizePromptGuidelines(self, guidelines: Any) -> list[str]:
        if not guidelines:
            return []
        unique: list[str] = []
        for guideline in list(guidelines):
            normalized = str(guideline).strip()
            if normalized and normalized not in unique:
                unique.append(normalized)
        return unique

    def _rebuildSystemPrompt(self, toolNames: list[str]) -> None:
        valid_tool_names = [name for name in toolNames if name in self._toolRegistry]
        tool_snippets: dict[str, str] = {}
        tool_guidelines: dict[str, list[str]] = {}
        hidden = getattr(self, "_hiddenDeclarations", frozenset())  # doubles built without __init__
        for name in self._toolRegistry:
            entry = self._toolDefinitions.get(name)
            if entry is None:
                continue
            # Tools without a snippet are not listed.
            if entry.promptSnippet:
                tool_snippets[name] = entry.promptSnippet
            if entry.promptGuidelines:
                tool_guidelines[name] = list(entry.promptGuidelines)

        loader_system_prompt = self._resourceLoader.getSystemPrompt()
        loader_append_system_prompt = self._resourceLoader.getAppendSystemPrompt()
        append_system_prompt = "\n\n".join(loader_append_system_prompt) if loader_append_system_prompt else ""
        # MISAKA fork: no `skills` option -- misaka's layered skill index (misaka/core/skills) replaces pi's
        # skills.ts and reaches the model through its own tools, not a prompt section.
        self._baseSystemPromptOptions = normalizeBuildSystemPromptOptions(
            {
                "cwd": self._cwd,
                "contextFiles": self._resourceLoader.getAgentsFiles()["agentsFiles"],
                "customPrompt": loader_system_prompt,
                "appendSystemPrompt": append_system_prompt,
                "selectedTools": valid_tool_names,
                "hiddenTools": list(hidden),
                "toolSnippets": tool_snippets,
                "toolGuidelines": tool_guidelines,
            }
        )

    def _preparePromptAndToolLoadout(
        self, options: dict[str, Any], messages: list[Any] | None = None
    ) -> SystemMessage | None:
        """Apply a prompt and tool loadout for the next request. Sets the executable tools and
        returns a system message patching the prompt sections the model currently has (replayed
        from `messages`), or None when the prompt is unchanged. Tool changes are declared by
        the agent loop before the request.

        A forced prompt does not affect the transcript: the structured sections are still diffed
        and persisted, and the forced text is projected onto the request by
        `_installAgentForcedPromptProjection`.
        """
        if messages is None:
            messages = self.agent.state.messages
        options["selectedTools"] = [tool.name for tool in self._applyToolLoadout(list(options["selectedTools"]))]
        # The tool list and rules must match the declarations the request carries.
        options["hiddenTools"] = list(self._hiddenDeclarations)
        current = getCurrentSystemMessage(messages)
        sections = diffSystemPromptSections(
            dict(current.sections or {}) if current else {}, buildSystemPromptSections(options)
        )
        return (
            SystemMessage(content="", sections=sections, timestamp=int(time.time() * 1000)) if sections else None
        )

    def _installHiddenDeclarationsProjection(self) -> None:
        """Remove the declarations that ``prepareLoadout`` hooks hide from every request. The whole
        transcript is filtered with the current set, so the projected declarations stay consistent
        across requests and only change when the loadout does."""
        previous_transform_context = self.agent.transformContext

        async def transform_context(messages: list[Any], signal: Any | None = None) -> list[Any]:
            transformed = (
                await previous_transform_context(messages, signal) if previous_transform_context else messages
            )
            hidden = self._hiddenDeclarations
            if not hidden:
                return transformed

            def project(message: Any) -> Any:
                tools_added = read_field(message, "toolsAdded")
                tools_removed = read_field(message, "toolsRemoved")
                if _message_role(message) != "system" or (not tools_added and not tools_removed):
                    return message
                added = [tool for tool in tools_added or [] if read_field(tool, "name") not in hidden]
                removed = [tool for tool in tools_removed or [] if read_field(tool, "name") not in hidden]
                if isinstance(message, dict):
                    projected = {key: value for key, value in message.items() if key not in ("toolsAdded", "toolsRemoved")}
                    if added:
                        projected["toolsAdded"] = added
                    if removed:
                        projected["toolsRemoved"] = removed
                    return projected
                return message.model_copy(update={"toolsAdded": added or None, "toolsRemoved": removed or None})

            return [project(message) for message in transformed]

        self.agent.transformContext = transform_context

    def _installAgentForcedPromptProjection(self) -> None:
        """Send a forced prompt as the provider's leading system prompt without recording it.

        A `before_agent_start` handler that returns `systemPrompt` needs that exact text at the
        head of the request; a mid-conversation system message would leave the original prompt
        in place. The forced text is a rendering of the current prompt, so the transcript keeps
        its structured sections and the request is projected instead: the system messages
        collapse into one head holding the forced text and the current tools. Runs after the
        `context` extension handlers.
        """
        previous_transform_context = self.agent.transformContext

        async def transform_context(messages: list[Any], signal: Any | None = None) -> list[Any]:
            transformed = (
                await previous_transform_context(messages, signal) if previous_transform_context else messages
            )
            forced = (
                self._runSystemPromptOptions.get("forceSystemPrompt")
                if self._runSystemPromptOptions is not None
                else None
            )
            if forced is None:
                return transformed
            current = getCurrentSystemMessage(transformed)
            head = SystemMessage(
                content=forced,
                toolsAdded=current.toolsAdded if current and current.toolsAdded else None,
                timestamp=current.timestamp if current else int(time.time() * 1000),
            )
            return [head, *(message for message in transformed if _message_role(message) != "system")]

        self.agent.transformContext = transform_context

    def _restoreToolsFromTranscript(self, modifiers: Sequence[str] = ()) -> None:
        """Restore the active tool loadout declared by the session transcript, if it declares one.
        Tools reachable only from other tools are never declared, but they do not depend on the
        active set, so the transcript's declarations are the whole loadout.

        `modifiers` are `+name`/`-name` entries applied to the restored loadout."""
        self._pendingToolNames.clear()
        current = getCurrentSystemMessage(self.sessionManager.buildSessionContext().messages)
        if not current:
            return
        names = applyToolModifiers([tool.name for tool in current.toolsAdded or []], modifiers)
        if self._toolScopes:
            # MISAKA fork: inside a tool scope (a research window) the transcript's loadout is the
            # scope's own; it must not become the user's lasting selection, nor wait as pending.
            self._setActiveTools(names)
            return
        self._pendingToolNames = dict.fromkeys(name for name in names if self._isAllowedTool(name))
        # The selection is set directly: the public `setActiveToolsByName` would drop the pending tools.
        # MISAKA fork: misaka's selection remembers names across registry refreshes, so the unregistered
        # ones wait as pending only; a tool that never registers is then dropped at the next run, as upstream.
        self._unscopedToolNames = [name for name in dict.fromkeys(names) if name in self._toolRegistry]
        self._setActiveTools(names)

    def _buildUserMessage(
        self,
        text: str,
        images: Sequence[ImageContent] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [TextContent(text=text).model_dump()]
        if images:
            content.extend(image.model_dump() if hasattr(image, "model_dump") else image for image in images)
        return {"role": "user", "content": content, "timestamp": int(time.time() * 1000)}

    def _extractUserMessageText(self, content: str | list[Any]) -> str:
        # `read_field` rather than `block.get`: a user message that came through
        # `Agent.prompt()` has been run through `validate_message`, so its blocks are
        # pydantic `TextContent`, not dicts, and the dict-only filter returned "" for
        # every one of them. Only the steer/followUp queue path (raw dicts built by
        # `_buildUserMessage`) still matches by text, so this was a silent zero rather
        # than a visible bug -- one normalization away from a queue that never drains.
        # pi has no such split: agent-session.ts:649 shares one `contentText` helper.
        if isinstance(content, str):
            return content
        return "".join(
            str(read_field(block, "text") or "")
            for block in content
            if read_field(block, "type") == "text"
        )

    def _installAgentToolHooks(self) -> None:
        """Install tool hooks once on the agent instance.

        The callbacks read ``self._extensionRunner`` at execution time, so extension reload swaps
        in the new runner without reinstalling hooks.
        """
        self.agent.beforeToolCall = lambda context, _signal=None: self._beforeToolCall(context)
        self.agent.afterToolCall = lambda context, _signal=None: self._afterToolCall(context)

    async def _beforeToolCall(self, payload: Any, parent_tool_call_id: str | None = None) -> Any:
        """``tool_call`` handlers. ``parent_tool_call_id`` is set for calls another tool made."""
        name = read_field(_event_field(payload, "toolCall"), "name", "")
        if self._toolAdmission is not None and not self._toolAdmission(name):
            return {"block": True, "reason": f"Tool {name!r} is outside the current agent tool policy."}
        if getattr(self, "_toolScopes", ()):
            name = read_field(_event_field(payload, "toolCall"), "name")
            # MISAKA fork: a nested call reaches callable tools, which need not be active.
            reachable = self.getCallableToolNames() if parent_tool_call_id else self.getActiveToolNames()
            if name not in reachable:
                return {"block": True, "reason": f"Tool {name!r} is outside the current tool scope."}
        runner = self._extensionRunner
        # MISAKA fork: the parts are asked first; a block from them is final.
        if not self.moments.parts and not runner.hasHandlers("tool_call"):
            return None
        tool_call = _event_field(payload, "toolCall")
        args = _event_field(payload, "args")
        event = {
            "type": "tool_call",
            "toolName": read_field(tool_call, "name"),
            "toolCallId": read_field(tool_call, "id"),
            **({"parentToolCallId": parent_tool_call_id} if parent_tool_call_id else {}),
            "input": args,
        }
        result = await self.moments.tool_call(event)
        if _event_field(result, "block", False) or not runner.hasHandlers("tool_call"):
            return result
        extension_result = await runner.emitToolCall(event)  # type: ignore[attr-defined]
        if extension_result and _event_field(result, "updatedInput") is not None:
            return {"block": _event_field(extension_result, "block", False),
                    "reason": _event_field(extension_result, "reason"),
                    "terminate": _event_field(extension_result, "terminate"),
                    "updatedInput": _event_field(result, "updatedInput")}
        return extension_result if extension_result else result

    async def _afterToolCall(self, payload: Any, parent_tool_call_id: str | None = None) -> Any:
        """``tool_result`` handlers and image normalization. ``parent_tool_call_id`` is set for
        calls another tool made."""
        runner = self._extensionRunner
        result = _event_field(payload, "result")
        tool_call = _event_field(payload, "toolCall")
        structured_content = _event_field(result, "structuredContent")
        hook_result = None
        if self.moments.parts or runner.hasHandlers("tool_result"):
            event = {
                "type": "tool_result",
                "toolName": read_field(tool_call, "name"),
                "toolCallId": read_field(tool_call, "id"),
                **({"parentToolCallId": parent_tool_call_id} if parent_tool_call_id else {}),
                "input": _event_field(payload, "args"),
                "content": _event_field(result, "content"),
                "details": _event_field(result, "details"),
                **({"structuredContent": structured_content} if structured_content is not None else {}),
                "isError": bool(_event_field(payload, "isError")),
                "usage": _event_field(result, "usage"),
            }
            # MISAKA fork: the parts rewrite the result first; extensions see their version.
            hook_result = await self.moments.tool_result(event)
            if hook_result is not None:
                event.update(hook_result)
                if event.get("structuredContent") is None:
                    event.pop("structuredContent", None)
            if runner.hasHandlers("tool_result"):
                extension_result = await runner.emitToolResult(event)  # type: ignore[attr-defined]
                if extension_result is not None:
                    hook_result = extension_result

        result_content = _event_field(result, "content")
        content = (
            _event_field(hook_result, "content")
            if hook_result is not None
            else result_content
        )
        if content is None:
            content = result_content
        if content is None:
            content = []
        # Runs after the extension hook so images injected or replaced by extensions are normalized too.
        normalized_content = await normalizeToolResultImages(
            content,
            NormalizeToolResultImagesOptions(
                autoResizeImages=self.settingsManager.getImageAutoResize(),
                resizeOptions=self._modelResizeOptions(),
            ),
        )

        if hook_result is None and normalized_content is content:
            return None

        hook_is_error = (
            _event_field(hook_result, "isError")
            if hook_result is not None
            else None
        )
        return {
            "content": normalized_content,
            "details": (
                _event_field(hook_result, "details")
                if hook_result is not None
                else None
            ),
            # The hook result already dropped structured content that replaced content no longer matches.
            "structuredContent": (
                _event_field(hook_result, "structuredContent") if hook_result is not None else structured_content
            ),
            "isError": (
                hook_is_error
                if hook_is_error is not None
                else _event_field(payload, "isError")
            ),
            # None here means "hook did not set it"; the agent loop falls back to the
            # executed result's usage (pi agent-session.ts:537 usage: hookResult?.usage).
            "usage": (
                _event_field(hook_result, "usage")
                if hook_result is not None
                else None
            ),
            "terminate": _event_field(hook_result, "terminate") if hook_result is not None else None,
        }

    async def _executeNestedToolCall(
        self, parent_tool_call_id: str, name: str, args: Any, options: ExecuteToolOptions | None = None
    ) -> AgentToolCallOutcome:
        """Run a call that the tool call ``parent_tool_call_id`` made through
        ``ctx.executeTool()``. It goes through the agent's tool pipeline with the session's hooks,
        against the callable tools."""
        # MISAKA fork: a tool run by another Agent built from this session's tools (a verifier
        # with its own filter and hooks) would otherwise reach the session's callable tools
        # through the session's hooks. Only calls the session's agent issued may nest.
        caller_root = parent_tool_call_id.split("/", 1)[0]
        issuing_message = self._findLastAssistantMessage()
        issued_ids = {
            read_field(block, "id")
            for block in (issuing_message.content if issuing_message is not None else [])
            if read_field(block, "type") == "toolCall"
        }
        if caller_root not in issued_ids:
            return AgentToolCallOutcome(
                toolCall=ToolCall.model_construct(
                    type="toolCall", id=f"{parent_tool_call_id}/0", name=name, arguments=args if args is not None else {}
                ),
                result=AgentToolResult(
                    content=[TextContent(text="Nested tool calls are not available in this context")], details={}
                ),
                isError=True,
            )
        if self._nestedToolCalls is None:
            session = self

            class _Host:
                def getTools(self) -> list[AgentTool]:
                    return session._getCallableTools()

                def isSequential(self) -> bool:
                    return session.agent.toolExecution == "sequential"

                async def runToolCall(
                    self, tool_call: Any, parent_id: str, signal: Any, on_update: Any
                ) -> AgentToolCallOutcome:
                    assistant_message = session._findLastAssistantMessage()
                    if assistant_message is None:
                        return AgentToolCallOutcome(
                            toolCall=tool_call,
                            result=AgentToolResult(
                                content=[TextContent(text="No assistant message issued this call")], details={}
                            ),
                            isError=True,
                        )
                    return await runToolCall(
                        tool_call,
                        RunToolCallOptions(
                            tools=session._getCallableTools(),
                            assistantMessage=assistant_message,
                            context=AgentContext(messages=session.agent.state.messages, tools=session.agent.state.tools),
                            beforeToolCall=lambda context, _signal=None: session._beforeToolCall(context, parent_id),
                            afterToolCall=lambda context, _signal=None: session._afterToolCall(context, parent_id),
                            signal=signal,
                            onUpdate=on_update,
                        ),
                    )

                async def emit(self, event: Any) -> None:
                    await session._extensionRunner.emit(_tool_execution_extension_event(event))
                    session._emit(event)

            self._nestedToolCalls = NestedToolCallRunner(_Host())
        return await self._nestedToolCalls.execute(
            parent_tool_call_id,
            name,
            args,
            NestedToolCallOptions(signal=read_field(options, "signal"), onUpdate=read_field(options, "onUpdate")),
        )

    def _hasContextEngine(self) -> bool:
        runner = getattr(self, "_extensionRunner", None)
        return bool(runner is not None and runner.hasHandlers("session_context_prepare")) or any(
            callable(getattr(part, "session_context_prepare", None))
            for part in getattr(getattr(self, "moments", None), "parts", ()))

    async def prepareContextMessages(self, messages, signal=None):
        """Engine preflight before EVERY provider request, before transient context hooks.

        prepareNextTurn runs only after a completed assistant/tool turn. It misses the
        initial request and newly drained steering, so it is not the engine boundary.
        Only the durable engine publication mutates the loop's source list here.
        """
        if self._hasContextEngine():
            before = self.agent.state.messages
            await self._runAutoCompaction(
                "threshold", False, preflight=True,
                current_tokens=estimateContextTokens(messages).tokens,
                parent_signal=signal)
            if self.agent.state.messages is not before:
                messages[:] = self.agent.state.messages
        return messages

    def _exceedsCompactionThreshold(self, model: Model[Any], projection: SessionProjection) -> bool:
        """Whether `projection`, the current session projection, exceeds the compaction threshold of `model`."""
        if model.contextWindow <= 0:
            return False
        return shouldCompact(
            estimateProjectedContextTokens(projection, self.sessionManager.getBranch()).tokens,
            model.contextWindow,
            CompactionSettings(**self.settingsManager.getCompactionSettings(self.model)),
        )

    async def _compactBeforeNextAssistantResponse(
        self,
        context: AgentContext,
    ) -> AgentContext:
        projection = self.sessionManager.buildSessionProjection()
        if self._hasContextEngine():
            # MISAKA fork: engine preparation runs at the provider boundary.
            return replace(context, messages=list(projection.messages))
        # A virtual selection is checked in prepareRequest, against the model the request is routed to.
        model = self.model
        if model is None or isVirtualModel(model) or not self._exceedsCompactionThreshold(model, projection):
            return replace(context, messages=list(projection.messages))
        await self._runAutoCompaction("threshold", False)
        return replace(context, messages=list(self.sessionManager.buildSessionProjection().messages))

    def _installAgentRequestProjection(self) -> None:
        previous_prepare_request = self.agent.prepareRequest

        async def prepare_request(request: PrepareRequestContext, signal: Any | None = None) -> AgentRequestUpdate:
            failed = self._failedResponse
            self._failedResponse = None

            async def prepare() -> tuple[Any, AgentContext, SessionProjection]:
                projection = self.sessionManager.buildSessionProjection()
                canonical_context = replace(
                    request.context,
                    messages=list(projection.messages),
                    # Messages declare the provider-visible loadout; context.tools keeps executable implementations.
                    tools=self.agent.state.tools[:],
                )
                previous: Any = None
                if previous_prepare_request is not None:
                    previous = previous_prepare_request(
                        replace(
                            request,
                            context=canonical_context,
                            model=self.agent.state.model,
                            thinkingLevel=self.agent.state.thinkingLevel,
                        ),
                        signal,
                    )
                    if inspect.isawaitable(previous):
                        previous = await previous
                return previous, read_field(previous, "context") or canonical_context, projection

            previous, context, projection = await prepare()
            model = read_field(previous, "model") or self.agent.state.model
            thinking_level = read_field(previous, "thinkingLevel") or self.agent.state.thinkingLevel
            if not isVirtualModel(model):
                self._requestModel = model
                return AgentRequestUpdate(context=context, model=model, thinkingLevel=thinking_level)

            # The selection stays in agent state; only this request uses the routed model. A routing
            # failure raises, which ends the run with an error response. Only messages the user wrote
            # start a turn; extension messages can follow them, e.g. from before_agent_start.
            last_response = max(
                (index for index, message in enumerate(context.messages) if _message_role(message) == "assistant"),
                default=-1,
            )
            user_turn = any(_message_role(message) == "user" for message in context.messages[last_response + 1 :])
            state = getVirtualModelState(self.sessionManager.getBranch(), model.provider, model.id)
            route = await self._modelRuntime.resolveModel(model, convertToLlm(context.messages), {"reason": "retry" if failed is not None else "user" if user_turn else "continuation", "thinkingLevel": thinking_level, "signal": signal, "failed": failed, "state": state})
            if route.state is not None and route.state is not state:
                data: VirtualModelStateData = {"provider": model.provider, "modelId": model.id, "state": route.state}
                entry = self.sessionManager.getEntry(
                    self.sessionManager.appendCustomEntry(VIRTUAL_MODEL_STATE_ENTRY, data)
                )
                if entry is not None:
                    self._emit({"type": "entry_appended", "entry": entry})
            # The route stands: the router already decided this request. The state entry does not change
            # the projection. MISAKA fork: a context engine prepares the context at the provider
            # boundary instead, sized from the routed model (`_requestModel`).
            if not self._hasContextEngine() and self._exceedsCompactionThreshold(route.model, projection):
                await self._runAutoCompaction("threshold", False)
                previous, context, _ = await prepare()
            self._requestModel = route.model
            return AgentRequestUpdate(context=context, model=route.model, thinkingLevel=route.thinkingLevel)

        self.agent.prepareRequest = prepare_request

    async def _dispatchTurnEndBoundary(self, message: Any, tool_results: list[Any]) -> bool:
        stop_reason = read_field(message, "stopReason")
        self._lastActivityOutcome = (
            "aborted" if stop_reason == "aborted" else "error" if stop_reason == "error" else "completed"
        )
        message_entry_id = self._findPersistedMessageEntryId(message)
        if not self._extensionRunner.hasHandlers("turn_end"):
            return False
        if not message_entry_id:
            self._extensionRunner.emitError(
                ExtensionError(
                    extensionPath="<boundary>",
                    event="turn_end",
                    error="turn_end could not resolve the persisted assistant entry ID",
                )
            )
            return False
        tool_result_entry_ids = [
            entry_id
            for entry_id in (self._findPersistedMessageEntryId(result) for result in tool_results)
            if entry_id
        ]
        boundary = await self._extensionRunner.emitBoundary(
            {
                "type": "turn_end",
                "turnIndex": self._turnIndex,
                "message": message,
                "toolResults": tool_results,
                "messageEntryId": message_entry_id,
                "toolResultEntryIds": tool_result_entry_ids,
                "outcome": self._lastActivityOutcome,
            },
            lambda entries: self._buildBoundaryContext(entries, "turn_end"),
        )
        self._commitBoundaryDrafts(boundary["entries"])
        if boundary["continue"] and not self._buildBoundaryContext([], "turn_end")["canContinue"]:
            self._reportInvalidBoundaryContinuation("turn_end")
            return False
        return bool(boundary["continue"])

    def _installAgentBoundaryHooks(self) -> None:
        previous_finish_turn = self.agent.finishTurn

        async def finish_turn(turn: AgentTurnContext, signal: Any | None = None) -> AgentTurnDecision | None:
            self._boundaryDispatchedMessages.add(id(turn.message))
            extension_continue = await self._dispatchTurnEndBoundary(turn.message, turn.toolResults)
            previous_decision: Any = None
            if previous_finish_turn is not None:
                previous_decision = previous_finish_turn(turn, signal)
                if inspect.isawaitable(previous_decision):
                    previous_decision = await previous_decision
            if read_field(previous_decision, "action") == "end":
                return previous_decision
            if extension_continue or read_field(previous_decision, "action") == "continue":
                return AgentTurnDecision(action="continue")
            return None

        self.agent.finishTurn = finish_turn

    def _installAgentNextTurnRefresh(self) -> None:
        """Refresh mutable session state before each request in one agent run."""
        previous_with_context = self.agent.prepareNextTurnWithContext
        previous_legacy = self.agent.prepareNextTurn

        async def prepare_next_turn(
            turn: PrepareNextTurnContext,
            signal: Any | None = None,
        ) -> AgentLoopTurnUpdate:
            context = await self._compactBeforeNextAssistantResponse(turn.context)
            previous_snapshot: Any = None
            if previous_with_context is not None:
                previous_snapshot = previous_with_context(replace(turn, context=context), signal)
            elif previous_legacy is not None:
                previous_snapshot = previous_legacy(signal)
            if inspect.isawaitable(previous_snapshot):
                previous_snapshot = await previous_snapshot

            previous_context = read_field(previous_snapshot, "context")
            next_context = previous_context if previous_context is not None else context
            run_options = (
                self._runSystemPromptOptions
                if self._runSystemPromptOptions is not None
                else self._baseSystemPromptOptions
            )
            options = normalizeBuildSystemPromptOptions(
                {
                    **run_options,
                    "selectedTools": self.getActiveToolNames(),
                    "toolSnippets": {**self._baseSystemPromptOptions["toolSnippets"], **run_options["toolSnippets"]},
                    "toolGuidelines": {
                        **self._baseSystemPromptOptions["toolGuidelines"],
                        **run_options["toolGuidelines"],
                    },
                }
            )
            next_messages = list(read_field(next_context, "messages", []) or [])
            update_message = self._preparePromptAndToolLoadout(options, next_messages)
            # Keep session.systemPrompt and ctx.getSystemPrompt() in step with what the provider sees.
            self._runSystemPromptOptions = options
            previous_messages = list(read_field(previous_snapshot, "messages", None) or [])
            return AgentLoopTurnUpdate(
                context=AgentContext(
                    messages=next_messages,
                    tools=self.agent.state.tools[:],
                ),
                messages=(
                    [*previous_messages, update_message]
                    if update_message is not None
                    else (previous_messages or None)
                ),
                model=self.agent.state.model,
                thinkingLevel=self.agent.state.thinkingLevel,
            )

        self.agent.prepareNextTurnWithContext = prepare_next_turn

    # =========================================================================
    # Canonical context and boundaries
    # =========================================================================
    def _refreshFinalizedContext(self) -> None:
        projection = self.sessionManager.buildSessionProjection()
        self._entryIdsByMessage = {}
        for entry in projection.entries:
            for message in entry.messages:
                self._entryIdsByMessage[id(message)] = (message, str(entry.sourceEntry.get("id")))
        self.agent.state.messages = projection.messages

    def _applyBoundaryDrafts(self, manager: SessionManager, drafts: list[Any]) -> list[Any]:
        appended: list[Any] = []
        for draft in drafts:
            draft_type = read_field(draft, "type")
            if draft_type == "custom":
                entry_id = manager.appendCustomEntry(read_field(draft, "customType"), read_field(draft, "data"))
            elif draft_type == "custom_message":
                entry_id = manager.appendCustomMessageEntry(
                    read_field(draft, "customType"),
                    read_field(draft, "content"),
                    bool(read_field(draft, "display")),
                    read_field(draft, "details"),
                )
            elif draft_type == "context_edit":
                entry_id = manager.appendContextEdit(read_field(draft, "targetId"), read_field(draft, "replacement"))
            elif draft_type == "compaction":
                tokens_before = estimateProjectedContextTokens(
                    manager.buildSessionProjection(), manager.getBranch()
                ).tokens
                entry_id = manager.appendCompaction(
                    read_field(draft, "summary"),
                    read_field(draft, "firstKeptEntryId"),
                    tokens_before,
                    read_field(draft, "details"),
                    True,
                    read_field(draft, "usage"),
                )
            else:
                raise ValueError(f"Unsupported boundary entry type: {draft_type}")
            entry = manager.getEntry(entry_id)
            if entry:
                appended.append(entry)
        return appended

    def _createBoundaryPreviewManager(self, drafts: list[Any]) -> SessionManager:
        header = self.sessionManager.getHeader()
        if not header:
            raise RuntimeError("Session header is missing")
        manager = SessionManager.inMemory(self._cwd)
        manager.fileEntries = [header, *self.sessionManager.getBranch()]
        manager._buildIndex()
        self._applyBoundaryDrafts(manager, drafts)
        return manager

    def _getPendingBoundaryMessages(self) -> list[Any]:
        return [*self.agent.peekQueuedMessages(), *self._pendingCustomMessages]

    def _buildBoundaryContext(self, drafts: list[Any], boundary: str) -> dict[str, Any]:
        projection = self._createBoundaryPreviewManager(drafts).buildSessionProjection()
        pending_messages = self._getPendingBoundaryMessages()
        llm_messages = convertToLlm(projection.messages)
        final_role = _message_role(llm_messages[-1]) if llm_messages else None
        has_non_system_context = any(_message_role(message) != "system" for message in llm_messages)
        context_can_continue = has_non_system_context and final_role != "assistant"
        pending_custom_context = len(self._pendingCustomMessages) > 0
        return {
            "contextEntries": projection.entries,
            "contextMessages": projection.messages,
            "llmMessages": llm_messages,
            "pendingMessages": pending_messages,
            "canContinue": (
                context_can_continue
                or pending_custom_context
                or (
                    self.agent.hasQueuedMessages()
                    if boundary == "turn_end"
                    else final_role == "assistant" and self.agent.hasQueuedMessages()
                )
            ),
        }

    def _commitBoundaryDrafts(self, drafts: list[Any]) -> None:
        appended = self._applyBoundaryDrafts(self.sessionManager, drafts)
        self._refreshFinalizedContext()
        for entry in appended:
            self._emit({"type": "entry_appended", "entry": entry})

    def _reportInvalidBoundaryContinuation(self, event: str) -> None:
        self._extensionRunner.emitError(
            ExtensionError(
                extensionPath="<boundary>",
                event=event,
                error=f"{event} requested continuation without runnable model context",
            )
        )

    def _findPersistedMessageEntryId(self, message: Any) -> str | None:
        mapped = self._entryIdsByMessage.get(id(message))
        if mapped is not None and mapped[0] is message:
            return mapped[1]
        for entry in reversed(self.sessionManager.getBranch()):
            if entry.get("type") == "message" and entry.get("message") is message:
                return str(entry.get("id"))
        message_index = next((i for i, m in enumerate(self.agent.state.messages) if m is message), -1)
        if message_index < 0:
            return None
        projection = self.sessionManager.buildSessionProjection()
        projected_index = 0
        for entry in projection.entries:
            for _ in entry.messages:
                if projected_index == message_index:
                    entry_id = str(entry.sourceEntry.get("id"))
                    self._entryIdsByMessage[id(message)] = (message, entry_id)
                    return entry_id
                projected_index += 1
        return None

    def _omitRecoveryAttempt(self, message: Any, tool_results: list[Any] | None = None) -> None:
        targets = [message, *(tool_results or [])]
        target_ids = [self._findPersistedMessageEntryId(target) for target in targets]
        unresolved_projected_target = any(
            target_ids[index] is None and any(m is target for m in self.agent.state.messages)
            for index, target in enumerate(targets)
        )
        if unresolved_projected_target:
            raise RuntimeError("Cannot persist recovery omission because a projected message has no source entry")
        for target_id in target_ids:
            if not target_id:
                continue
            edit_id = self.sessionManager.appendContextEdit(target_id, None)
            entry = self.sessionManager.getEntry(edit_id)
            if entry:
                self._emit({"type": "entry_appended", "entry": entry})
        self._refreshFinalizedContext()

    async def _startRun(
        self,
        trigger: BeforeAgentStartTrigger,
        onStarted: Callable[[], None] | None = None,
        *,
        admit: Callable[[], None] | None = None,
    ) -> None:
        """Start a run from a user or custom message. The session counts as busy from here on, so
        messages sent while before_agent_start handlers run are queued into this run instead of
        starting another, and an abort requested meanwhile aborts the run as soon as it exists.

        `onStarted` is called once the run's messages are prepared, before the first request.
        MISAKA fork: `admit` is called once the hooks ran, before the run takes its messages; a
        failure there undoes the start like a failing hook. The busy flag is set before the first
        await, so a caller that awaits this directly owns the session from that point (a research
        window's phase)."""
        self._isAgentRunActive = True
        self._agentRunAbortRequested = False
        try:
            messages = await self._prepareRun(trigger, admit)
        except BaseException as error:
            self._isAgentRunActive = False
            self._flushPendingBashMessages()
            self._flushPendingCustomMessages()
            self._handBackQueuedReceipts(error)
            if isinstance(error, _CustomRunBlocked):
                # MISAKA fork: a hook that blocks a run a custom message starts does not lose the
                # message. It is recorded, and its receipt acknowledged, without a turn: failing it
                # would have the durable outbox redeliver it into the same block indefinitely.
                try:
                    self._appendCustomMessage(trigger["message"])
                    self._refreshFinalizedContext()
                    self._emit({"type": "message_start", "message": trigger["message"]})
                    self._emit({"type": "message_end", "message": trigger["message"]})
                finally:
                    self._settleAgentRun()
                return
            self._settleAgentRun()
            raise
        # Messages held while the run was starting go before its starting messages, as they would
        # have while idle. No run exists yet, so they cannot split a tool call from its result.
        self._flushPendingBashMessages()
        self._flushPendingCustomMessages()
        if onStarted is not None:
            onStarted()

        # Compaction before the prompt may have scheduled a retry; the new prompt replaces it.
        self._failedResponse = None
        self._recordSelection()
        # The run records the loadout in the transcript; restored tools that did not register by now
        # are dropped, so a tool that never registers does not stay pending.
        self._pendingToolNames.clear()
        try:
            await self.agent.prompt(messages)
            while not self._agentRunAbortRequested:
                if await self._handlePostAgentRun():
                    if self._agentRunAbortRequested:
                        break
                    await self.agent.continue_()
                    continue
                if self._agentRunAbortRequested or not await self._runBeforeSettleBoundary():
                    break
                if self._agentRunAbortRequested:
                    break
                await self.agent.continue_()
        finally:
            if self._agentRunAbortRequested:
                self._finishCancelledRetry()
            self._failedResponse = None
            self._runSystemPromptOptions = None
            self._flushPendingBashMessages()
            self._flushPendingCustomMessages()
            await self._emitAgentSettled()

    def _handBackQueuedReceipts(self, error: BaseException) -> None:
        """MISAKA fork: custom messages whose sender redelivers them (a receipt with `_onError`: the Sister
        outbox, mail) that were queued into a run that then did not start (a hook blocked it, or preparing it
        failed) leave the Agent's queues and are failed back to that sender, which redelivers each into a fresh
        run. Left queued they would wait for some unrelated later run, and every redelivery would meet the receipt
        (`sendCustomMessage`) and be dropped. Every other message stays queued for the next run, as upstream leaves
        it: one without a receipt, or with a receipt nobody redelivers (a sub-agent's or a background shell's
        completion notice, `_onPersist` only) -- failing that one would lose it for good."""

        def receipt_of(message: Any) -> str | None:
            details = read_field(message, "details")
            delivery_id = details.get("delivery_id") if isinstance(details, dict) else None
            receipt = self._customMessageReceipts.get(delivery_id) if delivery_id else None
            return delivery_id if receipt is not None and receipt.get("_onError") is not None else None

        taken = self.agent.takeQueuedMessages(
            lambda message: read_field(message, "role") == "custom" and receipt_of(message) is not None
        )
        if not taken:
            return
        reason = RuntimeError(f"The run this message was queued into did not start: {error}")
        for message in taken:
            self._failCustomReceipt(receipt_of(message), reason)

    async def _prepareRun(
        self, trigger: BeforeAgentStartTrigger, admit: Callable[[], None] | None = None
    ) -> list[AgentMessage]:
        """Prepare a run and return its starting messages: compact first when needed, emit
        before_agent_start, apply the run's prompt options and tool loadout, and build the starting
        message followed by pending nextTurn messages (consumed here) and handler messages, with a
        system patch first when the prompt changed."""
        # Check if we need to compact before sending (catches aborted responses).
        # The starting message is sent below, so do not continue the agent here
        # (pi agent-session.ts:1230-1234): continuing would emit a whole agent run —
        # agent_start / turn_* / agent_end — that the user never asked for.
        last_assistant = self._findLastAssistantMessage()
        if last_assistant is not None:
            await self._checkCompaction(last_assistant, False)

        contributed, options = await self._emitBeforeAgentStart(trigger)

        messages: list[Any] = []
        if trigger["messageType"] == "custom":
            messages.append(trigger["message"])
        else:
            # Normalize images after before_agent_start so extension-driven model selection
            # determines the resize profile used for the request and history.
            normalized_images, hints = await self._normalizePromptImages(trigger.get("images"))
            text = f"{trigger['prompt']}\n\n" + "\n".join(hints) if hints else trigger["prompt"]
            messages.append(self._buildUserMessage(text, normalized_images))
        if admit is not None:
            admit()
        # Inject any pending "nextTurn" messages as context alongside the starting message
        pending_next_turn = self._pendingNextTurnMessages
        messages.extend(pending_next_turn)
        self._pendingNextTurnMessages = []
        try:
            for message in contributed:
                normalized_message = _message_dict(message)
                messages.append(
                    _normalize_nullish_message_content(
                        {
                            "role": "custom",
                            "customType": read_field(normalized_message, "customType"),
                            # Untyped extensions can pass null/missing content; normalize at ingestion.
                            "content": _message_content(normalized_message),
                            "display": bool(read_field(normalized_message, "display")),
                            "details": read_field(normalized_message, "details"),
                            "timestamp": int(time.time() * 1000),
                        }
                    )
                )
            update_message = self._preparePromptAndToolLoadout(options)
        except BaseException:
            # MISAKA fork: the run did not start; its notifications wait for the next one.
            self._pendingNextTurnMessages = [*pending_next_turn, *self._pendingNextTurnMessages]
            raise
        self._runSystemPromptOptions = options
        if update_message is not None:
            messages.insert(0, update_message)
        return messages

    def _settleAgentRun(self) -> None:
        """Release everyone waiting on idle, but only if the session really is idle.

        pi's ``_resolveIdleWaitIfIdle`` (agent-session.ts:599-607): it reads the run flag,
        it never clears it.  The guard matters because an ``agent_settled`` handler may
        start the next run while ``_emitAgentSettled`` is still awaiting handlers -- the
        finally then runs with a fresh run in flight, and clearing the flag there would
        report an active run as idle and wake ``waitForIdle`` callers early.
        """
        if self._isAgentRunActive:
            return
        # MISAKA fork: a tool scope that ended while a run was working records the loadout it gave
        # back now that no run can still declare it (see `toolScope`).
        if self._recordLoadoutAfterRun:
            self._recordLoadoutAfterRun = False
            self._recordUnscopedLoadout()
        waiters, self._idleWaiters = self._idleWaiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _emitAgentSettled(self) -> None:
        """Announce that the run has fully settled (pi agent-session.ts:609-616,1094-1096).

        Reached only once the retry / auto-compaction / queued-continuation loop in
        ``_handlePostAgentRun`` is done, so this is the "nothing more will run
        automatically" signal that ``agent_end`` is not.  pi clears the run flag before the
        handlers observe it and releases the idle waiters in a finally, so a handler that
        asks ``ctx.isIdle()`` gets the truth and a crashing handler still unblocks waiters.
        """
        if self._cacheWarmer is not None:
            self._cacheWarmer.onAgentSettled()
        self._isAgentRunActive = False
        self._isEmittingAgentSettled = True
        try:
            aborted = self._agentRunAbortRequested
            await self._extensionRunner.emit({"type": "agent_settled", "aborted": aborted})
            if not self._isAgentRunActive:
                await self.moments.agent_settled({"type": "agent_settled", "aborted": aborted})
            self._emit({"type": "agent_settled", "aborted": aborted})
        finally:
            self._isEmittingAgentSettled = False
        # Runs requested from `agent_settled` handlers are deferred until every settled handler
        # finished: they still observe `ctx.isIdle() === true`, but no longer see a reentrant
        # `agent_start` during the same notification dispatch.
        deferred, self._deferredSettledActions = self._deferredSettledActions, []
        if deferred and _beingCancelled():
            # MISAKA fork: this settle is the cleanup of a cancelled run (its `_startRun` finally): no new run
            # starts inside the cancellation. The deferred routes are handed back instead (`_handBackDeferred`).
            try:
                self._handBackDeferred(deferred)
            finally:
                self._settleAgentRun()
            return
        if deferred:
            try:
                # MISAKA fork: every deferred action runs even when an earlier one fails, and the first error is
                # raised after them. Upstream stops at the failure; here a skipped route keeps its delivery
                # receipt, so its outbox redeliveries are dropped as duplicates and the notification is lost.
                # Each failure is logged, so the ones after the first are not lost either. A `CancelledError` the
                # route raised itself is such a failure. A cancellation of this task stops the loop at once: the
                # routes not run yet are handed back, so none keeps its receipt held.
                failure: BaseException | None = None
                for index, action in enumerate(deferred):
                    try:
                        await action()
                    except Exception as error:  # re-raised below, after the rest ran
                        logging.getLogger(__name__).warning("a run deferred past agent_settled failed", exc_info=True)
                        if failure is None:
                            failure = error
                    except asyncio.CancelledError as error:
                        if _beingCancelled():
                            self._handBackDeferred(deferred[index + 1:])
                            raise
                        logging.getLogger(__name__).warning("a run deferred past agent_settled failed", exc_info=True)
                        if failure is None:
                            failure = error
                if failure is not None:
                    raise failure
            finally:
                self._settleAgentRun()
            return
        self._settleAgentRun()

    def _handBackDeferred(self, actions: list[Callable[[], Awaitable[None]]]) -> None:
        """MISAKA fork: actions deferred past `agent_settled` that a cancellation keeps from running. A delivery is
        handed back as its sender expects (`_keepOrFailCustomMessage`: the Sister outbox and the mail inbox
        redeliver; a notice nobody redelivers waits in the follow-up queue for the next run); a deferred prompt is
        dropped with the cancellation, as upstream drops it."""
        if not actions:
            return
        reason = RuntimeError("A cancellation kept this delivery's run from starting")
        handed = 0
        for action in actions:
            handBack = getattr(action, "handBack", None)
            if handBack is not None:
                handBack(reason)
                handed += 1
        logging.getLogger(__name__).warning(
            "a cancellation kept %d run(s) deferred past agent_settled from starting; %d delivery(ies) handed back",
            len(actions), handed)

    async def _handlePostAgentRun(self) -> bool:
        message = self._lastAssistantMessage
        tool_results = self._lastAssistantToolResults
        self._lastAssistantMessage = None
        self._lastAssistantToolResults = []
        if self._agentRunAbortRequested:
            self._finishCancelledRetry()
            return False
        if message is None:
            return self.agent.hasQueuedMessages()

        if self._stopHookContinuationPending:
            self._stopHookContinuationPending = False
            return True

        # MISAKA fork: a credential the provider refused is replaced once, and the turn is retried
        # with the replacement. pi ends the turn here -- a 401 is not retryable and auth is resolved
        # on a clock, never on a rejection -- which is what left four cards' first request dead after
        # another client rotated the OAuth token they shared, until a person restarted each one by
        # hand (2026-09-18, B7).
        if (self._isRetryableError(message) or await self._recoverRejectedCredential(message)) \
                and await self._prepareRetry(message):
            if self._agentRunAbortRequested:
                self._finishCancelledRetry()
            self._failedResponse = message
            return not self._agentRunAbortRequested
        if self._agentRunAbortRequested:
            self._finishCancelledRetry()
            return False

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

        if await self._checkCompaction(message, True, tool_results):
            return not self._agentRunAbortRequested

        # The low-level loop drains both queues before agent_end. Messages queued by
        # agent_end handlers (including a Sister/Last Order completion) require a fresh
        # run before pre-settlement handlers fire.
        return not self._agentRunAbortRequested and self.agent.hasQueuedMessages()

    async def _runBeforeSettleBoundary(self) -> bool:
        if not self._extensionRunner.hasHandlers("agent_before_settle"):
            return self.agent.hasQueuedMessages()
        self._isBeforeSettle = True
        self._abortDuringBeforeSettle = False
        try:
            result = await self._extensionRunner.emitBoundary(
                {"type": "agent_before_settle", "outcome": self._lastActivityOutcome},
                lambda entries: self._buildBoundaryContext(entries, "agent_before_settle"),
            )
            self._commitBoundaryDrafts(result["entries"])
            self._flushPendingCustomMessages()
            final_context = self._buildBoundaryContext([], "agent_before_settle")
            if self._abortDuringBeforeSettle:
                return False
            should_continue = bool(result["continue"]) or self.agent.hasQueuedMessages()
            if should_continue and not final_context["canContinue"]:
                if result["continue"]:
                    self._reportInvalidBoundaryContinuation("agent_before_settle")
                return False
            return should_continue
        finally:
            self._isBeforeSettle = False

    def _finishCancelledRetry(self) -> None:
        if self._retryAttempt == 0:
            return
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

    def _willRetryAfterAgentEnd(self, event: Any) -> bool:
        if self._agentRunAbortRequested:
            return False
        if self._stopHookContinuationPending:
            return True
        settings = self.settingsManager.getRetrySettings()
        if not bool(settings.get("enabled")) or self._retryAttempt >= int(settings.get("maxRetries", 0) or 0):
            return False

        for message in reversed(list(_event_field(event, "messages", []) or [])):
            assistant_message = _as_assistant_message(message)
            if assistant_message is not None:
                return self._isRetryableError(assistant_message)
        return False

    def _summarizationRetryCallbacks(self, source: dict[str, str]) -> RetryCallbacks:
        return RetryCallbacks(
            onRetryScheduled=lambda attempt, max_attempts, delay_ms, error_message: self._emit(
                {
                    "type": "summarization_retry_scheduled",
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "delayMs": delay_ms,
                    "errorMessage": error_message,
                }
            ),
            onRetryAttemptStart=lambda: self._emit(
                {"type": "summarization_retry_attempt_start", **source}
            ),
            onRetryFinished=lambda _success, _attempt, _final_error=None: self._emit(
                {"type": "summarization_retry_finished"}
            ),
        )

    def _summarizationRetryPolicy(self) -> RetryPolicy:
        """The retry budget summarization borrows from agent turns (pi #6647).

        pi threads the same `settings.retry` into summarization so one transient stream
        drop no longer throws away a whole compaction or branch summary.
        """
        return self._retryPolicyFrom(self.settingsManager.getRetrySettings())

    async def _recoverRejectedCredential(self, message: AssistantMessage) -> bool:
        """MISAKA fork: replace a stored credential the provider has just refused. True when it
        changed, so the turn is worth running again. The provider is the one that answered this
        message, not the session's current model. One rejected token is replaced once per turn;
        a second refusal of the same one is the account's answer and is reported as it stands."""
        if message.stopReason != "error" or not getattr(message, "provider", None):
            return False
        if not is_rejected_credential_error(message.errorMessage):
            return False
        # Rotating a token is worth doing once per turn, and only when a retry can follow it:
        # `_retryAttempt` is zero only on a turn's first failure.
        settings = self.settingsManager.getRetrySettings()
        if (self._retryAttempt != 0 or not bool(settings.get("enabled"))
                or int(settings.get("maxRetries", 0) or 0) < 1):
            return False
        try:
            return bool(await self._modelRuntime.recoverRejectedCredential(message.provider, self.sessionId))
        except Exception:  # noqa: BLE001 - the provider's own error is the one that must reach the user
            return False

    def _isRetryableError(self, message: AssistantMessage) -> bool:
        # Context overflow is handled by compaction, not retry. Everything else is the
        # shared classifier's call -- the private copy that used to live here predated the
        # exclusion table, so an out-of-quota 429 was retried three times for nothing.
        model = self._modelForMessage(message) or self.model
        context_window = model.contextWindow if model is not None else 0
        if isContextOverflow(message, context_window):
            return False

        return isRetryableAssistantError(message)

    async def _prepareRetry(self, message: AssistantMessage) -> bool:
        settings = self.settingsManager.getRetrySettings()
        if not bool(settings.get("enabled")):
            return False

        self._retryAttempt += 1
        max_retries = int(settings.get("maxRetries", 0) or 0)
        if self._retryAttempt > max_retries:
            self._retryAttempt -= 1
            return False

        delay_ms = retryDelayMs(self._retryPolicyFrom(settings), self._retryAttempt)
        self._emit(
            {
                "type": "auto_retry_start",
                "attempt": self._retryAttempt,
                "maxAttempts": max_retries,
                "delayMs": delay_ms,
                "errorMessage": message.errorMessage or "Unknown error",
            }
        )

        # Keep the failed attempt in raw history while durably omitting it from model projection.
        self._omitRecoveryAttempt(message)

        # Wait with exponential backoff (abortable)
        self._retryAbortController = AbortController()
        try:
            should_continue = await _sleep_with_abort(delay_ms, self._retryAbortController.signal)
            if not should_continue:
                # Aborted during sleep - emit end event so UI can clean up
                self._finishCancelledRetry()
                return False
        finally:
            self._retryAbortController = None

        return True

    @staticmethod
    def _retryPolicyFrom(settings: Mapping[str, Any]) -> RetryPolicy:
        return RetryPolicy(
            enabled=bool(settings.get("enabled")),
            maxRetries=int(settings.get("maxRetries", 0) or 0),
            baseDelayMs=int(settings.get("baseDelayMs", 0) or 0),
            maxAgentDelayMs=settings.get("maxAgentDelayMs"),
        )

    def _findLastAssistantMessage(self) -> AssistantMessage | None:
        for message in reversed(self.agent.state.messages):
            assistant_message = _as_assistant_message(message)
            if assistant_message is not None:
                return assistant_message
        return None

    def _flushPendingBashMessages(self) -> None:
        if not self._pendingBashMessages:
            return
        for bash_message in self._pendingBashMessages:
            self.sessionManager.appendMessage(bash_message)
        self._pendingBashMessages = []
        self._refreshFinalizedContext()

    def _flushPendingCustomMessages(self) -> None:
        # The conversation, the transcript and the UI are all told here rather than at
        # send time. The transcript replays in file order on resume, so an entry landing
        # between an assistant message and its toolResults would rebuild the same broken
        # conversation the queue exists to prevent -- and the UI has to follow the
        # transcript, because the TUI answers a custom message_end by clearing the chat
        # and rebuilding it from the session file (interactive_mode renderCurrentSessionState).
        # Announcing the message before the entry exists shows it for as long as it takes
        # the next toolResult to arrive, which is the opposite of showing it.
        if not self._pendingCustomMessages:
            return
        for custom_message in self._pendingCustomMessages:
            self._appendCustomMessage(custom_message)
            self._refreshFinalizedContext()
            self._emit({"type": "message_start", "message": custom_message})
            self._emit({"type": "message_end", "message": custom_message})
        self._pendingCustomMessages = []

    async def _checkCompaction(
        self,
        assistant_message: AssistantMessage,
        skip_aborted_check: bool = True,
        tool_results: list[Any] | None = None,
    ) -> bool:
        settings_data = self.settingsManager.getCompactionSettings(self.model)
        if not bool(settings_data.get("enabled")):
            return False

        if skip_aborted_check and assistant_message.stopReason == "aborted":
            return False

        # Skip overflow check if the message came from a different model.
        # This handles the case where user switched from a smaller-context model (e.g. opus)
        # to a larger-context model (e.g. codex) - the overflow error from the old model
        # shouldn't trigger compaction for the new model. Under a virtual selection, the
        # physical model that produced the message supplies the limits.
        message_model = self._modelForMessage(assistant_message)
        same_model = message_model is not None
        limits_model = message_model or self.model
        context_window = limits_model.contextWindow if limits_model is not None else 0

        branch_entries = self.sessionManager.getBranch()
        latest_compaction = getLatestCompactionEntry(branch_entries)
        latest_compaction_timestamp = _event_timestamp_ms(_event_field(latest_compaction, "timestamp"))
        assistant_timestamp = _event_timestamp_ms(assistant_message.timestamp)
        if (
            latest_compaction is not None
            and assistant_timestamp > 0
            and assistant_timestamp <= latest_compaction_timestamp
        ):
            return False

        # Automatic cases 1 and 2: context overflow.
        # A length stop is recoverable when output ended below the model's original desired limit,
        # independent of the configured context size or any context-clamped provider request limit.
        current_projection = self.sessionManager.buildSessionProjection()
        assistant_entry_id = self._findPersistedMessageEntryId(assistant_message)
        assistant_is_projected = assistant_entry_id is None or any(
            str(entry.sourceEntry.get("id")) == assistant_entry_id
            and any(_message_role(message) == "assistant" for message in entry.messages)
            for entry in current_projection.entries
        )
        branch = branch_entries
        assistant_index = (
            next((i for i, entry in enumerate(branch) if entry.get("id") == assistant_entry_id), -1)
            if assistant_entry_id
            else -1
        )
        entries_after_assistant = branch[assistant_index + 1 :] if assistant_index >= 0 else []
        has_post_assistant_context_edit = any(entry.get("type") == "context_edit" for entry in entries_after_assistant)
        latest_assistant_edit = next(
            (
                entry
                for entry in reversed(entries_after_assistant)
                if entry.get("type") == "context_edit" and entry.get("targetId") == assistant_entry_id
            ),
            None,
        )
        assistant_retained_for_explicit_recovery = assistant_entry_id is None or (
            not any(entry.get("type") == "compaction" for entry in entries_after_assistant)
            and (latest_assistant_edit is None or latest_assistant_edit.get("replacement") is not None)
        )
        assistant_usage_matches_projection = assistant_is_projected and not has_post_assistant_context_edit
        explicit_overflow = assistant_message.stopReason == "error" and isContextOverflow(assistant_message)
        context_overflow = same_model and (
            (explicit_overflow and assistant_retained_for_explicit_recovery)
            or (assistant_usage_matches_projection and isContextOverflow(assistant_message, context_window))
        )
        recoverable_length = (
            message_model is not None
            and assistant_is_projected
            and isRecoverableLength(assistant_message, message_model.maxTokens)
        )
        if context_overflow or recoverable_length:
            will_retry = assistant_message.stopReason != "stop"
            if not will_retry:
                return await self._runAutoCompaction("overflow", False)
            if self._overflow_recovery_attempted:
                error_message = (
                    "Truncated response recovery failed after one compact-and-retry attempt."
                    if recoverable_length and not context_overflow
                    else "Context overflow recovery failed after one compact-and-retry "
                         "attempt. Try reducing context or switching to a "
                         "larger-context model."
                )
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
                        "errorMessage": error_message,
                    }
                )
                await self._emitSessionCompactFailed(
                    reason="overflow",
                    error_message=error_message,
                    aborted=False,
                    will_retry=False,
                    from_extension=False,
                )
                return False

            # Persistently omit the selected final attempt before post-run recovery compaction.
            self._overflow_recovery_attempted = True
            self._omitRecoveryAttempt(assistant_message, tool_results or [])
            retry = await self._runAutoCompaction("overflow", True)
            if retry:
                self._failedResponse = assistant_message
            return retry

        # Case 3: threshold compaction without retry.
        direct_context_tokens = calculateContextTokens(assistant_message.usage)
        has_context_edits = any(entry.sourceEntry.get("type") == "context_edit" for entry in current_projection.entries)
        # Without provider usage direct=0 and threshold compaction would never fire,
        # so fall back to a message-size estimate (pi #8328/4495469a5).
        if has_context_edits:
            context_tokens = estimateProjectedContextTokens(current_projection, branch).tokens
        elif assistant_message.stopReason == "error" or direct_context_tokens == 0:
            estimate = estimateContextTokens(list(self.agent.state.messages))
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

        return await self._runAutoCompaction(
            "threshold", False, current_tokens=context_tokens, context_window=context_window
        )

    async def _runAutoCompaction(self, reason: str, will_retry: bool, *,
                                   preflight=False, current_tokens=None, parent_signal=None,
                                   context_window=None) -> bool:
        started = False
        from_hook = False
        published_result = None
        abort_controller: AbortController | None = None
        try:
            source = self._compactionSource()
            abort_controller = AbortController()
            self._auto_compaction_abort_controller = abort_controller
            operation_signal = AnyAbortSignal(abort_controller.signal, parent_signal) if parent_signal is not None else abort_controller.signal
            operation = await self._prepareCompactionOperation(
                reason, operation_signal,
                preflight=preflight, current_tokens=current_tokens, will_retry=will_retry,
                context_window=context_window)
            if operation is None:
                return False
            self._checkCompactionSource(source)
            self._emit({"type": "compaction_start", "reason": reason})
            started = True
            result, from_hook = await operation()
            if result is None:
                self._emit({"type": "compaction_end", "reason": reason, "result": None,
                            "aborted": False, "willRetry": False})
                return False

            if operation_signal.aborted:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": reason,
                        "result": None,
                        "aborted": True,
                        "willRetry": False,
                    }
                )
                await self._emitSessionCompactFailed(
                    reason=reason, aborted=True, will_retry=False,
                    from_extension=from_hook)
                return False

            saved_entry = self._publishCompaction(result, from_hook, source)
            published_result = result
            if saved_entry is not None:
                await self.moments.session_compact({  # MISAKA fork
                    "type": "session_compact", "compactionEntry": saved_entry, "fromExtension": from_hook,
                    "reason": reason, "willRetry": will_retry,
                })
                await self._extensionRunner.emit(
                    {
                        "type": "session_compact",
                        "compactionEntry": saved_entry,
                        "fromExtension": from_hook,
                        "reason": reason,
                        "willRetry": will_retry,
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
                return True

            # Auto-compaction can complete while follow-up/steering/custom messages are waiting.
            # Continue once so queued messages are delivered.
            return self.agent.hasQueuedMessages()
        except asyncio.CancelledError:
            if started:
                self._emit({"type": "compaction_end", "reason": reason,
                            "result": published_result, "aborted": published_result is None,
                            "willRetry": False})
            # MISAKA fork: reported even before compaction_start (pi: only once started), because the context
            # engine's session_context_prepare has already run and pairs with this terminal event.
            if published_result is None:
                await self._emitSessionCompactFailed(
                    reason=reason, aborted=True, will_retry=False, from_extension=from_hook)
            raise
        except Exception as error:
            error_message = str(error) if str(error) else "compaction failed"
            aborted = (
                (abort_controller is not None and abort_controller.signal.aborted)
                # MISAKA fork: a preflight compaction also stops with the request it runs for.
                or (parent_signal is not None and parent_signal.aborted)
                or isinstance(error, _CompactionCancelledByExtension)
            )
            formatted_error = None if aborted else (
                f"Context overflow recovery failed: {error_message}"
                if reason == "overflow"
                else f"Auto-compaction failed: {error_message}"
            )
            if published_result is not None:
                formatted_error = f"Compaction notification failed: {error_message}"
            if started:
                self._emit(
                    {
                        "type": "compaction_end",
                        "reason": reason,
                        "result": published_result,
                        "aborted": aborted and published_result is None,
                        "willRetry": False,
                        "errorMessage": formatted_error,
                    }
                )
            # MISAKA fork: reported even before compaction_start, as above (session_context_prepare has run).
            if published_result is None:
                await self._emitSessionCompactFailed(
                    reason=reason, aborted=aborted, will_retry=False,
                    error_message=formatted_error, from_extension=from_hook)
            if preflight and not aborted:
                raise
            return False
        finally:
            if self._auto_compaction_abort_controller is abort_controller:
                self._auto_compaction_abort_controller = None

    def _holdBackgroundTask(self, task: asyncio.Task[Any], event: str) -> None:
        """Keep the task alive and make sure its failure is not swallowed.

        pi fires these with `void promise`, and a JS promise is never collected. The
        event loop keeps only a weak reference to a Task, so the Python equivalent has
        to hold the reference itself -- the same conclusion `spawn_stream_task`
        (misaka/ai/utils/event_stream.py) reached for provider streams. Retrieving
        `exception()` in the done callback also turns a silently dropped extension
        failure into an ExtensionError the runner's listeners can see, instead of a
        "Task exception was never retrieved" line at GC time.
        """
        _BACKGROUND_TASKS.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            _BACKGROUND_TASKS.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is None:
                return
            self._extensionRunner.emitError(
                ExtensionError(extensionPath="<runtime>", event=event, error=str(error))
            )

        task.add_done_callback(_done)

    def _spawnBackground(self, awaitable: Any, event: str = "background") -> None:
        # KNOWN GAP (audit core-session-10, deliberately left as-is): the no-loop
        # fallback runs the coroutine on a brand-new loop. `self.abort()` /
        # `self.compact()` await futures created on the session's own loop
        # (`waitForIdle`'s `_idleWaiters`), so reaching this branch would be a
        # cross-loop wait. Every known caller is already on the session loop; making
        # this correct means giving the session a loop handle, which is a design
        # change, not a patch.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(awaitable)
            return
        # `void promise()`: the call runs at once, up to its first suspension, as a JavaScript async call does.
        self._holdBackgroundTask(asyncio.Task(awaitable, loop=loop, eager_start=True), event)

    def _spawnExtensionMessage(self, awaitable: Any, event: str = "send_message") -> None:
        """Match JavaScript async calls by running eagerly until their first suspension."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(awaitable)
            return
        self._holdBackgroundTask(asyncio.Task(awaitable, loop=loop, eager_start=True), event)


def _tool_execution_extension_event(event: Any) -> dict[str, Any]:
    """The extension-facing dict of a ``tool_execution_*`` event, with ``parentToolCallId`` only
    for calls another tool made."""
    event_type = _event_type(event)
    fields = {
        "tool_execution_start": ("toolCallId", "toolName", "args"),
        "tool_execution_update": ("toolCallId", "toolName", "args", "partialResult"),
        "tool_execution_end": ("toolCallId", "toolName", "result", "isError"),
    }[event_type]
    payload: dict[str, Any] = {"type": event_type, **{name: _event_field(event, name) for name in fields}}
    duration_ms = _event_field(event, "durationMs") if event_type == "tool_execution_end" else None
    if duration_ms is not None:
        payload["durationMs"] = duration_ms
    parent_tool_call_id = _event_field(event, "parentToolCallId")
    if parent_tool_call_id:
        payload["parentToolCallId"] = parent_tool_call_id
    return payload


def _beingCancelled() -> bool:
    """Whether the running task has a cancellation requested of it (``Task.cancelling``): its `CancelledError` is the
    caller's, not one a hook or route raised itself."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class _CustomRunBlocked(RuntimeError):
    """MISAKA fork: a before_agent_start hook blocked a run that a custom message starts."""


def _raiseRunBlocked(trigger: BeforeAgentStartTrigger, reason: str) -> NoReturn:
    """MISAKA fork: pi has no block. A blocked prompt fails; a blocked custom run is recorded
    without a turn by `_startRun`."""
    if trigger["messageType"] == "custom":
        raise _CustomRunBlocked(reason)
    raise RuntimeError(reason)


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


def _normalize_nullish_message_content(message: Any) -> Any:
    if _message_role(message) not in {"user", "assistant", "toolResult", "custom"}:
        return message
    if _message_content(message) is not None:
        return message
    if isinstance(message, Mapping):
        return {**message, "content": []}
    model_copy = getattr(message, "model_copy", None)
    return model_copy(update={"content": []}) if callable(model_copy) else message


def _validate_as(model_type: type, data: Any) -> Any | None:
    """Re-validate `data` as `model_type`, or None when it is not that shape."""
    validate = getattr(model_type, "model_validate", None)
    if not callable(validate):
        return None
    try:
        return validate(data)
    except Exception:  # noqa: BLE001 - a replacement of another shape falls back to the raw mapping
        return None


def _is_noop_compaction(preparation: CompactionPreparation) -> bool:
    """True when the preparation would summarize nothing at all.

    Such a compaction pays for a summarization request over an empty conversation,
    appends a summary entry that only grows the context, and re-fires next turn. A
    split turn with a prefix is not a no-op: its prefix is what gets summarized.
    """
    return not preparation.messagesToSummarize and not preparation.turnPrefixMessages


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


def _auth_overrides(signal: Any | None) -> Any:
    """`{ signal }` for `getAuth`, or nothing when no signal was given."""
    if signal is None:
        return None
    from misaka.ai.auth.resolve import AuthResolutionOverrides

    return AuthResolutionOverrides(signal=signal)


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
    "parseSkillBlock",
]
