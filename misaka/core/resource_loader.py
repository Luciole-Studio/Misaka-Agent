"""Resource loading for prompts, skills, context files, and extensions.

Translated from pi's ``coding-agent/src/core/resource-loader.ts``.

MISAKA fork: skills belong to MISAKA's own layer system (``core/skills``), so this loader only collects the skill paths
extensions contribute (no ``updateSkillsFromPaths``/``mapSkillPath``); and ``HOST_PROVIDED_EXTENSION_PACKAGES`` with
``collectExtensionPackageWarnings`` are not ported, see ``mergeExtensionWarnings``.

MISAKA fork: a project's config directory is ``home.project_dir`` (none when the cwd is the home), where pi joins
``CONFIG_DIR_NAME`` to the cwd.
"""

from __future__ import annotations

import dataclasses
import os
import stat as stat_module
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from misaka.config import home
from misaka.core.diagnostics import ResourceCollision, ResourceDiagnostic
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    clearExtensionCache,
    createExtensionRuntime,
    loadExtensionFromFactory,
    loadExtensionsCached,
)
from misaka.core.extensions.types import (
    Extension,
    ExtensionRuntime,
    InlineExtension,
    LoadExtensionsResult,
)
from misaka.core.footer_data_provider import findGitPaths
from misaka.core.package_manager import (
    DefaultPackageManager,
    PathMetadata,
    ResolvedResource,
)
from misaka.core.prompt_templates import PromptTemplate, loadPromptTemplates
from misaka.core.settings_manager import SettingsManager
from misaka.core.source_info import (
    BUILTIN_PATH_PREFIX,
    SourceInfo,
    createSourceInfo,
    getSyntheticPathSource,
    isSyntheticPath,
)
from misaka.core.timings import resetTimings
from misaka.modes.interactive.theme.theme import Theme, loadThemeFromPath
from misaka.ui.tui.terminal_image import (
    TerminalColorMode,
    detectCapabilities,
    getTerminalColorMode,
)
from misaka.utils.js import errorMessage, jsString
from misaka.utils.node_fs import readdirEntries
from misaka.utils.paths import canonicalizePath, isLocalPath, resolvePath
from misaka.utils.text import stripBom


class ResourcePathEntry(TypedDict):
    path: str
    metadata: PathMetadata


class ResourceExtensionPaths(TypedDict, total=False):
    promptPaths: list[ResourcePathEntry]
    themePaths: list[ResourcePathEntry]
    skillPaths: list[ResourcePathEntry]


class ResourceLoaderReloadOptions(TypedDict, total=False):
    resolveProjectTrust: Callable[[dict[str, LoadExtensionsResult]], Awaitable[bool]]


class _DefaultResourceLoaderOptionsRequired(TypedDict):
    cwd: str
    agentDir: str


# MISAKA fork: upstream's `collectExtensionPackageWarnings` flags npm extension packages that bundle a host package
# as a dependency instead of a peer dependency, which loads a second copy of it; a Python package cannot carry its own
# copy of an imported package, so there is nothing to warn about and only extension warnings are merged here.
def mergeExtensionWarnings(result: LoadExtensionsResult, warnings: list[dict[str, str]]) -> None:
    # Deduplicated by path: the first position of a path, its last warning (a JS Map).
    result.warnings = list({warning["path"]: warning for warning in [*(result.warnings or []), *warnings]}.values())


def isBuiltinExtension(input: InlineExtension) -> bool:
    """Built-in extensions supply the code of ``builtin:<name>`` extension paths (see
    ``InlineExtension``)."""
    return not callable(input) and input.get("builtin") is True


def omitReplacedExtensions(
    extensions: list[Extension], warnings: list[dict[str, str]] | None = None
) -> list[Extension]:
    """Leave out replaceable extensions (see ``InlineExtension``) that share a tool, command, or
    flag name with another extension. For example, a third-party MCP extension that registers
    ``/mcp`` replaces the built-in MCP extension instead of both connecting the same servers."""

    def names(extension: Extension) -> list[str]:
        return [
            *(f"tool:{name}" for name in extension.tools),
            *(f"command:{name}" for name in extension.commands),
            *(f"flag:{name}" for name in extension.flags),
        ]

    taken = {name: extension for extension in extensions if not extension.replaceable for name in names(extension)}
    kept: list[Extension] = []
    for extension in extensions:
        if not extension.replaceable:
            kept.append(extension)
            continue
        replacement = next(((name, taken[name]) for name in names(extension) if name in taken), None)
        if replacement is None:
            kept.append(extension)
            continue
        if extension.path.startswith(BUILTIN_PATH_PREFIX) and warnings is not None:
            builtinName = extension.path[len(BUILTIN_PATH_PREFIX):]
            kind, rawName = replacement[0].split(":")[:2]
            registeredName = f"/{rawName}" if kind == "command" else f"--{rawName}" if kind == "flag" else rawName
            # MISAKA fork: misaka has no `pi config` screen; built-in extensions are enabled in the
            # `extensions` setting.
            warnings.append({
                "path": extension.path,
                "warning": (
                    f"Extension {replacement[1].path} registers {kind} `{registeredName}`, so built-in extension "
                    f"`{builtinName}` was not loaded. To use `{builtinName}`, make sure the `extensions` setting "
                    f"does not disable it (`-{BUILTIN_PATH_PREFIX}{builtinName}`), then disable or remove the "
                    "existing extension. We recommend only having one or the other loaded at a time."
                ),
            })
    return kept


class ResourceLoader(Protocol):
    def getExtensions(self) -> LoadExtensionsResult: ...

    def getExtensionSkillPaths(self) -> list[str]: ...

    def getExtensionSkillResources(self) -> list[ResourcePathEntry]: ...

    def getPrompts(self) -> PromptsResult: ...

    def getThemes(self) -> ThemesResult: ...

    def getAgentsFiles(self) -> AgentsFilesResult: ...

    def getSystemPrompt(self) -> str | None: ...

    def getSystemPromptSource(self) -> dict[str, str] | None: ...

    def getAppendSystemPrompt(self) -> list[str]: ...

    def getAppendSystemPromptSources(self) -> list[dict[str, str]]: ...

    def extendResources(self, paths: ResourceExtensionPaths) -> None: ...

    async def reload(self, options: ResourceLoaderReloadOptions | None = None) -> None: ...


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _error_message(error: object, fallback: str) -> str:
    """``error instanceof Error ? error.message : fallback``."""
    return errorMessage(error) if isinstance(error, BaseException) else fallback


def resolvePromptInput(input: str | None, description: str) -> str | None:
    if not input:
        return None
    if os.path.exists(input):
        try:
            return stripBom(_readFileUtf8(input))
        except Exception as error:  # noqa: BLE001 - upstream warns on any read failure
            _warn(f"Warning: Could not read {description} file {input}: {jsString(error)}")
            return input

    return input


def _readFileUtf8(path: str) -> str:
    """``readFileSync(path, "utf-8")``: bytes that are not UTF-8 become U+FFFD, never an error."""
    with open(path, "rb") as file:
        return file.read().decode("utf-8", errors="replace")


def loadContextFileFromDir(dir: str) -> dict[str, str] | None:
    for filename in CONTEXT_FILE_NAMES:
        filePath = os.path.join(dir, filename)
        if os.path.exists(filePath):
            try:
                # Stat before reading: a FIFO named AGENTS.md would block the read forever, and
                # `loadProjectContextFiles` walks every ancestor up to the filesystem root.
                if not stat_module.S_ISREG(os.stat(filePath).st_mode):
                    continue
                return {"path": filePath, "content": stripBom(_readFileUtf8(filePath))}
            except Exception as error:  # noqa: BLE001 - upstream warns on any read failure
                _warn(f"Warning: Could not read {filePath}: {jsString(error)}")
    return None


ResourceLoaderLike = ResourceLoader


__all__ = [
    "DefaultResourceLoader",
    "DefaultResourceLoaderOptions",
    "ResourceCollision",
    "ResourceDiagnostic",
    "ResourceExtensionPaths",
    "ResourceLoader",
    "ResourceLoaderLike",
    "ResourceLoaderReloadOptions",
    "isBuiltinExtension",
]

# Kept for misaka/core/subagent, which imports these names.
resolve_prompt_input = resolvePromptInput


def findShadowedContextFile(cwd: str) -> str | None:
    gitPaths = findGitPaths(cwd)
    if gitPaths is None:
        return None

    commonGitDir = canonicalizePath(gitPaths.commonGitDir)
    worktreeRoot = canonicalizePath(gitPaths.repoDir)
    mainRepoRoot = os.path.dirname(commonGitDir)
    if not worktreeRoot.startswith(f"{mainRepoRoot}{os.sep}"):
        return None
    if canonicalizePath(os.path.join(mainRepoRoot, ".git")) != commonGitDir:
        return None

    worktreeContextFile = loadContextFileFromDir(worktreeRoot)
    if worktreeContextFile is None:
        return None
    return os.path.join(mainRepoRoot, os.path.basename(worktreeContextFile["path"]))


def loadProjectContextFiles(options: dict[str, str]) -> list[dict[str, str]]:
    resolvedCwd = resolvePath(options["cwd"])
    resolvedAgentDir = resolvePath(options["agentDir"])
    contextFiles: list[dict[str, str]] = []
    seenPaths: set[str] = set()

    globalContext = loadContextFileFromDir(resolvedAgentDir)
    if globalContext is not None:
        contextFiles.append(globalContext)
        seenPaths.add(globalContext["path"])

    ancestorContextFiles: list[dict[str, str]] = []
    shadowedContextFile = findShadowedContextFile(resolvedCwd)
    currentDir = resolvedCwd
    root = os.path.abspath(os.sep)
    while True:
        contextFile = loadContextFileFromDir(currentDir)
        # Compare the directory's real path, not the file's: a worktree AGENTS.md that
        # symlinks to the main repo's file resolves to the shadowed path itself.
        isShadowed = (
            shadowedContextFile is not None
            and contextFile is not None
            and os.path.join(canonicalizePath(currentDir), os.path.basename(contextFile["path"])) == shadowedContextFile
        )
        if contextFile is not None and not isShadowed and contextFile["path"] not in seenPaths:
            ancestorContextFiles.insert(0, contextFile)
            seenPaths.add(contextFile["path"])
        if currentDir == root:
            break
        parentDir = os.path.abspath(os.path.join(currentDir, ".."))
        if parentDir == currentDir:
            break
        currentDir = parentDir

    contextFiles.extend(ancestorContextFiles)
    return contextFiles


class DefaultResourceLoaderOptions(_DefaultResourceLoaderOptionsRequired, total=False):
    settingsManager: SettingsManager
    eventBus: Any
    additionalExtensionPaths: list[str]
    additionalPromptTemplatePaths: list[str]
    additionalThemePaths: list[str]
    extensionFactories: list[InlineExtension]
    noExtensions: bool
    # Built-in extensions not to load, by name (such as `mcp`), even when settings or `-e` enable them.
    disabledBuiltinExtensions: list[str]
    noPromptTemplates: bool
    noThemes: bool
    noContextFiles: bool
    systemPrompt: str
    appendSystemPrompt: list[str]
    extensionsOverride: Any
    promptsOverride: Any
    themesOverride: Any
    agentsFilesOverride: Any
    systemPromptOverride: Any
    appendSystemPromptOverride: Any


class PromptsResult(TypedDict):
    prompts: list[PromptTemplate]
    diagnostics: list[ResourceDiagnostic]


class ThemesResult(TypedDict):
    themes: list[Theme]
    diagnostics: list[ResourceDiagnostic]


class AgentsFilesResult(TypedDict):
    agentsFiles: list[dict[str, str]]


@dataclass(slots=True)
class DefaultResourceLoader:
    cwd: str
    agentDir: str
    settingsManager: SettingsManager
    packageManager: DefaultPackageManager
    eventBus: Any | None = None
    additionalExtensionPaths: list[str] = field(default_factory=list)
    additionalPromptTemplatePaths: list[str] = field(default_factory=list)
    additionalThemePaths: list[str] = field(default_factory=list)
    extensionFactories: list[InlineExtension] = field(default_factory=list)
    builtinExtensions: dict[str, Any] = field(default_factory=dict)
    noExtensions: bool = False
    disabledBuiltinExtensions: set[str] = field(default_factory=set)
    noPromptTemplates: bool = False
    noThemes: bool = False
    noContextFiles: bool = False
    systemPromptSource: str | None = None
    appendSystemPromptSource: list[str] | None = None
    extensionsOverride: Any = None
    promptsOverride: Any = None
    themesOverride: Any = None
    agentsFilesOverride: Any = None
    systemPromptOverride: Any = None
    appendSystemPromptOverride: Any = None
    extensionsResult: LoadExtensionsResult = field(
        default_factory=lambda: LoadExtensionsResult(extensions=[], errors=[], runtime=createExtensionRuntime())
    )
    prompts: list[PromptTemplate] = field(default_factory=list)
    promptDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    themes: list[Theme] = field(default_factory=list)
    themeDiagnostics: list[ResourceDiagnostic] = field(default_factory=list)
    agentsFiles: list[dict[str, str]] = field(default_factory=list)
    systemPrompt: str | None = None
    systemPromptSourcePath: str | None = None
    appendSystemPrompt: list[str] = field(default_factory=list)
    appendSystemPromptSourcePaths: list[str] = field(default_factory=list)
    lastPromptPaths: list[str] = field(default_factory=list)
    lastThemePaths: list[str] = field(default_factory=list)
    extensionSkillPaths: list[str] = field(default_factory=list)
    extensionSkillResources: list[ResourcePathEntry] = field(default_factory=list)
    extensionPromptSourceInfos: dict[str, SourceInfo] = field(default_factory=dict)
    extensionThemeSourceInfos: dict[str, SourceInfo] = field(default_factory=dict)
    resourceMetadataByPath: dict[str, PathMetadata] = field(default_factory=dict)
    loaded: bool = False

    def __init__(self, options: DefaultResourceLoaderOptions) -> None:
        self.cwd = resolvePath(options["cwd"])
        self.agentDir = resolvePath(options["agentDir"])
        self.settingsManager = options.get("settingsManager") or SettingsManager.create(self.cwd, self.agentDir)
        factories = list(options.get("extensionFactories", []))
        self.extensionFactories = [input_ for input_ in factories if not isBuiltinExtension(input_)]
        self.builtinExtensions = {input_["name"]: input_ for input_ in factories if isBuiltinExtension(input_)}
        self.packageManager = DefaultPackageManager(
            {
                "cwd": self.cwd,
                "agentDir": self.agentDir,
                "settingsManager": self.settingsManager,
                "builtinExtensions": list(self.builtinExtensions),
            }
        )
        self.eventBus = options.get("eventBus") or createEventBus()
        self.additionalExtensionPaths = list(options.get("additionalExtensionPaths", []))
        self.additionalPromptTemplatePaths = list(options.get("additionalPromptTemplatePaths", []))
        self.additionalThemePaths = list(options.get("additionalThemePaths", []))
        self.noExtensions = options.get("noExtensions", False)
        self.disabledBuiltinExtensions = set(options.get("disabledBuiltinExtensions") or [])
        self.noPromptTemplates = options.get("noPromptTemplates", False)
        self.noThemes = options.get("noThemes", False)
        self.noContextFiles = options.get("noContextFiles", False)
        self.systemPromptSource = options.get("systemPrompt")
        self.appendSystemPromptSource = options.get("appendSystemPrompt")
        self.extensionsOverride = options.get("extensionsOverride")
        self.promptsOverride = options.get("promptsOverride")
        self.themesOverride = options.get("themesOverride")
        self.agentsFilesOverride = options.get("agentsFilesOverride")
        self.systemPromptOverride = options.get("systemPromptOverride")
        self.appendSystemPromptOverride = options.get("appendSystemPromptOverride")
        self.extensionsResult = LoadExtensionsResult(extensions=[], errors=[], runtime=createExtensionRuntime())
        self.prompts = []
        self.promptDiagnostics = []
        self.themes = []
        self.themeDiagnostics = []
        self.agentsFiles = []
        self.systemPrompt = None
        self.systemPromptSourcePath = None
        self.appendSystemPrompt = []
        self.appendSystemPromptSourcePaths = []
        self.lastPromptPaths = []
        self.lastThemePaths = []
        self.extensionSkillPaths = []
        self.extensionSkillResources = []
        self.extensionPromptSourceInfos = {}
        self.extensionThemeSourceInfos = {}
        self.resourceMetadataByPath = {}
        self.loaded = False

    def getExtensions(self) -> LoadExtensionsResult:
        return self.extensionsResult

    def getPrompts(self) -> PromptsResult:
        return {"prompts": self.prompts, "diagnostics": self.promptDiagnostics}

    def getThemes(self) -> ThemesResult:
        return {"themes": self.themes, "diagnostics": self.themeDiagnostics}

    def getAgentsFiles(self) -> AgentsFilesResult:
        return {"agentsFiles": self.agentsFiles}

    def getSystemPrompt(self) -> str | None:
        return self.systemPrompt

    def getSystemPromptSource(self) -> dict[str, str] | None:
        return {"path": self.systemPromptSourcePath} if self.systemPromptSourcePath is not None else None

    def getAppendSystemPrompt(self) -> list[str]:
        return self.appendSystemPrompt

    def getAppendSystemPromptSources(self) -> list[dict[str, str]]:
        return [{"path": path} for path in self.appendSystemPromptSourcePaths]

    def extendResources(self, paths: ResourceExtensionPaths) -> None:
        promptPaths = self.normalizeExtensionPaths(paths.get("promptPaths", []))
        themePaths = self.normalizeExtensionPaths(paths.get("themePaths", []))

        for entry in promptPaths:
            self.extensionPromptSourceInfos[entry["path"]] = createSourceInfo(entry["path"], entry["metadata"])
        for entry in themePaths:
            self.extensionThemeSourceInfos[entry["path"]] = createSourceInfo(entry["path"], entry["metadata"])

        if promptPaths:
            self.lastPromptPaths = self.mergePaths(self.lastPromptPaths, [entry["path"] for entry in promptPaths])
            self.updatePromptsFromPaths(self.lastPromptPaths, self.resourceMetadataByPath)

        if themePaths:
            self.lastThemePaths = self.mergePaths(self.lastThemePaths, [entry["path"] for entry in themePaths])
            self.updateThemesFromPaths(self.lastThemePaths, self.resourceMetadataByPath)

        # MISAKA fork: skills are misaka's own layer system (core/skills), not this loader's;
        # the roots an extension contributes are kept here for that system to read.
        skillPaths = self.normalizeExtensionPaths(paths.get("skillPaths", []))
        if skillPaths:
            for entry in skillPaths:
                if entry not in self.extensionSkillResources:
                    self.extensionSkillResources.append(entry)
            self.extensionSkillPaths = self.mergePaths(
                self.getExtensionSkillPaths(), [entry["path"] for entry in skillPaths]
            )

    def getExtensionSkillPaths(self) -> list[str]:
        return list(self.extensionSkillPaths)

    def getExtensionSkillResources(self) -> list[ResourcePathEntry]:
        return [{"path": e["path"], "metadata": dict(e["metadata"])} for e in self.extensionSkillResources]

    async def loadProjectTrustExtensions(self) -> LoadExtensionsResult:
        """Bootstrap only user/CLI/inline extensions while project settings are gated."""
        self.settingsManager.setProjectTrusted(False)
        await self.settingsManager.reload()
        return await self.loadCurrentExtensionSet({"includeInlineFactories": True})

    async def reload(self, options: ResourceLoaderReloadOptions | None = None) -> None:
        resetTimings("extensions")

        if self.loaded:
            clearExtensionCache()

        preTrustExtensions: LoadExtensionsResult | None = None
        resolveProjectTrust = (options or {}).get("resolveProjectTrust")
        if resolveProjectTrust is not None:
            preTrustExtensions = await self.loadProjectTrustExtensions()
            projectTrusted = await resolveProjectTrust(
                {"extensionsResult": preTrustExtensions}
            )
            self.settingsManager.setProjectTrusted(projectTrusted)

        await self.settingsManager.reload()
        resolvedPaths = await self.packageManager.resolve()
        cliExtensionPaths = await self.packageManager.resolveExtensionSources(
            self.additionalExtensionPaths,
            {"temporary": True},
        )

        self.resourceMetadataByPath = {}
        metadataByPath = self.resourceMetadataByPath
        self.extensionSkillPaths = []
        self.extensionSkillResources = []
        self.extensionPromptSourceInfos = {}
        self.extensionThemeSourceInfos = {}

        def getEnabledResources(resources: list[ResolvedResource]) -> list[ResolvedResource]:
            for resource in resources:
                if resource.path not in metadataByPath:
                    metadataByPath[resource.path] = resource.metadata
            return [resource for resource in resources if resource.enabled]

        def enabled_paths(resources: list[ResolvedResource]) -> list[str]:
            return [resource.path for resource in getEnabledResources(resources)]

        enabledExtensions = enabled_paths(resolvedPaths.extensions)
        enabledPrompts = enabled_paths(resolvedPaths.prompts)
        enabledThemes = enabled_paths(resolvedPaths.themes)

        for resource in cliExtensionPaths.extensions:
            if resource.path not in metadataByPath:
                metadataByPath[resource.path] = {"source": "cli", "scope": "temporary", "origin": "top-level"}

        cliEnabledExtensions = enabled_paths(cliExtensionPaths.extensions)
        cliEnabledPrompts = enabled_paths(cliExtensionPaths.prompts)
        cliEnabledThemes = enabled_paths(cliExtensionPaths.themes)

        extensionPaths = [
            path
            for path in (
                cliEnabledExtensions
                if self.noExtensions
                else self.mergePaths(cliEnabledExtensions, enabledExtensions)
            )
            if not path.startswith(BUILTIN_PATH_PREFIX)
            or path[len(BUILTIN_PATH_PREFIX) :] not in self.disabledBuiltinExtensions
        ]

        extensionsResult = await self.loadFinalExtensionSet(
            extensionPaths,
            preTrustExtensions,
        )

        for rawPath in self.additionalExtensionPaths:
            if isLocalPath(rawPath):
                resolved = self.resolveResourcePath(rawPath)
                if not os.path.exists(resolved):
                    extensionsResult.errors.append(
                        {"path": resolved, "error": f"Extension path does not exist: {resolved}"}
                    )

        self.extensionsResult = (
            self.extensionsOverride(extensionsResult)
            if callable(self.extensionsOverride)
            else extensionsResult
        )
        self.applyExtensionSourceInfo(self.extensionsResult.extensions, metadataByPath)

        promptPaths = (
            self.mergePaths(cliEnabledPrompts, self.additionalPromptTemplatePaths)
            if self.noPromptTemplates
            else self.mergePaths([*cliEnabledPrompts, *enabledPrompts], self.additionalPromptTemplatePaths)
        )
        self.lastPromptPaths = promptPaths
        self.updatePromptsFromPaths(promptPaths, metadataByPath)
        for rawPath in self.additionalPromptTemplatePaths:
            if isLocalPath(rawPath):
                resolved = self.resolveResourcePath(rawPath)
                if not os.path.exists(resolved) and not any(
                    diagnostic.path == resolved for diagnostic in self.promptDiagnostics
                ):
                    self.promptDiagnostics.append(
                        ResourceDiagnostic(
                            type="error",
                            message="Prompt template path does not exist",
                            path=resolved,
                        )
                    )

        themePaths = (
            self.mergePaths(cliEnabledThemes, self.additionalThemePaths)
            if self.noThemes
            else self.mergePaths([*cliEnabledThemes, *enabledThemes], self.additionalThemePaths)
        )
        self.lastThemePaths = themePaths
        self.updateThemesFromPaths(themePaths, metadataByPath)
        for rawPath in self.additionalThemePaths:
            resolved = self.resolveResourcePath(rawPath)
            if not os.path.exists(resolved) and not any(
                diagnostic.path == resolved for diagnostic in self.themeDiagnostics
            ):
                self.themeDiagnostics.append(
                    ResourceDiagnostic(type="error", message="Theme path does not exist", path=resolved)
                )

        agentsFiles = {
            "agentsFiles": (
                [] if self.noContextFiles else loadProjectContextFiles({"cwd": self.cwd, "agentDir": self.agentDir})
            )
        }
        resolvedAgentsFiles = (
            self.agentsFilesOverride(agentsFiles) if callable(self.agentsFilesOverride) else agentsFiles
        )
        self.agentsFiles = resolvedAgentsFiles["agentsFiles"]

        discoveredSystemPrompt = self.discoverSystemPromptFile()
        systemPromptSource = self.systemPromptSource if self.systemPromptSource is not None else discoveredSystemPrompt
        baseSystemPrompt = resolvePromptInput(systemPromptSource, "system prompt")
        self.systemPrompt = (
            self.systemPromptOverride(baseSystemPrompt)
            if callable(self.systemPromptOverride)
            else baseSystemPrompt
        )
        self.systemPromptSourcePath = (
            resolvePath(systemPromptSource)
            if systemPromptSource is not None and os.path.exists(systemPromptSource)
            else None
        )

        discoveredAppendPrompt = self.discoverAppendSystemPromptFile()
        appendSources = self.appendSystemPromptSource if self.appendSystemPromptSource is not None else (
            [discoveredAppendPrompt] if discoveredAppendPrompt else []
        )
        baseAppend = [
            content
            for source in appendSources
            if (content := resolvePromptInput(source, "append system prompt")) is not None
        ]
        self.appendSystemPrompt = (
            self.appendSystemPromptOverride(baseAppend)
            if callable(self.appendSystemPromptOverride)
            else baseAppend
        )
        self.appendSystemPromptSourcePaths = [
            resolvePath(source) for source in appendSources if os.path.exists(source)
        ]
        self.loaded = True

    async def loadCurrentExtensionSet(self, options: Mapping[str, bool]) -> LoadExtensionsResult:
        resolvedPaths = await self.packageManager.resolve()
        cliExtensionPaths = await self.packageManager.resolveExtensionSources(
            self.additionalExtensionPaths,
            {"temporary": True},
        )
        enabledExtensions = [item.path for item in resolvedPaths.extensions if item.enabled]
        cliEnabledExtensions = [item.path for item in cliExtensionPaths.extensions if item.enabled]
        # Built-in extensions wait for the final pass: project settings can disable them, and a
        # loaded extension cannot be unloaded.
        extensionPaths = [
            path
            for path in (
                cliEnabledExtensions
                if self.noExtensions
                else self.mergePaths(cliEnabledExtensions, enabledExtensions)
            )
            if not path.startswith(BUILTIN_PATH_PREFIX)
        ]
        result = await loadExtensionsCached(extensionPaths, self.cwd, self.eventBus)
        if not options["includeInlineFactories"]:
            return result

        inline = await self.loadExtensionFactories(result.runtime)
        result.extensions.extend(inline["extensions"])
        result.errors.extend(inline["errors"])
        replacementWarnings: list[dict[str, str]] = []
        result.extensions = omitReplacedExtensions(result.extensions, replacementWarnings)
        mergeExtensionWarnings(result, replacementWarnings)
        return result

    def resolveExtensionLoadPath(self, path: str) -> str:
        # Keep the pre-trust/final-pass identity key byte-for-byte aligned with
        # extensions.loader._load_extension(), otherwise Unicode-space paths can
        # activate the same factory twice.
        return path if isSyntheticPath(path) else resolvePath(path, self.cwd, {"normalizeUnicodeSpaces": True})

    async def loadExtensionPaths(
        self, paths: list[str], runtime: ExtensionRuntime | None = None
    ) -> LoadExtensionsResult:
        """Load extension paths: files from disk and ``builtin:<name>`` paths from the built-in
        extensions."""

        def isBuiltinPath(path: str) -> bool:
            return path.startswith(BUILTIN_PATH_PREFIX)

        result = await loadExtensionsCached(
            [path for path in paths if not isBuiltinPath(path)], self.cwd, self.eventBus, runtime
        )
        for path in [path for path in paths if isBuiltinPath(path)]:
            builtin = self.builtinExtensions.get(path[len(BUILTIN_PATH_PREFIX):])
            if builtin is None:
                result.errors.append({"path": path, "error": f"Unknown built-in extension: {path}"})
                continue
            try:
                extension = await loadExtensionFromFactory(
                    builtin["factory"], self.cwd, self.eventBus, result.runtime, path
                )
                extension.hidden = True
                extension.replaceable = builtin.get("replaceable") is True
                result.extensions.append(extension)
            except Exception as error:  # noqa: BLE001 - extension code: a failing load is reported as an error entry
                result.errors.append({"path": path, "error": _error_message(error, "failed to load extension")})
        return result

    async def loadFinalExtensionSet(
        self,
        extensionPaths: list[str],
        preTrustExtensions: LoadExtensionsResult | None,
    ) -> LoadExtensionsResult:
        # Without a pre-trust pass nothing is preloaded, and inline extensions load here.
        preloaded = preTrustExtensions.extensions if preTrustExtensions is not None else []
        preloadedByPath = {
            extension.resolvedPath: extension for extension in preloaded if not extension.path.startswith("<inline:")
        }
        failedPreloadPaths = {
            self.resolveExtensionLoadPath(error["path"])
            for error in (preTrustExtensions.errors if preTrustExtensions is not None else [])
        }
        remainingPaths = [
            path
            for path in extensionPaths
            if self.resolveExtensionLoadPath(path) not in preloadedByPath
            and self.resolveExtensionLoadPath(path) not in failedPreloadPaths
        ]
        remaining = await self.loadExtensionPaths(
            remainingPaths, preTrustExtensions.runtime if preTrustExtensions is not None else None
        )
        loadedByPath = dict(preloadedByPath)
        loadedByPath.update({extension.resolvedPath: extension for extension in remaining.extensions})

        inline = (
            {"extensions": [extension for extension in preloaded if extension.path.startswith("<inline:")], "errors": []}
            if preTrustExtensions is not None
            else await self.loadExtensionFactories(remaining.runtime)
        )
        ordered = [
            loadedByPath[resolved]
            for path in extensionPaths
            if (resolved := self.resolveExtensionLoadPath(path)) in loadedByPath
        ]
        ordered.extend(inline["extensions"])

        replacementWarnings: list[dict[str, str]] = []
        result = LoadExtensionsResult(
            extensions=omitReplacedExtensions(ordered, replacementWarnings),
            errors=[
                *(preTrustExtensions.errors if preTrustExtensions is not None else []),
                *remaining.errors,
                *inline["errors"],
            ],
            warnings=[
                *(preTrustExtensions.warnings if preTrustExtensions is not None else []),
                *(remaining.warnings or []),
            ],
            runtime=remaining.runtime,
        )
        mergeExtensionWarnings(result, replacementWarnings)
        self.addExtensionConflictDiagnostics(result)
        return result

    def addExtensionConflictDiagnostics(self, extensionsResult: LoadExtensionsResult) -> None:
        for conflict in self.detectExtensionConflicts(extensionsResult.extensions):
            extensionsResult.errors.append({"path": conflict["path"], "error": conflict["message"]})

    def normalizeExtensionPaths(self, entries: list[ResourcePathEntry]) -> list[ResourcePathEntry]:
        normalized: list[ResourcePathEntry] = []
        for entry in entries:
            metadata = dict(entry.get("metadata", {}))
            if metadata.get("baseDir") is not None:
                metadata["baseDir"] = self.resolveResourcePath(str(metadata["baseDir"]))
            normalized.append(
                {
                    "path": self.resolveResourcePath(entry["path"]),
                    "metadata": metadata,  # type: ignore[typeddict-item]
                }
            )
        return normalized

    def updatePromptsFromPaths(
        self,
        promptPaths: list[str],
        metadataByPath: dict[str, PathMetadata] | None = None,
    ) -> None:
        if self.noPromptTemplates and not promptPaths:
            promptsResult = {"prompts": [], "diagnostics": []}
        else:
            loaded = loadPromptTemplates(
                {
                    "cwd": self.cwd,
                    "agentDir": self.agentDir,
                    "promptPaths": promptPaths,
                    "includeDefaults": False,
                }
            )
            deduped = self.dedupePrompts(loaded.templates)
            promptsResult = {
                "prompts": deduped["prompts"],
                "diagnostics": [*loaded.diagnostics, *deduped["diagnostics"]],
            }

        resolved = self.promptsOverride(promptsResult) if callable(self.promptsOverride) else promptsResult
        self.prompts = [
            PromptTemplate(
                name=prompt.name,
                description=prompt.description,
                content=prompt.content,
                sourceInfo=self.findSourceInfoForPath(
                    prompt.filePath,
                    self.extensionPromptSourceInfos,
                    metadataByPath,
                )
                or prompt.sourceInfo
                or self.getDefaultSourceInfoForPath(prompt.filePath),
                filePath=prompt.filePath,
                argumentHint=prompt.argumentHint,
            )
            for prompt in resolved["prompts"]
        ]
        self.promptDiagnostics = list(resolved["diagnostics"])

    def updateThemesFromPaths(
        self,
        themePaths: list[str],
        metadataByPath: dict[str, PathMetadata] | None = None,
    ) -> None:
        if self.noThemes and not themePaths:
            themesResult = {"themes": [], "diagnostics": []}
        else:
            # Theme construction only needs trueColor, so skip the unrelated tmux hyperlink probe.
            colorMode = getTerminalColorMode(
                dataclasses.replace(
                    detectCapabilities(lambda: False),
                    **self.settingsManager.getTerminalCapabilityOverrides(),
                )
            )
            loaded = self.loadThemes(themePaths, False, colorMode)
            deduped = self.dedupeThemes(loaded["themes"])
            themesResult = {
                "themes": deduped["themes"],
                "diagnostics": [*loaded["diagnostics"], *deduped["diagnostics"]],
            }

        resolved = self.themesOverride(themesResult) if callable(self.themesOverride) else themesResult
        self.themes = []
        for theme in resolved["themes"]:
            sourcePath = theme.sourcePath
            theme.sourceInfo = (
                self.findSourceInfoForPath(
                    sourcePath,
                    self.extensionThemeSourceInfos,
                    metadataByPath,
                )
                if sourcePath
                else None
            ) or theme.sourceInfo
            if sourcePath and theme.sourceInfo is None:
                theme.sourceInfo = self.getDefaultSourceInfoForPath(sourcePath)
            self.themes.append(theme)
        self.themeDiagnostics = list(resolved["diagnostics"])

    def applyExtensionSourceInfo(
        self,
        extensions: list[Extension],
        metadataByPath: dict[str, PathMetadata] | None = None,
    ) -> None:
        for extension in extensions:
            extension.sourceInfo = (
                self.findSourceInfoForPath(extension.path, None, metadataByPath)
                or self.getDefaultSourceInfoForPath(extension.path)
            )
            for command in extension.commands.values():
                command.sourceInfo = extension.sourceInfo
            for tool in extension.tools.values():
                tool.sourceInfo = extension.sourceInfo

    def findSourceInfoForPath(
        self,
        resourcePath: str,
        extraSourceInfos: dict[str, SourceInfo] | None = None,
        metadataByPath: dict[str, PathMetadata] | None = None,
    ) -> SourceInfo | None:
        if not resourcePath:
            return None
        if isSyntheticPath(resourcePath):
            return self.getDefaultSourceInfoForPath(resourcePath)

        normalizedResourcePath = os.path.abspath(resourcePath)
        for sourcePath, sourceInfo in (extraSourceInfos or {}).items():
            normalizedSourcePath = os.path.abspath(sourcePath)
            if normalizedResourcePath == normalizedSourcePath or normalizedResourcePath.startswith(
                f"{normalizedSourcePath}{os.sep}"
            ):
                return SourceInfo(
                    path=resourcePath,
                    source=sourceInfo.source,
                    scope=sourceInfo.scope,
                    origin=sourceInfo.origin,
                    baseDir=sourceInfo.baseDir,
                )

        if metadataByPath is not None:
            exact = metadataByPath.get(normalizedResourcePath) or metadataByPath.get(resourcePath)
            if exact is not None:
                return createSourceInfo(resourcePath, exact)

            for sourcePath, metadata in metadataByPath.items():
                normalizedSourcePath = os.path.abspath(sourcePath)
                if normalizedResourcePath == normalizedSourcePath or normalizedResourcePath.startswith(
                    f"{normalizedSourcePath}{os.sep}"
                ):
                    return createSourceInfo(resourcePath, metadata)
        return None

    def getDefaultSourceInfoForPath(self, filePath: str) -> SourceInfo:
        syntheticSource = getSyntheticPathSource(filePath)
        if syntheticSource:
            return SourceInfo(path=filePath, source=syntheticSource, scope="temporary", origin="top-level", baseDir=None)

        normalizedPath = os.path.abspath(filePath)
        agentRoots = [
            os.path.join(self.agentDir, "skills"),
            os.path.join(self.agentDir, "prompts"),
            os.path.join(self.agentDir, "themes"),
            os.path.join(self.agentDir, "extensions"),
        ]
        projectDir = home.project_dir(self.cwd)
        projectRoots = (
            [
                os.path.join(projectDir, "skills"),
                os.path.join(projectDir, "prompts"),
                os.path.join(projectDir, "themes"),
                os.path.join(projectDir, "extensions"),
            ]
            if projectDir is not None
            else []
        )

        for root in agentRoots:
            if self.isUnderPath(normalizedPath, root):
                return SourceInfo(path=filePath, source="local", scope="user", origin="top-level", baseDir=root)

        for root in projectRoots:
            if self.isUnderPath(normalizedPath, root):
                return SourceInfo(path=filePath, source="local", scope="project", origin="top-level", baseDir=root)

        stats = os.stat(normalizedPath)
        baseDir = (
            normalizedPath
            if stat_module.S_ISDIR(stats.st_mode)
            else os.path.abspath(os.path.join(normalizedPath, ".."))
        )
        return SourceInfo(
            path=filePath,
            source="local",
            scope="temporary",
            origin="top-level",
            baseDir=baseDir,
        )

    def mergePaths(self, primary: list[str], additional: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for value in [*primary, *additional]:
            resolved = self.resolveResourcePath(value)
            canonical = canonicalizePath(resolved)
            if canonical in seen:
                continue
            seen.add(canonical)
            merged.append(resolved)
        return merged

    def resolveResourcePath(self, p: str) -> str:
        return p if isSyntheticPath(p) else resolvePath(p, self.cwd, {"trim": True})

    def loadThemes(self, paths: list[str], includeDefaults: bool, colorMode: TerminalColorMode) -> dict[str, list[Any]]:
        themes: list[Theme] = []
        diagnostics: list[ResourceDiagnostic] = []
        if includeDefaults:
            # MISAKA fork: a project's config directory comes from `home.project_dir` (none when the cwd is the home).
            projectDir = home.project_dir(self.cwd)
            defaultDirs = [os.path.join(self.agentDir, "themes"), *([os.path.join(projectDir, "themes")] if projectDir else [])]

            for dir in defaultDirs:
                self.loadThemesFromDir(dir, themes, diagnostics, colorMode)

        for p in paths:
            resolved = self.resolveResourcePath(p)
            if not os.path.exists(resolved):
                diagnostics.append(ResourceDiagnostic(type="warning", message="theme path does not exist", path=resolved))
                continue

            try:
                stats = os.stat(resolved)
                if stat_module.S_ISDIR(stats.st_mode):
                    self.loadThemesFromDir(resolved, themes, diagnostics, colorMode)
                elif stat_module.S_ISREG(stats.st_mode) and resolved.endswith(".json"):
                    self.loadThemeFromFile(resolved, themes, diagnostics, colorMode)
                else:
                    diagnostics.append(
                        ResourceDiagnostic(type="warning", message="theme path is not a json file", path=resolved)
                    )
            except Exception as error:  # noqa: BLE001 - upstream reports any failure as a diagnostic
                message = _error_message(error, "failed to read theme path")
                diagnostics.append(ResourceDiagnostic(type="warning", message=message, path=resolved))

        return {"themes": themes, "diagnostics": diagnostics}

    def loadThemesFromDir(
        self,
        dir: str,
        themes: list[Theme],
        diagnostics: list[ResourceDiagnostic],
        colorMode: TerminalColorMode,
    ) -> None:
        if not os.path.exists(dir):
            return
        try:
            for entry in readdirEntries(dir):
                isFile = entry.is_file(follow_symlinks=False)
                if entry.is_symlink():
                    try:
                        isFile = os.path.isfile(entry.path)
                    except Exception:  # noqa: BLE001, S112 - an unreadable entry is skipped
                        continue
                if not isFile or not entry.name.endswith(".json"):
                    continue
                self.loadThemeFromFile(entry.path, themes, diagnostics, colorMode)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(
                ResourceDiagnostic(
                    type="warning",
                    message=_error_message(error, "failed to read theme directory"),
                    path=dir,
                )
            )

    def loadThemeFromFile(
        self,
        filePath: str,
        themes: list[Theme],
        diagnostics: list[ResourceDiagnostic],
        colorMode: TerminalColorMode,
    ) -> None:
        try:
            themes.append(loadThemeFromPath(filePath, colorMode))
        except Exception as error:  # noqa: BLE001 - a broken theme file is a diagnostic, not a crash
            diagnostics.append(
                ResourceDiagnostic(
                    type="warning",
                    message=_error_message(error, "failed to load theme"),
                    path=filePath,
                )
            )

    async def loadExtensionFactories(self, runtime: ExtensionRuntime) -> dict[str, list[Any]]:
        extensions: list[Extension] = []
        errors: list[dict[str, str]] = []
        for index, input_ in enumerate(self.extensionFactories, start=1):
            # Port of pi resource-loader.ts loadExtensionFactories: an InlineExtension is a bare
            # factory or {name, factory, hidden}; named ones show as <inline:name>, hidden ones
            # stay off the startup screen.
            isNamed = not callable(input_)
            factory = input_.get("factory") if isNamed else input_
            extensionPath = f"<inline:{input_.get('name') if isNamed else index}>"
            try:
                extension = await loadExtensionFromFactory(
                    factory,
                    self.cwd,
                    self.eventBus,
                    runtime,
                    extensionPath,
                )
                extension.hidden = bool(isNamed and input_.get("hidden"))
                extension.replaceable = bool(isNamed and input_.get("replaceable") is True)
                extensions.append(extension)
            except Exception as error:  # noqa: BLE001 - extension code: a failing load is reported as an error entry
                errors.append({"path": extensionPath, "error": _error_message(error, "failed to load extension")})
        return {"extensions": extensions, "errors": errors}

    def dedupePrompts(self, prompts: list[PromptTemplate]) -> dict[str, list[Any]]:
        seen: dict[str, PromptTemplate] = {}
        diagnostics: list[ResourceDiagnostic] = []
        for prompt in prompts:
            existing = seen.get(prompt.name)
            if existing is None:
                seen[prompt.name] = prompt
                continue
            diagnostics.append(
                ResourceDiagnostic(
                    type="collision",
                    message=f'name "/{prompt.name}" collision',
                    path=prompt.filePath,
                    collision=ResourceCollision(
                        resourceType="prompt",
                        name=prompt.name,
                        winnerPath=existing.filePath,
                        loserPath=prompt.filePath,
                    ),
                )
            )
        return {"prompts": list(seen.values()), "diagnostics": diagnostics}

    def dedupeThemes(self, themes: list[Theme]) -> dict[str, list[Any]]:
        seen: dict[str, Theme] = {}
        diagnostics: list[ResourceDiagnostic] = []
        for theme in themes:
            name = theme.name or "unnamed"
            existing = seen.get(name)
            if existing is None:
                seen[name] = theme
                continue
            diagnostics.append(
                ResourceDiagnostic(
                    type="collision",
                    message=f'name "{name}" collision',
                    path=theme.sourcePath,
                    collision=ResourceCollision(
                        resourceType="theme",
                        name=name,
                        winnerPath=existing.sourcePath or "<builtin>",
                        loserPath=theme.sourcePath or "<builtin>",
                    ),
                )
            )
        return {"themes": list(seen.values()), "diagnostics": diagnostics}

    def discoverSystemPromptFile(self) -> str | None:
        projectDir = home.project_dir(self.cwd)
        projectPath = os.path.join(projectDir, "SYSTEM.md") if projectDir is not None else None
        if projectPath is not None and self.settingsManager.isProjectTrusted() and os.path.exists(projectPath):
            return projectPath

        globalPath = os.path.join(self.agentDir, "SYSTEM.md")
        if os.path.exists(globalPath):
            return globalPath
        return None

    def discoverAppendSystemPromptFile(self) -> str | None:
        projectDir = home.project_dir(self.cwd)
        projectPath = os.path.join(projectDir, "APPEND_SYSTEM.md") if projectDir is not None else None
        if projectPath is not None and self.settingsManager.isProjectTrusted() and os.path.exists(projectPath):
            return projectPath

        globalPath = os.path.join(self.agentDir, "APPEND_SYSTEM.md")
        if os.path.exists(globalPath):
            return globalPath
        return None

    def isUnderPath(self, target: str, root: str) -> bool:
        normalizedRoot = os.path.abspath(root)
        normalizedTarget = os.path.abspath(target)
        if normalizedTarget == normalizedRoot:
            return True
        prefix = normalizedRoot if normalizedRoot.endswith(os.sep) else f"{normalizedRoot}{os.sep}"
        return normalizedTarget.startswith(prefix)

    def detectExtensionConflicts(self, extensions: list[Extension]) -> list[dict[str, str]]:
        conflicts: list[dict[str, str]] = []
        toolOwners: dict[str, str] = {}
        flagOwners: dict[str, str] = {}
        for extension in extensions:
            for toolName in extension.tools:
                existingOwner = toolOwners.get(toolName)
                if existingOwner is not None and existingOwner != extension.path:
                    conflicts.append(
                        {"path": extension.path, "message": f'Tool "{toolName}" conflicts with {existingOwner}'}
                    )
                else:
                    toolOwners[toolName] = extension.path
            for flagName in extension.flags:
                existingOwner = flagOwners.get(flagName)
                if existingOwner is not None and existingOwner != extension.path:
                    conflicts.append(
                        {"path": extension.path, "message": f'Flag "--{flagName}" conflicts with {existingOwner}'}
                    )
                else:
                    flagOwners[flagName] = extension.path
        return conflicts


# MISAKA fork: named, because the skills guard refuses writes to them (issue #10 audit H4).
# The files a session in a folder loads as instructions, from that folder and every one above it.
CONTEXT_FILE_NAMES = ("PROJECT.md", "AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")  # pi 8ecf8a9 + the project brief
