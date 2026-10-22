"""Extension type surface for coding-agent resource and tool loading."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NotRequired,
    Protocol,
    TypedDict,
    TypeGuard,
    TypeVar,
    overload,
)

from misaka.agent.types import (
    AgentMessage,
    AgentTool,
    AgentToolCallOutcome,
    AgentToolResult,
    AgentToolUpdateCallback,
    ThinkingLevel,
    ToolExecutionMode,
)
from misaka.ai.auth.types import OAuthCredentials
from misaka.ai.compat.extension_oauth_types import OAuthLoginCallbacks
from misaka.ai.models import Provider as RuntimeProvider
from misaka.ai.models import RefreshModelsContext
from misaka.ai.types import (
    Api,
    AssistantMessageEvent,
    AssistantMessageEventStream,
    ConstrainedSamplingConfig,
    Context,
    ImageContent,
    Model,
    ProviderClassifier,
    ProviderImages,
    SimpleStreamOptions,
    TextContent,
    ToolResultMessage,
    Usage,
)
from misaka.ai.utils._typebox import Static, TSchema
from misaka.core.compaction import CompactionPreparation
from misaka.core.compaction import CompactionResult as SessionCompactionResult
from misaka.core.event_bus import EventBus
from misaka.core.exec import ExecOptions, ExecResult
from misaka.core.keybindings import KeybindingsManager
from misaka.core.mcp_servers import (
    McpServerConfig,
    McpServerRegistry,
    RegisteredMcpServer,
)
from misaka.core.messages import CustomMessage
from misaka.core.model_registry import ModelRegistry
from misaka.core.session_manager import (
    CustomEntry,
    ReadonlySessionManager,
    SessionEntry,
    SessionManager,
)
from misaka.core.settings_manager import Settings
from misaka.core.slash_commands import SlashCommandInfo
from misaka.core.source_info import SourceInfo
from misaka.core.system_prompt import BuildSystemPromptOptions
from misaka.core.virtual_models import (
    ModelRoute,
    ModelRouteRequest,
    VirtualModelDefinition,
)
from misaka.modes.interactive.theme.theme import Theme
from misaka.ui.tui import (
    TUI,
    AutocompleteProvider,
    Component,
    EditorComponent,
    EditorTheme,
    KeyId,
    OverlayHandle,
    OverlayOptions,
)

if TYPE_CHECKING:
    from misaka.core.bash_executor import BashResult
    from misaka.core.tools.bash import BashOperations, BashToolDetails, BashToolInput
    from misaka.core.tools.edit import EditToolDetails, EditToolInput
    from misaka.core.tools.find import FindToolDetails, FindToolInput
    from misaka.core.tools.grep import GrepToolDetails, GrepToolInput
    from misaka.core.tools.ls import LsToolDetails, LsToolInput
    from misaka.core.tools.powershell import PowerShellToolDetails, PowerShellToolInput
    from misaka.core.tools.read import ReadToolDetails, ReadToolInput
    from misaka.core.tools.write import WriteToolInput
    from misaka.core.web.provider import WebSearchProvider

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
    "app.message.copy",
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
type InputSource = Literal["interactive", "extension"]
type UIPromptKind = Literal["select", "confirm", "input", "editor", "custom"]
type MessageRenderer[TDetails] = Callable[[CustomMessage[TDetails], "MessageRenderOptions", Theme], Component | None]
type MarkdownTransformer = Callable[[str, "MarkdownTransformContext"], str]
type EntryRenderer[TData] = Callable[[CustomEntry, "EntryRenderOptions", Theme], Component | None]
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


class AppendEntryHandler(Protocol):
    @overload
    def __call__(self, customType: str) -> None: ...

    @overload
    def __call__(self, customType: str, data: Any) -> None: ...


type SetSessionNameHandler = Callable[[str], None]
type GetSessionNameHandler = Callable[[], str | None]
type SetLabelHandler = Callable[[str, str | None], None]
type GetActiveToolsHandler = Callable[[], list[str]]
type GetAllToolsHandler = Callable[[], list["ToolInfo"]]
type GetSettingsHandler = Callable[[], Settings]
type SetActiveToolsHandler = Callable[[list[str]], None]
type RefreshToolsHandler = Callable[[], None]
type GetCommandsHandler = Callable[[], list[SlashCommandInfo]]
type SetModelHandler = Callable[[Model[Any]], Awaitable[bool]]
type GetThinkingLevelHandler = Callable[[], ThinkingLevel]
type SetThinkingLevelHandler = Callable[[ThinkingLevel], None]
type RegisterProviderHandler = Callable[[str, "ProviderConfig", str | None], None]
type RegisterNativeProviderHandler = Callable[[RuntimeProvider, str | None], None]
type UnregisterProviderHandler = Callable[[str, str | None], None]
type RegisterVirtualModelHandler = Callable[[VirtualModelDefinition, str | None], None]
type UnregisterVirtualModelHandler = Callable[[str, str], None]
type ToolCallRenderer[TArgs] = Callable[[TArgs, Theme, "ToolRenderContext"], Component]
type ToolResultRenderer = Callable[
    [AgentToolResult | Mapping[str, Any], "ToolRenderResultOptions", Theme, "ToolRenderContext"],
    Component,
]
type ToolRenderShell = Literal["default", "self"]
type ExtensionMode = Literal["tui", "json", "print"]


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
    # MISAKA fork: the status reported to the terminal (OSC 7501) while the component waits for the user, as
    # `{"kind": "question" | "permission" | "auth", "message": <dialog title>}`; AskUserQuestion reports its question.
    blocked: dict[str, str]


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
    expandPromptTemplates: bool


class _NewSessionOptions(TypedDict, total=False):
    parentSession: str
    carryOverContext: bool
    setup: Callable[[SessionManager], Awaitable[None]]
    withSession: Callable[[ReplacedSessionContext], Awaitable[None]]


class _ForkOptions(TypedDict, total=False):
    position: Literal["before", "at"]
    withSession: Callable[[ReplacedSessionContext], Awaitable[None]]


class _NavigateTreeOptions(TypedDict, total=False):
    summarize: bool
    customInstructions: str
    replaceInstructions: bool
    label: str


class _SwitchSessionOptions(TypedDict, total=False):
    withSession: Callable[[ReplacedSessionContext], Awaitable[None]]


# How the model reaches a tool. "Callable" means callable from other tools through
# `ctx.executeTool()`, as the `codemode` tool does.
#
# - `direct`: declared to the model while active, and callable while active.
# - `model-only`: declared to the model while active, never callable. Use it for orchestrating or
#   interactive tools.
# - `codemode`: callable whenever registered. Not declared to the model unless explicitly
#   activated. Codemode tools list it in their description.
# - `deferred`: like `codemode`, but codemode tools do not list it; tool search can find it.
# - `hidden`: registered but unreachable. Activating it has no effect.
#
# `direct` and `model-only` tools are activated when they are registered; the others are not.
# The active tool set (`getActiveTools`/`setActiveTools`) is the set declared to the model.
type ToolExposure = Literal["direct", "model-only", "codemode", "deferred", "hidden"]


class ToolAnnotations(TypedDict, total=False):
    """Hints about what a tool does, with the meaning of MCP tool annotations. They come from the
    tool's author and are not verified; permission extensions can use them to decide which calls to
    confirm."""

    # The tool does not modify its environment.
    readOnlyHint: bool
    # The tool may delete or overwrite data, rather than only add to it. Meaningful when not
    # read-only.
    destructiveHint: bool
    # Repeating a call with the same arguments has no further effect. Meaningful when not read-only.
    idempotentHint: bool
    # The tool reaches an open world of external entities, such as the web, rather than a closed
    # domain.
    openWorldHint: bool


class ToolNamespace(TypedDict):
    """A group of related tools, such as the tools of one MCP server. Codemode tools list them
    together."""

    # For example `mcp__docs`.
    name: str
    # Other names that refer to the namespace where tools look it up by name (codemode's
    # `describeNamespace()` and the `namespace` option of `searchTools()`), for example `docs`.
    aliases: NotRequired[Sequence[str]]
    # Short summary shown once with the group in model-facing tool listings.
    description: NotRequired[str]
    # Longer usage guidance, such as MCP server instructions. Not part of tool listings; tools that
    # describe the namespace on request (codemode's `describeNamespace()`) return it.
    instructions: NotRequired[str]


@dataclass(frozen=True, slots=True)
class ToolLoadout:
    """The tools of a session as ``ToolDefinition.prepareLoadout`` sees them."""

    # Tools declared to the model (the active tools), in order, with their original descriptions.
    declared: tuple[AgentTool, ...]
    # Tools callable through `ctx.executeTool()`.
    callable: tuple[AgentTool, ...]
    # Every registered tool.
    registered: tuple[AgentTool, ...]
    getExposure: Callable[[str], ToolExposure]
    getNamespace: Callable[[str], ToolNamespace | None]
    # A tool's `promptGuidelines`. Hidden declarations leave them out of the system prompt.
    getPromptGuidelines: Callable[[str], Sequence[str]]


class ToolLoadoutChanges(TypedDict, total=False):
    """Changes ``ToolDefinition.prepareLoadout`` makes to what the model sees."""

    # Model-facing descriptions of declared tools, by tool name.
    descriptions: Mapping[str, str]
    # Declared tools whose declarations requests leave out. They stay active and callable, and the
    # transcript still declares them, so the active set survives `/tree` and resume.
    hiddenDeclarations: Sequence[str]


@dataclass(slots=True)
class ToolDefinition[TArgs, TDetails]:
    name: str
    label: str
    description: str
    parameters: TSchema
    execute: Callable[
        [str, TArgs, AbortSignal | None, AgentToolUpdateCallback | None, ExtensionToolContext],
        Awaitable[AgentToolResult],
    ]
    aliases: tuple[str, ...] = ()
    constrainedSampling: Literal[False] | ConstrainedSamplingConfig | None = None
    prepareArguments: Callable[[Any], Static] | None = None
    # JSON Schema of `structuredContent` in successful results. Tools that declare it should
    # always set `structuredContent`; codemode scripts then receive it instead of the text content.
    outputSchema: TSchema | None = None
    # How the model reaches the tool. Default: `"direct"`. See `ToolExposure`.
    exposure: ToolExposure | None = None
    # Group the tool belongs to, for example its MCP server.
    namespace: ToolNamespace | None = None
    # For tools that reach other tools (such as `codemode`, which calls them, and `tool_search`, which
    # loads them): which tools a call with `input` may reach. `"all"`, or the words of the input that
    # may name them, where a word refers to a tool or namespace whose name it contains, so words may
    # over-match. Extensions that register tools late, such as MCP servers that are still connecting,
    # use it from their `tool_call` handler to finish registering those tools before the call runs.
    reachesTools: Callable[[Mapping[str, Any]], Literal["all"] | Sequence[str]] | None = None
    # Hints about what the tool does, for example from an MCP server.
    annotations: ToolAnnotations | None = None
    # Whether registering the tool activates it. Default: `True` for `direct` and `model-only`
    # tools; other exposures are never activated on registration. A tool with
    # `defaultActive=False` is activated by naming it in `--tools` or the `defaultTools` setting,
    # or with `setActiveTools()`.
    defaultActive: bool | None = None
    # Adjust how the loadout is presented to the model while this tool is active. Called whenever
    # the active tools change. Tools that orchestrate other tools use it, for example to list the
    # callable tools in their own description.
    prepareLoadout: Callable[[ToolLoadout], ToolLoadoutChanges | None] | None = None
    executionMode: ToolExecutionMode | None = None
    promptSnippet: str | None = None
    promptGuidelines: list[str] = field(default_factory=list)
    renderCall: ToolCallRenderer[TArgs] | None = None
    renderResult: ToolResultRenderer | None = None
    renderShell: ToolRenderShell | None = None


class ToolRenderers(TypedDict, total=False):
    """`Pick<ToolDefinition, "renderShell" | "renderCall" | "renderResult">`.

    MISAKA fork: a resolver may also hand back the `ToolDefinition` itself, which carries the three as attributes;
    readers take them with `read_field`.
    """

    renderShell: ToolRenderShell
    renderCall: ToolCallRenderer[Any]
    renderResult: ToolResultRenderer


# Chooses how calls to a tool are drawn, including tools that are not registered. `next()` returns
# the renderers the remaining resolvers, then the registered tool, would use.
type ToolRendererResolver = Callable[
    [str, Callable[[], "ToolRenderers | ToolDefinition[Any, Any] | None"]],
    "ToolRenderers | ToolDefinition[Any, Any] | None",
]


@dataclass(slots=True)
class ToolInfo:
    name: str
    description: str
    parameters: Any
    exposure: ToolExposure
    sourceInfo: SourceInfo
    # pi types.ts picks promptGuidelines and reachesTools off ToolDefinition too.
    promptGuidelines: list[str] = field(default_factory=list)
    reachesTools: Callable[[Mapping[str, Any]], Literal["all"] | Sequence[str]] | None = None
    namespace: ToolNamespace | None = None
    annotations: ToolAnnotations | None = None


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


class _BooleanExtensionFlagOptions(TypedDict):
    type: Literal["boolean"]
    description: NotRequired[str]
    default: NotRequired[bool]


class _StringExtensionFlagOptions(TypedDict):
    type: Literal["string"]
    description: NotRequired[str]
    default: NotRequired[str]


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


class ProviderModelConfigBase(TypedDict):
    # Model ID.
    id: str
    # Display name.
    name: str
    # API type override for this model.
    api: NotRequired[str]
    # API endpoint URL override for this model.
    baseUrl: NotRequired[str]
    # Supported input types.
    input: list[Literal["text", "image"]]
    # Provider input limits and cache-safe image preprocessing metadata.
    inputLimits: NotRequired[dict[str, Any]]
    # Per-million-token cost rates and optional request-wide input pricing tiers.
    cost: dict[str, Any]
    # Custom headers for this model.
    headers: NotRequired[dict[str, str]]


class ProviderChatModelConfig(ProviderModelConfigBase):
    """Chat model configuration. Omitted ``type`` is normalized to ``"chat"``."""

    type: NotRequired[Literal["chat"]]
    # Whether the model supports extended thinking.
    reasoning: bool
    # Maps pi thinking levels to provider/model-specific values; None marks a level unsupported.
    thinkingLevelMap: NotRequired[dict[str, str | None]]
    # Best-effort prompt cache lifetime in seconds per retention tier. Unset disables cache warming.
    promptCache: NotRequired[dict[str, Any]]
    # Maximum context window size in tokens.
    contextWindow: int
    # Maximum output tokens.
    maxTokens: int
    samplingParams: NotRequired[dict[str, Any]]
    # OpenAI compatibility settings.
    compat: NotRequired[dict[str, Any]]


class ProviderImageModelConfig(ProviderModelConfigBase):
    """Image-generation model configuration."""

    type: Literal["image"]
    output: list[Literal["text", "image"]]


class ProviderClassifierModelConfig(ProviderModelConfigBase):
    """Structured classifier model configuration."""

    type: Literal["classifier"]
    contextWindow: int


# Configuration for a model within a provider.
ProviderModelConfig = ProviderChatModelConfig | ProviderImageModelConfig | ProviderClassifierModelConfig


class OAuthProviderConfig(TypedDict):
    name: str
    isSubscription: NotRequired[bool]
    login: Callable[[OAuthLoginCallbacks], Awaitable[OAuthCredentials]]
    # Called as refreshToken(credentials, signal). A provider that declares only the
    # credentials parameter is still called with one argument, matching how upstream's
    # JavaScript drops the extra argument.
    refreshToken: Callable[..., Awaitable[OAuthCredentials]]
    getApiKey: Callable[[OAuthCredentials], str]
    getBaseUrl: NotRequired[Callable[[OAuthCredentials], str | None]]
    getAuthHeaders: NotRequired[Callable[[OAuthCredentials], dict[str, str] | None]]
    modifyModels: NotRequired[Callable[[list[Model[Any]], OAuthCredentials], list[Model[Any]]]]


@dataclass(slots=True, kw_only=True)
class ExtensionVirtualModel:
    """Virtual model registered via pi.registerVirtualModel()."""

    provider: str
    id: str
    name: str
    # Like `VirtualModelDefinition.route`, with an extension context.
    route: Callable[[ModelRouteRequest, ExtensionContext], ModelRoute | Awaitable[ModelRoute]]
    thinkingLevels: Sequence[ThinkingLevel] | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None
    input: list[Literal["text", "image"]] | None = None


class ProviderConfig(TypedDict):
    name: NotRequired[str]
    baseUrl: NotRequired[str]
    apiKey: NotRequired[str]
    api: NotRequired[Api]
    streamSimple: NotRequired[Callable[[Model[Api], Context, SimpleStreamOptions | None], AssistantMessageEventStream]]
    # Image-generation implementations keyed by image API.
    images: NotRequired[dict[str, ProviderImages]]
    # Classifier implementations keyed by classifier API.
    classifiers: NotRequired[dict[str, ProviderClassifier]]
    headers: NotRequired[dict[str, str]]
    authHeader: NotRequired[bool]
    # Models to register. If provided, replaces all existing models for this provider.
    models: NotRequired[list[ProviderModelConfig]]
    # Refresh this provider's model list. The returned list replaces extension-provided models.
    refreshModels: NotRequired[Callable[[RefreshModelsContext], Awaitable[list[ProviderModelConfig]]]]
    oauth: NotRequired[OAuthProviderConfig]


@dataclass(slots=True)
class PendingProviderRegistration:
    name: str
    config: ProviderConfig
    extensionPath: str


@dataclass(slots=True)
class PendingNativeProviderRegistration:
    provider: RuntimeProvider
    extensionPath: str


@dataclass(slots=True)
class PendingVirtualModelRegistration:
    definition: VirtualModelDefinition
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
    outputPad: int


class MarkdownTransformContext(TypedDict):
    messageType: Literal["user", "assistant", "assistant-thinking"]
    isStreaming: bool
    availableWidth: int


class EntryRenderOptions(TypedDict):
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
    # Milliseconds the tool's execution took, from the final result; `None` while it runs, when it did not run, or
    # for results stored before durations were recorded.
    durationMs: int | None
    # Horizontal padding configured by the outputPad setting. Renderers with `renderShell: "self"` apply it themselves.
    outputPad: int


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


class _ProjectTrustUIContext(TypedDict):
    select: Callable[..., Awaitable[str | None]]
    confirm: Callable[..., Awaitable[bool]]
    input: Callable[..., Awaitable[str | None]]
    notify: Callable[..., None]


class ProjectTrustEvent(TypedDict):
    type: Literal["project_trust"]
    cwd: str


type ProjectTrustEventDecision = Literal["yes", "no", "undecided"]


class ProjectTrustEventResult(TypedDict):
    trusted: ProjectTrustEventDecision
    remember: NotRequired[bool]


class ProjectTrustContext(TypedDict):
    cwd: str
    mode: ExtensionMode
    hasUI: bool
    ui: _ProjectTrustUIContext


type ProjectTrustHandler = Callable[
    [ProjectTrustEvent, ProjectTrustContext],
    Awaitable[ProjectTrustEventResult] | ProjectTrustEventResult,
]


class ResourcesDiscoverEvent(TypedDict):
    type: Literal["resources_discover"]
    cwd: str
    reason: Literal["startup", "reload"]


class ResourcesDiscoverResult(TypedDict, total=False):
    promptPaths: list[str]
    themePaths: list[str]

    skillPaths: list[str]


class McpServersChangeEvent(TypedDict):
    """Fired when an extension registers or unregisters an MCP server after the extensions are
    bound (see ``ExtensionAPI.registerMcpServer``). Servers registered while extensions load are
    read with ``pi.getMcpServers()`` on ``session_start``. Handling this event marks an extension
    as the one that connects registered servers."""

    type: Literal["mcp_servers_change"]
    # Every registered server after the change.
    servers: list[RegisteredMcpServer]


class SessionStartEvent(TypedDict):
    type: Literal["session_start"]
    reason: Literal["startup", "reload", "new", "resume", "fork"]
    previousSessionFile: NotRequired[str]


class SessionInfoChangedEvent(TypedDict):
    type: Literal["session_info_changed"]
    name: str | None


class SessionBeforeSwitchEvent(TypedDict):
    type: Literal["session_before_switch"]
    reason: Literal["new", "resume"]
    targetSessionFile: NotRequired[str]


class SessionBeforeForkEvent(TypedDict):
    type: Literal["session_before_fork"]
    entryId: str
    position: Literal["before", "at"]


class SessionContextPrepareEvent(TypedDict):
    """Early whole-context ownership, before native compaction gates."""
    type: Literal["session_context_prepare"]
    reason: Literal["manual", "threshold", "overflow"]
    preflight: bool
    allowCompression: bool
    currentTokens: int | None
    systemPrompt: str
    tools: list[dict[str, Any]]
    messages: list[AgentMessage]
    customInstructions: str | None
    signal: AbortSignal


class SessionContextPrepareResult(TypedDict):
    # None is an engine-owned no-op, not native fallback. The operation executes
    # only after core starts the ordinary compaction lifecycle/owner fencing.
    execute: Callable[[], Awaitable[SessionCompactionResult | None]] | None


class SessionBeforeCompactEvent(TypedDict):
    type: Literal["session_before_compact"]
    preparation: CompactionPreparation
    branchEntries: list[SessionEntry]
    customInstructions: NotRequired[str]
    reason: Literal["manual", "threshold", "overflow"]
    willRetry: bool
    signal: AbortSignal


class SessionCompactEvent(TypedDict):
    type: Literal["session_compact"]
    compactionEntry: CompactionEntry
    fromExtension: bool
    reason: Literal["manual", "threshold", "overflow"]
    willRetry: bool


class SessionCompactFailedEvent(TypedDict):
    """Terminal state of a failed or aborted compaction (pi #8175/a6b1dbceb); pairs with session_before_compact."""
    type: Literal["session_compact_failed"]
    reason: str            # "manual" | "threshold" | "overflow"
    errorMessage: str | None
    aborted: bool
    willRetry: bool
    fromExtension: bool


class SessionShutdownEvent(TypedDict):
    type: Literal["session_shutdown"]
    reason: Literal["quit", "reload", "new", "resume", "fork"]
    targetSessionFile: NotRequired[str]
    contextCarried: NotRequired[bool]


class SessionContextCarryEvent(TypedDict):
    type: Literal["session_context_carry"]
    sessionManager: SessionManager


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
    | SessionInfoChangedEvent
    | SessionBeforeSwitchEvent
    | SessionBeforeForkEvent
    | SessionBeforeCompactEvent
    | SessionContextPrepareEvent
    | SessionContextCarryEvent
    | SessionCompactEvent
    | SessionCompactFailedEvent
    | SessionShutdownEvent
    | SessionBeforeTreeEvent
    | SessionTreeEvent
)


class ContextEvent(TypedDict):
    """The conversation only: system messages belong to Pi and are restored after the handler."""

    type: Literal["context"]
    messages: list[AgentMessage]


class ContextWithSystemEvent(TypedDict):
    """The full transcript including system messages; runs after `context` handlers and
    the result is sent verbatim."""

    type: Literal["context_with_system"]
    messages: list[AgentMessage]


class CacheWarmingDecisionEvent(TypedDict):
    type: Literal["cache_warming_decision"]
    warmCost: float
    missCost: float
    continuationProbability: float
    action: Literal["warm", "stop"]


class BeforeProviderRequestEvent(TypedDict):
    type: Literal["before_provider_request"]
    payload: Any


class BeforeProviderHeadersEvent(TypedDict):
    """Fired after request headers are assembled, before the provider HTTP call.

    Handlers mutate ``headers`` in place (e.g. to inject tracing/session headers); the
    return value is ignored.  A ``None`` value deletes that header.
    """

    type: Literal["before_provider_headers"]
    headers: dict[str, str | None]


class AfterProviderResponseEvent(TypedDict):
    type: Literal["after_provider_response"]
    status: int
    headers: dict[str, str]


class ProviderStreamEvent(TypedDict):
    """Fired for a parsed provider stream event before misaka normalizes it."""

    type: Literal["provider_stream_event"]
    provider: str
    api: Api
    model: str
    data: Any


class _BeforeAgentStartEventBase(TypedDict):
    type: Literal["before_agent_start"]
    # The current system prompt, rendered from `systemPromptOptions` and earlier handler changes on
    # every read, so edits to the options show here.
    systemPrompt: str
    # Mutable, collection-complete prompt options for this run. Later handlers observe mutations made
    # by earlier handlers.
    systemPromptOptions: BuildSystemPromptOptions


class BeforeAgentStartUserEvent(_BeforeAgentStartEventBase):
    """A run started by a submitted prompt, RPC prompt, or `pi.sendUserMessage()`."""

    messageType: Literal["user"]
    # The raw user prompt text (after expansion).
    prompt: str
    # Images attached to the user prompt, if any.
    images: NotRequired[list[ImageContent]]


class BeforeAgentStartCustomEvent(_BeforeAgentStartEventBase):
    """A run started by `pi.sendMessage(..., {"triggerTurn": True})` while idle."""

    messageType: Literal["custom"]
    # The custom message that starts the run.
    message: CustomMessage[Any]


# Fired before a run starts from a user message or an idle custom message with `triggerTurn`.
type BeforeAgentStartEvent = BeforeAgentStartUserEvent | BeforeAgentStartCustomEvent


class AgentStartEvent(TypedDict):
    type: Literal["agent_start"]


class AgentEndEvent(TypedDict):
    type: Literal["agent_end"]
    messages: list[AgentMessage]


class AgentSettledEvent(TypedDict):
    """Fired after an agent run has fully settled: no automatic retry, compaction, or
    queued continuation will run."""

    type: Literal["agent_settled"]
    # Whether the run ended because it was aborted, for example with Escape.
    aborted: bool


class AgentEndEventResult(TypedDict, total=False):
    block: bool
    reason: str


class TurnStartEvent(TypedDict):
    type: Literal["turn_start"]
    turnIndex: int
    timestamp: int


class BoundaryContext(TypedDict):
    """What a boundary handler sees: the projection with the drafts so far applied."""

    contextEntries: list[Any]
    contextMessages: list[AgentMessage]
    llmMessages: list[Any]
    pendingMessages: list[AgentMessage]
    canContinue: bool


class TurnEndEvent(TypedDict):
    """An actionable boundary: return `{"entries": [...event.entries, draft], "continue": True}`
    to persist structural entries in order and ensure one next provider request."""

    type: Literal["turn_end"]
    turnIndex: int
    message: AgentMessage
    toolResults: list[ToolResultMessage]
    messageEntryId: str
    toolResultEntryIds: list[str]
    outcome: Literal["completed", "error", "aborted"]
    entries: list[Any]
    # The continuation decision so far.
    continue_: NotRequired[bool]
    context: BoundaryContext


class AgentBeforeSettleEvent(TypedDict):
    """The last boundary before the run settles; same contract as `turn_end`."""

    type: Literal["agent_before_settle"]
    outcome: Literal["completed", "error", "aborted"]
    entries: list[Any]
    continue_: NotRequired[bool]
    context: BoundaryContext


class BoundaryEventResult(TypedDict, total=False):
    entries: list[Any]
    # `continue` on the wire; spelled with a trailing underscore here because it is a keyword.
    continue_: bool


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
    # Set when another tool (for example a codemode script) made this call.
    parentToolCallId: NotRequired[str]


class ToolExecutionUpdateEvent(TypedDict):
    type: Literal["tool_execution_update"]
    toolCallId: str
    toolName: str
    args: Any
    partialResult: Any
    # Set when another tool (for example a codemode script) made this call.
    parentToolCallId: NotRequired[str]


class ToolExecutionEndEvent(TypedDict):
    type: Literal["tool_execution_end"]
    toolCallId: str
    toolName: str
    result: Any
    isError: bool
    # Milliseconds `execute()` took, measured with a monotonic clock; absent when the tool did not run.
    durationMs: NotRequired[int]
    # Set when another tool (for example a codemode script) made this call.
    parentToolCallId: NotRequired[str]


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
    streamingBehavior: NotRequired[Literal["steer", "followUp"]]


class UIPromptStartEvent(TypedDict):
    type: Literal["ui_prompt_start"]
    reason: Literal["ui_prompt"]
    kind: UIPromptKind
    title: NotRequired[str]


class UIPromptEndEvent(TypedDict):
    type: Literal["ui_prompt_end"]
    reason: Literal["ui_prompt"]
    kind: UIPromptKind
    title: NotRequired[str]


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
    # The call's id. For calls another tool made (with `parentToolCallId` set), misaka assigns
    # `<parent id>/<n>`; such ids never appear as tool calls or tool results in the transcript,
    # only in the parent result's `nestedCalls` record.
    toolCallId: str
    # Set when another tool (for example a codemode script) issued this call.
    parentToolCallId: NotRequired[str]


class BashToolCallEvent(ToolCallEventBase):
    toolName: Literal["bash"]
    input: BashToolInput


class PowerShellToolCallEvent(ToolCallEventBase):
    toolName: Literal["powershell"]
    input: PowerShellToolInput


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
    | PowerShellToolCallEvent
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
    # The call's id; `<parent id>/<n>` for nested calls, see `ToolCallEvent`.
    toolCallId: str
    # Set when another tool (for example a codemode script) issued this call.
    parentToolCallId: NotRequired[str]
    input: dict[str, Any]
    content: list[TextContent | ImageContent]
    # Machine-readable result for tools that declare an `outputSchema`. Handlers that redact
    # `content` should also replace this; replacing `content` alone drops it.
    structuredContent: NotRequired[Any]
    isError: bool
    # Usage from the tool execution itself, if available (pi types.ts ToolResultEventBase).
    usage: NotRequired[Usage]


class BashToolResultEvent(ToolResultEventBase):
    toolName: Literal["bash"]
    details: BashToolDetails | None


class PowerShellToolResultEvent(ToolResultEventBase):
    toolName: Literal["powershell"]
    details: PowerShellToolDetails | None


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
    | PowerShellToolResultEvent
    | ReadToolResultEvent
    | EditToolResultEvent
    | WriteToolResultEvent
    | GrepToolResultEvent
    | FindToolResultEvent
    | LsToolResultEvent
    | CustomToolResultEvent
)


type ExtensionEvent = (
    ProjectTrustEvent
    | ResourcesDiscoverEvent
    | McpServersChangeEvent
    | SessionEvent
    | ContextEvent
    | ContextWithSystemEvent
    | CacheWarmingDecisionEvent
    | BeforeProviderRequestEvent
    | BeforeProviderHeadersEvent
    | AfterProviderResponseEvent
    | ProviderStreamEvent
    | BeforeAgentStartEvent
    | AgentStartEvent
    | AgentEndEvent
    | AgentSettledEvent
    | TurnStartEvent
    | TurnEndEvent
    | AgentBeforeSettleEvent
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
    | UIPromptStartEvent
    | UIPromptEndEvent
    | ToolCallEvent
    | ToolResultEvent
)


class ContextEventResult(TypedDict, total=False):
    messages: list[AgentMessage]


type BeforeProviderRequestEventResult = Any


class ToolCallEventResult(TypedDict, total=False):
    # To change arguments, mutate event["input"] in place. Later handlers see the
    # mutation, and execution does not revalidate it (pi extensions/types.ts).
    block: bool
    reason: str
    # Hint that the agent should stop after the current tool batch when this call is blocked.
    # Early termination only happens when every finalized tool result in the batch sets it.
    terminate: bool


class UserBashEventResult(TypedDict, total=False):
    operations: BashOperations
    result: BashResult


class ToolResultEventResult(TypedDict, total=False):
    """Changes a ``tool_result`` handler makes. Omitted fields stay as they are, except that
    replacing ``content`` without returning ``structuredContent`` drops the structured content,
    because it may no longer match. Return it along with ``content`` to keep it."""

    content: list[TextContent | ImageContent]
    details: Any
    structuredContent: Any
    isError: bool
    usage: Usage


class MessageEndEventResult(TypedDict, total=False):
    message: AgentMessage


class BeforeAgentStartEventResult(TypedDict, total=False):
    message: _CustomMessagePayload
    # Becomes `forceSystemPrompt`: sent as the provider's leading system prompt without
    # being recorded; edit `event.systemPromptOptions` to change the structured prompt.
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
    usage: NotRequired[Usage]


class SessionBeforeTreeResult(TypedDict, total=False):
    cancel: bool
    summary: BranchSummaryPayload
    customInstructions: str
    replaceInstructions: bool
    label: str


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
    getSettings: GetSettingsHandler
    setActiveTools: SetActiveToolsHandler
    refreshTools: RefreshToolsHandler
    getCommands: GetCommandsHandler
    setModel: SetModelHandler
    getThinkingLevel: GetThinkingLevelHandler
    setThinkingLevel: SetThinkingLevelHandler
    flagValues: dict[str, bool | str] = field(default_factory=dict)
    pendingProviderRegistrations: list[PendingProviderRegistration] = field(default_factory=list)
    pendingNativeProviderRegistrations: list[PendingNativeProviderRegistration] = field(
        default_factory=list
    )
    # Virtual model registrations queued during extension loading, processed when runner binds.
    pendingVirtualModelRegistrations: list[PendingVirtualModelRegistration] = field(default_factory=list)
    # Create an extension context. Raises before the runner binds.
    createContext: Callable[[], ExtensionContext] = lambda: _raise_not_initialized()
    assertActive: Callable[[], None] = lambda: None
    invalidate: Callable[[str | None], None] = lambda _message=None: None
    trackEventBusSubscription: Callable[[Callable[[], None]], Callable[[], None]] = lambda unsubscribe: unsubscribe
    registerProvider: RegisterProviderHandler = lambda _name, _config, _extension_path=None: None
    registerNativeProvider: RegisterNativeProviderHandler = lambda _provider, _extension_path=None: None
    unregisterProvider: UnregisterProviderHandler = lambda _name, _extension_path=None: None
    # Servers registered with `pi.registerMcpServer()`.
    mcpServers: McpServerRegistry = field(default_factory=McpServerRegistry)
    registerVirtualModel: RegisterVirtualModelHandler = lambda _definition, _extension_path=None: None
    unregisterVirtualModel: UnregisterVirtualModelHandler = lambda _provider, _id: None


def _raise_not_initialized() -> Any:
    raise RuntimeError("Extension runtime not initialized")


@dataclass(slots=True)
class Extension:
    path: str
    resolvedPath: str
    sourceInfo: SourceInfo
    handlers: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    tools: dict[str, RegisteredTool] = field(default_factory=dict)
    webProviders: dict[str, WebSearchProvider] = field(default_factory=dict)
    browserProviders: dict[str, Any] = field(default_factory=dict)
    messageRenderers: dict[str, MessageRenderer[Any]] = field(default_factory=dict)
    markdownTransformer: MarkdownTransformer | None = None
    entryRenderers: dict[str, EntryRenderer[Any]] = field(default_factory=dict)
    toolRenderers: list[ToolRendererResolver] | None = None
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    flags: dict[str, ExtensionFlag] = field(default_factory=dict)
    shortcuts: dict[KeyId, ExtensionShortcut] = field(default_factory=dict)
    hidden: bool = False
    # See `InlineExtension`.
    replaceable: bool = False


@dataclass(slots=True)
class _LoadedExtension(Extension):
    promptPaths: list[str] = field(default_factory=list)
    themePaths: list[str] = field(default_factory=list)
    systemPrompt: str | None = None
    appendSystemPrompt: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LoadExtensionsResult:
    extensions: list[Extension]
    errors: list[dict[str, str]]
    runtime: ExtensionRuntime
    # `{path, warning}` entries. Upstream orders it before `runtime`; a dataclass field with a
    # default has to come last.
    warnings: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ExtensionError:
    extensionPath: str
    event: str
    error: str
    stack: str | None = None


class ExtensionContext(Protocol):
    ui: ExtensionUIContext
    # Current run mode. Guard terminal-only UI on `mode == "tui"` (pi types.ts:307,312-313).
    # pi's fourth value "rpc" has no counterpart: misaka removed RPC mode outright.
    mode: ExtensionMode
    hasUI: bool
    cwd: str
    sessionManager: ReadonlySessionManager
    modelRegistry: ModelRegistry
    model: Model[Any] | None
    # Models scoped to this session (resolved from `--models` / `enabledModels`), a
    # read-only snapshot; empty when no scoping is configured (pi types.ts:328).
    scopedModels: list[Any]
    # The session's current thinking level (pi types.ts:330).
    thinkingLevel: Any
    signal: AbortSignal | None

    def isIdle(self) -> bool: ...

    def isProjectTrusted(self) -> bool: ...

    def abort(self) -> None: ...

    def hasPendingMessages(self) -> bool: ...

    def shutdown(self) -> None: ...

    def getContextUsage(self) -> ContextUsage | None: ...

    def compact(self, options: CompactOptions | None = None) -> None: ...

    def getSystemPrompt(self) -> str: ...


class ExecuteToolOptions(TypedDict, total=False):
    """Options for ``ExtensionToolContext.executeTool``."""

    # Defaults to the calling tool's signal.
    signal: AbortSignal
    # Receives partial results of the nested tool, in addition to `tool_execution_update` events.
    onUpdate: AgentToolUpdateCallback


class ExtensionToolContext(ExtensionContext, Protocol):
    """Context passed to tool ``execute()`` in a session: the extension context plus
    ``executeTool()`` for running other tools through the same validation, hooks, and permission
    checks as model-issued calls.

    A tool wrapped with ``wrapToolDefinition()`` without a context factory, such as a built-in tool
    created with ``createBashTool()`` and run in a plain ``Agent`` or called directly, gets no
    context.
    """

    @property
    def tools(self) -> Sequence[AgentTool]:
        """Tools ``executeTool`` can call."""
        ...

    async def executeTool(
        self, name: str, args: Any, options: ExecuteToolOptions | None = None
    ) -> AgentToolCallOutcome:
        """Run another tool. The call gets the id ``<calling id>/<n>``, and the ``tool_call``,
        ``tool_result``, and ``tool_execution_*`` events carry ``parentToolCallId``. It does not
        appear in the transcript; a bounded record of it is kept as ``nestedCalls`` on the calling
        tool's result message.

        Never raises for tool failures: unknown tools, validation errors, blocked calls, and
        raised errors come back as ``isError: True``.
        """
        ...


class ExtensionCommandContext(ExtensionContext, Protocol):
    def getSystemPromptOptions(self) -> BuildSystemPromptOptions: ...

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
    getSettings: GetSettingsHandler
    setActiveTools: SetActiveToolsHandler
    refreshTools: RefreshToolsHandler
    getCommands: GetCommandsHandler
    setModel: SetModelHandler
    getThinkingLevel: GetThinkingLevelHandler
    setThinkingLevel: SetThinkingLevelHandler


class ExtensionContextActions(Protocol):
    getModel: Callable[[], Model[Any] | None]
    getScopedModels: Callable[[], list[Any]]
    isIdle: Callable[[], bool]
    isProjectTrusted: Callable[[], bool]
    getSignal: Callable[[], AbortSignal | None]
    abort: Callable[[], None]
    hasPendingMessages: Callable[[], bool]
    shutdown: Callable[[], None]
    getContextUsage: Callable[[], ContextUsage | None]
    compact: Callable[[CompactOptions | None], None]
    getSystemPrompt: Callable[[], str]
    getSystemPromptOptions: Callable[[], BuildSystemPromptOptions] | None
    # Runs a tool call on behalf of the tool call `callerId` (`ctx.executeTool()`).
    executeTool: Callable[[str, str, Any, ExecuteToolOptions], Awaitable[AgentToolCallOutcome]] | None
    # Tools `ctx.executeTool()` can call.
    getCallableTools: Callable[[], Sequence[AgentTool]] | None


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
    def on(self, event: Literal["project_trust"], handler: ProjectTrustHandler) -> None: ...

    @overload
    def on(self, event: Literal["resources_discover"], handler: ExtensionHandler[ResourcesDiscoverEvent, ResourcesDiscoverResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_start"], handler: ExtensionHandler[SessionStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_info_changed"], handler: ExtensionHandler[SessionInfoChangedEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_switch"], handler: ExtensionHandler[SessionBeforeSwitchEvent, SessionBeforeSwitchResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_fork"], handler: ExtensionHandler[SessionBeforeForkEvent, SessionBeforeForkResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_context_prepare"], handler: ExtensionHandler[SessionContextPrepareEvent, SessionContextPrepareResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_context_carry"], handler: ExtensionHandler[SessionContextCarryEvent, dict[str, Any]]) -> None: ...
    @overload
    def on(self, event: Literal["session_before_compact"], handler: ExtensionHandler[SessionBeforeCompactEvent, SessionBeforeCompactResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_compact"], handler: ExtensionHandler[SessionCompactEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_compact_failed"], handler: ExtensionHandler[SessionCompactFailedEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_shutdown"], handler: ExtensionHandler[SessionShutdownEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["mcp_servers_change"], handler: ExtensionHandler[McpServersChangeEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["session_before_tree"], handler: ExtensionHandler[SessionBeforeTreeEvent, SessionBeforeTreeResult]) -> None: ...

    @overload
    def on(self, event: Literal["session_tree"], handler: ExtensionHandler[SessionTreeEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["context"], handler: ExtensionHandler[ContextEvent, ContextEventResult]) -> None: ...

    @overload
    def on(
        self, event: Literal["context_with_system"], handler: ExtensionHandler[ContextWithSystemEvent, ContextEventResult]
    ) -> Callable[[], None]: ...

    @overload
    def on(
        self, event: Literal["cache_warming_decision"], handler: ExtensionHandler[CacheWarmingDecisionEvent, Any]
    ) -> Callable[[], None]: ...

    @overload
    def on(self, event: Literal["before_provider_request"], handler: ExtensionHandler[BeforeProviderRequestEvent, BeforeProviderRequestEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["before_provider_headers"], handler: ExtensionHandler[BeforeProviderHeadersEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["after_provider_response"], handler: ExtensionHandler[AfterProviderResponseEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["provider_stream_event"], handler: ExtensionHandler[ProviderStreamEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["before_agent_start"], handler: ExtensionHandler[BeforeAgentStartEvent, BeforeAgentStartEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["agent_start"], handler: ExtensionHandler[AgentStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["agent_end"], handler: ExtensionHandler[AgentEndEvent, AgentEndEventResult]) -> None: ...

    @overload
    def on(self, event: Literal["agent_settled"], handler: ExtensionHandler[AgentSettledEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["turn_start"], handler: ExtensionHandler[TurnStartEvent, None]) -> None: ...

    @overload
    def on(
        self, event: Literal["turn_end"], handler: ExtensionHandler[TurnEndEvent, BoundaryEventResult | None]
    ) -> Callable[[], None]: ...

    @overload
    def on(
        self,
        event: Literal["agent_before_settle"],
        handler: ExtensionHandler[AgentBeforeSettleEvent, BoundaryEventResult | None],
    ) -> Callable[[], None]: ...

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

    @overload
    def on(self, event: Literal["ui_prompt_start"], handler: ExtensionHandler[UIPromptStartEvent, None]) -> None: ...

    @overload
    def on(self, event: Literal["ui_prompt_end"], handler: ExtensionHandler[UIPromptEndEvent, None]) -> None: ...

    def on(self, event: str, handler: Callable[..., Any]) -> None: ...

    def registerTool(self, tool: ToolDefinition[Any, Any]) -> None: ...

    def registerWebSearchProvider(self, provider: WebSearchProvider) -> None:
        """Register a WebSearchProvider owned by this extension, not a model provider."""
        ...

    def unregisterWebSearchProvider(self, name: str) -> None:
        """Remove this extension's registration; reveal the previous owner, if any."""
        ...

    def registerBrowserProvider(self, provider: Any) -> None: ...

    def unregisterBrowserProvider(self, name: str) -> None: ...

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
        options: _BooleanExtensionFlagOptions | _StringExtensionFlagOptions,
    ) -> None: ...

    def registerMessageRenderer(self, customType: str, renderer: MessageRenderer[Any]) -> None: ...

    def registerMarkdownTransformer(self, transformer: MarkdownTransformer) -> None: ...

    def registerEntryRenderer(self, customType: str, renderer: EntryRenderer[Any]) -> None: ...

    def registerToolRenderer(self, resolver: ToolRendererResolver) -> None:
        """Choose how tool calls are drawn. Resolvers run in extension load order."""
        ...

    def getFlag(self, name: str) -> bool | str | None: ...

    def sendMessage(self, message: _CustomMessagePayload, options: _SendMessageOptions | None = None) -> None: ...

    def sendUserMessage(
        self,
        content: str | list[TextContent | ImageContent],
        options: _SendUserMessageOptions | None = None,
    ) -> None: ...

    @overload
    def appendEntry(self, customType: str) -> None: ...

    @overload
    def appendEntry(self, customType: str, data: Any) -> None: ...

    def setSessionName(self, name: str) -> None: ...

    def getSessionName(self) -> str | None: ...

    def setLabel(self, entryId: str, label: str | None) -> None: ...

    async def exec(self, command: str, args: list[str], options: ExecOptions | None = None) -> ExecResult: ...

    def getActiveTools(self) -> list[str]:
        """Get the names of the active tools, which are the tools declared to the model."""
        ...

    def getAllTools(self) -> list[ToolInfo]:
        """Get all configured tools with parameter schema, prompt guidelines, exposure, and source
        metadata."""
        ...

    def getSettings(self) -> Settings:
        """Get a copy of the effective settings (global and project settings merged, with
        overrides)."""
        ...

    def setActiveTools(self, toolNames: list[str]) -> None:
        """Set the active tools by name. Unknown and ``hidden`` tools are ignored. Tools with
        ``codemode`` or ``deferred`` exposure stay callable from codemode scripts whether active or
        not."""
        ...

    def getCommands(self) -> list[SlashCommandInfo]: ...

    async def setModel(self, model: Model[Any]) -> bool: ...

    def getThinkingLevel(self) -> ThinkingLevel: ...

    def setThinkingLevel(self, level: ThinkingLevel) -> None: ...

    @overload
    def registerProvider(self, provider: RuntimeProvider) -> None: ...

    @overload
    def registerProvider(self, name: str, config: ProviderConfig) -> None: ...

    def registerProvider(
        self,
        providerOrName: RuntimeProvider | str,
        config: ProviderConfig | None = None,
    ) -> None: ...

    def unregisterProvider(self, name: str) -> None: ...

    def registerMcpServer(self, name: str, config: McpServerConfig) -> None:
        """Register an MCP server for this session, with the same config as an ``mcpServers``
        entry in ``mcp.json``. The server connects next to the configured servers: on
        ``session_start`` when registered during extension load, right away when registered later.
        Registering a name again replaces the extension's earlier registration.

        The registration is not saved; register again on every load. A server of the same name in
        ``mcp.json`` takes precedence. Raises for invalid configs and for names another extension
        registered. When no loaded extension handles MCP servers (for example because another MCP
        extension replaced the built-in one), the registration is reported as an extension error.
        """
        ...

    def unregisterMcpServer(self, name: str) -> None:
        """Remove an MCP server this extension registered and close its connection."""
        ...

    def getMcpServers(self) -> list[RegisteredMcpServer]:
        """Every MCP server registered by extensions. For extensions that connect MCP servers."""
        ...

    def registerVirtualModel(self, model: ExtensionVirtualModel) -> None:
        """Register a virtual model: a selectable catalog entry that routes each request to a
        physical model. The selection (``ctx.model``, ``model_change`` entries) names the virtual
        model; assistant messages record the physical model and thinking level the router picked.

        ``provider`` may be any provider id, including one with physical models, and may list
        several virtual models. Registering the same provider and id again replaces the virtual
        model.
        """
        ...

    def unregisterVirtualModel(self, provider: str, id: str) -> None:
        """Remove a virtual model registered with ``registerVirtualModel()``."""
        ...


type ExtensionFactory = Callable[[ExtensionAPI], Awaitable[None] | None]


class NamedInlineExtension(TypedDict):
    # Display name shown as `<inline:name>` in the startup Extensions list and errors. With
    # `builtin`, the extension is named `builtin:name` in errors and diagnostics.
    name: str
    factory: ExtensionFactory
    # Omit this extension from the startup Extensions list.
    hidden: NotRequired[bool]
    # Leave this extension out when another extension registers a tool, command, or flag with a
    # name it registers during loading, instead of reporting a conflict. The built-in MCP,
    # codemode, and tool search extensions use it, so for example an MCP extension that registers
    # `/mcp` replaces the built-in MCP support. The factory still runs, so it should only register
    # tools, commands, flags, and event handlers.
    replaceable: NotRequired[bool]
    # Supply the code of the `builtin:<name>` extension instead of loading as an inline extension.
    # `builtin:<name>` is an extension resource like a file: it loads by default, `-builtin:<name>`
    # in the `extensions` setting and `--no-extensions` disable it, and `-e builtin:<name>` loads
    # it explicitly. It is hidden from the startup Extensions list and loads after project trust
    # is resolved, so it cannot handle `project_trust`.
    builtin: NotRequired[bool]


# Mirrors pi types.ts InlineExtension: a bare factory, or a named descriptor.
type InlineExtension = ExtensionFactory | NamedInlineExtension


def defineTool[TTool: ToolDefinition[Any, Any]](tool: TTool) -> TTool:
    return tool


def isBashToolResult(event: ToolResultEvent) -> TypeGuard[BashToolResultEvent]:
    return event["toolName"] == "bash"


def isPowerShellToolResult(
    event: ToolResultEvent,
) -> TypeGuard[PowerShellToolResultEvent]:
    return event["toolName"] == "powershell"


def isReadToolResult(event: ToolResultEvent) -> TypeGuard[ReadToolResultEvent]:
    return event["toolName"] == "read"


def isEditToolResult(event: ToolResultEvent) -> TypeGuard[EditToolResultEvent]:
    return event["toolName"] == "edit"


def isWriteToolResult(event: ToolResultEvent) -> TypeGuard[WriteToolResultEvent]:
    return event["toolName"] == "write"


def isGrepToolResult(event: ToolResultEvent) -> TypeGuard[GrepToolResultEvent]:
    return event["toolName"] == "grep"


def isFindToolResult(event: ToolResultEvent) -> TypeGuard[FindToolResultEvent]:
    return event["toolName"] == "find"


def isLsToolResult(event: ToolResultEvent) -> TypeGuard[LsToolResultEvent]:
    return event["toolName"] == "ls"


@overload
def isToolCallEventType(tool_name: Literal["bash"], event: ToolCallEvent) -> TypeGuard[BashToolCallEvent]: ...


@overload
def isToolCallEventType(
    tool_name: Literal["powershell"], event: ToolCallEvent
) -> TypeGuard[PowerShellToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["read"], event: ToolCallEvent) -> TypeGuard[ReadToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["edit"], event: ToolCallEvent) -> TypeGuard[EditToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["write"], event: ToolCallEvent) -> TypeGuard[WriteToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["grep"], event: ToolCallEvent) -> TypeGuard[GrepToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["find"], event: ToolCallEvent) -> TypeGuard[FindToolCallEvent]: ...


@overload
def isToolCallEventType(tool_name: Literal["ls"], event: ToolCallEvent) -> TypeGuard[LsToolCallEvent]: ...


def isToolCallEventType(tool_name: str, event: ToolCallEvent) -> bool:
    return event["toolName"] == tool_name




__all__ = [
    "AfterProviderResponseEvent",
    "AgentEndEvent",
    "AgentEndEventResult",
    "AgentSettledEvent",
    "AgentStartEvent",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AppKeybinding",
    "AppendEntryHandler",
    "AutocompleteProviderFactory",
    "BashToolCallEvent",
    "BashToolResultEvent",
    "BeforeAgentStartCustomEvent",
    "BeforeAgentStartEvent",
    "BeforeAgentStartEventResult",
    "BeforeAgentStartUserEvent",
    "BeforeProviderHeadersEvent",
    "BeforeProviderRequestEvent",
    "BeforeProviderRequestEventResult",
    "BuildSystemPromptOptions",
    "CompactOptions",
    "ContextEvent",
    "ContextEventResult",
    "ContextUsage",
    "CustomToolCallEvent",
    "CustomToolResultEvent",
    "EditToolCallEvent",
    "EditToolResultEvent",
    "EditorFactory",
    "EntryRenderOptions",
    "EntryRenderer",
    "ExecOptions",
    "ExecResult",
    "ExecuteToolOptions",
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
    "ExtensionMode",
    "ExtensionRuntime",
    "ExtensionShortcut",
    "ExtensionToolContext",
    "ExtensionUIContext",
    "ExtensionUIDialogOptions",
    "ExtensionWidgetOptions",
    "FindToolCallEvent",
    "FindToolResultEvent",
    "GetActiveToolsHandler",
    "GetAllToolsHandler",
    "GetCommandsHandler",
    "GetSessionNameHandler",
    "GetSettingsHandler",
    "GetThinkingLevelHandler",
    "GrepToolCallEvent",
    "GrepToolResultEvent",
    "InlineExtension",
    "InputEvent",
    "InputEventResult",
    "InputSource",
    "KeybindingsManager",
    "LoadExtensionsResult",
    "LsToolCallEvent",
    "LsToolResultEvent",
    "MarkdownTransformContext",
    "MarkdownTransformer",
    "McpServersChangeEvent",
    "MessageEndEvent",
    "MessageEndEventResult",
    "MessageRenderOptions",
    "MessageRenderer",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ModelSelectEvent",
    "ModelSelectSource",
    "NamedInlineExtension",
    "PowerShellToolCallEvent",
    "PowerShellToolResultEvent",
    "ProjectTrustContext",
    "ProjectTrustEvent",
    "ProjectTrustEventDecision",
    "ProjectTrustEventResult",
    "ProjectTrustHandler",
    "ProviderChatModelConfig",
    "ProviderClassifierModelConfig",
    "ProviderConfig",
    "ProviderImageModelConfig",
    "ProviderModelConfig",
    "ProviderStreamEvent",
    "ReadToolCallEvent",
    "ReadToolResultEvent",
    "RefreshToolsHandler",
    "RegisteredCommand",
    "RegisteredTool",
    "ReplacedSessionContext",
    "ResolvedCommand",
    "ResourcesDiscoverEvent",
    "ResourcesDiscoverResult",
    "SendMessageHandler",
    "SendUserMessageHandler",
    "SessionBeforeCompactEvent",
    "SessionBeforeCompactResult",
    "SessionBeforeForkEvent",
    "SessionBeforeForkResult",
    "SessionBeforeSwitchEvent",
    "SessionBeforeSwitchResult",
    "SessionBeforeTreeEvent",
    "SessionBeforeTreeResult",
    "SessionCompactEvent",
    "SessionContextCarryEvent",
    "SessionContextPrepareEvent",
    "SessionContextPrepareResult",
    "SessionEvent",
    "SessionInfoChangedEvent",
    "SessionShutdownEvent",
    "SessionStartEvent",
    "SessionTreeEvent",
    "SetActiveToolsHandler",
    "SetLabelHandler",
    "SetModelHandler",
    "SetSessionNameHandler",
    "SetThinkingLevelHandler",
    "TerminalInputHandler",
    "ThinkingLevelSelectEvent",
    "ToolAnnotations",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolDefinition",
    "ToolExecutionEndEvent",
    "ToolExecutionMode",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolExposure",
    "ToolInfo",
    "ToolLoadout",
    "ToolLoadoutChanges",
    "ToolNamespace",
    "ToolRenderContext",
    "ToolRenderResultOptions",
    "ToolResultEvent",
    "ToolResultEventResult",
    "TreePreparation",
    "TurnEndEvent",
    "TurnStartEvent",
    "UIPromptEndEvent",
    "UIPromptKind",
    "UIPromptStartEvent",
    "UserBashEvent",
    "UserBashEventResult",
    "WidgetPlacement",
    "WorkingIndicatorOptions",
    "WriteToolCallEvent",
    "WriteToolResultEvent",
    "defineTool",
    "isBashToolResult",
    "isEditToolResult",
    "isFindToolResult",
    "isGrepToolResult",
    "isLsToolResult",
    "isPowerShellToolResult",
    "isReadToolResult",
    "isToolCallEventType",
    "isWriteToolResult",
]
