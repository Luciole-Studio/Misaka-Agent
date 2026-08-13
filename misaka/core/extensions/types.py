"""Extension type surface for coding-agent resource and tool loading."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict, TypeGuard, TypeVar, overload

from misaka.agent.harness.messages import CustomMessage
from misaka.agent.types import AgentMessage, AgentToolResult, AgentToolUpdateCallback, ThinkingLevel, ToolExecutionMode
from misaka.ai.types import (
    Api,
    AssistantMessageEvent,
    AssistantMessageEventStream,
    Context,
    ImageContent,
    Model,
    SimpleStreamOptions,
    TextContent,
    ToolResultMessage,
)
from misaka.ai.utils.typebox_helpers import Static, TSchema
from misaka.ai.utils.oauth.types import OAuthCredentials, OAuthLoginCallbacks
from misaka.tui import (
    AutocompleteItem,
    AutocompleteProvider,
    Component,
    EditorComponent,
    EditorTheme,
    KeyId,
    OverlayHandle,
    OverlayOptions,
    TUI,
)

from misaka.core.compaction import CompactionPreparation, CompactionResult as SessionCompactionResult
from misaka.core.event_bus import EventBus
from misaka.core.exec import ExecOptions, ExecResult
from misaka.core.footer_data_provider import ReadonlyFooterDataProvider
from misaka.core.keybindings import KeybindingsManager
from misaka.core.model_registry import ModelRegistry
from misaka.core.session_manager import (
    ReadonlySessionManager,
    SessionEntry,
    SessionManager,
)
from misaka.core.slash_commands import SlashCommandInfo
from misaka.core.source_info import SourceInfo
from misaka.core.system_prompt import BuildSystemPromptOptions
from misaka.modes.interactive.theme.theme import Theme

if TYPE_CHECKING:
    from misaka.core.bash_executor import BashResult
    from misaka.core.tools.bash import BashOperations, BashToolDetails, BashToolInput
    from misaka.core.tools.edit import EditToolDetails, EditToolInput
    from misaka.core.tools.find import FindToolDetails, FindToolInput
    from misaka.core.tools.grep import GrepToolDetails, GrepToolInput
    from misaka.core.tools.ls import LsToolDetails, LsToolInput
    from misaka.core.tools.read import ReadToolDetails, ReadToolInput
    from misaka.core.tools.write import WriteToolInput

TArgs = TypeVar("TArgs")
TDetails = TypeVar("TDetails")
TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")


class AbortSignal(Protocol):
    aborted: bool

type AppKeybinding = Literal[
    "app.interrupt",
    "app.clear",
    "app.exit",
    "app.suspend",
    "app.thinking.cycle",
    "app.model.cycleForward",
    "app.model.cycleBackward",
    "app.model.select",
    "app.tools.expand",
    "app.thinking.toggle",
    "app.session.toggleNamedFilter",
    "app.editor.external",
    "app.message.followUp",
    "app.message.dequeue",
    "app.clipboard.pasteImage",
    "app.session.new",
    "app.session.tree",
    "app.session.fork",
    "app.session.resume",
    "app.tree.foldOrUp",
    "app.tree.unfoldOrDown",
    "app.tree.editLabel",
    "app.tree.toggleLabelTimestamp",
    "app.session.togglePath",
    "app.session.toggleSort",
    "app.session.rename",
    "app.session.delete",
    "app.session.deleteNoninvasive",
    "app.models.save",
    "app.models.enableAll",
    "app.models.clearAll",
    "app.models.toggleProvider",
    "app.models.reorderUp",
    "app.models.reorderDown",
    "app.tree.filter.default",
    "app.tree.filter.noTools",
    "app.tree.filter.userOnly",
    "app.tree.filter.labeledOnly",
    "app.tree.filter.all",
    "app.tree.filter.cycleForward",
    "app.tree.filter.cycleBackward",
]
type BranchSummaryEntry = SessionEntry
type CompactionEntry = SessionEntry
type WidgetPlacement = Literal["aboveEditor", "belowEditor"]


class ExtensionUIDialogOptions(TypedDict, total=False):
    signal: AbortSignal
    timeout: int


class ExtensionWidgetOptions(TypedDict, total=False):
    placement: WidgetPlacement


class TerminalInputResult(TypedDict, total=False):
    consume: bool
    data: str


type TerminalInputHandler = Callable[[str], TerminalInputResult | None]


class WorkingIndicatorOptions(TypedDict, total=False):
    frames: list[str]
    intervalMs: int


type AutocompleteProviderFactory = Callable[[AutocompleteProvider], AutocompleteProvider]
type EditorFactory = Callable[[TUI, EditorTheme, KeybindingsManager], EditorComponent]
type ExtensionErrorListener = Callable[["ExtensionError"], None]
type NewSessionHandler = Callable[[dict[str, Any] | None], Awaitable[dict[str, bool]]]
type ForkHandler = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, bool]]]
type NavigateTreeHandler = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, bool]]]
type SwitchSessionHandler = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, bool]]]
type ReloadHandler = Callable[[], Awaitable[None]]
type ShutdownHandler = Callable[[], None]
type ModelSelectSource = Literal["set", "cycle", "restore"]
type InputSource = Literal["interactive", "rpc", "extension"]
type MessageRenderer[TDetails] = Callable[[CustomMessage[TDetails], "MessageRenderOptions", Theme], Component | None]
type ExtensionHandler[TEvent, TResult] = Callable[
    [TEvent, "ExtensionContext"],
    Awaitable[TResult | None] | TResult | None,
]
type SendMessageHandler = Callable[
    [_CustomMessagePayload, _SendMessageOptions | None],
    None,
]
type SendUserMessageHandler = Callable[
    [str | list[TextContent | ImageContent], _SendUserMessageOptions | None],
    None,
]
type AppendEntryHandler = Callable[[str, Any], None]
type SetSessionNameHandler = Callable[[str], None]
type GetSessionNameHandler = Callable[[], str | None]
type SetLabelHandler = Callable[[str, str | None], None]
type GetActiveToolsHandler = Callable[[], list[str]]
type GetAllToolsHandler = Callable[[], list["ToolInfo"]]
type SetActiveToolsHandler = Callable[[list[str]], None]
type RefreshToolsHandler = Callable[[], None]
type GetCommandsHandler = Callable[[], list[SlashCommandInfo]]
type SetModelHandler = Callable[[Model[Any]], Awaitable[bool]]
type GetThinkingLevelHandler = Callable[[], ThinkingLevel]
type SetThinkingLevelHandler = Callable[[ThinkingLevel], None]
type RegisterProviderHandler = Callable[[str, "ProviderConfig", str | None], None]
type UnregisterProviderHandler = Callable[[str, str | None], None]
type ToolCallRenderer[TArgs] = Callable[[TArgs, Theme, "ToolRenderContext"], Component]
type ToolResultRenderer = Callable[
    [AgentToolResult | Mapping[str, Any], "ToolRenderResultOptions", Theme, "ToolRenderContext"],
    Component,
]
type ToolRenderShell = Literal["default", "self"]


class _ThemeInfo(TypedDict):
    name: str
    path: str | None


class _SetThemeResult(TypedDict):
    success: bool
    error: NotRequired[str]


class _CustomUIOptions(TypedDict, total=False):
    overlay: bool
    overlayOptions: OverlayOptions | Callable[[], OverlayOptions]
    onHandle: Callable[[OverlayHandle], None]


class _CustomMessagePayload(TypedDict, total=False):
    customType: str
    content: str | list[TextContent | ImageContent]
    display: Any
    details: Any


class _SendMessageOptions(TypedDict, total=False):
    triggerTurn: bool
    deliverAs: Literal["steer", "followUp", "nextTurn"]


class _SendUserMessageOptions(TypedDict, total=False):
    deliverAs: Literal["steer", "followUp"]


class _NewSessionOptions(TypedDict, total=False):
    parentSession: str
    setup: Callable[[SessionManager], Awaitable[None]]
    withSession: Callable[["ReplacedSessionContext"], Awaitable[None]]


class _ForkOptions(TypedDict, total=False):
    position: Literal["before", "at"]
    withSession: Callable[["ReplacedSessionContext"], Awaitable[None]]


class _NavigateTreeOptions(TypedDict, total=False):
    summarize: bool
    customInstructions: str
    replaceInstructions: bool
    label: str


class _SwitchSessionOptions(TypedDict, total=False):
    withSession: Callable[["ReplacedSessionContext"], Awaitable[None]]


@dataclass(slots=True)
class ToolDefinition[TArgs, TDetails]:
    name: str
    label: str
    description: str
    parameters: TSchema
    execute: Callable[
        [str, TArgs, AbortSignal | None, AgentToolUpdateCallback | None, "ExtensionContext"],
        Awaitable[AgentToolResult],
    ]
    prepareArguments: Callable[[Any], Static] | None = None
    executionMode: ToolExecutionMode | None = None
    promptSnippet: str | None = None
    promptGuidelines: list[str] = field(default_factory=list)
    renderCall: ToolCallRenderer[TArgs] | None = None
    renderResult: ToolResultRenderer | None = None
    renderShell: ToolRenderShell | None = None


@dataclass(slots=True)
class ToolInfo:
    name: str
    description: str
    parameters: Any
    sourceInfo: SourceInfo


@dataclass(slots=True)
class RegisteredTool:
    definition: ToolDefinition[Any, Any]
    sourceInfo: SourceInfo


@dataclass(slots=True)
class RegisteredCommand:
    name: str
    sourceInfo: SourceInfo
    description: str | None = None
    getArgumentCompletions: Callable[[str], list[Any] | None | Awaitable[list[Any] | None]] | None = None
    handler: Callable[[str, ExtensionCommandContext], Awaitable[None]] | None = None


@dataclass(slots=True)
class ResolvedCommand(RegisteredCommand):
    invocationName: str = ""


@dataclass(slots=True)
class ExtensionFlag:
    name: str
    extensionPath: str
    type: Literal["boolean", "string"]
    description: str | None = None
    default: bool | str | None = None


@dataclass(slots=True)
class ExtensionShortcut:
    shortcut: KeyId
    extensionPath: str
    handler: Callable[[ExtensionContext], Awaitable[None] | None]
    description: str | None = None


class ProviderModelConfig(TypedDict):
    id: str
    name: str
    api: NotRequired[Api]
    baseUrl: NotRequired[str]
    reasoning: bool
    thinkingLevelMap: NotRequired[dict[str, str | None]]
    input: list[Literal["text", "image"]]
    cost: dict[str, float]
    contextWindow: int
    maxTokens: int
    headers: NotRequired[dict[str, str]]
    compat: NotRequired[dict[str, Any]]


class OAuthProviderConfig(TypedDict):
    name: str
    login: Callable[[OAuthLoginCallbacks], Awaitable[OAuthCredentials]]
    refreshToken: Callable[[OAuthCredentials], Awaitable[OAuthCredentials]]
    getApiKey: Callable[[OAuthCredentials], str]
    modifyModels: NotRequired[Callable[[list[Model[Any]], OAuthCredentials], list[Model[Any]]]]


class ProviderConfig(TypedDict):
    name: NotRequired[str]
    baseUrl: NotRequired[str]
    apiKey: NotRequired[str]
    api: NotRequired[Api]
    streamSimple: NotRequired[Callable[[Model[Api], Context, SimpleStreamOptions | None], AssistantMessageEventStream]]
    headers: NotRequired[dict[str, str]]
    authHeader: NotRequired[bool]
    models: NotRequired[list[ProviderModelConfig]]
    oauth: NotRequired[OAuthProviderConfig]


@dataclass(slots=True)
class PendingProviderRegistration:
    name: str
    config: ProviderConfig
    extensionPath: str


class ContextUsage(TypedDict):
    tokens: int | None
    contextWindow: int
    percent: float | None


class CompactOptions(TypedDict, total=False):
    customInstructions: str
    onComplete: Callable[[SessionCompactionResult], None]
    onError: Callable[[Exception], None]


class MessageRenderOptions(TypedDict):
    expanded: bool


class ToolRenderResultOptions(TypedDict):
    expanded: bool
    isPartial: bool


class ToolRenderContext(Protocol):
    args: Any
    toolCallId: str
    invalidate: Callable[[], None]
    lastComponent: Component | None
    state: Any
    cwd: str
    executionStarted: bool
    argsComplete: bool
    isPartial: bool
    expanded: bool
    showImages: bool
    isError: bool


class ExtensionUIContext(Protocol):
    theme: Theme

    async def select(
        self,
        title: str,
        options: list[str],
        opts: ExtensionUIDialogOptions | None = None,
    ) -> str | None: ...

    async def confirm(
        self,
        title: str,
        message: str,
        opts: ExtensionUIDialogOptions | None = None,
    ) -> bool: ...

    async def input(
        self,
        title: str,
        placeholder: str | None = None,
        opts: ExtensionUIDialogOptions | None = None,
    ) -> str | None: ...

    def notify(self, message: str, type: Literal["info", "warning", "error"] | None = None) -> None: ...

    def onTerminalInput(self, handler: TerminalInputHandler) -> Callable[[], None]: ...

    def setStatus(self, key: str, text: str | None) -> None: ...

    def setWorkingMessage(self, message: str | None = None) -> None: ...

    def setWorkingVisible(self, visible: bool) -> None: ...

    def setWorkingIndicator(self, options: WorkingIndicatorOptions | None = None) -> None: ...

    def setHiddenThinkingLabel(self, label: str | None = None) -> None: ...

    def setWidget(self, key: str, content: list[str] | Callable[..., Any] | None, options: ExtensionWidgetOptions | None = None) -> None: ...

    def setFooter(self, factory: Callable[..., Any] | None) -> None: ...

    def setHeader(self, factory: Callable[..., Any] | None) -> None: ...

    def setTitle(self, title: str) -> None: ...

    async def custom(self, factory: Callable[..., Any], options: _CustomUIOptions | None = None) -> Any: ...

    def pasteToEditor(self, text: str) -> None: ...

    def setEditorText(self, text: str) -> None: ...

    def getEditorText(self) -> str: ...

    async def editor(self, title: str, prefill: str | None = None) -> str | None: ...

    def addAutocompleteProvider(self, factory: AutocompleteProviderFactory) -> None: ...

    def setEditorComponent(self, factory: EditorFactory | None) -> None: ...

    def getEditorComponent(self) -> EditorFactory | None: ...

    def getAllThemes(self) -> list[_ThemeInfo]: ...

    def getTheme(self, name: str) -> Theme | None: ...

    def setTheme(self, theme: str | Theme) -> _SetThemeResult: ...

    def getToolsExpanded(self) -> bool: ...

    def setToolsExpanded(self, expanded: bool) -> None: ...


class ResourcesDiscoverEvent(TypedDict):
    type: Literal["resources_discover"]
    cwd: str
    reason: Literal["startup", "reload"]


class ResourcesDiscoverResult(TypedDict, total=False):
    skillPaths: list[str]
    promptPaths: list[str]
    themePaths: list[str]


class SessionStartEvent(TypedDict):
    type: Literal["session_start"]
    reason: Literal["startup", "reload", "new", "resume", "fork"]
    previousSessionFile: NotRequired[str]


class SessionBeforeSwitchEvent(TypedDict):
    type: Literal["session_before_switch"]
    reason: Literal["new", "resume"]
    targetSessionFile: NotRequired[str]


class SessionBeforeForkEvent(TypedDict):
    type: Literal["session_before_fork"]
    entryId: str
    position: Literal["before", "at"]


class SessionBeforeCompactEvent(TypedDict):
    type: Literal["session_before_compact"]
    preparation: CompactionPreparation
    branchEntries: list[SessionEntry]
    customInstructions: NotRequired[str]
    signal: AbortSignal


class SessionCompactEvent(TypedDict):
    type: Literal["session_compact"]
    compactionEntry: CompactionEntry
    fromExtension: bool


class SessionShutdownEvent(TypedDict):
    type: Literal["session_shutdown"]
    reason: Literal["quit", "reload", "new", "resume", "fork"]
    targetSessionFile: NotRequired[str]


class TreePreparation(TypedDict):
    targetId: str
    oldLeafId: str | None
    commonAncestorId: str | None
    entriesToSummarize: list[SessionEntry]
    userWantsSummary: bool
    customInstructions: NotRequired[str]
    replaceInstructions: NotRequired[bool]
    label: NotRequired[str]


class SessionBeforeTreeEvent(TypedDict):
    type: Literal["session_before_tree"]
    preparation: TreePreparation
    signal: AbortSignal


class SessionTreeEvent(TypedDict):
    type: Literal["session_tree"]
    newLeafId: str | None
    oldLeafId: str | None
    summaryEntry: NotRequired[BranchSummaryEntry]
    fromExtension: NotRequired[bool]


type SessionEvent = (
    SessionStartEvent
    | SessionBeforeSwitchEvent
    | SessionBeforeForkEvent
    | SessionBeforeCompactEvent
    | SessionCompactEvent
    | SessionShutdownEvent
    | SessionBeforeTreeEvent
    | SessionTreeEvent
)


class ContextEvent(TypedDict):
    type: Literal["context"]
    messages: list[AgentMessage]


class BeforeProviderRequestEvent(TypedDict):
    type: Literal["before_provider_request"]
    payload: Any


class AfterProviderResponseEvent(TypedDict):
    type: Literal["after_provider_response"]
    status: int
    headers: dict[str, str]


class BeforeAgentStartEvent(TypedDict):
    type: Literal["before_agent_start"]
    prompt: str
    images: NotRequired[list[ImageContent]]
    systemPrompt: str
    systemPromptOptions: BuildSystemPromptOptions


class AgentStartEvent(TypedDict):
    type: Literal["agent_start"]


class AgentEndEvent(TypedDict):
    type: Literal["agent_end"]
    messages: list[AgentMessage]


class AgentEndEventResult(TypedDict, total=False):
    block: bool
    reason: str


class TurnStartEvent(TypedDict):
    type: Literal["turn_start"]
    turnIndex: int
    timestamp: int


class TurnEndEvent(TypedDict):
    type: Literal["turn_end"]
    turnIndex: int
    message: AgentMessage
    toolResults: list[ToolResultMessage]


class MessageStartEvent(TypedDict):
    type: Literal["message_start"]
    message: AgentMessage


class MessageUpdateEvent(TypedDict):
    type: Literal["message_update"]
    message: AgentMessage
    assistantMessageEvent: AssistantMessageEvent


class MessageEndEvent(TypedDict):
    type: Literal["message_end"]
    message: AgentMessage


class ToolExecutionStartEvent(TypedDict):
    type: Literal["tool_execution_start"]
    toolCallId: str
    toolName: str
    args: Any


class ToolExecutionUpdateEvent(TypedDict):
    type: Literal["tool_execution_update"]
    toolCallId: str
    toolName: str
    args: Any
    partialResult: Any


class ToolExecutionEndEvent(TypedDict):
    type: Literal["tool_execution_end"]
    toolCallId: str
    toolName: str
    result: Any
    isError: bool


class ModelSelectEvent(TypedDict):
    type: Literal["model_select"]
    model: Model[Any]
    previousModel: Model[Any] | None
    source: ModelSelectSource


class ThinkingLevelSelectEvent(TypedDict):
    type: Literal["thinking_level_select"]
    level: ThinkingLevel
    previousLevel: ThinkingLevel


class UserBashEvent(TypedDict):
    type: Literal["user_bash"]
    command: str
    excludeFromContext: bool
    cwd: str


class InputEvent(TypedDict):
    type: Literal["input"]
    text: str
    images: NotRequired[list[ImageContent]]
    source: InputSource


class InputEventContinueResult(TypedDict):
    action: Literal["continue"]


class InputEventTransformResult(TypedDict):
    action: Literal["transform"]
    text: str
    images: NotRequired[list[ImageContent]]


class InputEventHandledResult(TypedDict):
    action: Literal["handled"]


type InputEventResult = InputEventContinueResult | InputEventTransformResult | InputEventHandledResult


class ToolCallEventBase(TypedDict):
    type: Literal["tool_call"]
    toolCallId: str


class BashToolCallEvent(ToolCallEventBase):
    toolName: Literal["bash"]
    input: BashToolInput


class ReadToolCallEvent(ToolCallEventBase):
    toolName: Literal["read"]
    input: ReadToolInput


class EditToolCallEvent(ToolCallEventBase):
    toolName: Literal["edit"]
    input: EditToolInput


class WriteToolCallEvent(ToolCallEventBase):
    toolName: Literal["write"]
    input: WriteToolInput


class GrepToolCallEvent(ToolCallEventBase):
    toolName: Literal["grep"]
    input: GrepToolInput


class FindToolCallEvent(ToolCallEventBase):
    toolName: Literal["find"]
    input: FindToolInput


class LsToolCallEvent(ToolCallEventBase):
    toolName: Literal["ls"]
    input: LsToolInput


class CustomToolCallEvent(ToolCallEventBase):
    toolName: str
    input: dict[str, Any]


type ToolCallEvent = (
    BashToolCallEvent
    | ReadToolCallEvent
    | EditToolCallEvent
    | WriteToolCallEvent
    | GrepToolCallEvent
    | FindToolCallEvent
    | LsToolCallEvent
    | CustomToolCallEvent
)


class ToolResultEventBase(TypedDict):
    type: Literal["tool_result"]
    toolCallId: str
    input: dict[str, Any]
    content: list[TextContent | ImageContent]
    isError: bool


class BashToolResultEvent(ToolResultEventBase):
    toolName: Literal["bash"]
    details: BashToolDetails | None


class ReadToolResultEvent(ToolResultEventBase):
    toolName: Literal["read"]
    details: ReadToolDetails | None


class EditToolResultEvent(ToolResultEventBase):
    toolName: Literal["edit"]
    details: EditToolDetails | None


class WriteToolResultEvent(ToolResultEventBase):
    toolName: Literal["write"]
    details: None


class GrepToolResultEvent(ToolResultEventBase):
    toolName: Literal["grep"]
    details: GrepToolDetails | None


class FindToolResultEvent(ToolResultEventBase):
    toolName: Literal["find"]
    details: FindToolDetails | None


class LsToolResultEvent(ToolResultEventBase):
    toolName: Literal["ls"]
    details: LsToolDetails | None


class CustomToolResultEvent(ToolResultEventBase):
    toolName: str
    details: Any


type ToolResultEvent = (
    BashToolResultEvent
    | ReadToolResultEvent
    | EditToolResultEvent
    | WriteToolResultEvent
    | GrepToolResultEvent
    | FindToolResultEvent
    | LsToolResultEvent
    | CustomToolResultEvent
)


type ExtensionEvent = (
    ResourcesDiscoverEvent
    | SessionEvent
    | ContextEvent
    | BeforeProviderRequestEvent
    | AfterProviderResponseEvent
    | BeforeAgentStartEvent
    | AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | ModelSelectEvent
    | ThinkingLevelSelectEvent
    | UserBashEvent
    | InputEvent
    | ToolCallEvent
    | ToolResultEvent
)


class ContextEventResult(TypedDict, total=False):
    messages: list[AgentMessage]


type BeforeProviderRequestEventResult = Any


class ToolCallEventResult(TypedDict, total=False):
    block: bool
    reason: str
    updatedInput: dict[str, Any]


class UserBashEventResult(TypedDict, total=False):
    operations: BashOperations
    result: BashResult


class ToolResultEventResult(TypedDict, total=False):
    content: list[TextContent | ImageContent]
    details: Any
    isError: bool


class MessageEndEventResult(TypedDict, total=False):
    message: AgentMessage


class BeforeAgentStartEventResult(TypedDict, total=False):
    message: _CustomMessagePayload
    systemPrompt: str
    block: bool
    reason: str


class SessionBeforeSwitchResult(TypedDict, total=False):
    cancel: bool


class SessionBeforeForkResult(TypedDict, total=False):
    cancel: bool
    skipConversationRestore: bool


class SessionBeforeCompactResult(TypedDict, total=False):
    cancel: bool
    compaction: SessionCompactionResult


class BranchSummaryPayload(TypedDict):
    summary: str
    details: NotRequired[Any]


class SessionBeforeTreeResult(TypedDict, total=False):
    cancel: bool
    summary: BranchSummaryPayload
    customInstructions: str
    replaceInstructions: bool
    label: str


class ExtensionRuntimeState(Protocol):
    flagValues: dict[str, bool | str]
    pendingProviderRegistrations: list[PendingProviderRegistration]
    assertActive: Callable[[], None]
    invalidate: Callable[[str | None], None]
    # Retain an event-bus subscription until this runtime is invalidated.  (pi #7656)
    trackEventBusSubscription: Callable[[Callable[[], None]], Callable[[], None]]
    registerProvider: RegisterProviderHandler
    unregisterProvider: UnregisterProviderHandler


@dataclass(slots=True)
class ExtensionRuntime:
    sendMessage: SendMessageHandler
    sendUserMessage: SendUserMessageHandler
    appendEntry: AppendEntryHandler
    setSessionName: SetSessionNameHandler
    getSessionName: GetSessionNameHandler
    setLabel: SetLabelHandler
    getActiveTools: GetActiveToolsHandler
    getAllTools: GetAllToolsHandler
    setActiveTools: SetActiveToolsHandler
    refreshTools: RefreshToolsHandler
    getCommands: GetCommandsHandler
    setModel: SetModelHandler
    getThinkingLevel: GetThinkingLevelHandler
    setThinkingLevel: SetThinkingLevelHandler
    flagValues: dict[str, bool | str] = field(default_factory=dict)
    pendingProviderRegistrations: list[PendingProviderRegistration] = field(default_factory=list)
    assertActive: Callable[[], None] = lambda: None
    invalidate: Callable[[str | None], None] = lambda _message=None: None
    trackEventBusSubscription: Callable[[Callable[[], None]], Callable[[], None]] = lambda unsubscribe: unsubscribe
    registerProvider: RegisterProviderHandler = lambda _name, _config, _extension_path=None: None
    unregisterProvider: UnregisterProviderHandler = lambda _name, _extension_path=None: None


@dataclass(slots=True)
class _LoadedExtensionRuntime(ExtensionRuntime):
    loadedModules: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Extension:
    path: str
    resolvedPath: str
    sourceInfo: SourceInfo
    handlers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    tools: dict[str, RegisteredTool] = field(default_factory=dict)
    messageRenderers: dict[str, MessageRenderer[Any]] = field(default_factory=dict)
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    flags: dict[str, ExtensionFlag] = field(default_factory=dict)
    shortcuts: dict[KeyId, ExtensionShortcut] = field(default_factory=dict)
    hidden: bool = False


@dataclass(slots=True)
class _LoadedExtension(Extension):
    skillPaths: list[str] = field(default_factory=list)
    promptPaths: list[str] = field(default_factory=list)
    themePaths: list[str] = field(default_factory=list)
    systemPrompt: str | None = None
    appendSystemPrompt: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LoadExtensionsResult:
    extensions: list[Extension]
    errors: list[dict[str, str]]
    runtime: ExtensionRuntime


@dataclass(slots=True)
class ExtensionError:
    extensionPath: str
    event: str
    error: str
    stack: str | None = None


class ExtensionContext(Protocol):
    ui: ExtensionUIContext
    hasUI: bool
    cwd: str
    sessionManager: ReadonlySessionManager
    modelRegistry: ModelRegistry
    model: Model[Any] | None
    signal: AbortSignal | None

    def isIdle(self) -> bool: ...

    def abort(self) -> None: ...

    def hasPendingMessages(self) -> bool: ...

    def shutdown(self) -> None: ...

    def getContextUsage(self) -> ContextUsage | None: ...

    def compact(self, options: CompactOptions | None = None) -> None: ...

    def getSystemPrompt(self) -> str: ...


class ExtensionCommandContext(ExtensionContext, Protocol):
    async def waitForIdle(self) -> None: ...

    async def newSession(self, options: _NewSessionOptions | None = None) -> dict[str, bool]: ...

    async def fork(self, entryId: str, options: _ForkOptions | None = None) -> dict[str, bool]: ...

    async def navigateTree(self, targetId: str, options: _NavigateTreeOptions | None = None) -> dict[str, bool]: ...

    async def switchSession(self, sessionPath: str, options: _SwitchSessionOptions | None = None) -> dict[str, bool]: ...

    async def reload(self) -> None: ...


class ReplacedSessionContext(ExtensionCommandContext, Protocol):
    async def sendMessage(self, message: _CustomMessagePayload, options: _SendMessageOptions | None = None) -> None: ...

    async def sendUserMessage(
        self,
        content: str | list[TextContent | ImageContent],
        options: _SendUserMessageOptions | None = None,
    ) -> None: ...


class ExtensionActions(Protocol):
    sendMessage: SendMessageHandler
    sendUserMessage: SendUserMessageHandler
    appendEntry: AppendEntryHandler
    setSessionName: SetSessionNameHandler
    getSessionName: GetSessionNameHandler
    setLabel: SetLabelHandler
    getActiveTools: GetActiveToolsHandler
    getAllTools: GetAllToolsHandler
    setActiveTools: SetActiveToolsHandler
    refreshTools: RefreshToolsHandler
    getCommands: GetCommandsHandler
    setModel: SetModelHandler
    getThinkingLevel: GetThinkingLevelHandler
    setThinkingLevel: SetThinkingLevelHandler


class ExtensionContextActions(Protocol):
    getModel: Callable[[], Model[Any] | None]
    isIdle: Callable[[], bool]
    getSignal: Callable[[], AbortSignal | None]
    abort: Callable[[], None]
    hasPendingMessages: Callable[[], bool]
    shutdown: Callable[[], None]
    getContextUsage: Callable[[], ContextUsage | None]
    compact: Callable[[CompactOptions | None], None]
    getSystemPrompt: Callable[[], str]


class ExtensionCommandContextActions(Protocol):
    waitForIdle: Callable[[], Awaitable[None]]
    newSession: Callable[[_NewSessionOptions | None], Awaitable[dict[str, bool]]]
    fork: Callable[[str, _ForkOptions | None], Awaitable[dict[str, bool]]]
    navigateTree: Callable[[str, _NavigateTreeOptions | None], Awaitable[dict[str, bool]]]
    switchSession: Callable[[str, _SwitchSessionOptions | None], Awaitable[dict[str, bool]]]
    reload: Callable[[], Awaitable[None]]


class ExtensionAPI(Protocol):
    events: EventBus

    @overload
    def on(self, event: Literal["resources_discover"], handler: ExtensionHandler[ResourcesDiscoverEvent, ResourcesDiscoverResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_start"], handler: ExtensionHandler[SessionStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_switch"], handler: ExtensionHandler[SessionBeforeSwitchEvent, SessionBeforeSwitchResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_fork"], handler: ExtensionHandler[SessionBeforeForkEvent, SessionBeforeForkResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_compact"], handler: ExtensionHandler[SessionBeforeCompactEvent, SessionBeforeCompactResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_compact"], handler: ExtensionHandler[SessionCompactEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_shutdown"], handler: ExtensionHandler[SessionShutdownEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_tree"], handler: ExtensionHandler[SessionBeforeTreeEvent, SessionBeforeTreeResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_tree"], handler: ExtensionHandler[SessionTreeEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["context"], handler: ExtensionHandler[ContextEvent, ContextEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["before_provider_request"], handler: ExtensionHandler[BeforeProviderRequestEvent, BeforeProviderRequestEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["after_provider_response"], handler: ExtensionHandler[AfterProviderResponseEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["before_agent_start"], handler: ExtensionHandler[BeforeAgentStartEvent, BeforeAgentStartEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["agent_start"], handler: ExtensionHandler[AgentStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["agent_end"], handler: ExtensionHandler[AgentEndEvent, AgentEndEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["turn_start"], handler: ExtensionHandler[TurnStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["turn_end"], handler: ExtensionHandler[TurnEndEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["message_start"], handler: ExtensionHandler[MessageStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["message_update"], handler: ExtensionHandler[MessageUpdateEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["message_end"], handler: ExtensionHandler[MessageEndEvent, MessageEndEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["tool_execution_start"], handler: ExtensionHandler[ToolExecutionStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["tool_execution_update"], handler: ExtensionHandler[ToolExecutionUpdateEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["tool_execution_end"], handler: ExtensionHandler[ToolExecutionEndEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["model_select"], handler: ExtensionHandler[ModelSelectEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["thinking_level_select"], handler: ExtensionHandler[ThinkingLevelSelectEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["tool_call"], handler: ExtensionHandler[ToolCallEvent, ToolCallEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["tool_result"], handler: ExtensionHandler[ToolResultEvent, ToolResultEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["user_bash"], handler: ExtensionHandler[UserBashEvent, UserBashEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["input"], handler: ExtensionHandler[InputEvent, InputEventResult]) -> None: ...

    def on(self, event: str, handler: Callable[..., Any]) -> None: ...

    def registerTool(self, tool: ToolDefinition[Any, Any]) -> None: ...

    def registerCommand(
        self,
        name: str,
        options: dict[str, Any],
    ) -> None: ...

    def registerShortcut(
        self,
        shortcut: KeyId,
        options: dict[str, Any],
    ) -> None: ...

    def registerFlag(
        self,
        name: str,
        options: dict[str, Any],
    ) -> None: ...

    def registerMessageRenderer(self, customType: str, renderer: MessageRenderer[Any]) -> None: ...

    def getFlag(self, name: str) -> bool | str | None: ...

    def sendMessage(self, message: _CustomMessagePayload, options: _SendMessageOptions | None = None) -> None: ...

    def sendUserMessage(
        self,
        content: str | list[TextContent | ImageContent],
        options: _SendUserMessageOptions | None = None,
    ) -> None: ...

    def appendEntry(self, customType: str, data: Any = None) -> None: ...

    def setSessionName(self, name: str) -> None: ...

    def getSessionName(self) -> str | None: ...

    def setLabel(self, entryId: str, label: str | None) -> None: ...

    async def exec(self, command: str, args: list[str], options: ExecOptions | None = None) -> ExecResult: ...

    def getActiveTools(self) -> list[str]: ...

    def getAllTools(self) -> list[ToolInfo]: ...

    def setActiveTools(self, toolNames: list[str]) -> None: ...

    def getCommands(self) -> list[SlashCommandInfo]: ...

    async def setModel(self, model: Model[Any]) -> bool: ...

    def getThinkingLevel(self) -> ThinkingLevel: ...

    def setThinkingLevel(self, level: ThinkingLevel) -> None: ...

    def registerProvider(self, name: str, config: ProviderConfig) -> None: ...

    def unregisterProvider(self, name: str) -> None: ...


type ExtensionFactory = Callable[[ExtensionAPI], Awaitable[None] | None]

# pi 真源（types.ts InlineExtension）：裸工厂，或 {"name": ..., "factory": ..., "hidden": ...}——
# name 在启动屏显示为 <inline:name>；hidden 为真则不上启动屏列表。
type InlineExtension = ExtensionFactory | dict[str, Any]


def define_tool[TTool: ToolDefinition[Any, Any]](tool: TTool) -> TTool:
    return tool


def is_bash_tool_result(event: ToolResultEvent) -> TypeGuard[BashToolResultEvent]:
    return event["toolName"] == "bash"


def is_read_tool_result(event: ToolResultEvent) -> TypeGuard[ReadToolResultEvent]:
    return event["toolName"] == "read"


def is_edit_tool_result(event: ToolResultEvent) -> TypeGuard[EditToolResultEvent]:
    return event["toolName"] == "edit"


def is_write_tool_result(event: ToolResultEvent) -> TypeGuard[WriteToolResultEvent]:
    return event["toolName"] == "write"


def is_grep_tool_result(event: ToolResultEvent) -> TypeGuard[GrepToolResultEvent]:
    return event["toolName"] == "grep"


def is_find_tool_result(event: ToolResultEvent) -> TypeGuard[FindToolResultEvent]:
    return event["toolName"] == "find"


def is_ls_tool_result(event: ToolResultEvent) -> TypeGuard[LsToolResultEvent]:
    return event["toolName"] == "ls"


@overload
def is_tool_call_event_type(tool_name: Literal["bash"], event: ToolCallEvent) -> TypeGuard[BashToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["read"], event: ToolCallEvent) -> TypeGuard[ReadToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["edit"], event: ToolCallEvent) -> TypeGuard[EditToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["write"], event: ToolCallEvent) -> TypeGuard[WriteToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["grep"], event: ToolCallEvent) -> TypeGuard[GrepToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["find"], event: ToolCallEvent) -> TypeGuard[FindToolCallEvent]: ...


@overload
def is_tool_call_event_type(tool_name: Literal["ls"], event: ToolCallEvent) -> TypeGuard[LsToolCallEvent]: ...


def is_tool_call_event_type(tool_name: str, event: ToolCallEvent) -> bool:
    return event["toolName"] == tool_name


defineTool = define_tool
isBashToolResult = is_bash_tool_result
isReadToolResult = is_read_tool_result
isEditToolResult = is_edit_tool_result
isWriteToolResult = is_write_tool_result
isGrepToolResult = is_grep_tool_result
isFindToolResult = is_find_tool_result
isLsToolResult = is_ls_tool_result
isToolCallEventType = is_tool_call_event_type


__all__ = [
    "AfterProviderResponseEvent",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AgentEndEvent",
    "AgentEndEventResult",
    "AppKeybinding",
    "AgentStartEvent",
    "AppendEntryHandler",
    "AutocompleteProviderFactory",
    "BashToolCallEvent",
    "BashToolResultEvent",
    "BeforeAgentStartEvent",
    "BeforeAgentStartEventResult",
    "BeforeProviderRequestEvent",
    "BeforeProviderRequestEventResult",
    "BuildSystemPromptOptions",
    "CompactOptions",
    "ContextUsage",
    "ContextEvent",
    "ContextEventResult",
    "CustomToolCallEvent",
    "CustomToolResultEvent",
    "defineTool",
    "EditToolCallEvent",
    "EditToolResultEvent",
    "EditorFactory",
    "ExecOptions",
    "ExecResult",
    "Extension",
    "ExtensionAPI",
    "ExtensionActions",
    "ExtensionCommandContext",
    "ExtensionCommandContextActions",
    "ExtensionContext",
    "ExtensionContextActions",
    "ExtensionError",
    "ExtensionEvent",
    "ExtensionFactory",
    "ExtensionFlag",
    "ExtensionHandler",
    "ExtensionRuntime",
    "ExtensionRuntimeState",
    "ExtensionShortcut",
    "InlineExtension",
    "ExtensionUIDialogOptions",
    "ExtensionUIContext",
    "ExtensionWidgetOptions",
    "FindToolCallEvent",
    "FindToolResultEvent",
    "GetActiveToolsHandler",
    "GetAllToolsHandler",
    "GetCommandsHandler",
    "GetSessionNameHandler",
    "GetThinkingLevelHandler",
    "GrepToolCallEvent",
    "GrepToolResultEvent",
    "InputEvent",
    "InputEventResult",
    "InputSource",
    "isBashToolResult",
    "isEditToolResult",
    "isFindToolResult",
    "isGrepToolResult",
    "isLsToolResult",
    "isReadToolResult",
    "isToolCallEventType",
    "isWriteToolResult",
    "KeybindingsManager",
    "LoadExtensionsResult",
    "LsToolCallEvent",
    "LsToolResultEvent",
    "MessageEndEvent",
    "MessageEndEventResult",
    "MessageRenderOptions",
    "MessageRenderer",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ModelSelectEvent",
    "ModelSelectSource",
    "ProviderConfig",
    "ProviderModelConfig",
    "ReadToolCallEvent",
    "ReadToolResultEvent",
    "RegisteredCommand",
    "RegisteredTool",
    "ReplacedSessionContext",
    "ResourcesDiscoverEvent",
    "ResourcesDiscoverResult",
    "ResolvedCommand",
    "RefreshToolsHandler",
    "SessionBeforeCompactEvent",
    "SessionBeforeCompactResult",
    "SessionBeforeForkEvent",
    "SessionBeforeForkResult",
    "SessionBeforeSwitchEvent",
    "SessionBeforeSwitchResult",
    "SessionBeforeTreeEvent",
    "SessionBeforeTreeResult",
    "SessionCompactEvent",
    "SessionEvent",
    "SessionShutdownEvent",
    "SessionStartEvent",
    "SessionTreeEvent",
    "SendMessageHandler",
    "SendUserMessageHandler",
    "SetActiveToolsHandler",
    "SetLabelHandler",
    "SetModelHandler",
    "SetSessionNameHandler",
    "SetThinkingLevelHandler",
    "TerminalInputHandler",
    "ThinkingLevelSelectEvent",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolDefinition",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolInfo",
    "ToolExecutionMode",
    "ToolRenderContext",
    "ToolRenderResultOptions",
    "ToolResultEvent",
    "ToolResultEventResult",
    "TreePreparation",
    "TurnEndEvent",
    "TurnStartEvent",
    "UserBashEvent",
    "UserBashEventResult",
    "WidgetPlacement",
    "WorkingIndicatorOptions",
    "WriteToolCallEvent",
    "WriteToolResultEvent",
]
