"""Extension runner - executes extensions and manages their lifecycle."""

from __future__ import annotations

import inspect
import sys
import traceback
from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import Any

from misaka.ai.types import ImageContent, Model
from misaka.core.diagnostics import ResourceDiagnostic
from misaka.core.extensions.types import (
    Extension,
    ExtensionError,
    ExtensionFlag,
    ExtensionRuntime,
    ExtensionShortcut,
    ProviderConfig,
    RegisteredCommand,
    RegisteredTool,
    ResolvedCommand,
)
from misaka.ui.tui.interactive.theme.theme import theme

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

    def getTheme(self) -> Any:
        return None

    def setTheme(self, _theme: str | Any) -> dict[str, Any]:
        return {"success": False, "error": "UI not available"}

    def getToolsExpanded(self) -> bool:
        return False

    def setToolsExpanded(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _ContextBase:
    def __init__(self, runner: ExtensionRunner, extras: Mapping[str, Any] | None = None) -> None:
        self._runner = runner
        self._extras = dict(extras or {})

    def _extra(self, name: str, default: Any) -> Any:
        return self._extras.get(name, default)

    @property
    def ui(self) -> Any:
        self._runner._assert_active()
        return self._extra("ui", self._runner.uiContext)

    @property
    def hasUI(self) -> bool:
        self._runner._assert_active()
        return self._extra("hasUI", self._runner.has_ui())

    @property
    def cwd(self) -> str:
        self._runner._assert_active()
        return self._extra("cwd", self._runner.cwd)

    @property
    def sessionManager(self) -> Any:
        self._runner._assert_active()
        return self._extra("sessionManager", self._runner.sessionManager)

    @property
    def modelRegistry(self) -> Any:
        self._runner._assert_active()
        return self._extra("modelRegistry", self._runner.modelRegistry)

    @property
    def model(self) -> Model[Any] | None:
        self._runner._assert_active()
        return self._extra("model", self._runner.getModel())

    @property
    def signal(self) -> Any | None:
        self._runner._assert_active()
        return self._extra("signal", self._runner.getSignalFn())

    def isIdle(self) -> bool:
        self._runner._assert_active()
        return self._extra("isIdle", self._runner.isIdleFn())  # type: ignore[no-any-return]

    def abort(self) -> None:
        self._runner._assert_active()
        abort = self._extras.get("abort")
        if callable(abort):
            abort()
            return
        self._runner.abortFn()

    def hasPendingMessages(self) -> bool:
        self._runner._assert_active()
        return self._extra("hasPendingMessages", self._runner.hasPendingMessagesFn())  # type: ignore[no-any-return]

    def shutdown(self) -> None:
        self._runner._assert_active()
        shutdown = self._extras.get("shutdown")
        if callable(shutdown):
            shutdown()
            return
        self._runner.shutdownHandler()

    def getContextUsage(self) -> Any:
        self._runner._assert_active()
        get_context_usage = self._extras.get("getContextUsage")
        if callable(get_context_usage):
            return get_context_usage()
        return self._runner.getContextUsageFn()

    def compact(self, options: dict[str, Any] | None = None) -> None:
        self._runner._assert_active()
        compact = self._extras.get("compact")
        if callable(compact):
            compact(options)
            return
        self._runner.compactFn(options)

    def getSystemPrompt(self) -> str:
        self._runner._assert_active()
        get_system_prompt = self._extras.get("getSystemPrompt")
        if callable(get_system_prompt):
            return get_system_prompt()
        return self._runner.getSystemPromptFn()


class _CommandContextView(_ContextBase):
    async def waitForIdle(self) -> None:
        self._runner._assert_active()
        await self._runner.waitForIdleFn()

    async def newSession(self, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner._assert_active()
        return await self._runner.newSessionHandler(options)

    async def fork(self, entryId: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner._assert_active()
        return await self._runner.forkHandler(entryId, options)

    async def navigateTree(self, targetId: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner._assert_active()
        return await self._runner.navigateTreeHandler(targetId, options)

    async def switchSession(self, sessionPath: str, options: dict[str, Any] | None = None) -> dict[str, bool]:
        self._runner._assert_active()
        return await self._runner.switchSessionHandler(sessionPath, options)

    async def reload(self) -> None:
        self._runner._assert_active()
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


def _build_builtin_keybindings(resolvedKeybindings: Mapping[str, str | list[str] | None]) -> dict[str, dict[str, Any]]:
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


@dataclass(slots=True)
class ExtensionRunner:
    extensions: list[Extension]
    runtime: ExtensionRuntime
    cwd: str
    sessionManager: Any
    modelRegistry: Any
    uiContext: Any = field(default_factory=_NoUIContext)
    errorListeners: list[Any] = field(default_factory=list)
    getModel: Any = field(default=lambda: None)
    isIdleFn: Any = field(default=lambda: True)
    getSignalFn: Any = field(default=lambda: None)
    waitForIdleFn: Any = field(default=lambda: _completed_future())
    abortFn: Any = field(default=lambda: None)
    hasPendingMessagesFn: Any = field(default=lambda: False)
    getContextUsageFn: Any = field(default=lambda: None)
    compactFn: Any = field(default=lambda _options=None: None)
    getSystemPromptFn: Any = field(default=lambda: "")
    newSessionHandler: Any = field(default=lambda _options=None: _result_future({"cancelled": False}))
    forkHandler: Any = field(default=lambda _entry_id, _options=None: _result_future({"cancelled": False}))
    navigateTreeHandler: Any = field(default=lambda _target_id, _options=None: _result_future({"cancelled": False}))
    switchSessionHandler: Any = field(default=lambda _session_path, _options=None: _result_future({"cancelled": False}))
    reloadHandler: Any = field(default=lambda: _completed_future())
    shutdownHandler: Any = field(default=lambda: None)
    shortcutDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    commandDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    staleMessage: str | None = None

    def bind_core(
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
        self.runtime.setActiveTools = _required_action(actions, "setActiveTools")
        self.runtime.refreshTools = _required_action(actions, "refreshTools")
        self.runtime.getCommands = _required_action(actions, "getCommands")
        self.runtime.setModel = _required_action(actions, "setModel")
        self.runtime.getThinkingLevel = _required_action(actions, "getThinkingLevel")
        self.runtime.setThinkingLevel = _required_action(actions, "setThinkingLevel")

        self.getModel = _required_action(context_actions, "getModel")
        self.isIdleFn = _required_action(context_actions, "isIdle")
        self.getSignalFn = _required_action(context_actions, "getSignal")
        self.abortFn = _required_action(context_actions, "abort")
        self.hasPendingMessagesFn = _required_action(context_actions, "hasPendingMessages")
        self.shutdownHandler = _required_action(context_actions, "shutdown")
        self.getContextUsageFn = _required_action(context_actions, "getContextUsage")
        self.compactFn = _required_action(context_actions, "compact")
        self.getSystemPromptFn = _required_action(context_actions, "getSystemPrompt")

        register_provider = _resolve_action(provider_actions, "registerProvider")
        unregister_provider = _resolve_action(provider_actions, "unregisterProvider")
        fallback_register = getattr(self.modelRegistry, "registerProvider", None)
        fallback_unregister = getattr(self.modelRegistry, "unregisterProvider", None)

        for registration in list(self.runtime.pendingProviderRegistrations):
            try:
                if callable(register_provider):
                    register_provider(registration.name, registration.config)
                elif callable(fallback_register):
                    fallback_register(registration.name, registration.config)
                else:
                    raise RuntimeError("No provider registration handler bound")  # noqa: TRY004 - callers treat bad input as ValueError
            except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                self._emit_extension_exception(registration.extensionPath, "register_provider", error)
        self.runtime.pendingProviderRegistrations.clear()

        def register_provider_now(name: str, config: ProviderConfig, _extension_path: str | None = None) -> None:
            if callable(register_provider):
                register_provider(name, config)
                return
            if callable(fallback_register):
                fallback_register(name, config)
                return
            raise RuntimeError("No provider registration handler bound")

        def unregister_provider_now(name: str, _extension_path: str | None = None) -> None:
            if callable(unregister_provider):
                unregister_provider(name)
                return
            if callable(fallback_unregister):
                fallback_unregister(name)
                return
            raise RuntimeError("No provider unregistration handler bound")

        self.runtime.registerProvider = register_provider_now
        self.runtime.unregisterProvider = unregister_provider_now

    def bind_command_context(self, actions: Any | None = None) -> None:
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

    def set_ui_context(self, uiContext: Any | None = None) -> None:
        self.uiContext = uiContext if uiContext is not None else _NoUIContext()

    def get_ui_context(self) -> Any:
        return self.uiContext

    def has_ui(self) -> bool:
        return not isinstance(self.uiContext, _NoUIContext)

    def get_extension_paths(self) -> list[str]:
        return [extension.path for extension in self.extensions]

    def get_all_registered_tools(self) -> list[RegisteredTool]:
        tools_by_name: dict[str, RegisteredTool] = {}
        for extension in self.extensions:
            for tool in extension.tools.values():
                if tool.definition.name not in tools_by_name:
                    tools_by_name[tool.definition.name] = tool
        return list(tools_by_name.values())

    def get_tool_definition(self, toolName: str) -> Any:
        for extension in self.extensions:
            tool = extension.tools.get(toolName)
            if tool is not None:
                return tool.definition
        return None

    def get_flags(self) -> dict[str, ExtensionFlag]:
        flags: dict[str, ExtensionFlag] = {}
        for extension in self.extensions:
            for name, flag in extension.flags.items():
                if name not in flags:
                    flags[name] = flag
        return flags

    def set_flag_value(self, name: str, value: bool | str) -> None:
        self.runtime.flagValues[name] = value

    def get_flag_values(self) -> dict[str, bool | str]:
        return dict(self.runtime.flagValues)

    def get_shortcuts(self, resolvedKeybindings: Mapping[str, str | list[str] | None]) -> dict[str, ExtensionShortcut]:
        self.shortcutDiagnostics = []
        builtin_keybindings = _build_builtin_keybindings(resolvedKeybindings)
        extension_shortcuts: dict[str, ExtensionShortcut] = {}

        def add_diagnostic(message: str, extensionPath: str) -> None:
            self.shortcutDiagnostics.append(ResourceDiagnostic(type="warning", message=message, path=extensionPath))
            if not self.has_ui():
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

    def get_shortcut_diagnostics(self) -> list[ResourceDiagnostic]:
        return self.shortcutDiagnostics

    def invalidate(self, message: str | None = None) -> None:
        if self.staleMessage is not None:
            return
        self.staleMessage = (
            message
            or (
                "This extension ctx is stale after session replacement or reload. "
                "Do not use a captured harn or command ctx after ctx.newSession(), ctx.fork(), "
                "ctx.switchSession(), or ctx.reload(). For newSession, fork, and switchSession, "
                "move post-replacement work into withSession and use the ctx passed to withSession. "
                "For reload, do not use the old ctx after await ctx.reload()."
            )
        )
        self.runtime.invalidate(self.staleMessage)

    def _assert_active(self) -> None:
        if self.staleMessage:
            raise RuntimeError(self.staleMessage)

    def on_error(self, listener: ExtensionErrorListener) -> Any:
        if listener not in self.errorListeners:
            self.errorListeners.append(listener)

        def unsubscribe() -> None:
            if listener in self.errorListeners:
                self.errorListeners.remove(listener)

        return unsubscribe

    def emit_error(self, error: ExtensionError) -> None:
        for listener in list(self.errorListeners):
            listener(error)

    def _emit_extension_exception(self, extension_path: str, event: str, error: Exception) -> None:
        self.emit_error(
            ExtensionError(
                extensionPath=extension_path,
                event=event,
                error=str(error),
                stack="".join(traceback.format_exception(error)),
            )
        )

    def has_handlers(self, eventType: str) -> bool:
        return any(extension.handlers.get(eventType) for extension in self.extensions)

    def get_message_renderer(self, customType: str) -> Any:
        for extension in self.extensions:
            renderer = extension.messageRenderers.get(customType)
            if renderer is not None:
                return renderer
        return None

    def _resolve_registered_commands(self) -> list[ResolvedCommand]:
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

    def get_registered_commands(self) -> list[ResolvedCommand]:
        self.commandDiagnostics = []
        return self._resolve_registered_commands()

    def get_command_diagnostics(self) -> list[ResourceDiagnostic]:
        return self.commandDiagnostics

    def get_command(self, name: str) -> ResolvedCommand | None:
        for command in self._resolve_registered_commands():
            if command.invocationName == name:
                return command
        return None

    def shutdown(self) -> None:
        self.shutdownHandler()

    def create_context(self) -> Any:
        return _ContextBase(self)

    def create_command_context(self) -> Any:
        return _CommandContextView(self)

    async def emit(self, event: Any) -> Any:
        event_type = _event_type(event)
        ctx = self.create_context()
        result: Any = None
        for extension in self.extensions:
            handlers = extension.handlers.get(event_type, [])
            for handler in handlers:
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if event_type in {
                        "session_before_switch",
                        "session_before_fork",
                        "session_before_compact",
                        "session_before_tree",
                    } and handler_result:
                        result = handler_result
                        if _result_flag(result, "cancel", False):
                            return result
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, event_type, error)
        return result

    async def emit_message_end(self, event: Any) -> Any:
        ctx = self.create_context()
        current_message = _event_field(event, "message")
        modified = False
        for extension in self.extensions:
            for handler in extension.handlers.get("message_end", []):
                try:
                    current_event = _clone_with(event, message=current_message)
                    handler_result = _invoke_handler(handler, current_event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    message = _result_flag(handler_result, "message")
                    if message is None:
                        continue
                    if _event_field(message, "role") != _event_field(current_message, "role"):
                        self.emit_error(
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

    async def emit_tool_result(self, event: Any) -> Any:
        ctx = self.create_context()
        current_event = _clone_with(event)
        modified = False
        for extension in self.extensions:
            for handler in extension.handlers.get("tool_result", []):
                try:
                    handler_result = _invoke_handler(handler, current_event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if handler_result is None:
                        continue
                    for field_name in ("content", "details", "isError"):
                        value = _result_flag(handler_result, field_name, _MISSING)
                        if value is not _MISSING:
                            if isinstance(current_event, Mapping):
                                current_event[field_name] = value
                            else:
                                setattr(current_event, field_name, value)
                            modified = True
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "tool_result", error)
        if not modified:
            return None
        return {
            "content": _event_field(current_event, "content"),
            "details": _event_field(current_event, "details"),
            "isError": _event_field(current_event, "isError"),
        }

    async def emit_tool_call(self, event: Any) -> Any:
        ctx = self.create_context()
        result: Any = None
        for extension in self.extensions:
            for handler in extension.handlers.get("tool_call", []):
                handler_result = _invoke_handler(handler, event, ctx)
                if hasattr(handler_result, "__await__"):
                    handler_result = await handler_result
                if handler_result:
                    result = handler_result
                    if _result_flag(result, "block", False):
                        return result
        return result

    async def emit_user_bash(self, event: Any) -> Any:
        ctx = self.create_context()
        for extension in self.extensions:
            for handler in extension.handlers.get("user_bash", []):
                try:
                    handler_result = _invoke_handler(handler, event, ctx)
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if handler_result is not None:
                        return handler_result
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "user_bash", error)
        return None

    async def emit_context(self, messages: list[Any]) -> list[Any]:
        ctx = self.create_context()
        current_messages = deepcopy(messages)
        for extension in self.extensions:
            for handler in extension.handlers.get("context", []):
                try:
                    handler_result = _invoke_handler(
                        handler,
                        {"type": "context", "messages": current_messages},
                        ctx,
                    )
                    if hasattr(handler_result, "__await__"):
                        handler_result = await handler_result
                    if _result_flag(handler_result, "messages"):
                        current_messages = _result_flag(handler_result, "messages")
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "context", error)
        return current_messages

    async def emit_before_provider_request(self, payload: Any) -> Any:
        ctx = self.create_context()
        current_payload = payload
        for extension in self.extensions:
            for handler in extension.handlers.get("before_provider_request", []):
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

    async def emit_before_agent_start(
        self,
        prompt: str,
        images: list[ImageContent] | None,
        systemPrompt: str,
        systemPromptOptions: Any,
    ) -> dict[str, Any] | None:
        current_system_prompt = systemPrompt
        ctx = _ContextBase(self, {"getSystemPrompt": lambda: current_system_prompt})
        messages: list[Any] = []
        system_prompt_modified = False
        for extension in self.extensions:
            for handler in extension.handlers.get("before_agent_start", []):
                try:
                    event = {
                        "type": "before_agent_start",
                        "prompt": prompt,
                        "images": images,
                        "systemPrompt": current_system_prompt,
                        "systemPromptOptions": systemPromptOptions,
                    }
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
                        current_system_prompt = _result_flag(handler_result, "systemPrompt")
                        system_prompt_modified = True
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "before_agent_start", error)
        if messages or system_prompt_modified:
            return {
                "messages": messages or None,
                "systemPrompt": current_system_prompt if system_prompt_modified else None,
            }
        return None

    async def emit_agent_end(self, event: Any) -> dict[str, Any] | None:
        """Emit ``agent_end`` while preserving a stop-hook decision.

        Most lifecycle events are observational, but Claude-compatible Stop
        hooks may reject completion and return feedback for another model
        turn.  The generic emitter intentionally discards ordinary results,
        so this event needs an explicit decision channel.
        """

        ctx = self.create_context()
        for extension in self.extensions:
            for handler in extension.handlers.get("agent_end", []):
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

    async def emit_resources_discover(self, cwd: str, reason: str) -> dict[str, list[dict[str, str]]]:
        ctx = self.create_context()
        prompt_paths: list[dict[str, str]] = []
        theme_paths: list[dict[str, str]] = []
        for extension in self.extensions:
            for handler in extension.handlers.get("resources_discover", []):
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
                    ):
                        for path in _result_flag(handler_result, field_name, []) or []:
                            target.append({"path": path, "extensionPath": extension.path})
                except Exception as error:  # noqa: BLE001 - extension code: the failure is reported through emit_extension_exception
                    self._emit_extension_exception(extension.path, "resources_discover", error)
        return {
            "promptPaths": prompt_paths,
            "themePaths": theme_paths,
        }

    async def emit_input(self, text: str, images: list[ImageContent] | None, source: str) -> dict[str, Any]:
        ctx = self.create_context()
        current_text = text
        current_images = images
        for extension in self.extensions:
            for handler in extension.handlers.get("input", []):
                try:
                    event = {"type": "input", "text": current_text, "images": current_images, "source": source}
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

    bindCore = bind_core
    bindCommandContext = bind_command_context
    setUIContext = set_ui_context
    getUIContext = get_ui_context
    hasUI = has_ui
    getExtensionPaths = get_extension_paths
    getAllRegisteredTools = get_all_registered_tools
    get_registered_tools = get_all_registered_tools
    getToolDefinition = get_tool_definition
    getFlags = get_flags
    setFlagValue = set_flag_value
    getFlagValues = get_flag_values
    getShortcuts = get_shortcuts
    getShortcutDiagnostics = get_shortcut_diagnostics
    onError = on_error
    emitError = emit_error
    hasHandlers = has_handlers
    getMessageRenderer = get_message_renderer
    getRegisteredCommands = get_registered_commands
    getCommandDiagnostics = get_command_diagnostics
    getCommand = get_command
    createContext = create_context
    createCommandContext = create_command_context
    emitMessageEnd = emit_message_end
    emitToolResult = emit_tool_result
    emitToolCall = emit_tool_call
    emitUserBash = emit_user_bash
    emitContext = emit_context
    emitBeforeProviderRequest = emit_before_provider_request
    emitBeforeAgentStart = emit_before_agent_start
    emitAgentEnd = emit_agent_end
    emitResourcesDiscover = emit_resources_discover
    emitInput = emit_input


async def emit_session_shutdown_event(extensionRunner: ExtensionRunner, event: Any) -> bool:
    if extensionRunner.has_handlers("session_shutdown"):
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
    "ExtensionErrorListener",
    "ExtensionRunner",
    "ForkHandler",
    "NavigateTreeHandler",
    "NewSessionHandler",
    "ReloadHandler",
    "ShutdownHandler",
    "SwitchSessionHandler",
    ]
