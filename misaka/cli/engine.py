"""Minimal Phase 9 CLI entry orchestration."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from misaka.ai.models import models_are_equal
from misaka.ai.types import ImageContent
from misaka.cli import session_picker
from misaka.cli.args import Args, parse_args, print_help
from misaka.cli.file_processor import ProcessFileOptions, process_file_arguments
from misaka.cli.initial_message import build_initial_message
from misaka.cli.list_models import list_models
from misaka.config import ENV_SESSION_DIR, VERSION, expand_tilde_path, get_agent_dir
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    create_agent_session_runtime,
)
from misaka.core.agent_session_services import (
    AgentSessionRuntimeDiagnostic,
    create_agent_session_from_services,
    create_agent_session_services,
)
from misaka.core.auth_guidance import formatNoModelsAvailableMessage
from misaka.core.auth_storage import AuthStorage
from misaka.core.export_html import export_from_file
from misaka.core.keybindings import KeybindingsManager
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_resolver import ScopedModel, resolveCliModel, resolveModelScope
from misaka.core.output_guard import isStdoutTakenOver, restoreStdout, takeOverStdout
from misaka.core.session_cwd import (
    MissingSessionCwdError,
    SessionCwdIssue,
    format_missing_session_cwd_prompt,
    get_missing_session_cwd_issue,
)
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.modes import runPrintMode as run_print_mode
from misaka.ui.tui import TUI, ProcessTerminal, setKeybindings
from misaka.ui.tui.interactive import InteractiveMode
from misaka.ui.tui.interactive.components.extension_selector import (
    ExtensionSelectorComponent,
)
from misaka.ui.tui.interactive.theme.theme import init_theme, stop_theme_watcher
from misaka.utils.paths import is_local_path, normalize_path, resolve_path

AppMode = Literal["interactive", "print", "json"]
PrintOutputMode = Literal["text", "json"]

_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


@dataclass(slots=True)
class RuntimeDiagnostic:
    type: Literal["warning", "error", "info"]
    message: str


@dataclass(slots=True)
class ResolvedSession:
    type: Literal["path", "local", "global", "not_found"]
    path: str | None = None
    cwd: str | None = None
    arg: str | None = None


@dataclass(slots=True)
class BuildSessionOptionsResult:
    options: dict[str, Any] = field(default_factory=dict)
    cliThinkingFromModel: bool = False
    diagnostics: list[RuntimeDiagnostic] = field(default_factory=list)


class MainOptions(TypedDict, total=False):
    extensionFactories: list[Any]


SelectSessionFn = Callable[
    [Callable[..., Awaitable[list[Any]]], Callable[..., Awaitable[list[Any]]]],
    Awaitable[str | None],
]
ConfirmFn = Callable[[str], Awaitable[bool]]


async def read_piped_stdin() -> str | None:
    if sys.stdin.isatty():
        return None
    content = await asyncio.to_thread(sys.stdin.read)
    return content.strip() or None


def resolve_app_mode(parsed: Args, stdin_is_tty: bool) -> AppMode:
    if parsed.mode == "json":
        return "json"
    if parsed.print or not stdin_is_tty:
        return "print"
    return "interactive"


def to_print_output_mode(app_mode: AppMode) -> PrintOutputMode:
    return "json" if app_mode == "json" else "text"


def collect_settings_diagnostics(settings_manager: SettingsManager, context: str) -> list[RuntimeDiagnostic]:
    return [
        RuntimeDiagnostic(type="warning", message=f"({context}, {error.scope} settings) {error.error}")
        for error in settings_manager.drainErrors()
    ]


def report_diagnostics(
    diagnostics: list[RuntimeDiagnostic],
    *,
    stream: Any | None = None,
) -> None:
    output = stream or sys.stderr
    for diagnostic in diagnostics:
        color = _RED if diagnostic.type == "error" else _YELLOW if diagnostic.type == "warning" else _DIM
        prefix = "Error: " if diagnostic.type == "error" else "Warning: " if diagnostic.type == "warning" else ""
        output.write(f"{color}{prefix}{diagnostic.message}{_RESET}\n")


def _format_colored_message(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


async def prepare_initial_message(
    parsed: Args,
    auto_resize_images: bool,
    stdin_content: str | None = None,
) -> tuple[str | None, list[ImageContent] | None]:
    if not parsed.fileArgs:
        result = build_initial_message(parsed=parsed, stdinContent=stdin_content)
        return result.initialMessage, result.initialImages

    processed = await process_file_arguments(
        parsed.fileArgs,
        options=ProcessFileOptions(autoResizeImages=auto_resize_images),
    )
    result = build_initial_message(
        parsed=parsed,
        fileText=processed.text,
        fileImages=processed.images,
        stdinContent=stdin_content,
    )
    return result.initialMessage, result.initialImages


async def resolve_session_path(session_arg: str, cwd: str, session_dir: str | None = None) -> ResolvedSession:
    if "/" in session_arg or "\\" in session_arg or session_arg.endswith(".jsonl"):
        return ResolvedSession(type="path", path=resolve_path(session_arg, cwd))

    local_sessions = await SessionManager.list(cwd, session_dir)
    local_matches = [session for session in local_sessions if session.id.startswith(session_arg)]
    if local_matches:
        return ResolvedSession(type="local", path=local_matches[0].path)

    global_sessions = await SessionManager.listAll()
    global_matches = [session for session in global_sessions if session.id.startswith(session_arg)]
    if global_matches:
        match = global_matches[0]
        return ResolvedSession(type="global", path=match.path, cwd=match.cwd)

    return ResolvedSession(type="not_found", arg=session_arg)


async def prompt_confirm(message: str, *, input_stream: Any | None = None, output_stream: Any | None = None) -> bool:
    input_handle = input_stream or sys.stdin
    output_handle = output_stream or sys.stdout
    output_handle.write(f"{message} [y/N] ")
    flush = getattr(output_handle, "flush", None)
    if callable(flush):
        flush()
    answer = await asyncio.to_thread(input_handle.readline)
    return answer.strip().lower() in {"y", "yes"}


async def prompt_for_missing_session_cwd(
    issue: SessionCwdIssue,
    settings_manager: SettingsManager,
    *,
    terminal_factory: type[ProcessTerminal] = ProcessTerminal,
    ui_factory: type[TUI] = TUI,
    component_factory: type[ExtensionSelectorComponent] = ExtensionSelectorComponent,
    keybindings_factory: Callable[[], KeybindingsManager] = KeybindingsManager.create,
    set_keybindings_fn: Callable[[KeybindingsManager], None] = setKeybindings,
) -> str | None:
    init_theme(settings_manager.getTheme())
    keybindings = keybindings_factory()
    set_keybindings_fn(keybindings)
    try:
        ui = ui_factory(
            terminal_factory(),
            bool(getattr(settings_manager, "getShowHardwareCursor", lambda: False)()),
        )
    except TypeError:
        ui = ui_factory(terminal_factory())
    if hasattr(ui, "setClearOnShrink"):
        ui.setClearOnShrink(bool(getattr(settings_manager, "getClearOnShrink", lambda: False)()))

    done: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    closed = False

    def finish(result: str | None) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        ui.stop()
        done.set_result(result)

    selector = component_factory(
        format_missing_session_cwd_prompt(issue),
        ["Continue", "Cancel"],
        lambda option: finish(issue.fallbackCwd if option == "Continue" else None),
        lambda: finish(None),
        {"tui": ui},
    )
    ui.addChild(selector)
    ui.setFocus(selector)
    try:
        ui.start()
        return await done
    finally:
        if not closed:
            closed = True
            ui.stop()


def validate_fork_flags(parsed: Args) -> None:
    if not parsed.fork:
        return
    conflicting_flags = [
        "--session" if parsed.session else None,
        "--continue" if parsed.continue_ else None,
        "--resume" if parsed.resume else None,
        "--no-session" if parsed.noSession else None,
    ]
    conflicts = [flag for flag in conflicting_flags if flag is not None]
    if conflicts:
        raise ValueError(f"--fork cannot be combined with {', '.join(conflicts)}")


def resolve_cli_paths(cwd: str, paths: list[str] | None) -> list[str] | None:
    if paths is None:
        return None
    return [resolve_path(value, cwd) if is_local_path(value) else value for value in paths]


def build_session_options(
    parsed: Args,
    scoped_models: list[ScopedModel],
    has_existing_session: bool,
    model_registry: ModelRegistry,
    settings_manager: SettingsManager,
) -> BuildSessionOptionsResult:
    options: dict[str, Any] = {}
    diagnostics: list[RuntimeDiagnostic] = []
    cli_thinking_from_model = False

    if parsed.model:
        resolved = resolveCliModel(
            {
                "cliProvider": parsed.provider,
                "cliModel": parsed.model,
                "modelRegistry": model_registry,
            }
        )
        if resolved.warning:
            diagnostics.append(RuntimeDiagnostic(type="warning", message=resolved.warning))
        if resolved.error:
            diagnostics.append(RuntimeDiagnostic(type="error", message=resolved.error))
        if resolved.model is not None:
            options["model"] = resolved.model
            if parsed.thinking is None and resolved.thinkingLevel:
                options["thinkingLevel"] = resolved.thinkingLevel
                cli_thinking_from_model = True

    if "model" not in options and scoped_models and not has_existing_session:
        saved_provider = settings_manager.getDefaultProvider()
        saved_model_id = settings_manager.getDefaultModel()
        saved_model = model_registry.find(saved_provider, saved_model_id) if saved_provider and saved_model_id else None
        saved_in_scope = (
            next(
                (scoped for scoped in scoped_models if saved_model and models_are_equal(scoped.model, saved_model)),
                None,
            )
            if saved_model
            else None
        )
        selected = saved_in_scope or scoped_models[0]
        options["model"] = selected.model
        if parsed.thinking is None and selected.thinkingLevel:
            options["thinkingLevel"] = selected.thinkingLevel

    if parsed.thinking is not None:
        options["thinkingLevel"] = parsed.thinking

    if scoped_models:
        options["scopedModels"] = [
            {"model": scoped_model.model, "thinkingLevel": scoped_model.thinkingLevel}
            for scoped_model in scoped_models
        ]

    if parsed.noTools:
        options["noTools"] = "all"
    elif parsed.noBuiltinTools:
        options["noTools"] = "builtin"
    if parsed.tools:
        options["tools"] = list(parsed.tools)

    return BuildSessionOptionsResult(
        options=options,
        cliThinkingFromModel=cli_thinking_from_model,
        diagnostics=diagnostics,
    )


def _to_agent_runtime_diagnostics(diagnostics: list[RuntimeDiagnostic]) -> list[AgentSessionRuntimeDiagnostic]:
    return [AgentSessionRuntimeDiagnostic(type=item.type, message=item.message) for item in diagnostics]


def create_runtime_factory(
    parsed: Args,
    auth_storage: AuthStorage,
    *,
    resolved_extension_paths: list[str] | None = None,
    resolved_prompt_template_paths: list[str] | None = None,
    resolved_theme_paths: list[str] | None = None,
    extension_factories: list[Any] | None = None,
) -> Callable[[dict[str, Any]], Awaitable[CreateAgentSessionRuntimeResult]]:
    async def _factory(runtime_options: dict[str, Any]) -> CreateAgentSessionRuntimeResult:
        resource_loader_options: dict[str, Any] = {
            "noExtensions": parsed.noExtensions,
            "noPromptTemplates": parsed.noPromptTemplates,
            "noThemes": parsed.noThemes,
            "noContextFiles": parsed.noContextFiles,
            "systemPrompt": parsed.systemPrompt,
            "appendSystemPrompt": parsed.appendSystemPrompt,
        }
        if resolved_extension_paths is not None:
            resource_loader_options["additionalExtensionPaths"] = resolved_extension_paths
        if resolved_prompt_template_paths is not None:
            resource_loader_options["additionalPromptTemplatePaths"] = resolved_prompt_template_paths
        if resolved_theme_paths is not None:
            resource_loader_options["additionalThemePaths"] = resolved_theme_paths
        if extension_factories is not None:
            resource_loader_options["extensionFactories"] = extension_factories

        services = await create_agent_session_services(
            {
                "cwd": runtime_options["cwd"],
                "agentDir": runtime_options["agentDir"],
                "authStorage": auth_storage,
                "extensionFlagValues": parsed.unknownFlags,
                "resourceLoaderOptions": resource_loader_options,
            }
        )
        settings_manager = services.settingsManager
        if parsed.useTheme is not None:   # Override for this run only; never written back to settings (pi #7722).
            settings_manager.applyOverrides({"theme": parsed.useTheme})
        model_registry = services.modelRegistry
        resource_loader = services.resourceLoader

        diagnostics: list[AgentSessionRuntimeDiagnostic] = [
            *services.diagnostics,
            *_to_agent_runtime_diagnostics(collect_settings_diagnostics(settings_manager, "runtime creation")),
        ]
        for item in resource_loader.getExtensions().errors:
            path = item.get("path", "") if isinstance(item, dict) else getattr(item, "path", "")
            error = item.get("error", "") if isinstance(item, dict) else getattr(item, "error", "")
            diagnostics.append(
                AgentSessionRuntimeDiagnostic(type="error", message=f'Failed to load extension "{path}": {error}')
            )

        model_patterns = parsed.models or settings_manager.getEnabledModels()
        scoped_models = (
            await resolveModelScope(model_patterns, model_registry)
            if model_patterns and len(model_patterns) > 0
            else []
        )
        session_options = build_session_options(
            parsed,
            scoped_models,
            len(runtime_options["sessionManager"].buildSessionContext().messages) > 0,
            model_registry,
            settings_manager,
        )
        diagnostics.extend(_to_agent_runtime_diagnostics(session_options.diagnostics))

        if parsed.apiKey:
            selected_model = session_options.options.get("model")
            if selected_model is None:
                diagnostics.append(
                    AgentSessionRuntimeDiagnostic(
                        type="error",
                        message=(
                            "--api-key requires a model to be specified via --model, "
                            "--provider/--model, or --models"
                        ),
                    )
                )
            else:
                auth_storage.setRuntimeApiKey(selected_model.provider, parsed.apiKey)

        created = await create_agent_session_from_services(
            {
                "services": services,
                "sessionManager": runtime_options["sessionManager"],
                "sessionStartEvent": runtime_options.get("sessionStartEvent"),
                "model": session_options.options.get("model"),
                "thinkingLevel": session_options.options.get("thinkingLevel"),
                "scopedModels": session_options.options.get("scopedModels"),
                "tools": session_options.options.get("tools"),
                "noTools": session_options.options.get("noTools"),
                "customTools": session_options.options.get("customTools"),
            }
        )
        session = created["session"] if isinstance(created, dict) else created.session
        if session.model and (parsed.thinking is not None or session_options.cliThinkingFromModel):
            session.setThinkingLevel(session.thinkingLevel)

        return CreateAgentSessionRuntimeResult(
            session=session,
            services=services,
            diagnostics=diagnostics,
            extensionsResult=created.get("extensionsResult") if isinstance(created, dict) else created.extensionsResult,
            modelFallbackMessage=(
                created.get("modelFallbackMessage") if isinstance(created, dict) else created.modelFallbackMessage
            ),
        )

    return _factory


async def create_session_manager(
    parsed: Args,
    cwd: str,
    session_dir: str | None,
    settings_manager: SettingsManager,
    *,
    prompt_confirm_fn: ConfirmFn = prompt_confirm,
    select_session_fn: SelectSessionFn | None = None,
    output_stream: Any | None = None,
    error_stream: Any | None = None,
) -> SessionManager:
    out = output_stream or sys.stdout
    err = error_stream or sys.stderr
    selector = select_session_fn or session_picker.select_session

    def fork_session_or_exit(source_path: str) -> SessionManager:
        try:
            return SessionManager.forkFrom(source_path, cwd, session_dir)
        except Exception as error:
            err.write(_format_colored_message(f"Error: {error}", _RED) + "\n")
            raise SystemExit(1) from error

    if parsed.noSession:
        return SessionManager.inMemory()

    if parsed.fork:
        resolved = await resolve_session_path(parsed.fork, cwd, session_dir)
        if resolved.type in {"path", "local", "global"} and resolved.path:
            return fork_session_or_exit(resolved.path)
        err.write(_format_colored_message(f"No session found matching '{resolved.arg}'", _RED) + "\n")
        raise SystemExit(1)

    if parsed.session:
        resolved = await resolve_session_path(parsed.session, cwd, session_dir)
        if resolved.type in {"path", "local"} and resolved.path:
            return SessionManager.open(resolved.path, session_dir)
        if resolved.type == "global" and resolved.path:
            out.write(_format_colored_message(f"Session found in different project: {resolved.cwd}", _YELLOW) + "\n")
            should_fork = await prompt_confirm_fn("Fork this session into current directory?")
            if not should_fork:
                out.write(_format_colored_message("Aborted.", _DIM) + "\n")
                raise SystemExit(0)
            return fork_session_or_exit(resolved.path)
        err.write(_format_colored_message(f"No session found matching '{resolved.arg}'", _RED) + "\n")
        raise SystemExit(1)

    if parsed.resume:
        init_theme(settings_manager.getTheme(), True)
        try:
            selected_path = await selector(
                lambda onProgress=None: SessionManager.list(cwd, session_dir, onProgress),
                SessionManager.listAll,
            )
            if not selected_path:
                out.write(_format_colored_message("No session selected", _DIM) + "\n")
                raise SystemExit(0)
            return SessionManager.open(selected_path, session_dir)
        finally:
            stop_theme_watcher()

    if parsed.continue_:
        return SessionManager.continueRecent(cwd, session_dir)

    return SessionManager.create(cwd, session_dir)


async def main(args: list[str], options: MainOptions | None = None) -> int:
    parsed = parse_args(args)
    for diagnostic in parsed.diagnostics:
        prefix = "Error" if diagnostic.type == "error" else "Warning"
        color = _RED if diagnostic.type == "error" else _YELLOW
        print(_format_colored_message(f"{prefix}: {diagnostic.message}", color), file=sys.stderr)
    if any(diagnostic.type == "error" for diagnostic in parsed.diagnostics):
        return 1

    app_mode = resolve_app_mode(parsed, sys.stdin.isatty())
    took_over_stdout = app_mode != "interactive"
    if took_over_stdout:
        takeOverStdout()

    def finish(code: int) -> int:
        if took_over_stdout and isStdoutTakenOver():
            restoreStdout()
        return code

    if parsed.version:
        print(VERSION)
        return finish(0)

    if parsed.export:
        output_path = parsed.messages[0] if parsed.messages else None
        try:
            result = await export_from_file(parsed.export, output_path)
        except Exception as error:  # noqa: BLE001 - any export failure is reported to the user and exits 1
            print(_format_colored_message(f"Error: {error}", _RED), file=sys.stderr)
            return finish(1)
        print(f"Exported to: {result}")
        return finish(0)

    try:
        validate_fork_flags(parsed)
    except ValueError as error:
        print(_format_colored_message(f"Error: {error}", _RED), file=sys.stderr)
        return finish(1)

    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    startup_settings_manager = SettingsManager.create(cwd, agent_dir)
    report_diagnostics(collect_settings_diagnostics(startup_settings_manager, "startup session lookup"))
    session_dir = (
        normalize_path(parsed.sessionDir)
        if parsed.sessionDir
        else expand_tilde_path(os.environ[ENV_SESSION_DIR])
        if os.environ.get(ENV_SESSION_DIR)
        else startup_settings_manager.getSessionDir()
    )
    try:
        session_manager = await create_session_manager(parsed, cwd, session_dir, startup_settings_manager)
    except SystemExit as exit_signal:
        code = exit_signal.code
        return finish(int(code) if isinstance(code, int) else 1)
    missing_session_cwd_issue = None
    if hasattr(session_manager, "getSessionFile") and hasattr(session_manager, "getCwd"):
        missing_session_cwd_issue = get_missing_session_cwd_issue(session_manager, cwd)
    if missing_session_cwd_issue is not None:
        if resolve_app_mode(parsed, sys.stdin.isatty()) == "interactive":
            selected_cwd = await prompt_for_missing_session_cwd(missing_session_cwd_issue, startup_settings_manager)
            if selected_cwd is None:
                return finish(0)
            if not missing_session_cwd_issue.sessionFile:
                print(
                    _format_colored_message(f"Error: {MissingSessionCwdError(missing_session_cwd_issue)}", _RED),
                    file=sys.stderr,
                )
                return finish(1)
            session_manager = SessionManager.open(
                missing_session_cwd_issue.sessionFile,
                session_dir,
                selected_cwd,
            )
        else:
            print(
                _format_colored_message(f"Error: {MissingSessionCwdError(missing_session_cwd_issue)}", _RED),
                file=sys.stderr,
            )
            return finish(1)

    resolved_extension_paths = resolve_cli_paths(cwd, parsed.extensions)
    resolved_prompt_template_paths = resolve_cli_paths(cwd, parsed.promptTemplates)
    resolved_theme_paths = resolve_cli_paths(cwd, parsed.themes)
    auth_storage = AuthStorage.create()
    try:
        runtime = await create_agent_session_runtime(
            create_runtime_factory(
                parsed,
                auth_storage,
                resolved_extension_paths=resolved_extension_paths,
                resolved_prompt_template_paths=resolved_prompt_template_paths,
                resolved_theme_paths=resolved_theme_paths,
                extension_factories=options.get("extensionFactories") if options else None,
            ),
            {
                "cwd": session_manager.getCwd(),
                "agentDir": agent_dir,
                "sessionManager": session_manager,
            },
        )
        services = runtime.services
        session = runtime.session
        settings_manager = services.settingsManager
        model_registry = services.modelRegistry

        if parsed.help:
            extension_flags = [
                flag
                for extension in services.resourceLoader.getExtensions().extensions
                for flag in extension.flags.values()
            ]
            print_help(extension_flags)
            return 0

        if parsed.listModels is not None:
            await list_models(
                model_registry,
                parsed.listModels if isinstance(parsed.listModels, str) else None,
            )
            return 0

        stdin_content = await read_piped_stdin()
        if stdin_content is not None and app_mode == "interactive":
            app_mode = "print"

        initial_message, initial_images = await prepare_initial_message(
            parsed,
            settings_manager.getImageAutoResize(),
            stdin_content,
        )
        init_theme(settings_manager.getTheme(), app_mode == "interactive")
        report_diagnostics(list(runtime.diagnostics))
        if any(item.type == "error" for item in runtime.diagnostics):
            return 1

        if app_mode != "interactive" and session.model is None:
            print(_format_colored_message(formatNoModelsAvailableMessage(), _RED), file=sys.stderr)
            return 1

        if app_mode == "interactive":
            interactive_mode = InteractiveMode(
                runtime,
                {
                    "modelFallbackMessage": runtime.modelFallbackMessage,
                    "initialMessage": initial_message,
                    "initialImages": initial_images,
                    "initialMessages": list(parsed.messages),
                    "verbose": parsed.verbose,
                },
            )
            return await interactive_mode.run()
        exit_code = await run_print_mode(
            runtime,
            {
                "mode": to_print_output_mode(app_mode),
                "messages": list(parsed.messages),
                "initialMessage": initial_message,
                "initialImages": initial_images,
            },
        )
        stop_theme_watcher()
        return exit_code
    finally:
        if took_over_stdout and isStdoutTakenOver():
            restoreStdout()

__all__ = ["MainOptions", "main"]
