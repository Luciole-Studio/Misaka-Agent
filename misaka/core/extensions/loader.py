"""Local-file Python extension discovery and loading."""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import os
import sys
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from misaka.config import get_agent_dir
from misaka.core.event_bus import EventBusController, createEventBus
from misaka.core.exec import execCommand
from misaka.core.extensions.types import (
    EntryRenderer,
    ExecOptions,
    ExecResult,
    Extension,
    ExtensionFactory,
    ExtensionFlag,
    ExtensionRuntime,
    ExtensionShortcut,
    ExtensionVirtualModel,
    LoadExtensionsResult,
    MarkdownTransformer,
    PendingNativeProviderRegistration,
    PendingProviderRegistration,
    PendingVirtualModelRegistration,
    ProviderConfig,
    RegisteredCommand,
    RegisteredTool,
    ToolDefinition,
    ToolInfo,
    ToolRendererResolver,
    _LoadedExtension,
)
from misaka.core.mcp_servers import (
    McpServerConfig,
    McpServerRegistry,
    RegisteredMcpServer,
    mcpNamespace,
    validateMcpServerConfig,
)
from misaka.core.pi_manifest import readPiManifest
from misaka.core.settings_manager import Settings
from misaka.core.source_info import (
    createSyntheticSourceInfo,
    getSyntheticPathSource,
    isSyntheticPath,
)
from misaka.core.timings import time
from misaka.core.virtual_models import VirtualModelDefinition
from misaka.utils.node_fs import readdirEntries
from misaka.utils.paths import resolvePath
from misaka.utils.values import read_field

_ENTRY_DATA_UNSET = object()


@dataclass(slots=True)
class _RuntimeState:
    staleMessage: str | None = None
    eventBusUnsubscribers: set[Any] = field(default_factory=set)


@dataclass(slots=True)
class _FactoryLoad:
    extension: Extension
    runtime: ExtensionRuntime
    state: str = "loading"
    pendingFlagValues: dict[str, bool | str] = field(default_factory=dict)
    pendingRuntimeChanges: list[Callable[[], None]] = field(default_factory=list)
    loadingUnsubscribers: list[Callable[[], None]] = field(default_factory=list)

    def assertActive(self) -> None:
        if self.state == "failed":
            raise RuntimeError(
                f'Extension "{self.extension.path}" failed to load and its API is no longer active.'
            )
        self.runtime.assertActive()

    def applyRuntimeChange(self, change: Callable[[], None]) -> None:
        if self.state == "loading":
            self.pendingRuntimeChanges.append(change)
        else:
            change()

    def commit(self) -> None:
        if self.state != "loading":
            return
        self.runtime.assertActive()
        for name, value in self.pendingFlagValues.items():
            self.runtime.flagValues.setdefault(name, value)
        for apply in self.pendingRuntimeChanges:
            apply()
        self.state = "active"
        self.clearPending()

    def discard(self) -> None:
        if self.state != "loading":
            return
        self.state = "failed"
        for unsubscribe in self.loadingUnsubscribers:
            unsubscribe()
        self.clearPending()

    def clearPending(self) -> None:
        self.pendingFlagValues.clear()
        self.pendingRuntimeChanges.clear()
        self.loadingUnsubscribers.clear()


@dataclass(slots=True)
class _TrackedEventBus:
    """Extension-facing event bus: subscriptions are recorded on the runtime and
    unsubscribed together on invalidate (pi #7656 leak fix)."""
    load: _FactoryLoad
    bus: Any

    def emit(self, channel: str, data: Any) -> None:
        self.load.assertActive()
        self.bus.emit(channel, data)

    def on(self, channel: str, handler: Any) -> Callable[[], None]:
        self.load.assertActive()
        unsubscribe = self.load.runtime.trackEventBusSubscription(
            self.bus.on(channel, handler)
        )
        if self.load.state == "loading":
            self.load.loadingUnsubscribers.append(unsubscribe)
        return unsubscribe


@dataclass(slots=True)
class _ExtensionAPI:
    extension: Extension
    cwd: str
    runtime: ExtensionRuntime
    load: _FactoryLoad
    events: Any

    def on(self, event: str, handler: Any) -> Callable[[], None]:
        """Register a handler; the returned function drops it again. Handlers added or removed
        during a dispatch apply to later dispatches, not the current one (pi #8967)."""
        self.load.assertActive()

        def registered_handler(*args: Any) -> Any:
            return handler(*args)

        handlers = self.extension.handlers.setdefault(event, [])
        handlers.append(registered_handler)

        def unsubscribe() -> None:
            handlers = self.extension.handlers.get(event)
            if not handlers:
                return
            if registered_handler not in handlers:
                return
            handlers.remove(registered_handler)
            if not handlers:
                self.extension.handlers.pop(event, None)

        return unsubscribe

    def registerTool(self, definition: ToolDefinition[Any, Any]) -> None:
        self.load.assertActive()
        parameters = read_field(definition, "parameters")
        # MISAKA fork: an object schema is a JSON-schema mapping or a pydantic model class (pi: any non-array object).
        if not (
            isinstance(parameters, Mapping)
            or (isinstance(parameters, type) and hasattr(parameters, "model_json_schema"))
        ):
            raise ValueError(
                f'Tool "{read_field(definition, "name")}" registered by extension "{self.extension.path}" '
                "must define an object parameter schema."
            )
        # MISAKA fork: a definition may also be a mapping, as pi's are plain objects.
        if isinstance(definition, Mapping):
            definition = ToolDefinition(**definition)
        self.extension.tools[definition.name] = RegisteredTool(
            definition=definition,
            sourceInfo=self.extension.sourceInfo,
        )
        self.runtime.refreshTools()

    def registerWebSearchProvider(self, provider: Any) -> None:
        from misaka.core.web.registry import validate_provider

        self.load.assertActive()
        name = validate_provider(provider)
        self.extension.webProviders[name] = provider
        self.runtime.refreshTools()

    def unregisterWebSearchProvider(self, name: str) -> None:
        self.load.assertActive()
        self.extension.webProviders.pop(name.strip(), None)
        self.runtime.refreshTools()

    def registerBrowserProvider(self, provider: Any) -> None:
        from misaka.core.web.browser.providers import validate_provider

        self.load.assertActive()
        self.extension.browserProviders[validate_provider(provider)] = provider
        self.runtime.refreshTools()

    def unregisterBrowserProvider(self, name: str) -> None:
        self.load.assertActive()
        self.extension.browserProviders.pop(name.strip(), None)
        self.runtime.refreshTools()

    def registerCommand(self, name: str, options: dict[str, Any]) -> None:
        self.load.assertActive()
        if not isinstance(name, str) or len(name) == 0:
            raise ValueError(
                f'Command registered by extension "{self.extension.path}" must have a non-empty string name. '
                'Use pi.registerCommand("name", { description, handler }).'
            )
        if not callable((options or {}).get("handler")):
            raise ValueError(f'Command "/{name}" registered by extension "{self.extension.path}" must define handler().')  # noqa: TRY004 - upstream raises a plain Error
        self.extension.commands[name] = RegisteredCommand(
            name=name,
            sourceInfo=self.extension.sourceInfo,
            description=options.get("description"),
            getArgumentCompletions=options.get("getArgumentCompletions"),
            handler=options["handler"],
        )

    def registerShortcut(self, shortcut: str, options: dict[str, Any]) -> None:
        self.load.assertActive()
        self.extension.shortcuts[shortcut] = ExtensionShortcut(
            shortcut=shortcut,
            extensionPath=self.extension.path,
            description=options.get("description"),
            handler=options["handler"],
        )

    def registerFlag(self, name: str, options: dict[str, Any]) -> None:
        self.load.assertActive()
        if "default" in options:
            default = options["default"]
            expected_type = {"boolean": bool, "string": str}.get(options.get("type"))
            if expected_type is None or not isinstance(default, expected_type):
                actual_type = (
                    "boolean"
                    if isinstance(default, bool)
                    else "string"
                    if isinstance(default, str)
                    else type(default).__name__
                )
                raise TypeError(
                    f'Invalid default for flag "{name}": expected {options.get("type")}, got {actual_type}'
                )
        self.extension.flags[name] = ExtensionFlag(
            name=name,
            extensionPath=self.extension.path,
            type=options.get("type"),
            description=options.get("description"),
            default=options.get("default"),
        )
        if "default" in options and name not in self.runtime.flagValues:
            if self.load.state == "loading":
                self.load.pendingFlagValues.setdefault(name, options["default"])
            else:
                self.runtime.flagValues[name] = options["default"]

    def registerMessageRenderer(self, customType: str, renderer: Any) -> None:
        self.load.assertActive()
        self.extension.messageRenderers[customType] = renderer

    def registerMarkdownTransformer(self, transformer: MarkdownTransformer) -> None:
        self.load.assertActive()
        self.extension.markdownTransformer = transformer

    def registerEntryRenderer(self, customType: str, renderer: EntryRenderer[Any]) -> None:
        self.load.assertActive()
        self.extension.entryRenderers[customType] = renderer

    def registerToolRenderer(self, resolver: ToolRendererResolver) -> None:
        self.load.assertActive()
        if self.extension.toolRenderers is None:
            self.extension.toolRenderers = []
        self.extension.toolRenderers.append(resolver)

    def getFlag(self, name: str) -> bool | str | None:
        self.load.assertActive()
        if name not in self.extension.flags:
            return None
        if name in self.runtime.flagValues:
            return self.runtime.flagValues[name]
        return self.load.pendingFlagValues.get(name)

    def sendMessage(self, message: Any, options: dict[str, Any] | None = None) -> None:
        self.load.assertActive()
        self.runtime.sendMessage(message, options)

    def sendUserMessage(
        self,
        content: str | list[Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        self.load.assertActive()
        self.runtime.sendUserMessage(content, options)

    def appendEntry(self, customType: str, data: Any = _ENTRY_DATA_UNSET) -> None:
        self.load.assertActive()
        if data is _ENTRY_DATA_UNSET:
            self.runtime.appendEntry(customType)
        else:
            self.runtime.appendEntry(customType, data)

    def setSessionName(self, name: str) -> None:
        self.load.assertActive()
        self.runtime.setSessionName(name)

    def getSessionName(self) -> str | None:
        self.load.assertActive()
        return self.runtime.getSessionName()

    def setLabel(self, entryId: str, label: str | None) -> None:
        self.load.assertActive()
        self.runtime.setLabel(entryId, label)

    async def exec(self, command: str, args: list[str], options: ExecOptions | None = None) -> ExecResult:
        self.load.assertActive()
        resolved_options: ExecOptions = dict(options or {})
        cwd_override = resolved_options.get("cwd")
        resolved_cwd = self.cwd if cwd_override is None else str(cwd_override)
        return await execCommand(command, args, resolved_cwd, resolved_options)

    def getActiveTools(self) -> list[str]:
        self.load.assertActive()
        return self.runtime.getActiveTools()

    def getAllTools(self) -> list[ToolInfo]:
        self.load.assertActive()
        return self.runtime.getAllTools()

    def getSettings(self) -> Settings:
        self.load.assertActive()
        return self.runtime.getSettings()

    def setActiveTools(self, toolNames: list[str]) -> None:
        self.load.assertActive()
        self.runtime.setActiveTools(toolNames)

    def getCommands(self) -> list[dict[str, Any]]:
        self.load.assertActive()
        return self.runtime.getCommands()

    async def setModel(self, model: Any) -> bool:
        self.load.assertActive()
        return await self.runtime.setModel(model)

    def getThinkingLevel(self) -> str:
        self.load.assertActive()
        return self.runtime.getThinkingLevel()

    def setThinkingLevel(self, level: str) -> None:
        self.load.assertActive()
        self.runtime.setThinkingLevel(level)

    def registerProvider(
        self, providerOrName: Any, config: ProviderConfig | None = None
    ) -> None:
        self.load.assertActive()
        _validate_provider_registration(providerOrName, config)
        if isinstance(providerOrName, str):
            assert config is not None
            self.load.applyRuntimeChange(
                lambda: self.runtime.registerProvider(
                    providerOrName, config, self.extension.path
                )
            )
            return
        self.load.applyRuntimeChange(
            lambda: self.runtime.registerNativeProvider(
                providerOrName, self.extension.path
            )
        )

    def unregisterProvider(self, name: str) -> None:
        self.load.assertActive()
        self.load.applyRuntimeChange(
            lambda: self.runtime.unregisterProvider(name, self.extension.path)
        )

    def registerMcpServer(self, name: str, config: McpServerConfig) -> None:
        self.load.assertActive()
        validated = validateMcpServerConfig(name, config)
        if isinstance(validated, str):
            raise ValueError(f'Invalid MCP server registered by extension "{self.extension.path}": {validated}')  # noqa: TRY004 - an invalid config, reported as upstream's Error
        registered = self.runtime.mcpServers.get(name)
        owner = registered.extensionPath if registered is not None else None
        if owner is not None and owner != self.extension.path:
            raise ValueError(f'MCP server "{name}" is already registered by extension "{owner}"')
        # Names that differ only in `-` and `_` would share a namespace.
        clash = next(
            (
                server
                for server in self.runtime.mcpServers.list()
                if server.name != name and mcpNamespace(server.name) == mcpNamespace(name)
            ),
            None,
        )
        if clash is not None:
            raise ValueError(f'MCP server "{name}" conflicts with registered server "{clash.name}"')
        server = RegisteredMcpServer(name=name, config=copy.deepcopy(validated), extensionPath=self.extension.path)
        self.load.applyRuntimeChange(lambda: self.runtime.mcpServers.register(server))

    def unregisterMcpServer(self, name: str) -> None:
        self.load.assertActive()
        self.load.applyRuntimeChange(lambda: self.runtime.mcpServers.unregister(name, self.extension.path))

    def getMcpServers(self) -> list[RegisteredMcpServer]:
        self.load.assertActive()
        return self.runtime.mcpServers.list()

    def registerVirtualModel(self, model: ExtensionVirtualModel) -> None:
        self.load.assertActive()
        runtime = self.runtime
        route = read_field(model, "route")
        # Routing runs after the runner binds, so the context is created per request. The state
        # comes from the session branch that this router wrote.
        definition = VirtualModelDefinition(
            provider=read_field(model, "provider"),
            id=read_field(model, "id"),
            name=read_field(model, "name"),
            route=lambda request: route(request, runtime.createContext()),
            thinkingLevels=read_field(model, "thinkingLevels"),
            contextWindow=read_field(model, "contextWindow"),
            maxTokens=read_field(model, "maxTokens"),
            input=read_field(model, "input"),
        )
        self.load.applyRuntimeChange(lambda: runtime.registerVirtualModel(definition, self.extension.path))

    def unregisterVirtualModel(self, provider: str, id: str) -> None:
        self.load.assertActive()
        self.load.applyRuntimeChange(lambda: self.runtime.unregisterVirtualModel(provider, id))


extensionCacheCwd: str | None = None
extensionCacheGeneration = 0
extensionCache: dict[str, ExtensionFactory] = {}


@dataclass(frozen=True, slots=True)
class ExtensionCacheToken:
    cwd: str
    generation: int


def clearExtensionCache() -> None:
    global extensionCacheCwd, extensionCacheGeneration
    extensionCache.clear()
    extensionCacheCwd = None
    extensionCacheGeneration += 1


def useExtensionCacheCwd(cwd: str) -> ExtensionCacheToken:
    global extensionCacheCwd
    resolvedCwd = resolvePath(cwd)
    if extensionCacheCwd is not None and extensionCacheCwd != resolvedCwd:
        clearExtensionCache()
    extensionCacheCwd = resolvedCwd
    return ExtensionCacheToken(cwd=resolvedCwd, generation=extensionCacheGeneration)


def notInitialized(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("Extension runtime not initialized. Action methods cannot be called during extension loading.")


async def _set_model_not_initialized(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("Extension runtime not initialized")


def _validate_provider_registration(
    provider_or_name: Any,
    config: ProviderConfig | None,
) -> None:
    if isinstance(provider_or_name, str) and config is None:
        raise TypeError("config is required for legacy provider registration")
    if not isinstance(provider_or_name, str) and config is not None:
        raise TypeError("native provider registration takes one provider object")


def createExtensionRuntime() -> ExtensionRuntime:
    state = _RuntimeState()

    def assertActive() -> None:
        if state.staleMessage:
            raise RuntimeError(state.staleMessage)

    def invalidate(message: str | None = None) -> None:
        if state.staleMessage is not None:
            return
        state.staleMessage = (
            message
            or (
                "This extension ctx is stale after session replacement or reload. "
                "Do not use a captured pi or command ctx after ctx.newSession(), ctx.fork(), "
                "ctx.switchSession(), or ctx.reload(). For newSession, fork, and switchSession, "
                "move post-replacement work into withSession and use the ctx passed to withSession. "
                "For reload, do not use the old ctx after await ctx.reload()."
            )
        )
        for unsubscribe in list(state.eventBusUnsubscribers):  # drop every bus subscription this runtime made (pi #7656)
            unsubscribe()
        state.eventBusUnsubscribers.clear()

    def track_event_bus_subscription(unsubscribe: Callable[[], None]) -> Callable[[], None]:
        active = True

        def tracked_unsubscribe() -> None:
            nonlocal active
            if not active:
                return
            active = False
            state.eventBusUnsubscribers.discard(tracked_unsubscribe)
            unsubscribe()

        state.eventBusUnsubscribers.add(tracked_unsubscribe)
        return tracked_unsubscribe

    def queue_provider(
        name: str,
        config: ProviderConfig,
        extension_path: str | None = None,
    ) -> None:
        source = "<unknown>" if extension_path is None else extension_path
        runtime.pendingProviderRegistrations.append(
            PendingProviderRegistration(
                name=name,
                config=config,
                extensionPath=source,
            )
        )

    def queue_native_provider(
        provider: Any,
        extension_path: str | None = None,
    ) -> None:
        source = "<unknown>" if extension_path is None else extension_path
        runtime.pendingNativeProviderRegistrations.append(
            PendingNativeProviderRegistration(
                provider=provider,
                extensionPath=source,
            )
        )

    def unqueue_provider(name: str, _extension_path: str | None = None) -> None:
        runtime.pendingProviderRegistrations[:] = [
            entry for entry in runtime.pendingProviderRegistrations if entry.name != name
        ]
        runtime.pendingNativeProviderRegistrations[:] = [
            entry
            for entry in runtime.pendingNativeProviderRegistrations
            if getattr(entry.provider, "id", None) != name
        ]

    def queue_virtual_model(definition: VirtualModelDefinition, extension_path: str | None = None) -> None:
        source = "<unknown>" if extension_path is None else extension_path
        runtime.pendingVirtualModelRegistrations.append(
            PendingVirtualModelRegistration(definition=definition, extensionPath=source)
        )

    def unqueue_virtual_model(provider: str, id: str) -> None:
        runtime.pendingVirtualModelRegistrations[:] = [
            entry
            for entry in runtime.pendingVirtualModelRegistrations
            if entry.definition.provider != provider or entry.definition.id != id
        ]

    runtime = ExtensionRuntime(
        sendMessage=notInitialized,
        sendUserMessage=notInitialized,
        appendEntry=notInitialized,
        setSessionName=notInitialized,
        getSessionName=notInitialized,
        setLabel=notInitialized,
        getActiveTools=notInitialized,
        getAllTools=notInitialized,
        getSettings=notInitialized,
        setActiveTools=notInitialized,
        refreshTools=lambda: None,
        getCommands=notInitialized,
        setModel=_set_model_not_initialized,
        getThinkingLevel=notInitialized,
        setThinkingLevel=notInitialized,
        flagValues={},
        pendingProviderRegistrations=[],
        pendingNativeProviderRegistrations=[],
        pendingVirtualModelRegistrations=[],
        createContext=notInitialized,
        assertActive=assertActive,
        invalidate=invalidate,
        trackEventBusSubscription=track_event_bus_subscription,
        registerProvider=queue_provider,
        registerNativeProvider=queue_native_provider,
        unregisterProvider=unqueue_provider,
        mcpServers=McpServerRegistry(),
        # Pre-bind: queue registrations so bindCore() can flush them once the model registry is
        # available. bindCore() replaces both with direct calls.
        registerVirtualModel=queue_virtual_model,
        unregisterVirtualModel=unqueue_virtual_model,
    )
    return runtime


def _default_event_bus() -> EventBusController:
    return createEventBus()


async def loadExtensionFromFactory(
    factory: ExtensionFactory,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
    extension_path: str = "<inline>",
) -> Extension:
    return await initializeExtension(
        factory,
        extension_path,
        extension_path,
        resolvePath(cwd),
        event_bus,
        runtime,
    )


async def loadExtensionsInternal(
    paths: list[str],
    cwd: str,
    event_bus: Any | None = None,
    runtime: ExtensionRuntime | None = None,
    useCache: bool = False,
) -> LoadExtensionsResult:
    extensions: list[Extension] = []
    errors: list[dict[str, str]] = []
    cacheToken = useExtensionCacheCwd(cwd) if useCache else None
    resolved_cwd = cacheToken.cwd if cacheToken is not None else resolvePath(cwd)
    resolved_event_bus = event_bus if event_bus is not None else _default_event_bus()
    runtime = runtime or createExtensionRuntime()

    for ext_path in paths:
        extension, error = await loadExtension(ext_path, resolved_cwd, resolved_event_bus, runtime, cacheToken)
        if error is not None:
            errors.append({"path": ext_path, "error": error})
            continue
        if extension is not None:
            extensions.append(extension)

    return LoadExtensionsResult(extensions=extensions, errors=errors, warnings=[], runtime=runtime)


async def loadExtensions(
    paths: list[str],
    cwd: str,
    event_bus: Any | None = None,
    runtime: ExtensionRuntime | None = None,
) -> LoadExtensionsResult:
    return await loadExtensionsInternal(paths, cwd, event_bus, runtime)


async def loadExtensionsCached(
    paths: list[str],
    cwd: str,
    event_bus: Any | None = None,
    runtime: ExtensionRuntime | None = None,
) -> LoadExtensionsResult:
    return await loadExtensionsInternal(paths, cwd, event_bus, runtime, True)


def discoverExtensionsInDir(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    discovered: list[str] = []
    try:
        for entry in readdirEntries(dir_path):
            entry_path = entry.path
            if (entry.is_file() or entry.is_symlink()) and isExtensionFile(entry.name):
                discovered.append(entry_path)
                continue
            if entry.is_dir() or entry.is_symlink():
                entries = resolveExtensionEntries(entry_path)
                if entries:
                    discovered.extend(entries)
    except OSError:
        return []
    return discovered


async def discoverAndLoadExtensions(
    configured_paths: list[str],
    cwd: str,
    agent_dir: str | None = None,
    event_bus: Any | None = None,
) -> LoadExtensionsResult:
    resolved_cwd = resolvePath(cwd)
    resolved_agent_dir = resolvePath(get_agent_dir() if agent_dir is None else agent_dir)
    all_paths: list[str] = []
    seen: set[str] = set()

    def add_paths(paths: list[str]) -> None:
        for candidate in paths:
            resolved = os.path.abspath(candidate)
            if resolved in seen:
                continue
            seen.add(resolved)
            all_paths.append(candidate)

    add_paths(discoverExtensionsInDir(os.path.join(resolved_agent_dir, "extensions")))

    for raw_path in configured_paths:
        resolved = resolvePath(raw_path, resolved_cwd, {"normalizeUnicodeSpaces": True})
        if os.path.isdir(resolved):
            entries = resolveExtensionEntries(resolved)
            if entries:
                add_paths(entries)
            else:
                add_paths(discoverExtensionsInDir(resolved))
            continue
        add_paths([resolved])

    return await loadExtensions(all_paths, resolved_cwd, event_bus)


def resolveExtensionEntries(dir_path: str) -> list[str] | None:
    manifest = _read_harn_manifest(dir_path)
    package_json_path = os.path.join(dir_path, "package.json")
    if manifest is None and os.path.exists(package_json_path):
        manifest = readPiManifest(package_json_path)
    if manifest and manifest.get("extensions"):
        entries = [
            os.path.abspath(os.path.join(dir_path, candidate))
            for candidate in manifest["extensions"]
            if os.path.exists(os.path.join(dir_path, candidate))
        ]
        if entries:
            return entries
    index_py = os.path.join(dir_path, "index.py")
    if os.path.exists(index_py):
        return [index_py]
    return None


def isExtensionFile(name: str) -> bool:
    return name.endswith(".py")


def _read_harn_manifest(dir_path: str) -> dict[str, list[str]] | None:
    package_json_path = os.path.join(dir_path, "package.json")
    if os.path.exists(package_json_path):
        return _read_harn_package_json_manifest(package_json_path)
    pyproject_path = os.path.join(dir_path, "pyproject.toml")
    if os.path.exists(pyproject_path):
        return _read_harn_pyproject_manifest(pyproject_path)
    return None


def _read_harn_package_json_manifest(
    package_json_path: str,
) -> dict[str, list[str]] | None:
    try:
        package = json.loads(
            Path(package_json_path).read_text(encoding="utf-8").removeprefix("\ufeff")
        )
    except (OSError, ValueError):
        return None
    harn_section = package.get("harn") if isinstance(package, dict) else None
    return _validate_harn_manifest(harn_section)


def _read_harn_pyproject_manifest(pyproject_path: str) -> dict[str, list[str]] | None:
    try:
        package = tomllib.loads(
            Path(pyproject_path).read_text(encoding="utf-8").removeprefix("\ufeff")
        )
    except (OSError, ValueError):
        return None
    tool_section = package.get("tool")
    harn_section = tool_section.get("harn") if isinstance(tool_section, dict) else None
    return _validate_harn_manifest(harn_section)


def _validate_harn_manifest(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    entries = value.get("extensions")
    if isinstance(entries, list) and all(isinstance(entry, str) for entry in entries):
        return {"extensions": entries}
    return {}


async def loadExtension(
    path: str,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
    cacheToken: ExtensionCacheToken | None = None,
) -> tuple[Extension | None, str | None]:
    resolved_path = resolvePath(path, cwd, {"normalizeUnicodeSpaces": True})
    try:
        factory = loadExtensionModule(resolved_path, cacheToken)
        time(f"{path} module import", "extensions")
        if factory is None:
            return None, f"Extension does not export a valid factory function: {path}"
        extension = await initializeExtension(
            factory,
            path,
            resolved_path,
            cwd,
            event_bus,
            runtime,
        )
        return extension, None
    except Exception as error:  # noqa: BLE001 - extension code: a failing factory is reported as a load error
        return None, f"Failed to load extension: {error}"


def isCurrentCacheToken(cacheToken: ExtensionCacheToken | None) -> bool:
    return (
        cacheToken is not None
        and extensionCacheCwd == cacheToken.cwd
        and extensionCacheGeneration == cacheToken.generation
    )


def loadExtensionModule(resolved_path: str, cacheToken: ExtensionCacheToken | None = None) -> ExtensionFactory | None:
    if isCurrentCacheToken(cacheToken):
        cachedFactory = extensionCache.get(resolved_path)
        if cachedFactory is not None:
            return cachedFactory

    if not os.path.exists(resolved_path):
        raise FileNotFoundError(f"Extension path does not exist: {resolved_path}")
    spec = importlib.util.spec_from_file_location(f"harn_extension_{uuid.uuid4().hex}", resolved_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create module spec for {resolved_path}")
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous_module is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module
        raise
    candidate = getattr(module, "default", None)
    if not callable(candidate):
        return None
    if isCurrentCacheToken(cacheToken):
        extensionCache[resolved_path] = candidate
    return candidate


async def _invoke_factory(factory: ExtensionFactory, api: _ExtensionAPI) -> None:
    result = factory(api)
    if inspect.isawaitable(result):
        await result


async def initializeExtension(
    factory: ExtensionFactory,
    extension_path: str,
    resolved_path: str,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
) -> Extension:
    extension = createExtension(extension_path, resolved_path)
    load = _FactoryLoad(extension=extension, runtime=runtime)
    api = _ExtensionAPI(
        extension=extension,
        cwd=cwd,
        runtime=runtime,
        load=load,
        events=_TrackedEventBus(load=load, bus=event_bus),
    )
    try:
        await _invoke_factory(factory, api)
        load.commit()
    except BaseException:
        load.discard()
        raise
    time(f"{extension_path} factory", "extensions")
    return extension


def createExtension(path: str, resolved_path: str) -> Extension:
    synthetic_source = getSyntheticPathSource(path)
    source = synthetic_source if synthetic_source is not None else "local"
    base_dir = None if isSyntheticPath(path) else os.path.dirname(resolved_path)
    source_info = createSyntheticSourceInfo(path, {"source": source, "baseDir": base_dir})
    return _LoadedExtension(path=path, resolvedPath=resolved_path, sourceInfo=source_info)



__all__ = [
    "clearExtensionCache",
    "createExtensionRuntime",
    "discoverAndLoadExtensions",
    "loadExtensionFromFactory",
    "loadExtensions",
    "loadExtensionsCached",
]
