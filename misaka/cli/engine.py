"""Minimal Phase 9 CLI entry orchestration."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from misaka.ai.models import models_are_equal
from misaka.ai.types import ImageContent
from misaka.cli import session_picker
from misaka.cli.args import Args, parse_args, print_help
from misaka.cli.file_processor import ProcessFileOptions, process_file_arguments
from misaka.cli.initial_message import build_initial_message
from misaka.cli.list_models import list_models
from misaka.config import VERSION, get_agent_dir
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
from misaka.core.http_dispatcher import applyHttpProxySettings
from misaka.core.keybindings import KeybindingsManager
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_resolver import ScopedModel, resolveCliModel, resolveModelScope
from misaka.core.output_guard import isStdoutTakenOver, restoreStdout, takeOverStdout
from misaka.core.project_trust import (
    ProjectTrustStore,
    has_trust_requiring_project_resources,
    resolve_project_trusted,
)
from misaka.core.session_cwd import (
    MissingSessionCwdError,
    SessionCwdIssue,
    format_missing_session_cwd_prompt,
    get_missing_session_cwd_issue,
)
from misaka.core.session_manager import (
    NewSessionOptions,
    SessionManager,
    sessions_root_of,
)
from misaka.core.settings_diagnostics import (
    collect_settings_diagnostics,
    deduplicate_diagnostics,
)
from misaka.core.settings_manager import SettingsManager
from misaka.core.timings import printTimings, resetTimings, time
from misaka.modes import runPrintMode as run_print_mode
from misaka.ui.tui import TUI, ProcessTerminal, setCapabilityOverrides, setKeybindings
from misaka.ui.tui.interactive import InteractiveMode
from misaka.ui.tui.interactive.components.extension_input import (
    ExtensionInputComponent,
)
from misaka.ui.tui.interactive.components.extension_selector import (
    ExtensionSelectorComponent,
)
from misaka.ui.tui.interactive.theme.theme import init_theme, stop_theme_watcher
from misaka.utils.paths import (
    canonicalize_path,
    is_local_path,
    normalize_path,
    resolve_path,
)

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
    modelProfile: str
    modelDefaultsReadOnly: bool


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


def resolve_app_mode(parsed: Args, stdin_is_tty: bool, stdout_is_tty: bool = True) -> AppMode:
    if parsed.mode == "json":
        return "json"
    if parsed.print or not stdin_is_tty or not stdout_is_tty:
        return "print"
    return "interactive"


def is_plain_runtime_metadata_command(parsed: Args) -> bool:
    """pi main.ts:127-129: help and list-models answer on the real stdout unless a wire mode was asked for."""
    return not parsed.print and parsed.mode is None and (parsed.help or parsed.listModels is not None)


def to_print_output_mode(app_mode: AppMode) -> PrintOutputMode:
    return "json" if app_mode == "json" else "text"


def report_diagnostics(
    diagnostics: Sequence[AgentSessionRuntimeDiagnostic],
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
    stdin_content: str | None = None,
) -> tuple[str | None, list[ImageContent] | None]:
    if not parsed.fileArgs:
        result = build_initial_message(parsed=parsed, stdinContent=stdin_content)
        return result.initialMessage, result.initialImages

    # AgentSession resizes these after extension hooks select the request model.
    processed = await process_file_arguments(
        parsed.fileArgs,
        options=ProcessFileOptions(autoResizeImages=False),
    )
    result = build_initial_message(
        parsed=parsed,
        fileText=processed.text,
        fileImages=processed.images,
        stdinContent=stdin_content,
    )
    return result.initialMessage, result.initialImages


def _find_local_session_by_exact_id(session_id: str, cwd: str, session_dir: str | None) -> ResolvedSession | None:
    path = SessionManager.findById(cwd, session_id, session_dir)
    return ResolvedSession(type="local", path=path) if path else None


async def resolve_session_path(session_arg: str, cwd: str, session_dir: str | None = None) -> ResolvedSession:
    if "/" in session_arg or "\\" in session_arg or session_arg.endswith(".jsonl"):
        return ResolvedSession(type="path", path=resolve_path(session_arg, cwd))

    # Exact IDs only require reading session headers. Fall back to the full
    # metadata listing for prefix matches.
    exact_local_match = _find_local_session_by_exact_id(session_arg, cwd, session_dir)
    if exact_local_match is not None:
        return exact_local_match
    local_sessions = await SessionManager.list(cwd, session_dir)
    local_match = next(
        (
            session
            for session in local_sessions
            if session.id.startswith(session_arg)
        ),
        None,
    )
    if local_match is not None:
        return ResolvedSession(type="local", path=local_match.path)

    global_sessions = await SessionManager.listAll(sessions_root_of(session_dir))
    global_match = next(
        (session for session in global_sessions if session.id == session_arg), None
    )
    if global_match is None:
        global_match = next(
            (
                session
                for session in global_sessions
                if session.id.startswith(session_arg)
            ),
            None,
        )
    if global_match is not None:
        return ResolvedSession(
            type="global", path=global_match.path, cwd=global_match.cwd
        )

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


async def _show_startup_component(
    settings_manager: SettingsManager,
    builder: Callable[[Callable[[Any], None], TUI], tuple[Any, Any]],
) -> Any:
    """Run one modal before InteractiveMode owns the terminal."""
    init_theme(settings_manager.getTheme())
    setKeybindings(KeybindingsManager.create())
    try:
        ui = TUI(
            ProcessTerminal(),
            bool(getattr(settings_manager, "getShowHardwareCursor", lambda: False)()),
        )
    except TypeError:
        ui = TUI(ProcessTerminal())
    if hasattr(ui, "setClearOnShrink"):
        ui.setClearOnShrink(bool(getattr(settings_manager, "getClearOnShrink", lambda: False)()))

    done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    closed = False

    def finish(result: Any) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        ui.stop()
        done.set_result(result)

    component, focus = builder(finish, ui)
    ui.addChild(component)
    ui.setFocus(focus)
    try:
        ui.start()
        return await done
    finally:
        if not closed:
            closed = True
            ui.stop()


async def _show_startup_selector(
    settings_manager: SettingsManager,
    title: str,
    options: list[tuple[str, Any]],
) -> Any:
    labels = [label for label, _value in options]

    def build(finish: Callable[[Any], None], ui: TUI) -> tuple[Any, Any]:
        def select(label: str) -> None:
            selected = next((value for candidate, value in options if candidate == label), None)
            finish(selected)

        selector = ExtensionSelectorComponent(
            title,
            labels,
            select,
            lambda: finish(None),
            {"tui": ui},
        )
        return selector, selector

    return await _show_startup_component(settings_manager, build)


async def _show_startup_input(
    settings_manager: SettingsManager,
    title: str,
    placeholder: str | None,
) -> str | None:
    def build(finish: Callable[[Any], None], ui: TUI) -> tuple[Any, Any]:
        component = ExtensionInputComponent(
            title,
            placeholder,
            finish,
            lambda: finish(None),
            {"tui": ui},
        )
        return component, component

    result = await _show_startup_component(settings_manager, build)
    return str(result) if result is not None else None


def create_project_trust_context(
    *,
    cwd: str,
    mode: AppMode,
    settings_manager: SettingsManager,
    has_ui: bool,
) -> dict[str, Any]:
    async def select(title: str, options: list[str], _opts: Any = None) -> str | None:
        if not has_ui or mode != "interactive":
            return None
        result = await _show_startup_selector(
            settings_manager,
            title,
            [(option, option) for option in options],
        )
        return str(result) if result is not None else None

    async def confirm(title: str, message: str, _opts: Any = None) -> bool:
        if not has_ui or mode != "interactive":
            return False
        result = await _show_startup_selector(
            settings_manager,
            f"{title}\n{message}",
            [("Yes", True), ("No", False)],
        )
        return bool(result)

    async def input_value(
        title: str,
        placeholder: str | None = None,
        _opts: Any = None,
    ) -> str | None:
        if not has_ui or mode != "interactive":
            return None
        return await _show_startup_input(settings_manager, title, placeholder)

    def notify(message: str, type: str = "info") -> None:
        if mode == "interactive":
            return
        color = _RED if type == "error" else _YELLOW if type == "warning" else _DIM
        print(_format_colored_message(message, color), file=sys.stderr)

    return {
        "cwd": cwd,
        "mode": "tui" if mode == "interactive" else mode,
        "hasUI": has_ui,
        "ui": {
            "select": select,
            "confirm": confirm,
            "input": input_value,
            "notify": notify,
        },
    }


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


_SESSION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")   # pi session-manager.ts:212-218


def validate_session_id_flags(parsed: Args) -> None:
    if parsed.sessionId is None:
        return
    conflicts = [
        flag
        for flag, given in (("--session", parsed.session), ("--continue", parsed.continue_), ("--resume", parsed.resume))
        if given
    ]
    if conflicts:
        raise ValueError(f"--session-id cannot be combined with {', '.join(conflicts)}")
    if not _SESSION_ID.match(parsed.sessionId):
        raise ValueError(
            "Session id must be non-empty, contain only alphanumeric characters, '-', '_', and '.', "
            "and start and end with an alphanumeric character"
        )


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
                "cliThinking": parsed.thinking,
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

    role_default = bool(
        "model" not in options and scoped_models and not has_existing_session and not parsed.models
        and getattr(settings_manager, "hasPinnedModelDefault", lambda: False)()
    )
    if "model" not in options and scoped_models and not has_existing_session and (parsed.models or not role_default):
        saved_provider, saved_model_id = settings_manager.getDefaultModelPair()
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
    if parsed.excludeTools:
        options["excludeTools"] = list(parsed.excludeTools)

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
    custom_tools: list[Any] | None = None,
    parts: list[Any] | None = None,
    model_profile: str | None = None,
    model_defaults_read_only: bool = False,
    app_mode: AppMode = "print",
    startup_settings_manager: SettingsManager | None = None,
) -> Callable[[dict[str, Any]], Awaitable[CreateAgentSessionRuntimeResult]]:
    if parsed.agents is not None:
        for part in parts or ():
            configure = getattr(part, "configure_agents", None)
            if configure is not None:
                configure(parsed.agents)
    project_trust_by_cwd: dict[str, bool] = {}

    async def _factory(runtime_options: dict[str, Any]) -> CreateAgentSessionRuntimeResult:
        runtime_cwd = str(runtime_options["cwd"])
        agent_dir = str(runtime_options["agentDir"])
        trust_key = canonicalize_path(resolve_path(runtime_cwd))
        trust_store = ProjectTrustStore(agent_dir)
        has_trust_resources = has_trust_requiring_project_resources(runtime_cwd)
        has_cached_trust = trust_key in project_trust_by_cwd
        should_resolve_trust = (
            parsed.projectTrustOverride is None
            and not has_cached_trust
            and has_trust_resources
        )
        if should_resolve_trust:
            project_trusted = False
        elif has_cached_trust:
            project_trusted = project_trust_by_cwd[trust_key]
        elif parsed.projectTrustOverride is not None:
            project_trusted = parsed.projectTrustOverride
        else:
            # Reaching this branch means `should_resolve_trust` was false with no cached
            # decision and no override, which -- given how it is computed above -- can only
            # happen when `has_trust_resources` is false. There is nothing to trust, so
            # there is nothing to look up: the saved decisions are read by `resolve_trust`
            # below, via `resolve_project_trusted`.
            project_trusted = True

        settings_manager = SettingsManager.create(
            runtime_cwd,
            agent_dir,
            {"projectTrusted": project_trusted},
        )
        project_trust_diagnostics: list[AgentSessionRuntimeDiagnostic] = []

        async def resolve_trust(payload: dict[str, Any]) -> bool:
            nonlocal project_trusted
            context = runtime_options.get("projectTrustContext")
            if context is None:
                context_settings = startup_settings_manager or SettingsManager.create(
                    runtime_cwd,
                    agent_dir,
                    {"projectTrusted": False},
                )
                context = create_project_trust_context(
                    cwd=runtime_cwd,
                    mode=app_mode,
                    settings_manager=context_settings,
                    has_ui=(
                        runtime_options.get("sessionStartEvent") is None
                        and app_mode == "interactive"
                    ),
                )
            project_trusted = await resolve_project_trusted(
                {
                    "cwd": runtime_cwd,
                    "trustStore": trust_store,
                    "trustOverride": parsed.projectTrustOverride,
                    "defaultProjectTrust": (
                        startup_settings_manager.getDefaultProjectTrust()
                        if startup_settings_manager is not None
                        else settings_manager.getDefaultProjectTrust()
                    ),
                    "extensionsResult": payload["extensionsResult"],
                    "projectTrustContext": context,
                    "onExtensionError": lambda message: project_trust_diagnostics.append(
                        AgentSessionRuntimeDiagnostic(type="warning", message=message)
                    ),
                }
            )
            project_trust_by_cwd[trust_key] = project_trusted
            return project_trusted

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
                "cwd": runtime_cwd,
                "agentDir": agent_dir,
                "authStorage": auth_storage,
                "settingsManager": settings_manager,
                "extensionFlagValues": parsed.unknownFlags,
                "resourceLoaderOptions": resource_loader_options,
                **(
                    {
                        "resourceLoaderReloadOptions": {
                            "resolveProjectTrust": resolve_trust,
                        }
                    }
                    if should_resolve_trust
                    else {}
                ),
            }
        )
        settings_manager = services.settingsManager
        if parsed.useTheme is not None:   # Override for this run only; never written back to settings (pi #7722).
            settings_manager.applyOverrides({"theme": parsed.useTheme})
        model_registry = services.modelRegistry
        if model_profile:
            settings_manager.bindModelProfile(model_profile, model_registry)
        if model_defaults_read_only:
            settings_manager.restrictModelDefaults()
        resource_loader = services.resourceLoader

        diagnostics: list[AgentSessionRuntimeDiagnostic] = [
            *project_trust_diagnostics,
            *services.diagnostics,
            *collect_settings_diagnostics(settings_manager),
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
                "excludeTools": session_options.options.get("excludeTools"),
                "noTools": session_options.options.get("noTools"),
                "customTools": custom_tools,
                "parts": parts,
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

    def fork_session_or_exit(source_path: str, session_id: str | None = None) -> SessionManager:
        try:
            return SessionManager.forkFrom(
                source_path, cwd, session_dir, NewSessionOptions(id=session_id) if session_id else None
            )
        except Exception as error:
            err.write(_format_colored_message(f"Error: {error}", _RED) + "\n")
            raise SystemExit(1) from error

    async def find_local_session_by_exact_id(session_id: str) -> str | None:
        match = _find_local_session_by_exact_id(session_id, cwd, session_dir)
        return match.path if match else None

    if parsed.noSession or parsed.help or parsed.listModels is not None:
        manager = SessionManager.inMemory(cwd)
        if parsed.sessionId:
            manager.newSession(NewSessionOptions(id=parsed.sessionId))
        return manager

    if parsed.fork:
        if parsed.sessionId and await find_local_session_by_exact_id(parsed.sessionId):
            err.write(_format_colored_message(f"Session already exists with id '{parsed.sessionId}'", _RED) + "\n")
            raise SystemExit(1)
        resolved = await resolve_session_path(parsed.fork, cwd, session_dir)
        if resolved.type in {"path", "local", "global"} and resolved.path:
            return fork_session_or_exit(resolved.path, parsed.sessionId)
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
                lambda onProgress=None: SessionManager.listAll(sessions_root_of(session_dir), onProgress),
            )
            if not selected_path:
                out.write(_format_colored_message("No session selected", _DIM) + "\n")
                raise SystemExit(0)
            return SessionManager.open(selected_path, session_dir)
        finally:
            stop_theme_watcher()

    if parsed.continue_:
        return SessionManager.continueRecent(cwd, session_dir)

    if parsed.sessionId:   # pi main.ts:430-442: an exact project session id reopens it, otherwise it is created
        existing = await find_local_session_by_exact_id(parsed.sessionId)
        if existing:
            return SessionManager.open(existing, session_dir)
        err.write(_format_colored_message(
            f"Warning: No project session found with id '{parsed.sessionId}'; creating a new session with that id.",
            _YELLOW,
        ) + "\n")
        manager = SessionManager.create(cwd, session_dir)
        manager.newSession(NewSessionOptions(id=parsed.sessionId))
        return manager

    return SessionManager.create(cwd, session_dir)


async def main(args: list[str], options: MainOptions | None = None) -> int:
    resetTimings()
    parsed = parse_args(args)
    for diagnostic in parsed.diagnostics:
        prefix = "Error" if diagnostic.type == "error" else "Warning"
        color = _RED if diagnostic.type == "error" else _YELLOW
        print(_format_colored_message(f"{prefix}: {diagnostic.message}", color), file=sys.stderr)
    if any(diagnostic.type == "error" for diagnostic in parsed.diagnostics):
        return 1
    time("parseArgs")

    # pi main.ts:614-637: version and export answer on the real stdout before any takeover.
    if parsed.version:
        print(VERSION)
        return 0

    if parsed.export:
        output_path = parsed.messages[0] if parsed.messages else None
        try:
            result = await export_from_file(parsed.export, output_path)
        except Exception as error:  # noqa: BLE001 - any export failure is reported to the user and exits 1
            print(_format_colored_message(f"Error: {error}", _RED), file=sys.stderr)
            return 1
        print(f"Exported to: {result}")
        return 0

    app_mode = resolve_app_mode(parsed, sys.stdin.isatty(), sys.stdout.isatty())
    took_over_stdout = app_mode != "interactive" and not is_plain_runtime_metadata_command(parsed)
    if took_over_stdout:
        takeOverStdout()

    def finish(code: int) -> int:
        if took_over_stdout and isStdoutTakenOver():
            restoreStdout()
        return code

    try:
        validate_fork_flags(parsed)
        validate_session_id_flags(parsed)
    except ValueError as error:
        print(_format_colored_message(f"Error: {error}", _RED), file=sys.stderr)
        return finish(1)

    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    # Session lookup and the pre-runtime trust prompt may use global settings,
    # but project settings are precisely what the trust decision protects.
    startup_settings_manager = SettingsManager.create(
        cwd,
        agent_dir,
        {"projectTrusted": False},
    )
    applyHttpProxySettings(startup_settings_manager.getGlobalSettings().get("httpProxy"))
    startup_settings_diagnostics = deduplicate_diagnostics(
        collect_settings_diagnostics(startup_settings_manager)
    )
    report_diagnostics(startup_settings_diagnostics)
    session_dir = (
        normalize_path(parsed.sessionDir)
        if parsed.sessionDir
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
        if resolve_app_mode(parsed, sys.stdin.isatty(), sys.stdout.isatty()) == "interactive":
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
    if parsed.name is not None:   # pi main.ts:689-696
        name = parsed.name.strip()
        if not name:
            print(_format_colored_message("Error: --name requires a non-empty value", _RED), file=sys.stderr)
            return finish(1)
        session_manager.appendSessionInfo(name)

    time("createSessionManager")
    resolved_extension_paths = resolve_cli_paths(cwd, parsed.extensions)
    resolved_prompt_template_paths = resolve_cli_paths(cwd, parsed.promptTemplates)
    resolved_theme_paths = resolve_cli_paths(cwd, parsed.themes)
    session_cwd = session_manager.getCwd()
    auto_trust_on_reload_cwd = (
        session_cwd
        if parsed.projectTrustOverride is None
        and not has_trust_requiring_project_resources(session_cwd)
        else None
    )
    auth_storage = AuthStorage.create()
    runtime = None
    runtime_handed_off = False   # set once a mode is running: the mode disposes the runtime itself
    try:
        runtime_factory = create_runtime_factory(
            parsed,
            auth_storage,
            resolved_extension_paths=resolved_extension_paths,
            resolved_prompt_template_paths=resolved_prompt_template_paths,
            resolved_theme_paths=resolved_theme_paths,
            extension_factories=options.get("extensionFactories") if options else None,
            custom_tools=options.get("customTools") if options else None,
            parts=options.get("parts") if options else None,
            model_profile=options.get("modelProfile") if options else None,
            model_defaults_read_only=bool(options.get("modelDefaultsReadOnly")) if options else False,
            app_mode=(
                "print"
                if parsed.help or parsed.listModels is not None
                else app_mode
            ),
            startup_settings_manager=startup_settings_manager,
        )
        time("createRuntime")
        runtime = await create_agent_session_runtime(
            runtime_factory,
            {
                "cwd": session_manager.getCwd(),
                "agentDir": agent_dir,
                "sessionManager": session_manager,
            },
        )
        time("createAgentSessionRuntime")
        services = runtime.services
        session = runtime.session
        settings_manager = services.settingsManager
        setCapabilityOverrides(settings_manager.getTerminalCapabilityOverrides())
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

        # No downgrade to "print" here, unlike upstream `main.ts:867-872`: piped stdin
        # already forced `resolve_app_mode` to "print" (`read_piped_stdin` returns None on
        # a tty), so the downgrade was unreachable -- and `took_over_stdout` was computed
        # from the pre-downgrade mode, so a reachable version of it would have run print
        # mode with stdout never taken over.
        stdin_content = await read_piped_stdin()
        time("readPipedStdin")

        initial_message, initial_images = await prepare_initial_message(parsed, stdin_content)
        time("prepareInitialMessage")
        init_theme(settings_manager.getTheme(), app_mode == "interactive")
        time("initTheme")
        time("resolveModelScope")
        display_diagnostics = deduplicate_diagnostics(
            [*startup_settings_diagnostics, *runtime.diagnostics]
        )
        report_diagnostics(display_diagnostics[len(startup_settings_diagnostics) :])
        if any(item.type == "error" for item in runtime.diagnostics):
            return 1
        time("createAgentSession")

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
                    "autoTrustOnReloadCwd": auto_trust_on_reload_cwd,
                },
            )
            printTimings()
            runtime_handed_off = True
            return await interactive_mode.run()
        printTimings()
        runtime_handed_off = True
        return await run_print_mode(
            runtime,
            {
                "mode": to_print_output_mode(app_mode),
                "messages": list(parsed.messages),
                "initialMessage": initial_message,
                "initialImages": initial_images,
            },
        )
    finally:
        # pi main.ts:858/865 leaves via process.exit() right after --help / --list-models, which
        # releases everything by definition. Returning instead means every early return above
        # (help, list-models, an error diagnostic from the runtime, no model in a non-interactive
        # run) has to release explicitly, or the extensions registered on session_shutdown -- MCP
        # server subprocesses, the LCM context flush, subagent and research teardown -- never get
        # their event. Both modes dispose the runtime themselves (print_mode.py:189,
        # interactive_mode.py:5604), so a handed-off runtime is left alone rather than being sent
        # session_shutdown twice.
        if runtime is not None and not runtime_handed_off:
            try:
                await runtime.dispose()
            except Exception as error:  # noqa: BLE001 - shutdown must not replace the command's own result
                print(
                    _format_colored_message(f"Warning: session shutdown failed: {error}", _YELLOW),
                    file=sys.stderr,
                )
        stop_theme_watcher()   # pi main.ts:971-972 stops it beside restoreStdout()
        if took_over_stdout and isStdoutTakenOver():
            restoreStdout()

__all__ = ["MainOptions", "main"]
