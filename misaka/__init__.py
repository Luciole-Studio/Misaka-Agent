"""Public exports for the misaka package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {}


def _register(module_name: str, *names: str) -> None:
    for name in names:
        _EXPORTS[name] = (module_name, name)


_register("misaka.config", "getAgentDir", "VERSION")
_register(
    "misaka.core.agent_session",
    "AgentSession",
    "AgentSessionConfig",
    "AgentSessionEvent",
    "AgentSessionEventListener",
    "ModelCycleResult",
    "ParsedSkillBlock",
    "PromptOptions",
    "parseSkillBlock",
    "SessionStats",
)
_register(
    "misaka.core.auth_storage",
    "ApiKeyCredential",
    "AuthCredential",
    "AuthStatus",
    "AuthStorage",
    "AuthStorageBackend",
    "FileAuthStorageBackend",
    "InMemoryAuthStorageBackend",
    "OAuthCredential",
)
_register(
    "misaka.core.compaction",
    "BranchPreparation",
    "BranchSummaryResult",
    "CollectEntriesResult",
    "CompactionResult",
    "CutPointResult",
    "calculateContextTokens",
    "collectEntriesForBranchSummary",
    "compact",
    "DEFAULT_COMPACTION_SETTINGS",
    "estimateTokens",
    "FileOperations",
    "findCutPoint",
    "findTurnStartIndex",
    "GenerateBranchSummaryOptions",
    "generateBranchSummary",
    "generateSummary",
    "getLastAssistantUsage",
    "prepareBranchEntries",
    "serializeConversation",
    "shouldCompact",
)
_register("misaka.core.event_bus", "createEventBus", "EventBus", "EventBusController")
_register(
    "misaka.core.extensions",
    "AgentEndEvent",
    "AgentStartEvent",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AppKeybinding",
    "AutocompleteProviderFactory",
    "BashToolCallEvent",
    "BeforeAgentStartEvent",
    "BeforeAgentStartEventResult",
    "BeforeProviderRequestEvent",
    "BeforeProviderRequestEventResult",
    "BuildSystemPromptOptions",
    "CompactOptions",
    "ContextEvent",
    "ContextUsage",
    "CustomToolCallEvent",
    "EditToolCallEvent",
    "ExecOptions",
    "ExecResult",
    "Extension",
    "ExtensionActions",
    "ExtensionAPI",
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
    "ExtensionShortcut",
    "ExtensionUIContext",
    "ExtensionUIDialogOptions",
    "ExtensionWidgetOptions",
    "FindToolCallEvent",
    "GrepToolCallEvent",
    "InputEvent",
    "InputEventResult",
    "InputSource",
    "KeybindingsManager",
    "LoadExtensionsResult",
    "LsToolCallEvent",
    "MessageRenderer",
    "MessageRenderOptions",
    "ProviderConfig",
    "ProviderModelConfig",
    "ReadToolCallEvent",
    "RegisteredCommand",
    "RegisteredTool",
    "ResolvedCommand",
    "SessionBeforeCompactEvent",
    "SessionBeforeForkEvent",
    "SessionBeforeSwitchEvent",
    "SessionBeforeTreeEvent",
    "SessionCompactEvent",
    "SessionShutdownEvent",
    "SessionStartEvent",
    "SessionTreeEvent",
    "SlashCommandInfo",
    "SlashCommandSource",
    "SourceInfo",
    "TerminalInputHandler",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolDefinition",
    "ToolExecutionMode",
    "ToolInfo",
    "ToolRenderResultOptions",
    "ToolResultEvent",
    "TurnEndEvent",
    "TurnStartEvent",
    "UserBashEvent",
    "UserBashEventResult",
    "WidgetPlacement",
    "WorkingIndicatorOptions",
    "WriteToolCallEvent",
    "createExtensionRuntime",
    "defineTool",
    "discoverAndLoadExtensions",
    "ExtensionRunner",
    "isBashToolResult",
    "isEditToolResult",
    "isFindToolResult",
    "isGrepToolResult",
    "isLsToolResult",
    "isReadToolResult",
    "isToolCallEventType",
    "isWriteToolResult",
    "wrapRegisteredTool",
    "wrapRegisteredTools",
)
_register("misaka.core.footer_data_provider", "ReadonlyFooterDataProvider")
_register("misaka.core.messages", "convertToLlm")
_register("misaka.core.model_registry", "ModelRegistry")
_register(
    "misaka.core.package_manager",
    "PackageManager",
    "PathMetadata",
    "ProgressCallback",
    "ProgressEvent",
    "ResolvedPaths",
    "ResolvedResource",
    "DefaultPackageManager",
)
_register(
    "misaka.core.resource_loader",
    "ResourceCollision",
    "ResourceDiagnostic",
    "ResourceLoader",
    "DefaultResourceLoader",
    "loadProjectContextFiles",
)
_register(
    "misaka.core.sdk",
    "AgentSessionRuntime",
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionOptions",
    "CreateAgentSessionResult",
    "CreateAgentSessionRuntimeFactory",
    "CreateAgentSessionRuntimeResult",
    "CreateAgentSessionServicesOptions",
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
    "PromptTemplate",
)
_register(
    "misaka.core.session_manager",
    "BranchSummaryEntry",
    "buildSessionContext",
    "CompactionEntry",
    "CURRENT_SESSION_VERSION",
    "CustomEntry",
    "CustomMessageEntry",
    "FileEntry",
    "getLatestCompactionEntry",
    "ModelChangeEntry",
    "migrateSessionEntries",
    "NewSessionOptions",
    "parseSessionEntries",
    "SessionContext",
    "SessionEntry",
    "SessionEntryBase",
    "SessionHeader",
    "SessionInfo",
    "SessionInfoEntry",
    "SessionManager",
    "SessionMessageEntry",
    "ThinkingLevelChangeEntry",
)
_register(
    "misaka.core.settings_manager",
    "CompactionSettings",
    "ImageSettings",
    "PackageSource",
    "RetrySettings",
    "SettingsManager",
)
_register(
    "misaka.core.skills",
    "formatSkillsForPrompt",
    "LoadSkillsFromDirOptions",
    "LoadSkillsResult",
    "loadSkills",
    "loadSkillsFromDir",
    "Skill",
    "SkillFrontmatter",
)
_register("misaka.core.source_info", "createSyntheticSourceInfo")
_register(
    "misaka.core.tools",
    "BashOperations",
    "BashSpawnContext",
    "BashSpawnHook",
    "BashToolDetails",
    "BashToolInput",
    "BashToolOptions",
    "createBashToolDefinition",
    "createEditToolDefinition",
    "createFindToolDefinition",
    "createGrepToolDefinition",
    "createLocalBashOperations",
    "createLsToolDefinition",
    "createReadToolDefinition",
    "createWriteToolDefinition",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "EditOperations",
    "EditToolDetails",
    "EditToolInput",
    "EditToolOptions",
    "FindOperations",
    "FindToolDetails",
    "FindToolInput",
    "FindToolOptions",
    "formatSize",
    "GrepOperations",
    "GrepToolDetails",
    "GrepToolInput",
    "GrepToolOptions",
    "LsOperations",
    "LsToolDetails",
    "LsToolInput",
    "LsToolOptions",
    "ReadOperations",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    "ToolsOptions",
    "TruncationOptions",
    "TruncationResult",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "WriteOperations",
    "WriteToolInput",
    "WriteToolOptions",
    "withFileMutationQueue",
)
_register("misaka.cli.engine", "MainOptions", "main")
_register(
    "misaka.ui.tui.interactive.interactive_mode",
    "InteractiveMode",
    "InteractiveModeOptions",
)
_register("misaka.modes.rpc.rpc_client", "ModelInfo")
_register("misaka.modes.print_mode", "PrintModeOptions")
_register(
    "misaka.modes.rpc.rpc_client",
    "RpcClient",
    "RpcClientOptions",
)
_register(
    "misaka.modes.rpc.rpc_types",
    "RpcCommand",
)
_register("misaka.modes.rpc.rpc_client", "RpcEventListener")
_register(
    "misaka.modes.rpc.rpc_types",
    "RpcResponse",
    "RpcSessionState",
)
_register("misaka.modes.print_mode", "runPrintMode")
_register("misaka.modes.rpc.rpc_mode", "runRpcMode")
_register(
    "misaka.ui.tui.interactive.components",
    "AssistantMessageComponent",
    "BashExecutionComponent",
    "BorderedLoader",
    "BranchSummaryMessageComponent",
    "CompactionSummaryMessageComponent",
    "CustomEditor",
    "CustomMessageComponent",
    "DynamicBorder",
    "ExtensionEditorComponent",
    "ExtensionInputComponent",
    "ExtensionSelectorComponent",
    "FooterComponent",
    "keyHint",
    "keyText",
    "LoginDialogComponent",
    "ModelSelectorComponent",
    "OAuthSelectorComponent",
    "RenderDiffOptions",
    "rawKeyHint",
    "renderDiff",
    "SessionSelectorComponent",
    "SettingsCallbacks",
    "SettingsConfig",
    "SettingsSelectorComponent",
    "ShowImagesSelectorComponent",
    "SkillInvocationMessageComponent",
    "ThemeSelectorComponent",
    "ThinkingSelectorComponent",
    "ToolExecutionComponent",
    "ToolExecutionOptions",
    "TreeSelectorComponent",
    "truncateToVisualLines",
    "UserMessageComponent",
    "UserMessageSelectorComponent",
    "VisualTruncateResult",
)
_register(
    "misaka.ui.tui.interactive.theme.theme",
    "getLanguageFromPath",
    "getMarkdownTheme",
    "getSelectListTheme",
    "getSettingsListTheme",
    "highlightCode",
    "initTheme",
    "Theme",
    "ThemeColor",
)
_register("misaka.utils.clipboard", "copyToClipboard")
_register("misaka.utils.frontmatter", "parseFrontmatter", "stripFrontmatter")
_register("misaka.utils.image_resize", "formatDimensionNote", "ResizedImage", "resizeImage")
_register("misaka.utils.shell", "getShellConfig")

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    module = import_module(module_name)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
