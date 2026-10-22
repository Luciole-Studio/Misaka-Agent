"""Extension runner - executes extensions and manages their lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import sys
import traceback
from collections.abc import Callable, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from misaka.agent.types import AgentToolCallOutcome, AgentToolResult
from misaka.ai.types import ImageContent, Model, TextContent, ToolCall
from misaka.ai.utils.transcript import getCurrentSystemMessage
from misaka.core.diagnostics import ResourceDiagnostic
from misaka.core.extensions.types import (
    EntryRenderer,
    Extension,
    ExtensionError,
    ExtensionFlag,
    ExtensionMode,
    ExtensionRuntime,
    ExtensionShortcut,
    InputSource,
    LoadExtensionsResult,
    MarkdownTransformer,
    ProjectTrustContext,
    ProjectTrustEvent,
    ProviderConfig,
    RegisteredCommand,
    RegisteredTool,
    ResolvedCommand,
    ToolDefinition,
    ToolRenderers,
    UIPromptKind,
)
from misaka.core.system_prompt import (
    BuildSystemPromptOptions,
    buildSystemPrompt,
    normalizeBuildSystemPromptOptions,
)
from misaka.core.virtual_models import VirtualModelDefinition
from misaka.modes.interactive.theme.theme import theme
from misaka.utils.values import maybe_await

RESERVED_KEYBINDINGS_FOR_EXTENSION_CONFLICTS = (
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
    "app.editor.external",
    "app.message.followUp",
    "tui.input.submit",
    "tui.select.confirm",
    "tui.select.cancel",
    "tui.input.copy",
    "tui.editor.deleteToLineEnd",
)

type ExtensionErrorListener = Any
type NewSessionHandler = Any
type ForkHandler = Any
type NavigateTreeHandler = Any
type SwitchSessionHandler = Any
type ReloadHandler = Any
type ShutdownHandler = Any

_MISSING = object()


def isUserBashEventResult(value: Any) -> bool:
    if not isinstance(value, Mapping) and not hasattr(value, "__dict__"):
        return False
    operations = _result_flag(value, "operations", None)
    result = _result_flag(value, "result", None)
    has_operations = operations is not None
    has_result = result is not None
    if has_operations == has_result:
        return False
    if has_operations:
        return callable(_result_flag(operations, "exec", None))
    output = _result_flag(result, "output", None)
    exit_code = _result_flag(result, "exitCode", _MISSING)
    full_output_path = _result_flag(result, "fullOutputPath", None)
    return (
        isinstance(output, str)
        and exit_code is not _MISSING
        and (exit_code is None or isinstance(exit_code, int))
        and isinstance(_result_flag(result, "cancelled", None), bool)
        and isinstance(_result_flag(result, "truncated", None), bool)
        and (full_output_path is None or isinstance(full_output_path, str))
    )


class _BeforeAgentStartUserTrigger(TypedDict):
    """``Pick<BeforeAgentStartUserEvent, "messageType" | "prompt" | "images">``."""

    messageType: Literal["user"]
    prompt: str
    images: NotRequired[list[ImageContent] | None]


class _BeforeAgentStartCustomTrigger(TypedDict):
    """``Pick<BeforeAgentStartCustomEvent, "messageType" | "message">``."""

    messageType: Literal["custom"]
    message: Any


# The message that starts a run: the before_agent_start event without its prompt fields.
type BeforeAgentStartTrigger = _BeforeAgentStartUserTrigger | _BeforeAgentStartCustomTrigger


class _BeforeAgentStartEvent(dict[str, Any]):
    """The `before_agent_start` event: `systemPrompt` renders the current options on every
    read (pi's `get systemPrompt()`), so a handler that edited `systemPromptOptions` sees the
    result. Mapping-shaped like every other event, so handlers read it the same way."""

    def __init__(self, *, trigger: BeforeAgentStartTrigger, systemPromptOptions: Any, render: Any) -> None:
        # `{ ...trigger, type, systemPrompt, systemPromptOptions }`
        super().__init__(trigger)
        self["type"] = "before_agent_start"
        self["systemPromptOptions"] = systemPromptOptions
        self._render = render

    def __getitem__(self, key: str) -> Any:
        if key == "systemPrompt":
            return self._render()
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "systemPrompt":
            return self._render()
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key == "systemPrompt" or super().__contains__(key)

    def __getattr__(self, name: str) -> Any:
        if name == "systemPrompt":
            return self._render()
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def snapshotEventHandlers(extensions: list[Any], event: str) -> list[tuple[Any, list[Any]]]:
    """Handlers added or removed during a dispatch apply to later dispatches, not the current one."""
    return [(extension, list(extension.handlers.get(event) or [])) for extension in extensions]


def sameMessages(left: list[Any], right: list[Any]) -> bool:
    return len(left) == len(right) and all(message is right[index] for index, message in enumerate(left))


def restoreSystemMessages(current: list[Any], visible: list[Any], returned: list[Any]) -> list[Any]:
    """Re-attach the prompt and tool state after a `context` handler. Handlers only see the
    conversation; the system messages belong to Pi. An unchanged conversation keeps every
    system message in place, so models with mid-conversation support keep their cached
    prefix. A changed one gets the replayed prompt sections and tool declarations as one
    leading system message, so pruning, windowing, or slicing from a compaction summary
    cannot drop them."""
    if sameMessages(returned, visible):
        return current
    head = getCurrentSystemMessage(current)
    return [head, *returned] if head else returned


@dataclass(slots=True)
class _NoUIContext:
    theme: Any = theme

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

    def getTheme(self, _name: str | None = None) -> Any:
        return None

    def setTheme(self, _theme: str | Any) -> dict[str, Any]:
        return {"success": False, "error": "UI not available"}

    def getToolsExpanded(self) -> bool:
        return False

    def setToolsExpanded(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _UIPromptUIContext:
    """Transparent UI proxy that adds Pi's blocking-prompt lifecycle events."""

    def __init__(self, runner: ExtensionRunner, ui: Any) -> None:
        self._runner = runner
        self._ui = ui

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ui, name)

    async def select(self, title: str, *args: Any, **kwargs: Any) -> Any:
        return await self._runner._with_ui_prompt(
            "select", title, self._ui.select, title, *args, **kwargs
        )

    async def confirm(self, title: str, *args: Any, **kwargs: Any) -> Any:
        return await self._runner._with_ui_prompt(
            "confirm", title, self._ui.confirm, title, *args, **kwargs
        )

    async def input(self, title: str, *args: Any, **kwargs: Any) -> Any:
        return await self._runner._with_ui_prompt(
            "input", title, self._ui.input, title, *args, **kwargs
        )

    async def editor(self, title: str, *args: Any, **kwargs: Any) -> Any:
        return await self._runner._with_ui_prompt(
            "editor", title, self._ui.editor, title, *args, **kwargs
        )

    async def custom(self, *args: Any, **kwargs: Any) -> Any:
        return await self._runner._with_ui_prompt("custom", None, self._ui.custom, *args, **kwargs)


class _ContextBase:
    def __init__(self, runner: ExtensionRunner, extras: Mapping[str, Any] | None = None) -> None:
        self._runner = runner
        self._extras = dict(extras or {})

    def _extra(self, name: str, default: Any) -> Any:
        return self._extras.get(name, default)

    @property
    def ui(self) -> Any:
        self._runner.assertActive()
        return self._extra("ui", self._runner.uiContext)

    @property
    def mode(self) -> Any:
        self._runner.assertActive()
        return self._extra("mode", self._runner.mode)

    @property
    def hasUI(self) -> bool:
        self._runner.assertActive()
        return self._extra("hasUI", self._runner.hasUI())

    @property
    def cwd(self) -> str:
        self._runner.assertActive()
        return self._extra("cwd", self._runner.cwd)

    @property
    def sessionManager(self) -> Any:
        self._runner.assertActive()
        return self._extra("sessionManager", self._runner.sessionManager)

    @property
    def modelRegistry(self) -> Any:
        self._runner.assertActive()
        return self._extra("modelRegistry", self._runner.modelRegistry)

    @property
    def model(self) -> Model[Any] | None:
        self._runner.assertActive()
        return self._extra("model", self._runner.getModel())

    @property
    def scopedModels(self) -> list[Any]:
        """Read-only snapshot of the session's scoped models (pi runner.ts:756)."""
        self._runner.assertActive()
        return self._extra("scopedModels", self._runner.getScopedModels())

    @property
    def thinkingLevel(self) -> Any:
        """The session's current thinking level (pi runner.ts:760)."""
        self._runner.assertActive()
        return self._extra("thinkingLevel", self._runner.runtime.getThinkingLevel())

    @property
    def signal(self) -> Any | None:
        self._runner.assertActive()
        return self._extra("signal", self._runner.getSignalFn())

    def isIdle(self) -> bool:
        self._runner.assertActive()
        return self._extra("isIdle", self._runner.isIdleFn())  # type: ignore[no-any-return]

    def isProjectTrusted(self) -> bool:
        self._runner.assertActive()
        return self._runner.isProjectTrustedFn()  # type: ignore[no-any-return]

    def abort(self) -> None:
        self._runner.assertActive()
        abort = self._extras.get("abort")
        if callable(abort):
            abort()
            return
        self._runner.abortFn()

    def hasPendingMessages(self) -> bool:
        self._runner.assertActive()
        return self._extra("hasPendingMessages", self._runner.hasPendingMessagesFn())  # type: ignore[no-any-return]

    def shutdown(self) -> None:
        self._runner.assertActive()
        shutdown = self._extras.get("shutdown")
        if callable(shutdown):
            shutdown()
            return
        self._runner.shutdownHandler()

    def getContextUsage(self) -> Any:
        self._runner.assertActive()
        get_context_usage = self._extras.get("getContextUsage")
        if callable(get_context_usage):
            return get_context_usage()
        return self._runner.getContextUsageFn()

    def compact(self, options: dict[str, Any] | None = None) -> None:
        self._runner.assertActive()
        compact = self._extras.get("compact")
        if callable(compact):
            compact(options)
            return
        self._runner.compactFn(options)

    def getSystemPrompt(self) -> str:
        self._runner.assertActive()
        get_system_prompt = self._extras.get("getSystemPrompt")
        if callable(get_system_prompt):
            return get_system_prompt()
        return self._runner.getSystemPromptFn()


class _ToolContextView(_ContextBase):
    """The context for executing one tool call: the extension context plus ``tools`` and
    ``executeTool()``. ``signal`` is the default signal of nested calls."""

    def __init__(self, runner: ExtensionRunner, toolCallId: str, signal: Any) -> None:
        super().__init__(runner)
        self._toolCallId = toolCallId
        self._toolSignal = signal

    @property
    def tools(self) -> Any:
        self._runner.assertActive()
        return self._runner.getCallableToolsFn()

    async def executeTool(self, name: str, args: Any, options: Mapping[str, Any] | None = None) -> AgentToolCallOutcome:
        runner = self._runner
        runner.assertActive()
        if runner.executeToolFn is None:
            return AgentToolCallOutcome(
                toolCall=ToolCall(id=f"{self._toolCallId}/0", name=name, arguments={}),
                result=AgentToolResult(
                    content=[TextContent(text="Nested tool calls are not available in this context")], details={}
                ),
                isError=True,
            )
        options = dict(options or {})
        signal = options.get("signal")
        return await runner.executeToolFn(
            self._toolCallId, name, args, {**options, "signal": signal if signal is not None else self._toolSignal}
        )


class _CommandContextView(_ContextBase):
    def getSystemPromptOptions(self) -> BuildSystemPromptOptions:
        self._runner.assertActive()
        return self._runner.getSystemPromptOptionsFn()  # type: ignore[no-any-return]

    async def waitForIdle(self) -> None:
        self._runner.assertActive()
        await self._runner.waitForIdleFn()

    async def newSession(self, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner.assertActive()
        return await self._runner.newSessionHandler(options)

    async def fork(self, entryId: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner.assertActive()
        return await self._runner.forkHandler(entryId, options)

    async def navigateTree(self, targetId: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner.assertActive()
        return await self._runner.navigateTreeHandler(targetId, options)

    async def switchSession(self, sessionPath: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner.assertActive()
        return await self._runner.switchSessionHandler(sessionPath, options)

    async def reload(self) -> None:
        self._runner.assertActive()
        await self._runner.reloadHandler()


def _resolve_action(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _required_action(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source[name]
    return getattr(source, name)


def _event_type(event: Any) -> str:
    if isinstance(event, Mapping):
        return str(event["type"])
    return str(event.type)


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _clone_with(event: Any, **updates: Any) -> Any:
    if isinstance(event, Mapping):
        cloned = dict(event)
        cloned.update(updates)
        return cloned
    cloned = copy(event)
    for key, value in updates.items():
        setattr(cloned, key, value)
    return cloned


def _result_flag(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _invoke_handler(handler: Any, event: Any, ctx: Any) -> Any:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(event, ctx)

    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values()):
        return handler(event, ctx)
    if len(positional) >= 2:
        return handler(event, ctx)
    if len(positional) == 1:
        return handler(event)
    return handler()


def buildBuiltinKeybindings(resolvedKeybindings: Mapping[str, str | list[str] | None]) -> dict[str, dict[str, Any]]:
    builtin: dict[str, dict[str, Any]] = {}
    for keybinding, keys in resolvedKeybindings.items():
        if keys is None:
            continue
        key_list = keys if isinstance(keys, list) else [keys]
        restrict_override = keybinding in RESERVED_KEYBINDINGS_FOR_EXTENSION_CONFLICTS
        for key in key_list:
            normalized_key = key.lower()
            existing = builtin.get(normalized_key)
            if existing and existing["restrictOverride"] and not restrict_override:
                continue
            builtin[normalized_key] = {
                "keybinding": keybinding,
                "restrictOverride": restrict_override,
            }
    return builtin


async def emitProjectTrustEvent(
    extensions_result: LoadExtensionsResult,
    event: ProjectTrustEvent,
    ctx: ProjectTrustContext,
) -> dict[str, Any]:
    errors: list[ExtensionError] = []
    for extension, handlers in snapshotEventHandlers(extensions_result.extensions, "project_trust"):
        for handler in handlers:
            try:
                handler_result = _invoke_handler(handler, event, ctx)
                if hasattr(handler_result, "__await__"):
                    handler_result = await handler_result
                trusted = (
                    handler_result["trusted"]
                    if isinstance(handler_result, Mapping)
                    else handler_result.trusted
                )
                if trusted == "undecided":
                    continue
                return {"result": handler_result, "errors": errors}
            except Exception as error:  # noqa: BLE001 - extension failures are returned to the caller
                errors.append(
                    ExtensionError(
                        extensionPath=extension.path,
                        event=_event_type(event),
                        error=str(error),
                        stack="".join(traceback.format_exception(error)),
                    )
                )
    return {"errors": errors}


@dataclass(slots=True)
class ExtensionRunner:
    extensions: list[Extension]
    runtime: ExtensionRuntime
    cwd: str
    sessionManager: Any
    modelRegistry: Any
    uiContext: Any = field(default_factory=_NoUIContext)
    # Which run mode the host is driving (pi runner.ts:273). Extensions guard TUI-only
    # features on it; "print" is the safe default for hosts that never bind a mode.
    mode: ExtensionMode = "print"
    errorListeners: list[Any] = field(default_factory=list)
    getModel: Any = field(default=lambda: None)
    getScopedModels: Any = field(default=list)
    isIdleFn: Any = field(default=lambda: True)
    isProjectTrustedFn: Any = field(default=lambda: True)
    getSignalFn: Any = field(default=lambda: None)
    waitForIdleFn: Any = field(default=lambda: _completed_future())
    abortFn: Any = field(default=lambda: None)
    hasPendingMessagesFn: Any = field(default=lambda: False)
    getContextUsageFn: Any = field(default=lambda: None)
    compactFn: Any = field(default=lambda _options=None: None)
    getSystemPromptFn: Any = field(default=lambda: "")
    getSystemPromptOptionsFn: Any = field(init=False, repr=False)
    executeToolFn: Any = None
    getCallableToolsFn: Any = field(default=list)
    reportedMcpServers: set[str] = field(default_factory=set)
    newSessionHandler: Any = field(default=lambda _options=None: _result_future({"cancelled": False}))
    forkHandler: Any = field(default=lambda _entry_id, _options=None: _result_future({"cancelled": False}))
    navigateTreeHandler: Any = field(default=lambda _target_id, _options=None: _result_future({"cancelled": False}))
    switchSessionHandler: Any = field(default=lambda _session_path, _options=None: _result_future({"cancelled": False}))
    reloadHandler: Any = field(default=lambda: _completed_future())
    shutdownHandler: Any = field(default=lambda: None)
    shortcutDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    commandDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    staleMessage: str | None = None
    uiPromptDepth: int = field(default=0, init=False, repr=False)
    # MISAKA fork: the session's parts (core.moments), so the runner's own ui_prompt events reach them.
    moments: Any = field(default=None, init=False, repr=False)
    activeUIPrompt: tuple[UIPromptKind, str | None] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.getSystemPromptOptionsFn = self._default_system_prompt_options

    def _default_system_prompt_options(self) -> BuildSystemPromptOptions:
        return normalizeBuildSystemPromptOptions({"cwd": self.cwd})  # type: ignore[return-value]

    def bindCore(
        self,
        actions: Any,
        context_actions: Any,
        provider_actions: Any | None = None,
    ) -> None:
        self.runtime.sendMessage = _required_action(actions, "sendMessage")
        self.runtime.sendUserMessage = _required_action(actions, "sendUserMessage")
        self.runtime.appendEntry = _required_action(actions, "appendEntry")
        self.runtime.setSessionName = _required_action(actions, "setSessionName")
        self.runtime.getSessionName = _required_action(actions, "getSessionName")
        self.runtime.setLabel = _required_action(actions, "setLabel")
        self.runtime.getActiveTools = _required_action(actions, "getActiveTools")
        self.runtime.getAllTools = _required_action(actions, "getAllTools")
        self.runtime.getSettings = _required_action(actions, "getSettings")
        self.runtime.setActiveTools = _required_action(actions, "setActiveTools")
        self.runtime.refreshTools = _required_action(actions, "refreshTools")
        self.runtime.getCommands = _required_action(actions, "getCommands")
        self.runtime.setModel = _required_action(actions, "setModel")
        self.runtime.getThinkingLevel = _required_action(actions, "getThinkingLevel")
        self.runtime.setThinkingLevel = _required_action(actions, "setThinkingLevel")
        self.runtime.createContext = lambda: self.createContext()

        self.getModel = _required_action(context_actions, "getModel")
        self.getScopedModels = _resolve_action(context_actions, "getScopedModels", self.getScopedModels)
        self.isIdleFn = _required_action(context_actions, "isIdle")
        self.isProjectTrustedFn = _required_action(context_actions, "isProjectTrusted")
        self.getSignalFn = _required_action(context_actions, "getSignal")
        self.abortFn = _required_action(context_actions, "abort")
        self.hasPendingMessagesFn = _required_action(context_actions, "hasPendingMessages")
        self.shutdownHandler = _required_action(context_actions, "shutdown")
        self.getContextUsageFn = _required_action(context_actions, "getContextUsage")
        self.compactFn = _required_action(context_actions, "compact")
        self.getSystemPromptFn = _required_action(context_actions, "getSystemPrompt")
        get_system_prompt_options = _resolve_action(context_actions, "getSystemPromptOptions")
        self.getSystemPromptOptionsFn = (
            get_system_prompt_options
            if get_system_prompt_options is not None
            else self._default_system_prompt_options
        )
        self.executeToolFn = _resolve_action(context_actions, "executeTool")
        get_callable_tools = _resolve_action(context_actions, "getCallableTools")
        self.getCallableToolsFn = get_callable_tools if get_callable_tools is not None else list

        # Servers registered from now on reach the extension that connects them right away.
        # Servers registered during loading are read on session_start.
        def on_mcp_servers_change() -> None:
            # Upstream `void this.emit(...)`: not awaited. With no running loop there is nobody to
            # deliver it to yet.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(
                    self.emit({"type": "mcp_servers_change", "servers": self.runtime.mcpServers.list()})
                )
                task.add_done_callback(lambda done: done.cancelled() or done.exception())
            self.reportUnhandledMcpServers()

        self.runtime.mcpServers.setChangeListener(on_mcp_servers_change)

        register_provider = _resolve_action(provider_actions, "registerProvider")
        register_native_provider = _resolve_action(
            provider_actions, "registerNativeProvider"
        )
        unregister_provider = _resolve_action(provider_actions, "unregisterProvider")
        fallback_register = getattr(self.modelRegistry, "registerProvider", None)
        fallback_register_native = getattr(
            self.modelRegistry, "registerNativeProvider", None
        )
        fallback_unregister = getattr(self.modelRegistry, "unregisterProvider", None)

        def dispatch_register(name: str, config: ProviderConfig) -> None:
            if callable(register_provider):
                register_provider(name, config)
                return
            if callable(fallback_register):
                fallback_register(name, config)
                return
            raise RuntimeError("No provider registration handler bound")

        def dispatch_register_native(provider: Any) -> None:
            if callable(register_native_provider):
                register_native_provider(provider)
                return
            if callable(fallback_register_native):
                fallback_register_native(provider)
                return
            if callable(fallback_register):
                fallback_register(provider)
                return
            raise RuntimeError("No native provider registration handler bound")

        for registration in list(self.runtime.pendingProviderRegistrations):
            try:
                dispatch_register(registration.name, registration.config)
            except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                self._emit_extension_exception(registration.extensionPath, "register_provider", error)
        self.runtime.pendingProviderRegistrations.clear()

        for registration in list(self.runtime.pendingNativeProviderRegistrations):
            try:
                dispatch_register_native(registration.provider)
            except Exception as error:  # noqa: BLE001 - extension code: reported through the runner
                self._emit_extension_exception(
                    registration.extensionPath, "register_provider", error
                )
        self.runtime.pendingNativeProviderRegistrations.clear()
        register_virtual_model_action = _resolve_action(provider_actions, "registerVirtualModel")
        unregister_virtual_model_action = _resolve_action(provider_actions, "unregisterVirtualModel")

        def register_virtual_model(definition: VirtualModelDefinition, _extension_path: str | None = None) -> None:
            if callable(register_virtual_model_action):
                register_virtual_model_action(definition)
                return
            self.modelRegistry.registerVirtualModel(definition)

        for virtual in list(self.runtime.pendingVirtualModelRegistrations):
            try:
                register_virtual_model(virtual.definition)
            except Exception as error:  # noqa: BLE001 - extension code: reported through the runner
                self._emit_extension_exception(virtual.extensionPath, "register_virtual_model", error)
        self.runtime.pendingVirtualModelRegistrations.clear()

        def register_provider_now(
            name: str,
            config: ProviderConfig,
            _extension_path: str | None = None,
        ) -> None:
            dispatch_register(name, config)

        def register_native_provider_now(
            provider: Any,
            _extension_path: str | None = None,
        ) -> None:
            dispatch_register_native(provider)

        def unregister_provider_now(name: str, _extension_path: str | None = None) -> None:
            if callable(unregister_provider):
                unregister_provider(name)
                return
            if callable(fallback_unregister):
                fallback_unregister(name)
                return
            raise RuntimeError("No provider unregistration handler bound")

        def unregister_virtual_model_now(provider: str, id: str) -> None:
            if callable(unregister_virtual_model_action):
                unregister_virtual_model_action(provider, id)
                return
            self.modelRegistry.unregisterVirtualModel(provider, id)

        self.runtime.registerProvider = register_provider_now
        self.runtime.registerNativeProvider = register_native_provider_now
        self.runtime.unregisterProvider = unregister_provider_now
        self.runtime.registerVirtualModel = register_virtual_model
        self.runtime.unregisterVirtualModel = unregister_virtual_model_now

    def bindCommandContext(self, actions: Any | None = None) -> None:
        if actions is None:
            self.waitForIdleFn = lambda: _completed_future()
            self.newSessionHandler = lambda _options=None: _result_future({"cancelled": False})
            self.forkHandler = lambda _entry_id, _options=None: _result_future({"cancelled": False})
            self.navigateTreeHandler = lambda _target_id, _options=None: _result_future({"cancelled": False})
            self.switchSessionHandler = lambda _session_path, _options=None: _result_future({"cancelled": False})
            self.reloadHandler = lambda: _completed_future()
            return

        self.waitForIdleFn = _required_action(actions, "waitForIdle")
        self.newSessionHandler = _required_action(actions, "newSession")
        self.forkHandler = _required_action(actions, "fork")
        self.navigateTreeHandler = _required_action(actions, "navigateTree")
        self.switchSessionHandler = _required_action(actions, "switchSession")
        self.reloadHandler = _required_action(actions, "reload")

    def setUIContext(self, uiContext: Any | None = None, mode: ExtensionMode = "print") -> None:
        self.uiContext = (
            _UIPromptUIContext(self, uiContext) if uiContext is not None else _NoUIContext()
        )
        self.mode = mode

    async def _with_ui_prompt(
        self,
        kind: UIPromptKind,
        title: str | None,
        method: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.uiPromptDepth == 0:
            self.activeUIPrompt = (kind, title or None)
            self._queue_ui_prompt_event("ui_prompt_start")
        self.uiPromptDepth += 1
        try:
            return await method(*args, **kwargs)
        finally:
            self.uiPromptDepth = max(0, self.uiPromptDepth - 1)
            if self.uiPromptDepth == 0 and self.activeUIPrompt is not None:
                self._queue_ui_prompt_event("ui_prompt_end")
                self.activeUIPrompt = None

    def _queue_ui_prompt_event(
        self, event_type: Literal["ui_prompt_start", "ui_prompt_end"]
    ) -> None:
        active = self.activeUIPrompt
        if active is None:
            return
        kind, title = active
        event: dict[str, Any] = {
            "type": event_type,
            "reason": "ui_prompt",
            "kind": kind,
        }
        if title:
            event["title"] = title
        moments = self.moments  # MISAKA fork: the session's parts hear it first

        async def dispatch() -> None:
            if moments is not None:
                await getattr(moments, event_type)(dict(event))
            await self.emit(event)

        task = asyncio.create_task(dispatch())
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    def getUIContext(self) -> Any:
        return self.uiContext

    def hasUI(self) -> bool:
        return not isinstance(self.uiContext, _NoUIContext)

    def getExtensionPaths(self) -> list[str]:
        return [extension.path for extension in self.extensions]

    def getActiveTools(self) -> list[str]:
        """Names of the tools the agent currently exposes (pi runner.ts:714)."""
        self.assertActive()
        return self.runtime.getActiveTools()

    def getAllRegisteredTools(self) -> list[RegisteredTool]:
        tools_by_name: dict[str, RegisteredTool] = {}
        for extension in self.extensions:
            for tool in extension.tools.values():
                if tool.definition.name not in tools_by_name:
                    tools_by_name[tool.definition.name] = tool
        return list(tools_by_name.values())

    def getToolDefinition(self, toolName: str) -> Any:
        for extension in self.extensions:
            tool = extension.tools.get(toolName)
            if tool is not None:
                return tool.definition
        return None

    def getFlags(self) -> dict[str, ExtensionFlag]:
        flags: dict[str, ExtensionFlag] = {}
        for extension in self.extensions:
            for name, flag in extension.flags.items():
                if name not in flags:
                    flags[name] = flag
        return flags

    def setFlagValue(self, name: str, value: bool | str) -> None:
        self.runtime.flagValues[name] = value

    def getFlagValues(self) -> dict[str, bool | str]:
        return dict(self.runtime.flagValues)

    def getShortcuts(self, resolvedKeybindings: Mapping[str, str | list[str] | None]) -> dict[str, ExtensionShortcut]:
        self.shortcutDiagnostics = []
        builtin_keybindings = buildBuiltinKeybindings(resolvedKeybindings)
        extension_shortcuts: dict[str, ExtensionShortcut] = {}

        def add_diagnostic(message: str, extensionPath: str) -> None:
            self.shortcutDiagnostics.append(ResourceDiagnostic(type="warning", message=message, path=extensionPath))
            if not self.hasUI():
                print(message, file=sys.stderr)

        for extension in self.extensions:
            for key, shortcut in extension.shortcuts.items():
                normalized_key = key.lower()
                built_in = builtin_keybindings.get(normalized_key)
                if built_in and built_in["restrictOverride"] is True:
                    add_diagnostic(
                        (
                            f"Extension shortcut '{key}' from {shortcut.extensionPath} "
                            "conflicts with built-in shortcut. Skipping."
                        ),
                        shortcut.extensionPath,
                    )
                    continue
                if built_in and built_in["restrictOverride"] is False:
                    add_diagnostic(
                        (
                            f"Extension shortcut conflict: '{key}' is built-in shortcut for "
                            f"{built_in['keybinding']} and {shortcut.extensionPath}. "
                            f"Using {shortcut.extensionPath}."
                        ),
                        shortcut.extensionPath,
                    )
                existing = extension_shortcuts.get(normalized_key)
                if existing is not None:
                    add_diagnostic(
                        (
                            f"Extension shortcut conflict: '{key}' registered by both "
                            f"{existing.extensionPath} and {shortcut.extensionPath}. "
                            f"Using {shortcut.extensionPath}."
                        ),
                        shortcut.extensionPath,
                    )
                extension_shortcuts[normalized_key] = shortcut
        return extension_shortcuts

    def getShortcutDiagnostics(self) -> list[ResourceDiagnostic]:
        return self.shortcutDiagnostics

    def invalidate(self, message: str | None = None) -> None:
        if self.staleMessage is not None:
            return
        self.staleMessage = (
            message
            or (
                "This extension ctx is stale after session replacement or reload. "
                "Do not use a captured pi or command ctx after ctx.newSession(), ctx.fork(), "
                "ctx.switchSession(), or ctx.reload(). For newSession, fork, and switchSession, "
                "move post-replacement work into withSession and use the ctx passed to withSession. "
                "For reload, do not use the old ctx after await ctx.reload()."
            )
        )
        self.runtime.invalidate(self.staleMessage)

    def assertActive(self) -> None:
        if self.staleMessage:
            raise RuntimeError(self.staleMessage)

    def onError(self, listener: ExtensionErrorListener) -> Any:
        if listener not in self.errorListeners:
            self.errorListeners.append(listener)

        def unsubscribe() -> None:
            if listener in self.errorListeners:
                self.errorListeners.remove(listener)

        return unsubscribe

    def emitError(self, error: ExtensionError) -> None:
        for listener in list(self.errorListeners):
            listener(error)

    def _emit_extension_exception(self, extension_path: str, event: str, error: Exception) -> None:
        self.emitError(
            ExtensionError(
                extensionPath=extension_path,
                event=event,
                error=str(error),
                stack="".join(traceback.format_exception(error)),
            )
        )

    def reportUnhandledMcpServers(self) -> None:
        """Report registered MCP servers when no extension handles ``mcp_servers_change``, which
        means nothing connects them (for example when another MCP extension replaced the built-in
        one)."""
        if self.hasHandlers("mcp_servers_change"):
            return
        for server in self.runtime.mcpServers.list():
            if server.name in self.reportedMcpServers:
                continue
            self.reportedMcpServers.add(server.name)
            self.emitError(
                ExtensionError(
                    extensionPath=server.extensionPath,
                    event="register_mcp_server",
                    error=(
                        f'MCP server "{server.name}" is registered, but no loaded extension connects MCP '
                        "servers; another extension may have replaced the built-in MCP support"
                    ),
                )
            )

    def hasHandlers(self, eventType: str) -> bool:
        return any(extension.handlers.get(eventType) for extension in self.extensions)

    def getMessageRenderer(self, customType: str) -> Any:
        for extension in self.extensions:
            renderer = extension.messageRenderers.get(customType)
            if renderer is not None:
                return renderer
        return None

    def getMarkdownTransformers(self) -> list[MarkdownTransformer]:
        return [
            transformer
            for extension in self.extensions
            if (transformer := extension.markdownTransformer) is not None
        ]

    def resolveToolRenderers(
        self, toolName: str, base: Callable[[], ToolRenderers | ToolDefinition[Any, Any] | None]
    ) -> ToolRenderers | ToolDefinition[Any, Any] | None:
        """Renderers of calls to `toolName`: extension resolvers in load order, then `base`."""
        resolvers = [resolver for extension in self.extensions for resolver in extension.toolRenderers or []]

        def resolve(index: int) -> ToolRenderers | ToolDefinition[Any, Any] | None:
            return resolvers[index](toolName, lambda: resolve(index + 1)) if index < len(resolvers) else base()

        return resolve(0)

    def getEntryRenderer(self, customType: str) -> EntryRenderer[Any] | None:
        for extension in self.extensions:
            renderer = extension.entryRenderers.get(customType)
            if renderer is not None:
                return renderer
        return None

    def resolveRegisteredCommands(self) -> list[ResolvedCommand]:
        commands: list[RegisteredCommand] = []
        counts: dict[str, int] = {}
        for extension in self.extensions:
            for command in extension.commands.values():
                commands.append(command)
                counts[command.name] = counts.get(command.name, 0) + 1

        seen: dict[str, int] = {}
        taken: set[str] = set()
        resolved: list[ResolvedCommand] = []
        for command in commands:
            occurrence = seen.get(command.name, 0) + 1
            seen[command.name] = occurrence
            invocation_name = f"{command.name}:{occurrence}" if counts.get(command.name, 0) > 1 else command.name
            suffix = occurrence
            while invocation_name in taken:
                suffix += 1
                invocation_name = f"{command.name}:{suffix}"
            taken.add(invocation_name)
            resolved.append(
                ResolvedCommand(
                    name=command.name,
                    sourceInfo=command.sourceInfo,
                    description=command.description,
                    getArgumentCompletions=command.getArgumentCompletions,
                    handler=command.handler,
                    invocationName=invocation_name,
                )
            )
        return resolved

    def getRegisteredCommands(self) -> list[ResolvedCommand]:
        self.commandDiagnostics = []
        return self.resolveRegisteredCommands()

    def getCommandDiagnostics(self) -> list[ResourceDiagnostic]:
        return self.commandDiagnostics

    def getCommand(self, name: str) -> ResolvedCommand | None:
        for command in self.resolveRegisteredCommands():
            if command.invocationName == name:
                return command
        return None

    def shutdown(self) -> None:
        self.shutdownHandler()

    def createContext(self) -> Any:
        return _ContextBase(self)

    def createToolContext(self, toolCallId: str, signal: Any) -> Any:
        """Create the context for executing the tool call ``toolCallId``: the extension context
        plus ``tools`` and ``executeTool()``. ``signal`` is the default signal of nested calls."""
        return _ToolContextView(self, toolCallId, signal)

    def createCommandContext(self) -> Any:
        return _CommandContextView(self)

    async def emit(self, event: Any) -> Any:
        event_type = _event_type(event)
        ctx = self.createContext()
        result: Any = None
        for extension, handlers in snapshotEventHandlers(self.extensions, event_type):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if event_type in {"session_context_prepare", "session_context_carry"} and handler_result is not None:
                        return handler_result
                    if event_type in {
                        "session_before_switch",
                        "session_before_fork",
                        "session_before_compact",
                        "session_before_tree",
                    } and handler_result:
                        result = handler_result
                        if _result_flag(result, "cancel", False):
                            return result
                except Exception as error:
                    self._emit_extension_exception(extension.path, event_type, error)
                    if event_type in {"session_context_prepare", "session_context_carry"}:
                        raise
        return result

    async def emitMessageEnd(self, event: Any) -> Any:
        ctx = self.createContext()
        current_message = _event_field(event, "message")
        modified = False
        for extension, handlers in snapshotEventHandlers(self.extensions, "message_end"):
            for handler in handlers:
                try:
                    current_event = _clone_with(event, message=current_message)
                    handler_result = _invoke_handler(handler, current_event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    message = _result_flag(handler_result, "message")
                    if message is None:
                        continue
                    if _event_field(message, "role") != _event_field(current_message, "role"):
                        self.emitError(
                            ExtensionError(
                                extensionPath=extension.path,
                                event="message_end",
                                error="message_end handlers must return a message with the same role",
                            )
                        )
                        continue
                    current_message = message
                    modified = True
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "message_end", error)
        return current_message if modified else None

    async def emitToolResult(self, event: Any) -> Any:
        ctx = self.createContext()
        current_event = _clone_with(event)
        modified = False
        for extension, handlers in snapshotEventHandlers(self.extensions, "tool_result"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, current_event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if handler_result is None:
                        continue
                    for field_name in ("content", "details", "structuredContent", "isError", "usage"):
                        value = _result_flag(handler_result, field_name, _MISSING)
                        if value is not _MISSING:
                            if isinstance(current_event, Mapping):
                                current_event[field_name] = value
                            else:
                                setattr(current_event, field_name, value)
                            modified = True
                            if (
                                field_name == "content"
                                and _result_flag(handler_result, "structuredContent", _MISSING) is _MISSING
                            ):
                                # Structured content that is not replaced along with the content may
                                # no longer match it.
                                if isinstance(current_event, Mapping):
                                    current_event.pop("structuredContent", None)
                                else:
                                    current_event.structuredContent = None
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "tool_result", error)
        if not modified:
            return None
        return {
            "content": _event_field(current_event, "content"),
            "details": _event_field(current_event, "details"),
            "structuredContent": _event_field(current_event, "structuredContent"),
            "isError": _event_field(current_event, "isError"),
            "usage": _event_field(current_event, "usage"),
        }

    async def emitToolCall(self, event: Any) -> Any:
        ctx = self.createContext()
        result: Any = None
        for extension, handlers in snapshotEventHandlers(self.extensions, "tool_call"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                except Exception as error:
                    # Unlike the other emitters this one must stay fail-closed: a permission
                    # hook that crashes must not fall through to "allowed". Report through
                    # the extension-error channel, then re-raise so the agent loop turns it
                    # into an error tool result.
                    self._emit_extension_exception(extension.path, "tool_call", error)
                    raise
                if handler_result:
                    result = handler_result
                    if _result_flag(result, "block", False):
                        return result
        return result

    async def emitUserBash(self, event: Any) -> Any:
        """`user_bash` fails closed: an error or an invalid defined result aborts the command
        without invoking later handlers or executing locally (pi #9068)."""
        ctx = self.createContext()
        for extension, handlers in snapshotEventHandlers(self.extensions, "user_bash"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if handler_result is None:
                        continue
                    if not isUserBashEventResult(handler_result):
                        raise ValueError(
                            "Invalid user_bash handler result: return undefined for local execution "
                            "or exactly one valid { operations } or { result } object"
                        )
                    return handler_result
                except Exception as error:
                    self._emit_extension_exception(extension.path, "user_bash", error)
                    raise
        return None

    async def emitContext(self, messages: list[Any]) -> list[Any]:
        """Run the request-time transforms in two phases. `context` handlers see the conversation
        only and Pi restores the prompt and tool state after each; `context_with_system`
        handlers then see the full transcript and their output is used as returned."""
        ctx = self.createContext()
        current_messages = deepcopy(messages)
        for extension, handlers in snapshotEventHandlers(self.extensions, "context"):
            for handler in handlers:
                try:
                    visible_messages = [m for m in current_messages if _event_field(m, "role") != "system"]
                    visible_snapshot = list(visible_messages)
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "context", "messages": visible_messages},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    # Handlers may return a new list or edit event.messages in place.
                    returned = _result_flag(handler_result, "messages")
                    if returned is None:
                        returned = None if sameMessages(visible_messages, visible_snapshot) else visible_messages
                    if returned is None:
                        continue
                    current_messages = restoreSystemMessages(current_messages, visible_snapshot, returned)
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "context", error)
        for extension, handlers in snapshotEventHandlers(self.extensions, "context_with_system"):
            for handler in handlers:
                try:
                    had_leading_system_message = bool(current_messages) and _event_field(current_messages[0], "role") == "system"
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "context_with_system", "messages": current_messages},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    returned = _result_flag(handler_result, "messages")
                    current_messages = returned if returned is not None else current_messages
                    # Providers read the prompt and initial tools from the leading system message.
                    # Losing it is never intended; report it but honor the handler's output.
                    if had_leading_system_message and (
                        not current_messages or _event_field(current_messages[0], "role") != "system"
                    ):
                        self.emitError(
                            ExtensionError(
                                extensionPath=extension.path,
                                event="context_with_system",
                                error=(
                                    "Handler removed the leading system message; the request has no prompt or "
                                    "initial tool declarations. Keep it at index 0 or replace a dropped prefix "
                                    "with getCurrentSystemMessage()."
                                ),
                            )
                        )
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "context_with_system", error)
        return current_messages

    async def emitBeforeProviderRequest(self, payload: Any) -> Any:
        ctx = self.createContext()
        current_payload = payload
        for extension, handlers in snapshotEventHandlers(self.extensions, "before_provider_request"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "before_provider_request", "payload": current_payload},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if handler_result is not None:
                        current_payload = handler_result
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "before_provider_request", error)
        return current_payload

    async def emitBeforeProviderHeaders(self, headers: dict[str, str | None]) -> dict[str, str | None]:
        """Let extensions mutate the assembled request headers in place (pi runner.ts:1100-1127).

        The handler return value is ignored; a ``None`` value deletes that header.
        """
        ctx = self.createContext()
        for extension, handlers in snapshotEventHandlers(self.extensions, "before_provider_headers"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "before_provider_headers", "headers": headers},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        await handler_result
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "before_provider_headers", error)
        return headers

    async def emitBeforeAgentStart(
        self,
        trigger: BeforeAgentStartTrigger,
        systemPromptOptions: Any,
    ) -> dict[str, Any]:
        """Handlers see and may edit the mutable prompt options; a returned `systemPrompt`
        becomes `forceSystemPrompt`. Always returns `{messages, systemPromptOptions}`, or
        `{block, reason}` when a MISAKA hook blocked the turn (# MISAKA fork: pi has no block)."""
        current_options = normalizeBuildSystemPromptOptions(systemPromptOptions)

        def render_current_system_prompt() -> str:
            return buildSystemPrompt(current_options)

        ctx = _ContextBase(self, {"getSystemPrompt": render_current_system_prompt})
        messages: list[Any] = []
        for extension, handlers in snapshotEventHandlers(self.extensions, "before_agent_start"):
            for handler in handlers:
                try:
                    event = _BeforeAgentStartEvent(
                        trigger=trigger,
                        systemPromptOptions=current_options,
                        render=render_current_system_prompt,
                    )
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if _result_flag(handler_result, "block", False):
                        return {
                            "block": True,
                            "reason": _result_flag(
                                handler_result,
                                "reason",
                                "before_agent_start hook blocked the turn",
                            ),
                        }
                    message = _result_flag(handler_result, "message")
                    if message is not None:
                        messages.append(message)
                    if _result_flag(handler_result, "systemPrompt") is not None:
                        current_options["forceSystemPrompt"] = _result_flag(handler_result, "systemPrompt")
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "before_agent_start", error)
        return {"messages": messages, "systemPromptOptions": current_options}

    async def emitBoundary(self, base_event: Mapping[str, Any], build_context: Any) -> dict[str, Any]:
        """Dispatch an actionable boundary (`turn_end`, `agent_before_settle`): each handler sees
        the entries and continuation decision so far plus a context built from them, and may
        return replacements for either."""
        ctx = self.createContext()
        entries: list[Any] = []
        should_continue = False
        context = await maybe_await(build_context(entries))
        valid = True
        event_type = str(base_event["type"])
        for extension, handlers in snapshotEventHandlers(self.extensions, event_type):
            for handler in handlers:
                event = {**base_event, "entries": entries, "continue": should_continue, "context": context}
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    next_entries = _result_flag(handler_result, "entries", _MISSING)
                    if next_entries is not _MISSING and next_entries is not None:
                        entries = list(next_entries)
                    next_continue = _result_flag(handler_result, "continue", _MISSING)
                    if next_continue is not _MISSING and next_continue is not None:
                        should_continue = bool(next_continue)
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, event_type, error)
                try:
                    context = await maybe_await(build_context(entries))
                    valid = True
                except Exception as error:  # noqa: BLE001 - the drafts, not the runner, are what failed
                    valid = False
                    self.emitError(
                        ExtensionError(
                            extensionPath=extension.path,
                            event=event_type,
                            error=f"Invalid boundary entries: {error}",
                            stack=traceback.format_exc(),
                        )
                    )
        if valid:
            return {"entries": entries, "continue": should_continue, "context": context, "valid": True}
        return {"entries": [], "continue": False, "context": context, "valid": False}

    async def emitCacheWarmingDecision(self, event: Mapping[str, Any]) -> Any:
        """Returns the event's own action unless a handler overrides it; the last override wins."""
        ctx = self.createContext()
        action = event.get("action")
        for extension, handlers in snapshotEventHandlers(self.extensions, str(event["type"])):
            for handler in handlers:
                try:
                    result = _invoke_handler(handler, event, ctx)
                    if hasattr(result, "__await__"):
                        result = await result
                    next_action = _result_flag(result, "action", None)
                    if next_action is not None:
                        action = next_action
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, str(event["type"]), error)
        return action

    async def emitAgentEnd(self, event: Any) -> dict[str, Any] | None:
        """Emit ``agent_end`` while preserving a stop-hook decision.

        Most lifecycle events are observational, but Claude-compatible Stop
        hooks may reject completion and return feedback for another model
        turn.  The generic emitter intentionally discards ordinary results,
        so this event needs an explicit decision channel.
        """

        ctx = self.createContext()
        for extension, handlers in snapshotEventHandlers(self.extensions, "agent_end"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if _result_flag(handler_result, "block", False):
                        return {
                            "block": True,
                            "reason": _result_flag(
                                handler_result,
                                "reason",
                                "agent_end hook blocked completion",
                            ),
                        }
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "agent_end", error)
        return None

    async def emitResourcesDiscover(self, cwd: str, reason: str) -> dict[str, list[dict[str, str]]]:
        ctx = self.createContext()
        prompt_paths: list[dict[str, str]] = []
        theme_paths: list[dict[str, str]] = []
        skill_paths: list[dict[str, str]] = []
        for extension, handlers in snapshotEventHandlers(self.extensions, "resources_discover"):
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "resources_discover", "cwd": cwd, "reason": reason},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    for field_name, target in (
                        ("promptPaths", prompt_paths),
                        ("themePaths", theme_paths),
                        ("skillPaths", skill_paths),
                    ):
                        for path in _result_flag(handler_result, field_name, []) or []:
                            target.append({"path": path, "extensionPath": extension.path})
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "resources_discover", error)
        return {
            "promptPaths": prompt_paths,
            "themePaths": theme_paths,
            "skillPaths": skill_paths,
        }

    async def emitInput(
        self,
        text: str,
        images: list[ImageContent] | None,
        source: InputSource,
        streamingBehavior: Literal["steer", "followUp"] | None = None,
    ) -> dict[str, Any]:
        ctx = self.createContext()
        current_text = text
        current_images = images
        for extension, handlers in snapshotEventHandlers(self.extensions, "input"):
            for handler in handlers:
                try:
                    event = {
                        "type": "input",
                        "text": current_text,
                        "images": current_images,
                        "source": source,
                        "streamingBehavior": streamingBehavior,
                    }
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    action = _result_flag(handler_result, "action")
                    if action == "handled":
                        return handler_result
                    if action == "transform":
                        current_text = _result_flag(handler_result, "text", current_text)
                        images_result = _result_flag(handler_result, "images", _MISSING)
                        if images_result is not _MISSING and images_result is not None:
                            current_images = images_result
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "input", error)
        if current_text != text or current_images != images:
            return {"action": "transform", "text": current_text, "images": current_images}
        return {"action": "continue"}



async def emitSessionShutdownEvent(extensionRunner: ExtensionRunner, event: Any) -> bool:
    if extensionRunner.hasHandlers("session_shutdown"):
        await extensionRunner.emit(event)
        return True
    return False


def _completed_future() -> Any:
    async def _done() -> None:
        return None

    return _done()


def _result_future(value: Any) -> Any:
    async def _done() -> Any:
        return value

    return _done()


__all__ = [
    "BeforeAgentStartTrigger",
    "ExtensionErrorListener",
    "ExtensionRunner",
    "ForkHandler",
    "NavigateTreeHandler",
    "NewSessionHandler",
    "ReloadHandler",
    "ShutdownHandler",
    "SwitchSessionHandler",
    "emitProjectTrustEvent",
]
