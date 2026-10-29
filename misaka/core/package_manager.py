"""Local resource discovery for extensions, prompts and themes: the agent dir's auto
directories, explicit settings entries, and CLI ``-e`` paths. (pi's npm/git package
installer, updater and ``install``/``update`` commands are gone: MISAKA ships its
extensions in-process and has no package users.)"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from pathspec import GitIgnoreSpec
from wcmatch import glob as wc_glob

from misaka.config import home
from misaka.core.settings_manager import SettingsManager
from misaka.core.source_info import BUILTIN_PATH_PREFIX, SourceScope
from misaka.utils.node_fs import readdirEntries
from misaka.utils.paths import (
    canonicalizePath,
    homedir,
    isLocalPath,
    normalizePath,
    resolvePath,
)

ResourceType = Literal["extensions", "prompts", "themes"]
SourceOrigin = Literal["package", "top-level"]

RESOURCE_TYPES: tuple[ResourceType, ...] = ("extensions", "prompts", "themes")
IGNORE_FILE_NAMES = (".gitignore", ".ignore", ".fdignore")
_GLOB_MATCH_FLAGS = wc_glob.GLOBSTAR | wc_glob.FORCEUNIX


class PathMetadata(TypedDict):
    source: str
    scope: SourceScope
    origin: Literal["package", "top-level"]
    baseDir: NotRequired[str]


class PackageManagerOptions(TypedDict):
    cwd: str
    agentDir: str
    settingsManager: SettingsManager
    # Names of the built-in extensions, resolved as `builtin:<name>` extension resources.
    builtinExtensions: NotRequired[list[str]]


@dataclass(slots=True)
class ResolvedResource:
    path: str
    enabled: bool
    metadata: PathMetadata


@dataclass(slots=True)
class ResolvedPaths:
    extensions: list[ResolvedResource] = field(default_factory=list)
    prompts: list[ResolvedResource] = field(default_factory=list)
    themes: list[ResolvedResource] = field(default_factory=list)


@dataclass(slots=True)
class _Accumulator:
    extensions: dict[str, tuple[PathMetadata, bool]] = field(default_factory=dict)
    prompts: dict[str, tuple[PathMetadata, bool]] = field(default_factory=dict)
    themes: dict[str, tuple[PathMetadata, bool]] = field(default_factory=dict)


class _IgnoreMatcher:
    def __init__(self) -> None:
        self._patterns: list[str] = []
        self._spec: GitIgnoreSpec | None = None

    def add(self, patterns: list[str]) -> None:
        self._patterns.extend(patterns)
        self._spec = None

    def ignores(self, path: str) -> bool:
        if not self._patterns:
            return False
        if self._spec is None:
            self._spec = GitIgnoreSpec.from_lines(self._patterns)
        return self._spec.match_file(path)


def toPosixPath(path: str) -> str:
    return path.replace(os.sep, "/")


def getHomeDir() -> str:
    return os.environ.get("HOME") or homedir()


def prefixIgnorePattern(line: str, prefix: str) -> str | None:
    trimmed = line.strip()
    if not trimmed:
        return None
    if trimmed.startswith("#") and not trimmed.startswith("\\#"):
        return None

    pattern = line
    negated = False
    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]
    elif pattern.startswith("\\!"):
        pattern = pattern[1:]

    pattern = pattern.removeprefix("/")

    prefixed = f"{prefix}{pattern}" if prefix else pattern
    return f"!{prefixed}" if negated else prefixed


def addIgnoreRules(matcher: _IgnoreMatcher, dir_path: str, root_dir: str) -> None:
    relative_dir = os.path.relpath(dir_path, root_dir)
    prefix = f"{toPosixPath(relative_dir)}/" if relative_dir != "." else ""
    for filename in IGNORE_FILE_NAMES:
        ignore_path = os.path.join(dir_path, filename)
        if not os.path.exists(ignore_path):
            continue
        try:
            content = Path(ignore_path).read_text(encoding="utf-8-sig")
        except OSError:
            continue
        patterns = [
            pattern
            for line in content.splitlines()
            if (pattern := prefixIgnorePattern(line, prefix)) is not None
        ]
        if patterns:
            matcher.add(patterns)


def isPattern(value: str) -> bool:
    return value.startswith(("!", "+", "-")) or "*" in value or "?" in value


def isOverridePattern(value: str) -> bool:
    return value.startswith(("!", "+", "-"))


def splitPatterns(entries: list[str]) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    patterns: list[str] = []
    for entry in entries:
        (patterns if isPattern(entry) else plain).append(entry)
    return plain, patterns


def _match_pattern(pattern: str, *candidates: str) -> bool:
    normalized = toPosixPath(normalizePath(pattern))
    return any(wc_glob.globmatch(candidate, normalized, flags=_GLOB_MATCH_FLAGS) for candidate in candidates)


def _relative_to(file_path: str, base_dir: str) -> str:
    # MISAKA fork: `path.relative` never throws; `os.path.relpath` does on Windows when a
    # drive-less name such as `builtin:mcp` and the base sit on different drives.
    try:
        return toPosixPath(os.path.relpath(file_path, base_dir))
    except ValueError:
        return toPosixPath(file_path)


def matchesAnyPattern(file_path: str, patterns: list[str], base_dir: str) -> bool:
    rel = _relative_to(file_path, base_dir)
    name = os.path.basename(file_path)
    full = toPosixPath(file_path)
    return any(_match_pattern(pattern, rel, name, full) for pattern in patterns)


def normalizeExactPattern(pattern: str) -> str:
    if pattern.startswith(("./", ".\\")):
        pattern = pattern[2:]
    return toPosixPath(normalizePath(pattern))


def matchesAnyExactPattern(file_path: str, patterns: list[str], base_dir: str) -> bool:
    if not patterns:
        return False
    rel = _relative_to(file_path, base_dir)
    full = toPosixPath(file_path)
    return any(normalizeExactPattern(pattern) in {rel, full} for pattern in patterns)


def applyPatterns(all_paths: list[str], patterns: list[str], base_dir: str) -> set[str]:
    includes: list[str] = []
    excludes: list[str] = []
    force_includes: list[str] = []
    force_excludes: list[str] = []

    for pattern in patterns:
        if pattern.startswith("+"):
            force_includes.append(pattern[1:])
        elif pattern.startswith("-"):
            force_excludes.append(pattern[1:])
        elif pattern.startswith("!"):
            excludes.append(pattern[1:])
        else:
            includes.append(pattern)

    if includes:
        result = [path for path in all_paths if matchesAnyPattern(path, includes, base_dir)]
    else:
        result = list(all_paths)

    if excludes:
        result = [path for path in result if not matchesAnyPattern(path, excludes, base_dir)]

    if force_includes:
        for path in all_paths:
            if path not in result and matchesAnyExactPattern(path, force_includes, base_dir):
                result.append(path)

    if force_excludes:
        result = [path for path in result if not matchesAnyExactPattern(path, force_excludes, base_dir)]

    return set(result)


def getOverridePatterns(entries: list[str]) -> list[str]:
    return [pattern for pattern in entries if isOverridePattern(pattern)]


def applyAutoloadDisabledPatterns(all_paths: list[str], patterns: list[str], base_dir: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for pattern in patterns:
        target = pattern[1:] if pattern.startswith(("+", "-", "!")) else pattern
        enabled = not pattern.startswith(("-", "!"))
        exact = pattern.startswith(("+", "-"))
        for file_path in all_paths:
            if (
                matchesAnyExactPattern(file_path, [target], base_dir)
                if exact
                else matchesAnyPattern(file_path, [target], base_dir)
            ):
                result[file_path] = enabled
    return result


def resourcePrecedenceRank(metadata: PathMetadata) -> int:
    # Built-in extensions load after file and package extensions, whichever scope enables them.
    if metadata.get("source") == "builtin":
        return 5
    if metadata.get("origin") == "package":
        return 4
    scope_base = 0 if metadata.get("scope") == "project" else 2
    return scope_base + (0 if metadata.get("source") == "local" else 1)


def _resolve_dir_entry(entry: os.DirEntry[str]) -> tuple[bool, bool]:
    is_dir = entry.is_dir(follow_symlinks=False)
    is_file = entry.is_file(follow_symlinks=False)
    if entry.is_symlink():
        stats = os.stat(entry.path)
        is_dir = stat_module.S_ISDIR(stats.st_mode)
        is_file = stat_module.S_ISREG(stats.st_mode)
    return is_dir, is_file


def collectFiles(
    dir_path: str,
    predicate: Callable[[str], bool],
    *,
    skip_node_modules: bool = True,
    ignore_matcher: _IgnoreMatcher | None = None,
    root_dir: str | None = None,
) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    root = root_dir or dir_path
    matcher = ignore_matcher or _IgnoreMatcher()
    addIgnoreRules(matcher, dir_path, root)
    files: list[str] = []
    try:
        for entry in readdirEntries(dir_path):
            if entry.name.startswith("."):
                continue
            if skip_node_modules and entry.name == "node_modules":
                continue
            full_path = entry.path
            try:
                is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = toPosixPath(os.path.relpath(full_path, root))
            ignore_path = f"{rel_path}/" if is_dir else rel_path
            if matcher.ignores(ignore_path):
                continue
            if is_dir:
                files.extend(
                    collectFiles(
                        full_path,
                        predicate,
                        skip_node_modules=skip_node_modules,
                        ignore_matcher=matcher,
                        root_dir=root,
                    )
                )
            elif is_file and predicate(entry.name):
                files.append(full_path)
    except OSError:
        return files
    return files


def collectAutoPromptEntries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    matcher = _IgnoreMatcher()
    addIgnoreRules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in readdirEntries(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            try:
                _is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = toPosixPath(os.path.relpath(entry.path, dir_path))
            if is_file and entry.name.endswith(".md") and not matcher.ignores(rel_path):
                entries.append(entry.path)
    except OSError:
        return entries
    return entries


def collectAutoThemeEntries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    matcher = _IgnoreMatcher()
    addIgnoreRules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in readdirEntries(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            try:
                _is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = toPosixPath(os.path.relpath(entry.path, dir_path))
            if is_file and entry.name.endswith(".json") and not matcher.ignores(rel_path):
                entries.append(entry.path)
    except OSError:
        return entries
    return entries


def resolveExtensionEntries(dir_path: str) -> list[str] | None:
    index_py = os.path.join(dir_path, "index.py")
    if os.path.exists(index_py):
        return [index_py]
    return None


def collectAutoExtensionEntries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    root_entries = resolveExtensionEntries(dir_path)
    if root_entries is not None:
        return root_entries

    matcher = _IgnoreMatcher()
    addIgnoreRules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in readdirEntries(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue

            full_path = entry.path
            try:
                is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue

            rel_path = toPosixPath(os.path.relpath(full_path, dir_path))
            ignore_path = f"{rel_path}/" if is_dir else rel_path
            if matcher.ignores(ignore_path):
                continue

            if is_file and entry.name.endswith(".py"):
                entries.append(full_path)
            elif is_dir:
                resolved_entries = resolveExtensionEntries(full_path)
                if resolved_entries:
                    entries.extend(resolved_entries)
    except OSError:
        return entries
    return entries


def collectResourceFiles(dir_path: str, resource_type: ResourceType) -> list[str]:
    if resource_type == "extensions":
        return collectAutoExtensionEntries(dir_path)
    if resource_type == "prompts":
        return collectFiles(dir_path, lambda name: name.endswith(".md"))
    return collectFiles(dir_path, lambda name: name.endswith(".json"))


class DefaultPackageManager:
    def __init__(self, options: PackageManagerOptions | dict[str, Any]) -> None:
        self.cwd = resolvePath(str(options["cwd"]))
        self.agentDir = resolvePath(str(options["agentDir"]))
        self.settingsManager = cast(SettingsManager, options["settingsManager"])
        self.builtinExtensions: list[str] = list(options.get("builtinExtensions") or [])


    async def resolve(self) -> ResolvedPaths:
        """Resources from settings (explicit ``extensions``/``prompts``/``themes`` entries) plus
        auto-discovered user resources. Project files never opt themselves into Python execution;
        a local extension must be selected explicitly with the CLI."""
        accumulator = _Accumulator()
        global_settings = self.settingsManager.getGlobalSettings()
        project_settings = self.settingsManager.getProjectSettings()
        # A directory whose config directory is the home has no project scope to trust.
        project_dir = home.project_dir(self.cwd)
        project_trusted = self.settingsManager.isProjectTrusted() and project_dir is not None
        global_base_dir = self.agentDir
        project_base_dir = str(project_dir) if project_dir is not None else self.cwd
        for resource_type in RESOURCE_TYPES:
            target = self.getTargetMap(accumulator, resource_type)
            if project_trusted and resource_type != "extensions":
                self.resolveLocalEntries(
                    list(project_settings.get(resource_type) or []),
                    resource_type,
                    target,
                    {"source": "local", "scope": "project", "origin": "top-level"},
                    project_base_dir,
                )
            self.resolveLocalEntries(
                list(global_settings.get(resource_type) or []),
                resource_type,
                target,
                {"source": "local", "scope": "user", "origin": "top-level"},
                global_base_dir,
            )
        self.addAutoDiscoveredResources(
            accumulator,
            global_settings,
            project_settings,
            global_base_dir,
            project_base_dir,
            project_trusted=project_trusted,
        )

        # Built-in extensions are enabled unless the user `extensions` setting excludes them, for
        # example with `-builtin:mcp`. A matching `+`, `-`, or `!` entry in the project setting
        # overrides that. (MISAKA fork: only a trusted project's setting counts; toggling a bundled
        # extension runs no project code.)
        project_extensions = list(project_settings.get("extensions") or []) if project_trusted else []
        for name in self.builtinExtensions:
            path = f"{BUILTIN_PATH_PREFIX}{name}"
            project_enabled = applyAutoloadDisabledPatterns(
                [path], getOverridePatterns(project_extensions), project_base_dir
            ).get(path)
            self.addResource(
                accumulator.extensions,
                path,
                {"source": "builtin", "scope": "user" if project_enabled is None else "project", "origin": "top-level"},
                project_enabled
                if project_enabled is not None
                else self.isEnabledByOverrides(path, list(global_settings.get("extensions") or []), global_base_dir),
            )

        return self.toResolvedPaths(accumulator)

    async def resolveExtensionSources(
        self,
        sources: list[str],
        options: dict[str, bool] | None = None,
    ) -> ResolvedPaths:
        """CLI ``-e`` sources: local files or directories only."""
        accumulator = _Accumulator()
        if options and options.get("temporary"):
            scope: SourceScope = "temporary"
        elif options and options.get("local"):
            scope = "project"
        else:
            scope = "user"
        # `-e builtin:<name>` loads a built-in extension; the resource loader reports unknown names.
        for source in [source for source in sources if source.startswith(BUILTIN_PATH_PREFIX)]:
            self.addResource(
                accumulator.extensions, source, {"source": "builtin", "scope": scope, "origin": "top-level"}, True
            )
        for source in [source for source in sources if not source.startswith(BUILTIN_PATH_PREFIX)]:
            if not isLocalPath(source):
                raise ValueError(f"Extension sources must be local paths (packages are not supported): {source}")
            self._resolve_local_source(
                source, accumulator, {"source": source, "scope": scope, "origin": "package"},
                self.getBaseDirForScope(scope),
            )
        return self.toResolvedPaths(accumulator)

    def getBaseDirForScope(self, scope: SourceScope) -> str:
        if scope == "project":
            return str(home.project_dir(self.cwd) or self.cwd)
        if scope == "user":
            return self.agentDir
        return self.cwd

    def resolvePathFromBase(self, value: str, base_dir: str) -> str:
        return resolvePath(value, base_dir, {"homeDir": getHomeDir(), "trim": True})

    def resolvePath(self, value: str) -> str:
        return resolvePath(value, self.cwd, {"homeDir": getHomeDir(), "trim": True})


    def _resolve_local_source(
        self,
        source: str,
        accumulator: _Accumulator,
        metadata: PathMetadata,
        base_dir: str,
    ) -> None:
        resolved = self.resolvePathFromBase(source, base_dir)
        if not os.path.exists(resolved):
            return

        if os.path.isfile(resolved):
            metadata["baseDir"] = os.path.dirname(resolved)
            self.addResource(accumulator.extensions, resolved, metadata, True)
            return

        metadata["baseDir"] = resolved
        if self.collectPackageResources(resolved, accumulator, metadata):
            return

        from misaka.core.extensions.loader import resolveExtensionEntries

        entries = resolveExtensionEntries(resolved)
        if entries is not None:
            for entry in entries:
                self.addResource(accumulator.extensions, entry, metadata, True)
            return

        self.addResource(accumulator.extensions, resolved, metadata, True)


    def collectPackageResources(
        self,
        package_root: str,
        accumulator: _Accumulator,
        metadata: PathMetadata,
    ) -> bool:
        has_any_dir = False
        for resource_type in RESOURCE_TYPES:
            directory = os.path.join(package_root, resource_type)
            if not os.path.exists(directory):
                continue
            has_any_dir = True
            for file_path in collectResourceFiles(directory, resource_type):
                self.addResource(self.getTargetMap(accumulator, resource_type), file_path, metadata, True)
        return has_any_dir


    def resolveLocalEntries(
        self,
        entries: list[str],
        resource_type: ResourceType,
        target: dict[str, tuple[PathMetadata, bool]],
        metadata: PathMetadata,
        base_dir: str,
    ) -> None:
        if not entries:
            return
        plain, patterns = splitPatterns(entries)
        resolved_plain = [self.resolvePathFromBase(path, base_dir) for path in plain]
        all_files = self.collectFilesFromPaths(resolved_plain, resource_type)
        enabled_paths = applyPatterns(all_files, patterns, base_dir)
        for file_path in all_files:
            self.addResource(target, file_path, metadata, file_path in enabled_paths)

    def addAutoDiscoveredResources(
        self,
        accumulator: _Accumulator,
        global_settings: dict[str, Any],
        project_settings: dict[str, Any],
        global_base_dir: str,
        project_base_dir: str,
        *,
        project_trusted: bool,
    ) -> None:
        user_metadata: PathMetadata = {
            "source": "auto",
            "scope": "user",
            "origin": "top-level",
            "baseDir": global_base_dir,
        }
        project_metadata: PathMetadata = {
            "source": "auto",
            "scope": "project",
            "origin": "top-level",
            "baseDir": project_base_dir,
        }
        user_overrides = {
            resource_type: list(global_settings.get(resource_type) or [])
            for resource_type in RESOURCE_TYPES
        }
        project_overrides = {
            resource_type: list(project_settings.get(resource_type) or []) for resource_type in RESOURCE_TYPES
        }
        user_dirs = {
            resource_type: os.path.join(global_base_dir, resource_type)
            for resource_type in RESOURCE_TYPES
        }
        project_dirs = {
            resource_type: os.path.join(project_base_dir, resource_type)
            for resource_type in RESOURCE_TYPES
        }

        def add_resources(
            resource_type: ResourceType,
            paths: list[str],
            metadata: PathMetadata,
            overrides: list[str],
            base_dir: str,
        ) -> None:
            target = self.getTargetMap(accumulator, resource_type)
            for path in paths:
                self.addResource(target, path, metadata, self.isEnabledByOverrides(path, overrides, base_dir))

        if project_trusted:
            add_resources(
                "prompts",
                collectAutoPromptEntries(project_dirs["prompts"]),
                project_metadata,
                project_overrides["prompts"],
                project_base_dir,
            )
            add_resources(
                "themes",
                collectAutoThemeEntries(project_dirs["themes"]),
                project_metadata,
                project_overrides["themes"],
                project_base_dir,
            )

        add_resources(
            "extensions",
            collectAutoExtensionEntries(user_dirs["extensions"]),
            user_metadata,
            user_overrides["extensions"],
            global_base_dir,
        )
        add_resources(
            "prompts",
            collectAutoPromptEntries(user_dirs["prompts"]),
            user_metadata,
            user_overrides["prompts"],
            global_base_dir,
        )
        add_resources(
            "themes",
            collectAutoThemeEntries(user_dirs["themes"]),
            user_metadata,
            user_overrides["themes"],
            global_base_dir,
        )

    def collectFilesFromPaths(self, paths: list[str], resource_type: ResourceType) -> list[str]:
        files: list[str] = []
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                if os.path.isfile(path):
                    files.append(path)
                elif os.path.isdir(path):
                    files.extend(collectResourceFiles(path, resource_type))
            except OSError:
                continue
        return files

    def getTargetMap(
        self,
        accumulator: _Accumulator,
        resource_type: ResourceType,
    ) -> dict[str, tuple[PathMetadata, bool]]:
        return cast(dict[str, tuple[PathMetadata, bool]], getattr(accumulator, resource_type))

    def addResource(
        self,
        target: dict[str, tuple[PathMetadata, bool]],
        path: str,
        metadata: PathMetadata,
        enabled: bool,
    ) -> None:
        if path and path not in target:
            target[path] = (metadata, enabled)

    def isEnabledByOverrides(self, path: str, patterns: list[str], base_dir: str) -> bool:
        if not patterns:
            return True
        patterns = [pattern for pattern in patterns if isOverridePattern(pattern)]
        enabled = True
        excludes = [pattern[1:] for pattern in patterns if pattern.startswith("!")]
        force_includes = [pattern[1:] for pattern in patterns if pattern.startswith("+")]
        force_excludes = [pattern[1:] for pattern in patterns if pattern.startswith("-")]
        if excludes and matchesAnyPattern(path, excludes, base_dir):
            enabled = False
        if force_includes and matchesAnyExactPattern(path, force_includes, base_dir):
            enabled = True
        if force_excludes and matchesAnyExactPattern(path, force_excludes, base_dir):
            enabled = False
        return enabled

    def toResolvedPaths(self, accumulator: _Accumulator) -> ResolvedPaths:
        def materialize(entries: dict[str, tuple[PathMetadata, bool]]) -> list[ResolvedResource]:
            resolved = [
                ResolvedResource(path=path, metadata=metadata, enabled=enabled)
                for path, (metadata, enabled) in entries.items()
            ]
            resolved.sort(key=lambda entry: resourcePrecedenceRank(entry.metadata))
            seen: set[str] = set()
            deduped: list[ResolvedResource] = []
            for entry in resolved:
                canonical = canonicalizePath(entry.path)
                if canonical in seen:
                    continue
                seen.add(canonical)
                deduped.append(entry)
            return deduped

        return ResolvedPaths(
            extensions=materialize(accumulator.extensions),
            prompts=materialize(accumulator.prompts),
            themes=materialize(accumulator.themes),
        )


__all__ = [
    "DefaultPackageManager",
    "PathMetadata",
    "ResolvedPaths",
    "ResolvedResource",
]
