"""CLI front door for package-backed configuration commands."""

from __future__ import annotations

import asyncio
import os
import signal as signal_module
import sys
import traceback
from dataclasses import dataclass
from typing import Literal

from misaka.cli.config_selector import select_config
from misaka.config import (
    APP_NAME,
    PACKAGE_NAME,
    VERSION,
    SelfUpdateCommand,
    get_agent_dir,
    get_package_dir,
    get_self_update_command,
    get_self_update_unavailable_instruction,
)
from misaka.core.package_manager import DefaultPackageManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.child_process import spawn_process
from misaka.utils.version_check import get_latest_harn_release, is_newer_package_version
from misaka.utils.windows_self_update import (
    cleanup_windows_self_update_quarantine,
    quarantine_windows_native_dependencies,
)


PackageCommand = Literal["install", "remove", "update", "list"]
UpdateTargetType = Literal["all", "self", "extensions"]


@dataclass(slots=True)
class UpdateTarget:
    type: UpdateTargetType
    source: str | None = None


@dataclass(slots=True)
class PackageCommandOptions:
    command: PackageCommand
    source: str | None = None
    updateTarget: UpdateTarget | None = None
    local: bool = False
    force: bool = False
    help: bool = False
    invalidOption: str | None = None
    invalidArgument: str | None = None
    missingOptionValue: str | None = None
    conflictingOptions: str | None = None


@dataclass(slots=True)
class _SelfUpdatePlan:
    packageName: str
    shouldRun: bool
    note: str | None = None


_command_exit_code = 0


def _set_command_exit_code(code: int) -> None:
    global _command_exit_code
    _command_exit_code = code


def _take_command_exit_code() -> int:
    global _command_exit_code
    code = _command_exit_code
    _command_exit_code = 0
    return code


def _get_package_command_usage(command: PackageCommand) -> str:
    match command:
        case "install":
            return f"{APP_NAME} install <source> [-l]"
        case "remove":
            return f"{APP_NAME} remove <source> [-l]"
        case "update":
            return f"{APP_NAME} update [source|self|harn] [--self] [--extensions] [--extension <source>] [--force]"
        case "list":
            return f"{APP_NAME} list"


def _update_target_includes_self(target: UpdateTarget) -> bool:
    return target.type in {"all", "self"}


def _update_target_includes_extensions(target: UpdateTarget) -> bool:
    return target.type in {"all", "extensions"}


def _report_settings_errors(settings_manager: SettingsManager, context: str) -> None:
    for settings_error in settings_manager.drainErrors():
        error = settings_error.error
        print(
            f"Warning ({context}, {settings_error.scope} settings): {error}",
            file=sys.stderr,
        )
        if error.__traceback__ is not None:
            print(
                "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip("\n"),
                file=sys.stderr,
            )


def _print_self_update_unavailable(
    python_command: list[str] | None = None,
    update_package_name: str = PACKAGE_NAME,
) -> None:
    print(f"error: {APP_NAME} cannot self-update this installation.", file=sys.stderr)
    print(
        get_self_update_unavailable_instruction(PACKAGE_NAME, python_command, update_package_name),
        file=sys.stderr,
    )
    entrypoint = sys.argv[0] if sys.argv else None
    if entrypoint:
        print("", file=sys.stderr)
        print(f"Location of {APP_NAME} executable: {entrypoint}", file=sys.stderr)


def _print_self_update_fallback(command: SelfUpdateCommand) -> None:
    print(f"If this keeps failing, run this command yourself: {command.display}", file=sys.stderr)


def _print_self_update_note(note: str) -> None:
    trimmed = note.strip()
    if not trimmed:
        return
    print()
    print("Update note")
    print(trimmed)
    print()


async def _get_self_update_plan(force: bool) -> _SelfUpdatePlan:
    if force:
        return _SelfUpdatePlan(packageName=PACKAGE_NAME, shouldRun=True)

    try:
        latest_release = await get_latest_harn_release(VERSION)
        package_name = latest_release.packageName if latest_release and latest_release.packageName else PACKAGE_NAME
        if (
            latest_release is None
            or package_name != PACKAGE_NAME
            or is_newer_package_version(latest_release.version, VERSION)
        ):
            return _SelfUpdatePlan(
                packageName=package_name,
                shouldRun=True,
                note=latest_release.note if latest_release else None,
            )
    except Exception:  # noqa: BLE001
        return _SelfUpdatePlan(packageName=PACKAGE_NAME, shouldRun=True)

    print(f"{APP_NAME} is already up to date (v{VERSION})")
    return _SelfUpdatePlan(packageName=PACKAGE_NAME, shouldRun=False)


async def _run_self_update(command: SelfUpdateCommand) -> None:
    print(f"Updating {APP_NAME} with {command.display}...")
    for step in command.steps or (command,):
        try:
            child = spawn_process(
                step.command,
                step.args,
                stdio=("ignore", "inherit", "inherit"),
            )
        except OSError as error:
            raise RuntimeError(str(error)) from error
        code = await asyncio.to_thread(child.wait)
        if code == 0:
            continue
        if code is not None and code < 0:
            signal_number = -code
            try:
                signal_name = signal_module.Signals(signal_number).name
            except ValueError:
                signal_name = str(signal_number)
            raise RuntimeError(f"{step.display} terminated by signal {signal_name}")
        raise RuntimeError(f"{step.display} exited with code {code if code is not None else 'unknown'}")


def _prepare_windows_self_update() -> None:
    if sys.platform != "win32":
        return
    package_dir = get_package_dir()
    cleanup_windows_self_update_quarantine(package_dir)
    quarantine_windows_native_dependencies(package_dir)


def _parse_package_command(args: list[str]) -> PackageCommandOptions | None:
    if not args:
        return None

    raw_command = args[0]
    command: PackageCommand | None = None
    if raw_command == "uninstall":
        command = "remove"
    elif raw_command in {"install", "remove", "update", "list"}:
        command = raw_command
    if command is None:
        return None

    options = PackageCommandOptions(command=command)
    remainder = args[1:]
    self_flag = False
    extensions_flag = False
    extension_flag_source: str | None = None
    index = 0
    while index < len(remainder):
        arg = remainder[index]
        if arg in {"-h", "--help"}:
            options.help = True
        elif arg in {"-l", "--local"}:
            if command in {"install", "remove"}:
                options.local = True
            elif options.invalidOption is None:
                options.invalidOption = arg
        elif arg == "--self":
            if command == "update":
                self_flag = True
            elif options.invalidOption is None:
                options.invalidOption = arg
        elif arg == "--extensions":
            if command == "update":
                extensions_flag = True
            elif options.invalidOption is None:
                options.invalidOption = arg
        elif arg == "--force":
            if command == "update":
                options.force = True
            elif options.invalidOption is None:
                options.invalidOption = arg
        elif arg == "--extension":
            if command != "update":
                if options.invalidOption is None:
                    options.invalidOption = arg
            else:
                value = remainder[index + 1] if index + 1 < len(remainder) else None
                if value is None or value.startswith("-"):
                    if options.missingOptionValue is None:
                        options.missingOptionValue = arg
                elif extension_flag_source is not None:
                    if options.conflictingOptions is None:
                        options.conflictingOptions = "--extension can only be provided once"
                    index += 1
                else:
                    extension_flag_source = value
                    index += 1
        elif arg.startswith("-"):
            if options.invalidOption is None:
                options.invalidOption = arg
        elif options.source is None:
            options.source = arg
        elif options.invalidArgument is None:
            options.invalidArgument = arg
        index += 1

    if command == "update":
        if extension_flag_source is not None:
            if self_flag or extensions_flag:
                options.conflictingOptions = (
                    options.conflictingOptions
                    or "--extension cannot be combined with --self or --extensions"
                )
            if options.source:
                options.conflictingOptions = (
                    options.conflictingOptions
                    or "--extension cannot be combined with a positional source"
                )
            options.updateTarget = UpdateTarget(type="extensions", source=extension_flag_source)
        elif options.source:
            source_is_self = options.source in {"self", "harn"}
            if source_is_self:
                options.updateTarget = UpdateTarget(type="all" if extensions_flag else "self")
            else:
                if extensions_flag or self_flag:
                    options.conflictingOptions = (
                        options.conflictingOptions
                        or "positional update targets cannot be combined with --self or --extensions"
                    )
                options.updateTarget = UpdateTarget(type="extensions", source=options.source)
        elif self_flag and extensions_flag:
            options.updateTarget = UpdateTarget(type="all")
        elif self_flag:
            options.updateTarget = UpdateTarget(type="self")
        elif extensions_flag:
            options.updateTarget = UpdateTarget(type="extensions")
        else:
            options.updateTarget = UpdateTarget(type="all")

    return options


def _print_package_command_help(command: PackageCommand) -> None:
    if command == "install":
        print(
            f"Usage:\n"
            f"  {APP_NAME} install <source> [-l]\n\n"
            "Install a package and add it to settings.\n\n"
            "Options:\n"
            "  -l, --local    Install project-locally (.misaka/settings.json)\n\n"
            "Examples:\n"
            f"  {APP_NAME} install npm:@foo/bar\n"
            f"  {APP_NAME} install git:github.com/user/repo\n"
            f"  {APP_NAME} install git:git@github.com:user/repo\n"
            f"  {APP_NAME} install https://github.com/user/repo\n"
            f"  {APP_NAME} install ssh://git@github.com/user/repo\n"
            f"  {APP_NAME} install ./local/path\n"
        )
        return
    if command == "remove":
        print(
            f"Usage:\n"
            f"  {APP_NAME} remove <source> [-l]\n\n"
            "Remove a package and its source from settings.\n"
            f"Alias: {APP_NAME} uninstall <source> [-l]\n\n"
            "Options:\n"
            "  -l, --local    Remove from project settings (.misaka/settings.json)\n\n"
            "Examples:\n"
            f"  {APP_NAME} remove npm:@foo/bar\n"
            f"  {APP_NAME} uninstall npm:@foo/bar\n"
        )
        return
    if command == "update":
        print(
            f"Usage:\n"
            f"  {APP_NAME} update [source|self|harn] [--self] [--extensions] [--extension <source>] [--force]\n\n"
            f"Update {APP_NAME} and installed packages.\n\n"
            "Options:\n"
            f"  --self                  Update {APP_NAME} only\n"
            "  --extensions            Update installed packages only\n"
            "  --extension <source>    Update one package only\n"
            f"  --force                 Reinstall {APP_NAME} even if the current version is latest\n\n"
            "Short forms:\n"
            f"  {APP_NAME} update                Update {APP_NAME} and all extensions\n"
            f"  {APP_NAME} update <source>       Update one package\n"
            f"  {APP_NAME} update harn         Update {APP_NAME} only (self works as alias to harn)\n"
        )
        return
    print(
        f"Usage:\n"
        f"  {APP_NAME} list\n\n"
        "List installed packages from user and project settings.\n"
    )


async def handle_config_command(args: list[str]) -> bool | None:
    if not args or args[0] != "config":
        return None
    _set_command_exit_code(0)

    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    settings_manager = SettingsManager.create(cwd, agent_dir)
    _report_settings_errors(settings_manager, "config command")
    package_manager = DefaultPackageManager(
        {
            "cwd": cwd,
            "agentDir": agent_dir,
            "settingsManager": settings_manager,
        }
    )
    resolved_paths = await package_manager.resolve()
    # select_config 按属性取值（TS 对象字面量的移植语义）——传 dict 必 AttributeError
    #（审查 2026-08-20：config 子命令必崩的镜像翻译漏）
    from types import SimpleNamespace
    await select_config(
        SimpleNamespace(
            resolvedPaths=resolved_paths,
            settingsManager=settings_manager,
            cwd=cwd,
            agentDir=agent_dir,
        )
    )
    return True


async def handle_package_command(args: list[str]) -> bool | None:
    parsed = _parse_package_command(args)
    if parsed is None:
        return None
    _set_command_exit_code(0)

    if parsed.help:
        _print_package_command_help(parsed.command)
        return True

    if parsed.invalidOption:
        print(f'Unknown option {parsed.invalidOption} for "{parsed.command}".', file=sys.stderr)
        print(f'Use "{APP_NAME} --help" or "{_get_package_command_usage(parsed.command)}".', file=sys.stderr)
        _set_command_exit_code(1)
        return True

    if parsed.missingOptionValue:
        print(f"Missing value for {parsed.missingOptionValue}.", file=sys.stderr)
        print(f"Usage: {_get_package_command_usage(parsed.command)}", file=sys.stderr)
        _set_command_exit_code(1)
        return True

    if parsed.invalidArgument:
        print(f"Unexpected argument {parsed.invalidArgument}.", file=sys.stderr)
        print(f"Usage: {_get_package_command_usage(parsed.command)}", file=sys.stderr)
        _set_command_exit_code(1)
        return True

    if parsed.conflictingOptions:
        print(parsed.conflictingOptions, file=sys.stderr)
        print(f"Usage: {_get_package_command_usage(parsed.command)}", file=sys.stderr)
        _set_command_exit_code(1)
        return True

    if parsed.command in {"install", "remove"} and not parsed.source:
        print(f"Missing {parsed.command} source.", file=sys.stderr)
        print(f"Usage: {_get_package_command_usage(parsed.command)}", file=sys.stderr)
        _set_command_exit_code(1)
        return True

    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    settings_manager = SettingsManager.create(cwd, agent_dir)
    _report_settings_errors(settings_manager, "package command")
    self_update_python_command = settings_manager.getNpmCommand()
    package_manager = DefaultPackageManager(
        {
            "cwd": cwd,
            "agentDir": agent_dir,
            "settingsManager": settings_manager,
        }
    )
    set_progress_callback = getattr(package_manager, "setProgressCallback", None)
    if callable(set_progress_callback):
        set_progress_callback(
            lambda event: (
                print(event.message)
                if getattr(event, "type", None) == "start" and getattr(event, "message", None)
                else None
            )
        )

    try:
        if parsed.command == "list":
            configured_packages = package_manager.listConfiguredPackages()
            user_packages = [package for package in configured_packages if package.scope == "user"]
            project_packages = [package for package in configured_packages if package.scope == "project"]

            if not configured_packages:
                print("No packages installed.")
                return True

            def format_package(package: object) -> None:
                display = f"{package.source} (filtered)" if package.filtered else package.source
                print(f"  {display}")
                if package.installedPath:
                    print(f"    {package.installedPath}")

            if user_packages:
                print("User packages:")
                for package in user_packages:
                    format_package(package)

            if project_packages:
                if user_packages:
                    print()
                print("Project packages:")
                for package in project_packages:
                    format_package(package)
            return True

        if parsed.command == "install":
            await package_manager.installAndPersist(parsed.source, {"local": parsed.local})
            print(f"Installed {parsed.source}")
            return True

        if parsed.command == "remove":
            removed = await package_manager.removeAndPersist(parsed.source, {"local": parsed.local})
            if not removed:
                print(f"No matching package found for {parsed.source}", file=sys.stderr)
                _set_command_exit_code(1)
                return True
            print(f"Removed {parsed.source}")
            return True

        if parsed.command == "update":
            target = parsed.updateTarget or UpdateTarget(type="all")
            if _update_target_includes_extensions(target):
                extension_target = target.source if target.type == "extensions" else None
                await package_manager.update(extension_target)
                if extension_target:
                    print(f"Updated {extension_target}")
                else:
                    print("Updated packages")
            if _update_target_includes_self(target):
                self_update_plan = await _get_self_update_plan(parsed.force)
                if not self_update_plan.shouldRun:
                    return True
                self_update_command = get_self_update_command(
                    PACKAGE_NAME,
                    self_update_python_command,
                    self_update_plan.packageName,
                )
                if self_update_command is None:
                    _print_self_update_unavailable(self_update_python_command, self_update_plan.packageName)
                    _set_command_exit_code(1)
                    return True
                if self_update_plan.note:
                    _print_self_update_note(self_update_plan.note)
                try:
                    _prepare_windows_self_update()
                    await _run_self_update(self_update_command)
                except Exception as error:  # noqa: BLE001
                    print(f"Error: {error}", file=sys.stderr)
                    _print_self_update_fallback(self_update_command)
                    _set_command_exit_code(1)
                    return True
                print(f"Updated {APP_NAME}")
            return True
    except Exception as error:  # noqa: BLE001
        print(f"Error: {error}", file=sys.stderr)
        _set_command_exit_code(1)
        return True

    return True


handleConfigCommand = handle_config_command
handlePackageCommand = handle_package_command

__all__ = [
    "PackageCommand",
    "handleConfigCommand",
    "handlePackageCommand",
]
