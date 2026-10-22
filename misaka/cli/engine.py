"""Translated from pi's ``packages/coding-agent/src/main.ts`` (v1.1.0): the engine's CLI entry.

MISAKA forks:
- ``misaka.cli.app`` routes the product's subcommands (``mcp`` among them) before this runs; there is no package
  manager (install / remove / update / list), no ``config`` command, no RPC mode, no first-time setup, no version
  check, no managed-install or Windows self-update cleanup, no migrations (nothing older ships) and no startup
  benchmark.
- Skills are MISAKA's own subsystem (``core/skills``), so ``--skill`` / ``--no-skills`` are not offered.
- Bundled extensions arrive through ``bootstrap.bundled`` rather than ``builtInExtensions``; role-bound settings
  (``modelProfile``), read-only model defaults, core ``parts`` and ``customTools`` are MISAKA's options.
- The session store is the product tree (``sessions_root_of``); there is no session-directory environment variable.
- ``main`` returns the exit code where pi calls ``process.exit()`` or sets ``process.exitCode``, so the runtime and
  the terminal are released on every path; the helpers that pi lets ``process.exit()`` raise ``SystemExit``.
- The runtime factory is a module function (``createRuntimeFactory``) because headless sessions
  (``core/platform/session.py``) build their runtime through it too; pi keeps it as a closure inside ``main``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from misaka.ai.auth.types import AuthOperationOptions
from misaka.ai.models import modelsAreEqual
from misaka.cli.args import Args, normalizeSessionName, parseArgs, printHelp
from misaka.cli.file_processor import ProcessFileOptions, processFileArguments
from misaka.cli.initial_message import InitialMessageResult, buildInitialMessage
from misaka.cli.list_models import listModels
from misaka.cli.project_trust import createProjectTrustContext
from misaka.cli.session_picker import selectSession
from misaka.cli.startup_ui import StartupOption, showStartupSelector
from misaka.config import APP_NAME, VERSION, get_agent_dir
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    createAgentSessionRuntime,
)
from misaka.core.agent_session_services import (
    AgentSessionRuntimeDiagnostic,
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.auth_guidance import formatNoModelsAvailableMessage
from misaka.core.export_html import exportFromFile
from misaka.core.http_dispatcher import applyHttpProxySettings, configureHttpDispatcher
from misaka.core.model_resolver import ScopedModel, resolveCliModel, resolveModelScope
from misaka.core.model_runtime import ModelRuntime
from misaka.core.output_guard import isStdoutTakenOver, restoreStdout, takeOverStdout
from misaka.core.project_trust import AppMode, resolveProjectTrusted
from misaka.core.session_cwd import (
    MissingSessionCwdError,
    SessionCwdIssue,
    formatMissingSessionCwdPrompt,
    getMissingSessionCwdIssue,
)
from misaka.core.session_manager import (
    NewSessionOptions,
    SessionManager,
    assertValidSessionId,
    sessions_root_of,
)
from misaka.core.settings_diagnostics import (
    collectSettingsDiagnostics,
    deduplicateDiagnostics,
)
from misaka.core.settings_manager import SettingsManager
from misaka.core.timings import printTimings, resetTimings, time
from misaka.core.trust_manager import (
    ProjectTrustStore,
    hasTrustRequiringProjectResources,
)
from misaka.modes import InteractiveMode, runPrintMode
from misaka.modes.interactive.theme.theme import (
    initTheme,
    setThemeJsonValidator,
    stopThemeWatcher,
)
from misaka.modes.interactive.theme.theme_schema import validateThemeJson
from misaka.ui.tui import (
    setCapabilityOverrides,
)
from misaka.utils._abort_signal import TimeoutAbortSignal
from misaka.utils.chalk import chalk
from misaka.utils.js import errorMessage, jsTrim
from misaka.utils.paths import (
    isLocalPath,
    normalizePath,
    resolvePath,
)

PrintOutputMode = Literal["text", "json"]

EXTENSION_LOAD_FAILURE_HINT = f'Hint: Start without extensions using "{APP_NAME} -ne".'


async def readPipedStdin() -> str | None:
    """Read all content from piped stdin.
    Returns undefined if stdin is a TTY (interactive terminal)."""
    # If stdin is a TTY, we're running interactively - don't read stdin
    if sys.stdin.isatty():
        return None

    data = await asyncio.to_thread(sys.stdin.read)
    return jsTrim(data) or None


def reportDiagnostics(diagnostics: Sequence[AgentSessionRuntimeDiagnostic]) -> None:
    for diagnostic in diagnostics:
        color = chalk.red if diagnostic.type == "error" else chalk.yellow if diagnostic.type == "warning" else chalk.dim
        prefix = "Error: " if diagnostic.type == "error" else "Warning: " if diagnostic.type == "warning" else ""
        print(color(f"{prefix}{diagnostic.message}"), file=sys.stderr)


def isTruthyEnvFlag(value: str | None) -> bool:
    if not value:
        return False
    return value == "1" or value.lower() == "true" or value.lower() == "yes"


def resolveAppMode(parsed: Args, stdinIsTTY: bool, stdoutIsTTY: bool) -> AppMode:
    # MISAKA fork: there is no RPC mode.
    if parsed.mode == "json":
        return "json"
    if parsed.print or not stdinIsTTY or not stdoutIsTTY:
        return "print"
    return "interactive"


def toPrintOutputMode(appMode: AppMode) -> PrintOutputMode:
    return "json" if appMode == "json" else "text"


def isPlainRuntimeMetadataCommand(parsed: Args) -> bool:
    return not parsed.print and parsed.mode is None and (parsed.help is True or parsed.listModels is not None)


async def runAuthCommand(args: list[str]) -> int | None:
    """pi's ``runAuthCommand``: the exit code of an ``auth`` command, or None when ``args`` is not one.

    MISAKA fork: the exit code is returned, where pi sets ``process.exitCode`` and returns true.
    """
    from misaka.cli.auth_check import (
        AuthCheckResult,
        checkProviderAuth,
        createAuthCheckModelRuntime,
        getProviderCredential,
    )
    from misaka.cli.auth_command import (
        AuthCommandError,
        getAuthCommandName,
        getAuthCommandUsage,
        isAuthCommandHelp,
        parseAuthCommand,
        printAuthCommandHelp,
        validateAuthCommandArgs,
    )
    from misaka.cli.credential_print import resolveCredentialForPrint
    from misaka.core.auth_storage import AuthStorage, ReadOnlyAuthStorage
    from misaka.utils.js import jsonStringify

    if isAuthCommandHelp(args):
        printAuthCommandHelp()
        return 0

    try:
        command = parseAuthCommand(args)
    except Exception as error:  # noqa: BLE001 - upstream reports any parse failure
        message = str(error) if isinstance(error, AuthCommandError) else "Failed to parse auth command"
        print(chalk.red(f"Error: {message}"), file=sys.stderr)
        return 1
    if not command:
        return None

    parsed = parseArgs(command.args)
    if len(parsed.unknownFlags) > 0:
        option = next(iter(parsed.unknownFlags))
        print(chalk.red(f'Unknown option --{option} for "{getAuthCommandName(command.kind)}".'), file=sys.stderr)
        print(chalk.dim(f'Use "{APP_NAME} --help" or "{getAuthCommandUsage(command.kind)}".'), file=sys.stderr)
        return 1
    try:
        if len(parsed.diagnostics) > 0:
            raise AuthCommandError("\n".join(diagnostic.message for diagnostic in parsed.diagnostics))
        if command.kind != "check":
            signal = TimeoutAbortSignal(15_000)
            modelRuntime = await ModelRuntime.create({"allowModelNetwork": False, "signal": signal})
            credential = await resolveCredentialForPrint(parsed, modelRuntime, command.kind, command.minExpiryMs, signal)
            sys.stdout.write(f"{credential}\n")
            return 0

        # MISAKA fork: a bare `auth check` prints MISAKA's provider overview, where pi rejects it.
        if not command.json and not command.credentials and not jsTrim(parsed.provider or "") and not jsTrim(parsed.model or ""):
            from misaka.cli.auth_overview import print_provider_overview

            return await print_provider_overview()
        requestedAuth = validateAuthCommandArgs(parsed, command.kind)
        credential = None
        try:
            credentials = ReadOnlyAuthStorage() if command.noRefresh else AuthStorage.create()
            modelRuntime = await createAuthCheckModelRuntime(credentials)
            result = await checkProviderAuth(parsed, modelRuntime, {"refresh": not command.noRefresh})
            if command.credentials and result.status == "ready":
                credential = await getProviderCredential(
                    result.provider, modelRuntime, credentials, {"refresh": not command.noRefresh}
                )
                if not credential:
                    result = AuthCheckResult(status="not_ready", provider=result.provider, reason="credential_not_available")
        except Exception:  # noqa: BLE001 - upstream's catch-all: an invalid state
            result = AuthCheckResult(
                status="invalid",
                provider=requestedAuth["provider"] if requestedAuth["provider"] is not None else requestedAuth["model"],
                reason="invalid_state",
            )
        record = {"status": result.status, "provider": result.provider}
        if result.reason is not None:
            record["reason"] = result.reason
        if result.authType is not None:
            record["authType"] = result.authType
        output = jsonStringify({**record, **({"credentials": credential} if credential else {})}) if command.json else (
            credential if credential is not None else result.status
        )
        sys.stdout.write(f"{output}\n")
        return 0 if result.status == "ready" else 1 if result.status == "not_ready" else 2
    except Exception as error:  # noqa: BLE001 - upstream reports any failure
        message = str(error) if isinstance(error, AuthCommandError) else "Failed to resolve credential"
        print(chalk.red(f"Error: {message}"), file=sys.stderr)
        return 2 if command.kind == "check" else 1


async def prepareInitialMessage(
    parsed: Args,
    stdinContent: str | None = None,
) -> InitialMessageResult:
    if len(parsed.fileArgs) == 0:
        return buildInitialMessage(parsed=parsed, stdinContent=stdinContent)

    # AgentSession resizes these after extension hooks select the request model.
    processed = await processFileArguments(parsed.fileArgs, options=ProcessFileOptions(autoResizeImages=False))
    return buildInitialMessage(
        parsed=parsed,
        fileText=processed.text,
        fileImages=processed.images,
        stdinContent=stdinContent,
    )


@dataclass(slots=True)
class ResolvedSession:
    """Result from resolving a session argument"""

    type: Literal["path", "local", "global", "not_found"]
    path: str | None = None
    cwd: str | None = None
    arg: str | None = None


def findLocalSessionByExactId(
    sessionId: str,
    cwd: str,
    sessionDir: str | None = None,
) -> ResolvedSession | None:
    """Resolve a session argument to a file path.
    If it looks like a path, use as-is. Otherwise try to match as session ID prefix."""
    path = SessionManager.findById(cwd, sessionId, sessionDir)
    return ResolvedSession(type="local", path=path) if path else None


async def resolveSessionPath(sessionArg: str, cwd: str, sessionDir: str | None = None) -> ResolvedSession:
    # If it looks like a file path, resolve it before handing it to the session manager.
    if "/" in sessionArg or "\\" in sessionArg or sessionArg.endswith(".jsonl"):
        return ResolvedSession(type="path", path=resolvePath(sessionArg, cwd))

    # Exact IDs only require reading session headers. Fall back to the full
    # metadata listing for prefix matches.
    exactLocalMatch = findLocalSessionByExactId(sessionArg, cwd, sessionDir)
    if exactLocalMatch:
        return exactLocalMatch

    localSessions = await SessionManager.list(cwd, sessionDir)
    localMatch = next((s for s in localSessions if s.id.startswith(sessionArg)), None)

    if localMatch:
        return ResolvedSession(type="local", path=localMatch.path)

    # Try global search across all projects
    # MISAKA fork: "all projects" is the root of this run's store (a window's own store), not a flat directory.
    allSessions = await SessionManager.listAll(sessions_root_of(sessionDir))
    globalMatch = next((s for s in allSessions if s.id == sessionArg), None)
    if globalMatch is None:
        globalMatch = next((s for s in allSessions if s.id.startswith(sessionArg)), None)

    if globalMatch:
        return ResolvedSession(type="global", path=globalMatch.path, cwd=globalMatch.cwd)

    # Not found anywhere
    return ResolvedSession(type="not_found", arg=sessionArg)


async def promptConfirm(message: str) -> bool:
    """Prompt user for yes/no confirmation"""
    sys.stdout.write(f"{message} [y/N] ")
    sys.stdout.flush()
    # readline's answer carries no line terminator; Python's readline() keeps it.
    answer = (await asyncio.to_thread(sys.stdin.readline)).rstrip("\r\n")
    return answer.lower() == "y" or answer.lower() == "yes"


def validateForkFlags(parsed: Args) -> None:
    if not parsed.fork:
        return

    conflictingFlags = [
        flag
        for flag in (
            "--session" if parsed.session else None,
            "--continue" if parsed.continue_ else None,
            "--resume" if parsed.resume else None,
            "--no-session" if parsed.noSession else None,
        )
        if flag is not None
    ]

    if len(conflictingFlags) > 0:
        print(chalk.red(f"Error: --fork cannot be combined with {', '.join(conflictingFlags)}"), file=sys.stderr)
        raise SystemExit(1)


def validateSessionIdFlags(parsed: Args) -> None:
    if parsed.sessionId is None:
        return

    conflictingFlags = [
        flag
        for flag in (
            "--session" if parsed.session else None,
            "--continue" if parsed.continue_ else None,
            "--resume" if parsed.resume else None,
        )
        if flag is not None
    ]

    if len(conflictingFlags) > 0:
        print(chalk.red(f"Error: --session-id cannot be combined with {', '.join(conflictingFlags)}"), file=sys.stderr)
        raise SystemExit(1)

    try:
        assertValidSessionId(parsed.sessionId)
    except Exception as error:
        print(chalk.red(f"Error: {errorMessage(error)}"), file=sys.stderr)
        raise SystemExit(1) from error


def openSessionOrExit(path: str, sessionDir: str | None = None) -> SessionManager:
    try:
        return SessionManager.open(path, sessionDir)
    except Exception as error:
        print(chalk.red(f"Error: {errorMessage(error)}"), file=sys.stderr)
        raise SystemExit(1) from error


def forkSessionOrExit(sourcePath: str, cwd: str, sessionDir: str | None = None, sessionId: str | None = None) -> SessionManager:
    try:
        return SessionManager.forkFrom(sourcePath, cwd, sessionDir, NewSessionOptions(id=sessionId))
    except Exception as error:
        print(chalk.red(f"Error: {errorMessage(error)}"), file=sys.stderr)
        raise SystemExit(1) from error


async def createSessionManager(
    parsed: Args,
    cwd: str,
    sessionDir: str | None,
    settingsManager: SettingsManager,
) -> SessionManager:
    if parsed.noSession or parsed.help or parsed.listModels is not None:
        return SessionManager.inMemory(cwd, NewSessionOptions(id=parsed.sessionId) if parsed.sessionId is not None else None)

    if parsed.fork:
        if parsed.sessionId:
            existingTarget = findLocalSessionByExactId(parsed.sessionId, cwd, sessionDir)
            if existingTarget:
                print(chalk.red(f"Session already exists with id '{parsed.sessionId}'"), file=sys.stderr)
                raise SystemExit(1)

        resolved = await resolveSessionPath(parsed.fork, cwd, sessionDir)

        match resolved.type:
            case "path" | "local" | "global":
                return forkSessionOrExit(resolved.path, cwd, sessionDir, parsed.sessionId)

            case "not_found":
                print(chalk.red(f"No session found matching '{resolved.arg}'"), file=sys.stderr)
                raise SystemExit(1)

    if parsed.session:
        resolved = await resolveSessionPath(parsed.session, cwd, sessionDir)

        match resolved.type:
            case "path" | "local":
                return openSessionOrExit(resolved.path, sessionDir)

            case "global":
                print(chalk.yellow(f"Session found in different project: {resolved.cwd}"))
                shouldFork = await promptConfirm("Fork this session into current directory?")
                if not shouldFork:
                    print(chalk.dim("Aborted."))
                    raise SystemExit(0)
                return forkSessionOrExit(resolved.path, cwd, sessionDir)

            case "not_found":
                print(chalk.red(f"No session found matching '{resolved.arg}'"), file=sys.stderr)
                raise SystemExit(1)

    if parsed.resume:
        try:
            selectedPath = await selectSession(
                lambda onProgress=None, signal=None: SessionManager.list(cwd, sessionDir, onProgress, signal),
                # MISAKA fork: the all-sessions tab lists the root of this run's store (a window's own store).
                lambda onProgress=None, signal=None: SessionManager.listAll(sessions_root_of(sessionDir), onProgress, signal),
                settingsManager,
            )
            if not selectedPath:
                print(chalk.dim("No session selected"))
                raise SystemExit(0)
            return SessionManager.open(selectedPath, sessionDir)
        finally:
            stopThemeWatcher()

    if parsed.continue_:
        return SessionManager.continueRecent(cwd, sessionDir)

    if parsed.sessionId:
        existingSession = findLocalSessionByExactId(parsed.sessionId, cwd, sessionDir)
        if existingSession:
            return SessionManager.open(existingSession.path, sessionDir)
        print(
            chalk.yellow(
                f"Warning: No project session found with id '{parsed.sessionId}'; creating a new session with that id.",
            ),
            file=sys.stderr,
        )

    return SessionManager.create(cwd, sessionDir, NewSessionOptions(id=parsed.sessionId))


@dataclass(slots=True)
class BuildSessionOptionsResult:
    """The object literal ``buildSessionOptions`` returns."""

    options: dict[str, Any] = field(default_factory=dict)
    cliThinkingFromModel: bool = False
    diagnostics: list[AgentSessionRuntimeDiagnostic] = field(default_factory=list)


def buildSessionOptions(
    parsed: Args,
    scopedModels: list[ScopedModel],
    hasExistingSession: bool,
    modelRuntime: ModelRuntime,
    settingsManager: SettingsManager,
) -> BuildSessionOptionsResult:
    options: dict[str, Any] = {}
    diagnostics: list[AgentSessionRuntimeDiagnostic] = []
    cliThinkingFromModel = False

    # Model from CLI
    # - supports --provider <name> --model <pattern>
    # - supports --model <provider>/<pattern>
    if parsed.provider and not parsed.model:
        diagnostics.append(AgentSessionRuntimeDiagnostic(
            type="error",
            message=f"--provider requires --model (for example: --provider {parsed.provider} --model <pattern>)",
        ))
    if parsed.model:
        resolved = resolveCliModel(
            {
                "cliProvider": parsed.provider,
                "cliModel": parsed.model,
                "cliThinking": parsed.thinking,
                "inlineApiKey": bool(parsed.apiKey),
                "modelRuntime": modelRuntime,
            }
        )
        if resolved.warning:
            diagnostics.append(AgentSessionRuntimeDiagnostic(type="warning", message=resolved.warning))
        if resolved.error:
            diagnostics.append(AgentSessionRuntimeDiagnostic(type="error", message=resolved.error))
        if resolved.model:
            options["model"] = resolved.model
            # Allow "--model <pattern>:<thinking>" as a shorthand.
            # Explicit --thinking still takes precedence (applied later).
            if not parsed.thinking and resolved.thinkingLevel:
                options["thinkingLevel"] = resolved.thinkingLevel
                cliThinkingFromModel = True

    # MISAKA fork: a role that pins its default model starts on it; scoped models do not override the pin unless
    # --models names them.
    roleDefault = bool(
        "model" not in options and scopedModels and not hasExistingSession and not parsed.models
        and settingsManager.hasPinnedModelDefault()
    )
    if not options.get("model") and len(scopedModels) > 0 and not hasExistingSession and not roleDefault:
        # Check if saved default is in scoped models - use it if so, otherwise first scoped model
        savedProvider = settingsManager.getDefaultProvider()
        savedModelId = settingsManager.getDefaultModel()
        savedModel = modelRuntime.getModel(savedProvider, savedModelId) if savedProvider and savedModelId else None
        savedInScope = next((sm for sm in scopedModels if modelsAreEqual(sm.model, savedModel)), None) if savedModel else None

        if savedInScope:
            options["model"] = savedInScope.model
            # Use thinking level from scoped model config if explicitly set
            if not parsed.thinking and savedInScope.thinkingLevel:
                options["thinkingLevel"] = savedInScope.thinkingLevel
        else:
            options["model"] = scopedModels[0].model
            # Use thinking level from first scoped model if explicitly set
            if not parsed.thinking and scopedModels[0].thinkingLevel:
                options["thinkingLevel"] = scopedModels[0].thinkingLevel

    # Thinking level from CLI (takes precedence over scoped model thinking levels set above)
    if parsed.thinking:
        options["thinkingLevel"] = parsed.thinking

    # Scoped models for Ctrl+P cycling
    # Keep thinking level undefined when not explicitly set in the model pattern.
    # Undefined means "inherit current session thinking level" during cycling.
    if len(scopedModels) > 0:
        options["scopedModels"] = [{"model": sm.model, "thinkingLevel": sm.thinkingLevel} for sm in scopedModels]

    # API key from CLI - set as a non-persistent runtime override
    # (handled by caller before createAgentSession)

    # Tools
    if parsed.noTools:
        options["noTools"] = "all"
    elif parsed.noBuiltinTools:
        options["noTools"] = "builtin"
    if parsed.tools is not None:
        options["tools"] = [*parsed.tools]
    if parsed.excludeTools is not None:
        options["excludeTools"] = [*parsed.excludeTools]

    return BuildSessionOptionsResult(options=options, cliThinkingFromModel=cliThinkingFromModel, diagnostics=diagnostics)


def resolveCliPaths(cwd: str, paths: list[str] | None) -> list[str] | None:
    return [resolvePath(value, cwd) if isLocalPath(value) else value for value in paths] if paths is not None else None


async def promptForMissingSessionCwd(
    issue: SessionCwdIssue,
    settingsManager: SettingsManager,
) -> str | None:
    return await showStartupSelector(settingsManager, formatMissingSessionCwdPrompt(issue), [
        StartupOption(label="Continue", value=issue.fallbackCwd),
        StartupOption(label="Cancel", value=None),
    ])


def createRuntimeFactory(
    parsed: Args,
    *,
    resolvedExtensionPaths: list[str] | None = None,
    resolvedPromptTemplatePaths: list[str] | None = None,
    resolvedThemePaths: list[str] | None = None,
    extensionFactories: list[Any] | None = None,
    customTools: list[Any] | None = None,
    parts: list[Any] | None = None,
    modelProfile: str | None = None,
    modelDefaultsReadOnly: bool = False,
    appMode: AppMode = "print",
    trustPromptMode: AppMode = "print",
    startupSettingsManager: SettingsManager | None = None,
) -> Callable[[dict[str, Any]], Awaitable[CreateAgentSessionRuntimeResult]]:
    """pi's ``createRuntime`` closure in ``main``, hoisted so headless sessions share it (see the module docstring).
    The keyword options are the values that closure captures from ``main``."""
    # MISAKA fork: `--agents` configures the core parts that host subagents.
    if parsed.agents is not None:
        for part in parts or ():
            configure = getattr(part, "configure_agents", None)
            if configure is not None:
                configure(parsed.agents)
    projectTrustByCwd: dict[str, bool] = {}

    async def createRuntime(runtimeOptions: dict[str, Any]) -> CreateAgentSessionRuntimeResult:
        cwd = str(runtimeOptions["cwd"])
        agentDir = str(runtimeOptions["agentDir"])
        sessionManager = runtimeOptions["sessionManager"]
        sessionStartEvent = runtimeOptions.get("sessionStartEvent")
        projectTrustContext = runtimeOptions.get("projectTrustContext")
        trustStore = ProjectTrustStore(agentDir)
        # MISAKA fork: a headless session has no startup settings manager; it reads global settings the same way.
        trustSettingsManager = startupSettingsManager or SettingsManager.create(cwd, agentDir, {"projectTrusted": False})
        isInitialRuntime = sessionStartEvent is None
        projectTrustDiagnostics: list[AgentSessionRuntimeDiagnostic] = []
        cachedProjectTrust = projectTrustByCwd.get(cwd)
        hasTrustRequiringResources = hasTrustRequiringProjectResources(cwd)
        shouldResolveProjectTrust = (
            parsed.projectTrustOverride is None and cachedProjectTrust is None and hasTrustRequiringResources
        )
        projectTrusted = (
            False
            if shouldResolveProjectTrust
            else cachedProjectTrust
            if cachedProjectTrust is not None
            else parsed.projectTrustOverride
            if parsed.projectTrustOverride is not None
            else (not hasTrustRequiringResources or trustStore.get(cwd) is True)
        )
        runtimeSettingsManager = SettingsManager.create(cwd, agentDir, {"projectTrusted": projectTrusted})

        async def resolveProjectTrust(payload: dict[str, Any]) -> bool:
            trusted = await resolveProjectTrusted(
                {
                    "cwd": cwd,
                    "trustStore": trustStore,
                    "trustOverride": parsed.projectTrustOverride,
                    "defaultProjectTrust": trustSettingsManager.getDefaultProjectTrust(),
                    "extensionsResult": payload["extensionsResult"],
                    "projectTrustContext": (
                        projectTrustContext
                        if projectTrustContext is not None
                        else createProjectTrustContext({"cwd": cwd, "mode": trustPromptMode if isInitialRuntime else appMode, "settingsManager": trustSettingsManager, "hasUI": isInitialRuntime and trustPromptMode == "interactive"})
                    ),
                    "onExtensionError": lambda message: projectTrustDiagnostics.append(
                        AgentSessionRuntimeDiagnostic(type="warning", message=message)
                    ),
                }
            )
            projectTrustByCwd[cwd] = trusted
            return trusted

        resourceLoaderOptions: dict[str, Any] = {
            "noExtensions": parsed.noExtensions,
            "disabledBuiltinExtensions": ["mcp"] if parsed.noMcp else None,
            "noPromptTemplates": parsed.noPromptTemplates,
            "noThemes": parsed.noThemes,
            "noContextFiles": parsed.noContextFiles,
            "systemPrompt": parsed.systemPrompt,
            "appendSystemPrompt": parsed.appendSystemPrompt,
        }
        # Absent keys stand for pi's `undefined` values.
        if resolvedExtensionPaths is not None:
            resourceLoaderOptions["additionalExtensionPaths"] = resolvedExtensionPaths
        if resolvedPromptTemplatePaths is not None:
            resourceLoaderOptions["additionalPromptTemplatePaths"] = resolvedPromptTemplatePaths
        if resolvedThemePaths is not None:
            resourceLoaderOptions["additionalThemePaths"] = resolvedThemePaths
        if extensionFactories is not None:
            resourceLoaderOptions["extensionFactories"] = extensionFactories

        services = await createAgentSessionServices(
            {
                "cwd": cwd,
                "agentDir": agentDir,
                "settingsManager": runtimeSettingsManager,
                "modelRuntimeSignal": TimeoutAbortSignal(15_000),
                "extensionFlagValues": parsed.unknownFlags,
                **(
                    {"resourceLoaderReloadOptions": {"resolveProjectTrust": resolveProjectTrust}}
                    if shouldResolveProjectTrust
                    else {}
                ),
                "resourceLoaderOptions": resourceLoaderOptions,
            }
        )
        settingsManager = services.settingsManager
        modelRuntime = services.modelRuntime
        resourceLoader = services.resourceLoader
        # MISAKA fork: role-bound model defaults.
        if modelProfile:
            settingsManager.bindModelProfile(modelProfile, modelRuntime)
        if modelDefaultsReadOnly:
            settingsManager.restrictModelDefaults()
        diagnostics: list[AgentSessionRuntimeDiagnostic] = [
            *projectTrustDiagnostics,
            *services.diagnostics,
            *collectSettingsDiagnostics(settingsManager),
            *(
                AgentSessionRuntimeDiagnostic(
                    type="error", message=f'Failed to load extension "{_field(item, "path")}": {_field(item, "error")}'
                )
                for item in resourceLoader.getExtensions().errors
            ),
            *(
                AgentSessionRuntimeDiagnostic(
                    type="warning", message=f'Extension package "{_field(item, "path")}": {_field(item, "warning")}'
                )
                for item in (resourceLoader.getExtensions().warnings or [])
            ),
        ]

        modelPatterns = parsed.models if parsed.models is not None else settingsManager.getEnabledModels()
        scopedModels = (
            await resolveModelScope(modelPatterns, modelRuntime, AuthOperationOptions(signal=TimeoutAbortSignal(15_000)))
            if modelPatterns and len(modelPatterns) > 0
            else []
        )
        sessionOptionsResult = buildSessionOptions(
            parsed,
            scopedModels,
            len(sessionManager.buildSessionContext().messages) > 0,
            modelRuntime,
            settingsManager,
        )
        sessionOptions = sessionOptionsResult.options
        cliThinkingFromModel = sessionOptionsResult.cliThinkingFromModel
        diagnostics.extend(sessionOptionsResult.diagnostics)

        if parsed.apiKey:
            if not sessionOptions.get("model"):
                diagnostics.append(
                    AgentSessionRuntimeDiagnostic(
                        type="error",
                        message="--api-key requires a model to be specified via --model, --provider/--model, or --models",
                    )
                )
            else:
                await modelRuntime.setRuntimeApiKey(sessionOptions["model"].provider, parsed.apiKey)

        created = await createAgentSessionFromServices(
            {
                "services": services,
                "sessionManager": sessionManager,
                "sessionStartEvent": sessionStartEvent,
                "model": sessionOptions.get("model"),
                "thinkingLevel": sessionOptions.get("thinkingLevel"),
                "scopedModels": sessionOptions.get("scopedModels"),
                "tools": sessionOptions.get("tools"),
                "excludeTools": sessionOptions.get("excludeTools"),
                "noTools": sessionOptions.get("noTools"),
                # MISAKA fork: the product's own tools and core parts.
                "customTools": customTools,
                "parts": parts,
            }
        )
        cliThinkingOverride = parsed.thinking is not None or cliThinkingFromModel
        if created["session"].model and cliThinkingOverride:
            created["session"].setThinkingLevel(created["session"].thinkingLevel)

        return CreateAgentSessionRuntimeResult(
            session=created["session"],
            services=services,
            diagnostics=diagnostics,
            extensionsResult=created.get("extensionsResult"),
            modelFallbackMessage=created.get("modelFallbackMessage"),
        )

    return createRuntime


def _field(item: Any, name: str) -> Any:
    """``{ path, error }`` destructuring of a loader error or warning, which arrives as a mapping or an object."""
    return item.get(name, "") if isinstance(item, dict) else getattr(item, name, "")


class MainOptions(TypedDict, total=False):
    extensionFactories: list[Any]
    # MISAKA fork: the product's own options.
    customTools: list[Any]
    parts: list[Any]
    modelProfile: str
    modelDefaultsReadOnly: bool


async def main(args: list[str], options: MainOptions | None = None) -> int:
    resetTimings()
    # Commands and startup selectors can load user-authored themes before runtime creation.
    setThemeJsonValidator(validateThemeJson)
    extensionFactories = (options or {}).get("extensionFactories")
    # MISAKA fork: `MISAKA_OFFLINE`, where upstream reads `PI_OFFLINE`; misaka has no version check to skip.
    offlineMode = "--offline" in args or isTruthyEnvFlag(os.environ.get("MISAKA_OFFLINE"))
    if offlineMode:
        os.environ["MISAKA_OFFLINE"] = "1"

    authExitCode = await runAuthCommand(args)
    if authExitCode is not None:
        return authExitCode

    cwd = os.getcwd()
    agentDir = get_agent_dir()
    bootstrapSettingsManager = SettingsManager.create(cwd, agentDir, {"projectTrusted": False})
    applyHttpProxySettings(bootstrapSettingsManager.getGlobalSettings().get("httpProxy"))
    configureHttpDispatcher()

    parsed = parseArgs(args)
    if len(parsed.diagnostics) > 0:
        for d in parsed.diagnostics:
            color = chalk.red if d.type == "error" else chalk.yellow
            print(color(f"{'Error' if d.type == 'error' else 'Warning'}: {d.message}"), file=sys.stderr)
        if any(d.type == "error" for d in parsed.diagnostics):
            return 1
    time("parseArgs")

    if parsed.version:
        print(VERSION)
        return 0

    if parsed.export:
        try:
            outputPath = parsed.messages[0] if len(parsed.messages) > 0 else None
            result = await exportFromFile(parsed.export, outputPath)
        except Exception as error:  # noqa: BLE001 - any export failure is reported to the user and exits 1
            print(chalk.red(f"Error: {errorMessage(error)}"), file=sys.stderr)
            return 1
        print(f"Exported to: {result}")
        return 0

    appMode = resolveAppMode(parsed, sys.stdin.isatty(), sys.stdout.isatty())
    shouldTakeOverStdout = appMode != "interactive" and not isPlainRuntimeMetadataCommand(parsed)
    if shouldTakeOverStdout:
        takeOverStdout()

    runtime = None
    runtimeHandedOff = False   # MISAKA fork: set once a mode is running; the mode disposes the runtime itself
    try:
        try:
            validateForkFlags(parsed)
            validateSessionIdFlags(parsed)

            startupSettingsManager = SettingsManager.create(cwd, agentDir)
            startupSettingsDiagnostics = collectSettingsDiagnostics(startupSettingsManager)

            if appMode == "interactive" and parsed.useTheme is not None:
                startupSettingsManager.applyOverrides({"theme": parsed.useTheme})

            # Decide the final runtime cwd before creating cwd-bound runtime services.
            # --session and --resume may select a session from another project, so project-local
            # settings, resources, provider registrations, and models must be resolved only after
            # the target session cwd is known. The startup-cwd settings manager is used only for
            # sessionDir lookup during session selection.
            sessionDir = normalizePath(parsed.sessionDir) if parsed.sessionDir else startupSettingsManager.getSessionDir()
            sessionManager = await createSessionManager(parsed, cwd, sessionDir, startupSettingsManager)
            missingSessionCwdIssue = getMissingSessionCwdIssue(sessionManager, cwd)
            if missingSessionCwdIssue:
                if appMode == "interactive":
                    selectedCwd = await promptForMissingSessionCwd(missingSessionCwdIssue, startupSettingsManager)
                    if not selectedCwd:
                        return 0
                    sessionManager = SessionManager.open(missingSessionCwdIssue.sessionFile, sessionDir, selectedCwd)
                else:
                    print(chalk.red(str(MissingSessionCwdError(missingSessionCwdIssue))), file=sys.stderr)
                    return 1
            if parsed.name is not None:
                name = normalizeSessionName(parsed.name)
                if name is None:
                    print(chalk.red("Error: --name requires a non-empty value"), file=sys.stderr)
                    return 1
                sessionManager.appendSessionInfo(name)
        except SystemExit as exitSignal:
            return int(exitSignal.code) if isinstance(exitSignal.code, int) else 1
        time("createSessionManager")

        sessionCwd = sessionManager.getCwd()
        autoTrustOnReloadCwd = (
            sessionCwd
            if parsed.projectTrustOverride is None and not hasTrustRequiringProjectResources(sessionCwd)
            else None
        )
        trustPromptMode: AppMode = "print" if parsed.help or parsed.listModels is not None else appMode

        createRuntime = createRuntimeFactory(
            parsed,
            resolvedExtensionPaths=resolveCliPaths(cwd, parsed.extensions),
            resolvedPromptTemplatePaths=resolveCliPaths(cwd, parsed.promptTemplates),
            resolvedThemePaths=resolveCliPaths(cwd, parsed.themes),
            extensionFactories=extensionFactories,
            customTools=(options or {}).get("customTools"),
            parts=(options or {}).get("parts"),
            modelProfile=(options or {}).get("modelProfile"),
            modelDefaultsReadOnly=bool((options or {}).get("modelDefaultsReadOnly")),
            appMode=appMode,
            trustPromptMode=trustPromptMode,
            startupSettingsManager=startupSettingsManager,
        )
        time("createRuntime")
        runtime = await createAgentSessionRuntime(
            createRuntime,
            {
                "cwd": sessionManager.getCwd(),
                "agentDir": agentDir,
                "sessionManager": sessionManager,
            },
        )
        time("createAgentSessionRuntime")
        services = runtime.services
        session = runtime.session
        modelFallbackMessage = runtime.modelFallbackMessage
        settingsManager = services.settingsManager
        modelRuntime = services.modelRuntime
        resourceLoader = services.resourceLoader
        setCapabilityOverrides(settingsManager.getTerminalCapabilityOverrides())
        applyHttpProxySettings(settingsManager.getGlobalSettings().get("httpProxy"))
        configureHttpDispatcher(settingsManager.getHttpIdleTimeoutMs())

        if parsed.help:
            reportDiagnostics(startupSettingsDiagnostics)
            extensionFlags = [flag for extension in resourceLoader.getExtensions().extensions for flag in extension.flags.values()]
            printHelp(extensionFlags)
            return 0

        if parsed.listModels is not None:
            reportDiagnostics(startupSettingsDiagnostics)
            searchPattern = parsed.listModels if isinstance(parsed.listModels, str) else None
            await listModels(modelRuntime, searchPattern, TimeoutAbortSignal(15_000))
            return 0

        # Read piped stdin content (if any)
        stdinContent = await readPipedStdin()
        if stdinContent is not None and appMode == "interactive":
            appMode = "print"
        time("readPipedStdin")

        initial = await prepareInitialMessage(parsed, stdinContent)
        initialMessage, initialImages = initial.initialMessage, initial.initialImages
        time("prepareInitialMessage")
        initTheme(settingsManager.getTheme(), appMode == "interactive")
        time("initTheme")

        time("resolveModelScope")
        startupDiagnostics = deduplicateDiagnostics([*startupSettingsDiagnostics, *runtime.diagnostics])
        hasRuntimeErrors = any(diagnostic.type == "error" for diagnostic in runtime.diagnostics)
        if appMode != "interactive" or hasRuntimeErrors:
            reportDiagnostics(startupDiagnostics)
        if hasRuntimeErrors:
            if any("Failed to load extension" in diagnostic.message for diagnostic in runtime.diagnostics):
                print(chalk.yellow(EXTENSION_LOAD_FAILURE_HINT), file=sys.stderr)
            return 1
        time("createAgentSession")

        if appMode != "interactive" and not session.model:
            print(chalk.red(formatNoModelsAvailableMessage()), file=sys.stderr)
            return 1

        if appMode == "interactive":
            interactiveMode = InteractiveMode(
                runtime,
                {
                    "startupDiagnostics": startupDiagnostics,
                    "modelFallbackMessage": modelFallbackMessage,
                    "autoTrustOnReloadCwd": autoTrustOnReloadCwd,
                    "initialMessage": initialMessage,
                    "initialImages": initialImages,
                    "initialMessages": list(parsed.messages),
                    "verbose": parsed.verbose,
                    "initialThemeSetting": parsed.useTheme,
                },
            )
            printTimings()
            runtimeHandedOff = True
            return await interactiveMode.run()
        printTimings()
        runtimeHandedOff = True
        return await runPrintMode(
            runtime,
            {
                "mode": toPrintOutputMode(appMode),
                "messages": list(parsed.messages),
                "initialMessage": initialMessage,
                "initialImages": initialImages,
            },
        )
    finally:
        # MISAKA fork: pi leaves via process.exit() right after --help / --list-models, which releases everything by
        # definition. Returning instead means every early return above has to release explicitly, or the extensions
        # registered on session_shutdown -- MCP server subprocesses, the LCM context flush, subagent and research
        # teardown -- never get their event. Both modes dispose the runtime themselves, so a handed-off runtime is
        # left alone rather than being sent session_shutdown twice.
        if runtime is not None and not runtimeHandedOff:
            try:
                await runtime.dispose()
            except Exception as error:  # noqa: BLE001 - shutdown must not replace the command's own result
                print(chalk.yellow(f"Warning: session shutdown failed: {errorMessage(error)}"), file=sys.stderr)
        stopThemeWatcher()
        if shouldTakeOverStdout and isStdoutTakenOver():
            restoreStdout()


__all__ = ["MainOptions", "createSessionManager", "main"]
