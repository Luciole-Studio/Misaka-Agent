"""Cwd-bound session service construction for coding-agent runtimes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from misaka.agent.types import ThinkingLevel
from misaka.ai.types import Model
from misaka.config import get_agent_dir
from misaka.core.auth_storage import AuthStorage
from misaka.core.extensions.types import ToolDefinition
from misaka.core.http_dispatcher import applyHttpProxySettings
from misaka.core.model_registry import ModelRegistry
from misaka.core.resource_loader import (
    DefaultResourceLoader,
    DefaultResourceLoaderOptions,
    ResourceLoaderLike,
    ResourceLoaderReloadOptions,
)
from misaka.core.sdk import CreateAgentSessionResult, create_agent_session
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.paths import resolve_path


@dataclass(slots=True)
class AgentSessionRuntimeDiagnostic:
    type: Literal["info", "warning", "error"]
    message: str


class CreateAgentSessionServicesOptions(TypedDict):
    cwd: str
    agentDir: NotRequired[str]
    authStorage: NotRequired[AuthStorage]
    settingsManager: NotRequired[SettingsManager]
    modelRegistry: NotRequired[ModelRegistry]
    extensionFlagValues: NotRequired[Mapping[str, bool | str]]
    resourceLoaderOptions: NotRequired[DefaultResourceLoaderOptions]
    resourceLoaderReloadOptions: NotRequired[ResourceLoaderReloadOptions]


@dataclass(slots=True)
class AgentSessionServices:
    cwd: str
    agentDir: str
    authStorage: AuthStorage
    settingsManager: SettingsManager
    modelRegistry: ModelRegistry
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


def apply_extension_flag_values(
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


async def create_agent_session_services(
    options: CreateAgentSessionServicesOptions,
) -> AgentSessionServices:
    cwd = resolve_path(options["cwd"])
    agent_dir = resolve_path(options["agentDir"]) if options.get("agentDir") else get_agent_dir()
    # Credentials sit under credentials/ in the home and in any directory laid out like it,
    # never beside settings.json (config.home.LAYOUT).
    from misaka.config import home

    auth_storage = options.get("authStorage") or AuthStorage.create(
        str(home.path("auth", agent_dir if options.get("agentDir") else None)))
    settings_manager = options.get("settingsManager") or SettingsManager.create(cwd, agent_dir)
    applyHttpProxySettings(settings_manager.getGlobalSettings().get("httpProxy"))
    model_registry = options.get("modelRegistry") or ModelRegistry.create(
        auth_storage,
        os.path.join(agent_dir, "models.json"),
    )
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
            model_registry.registerProvider(registration.name, registration.config)
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
            model_registry.registerNativeProvider(registration.provider)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(
                AgentSessionRuntimeDiagnostic(
                    type="error",
                    message=f'Extension "{registration.extensionPath}" error: {error}',
                )
            )
    extensions_result.runtime.pendingNativeProviderRegistrations = []

    refresh_result = await model_registry.refresh({"allowNetwork": False})
    for provider_id, error in refresh_result.errors.items():
        diagnostics.append(
            AgentSessionRuntimeDiagnostic(
                type="warning",
                message=f'Could not restore model catalog for "{provider_id}": {error}',
            )
        )
    diagnostics.extend(apply_extension_flag_values(resource_loader, options.get("extensionFlagValues")))

    return AgentSessionServices(
        cwd=cwd,
        agentDir=agent_dir,
        authStorage=auth_storage,
        settingsManager=settings_manager,
        modelRegistry=model_registry,
        resourceLoader=resource_loader,
        diagnostics=diagnostics,
    )


async def create_agent_session_from_services(
    options: CreateAgentSessionFromServicesOptions,
) -> CreateAgentSessionResult:
    services = options["services"]
    return await create_agent_session(
        {
            "cwd": services.cwd,
            "agentDir": services.agentDir,
            "authStorage": services.authStorage,
            "settingsManager": services.settingsManager,
            "modelRegistry": services.modelRegistry,
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
createAgentSessionFromServices = create_agent_session_from_services
createAgentSessionServices = create_agent_session_services

__all__ = [
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionServicesOptions",
    "createAgentSessionFromServices",
    "createAgentSessionServices",
]
