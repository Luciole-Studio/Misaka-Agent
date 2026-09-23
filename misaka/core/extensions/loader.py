"""Local-file Python extension discovery and loading."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from misaka.config import get_agent_dir
from misaka.core.event_bus import EventBusController, createEventBus
from misaka.core.exec import exec_command
from misaka.core.extensions.types import (
    EntryRenderer,
    ExecOptions,
    ExecResult,
    Extension,
    ExtensionFactory,
    ExtensionFlag,
    ExtensionRuntime,
    ExtensionShortcut,
    LoadExtensionsResult,
    MarkdownTransformer,
    PendingNativeProviderRegistration,
    PendingProviderRegistration,
    ProviderConfig,
    RegisteredCommand,
    RegisteredTool,
    ToolDefinition,
    ToolInfo,
    _LoadedExtension,
)
from misaka.core.pi_manifest import read_pi_manifest
from misaka.core.source_info import create_synthetic_source_info
from misaka.core.timings import time
from misaka.utils.paths import resolve_path

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

    def assert_active(self) -> None:
        if self.state == "failed":
            raise RuntimeError(
                f'Extension "{self.extension.path}" failed to load and its API is no longer active.'
            )
        self.runtime.assertActive()

    def apply_runtime_change(self, change: Callable[[], None]) -> None:
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
        self._clear_pending()

    def discard(self) -> None:
        if self.state != "loading":
            return
        self.state = "failed"
        for unsubscribe in self.loadingUnsubscribers:
            unsubscribe()
        self._clear_pending()

    def _clear_pending(self) -> None:
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
        self.load.assert_active()
        self.bus.emit(channel, data)

    def on(self, channel: str, handler: Any) -> Callable[[], None]:
        self.load.assert_active()
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
        self.load.assert_active()

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
        self.load.assert_active()
        parameters = getattr(definition, "parameters", None)
        if parameters is None or isinstance(parameters, list):
            # A tool without a parameter schema would break the provider request; refuse it
            # at registration instead (pi #9300).
            raise ValueError(
                f'Tool "{definition.name}" registered by extension "{self.extension.path}" '
                "must define an object parameter schema."
            )
        self.extension.tools[definition.name] = RegisteredTool(
            definition=definition,
            sourceInfo=self.extension.sourceInfo,
        )
        self.runtime.refreshTools()

    def registerWebSearchProvider(self, provider: Any) -> None:
        from misaka.core.web.registry import validate_provider

        self.load.assert_active()
        name = validate_provider(provider)
        self.extension.webProviders[name] = provider
        self.runtime.refreshTools()

    def unregisterWebSearchProvider(self, name: str) -> None:
        self.load.assert_active()
        self.extension.webProviders.pop(name.strip(), None)
        self.runtime.refreshTools()

    def registerBrowserProvider(self, provider: Any) -> None:
        from misaka.core.web.browser.providers import validate_provider

        self.load.assert_active()
        self.extension.browserProviders[validate_provider(provider)] = provider
        self.runtime.refreshTools()

    def unregisterBrowserProvider(self, name: str) -> None:
        self.load.assert_active()
        self.extension.browserProviders.pop(name.strip(), None)
        self.runtime.refreshTools()

    def registerCommand(self, name: str, options: dict[str, Any]) -> None:
        self.load.assert_active()
        self.extension.commands[name] = RegisteredCommand(
            name=name,
            sourceInfo=self.extension.sourceInfo,
            description=options.get("description"),
            getArgumentCompletions=options.get("getArgumentCompletions"),
            handler=options["handler"],
        )

    def registerShortcut(self, shortcut: str, options: dict[str, Any]) -> None:
        self.load.assert_active()
        self.extension.shortcuts[shortcut] = ExtensionShortcut(
            shortcut=shortcut,
            extensionPath=self.extension.path,
            description=options.get("description"),
            handler=options["handler"],
        )

    def registerFlag(self, name: str, options: dict[str, Any]) -> None:
        self.load.assert_active()
        if "default" in options:
            default = options["default"]
            expected_type = {"boolean": bool, "string": str}.get(options["type"])
            if expected_type is None or not isinstance(default, expected_type):
                actual_type = (
                    "boolean"
                    if isinstance(default, bool)
                    else "string"
                    if isinstance(default, str)
                    else type(default).__name__
                )
                raise TypeError(
                    f'Invalid default for flag "{name}": expected {options["type"]}, got {actual_type}'
                )
        self.extension.flags[name] = ExtensionFlag(
            name=name,
            extensionPath=self.extension.path,
            type=options["type"],
            description=options.get("description"),
            default=options.get("default"),
        )
        if "default" in options and name not in self.runtime.flagValues:
            if self.load.state == "loading":
                self.load.pendingFlagValues.setdefault(name, options["default"])
            else:
                self.runtime.flagValues[name] = options["default"]

    def registerMessageRenderer(self, customType: str, renderer: Any) -> None:
        self.load.assert_active()
        self.extension.messageRenderers[customType] = renderer

    def registerMarkdownTransformer(self, transformer: MarkdownTransformer) -> None:
        self.load.assert_active()
        self.extension.markdownTransformer = transformer

    def registerEntryRenderer(self, customType: str, renderer: EntryRenderer[Any]) -> None:
        self.load.assert_active()
        self.extension.entryRenderers[customType] = renderer

    def getFlag(self, name: str) -> bool | str | None:
        self.load.assert_active()
        if name not in self.extension.flags:
            return None
        if name in self.runtime.flagValues:
            return self.runtime.flagValues[name]
        return self.load.pendingFlagValues.get(name)

    def sendMessage(self, message: Any, options: dict[str, Any] | None = None) -> None:
        self.load.assert_active()
        self.runtime.sendMessage(message, options)

    def sendUserMessage(
        self,
        content: str | list[Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        self.load.assert_active()
        self.runtime.sendUserMessage(content, options)

    def appendEntry(self, customType: str, data: Any = _ENTRY_DATA_UNSET) -> None:
        self.load.assert_active()
        if data is _ENTRY_DATA_UNSET:
            self.runtime.appendEntry(customType)
        else:
            self.runtime.appendEntry(customType, data)

    def setSessionName(self, name: str) -> None:
        self.load.assert_active()
        self.runtime.setSessionName(name)

    def getSessionName(self) -> str | None:
        self.load.assert_active()
        return self.runtime.getSessionName()

    def setLabel(self, entryId: str, label: str | None) -> None:
        self.load.assert_active()
        self.runtime.setLabel(entryId, label)

    async def exec(self, command: str, args: list[str], options: ExecOptions | None = None) -> ExecResult:
        self.load.assert_active()
        resolved_options: ExecOptions = dict(options or {})
        cwd_override = resolved_options.get("cwd")
        resolved_cwd = self.cwd if cwd_override is None else str(cwd_override)
        return await exec_command(command, args, resolved_cwd, resolved_options)

    def getActiveTools(self) -> list[str]:
        self.load.assert_active()
        return self.runtime.getActiveTools()

    def getAllTools(self) -> list[ToolInfo]:
        self.load.assert_active()
        return self.runtime.getAllTools()

    def setActiveTools(self, toolNames: list[str]) -> None:
        self.load.assert_active()
        self.runtime.setActiveTools(toolNames)

    def getCommands(self) -> list[dict[str, Any]]:
        self.load.assert_active()
        return self.runtime.getCommands()

    async def setModel(self, model: Any) -> bool:
        self.load.assert_active()
        return await self.runtime.setModel(model)

    def getThinkingLevel(self) -> str:
        self.load.assert_active()
        return self.runtime.getThinkingLevel()

    def setThinkingLevel(self, level: str) -> None:
        self.load.assert_active()
        self.runtime.setThinkingLevel(level)

    def registerProvider(
        self, providerOrName: Any, config: ProviderConfig | None = None
    ) -> None:
        self.load.assert_active()
        _validate_provider_registration(providerOrName, config)
        if isinstance(providerOrName, str):
            assert config is not None
            self.load.apply_runtime_change(
                lambda: self.runtime.registerProvider(
                    providerOrName, config, self.extension.path
                )
            )
            return
        self.load.apply_runtime_change(
            lambda: self.runtime.registerNativeProvider(
                providerOrName, self.extension.path
            )
        )

    def unregisterProvider(self, name: str) -> None:
        self.load.assert_active()
        self.load.apply_runtime_change(
            lambda: self.runtime.unregisterProvider(name, self.extension.path)
        )


def _not_initialized(*_args: Any, **_kwargs: Any) -> Any:
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


def create_extension_runtime() -> ExtensionRuntime:
    state = _RuntimeState()

    def assert_active() -> None:
        if state.staleMessage:
            raise RuntimeError(state.staleMessage)

    def invalidate(message: str | None = None) -> None:
        if state.staleMessage is not None:
            return
        state.staleMessage = (
            message
            or (
                "This extension ctx is stale after session replacement or reload. "
                "Do not use a captured harn or command ctx after ctx.newSession(), ctx.fork(), "
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

    runtime = ExtensionRuntime(
        sendMessage=_not_initialized,
        sendUserMessage=_not_initialized,
        appendEntry=_not_initialized,
        setSessionName=_not_initialized,
        getSessionName=_not_initialized,
        setLabel=_not_initialized,
        getActiveTools=_not_initialized,
        getAllTools=_not_initialized,
        setActiveTools=_not_initialized,
        refreshTools=lambda: None,
        getCommands=_not_initialized,
        setModel=_set_model_not_initialized,
        getThinkingLevel=_not_initialized,
        setThinkingLevel=_not_initialized,
        flagValues={},
        pendingProviderRegistrations=[],
        pendingNativeProviderRegistrations=[],
        assertActive=assert_active,
        invalidate=invalidate,
        trackEventBusSubscription=track_event_bus_subscription,
        registerProvider=queue_provider,
        registerNativeProvider=queue_native_provider,
        unregisterProvider=unqueue_provider,
    )
    return runtime


def _default_event_bus() -> EventBusController:
    return createEventBus()


async def load_extension_from_factory(
    factory: ExtensionFactory,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
    extension_path: str = "<inline>",
) -> Extension:
    return await _initialize_extension(
        factory,
        extension_path,
        extension_path,
        resolve_path(cwd),
        event_bus,
        runtime,
    )


async def load_extensions(
    paths: list[str],
    cwd: str,
    event_bus: Any | None = None,
    runtime: ExtensionRuntime | None = None,
) -> LoadExtensionsResult:
    extensions: list[Extension] = []
    errors: list[dict[str, str]] = []
    resolved_cwd = resolve_path(cwd)
    resolved_event_bus = event_bus if event_bus is not None else _default_event_bus()
    runtime = runtime or create_extension_runtime()

    for ext_path in paths:
        extension, error = await _load_extension(ext_path, resolved_cwd, resolved_event_bus, runtime)
        if error is not None:
            errors.append({"path": ext_path, "error": error})
            continue
        if extension is not None:
            extensions.append(extension)

    return LoadExtensionsResult(extensions=extensions, errors=errors, runtime=runtime)


def discover_extensions_in_dir(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    discovered: list[str] = []
    try:
        for entry in os.scandir(dir_path):
            entry_path = entry.path
            if (entry.is_file() or entry.is_symlink()) and is_extension_file(entry.name):
                discovered.append(entry_path)
                continue
            if entry.is_dir() or entry.is_symlink():
                entries = resolve_extension_entries(entry_path)
                if entries:
                    discovered.extend(entries)
    except OSError:
        return []
    return discovered


async def discover_and_load_extensions(
    configured_paths: list[str],
    cwd: str,
    agent_dir: str | None = None,
    event_bus: Any | None = None,
) -> LoadExtensionsResult:
    resolved_cwd = resolve_path(cwd)
    resolved_agent_dir = resolve_path(get_agent_dir() if agent_dir is None else agent_dir)
    all_paths: list[str] = []
    seen: set[str] = set()

    def add_paths(paths: list[str]) -> None:
        for candidate in paths:
            resolved = os.path.abspath(candidate)
            if resolved in seen:
                continue
            seen.add(resolved)
            all_paths.append(candidate)

    add_paths(discover_extensions_in_dir(os.path.join(resolved_agent_dir, "extensions")))

    for raw_path in configured_paths:
        resolved = resolve_path(raw_path, resolved_cwd, normalize_unicode_spaces=True)
        if os.path.isdir(resolved):
            entries = resolve_extension_entries(resolved)
            if entries:
                add_paths(entries)
            else:
                add_paths(discover_extensions_in_dir(resolved))
            continue
        add_paths([resolved])

    return await load_extensions(all_paths, resolved_cwd, event_bus)


def resolve_extension_entries(dir_path: str) -> list[str] | None:
    manifest = _read_harn_manifest(dir_path)
    package_json_path = os.path.join(dir_path, "package.json")
    if manifest is None and os.path.exists(package_json_path):
        manifest = read_pi_manifest(package_json_path)
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


def is_extension_file(name: str) -> bool:
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


async def _load_extension(
    path: str,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
) -> tuple[Extension | None, str | None]:
    resolved_path = resolve_path(path, cwd, normalize_unicode_spaces=True)
    try:
        factory = _load_extension_module(resolved_path)
        time(f"{path} module import", "extensions")
        if factory is None:
            return None, f"Extension does not export a valid factory function: {path}"
        extension = await _initialize_extension(
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


def _load_extension_module(resolved_path: str) -> ExtensionFactory | None:
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
    return candidate if callable(candidate) else None


async def _invoke_factory(factory: ExtensionFactory, api: _ExtensionAPI) -> None:
    result = factory(api)
    if inspect.isawaitable(result):
        await result


async def _initialize_extension(
    factory: ExtensionFactory,
    extension_path: str,
    resolved_path: str,
    cwd: str,
    event_bus: Any,
    runtime: ExtensionRuntime,
) -> Extension:
    extension = _create_extension(extension_path, resolved_path)
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


def _create_extension(path: str, resolved_path: str) -> Extension:
    source = path[1:-1].split(":")[0] if path.startswith("<") and path.endswith(">") else "local"
    base_dir = None if path.startswith("<") else os.path.dirname(resolved_path)
    source_info = create_synthetic_source_info(path, {"source": source or "temporary", "baseDir": base_dir})
    return _LoadedExtension(path=path, resolvedPath=resolved_path, sourceInfo=source_info)


createExtensionRuntime = create_extension_runtime
discoverAndLoadExtensions = discover_and_load_extensions
loadExtensionFromFactory = load_extension_from_factory
loadExtensions = load_extensions

__all__ = [
    "createExtensionRuntime",
    "discoverAndLoadExtensions",
    "loadExtensionFromFactory",
    "loadExtensions",
]
