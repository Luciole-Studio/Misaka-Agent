"""Translated from pi's ``coding-agent/src/cli/project-trust.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from typing import Any, TypedDict

from misaka.core.extensions.types import ProjectTrustContext
from misaka.core.project_trust import AppMode
from misaka.core.settings_manager import SettingsManager
from misaka.utils.chalk import chalk

from .startup_ui import StartupOption, showStartupInput, showStartupSelector


class CreateProjectTrustContextOptions(TypedDict):
    cwd: str
    mode: AppMode
    settingsManager: SettingsManager
    hasUI: bool


def createProjectTrustContext(options: CreateProjectTrustContextOptions) -> ProjectTrustContext:
    async def select(title: str, selectOptions: list[str], _opts: Any = None) -> str | None:
        if not options["hasUI"]:
            return None
        if options["mode"] != "interactive":
            return None
        return await showStartupSelector(
            options["settingsManager"], title, [StartupOption(label=option, value=option) for option in selectOptions]
        )

    async def confirm(title: str, message: str, _opts: Any = None) -> bool:
        if not options["hasUI"]:
            return False
        if options["mode"] != "interactive":
            return False
        result = await showStartupSelector(
            options["settingsManager"],
            f"{title}\n{message}",
            [StartupOption(label="Yes", value=True), StartupOption(label="No", value=False)],
        )
        return result if result is not None else False

    async def input(title: str, placeholder: str | None = None, _opts: Any = None) -> str | None:
        if not options["hasUI"]:
            return None
        if options["mode"] != "interactive":
            return None
        return await showStartupInput(options["settingsManager"], title, placeholder)

    def notify(message: str, type: str = "info") -> None:
        if options["mode"] != "interactive":
            color = chalk.red if type == "error" else chalk.yellow if type == "warning" else chalk.cyan
            print(color(message), file=sys.stderr)

    return {
        "cwd": options["cwd"],
        "mode": "tui" if options["mode"] == "interactive" else options["mode"],  # type: ignore[typeddict-item]
        "hasUI": options["hasUI"],
        "ui": {"select": select, "confirm": confirm, "input": input, "notify": notify},
    }


__all__ = ["createProjectTrustContext"]
