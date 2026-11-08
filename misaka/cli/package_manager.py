"""Translated from pi's ``coding-agent/src/package-manager-cli.ts`` (v1.5.0): the ``config`` command only.

MISAKA forks:
- Only ``config`` and the helpers it uses (``createCommandSettingsManager`` without ``useSavedProjectTrustOnly``,
  ``reportProjectTrustWarnings``, ``reportSettingsErrors``, ``getCommandAppMode``). The package commands (``install``, ``remove``, ``update``,
  ``list``) and the self-update are not ported: MISAKA has no packages, and ``misaka update`` updates MISAKA.
- The built-in extensions come from ``extensions.builtin_names()``, where upstream filters the CLI's
  extension factories; and no extension factories are passed on, so only user and CLI extensions see
  ``project_trust``.
- No package install warnings: ``DefaultPackageManager.resolve`` installs nothing.
- ``handleConfigCommand`` returns the exit code (``None`` when the arguments are not ``config``), where upstream
  returns whether it handled them, sets ``process.exitCode`` and exits.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass

from misaka import extensions
from misaka.config import APP_NAME, get_agent_dir, home
from misaka.core.package_manager import DefaultPackageManager
from misaka.core.project_trust import AppMode, resolveProjectTrusted
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.settings_manager import SettingsManager
from misaka.core.trust_manager import (
    ProjectTrustStore,
    hasTrustRequiringProjectResources,
)
from misaka.utils.chalk import chalk
from misaka.utils.values import read_field

from .config_selector import ConfigSelectorOptions, selectConfig
from .project_trust import createProjectTrustContext

CONFIG_COMMAND_USAGE = f"{APP_NAME} config [-l] [--approve|--no-approve]"


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def reportSettingsErrors(settingsManager: SettingsManager, context: str) -> None:
    errors = settingsManager.drainErrors()
    for settingsError in errors:
        _error(chalk.yellow(f"Warning ({context}, {settingsError.scope} settings): {settingsError.error}"))
        if settingsError.error.__traceback__ is not None:
            _error(chalk.dim("".join(traceback.format_exception(settingsError.error)).rstrip()))


def printConfigCommandHelp() -> None:
    # MISAKA fork: the home's settings.json, where upstream names `~/.pi/agent/settings.json`.
    print(f"""{chalk.bold("Usage:")}
  {CONFIG_COMMAND_USAGE}

Open the resource configuration TUI to enable or disable package resources.
Without -l, starts in global settings ({home.display(home.path("settings"))}).
Press Tab in the TUI to switch between global and project-local modes.

Options:
  -l, --local       Edit project overrides ({home.DIR_NAME}/settings.json)
  -a, --approve     Trust project-local files for this command with -l
  -na, --no-approve Ignore project-local files for this command with -l
""")


@dataclass(slots=True)
class CommandSettingsResult:
    settingsManager: SettingsManager
    projectTrustWarnings: list[str]


def getCommandAppMode() -> AppMode:
    return "interactive" if sys.stdin.isatty() and sys.stdout.isatty() else "print"


def reportProjectTrustWarnings(warnings: list[str]) -> None:
    for warning in warnings:
        _error(chalk.yellow(f"Warning: {warning}"))


async def createCommandSettingsManager(
    *,
    cwd: str,
    agentDir: str,
    projectTrustOverride: bool | None = None,
) -> CommandSettingsResult:
    settingsManager = SettingsManager.create(cwd, agentDir, {"projectTrusted": False})
    projectTrustWarnings: list[str] = []
    trustStore = ProjectTrustStore(agentDir)

    appMode = getCommandAppMode()
    extensionsResult = (
        await DefaultResourceLoader(
            {"cwd": cwd, "agentDir": agentDir, "settingsManager": settingsManager}
        ).loadProjectTrustExtensions()
        if projectTrustOverride is None and hasTrustRequiringProjectResources(cwd)
        else None
    )
    for error in (extensionsResult.errors if extensionsResult is not None else []):
        projectTrustWarnings.append(f'Failed to load extension "{read_field(error, "path")}": {read_field(error, "error")}')

    options = {
        "cwd": cwd,
        "trustStore": trustStore,
        "defaultProjectTrust": settingsManager.getDefaultProjectTrust(),
        "extensionsResult": extensionsResult,
        "projectTrustContext": createProjectTrustContext({
            "cwd": cwd,
            "mode": appMode,
            "settingsManager": settingsManager,
            "hasUI": appMode == "interactive",
        }),
        "onExtensionError": projectTrustWarnings.append,
    }
    if projectTrustOverride is not None:
        options["trustOverride"] = projectTrustOverride
    projectTrusted = await resolveProjectTrusted(options)  # type: ignore[arg-type]
    settingsManager.setProjectTrusted(projectTrusted)
    return CommandSettingsResult(settingsManager, projectTrustWarnings)


async def handleConfigCommand(args: list[str]) -> int | None:
    if not args or args[0] != "config":
        return None
    rest = args[1:]

    if "-h" in rest or "--help" in rest:
        printConfigCommandHelp()
        return 0

    local = False
    projectTrustOverride: bool | None = None
    for arg in rest:
        if arg in ("-l", "--local"):
            local = True
        elif arg in ("-a", "--approve"):
            projectTrustOverride = True
        elif arg in ("-na", "--no-approve"):
            projectTrustOverride = False
        elif arg.startswith("-"):
            _error(chalk.red(f'Unknown option {arg} for "config".'))
            _error(chalk.dim(f'Use "{APP_NAME} --help" or "{CONFIG_COMMAND_USAGE}".'))
            return 1
        else:
            _error(chalk.red(f"Unexpected argument {arg}."))
            _error(chalk.dim(f"Usage: {CONFIG_COMMAND_USAGE}"))
            return 1

    cwd = os.getcwd()
    agentDir = get_agent_dir()
    result = await createCommandSettingsManager(cwd=cwd, agentDir=agentDir, projectTrustOverride=projectTrustOverride)
    settingsManager = result.settingsManager
    reportProjectTrustWarnings(result.projectTrustWarnings)
    if local and not settingsManager.isProjectTrusted():
        _error(chalk.red("Project is not trusted. Use --approve to modify local resource config."))
        return 1
    reportSettingsErrors(settingsManager, "config command")
    # MISAKA fork: every built-in extension, whatever role loads it (upstream: the CLI's built-in factories).
    builtinExtensions = extensions.builtin_names()
    globalSettingsManager = SettingsManager.create(cwd, agentDir, {"projectTrusted": False})
    globalResolvedPaths = await DefaultPackageManager({
        "cwd": cwd,
        "agentDir": agentDir,
        "settingsManager": globalSettingsManager,
        "builtinExtensions": builtinExtensions,
    }).resolve()
    projectResolvedPaths = (
        await DefaultPackageManager({
            "cwd": cwd,
            "agentDir": agentDir,
            "settingsManager": settingsManager,
            "builtinExtensions": builtinExtensions,
        }).resolve()
        if settingsManager.isProjectTrusted()
        else globalResolvedPaths
    )

    await selectConfig(ConfigSelectorOptions(
        resolvedPaths={"global": globalResolvedPaths, "project": projectResolvedPaths},
        settingsManager=settingsManager,
        cwd=cwd,
        agentDir=agentDir,
        writeScope="project" if local else "global",
        projectModeAvailable=settingsManager.isProjectTrusted(),
    ))
    return 0


__all__ = [
    "CONFIG_COMMAND_USAGE",
    "createCommandSettingsManager",
    "handleConfigCommand",
]
