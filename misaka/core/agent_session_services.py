"""Cwd-bound session service construction for coding-agent runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from misaka.agent.types import ThinkingLevel
from misaka.ai.models import ModelsRefreshOptions
from misaka.ai.types import Model
from misaka.config import get_agent_dir
from misaka.core.extensions.types import ToolDefinition
from misaka.core.model_runtime import ModelRuntime
from misaka.core.resource_loader import (
    DefaultResourceLoader,
    DefaultResourceLoaderOptions,
    ResourceLoaderLike,
    ResourceLoaderReloadOptions,
)
from misaka.core.sdk import CreateAgentSessionResult, createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.paths import resolvePath


@dataclass(slots=True)
class AgentSessionRuntimeDiagnostic:
    type: Literal["info", "warning", "error"]
    message: str


class CreateAgentSessionServicesOptions(TypedDict):
    cwd: str
    agentDir: NotRequired[str]
    modelRuntime: NotRequired[ModelRuntime]
    modelRuntimeSignal: NotRequired[Any]
    settingsManager: NotRequired[SettingsManager]
    extensionFlagValues: NotRequired[Mapping[str, bool | str]]
    resourceLoaderOptions: NotRequired[DefaultResourceLoaderOptions]
    resourceLoaderReloadOptions: NotRequired[ResourceLoaderReloadOptions]


@dataclass(slots=True)
class AgentSessionServices:
    cwd: str
    agentDir: str
    modelRuntime: ModelRuntime
    settingsManager: SettingsManager
    resourceLoader: ResourceLoaderLike
    diagnostics: list[AgentSessionRuntimeDiagnostic] = field(default_factory=list)


class CreateAgentSessionFromServicesOptions(TypedDict):
    services: AgentSessionServices
    sessionManager: SessionManager
    sessionStartEvent: NotRequired[dict[str, Any]]
    model: NotRequired[Model[Any]]
    thinkingLevel: NotRequired[ThinkingLevel]
    scopedModels: NotRequired[list[dict[str, Any]]]
    tools: NotRequired[list[str]]
    excludeTools: NotRequired[list[str]]
    noTools: NotRequired[Literal["all", "builtin"]]
    customTools: NotRequired[list[ToolDefinition[Any, Any] | Any]]
    parts: NotRequired[list[Any]]


def applyExtensionFlagValues(
    resource_loader: ResourceLoaderLike,
    extension_flag_values: Mapping[str, bool | str] | None,
) -> list[AgentSessionRuntimeDiagnostic]:
    if not extension_flag_values:
        return []

    diagnostics: list[AgentSessionRuntimeDiagnostic] = []
    extensions_result = resource_loader.getExtensions()
    registered_flags: dict[str, dict[str, str]] = {}
    for extension in extensions_result.extensions:
        for name, flag in extension.flags.items():
            registered_flags[name] = {"type": flag.type}

    unknown_flags: list[str] = []
    for name, value in extension_flag_values.items():
        flag = registered_flags.get(name)
        if flag is None:
            unknown_flags.append(name)
            continue
        if flag["type"] == "boolean":
            extensions_result.runtime.flagValues[name] = True
            continue
        if isinstance(value, str):
            extensions_result.runtime.flagValues[name] = value
            continue
        diagnostics.append(
            AgentSessionRuntimeDiagnostic(
                type="error",
                message=f'Extension flag "--{name}" requires a value',
            )
        )

    if unknown_flags:
        suffix = "" if len(unknown_flags) == 1 else "s"
        rendered = ", ".join(f"--{name}" for name in unknown_flags)
        diagnostics.append(
            AgentSessionRuntimeDiagnostic(
                type="error",
                message=f"Unknown option{suffix}: {rendered}",
            )
        )

    return diagnostics


async def createAgentSessionServices(
    options: CreateAgentSessionServicesOptions,
) -> AgentSessionServices:
    cwd = resolvePath(options["cwd"])
    agent_dir = resolvePath(options["agentDir"]) if options.get("agentDir") else get_agent_dir()
    # Credentials sit under credentials/ in the home and in any directory laid out like it,
    # never beside settings.json (config.home.LAYOUT).
    from misaka.config import home

    role_dir = agent_dir if options.get("agentDir") else None
    model_runtime = options.get("modelRuntime") or await ModelRuntime.create({"authPath": str(home.path("auth", role_dir)), "modelsPath": str(home.path("models", role_dir)), "signal": options.get("modelRuntimeSignal")})
    settings_manager = options.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    resource_loader = DefaultResourceLoader(
        {
            **(options.get("resourceLoaderOptions") or {}),
            "cwd": cwd,
            "agentDir": agent_dir,
            "settingsManager": settings_manager,
        }
    )
    await resource_loader.reload(options.get("resourceLoaderReloadOptions"))

    diagnostics: list[AgentSessionRuntimeDiagnostic] = []
    extensions_result = resource_loader.getExtensions()
    for registration in list(extensions_result.runtime.pendingProviderRegistrations):
        try:
            model_runtime.registerProvider(registration.name, registration.config)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(
                AgentSessionRuntimeDiagnostic(
                    type="error",
                    message=f'Extension "{registration.extensionPath}" error: {error}',
                )
            )
    extensions_result.runtime.pendingProviderRegistrations = []
    for registration in list(
        extensions_result.runtime.pendingNativeProviderRegistrations
    ):
        try:
            model_runtime.registerNativeProvider(registration.provider)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(
                AgentSessionRuntimeDiagnostic(
                    type="error",
                    message=f'Extension "{registration.extensionPath}" error: {error}',
                )
            )
    extensions_result.runtime.pendingNativeProviderRegistrations = []
    for virtual in list(extensions_result.runtime.pendingVirtualModelRegistrations):
        try:
            model_runtime.registerVirtualModel(virtual.definition)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(
                AgentSessionRuntimeDiagnostic(
                    type="error",
                    message=f'Extension "{virtual.extensionPath}" error: {error}',
                )
            )
    extensions_result.runtime.pendingVirtualModelRegistrations = []

    refresh_result = await model_runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    # MISAKA fork: a catalog that could not be restored is reported rather than dropped silently.
    for provider_id, error in refresh_result.errors.items():
        diagnostics.append(
            AgentSessionRuntimeDiagnostic(
                type="warning",
                message=f'Could not restore model catalog for "{provider_id}": {error}',
            )
        )
    diagnostics.extend(applyExtensionFlagValues(resource_loader, options.get("extensionFlagValues")))

    return AgentSessionServices(
        cwd=cwd,
        agentDir=agent_dir,
        modelRuntime=model_runtime,
        settingsManager=settings_manager,
        resourceLoader=resource_loader,
        diagnostics=diagnostics,
    )


async def createAgentSessionFromServices(
    options: CreateAgentSessionFromServicesOptions,
) -> CreateAgentSessionResult:
    services = options["services"]
    return await createAgentSession(
        {
            "cwd": services.cwd,
            "agentDir": services.agentDir,
            "modelRuntime": services.modelRuntime,
            "settingsManager": services.settingsManager,
            "resourceLoader": services.resourceLoader,
            "sessionManager": options["sessionManager"],
            "model": options.get("model"),
            "thinkingLevel": options.get("thinkingLevel"),
            "scopedModels": options.get("scopedModels"),
            "tools": options.get("tools"),
            "excludeTools": options.get("excludeTools"),
            "noTools": options.get("noTools"),
            "customTools": options.get("customTools"),
            "parts": options.get("parts"),
            "sessionStartEvent": options.get("sessionStartEvent"),
        }
    )

__all__ = [
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionServicesOptions",
    "createAgentSessionFromServices",
    "createAgentSessionServices",
]
