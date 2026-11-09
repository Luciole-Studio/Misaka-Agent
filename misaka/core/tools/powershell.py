"""Optional PowerShell tool backed by the shared shell implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from misaka.agent.types import AgentTool
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.bash import (
    BashOperations,
    BashSpawnContext,
    BashSpawnHook,
    BashToolDetails,
    BashToolInput,
    BashToolOptions,
    ShellToolConfig,
    createLocalShellOperations,
    createShellToolDefinition,
)
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.utils.shell import getPowerShellConfig

UTF8_OUTPUT_PREFIX = (
    "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n"
)
_SESSION_ENV_GUIDELINE = (
    "You can inspect PI_* environment variables for current model and session details."
)

powershellToolSystemPromptContribution = {
    "snippet": "Execute PowerShell commands",
    "guidelines": [_SESSION_ENV_GUIDELINE],
}

PowerShellOperations = BashOperations
PowerShellSpawnContext = BashSpawnContext
PowerShellSpawnHook = BashSpawnHook
PowerShellToolDetails = BashToolDetails
PowerShellToolInput = BashToolInput


@dataclass(slots=True)
class PowerShellToolOptions:
    operations: PowerShellOperations | None = None
    exposeSessionEnvironment: bool | None = True
    spawnHook: PowerShellSpawnHook | None = None


@dataclass(slots=True)
class _LocalPowerShellOperations:
    operations: BashOperations

    async def exec(
        self,
        command: str,
        cwd: str,
        options: Any,
    ) -> dict[str, int | None]:
        return await self.operations.exec(
            f"{UTF8_OUTPUT_PREFIX}{command}",
            cwd,
            options,
        )


def createLocalPowerShellOperations() -> PowerShellOperations:
    return _LocalPowerShellOperations(
        createLocalShellOperations("PowerShell", getPowerShellConfig)
    )


_POWERSHELL_TOOL_CONFIG = ShellToolConfig(
    name="powershell",
    label="powershell",
    shellName="PowerShell",
    prompt="PS>",
    promptSnippet=powershellToolSystemPromptContribution["snippet"],
    promptGuidelines=tuple(
        powershellToolSystemPromptContribution["guidelines"]
    ),
    # MISAKA fork: output files are named after misaka (upstream: `pi-powershell`).
    tempFilePrefix="misaka-powershell",
)


def _coerce_options(
    options: PowerShellToolOptions | Mapping[str, Any] | None,
) -> BashToolOptions:
    if options is None:
        resolved = PowerShellToolOptions()
    elif isinstance(options, PowerShellToolOptions):
        resolved = options
    else:
        expose_session_environment = options.get("exposeSessionEnvironment")
        resolved = PowerShellToolOptions(
            operations=options.get("operations"),
            exposeSessionEnvironment=(
                True
                if expose_session_environment is None
                else expose_session_environment
            ),
            spawnHook=options.get("spawnHook"),
        )
    operations = resolved.operations
    if operations is None:
        operations = createLocalPowerShellOperations()
    return BashToolOptions(
        operations=operations,
        exposeSessionEnvironment=resolved.exposeSessionEnvironment,
        spawnHook=resolved.spawnHook,
    )


def createPowerShellToolDefinition(
    cwd: str,
    options: PowerShellToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], PowerShellToolDetails | None]:
    return createShellToolDefinition(
        cwd,
        _POWERSHELL_TOOL_CONFIG,
        _coerce_options(options),
    )


def createPowerShellTool(
    cwd: str,
    options: PowerShellToolOptions | Mapping[str, Any] | None = None,
) -> AgentTool:
    definition = createPowerShellToolDefinition(cwd, options)
    tool = wrapToolDefinition(definition)
    object.__setattr__(tool, "promptSnippet", definition.promptSnippet)
    object.__setattr__(tool, "promptGuidelines", definition.promptGuidelines)
    return tool



__all__ = [
    "PowerShellOperations",
    "PowerShellSpawnContext",
    "PowerShellSpawnHook",
    "PowerShellToolDetails",
    "PowerShellToolInput",
    "PowerShellToolOptions",
    "createLocalPowerShellOperations",
    "createPowerShellTool",
    "createPowerShellToolDefinition",
]
