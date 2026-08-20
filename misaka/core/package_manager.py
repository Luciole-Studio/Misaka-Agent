"""Package-backed resource discovery for coding-agent extensions and assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal as signal_module
import stat as stat_module
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict, TypeVar, cast
from urllib.parse import urlparse

from pathspec import GitIgnoreSpec
from wcmatch import glob as wc_glob

from misaka.config import CONFIG_DIR_NAME
from misaka.core.output_guard import isStdoutTakenOver
from misaka.core.settings_manager import PackageSource, SettingsManager
from misaka.core.source_info import SourceScope
from misaka.utils.child_process import spawn_process_sync
from misaka.utils.git import GitSource, parse_git_url
from misaka.utils.paths import (
    canonicalize_path,
    is_local_path,
    mark_path_ignored_by_cloud_sync,
    resolve_path,
)

MissingSourceAction = Literal["install", "skip", "error"]
ResourceType = Literal["extensions", "skills", "prompts", "themes"]
SourceOrigin = Literal["package", "top-level"]
_T = TypeVar("_T")

RESOURCE_TYPES: tuple[ResourceType, ...] = ("extensions", "skills", "prompts", "themes")
IGNORE_FILE_NAMES = (".gitignore", ".ignore", ".fdignore")
NETWORK_TIMEOUT_MS = 10000
UPDATE_CHECK_CONCURRENCY = 4
GIT_UPDATE_CONCURRENCY = 4
_GLOB_MATCH_FLAGS = wc_glob.GLOBSTAR | wc_glob.FORCEUNIX
_GIT_HEAD_RE = re.compile(r"^([0-9a-f]{40})\s+", re.MULTILINE)
_GIT_HEAD_EXACT_RE = re.compile(r"^([0-9a-f]{40})\s+HEAD$", re.MULTILINE)


class PackageFilter(TypedDict, total=False):
    source: str
    extensions: list[str]
    skills: list[str]
    prompts: list[str]
    themes: list[str]


class HarnManifest(TypedDict, total=False):
    extensions: list[str]
    skills: list[str]
    prompts: list[str]
    themes: list[str]


class PathMetadata(TypedDict):
    source: str
    scope: SourceScope
    origin: Literal["package", "top-level"]
    baseDir: NotRequired[str]


class PackageManagerOptions(TypedDict):
    cwd: str
    agentDir: str
    settingsManager: SettingsManager


class PackageManager(Protocol):
    async def resolve(
        self,
        onMissing: Callable[[str], Awaitable[MissingSourceAction]] | None = None,
    ) -> ResolvedPaths: ...

    async def install(self, source: str, options: dict[str, bool] | None = None) -> None: ...

    async def installAndPersist(self, source: str, options: dict[str, bool] | None = None) -> None: ...

    async def remove(self, source: str, options: dict[str, bool] | None = None) -> None: ...

    async def removeAndPersist(self, source: str, options: dict[str, bool] | None = None) -> bool: ...

    async def update(self, source: str | None = None) -> None: ...

    def listConfiguredPackages(self) -> list[ConfiguredPackage]: ...

    async def resolveExtensionSources(
        self,
        sources: list[str],
        options: dict[str, bool] | None = None,
    ) -> ResolvedPaths: ...

    def addSourceToSettings(self, source: str, options: dict[str, bool] | None = None) -> bool: ...

    def removeSourceFromSettings(self, source: str, options: dict[str, bool] | None = None) -> bool: ...

    def setProgressCallback(self, callback: ProgressCallback | None) -> None: ...

    def getInstalledPath(self, source: str, scope: Literal["user", "project"]) -> str | None: ...


@dataclass(slots=True)
class ResolvedResource:
    path: str
    enabled: bool
    metadata: PathMetadata


@dataclass(slots=True)
class ResolvedPaths:
    extensions: list[ResolvedResource] = field(default_factory=list)
    skills: list[ResolvedResource] = field(default_factory=list)
    prompts: list[ResolvedResource] = field(default_factory=list)
    themes: list[ResolvedResource] = field(default_factory=list)


@dataclass(slots=True)
class ProgressEvent:
    type: Literal["start", "progress", "complete", "error"]
    action: Literal["install", "remove", "update", "clone", "pull"]
    source: str
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(slots=True)
class PackageUpdate:
    source: str
    displayName: str
    type: Literal["npm", "git"]
    scope: Literal["user", "project"]


@dataclass(slots=True)
class ConfiguredPackage:
    source: str
    scope: Literal["user", "project"]
    filtered: bool
    installedPath: str | None = None


@dataclass(slots=True)
class _NpmSource:
    type: Literal["npm"]
    spec: str
    name: str
    pinned: bool


@dataclass(slots=True)
class _LocalSource:
    type: Literal["local"]
    path: str


@dataclass(slots=True)
class _GitUpdateTarget:
    ref: str
    head: str
    fetch_args: list[str]


@dataclass(slots=True)
class _ConfiguredUpdateSource:
    source: str
    scope: Literal["user", "project"]


@dataclass(slots=True)
class _NpmUpdateTarget:
    source: str
    scope: Literal["user", "project"]
    parsed: _NpmSource


@dataclass(slots=True)
class _GitConfiguredUpdateSource:
    source: str
    scope: Literal["user", "project"]
    parsed: GitSource


@dataclass(slots=True)
class _ManifestFiles:
    allFiles: list[str]
    enabledByManifest: set[str]


@dataclass(slots=True)
class _Accumulator:
    extensions: dict[str, tuple[PathMetadata, bool]] = field(default_factory=dict)
    skills: dict[str, tuple[PathMetadata, bool]] = field(default_factory=dict)
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


def _to_posix_path(path: str) -> str:
    return path.replace(os.sep, "/")


def _get_env() -> Mapping[str, str]:
    if sys.platform != "linux" or len(os.environ) > 0:
        return os.environ
    try:
        data = Path("/proc/self/environ").read_text(encoding="utf-8")
    except OSError:
        return os.environ
    env: dict[str, str] = {}
    for entry in data.split("\0"):
        index = entry.find("=")
        if index > 0:
            env[entry[:index]] = entry[index + 1 :]
    return env


def _get_home_dir() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _prefix_ignore_pattern(line: str, prefix: str) -> str | None:
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

    if pattern.startswith("/"):
        pattern = pattern[1:]

    prefixed = f"{prefix}{pattern}" if prefix else pattern
    return f"!{prefixed}" if negated else prefixed


def _add_ignore_rules(matcher: _IgnoreMatcher, dir_path: str, root_dir: str) -> None:
    relative_dir = os.path.relpath(dir_path, root_dir)
    prefix = f"{_to_posix_path(relative_dir)}/" if relative_dir != "." else ""
    for filename in IGNORE_FILE_NAMES:
        ignore_path = os.path.join(dir_path, filename)
        if not os.path.exists(ignore_path):
            continue
        try:
            content = Path(ignore_path).read_text(encoding="utf-8")
        except OSError:
            continue
        patterns = [
            pattern
            for line in content.splitlines()
            if (pattern := _prefix_ignore_pattern(line, prefix)) is not None
        ]
        if patterns:
            matcher.add(patterns)


def _is_pattern(value: str) -> bool:
    return value.startswith(("!", "+", "-")) or "*" in value or "?" in value


def _is_override_pattern(value: str) -> bool:
    return value.startswith(("!", "+", "-"))


def _has_glob_pattern(value: str) -> bool:
    return "*" in value or "?" in value


def _split_patterns(entries: list[str]) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    patterns: list[str] = []
    for entry in entries:
        (patterns if _is_pattern(entry) else plain).append(entry)
    return plain, patterns


def _match_pattern(pattern: str, *candidates: str) -> bool:
    normalized = _to_posix_path(pattern)
    return any(wc_glob.globmatch(candidate, normalized, flags=_GLOB_MATCH_FLAGS) for candidate in candidates)


def _matches_any_pattern(file_path: str, patterns: list[str], base_dir: str) -> bool:
    rel = _to_posix_path(os.path.relpath(file_path, base_dir))
    name = os.path.basename(file_path)
    full = _to_posix_path(file_path)
    if any(_match_pattern(pattern, rel, name, full) for pattern in patterns):
        return True

    if name != "SKILL.md":
        return False
    parent = os.path.dirname(file_path)
    parent_rel = _to_posix_path(os.path.relpath(parent, base_dir))
    parent_name = os.path.basename(parent)
    parent_full = _to_posix_path(parent)
    return any(_match_pattern(pattern, parent_rel, parent_name, parent_full) for pattern in patterns)


def _normalize_exact_pattern(pattern: str) -> str:
    if pattern.startswith("./") or pattern.startswith(".\\"):
        pattern = pattern[2:]
    return _to_posix_path(pattern)


def _matches_any_exact_pattern(file_path: str, patterns: list[str], base_dir: str) -> bool:
    if not patterns:
        return False
    rel = _to_posix_path(os.path.relpath(file_path, base_dir))
    full = _to_posix_path(file_path)
    if any(_normalize_exact_pattern(pattern) in {rel, full} for pattern in patterns):
        return True

    if os.path.basename(file_path) != "SKILL.md":
        return False
    parent = os.path.dirname(file_path)
    parent_rel = _to_posix_path(os.path.relpath(parent, base_dir))
    parent_full = _to_posix_path(parent)
    return any(_normalize_exact_pattern(pattern) in {parent_rel, parent_full} for pattern in patterns)


def _apply_patterns(all_paths: list[str], patterns: list[str], base_dir: str) -> set[str]:
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
        result = [path for path in all_paths if _matches_any_pattern(path, includes, base_dir)]
    else:
        result = list(all_paths)

    if excludes:
        result = [path for path in result if not _matches_any_pattern(path, excludes, base_dir)]

    if force_includes:
        for path in all_paths:
            if path not in result and _matches_any_exact_pattern(path, force_includes, base_dir):
                result.append(path)

    if force_excludes:
        result = [path for path in result if not _matches_any_exact_pattern(path, force_excludes, base_dir)]

    return set(result)


def _resource_precedence_rank(metadata: PathMetadata) -> int:
    if metadata.get("origin") == "package":
        return 4
    scope_base = 0 if metadata.get("scope") == "project" else 2
    return scope_base + (0 if metadata.get("source") == "local" else 1)


def _read_harn_manifest(package_root: str) -> HarnManifest | None:
    package_json_path = os.path.join(package_root, "package.json")
    if os.path.exists(package_json_path):
        return _read_harn_package_json_manifest(package_json_path)
    pyproject_path = os.path.join(package_root, "pyproject.toml")
    if os.path.exists(pyproject_path):
        return _read_harn_pyproject_manifest(pyproject_path)
    return None


def _read_harn_package_json_manifest(package_json_path: str) -> HarnManifest | None:
    try:
        payload = json.loads(Path(package_json_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    manifest = payload.get("harn")
    return cast(HarnManifest, manifest) if isinstance(manifest, dict) else None


def _read_harn_pyproject_manifest(pyproject_path: str) -> HarnManifest | None:
    try:
        payload = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    tool = payload.get("tool")
    manifest = tool.get("harn") if isinstance(tool, dict) else None
    return cast(HarnManifest, manifest) if isinstance(manifest, dict) else None


def _resolve_dir_entry(entry: os.DirEntry[str]) -> tuple[bool, bool]:
    is_dir = entry.is_dir(follow_symlinks=False)
    is_file = entry.is_file(follow_symlinks=False)
    if entry.is_symlink():
        stats = os.stat(entry.path)
        is_dir = stat_module.S_ISDIR(stats.st_mode)
        is_file = stat_module.S_ISREG(stats.st_mode)
    return is_dir, is_file


def _collect_files(
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
    _add_ignore_rules(matcher, dir_path, root)
    files: list[str] = []
    try:
        for entry in os.scandir(dir_path):
            if entry.name.startswith("."):
                continue
            if skip_node_modules and entry.name == "node_modules":
                continue
            full_path = entry.path
            try:
                is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = _to_posix_path(os.path.relpath(full_path, root))
            ignore_path = f"{rel_path}/" if is_dir else rel_path
            if matcher.ignores(ignore_path):
                continue
            if is_dir:
                files.extend(
                    _collect_files(
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


def _collect_skill_entries(
    dir_path: str,
    mode: Literal["harn", "agents"],
    ignore_matcher: _IgnoreMatcher | None = None,
    root_dir: str | None = None,
) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    root = root_dir or dir_path
    matcher = ignore_matcher or _IgnoreMatcher()
    _add_ignore_rules(matcher, dir_path, root)
    entries: list[str] = []
    try:
        dir_entries = list(os.scandir(dir_path))
    except OSError:
        return entries

    for entry in dir_entries:
        if entry.name != "SKILL.md":
            continue
        full_path = entry.path
        try:
            _is_dir, is_file = _resolve_dir_entry(entry)
        except OSError:
            continue
        rel_path = _to_posix_path(os.path.relpath(full_path, root))
        if is_file and not matcher.ignores(rel_path):
            return [full_path]

    for entry in dir_entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        full_path = entry.path
        try:
            is_dir, is_file = _resolve_dir_entry(entry)
        except OSError:
            continue
        rel_path = _to_posix_path(os.path.relpath(full_path, root))
        if (
            mode == "harn"
            and dir_path == root
            and is_file
            and entry.name.endswith(".md")
            and not matcher.ignores(rel_path)
        ):
            entries.append(full_path)
            continue
        if not is_dir or matcher.ignores(f"{rel_path}/"):
            continue
        entries.extend(_collect_skill_entries(full_path, mode, matcher, root))
    return entries


def _collect_auto_skill_entries(dir_path: str, mode: Literal["harn", "agents"]) -> list[str]:
    return _collect_skill_entries(dir_path, mode)


def _collect_auto_prompt_entries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    matcher = _IgnoreMatcher()
    _add_ignore_rules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in os.scandir(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            try:
                _is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = _to_posix_path(os.path.relpath(entry.path, dir_path))
            if is_file and entry.name.endswith(".md") and not matcher.ignores(rel_path):
                entries.append(entry.path)
    except OSError:
        return entries
    return entries


def _collect_auto_theme_entries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    matcher = _IgnoreMatcher()
    _add_ignore_rules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in os.scandir(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            try:
                _is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue
            rel_path = _to_posix_path(os.path.relpath(entry.path, dir_path))
            if is_file and entry.name.endswith(".json") and not matcher.ignores(rel_path):
                entries.append(entry.path)
    except OSError:
        return entries
    return entries


def _resolve_extension_entries(dir_path: str) -> list[str] | None:
    manifest = _read_harn_manifest(dir_path)
    if manifest and manifest.get("extensions"):
        entries = [
            os.path.abspath(os.path.join(dir_path, candidate))
            for candidate in manifest["extensions"]
            if os.path.exists(os.path.join(dir_path, candidate))
        ]
        if entries:
            return entries
    index_py = os.path.join(dir_path, "index.py")
    if os.path.exists(index_py):
        return [index_py]
    return None


def _collect_auto_extension_entries(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    root_entries = _resolve_extension_entries(dir_path)
    if root_entries is not None:
        return root_entries

    matcher = _IgnoreMatcher()
    _add_ignore_rules(matcher, dir_path, dir_path)
    entries: list[str] = []
    try:
        for entry in os.scandir(dir_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue

            full_path = entry.path
            try:
                is_dir, is_file = _resolve_dir_entry(entry)
            except OSError:
                continue

            rel_path = _to_posix_path(os.path.relpath(full_path, dir_path))
            ignore_path = f"{rel_path}/" if is_dir else rel_path
            if matcher.ignores(ignore_path):
                continue

            if is_file and entry.name.endswith(".py"):
                entries.append(full_path)
            elif is_dir:
                resolved_entries = _resolve_extension_entries(full_path)
                if resolved_entries:
                    entries.extend(resolved_entries)
    except OSError:
        return entries
    return entries


def _collect_resource_files(dir_path: str, resource_type: ResourceType) -> list[str]:
    if resource_type == "skills":
        return _collect_skill_entries(dir_path, "harn")
    if resource_type == "extensions":
        return _collect_auto_extension_entries(dir_path)
    if resource_type == "prompts":
        return _collect_files(dir_path, lambda name: name.endswith(".md"))
    return _collect_files(dir_path, lambda name: name.endswith(".json"))


def _find_git_repo_root(start_dir: str) -> str | None:
    directory = os.path.abspath(start_dir)
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _collect_ancestor_agents_skill_dirs(start_dir: str) -> list[str]:
    skill_dirs: list[str] = []
    directory = os.path.abspath(start_dir)
    git_root = _find_git_repo_root(directory)
    while True:
        skill_dirs.append(os.path.join(directory, ".agents", "skills"))
        if git_root and directory == git_root:
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return skill_dirs


def _is_offline_mode_enabled() -> bool:
    value = os.environ.get("MISAKA_OFFLINE")
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes"}


class DefaultPackageManager:
    def __init__(self, options: PackageManagerOptions | dict[str, Any]) -> None:
        self.cwd = resolve_path(str(options["cwd"]))
        self.agentDir = resolve_path(str(options["agentDir"]))
        self.settingsManager = cast(SettingsManager, options["settingsManager"])
        self.globalNpmRoot: str | None = None
        self.globalNpmRootCommandKey: str | None = None
        self.progressCallback: ProgressCallback | None = None

    def setProgressCallback(self, callback: ProgressCallback | None) -> None:
        self.progressCallback = callback

    def addSourceToSettings(self, source: str, options: dict[str, bool] | None = None) -> bool:
        scope: SourceScope = "project" if options and options.get("local") else "user"
        current_settings = (
            self.settingsManager.getProjectSettings()
            if scope == "project"
            else self.settingsManager.getGlobalSettings()
        )
        current_packages = list(current_settings.get("packages") or [])
        normalized_source = self._normalize_source_for_settings(source, scope)
        for index, existing in enumerate(current_packages):
            if not self._package_sources_match(existing, source, scope):
                continue
            if self._get_source_string(existing) == normalized_source:
                return False
            next_packages = list(current_packages)
            next_packages[index] = (
                normalized_source if isinstance(existing, str) else {**existing, "source": normalized_source}
            )
            self._set_scoped_packages(scope, next_packages)
            return True
        self._set_scoped_packages(scope, [*current_packages, normalized_source])
        return True

    def removeSourceFromSettings(self, source: str, options: dict[str, bool] | None = None) -> bool:
        scope: SourceScope = "project" if options and options.get("local") else "user"
        current_settings = (
            self.settingsManager.getProjectSettings()
            if scope == "project"
            else self.settingsManager.getGlobalSettings()
        )
        current_packages = list(current_settings.get("packages") or [])
        next_packages = [pkg for pkg in current_packages if not self._package_sources_match(pkg, source, scope)]
        if len(next_packages) == len(current_packages):
            return False
        self._set_scoped_packages(scope, next_packages)
        return True

    def getInstalledPath(self, source: str, scope: Literal["user", "project"]) -> str | None:
        parsed = self._parse_source(source)
        if parsed.type == "npm":
            installed = self._get_npm_install_path(parsed, scope)
            return installed if os.path.exists(installed) else None
        if parsed.type == "git":
            installed = self._get_git_install_path(parsed, scope)
            return installed if os.path.exists(installed) else None
        if parsed.type == "local":
            resolved = self._resolve_path_from_base(parsed.path, self._get_base_dir_for_scope(scope))
            return resolved if os.path.exists(resolved) else None
        return None

    async def resolve(
        self,
        onMissing: Callable[[str], Any] | None = None,
    ) -> ResolvedPaths:
        accumulator = _Accumulator()
        global_settings = self.settingsManager.getGlobalSettings()
        project_settings = self.settingsManager.getProjectSettings()

        all_packages: list[tuple[PackageSource, SourceScope]] = [
            *[(pkg, "project") for pkg in project_settings.get("packages") or []],
            *[(pkg, "user") for pkg in global_settings.get("packages") or []],
        ]
        deduped_packages = self._dedupe_packages(all_packages)
        await self._resolve_package_sources(deduped_packages, accumulator, onMissing)

        global_base_dir = self.agentDir
        project_base_dir = os.path.join(self.cwd, CONFIG_DIR_NAME)
        for resource_type in RESOURCE_TYPES:
            target = self._get_target_map(accumulator, resource_type)
            self._resolve_local_entries(
                list(project_settings.get(resource_type) or []),
                resource_type,
                target,
                {"source": "local", "scope": "project", "origin": "top-level"},
                project_base_dir,
            )
            self._resolve_local_entries(
                list(global_settings.get(resource_type) or []),
                resource_type,
                target,
                {"source": "local", "scope": "user", "origin": "top-level"},
                global_base_dir,
            )

        self._add_auto_discovered_resources(
            accumulator,
            global_settings,
            project_settings,
            global_base_dir,
            project_base_dir,
        )
        return self._to_resolved_paths(accumulator)

    async def resolveExtensionSources(
        self,
        sources: list[str],
        options: dict[str, bool] | None = None,
    ) -> ResolvedPaths:
        accumulator = _Accumulator()
        if options and options.get("temporary"):
            scope: SourceScope = "temporary"
        elif options and options.get("local"):
            scope = "project"
        else:
            scope = "user"
        await self._resolve_package_sources([(cast(PackageSource, source), scope) for source in sources], accumulator)
        return self._to_resolved_paths(accumulator)

    def listConfiguredPackages(self) -> list[ConfiguredPackage]:
        configured: list[ConfiguredPackage] = []
        for pkg in self.settingsManager.getGlobalSettings().get("packages") or []:
            source = self._get_source_string(pkg)
            configured.append(
                ConfiguredPackage(
                    source=source,
                    scope="user",
                    filtered=isinstance(pkg, dict),
                    installedPath=self.getInstalledPath(source, "user"),
                )
            )
        for pkg in self.settingsManager.getProjectSettings().get("packages") or []:
            source = self._get_source_string(pkg)
            configured.append(
                ConfiguredPackage(
                    source=source,
                    scope="project",
                    filtered=isinstance(pkg, dict),
                    installedPath=self.getInstalledPath(source, "project"),
                )
            )
        return configured

    async def install(self, source: str, options: dict[str, bool] | None = None) -> None:
        parsed = self._parse_source(source)
        scope: SourceScope = "project" if options and options.get("local") else "user"

        async def operation() -> None:
            if parsed.type == "npm":
                await self._install_npm(parsed, scope, scope == "temporary")
                return
            if parsed.type == "git":
                await self._install_git(parsed, scope)
                return
            if parsed.type == "local":
                resolved = self._resolve_path_from_base(parsed.path, self.cwd)
                if not os.path.exists(resolved):
                    raise FileNotFoundError(f"Path does not exist: {resolved}")
                return
            raise ValueError(f"Unsupported install source: {source}")

        await self._with_progress("install", source, f"Installing {source}...", operation)

    async def installAndPersist(self, source: str, options: dict[str, bool] | None = None) -> None:
        await self.install(source, options)
        self.addSourceToSettings(source, options)

    async def remove(self, source: str, options: dict[str, bool] | None = None) -> None:
        parsed = self._parse_source(source)
        scope: SourceScope = "project" if options and options.get("local") else "user"

        async def operation() -> None:
            if parsed.type == "npm":
                await self._uninstall_npm(parsed, scope)
                return
            if parsed.type == "git":
                await self._remove_git(parsed, scope)
                return
            if parsed.type == "local":
                return
            raise ValueError(f"Unsupported remove source: {source}")

        await self._with_progress("remove", source, f"Removing {source}...", operation)

    async def removeAndPersist(self, source: str, options: dict[str, bool] | None = None) -> bool:
        await self.remove(source, options)
        return self.removeSourceFromSettings(source, options)

    async def update(self, source: str | None = None) -> None:
        global_settings = self.settingsManager.getGlobalSettings()
        project_settings = self.settingsManager.getProjectSettings()
        identity = self._get_package_identity(source) if source is not None else None
        matched = False
        update_sources: list[_ConfiguredUpdateSource] = []

        for pkg in global_settings.get("packages") or []:
            source_str = self._get_source_string(pkg)
            if identity and self._get_package_identity(source_str, "user") != identity:
                continue
            matched = True
            update_sources.append(_ConfiguredUpdateSource(source=source_str, scope="user"))

        for pkg in project_settings.get("packages") or []:
            source_str = self._get_source_string(pkg)
            if identity and self._get_package_identity(source_str, "project") != identity:
                continue
            matched = True
            update_sources.append(_ConfiguredUpdateSource(source=source_str, scope="project"))

        if source is not None and not matched:
            configured = [
                *(global_settings.get("packages") or []),
                *(project_settings.get("packages") or []),
            ]
            raise ValueError(self._build_no_matching_package_message(source, configured))
        await self._update_configured_sources(update_sources)

    async def checkForAvailableUpdates(self) -> list[PackageUpdate]:
        if _is_offline_mode_enabled():
            return []

        global_settings = self.settingsManager.getGlobalSettings()
        project_settings = self.settingsManager.getProjectSettings()
        all_packages: list[tuple[PackageSource, SourceScope]] = [
            *[(pkg, "project") for pkg in project_settings.get("packages") or []],
            *[(pkg, "user") for pkg in global_settings.get("packages") or []],
        ]

        checks: list[Callable[[], Awaitable[PackageUpdate | None]]] = []
        for pkg, scope in self._dedupe_packages(all_packages):
            if scope == "temporary":
                continue

            async def check(
                pkg: PackageSource = pkg,
                scope: Literal["user", "project"] = cast(Literal["user", "project"], scope),
            ) -> PackageUpdate | None:
                source = self._get_source_string(pkg)
                parsed = self._parse_source(source)
                if parsed.type == "local" or parsed.pinned:
                    return None

                if parsed.type == "npm":
                    installed = self._get_npm_install_path(parsed, scope)
                    if not os.path.exists(installed):
                        return None
                    if not await self._npm_has_available_update(parsed, installed):
                        return None
                    return PackageUpdate(
                        source=source,
                        displayName=parsed.name,
                        type="npm",
                        scope=scope,
                    )

                installed = self._get_git_install_path(parsed, scope)
                if not os.path.exists(installed):
                    return None
                if not await self._git_has_available_update(installed):
                    return None
                return PackageUpdate(
                    source=source,
                    displayName=f"{parsed.host}/{parsed.path}",
                    type="git",
                    scope=scope,
                )

            checks.append(check)

        results = await self._run_with_concurrency(checks, UPDATE_CHECK_CONCURRENCY)
        return [result for result in results if result is not None]

    def _set_scoped_packages(self, scope: SourceScope, packages: list[PackageSource]) -> None:
        if scope == "project":
            self.settingsManager.setProjectPackages(packages)
        else:
            self.settingsManager.setPackages(packages)

    def _emit_progress(
        self,
        event_type: Literal["start", "progress", "complete", "error"],
        action: Literal["install", "remove", "update", "clone", "pull"],
        source: str,
        message: str | None = None,
    ) -> None:
        if self.progressCallback is None:
            return
        self.progressCallback(ProgressEvent(type=event_type, action=action, source=source, message=message))

    async def _with_progress(
        self,
        action: Literal["install", "remove", "update", "clone", "pull"],
        source: str,
        message: str,
        operation: Callable[[], Any],
    ) -> None:
        self._emit_progress("start", action, source, message)
        try:
            await operation()
        except Exception as error:  # noqa: BLE001
            self._emit_progress("error", action, source, str(error))
            raise
        self._emit_progress("complete", action, source)

    async def _resolve_package_sources(
        self,
        sources: list[tuple[PackageSource, SourceScope]],
        accumulator: _Accumulator,
        onMissing: Callable[[str], Any] | None = None,
    ) -> None:
        for pkg, scope in sources:
            source_str = self._get_source_string(pkg)
            package_filter = pkg if isinstance(pkg, dict) else None
            metadata: PathMetadata = {"source": source_str, "scope": scope, "origin": "package"}
            parsed = self._parse_source(source_str)

            if parsed.type == "local":
                self._resolve_local_source(
                    parsed,
                    accumulator,
                    package_filter,
                    metadata,
                    self._get_base_dir_for_scope(scope),
                )
                continue

            async def install_missing(
                *,
                parsed: GitSource | _NpmSource,
                scope: SourceScope,
                source_str: str,
            ) -> bool:
                if _is_offline_mode_enabled():
                    return False
                if onMissing is not None:
                    action = await onMissing(source_str)
                    if action == "skip":
                        return False
                    if action == "error":
                        raise RuntimeError(f"Missing source: {source_str}")
                if parsed.type == "npm":
                    await self._install_npm(parsed, scope, scope == "temporary")
                    return True
                await self._install_git(parsed, scope)
                return True

            if parsed.type == "npm":
                installed = self._get_npm_install_path(parsed, scope)
                needs_install = not os.path.exists(installed) or (
                    parsed.pinned and not await self._installed_npm_matches_pinned_version(parsed, installed)
                )
                if needs_install:
                    if not await install_missing(parsed=parsed, scope=scope, source_str=source_str):
                        continue
                    installed = self._get_npm_install_path(parsed, scope)
                metadata["baseDir"] = installed
                self._collect_package_resources(installed, accumulator, package_filter, metadata)
                continue

            if parsed.type == "git":
                installed = self._get_git_install_path(parsed, scope)
                if not os.path.exists(installed):
                    if not await install_missing(parsed=parsed, scope=scope, source_str=source_str):
                        continue
                    installed = self._get_git_install_path(parsed, scope)
                elif scope == "temporary" and not parsed.pinned and not _is_offline_mode_enabled():
                    await self._refresh_temporary_git_source(parsed, source_str)

                metadata["baseDir"] = installed
                self._collect_package_resources(installed, accumulator, package_filter, metadata)

    def _get_source_string(self, pkg: PackageSource) -> str:
        return str(pkg if isinstance(pkg, str) else pkg["source"])

    def _normalize_source_for_settings(self, source: str, scope: SourceScope) -> str:
        parsed = self._parse_source(source)
        if parsed.type != "local":
            return source
        base_dir = self._get_base_dir_for_scope(scope)
        resolved = self._resolve_path(parsed.path)
        relative = os.path.relpath(resolved, base_dir)
        return relative or "."

    def _package_sources_match(self, existing: PackageSource, source: str, scope: SourceScope) -> bool:
        left = self._get_source_match_key_for_settings(self._get_source_string(existing), scope)
        right = self._get_source_match_key_for_input(source)
        return left == right

    def _get_source_match_key_for_input(self, source: str) -> str:
        parsed = self._parse_source(source)
        if parsed.type == "git":
            return f"git:{parsed.host}/{parsed.path}"
        if parsed.type == "npm":
            return f"npm:{parsed.name}"
        return f"local:{self._resolve_path(parsed.path)}"

    def _get_source_match_key_for_settings(self, source: str, scope: SourceScope) -> str:
        parsed = self._parse_source(source)
        if parsed.type == "git":
            return f"git:{parsed.host}/{parsed.path}"
        if parsed.type == "npm":
            return f"npm:{parsed.name}"
        base_dir = self._get_base_dir_for_scope(scope)
        return f"local:{self._resolve_path_from_base(parsed.path, base_dir)}"

    def _dedupe_packages(
        self,
        entries: list[tuple[PackageSource, SourceScope]],
    ) -> list[tuple[PackageSource, SourceScope]]:
        seen: dict[str, tuple[PackageSource, SourceScope]] = {}
        for pkg, scope in entries:
            identity = self._get_package_identity(self._get_source_string(pkg), scope)
            existing = seen.get(identity)
            if existing is None or (scope == "project" and existing[1] == "user"):
                seen[identity] = (pkg, scope)
        return list(seen.values())

    def _get_base_dir_for_scope(self, scope: SourceScope) -> str:
        if scope == "project":
            return os.path.join(self.cwd, CONFIG_DIR_NAME)
        if scope == "user":
            return self.agentDir
        return self.cwd

    def _resolve_path_from_base(self, value: str, base_dir: str) -> str:
        return resolve_path(value, base_dir, home_dir=_get_home_dir(), trim=True)

    def _resolve_path(self, value: str) -> str:
        return resolve_path(value, self.cwd, home_dir=_get_home_dir(), trim=True)

    def _parse_source(self, source: str) -> GitSource | _NpmSource | _LocalSource:
        trimmed = source.strip()
        if trimmed.startswith("npm:"):
            spec = trimmed[len("npm:") :].strip()
            name, version = self._parse_npm_spec(spec)
            return _NpmSource(type="npm", spec=spec, name=name, pinned=version is not None)

        if is_local_path(source):
            return _LocalSource(type="local", path=source)

        git_parsed = parse_git_url(trimmed)
        if git_parsed is not None:
            return git_parsed
        local_git_parsed = self._parse_local_file_git_source(trimmed)
        if local_git_parsed is not None:
            return local_git_parsed

        return _LocalSource(type="local", path=source)

    def _parse_local_file_git_source(self, source: str) -> GitSource | None:
        if not source.startswith("git:file://"):
            return None

        url = source[4:].strip()
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        if parsed.scheme != "file":
            return None

        raw_path = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
        normalized_path = raw_path.lstrip("/").removesuffix(".git")
        if not normalized_path:
            return None

        return GitSource(
            type="git",
            repo=url.rstrip("/"),
            host="local",
            path=normalized_path,
            ref=None,
            pinned=False,
        )

    def _parse_npm_spec(self, spec: str) -> tuple[str, str | None]:
        match = re.match(r"^(@?[^@]+(?:/[^@]+)?)(?:@(.+))?$", spec)
        if match is None:
            return spec, None
        return match.group(1) or spec, match.group(2)

    def _get_npm_command(self) -> tuple[str, list[str]]:
        configured = self.settingsManager.getNpmCommand()
        if not configured:
            return "npm", []
        command, *args = configured
        if not command:
            raise ValueError("Invalid npmCommand: first array entry must be a non-empty command")
        return command, args

    def _get_package_manager_name(self) -> str:
        command, args = self._get_npm_command()
        command_parts = [command, *args]
        package_manager_command = command
        if "--" in command_parts:
            separator_index = len(command_parts) - 1 - command_parts[::-1].index("--")
            if separator_index + 1 < len(command_parts):
                package_manager_command = command_parts[separator_index + 1]
        return re.sub(r"\.(cmd|exe)$", "", os.path.basename(package_manager_command), flags=re.IGNORECASE)

    async def _run_npm_command_capture(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        command, base_args = self._get_npm_command()
        return await self._run_command_capture(command, [*base_args, *args], cwd=cwd, timeout_ms=timeout_ms)

    async def _run_npm_command(self, args: list[str], *, cwd: str | None = None) -> None:
        command, base_args = self._get_npm_command()
        await self._run_command(command, [*base_args, *args], cwd=cwd)

    def _run_npm_command_sync(self, args: list[str]) -> str:
        command, base_args = self._get_npm_command()
        return self._run_command_sync(command, [*base_args, *args])

    def _get_git_dependency_install_args(self) -> list[str]:
        configured = self.settingsManager.getNpmCommand()
        if configured and len(configured) > 0:
            return ["install"]
        return ["install", "--omit=dev"]

    def _get_npm_install_args(self, specs: list[str], install_root: str) -> list[str]:
        package_manager_name = self._get_package_manager_name()
        if package_manager_name == "bun":
            return ["install", *specs, "--cwd", install_root]
        if package_manager_name == "pnpm":
            return ["install", *specs, "--prefix", install_root, "--config.strict-dep-builds=false"]
        return ["install", *specs, "--prefix", install_root]

    def _ensure_npm_project(self, install_root: str) -> None:
        os.makedirs(install_root, exist_ok=True)
        mark_path_ignored_by_cloud_sync(install_root)
        self._ensure_git_ignore(install_root)
        package_json_path = os.path.join(install_root, "package.json")
        if not os.path.exists(package_json_path):
            Path(package_json_path).write_text(
                json.dumps({"name": "harn-extensions", "private": True}, indent=2),
                encoding="utf-8",
            )

    def _get_npm_install_root(self, scope: SourceScope, temporary: bool) -> str:
        if temporary:
            return self._get_temporary_dir("npm")
        if scope == "project":
            return os.path.join(self.cwd, CONFIG_DIR_NAME, "npm")
        return os.path.join(self.agentDir, "npm")

    def _get_managed_npm_install_path(self, source: _NpmSource, scope: SourceScope) -> str:
        return os.path.join(self._get_npm_install_root(scope, scope == "temporary"), "node_modules", source.name)

    def _get_global_npm_root(self) -> str:
        command, args = self._get_npm_command()
        command_key = "\0".join([command, *args])
        if self.globalNpmRoot and self.globalNpmRootCommandKey == command_key:
            return self.globalNpmRoot
        if self._get_package_manager_name() == "bun":
            bin_dir = self._run_npm_command_sync(["pm", "bin", "-g"]).strip()
            self.globalNpmRoot = os.path.join(os.path.dirname(bin_dir), "install", "global", "node_modules")
        else:
            self.globalNpmRoot = self._run_npm_command_sync(["root", "-g"]).strip()
        self.globalNpmRootCommandKey = command_key
        return self.globalNpmRoot

    def _get_pnpm_global_package_path(self, package_name: str) -> str | None:
        if self._get_package_manager_name() != "pnpm":
            return None
        output = self._run_npm_command_sync(["list", "-g", "--depth", "0", "--json"])
        entries = json.loads(output)
        if not isinstance(entries, list):
            return None
        for entry in entries:
            path = ((entry or {}).get("dependencies") or {}).get(package_name, {}).get("path")
            if isinstance(path, str) and path:
                return path
        return None

    def _get_legacy_global_npm_install_path(self, source: _NpmSource) -> str | None:
        try:
            return self._get_pnpm_global_package_path(source.name) or os.path.join(
                self._get_global_npm_root(),
                source.name,
            )
        except Exception:
            return None

    def _get_npm_install_path(self, source: _NpmSource, scope: SourceScope) -> str:
        managed_path = self._get_managed_npm_install_path(source, scope)
        if scope != "user" or os.path.exists(managed_path):
            return managed_path
        legacy_path = self._get_legacy_global_npm_install_path(source)
        return legacy_path if legacy_path and os.path.exists(legacy_path) else managed_path

    def _get_package_identity(self, source: str, scope: SourceScope | None = None) -> str:
        parsed = self._parse_source(source)
        if parsed.type == "npm":
            return f"npm:{parsed.name}"
        if parsed.type == "git":
            return f"git:{parsed.host}/{parsed.path}"
        if scope is not None:
            return f"local:{self._resolve_path_from_base(parsed.path, self._get_base_dir_for_scope(scope))}"
        return f"local:{self._resolve_path(parsed.path)}"

    def _build_no_matching_package_message(self, source: str, configured_packages: list[PackageSource]) -> str:
        suggestion = self._find_suggested_configured_source(source, configured_packages)
        if suggestion is None:
            return f"No matching package found for {source}"
        return f"No matching package found for {source}. Did you mean {suggestion}?"

    def _find_suggested_configured_source(
        self,
        source: str,
        configured_packages: list[PackageSource],
    ) -> str | None:
        trimmed = source.strip()
        for pkg in configured_packages:
            source_str = self._get_source_string(pkg)
            parsed = self._parse_source(source_str)
            if parsed.type == "npm":
                if trimmed in {parsed.name, parsed.spec}:
                    return source_str
                continue
            if parsed.type == "git":
                shorthand = f"{parsed.host}/{parsed.path}"
                shorthand_with_ref = f"{shorthand}@{parsed.ref}" if parsed.ref else None
                if trimmed == shorthand or trimmed == shorthand_with_ref:
                    return source_str
        return None

    def _resolve_local_source(
        self,
        source: _LocalSource,
        accumulator: _Accumulator,
        package_filter: PackageFilter | None,
        metadata: PathMetadata,
        base_dir: str,
    ) -> None:
        resolved = self._resolve_path_from_base(source.path, base_dir)
        if not os.path.exists(resolved):
            return

        if os.path.isfile(resolved):
            metadata["baseDir"] = os.path.dirname(resolved)
            self._add_resource(accumulator.extensions, resolved, metadata, True)
            return

        metadata["baseDir"] = resolved
        if self._collect_package_resources(resolved, accumulator, package_filter, metadata):
            return

        self._add_resource(accumulator.extensions, resolved, metadata, True)

    async def _run_command_capture(
        self,
        command: str,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        child = self._spawn_capture_command(command, args, cwd=cwd, env=env)
        timeout_seconds = None if timeout_ms is None else timeout_ms / 1000
        try:
            stdout, stderr = await asyncio.to_thread(child.communicate, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            child.kill()
            await asyncio.to_thread(child.communicate)
            raise RuntimeError(f"{self._format_command_display(command, args)} timed out after {timeout_ms}ms") from error

        if child.returncode == 0:
            return (stdout or "").strip()

        if child.returncode is None:
            exit_status = "signal unknown"
        elif child.returncode < 0:
            try:
                signal_name = signal_module.Signals(-child.returncode).name
            except ValueError:
                signal_name = str(-child.returncode)
            exit_status = f"signal {signal_name}"
        else:
            exit_status = f"code {child.returncode}"

        raise RuntimeError(
            f"{self._format_command_display(command, args)} failed with {exit_status}: {(stderr or stdout) or ''}"
        )

    async def _run_git_remote_command(self, installed_path: str, args: list[str]) -> str:
        return await self._run_command_capture(
            "git",
            args,
            cwd=installed_path,
            env={"GIT_TERMINAL_PROMPT": "0"},
            timeout_ms=NETWORK_TIMEOUT_MS,
        )

    async def _install_npm(self, source: _NpmSource, scope: SourceScope, temporary: bool) -> None:
        install_root = self._get_npm_install_root(scope, temporary)
        self._ensure_npm_project(install_root)
        await self._run_npm_command(self._get_npm_install_args([source.spec], install_root))

    async def _uninstall_npm(self, source: _NpmSource, scope: SourceScope) -> None:
        install_root = self._get_npm_install_root(scope, False)
        if not os.path.exists(install_root):
            return
        if self._get_package_manager_name() == "bun":
            await self._run_npm_command(["uninstall", source.name, "--cwd", install_root])
            return
        await self._run_npm_command(["uninstall", source.name, "--prefix", install_root])

    async def _get_latest_npm_version(self, package_name: str) -> str:
        raw = (
            await self._run_npm_command_capture(
                ["view", package_name, "version", "--json"],
                cwd=self.cwd,
                timeout_ms=NETWORK_TIMEOUT_MS,
            )
        ).strip()
        if not raw:
            raise RuntimeError("Empty response from npm view")
        parsed = json.loads(raw)
        if isinstance(parsed, str):
            return parsed
        raise RuntimeError(f"Invalid npm view response for {package_name}: {raw}")

    def _get_installed_npm_version(self, installed_path: str) -> str | None:
        package_json_path = os.path.join(installed_path, "package.json")
        if not os.path.exists(package_json_path):
            return None
        try:
            payload = json.loads(Path(package_json_path).read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return None
        version = payload.get("version")
        return version if isinstance(version, str) else None

    async def _installed_npm_matches_pinned_version(self, source: _NpmSource, installed_path: str) -> bool:
        installed_version = self._get_installed_npm_version(installed_path)
        if not installed_version:
            return False
        _name, pinned_version = self._parse_npm_spec(source.spec)
        if not pinned_version:
            return True
        return installed_version == pinned_version

    async def _npm_has_available_update(self, source: _NpmSource, installed_path: str) -> bool:
        if _is_offline_mode_enabled():
            return False
        installed_version = self._get_installed_npm_version(installed_path)
        if not installed_version:
            return False
        try:
            latest_version = await self._get_latest_npm_version(source.name)
        except Exception:
            return False
        return latest_version != installed_version

    async def _should_update_npm_source(self, source: _NpmSource, scope: Literal["user", "project"]) -> bool:
        installed_path = self._get_managed_npm_install_path(source, scope)
        installed_version = self._get_installed_npm_version(installed_path) if os.path.exists(installed_path) else None
        if not installed_version:
            return True
        try:
            latest_version = await self._get_latest_npm_version(source.name)
        except Exception:
            return True
        return latest_version != installed_version

    async def _update_configured_sources(self, sources: list[_ConfiguredUpdateSource]) -> None:
        if _is_offline_mode_enabled() or not sources:
            return

        npm_candidates: list[_NpmUpdateTarget] = []
        git_candidates: list[_GitConfiguredUpdateSource] = []

        for entry in sources:
            parsed = self._parse_source(entry.source)
            if parsed.type == "local" or parsed.pinned:
                continue
            if parsed.type == "npm":
                npm_candidates.append(_NpmUpdateTarget(source=entry.source, scope=entry.scope, parsed=parsed))
                continue
            git_candidates.append(_GitConfiguredUpdateSource(source=entry.source, scope=entry.scope, parsed=parsed))

        async def check_npm(entry: _NpmUpdateTarget) -> tuple[_NpmUpdateTarget, bool]:
            return entry, await self._should_update_npm_source(entry.parsed, entry.scope)

        npm_check_results = await self._run_with_concurrency(
            [lambda entry=entry: check_npm(entry) for entry in npm_candidates],
            UPDATE_CHECK_CONCURRENCY,
        )

        user_npm_updates: list[_NpmUpdateTarget] = []
        project_npm_updates: list[_NpmUpdateTarget] = []
        for entry, should_update in npm_check_results:
            if not should_update:
                continue
            if entry.scope == "user":
                user_npm_updates.append(entry)
            else:
                project_npm_updates.append(entry)

        tasks: list[Awaitable[object]] = []
        if user_npm_updates:
            tasks.append(self._update_npm_batch(user_npm_updates, "user"))
        if project_npm_updates:
            tasks.append(self._update_npm_batch(project_npm_updates, "project"))
        if git_candidates:
            git_tasks = [
                lambda entry=entry: self._with_progress(
                    "update",
                    entry.source,
                    f"Updating {entry.source}...",
                    lambda entry=entry: self._update_git(entry.parsed, entry.scope),
                )
                for entry in git_candidates
            ]
            tasks.append(self._run_with_concurrency(git_tasks, GIT_UPDATE_CONCURRENCY))

        if tasks:
            await asyncio.gather(*tasks)

    async def _update_npm_batch(
        self,
        sources: list[_NpmUpdateTarget],
        scope: Literal["user", "project"],
    ) -> None:
        if not sources:
            return

        source_label = sources[0].source if len(sources) == 1 else f"{scope} npm packages"
        message = f"Updating {sources[0].source}..." if len(sources) == 1 else f"Updating {scope} npm packages..."
        specs = [f"{entry.parsed.name}@latest" for entry in sources]

        await self._with_progress(
            "update",
            source_label,
            message,
            lambda: self._install_npm_batch(specs, scope),
        )

    async def _install_npm_batch(self, specs: list[str], scope: Literal["user", "project"]) -> None:
        install_root = self._get_npm_install_root(scope, False)
        self._ensure_npm_project(install_root)
        await self._run_npm_command(self._get_npm_install_args(specs, install_root))

    async def _git_has_available_update(self, installed_path: str) -> bool:
        if _is_offline_mode_enabled():
            return False
        try:
            local_head = await self._run_command_capture(
                "git",
                ["rev-parse", "HEAD"],
                cwd=installed_path,
                timeout_ms=NETWORK_TIMEOUT_MS,
            )
            remote_head = await self._get_remote_git_head(installed_path)
        except Exception:
            return False
        return local_head.strip() != remote_head.strip()

    async def _get_remote_git_head(self, installed_path: str) -> str:
        upstream_ref = await self._get_git_upstream_ref(installed_path)
        if upstream_ref is not None:
            remote_head = await self._run_git_remote_command(installed_path, ["ls-remote", "origin", upstream_ref])
            match = _GIT_HEAD_RE.search(remote_head)
            if match is not None:
                return match.group(1)

        remote_head = await self._run_git_remote_command(installed_path, ["ls-remote", "origin", "HEAD"])
        match = _GIT_HEAD_EXACT_RE.search(remote_head)
        if match is None:
            raise RuntimeError("Failed to determine remote HEAD")
        return match.group(1)

    async def _get_local_git_update_target(self, installed_path: str) -> _GitUpdateTarget:
        try:
            upstream = (
                await self._run_command_capture(
                    "git",
                    ["rev-parse", "--abbrev-ref", "@{upstream}"],
                    cwd=installed_path,
                    timeout_ms=NETWORK_TIMEOUT_MS,
                )
            ).strip()
            if not upstream.startswith("origin/"):
                raise RuntimeError(f"Unsupported upstream remote: {upstream}")
            branch = upstream[len("origin/") :]
            if not branch:
                raise RuntimeError("Missing upstream branch name")
            head = await self._run_command_capture(
                "git",
                ["rev-parse", "@{upstream}"],
                cwd=installed_path,
                timeout_ms=NETWORK_TIMEOUT_MS,
            )
            return _GitUpdateTarget(
                ref="@{upstream}",
                head=head,
                fetch_args=[
                    "fetch",
                    "--prune",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                ],
            )
        except Exception:
            try:
                await self._run_command("git", ["remote", "set-head", "origin", "-a"], cwd=installed_path)
            except Exception:
                pass
            head = await self._run_command_capture(
                "git",
                ["rev-parse", "origin/HEAD"],
                cwd=installed_path,
                timeout_ms=NETWORK_TIMEOUT_MS,
            )
            try:
                origin_head_ref = await self._run_command_capture(
                    "git",
                    ["symbolic-ref", "refs/remotes/origin/HEAD"],
                    cwd=installed_path,
                    timeout_ms=NETWORK_TIMEOUT_MS,
                )
            except Exception:
                origin_head_ref = ""
            branch = origin_head_ref.strip().removeprefix("refs/remotes/origin/")
            if branch:
                return _GitUpdateTarget(
                    ref="origin/HEAD",
                    head=head,
                    fetch_args=[
                        "fetch",
                        "--prune",
                        "--no-tags",
                        "origin",
                        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                    ],
                )
            return _GitUpdateTarget(
                ref="origin/HEAD",
                head=head,
                fetch_args=["fetch", "--prune", "--no-tags", "origin", "+HEAD:refs/remotes/origin/HEAD"],
            )

    async def _get_git_upstream_ref(self, installed_path: str) -> str | None:
        try:
            upstream = (
                await self._run_command_capture(
                    "git",
                    ["rev-parse", "--abbrev-ref", "@{upstream}"],
                    cwd=installed_path,
                    timeout_ms=NETWORK_TIMEOUT_MS,
                )
            ).strip()
        except Exception:
            return None
        if not upstream.startswith("origin/"):
            return None
        branch = upstream[len("origin/") :]
        return f"refs/heads/{branch}" if branch else None

    def _ensure_git_ignore(self, directory: str) -> None:
        os.makedirs(directory, exist_ok=True)
        ignore_path = os.path.join(directory, ".gitignore")
        if not os.path.exists(ignore_path):
            Path(ignore_path).write_text("*\n!.gitignore\n", encoding="utf-8")

    def _get_temporary_dir(self, prefix: str, suffix: str | None = None) -> str:
        digest = hashlib.sha256(f"{prefix}-{suffix or ''}".encode()).hexdigest()[:8]
        base = os.path.join(tempfile.gettempdir(), "harn-extensions", prefix, digest)
        return os.path.join(base, suffix) if suffix else base

    def _get_git_install_path(self, source: GitSource, scope: SourceScope) -> str:
        host = source.host or "local"
        if scope == "temporary":
            return self._get_temporary_dir(f"git-{host}", source.path)
        if scope == "project":
            return os.path.join(self.cwd, CONFIG_DIR_NAME, "git", host, source.path)
        return os.path.join(self.agentDir, "git", host, source.path)

    def _get_git_install_root(self, scope: SourceScope) -> str | None:
        if scope == "temporary":
            return None
        if scope == "project":
            return os.path.join(self.cwd, CONFIG_DIR_NAME, "git")
        return os.path.join(self.agentDir, "git")

    async def _install_git(self, source: GitSource, scope: SourceScope) -> None:
        target_dir = self._get_git_install_path(source, scope)
        if os.path.exists(target_dir):
            if source.ref:
                await self._ensure_git_ref(target_dir, ["fetch", "origin", source.ref], "FETCH_HEAD")
                return
            target = await self._get_local_git_update_target(target_dir)
            await self._ensure_git_ref(target_dir, target.fetch_args, target.ref)
            return

        git_root = self._get_git_install_root(scope)
        if git_root is not None:
            self._ensure_git_ignore(git_root)
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)

        await self._run_command("git", ["clone", source.repo, target_dir])
        if source.ref:
            await self._run_command("git", ["checkout", source.ref], cwd=target_dir)

        package_json_path = os.path.join(target_dir, "package.json")
        if os.path.exists(package_json_path):
            await self._run_npm_command(self._get_git_dependency_install_args(), cwd=target_dir)

    async def _update_git(self, source: GitSource, scope: SourceScope) -> None:
        target_dir = self._get_git_install_path(source, scope)
        if not os.path.exists(target_dir):
            await self._install_git(source, scope)
            return

        target = await self._get_local_git_update_target(target_dir)
        await self._ensure_git_ref(target_dir, target.fetch_args, target.ref)

    async def _ensure_git_ref(self, target_dir: str, fetch_args: list[str], ref: str) -> None:
        await self._run_command("git", fetch_args, cwd=target_dir)
        local_head = await self._run_command_capture(
            "git",
            ["rev-parse", "HEAD"],
            cwd=target_dir,
            timeout_ms=NETWORK_TIMEOUT_MS,
        )
        target_head = await self._run_command_capture(
            "git",
            ["rev-parse", ref],
            cwd=target_dir,
            timeout_ms=NETWORK_TIMEOUT_MS,
        )
        if local_head.strip() == target_head.strip():
            return

        await self._run_command("git", ["reset", "--hard", ref], cwd=target_dir)
        await self._run_command("git", ["clean", "-fdx"], cwd=target_dir)

        package_json_path = os.path.join(target_dir, "package.json")
        if os.path.exists(package_json_path):
            await self._run_npm_command(self._get_git_dependency_install_args(), cwd=target_dir)

    async def _refresh_temporary_git_source(self, source: GitSource, source_str: str) -> None:
        if _is_offline_mode_enabled():
            return
        try:
            await self._with_progress(
                "pull",
                source_str,
                f"Refreshing {source_str}...",
                lambda: self._update_git(source, "temporary"),
            )
        except Exception:
            return

    async def _remove_git(self, source: GitSource, scope: SourceScope) -> None:
        target_dir = self._get_git_install_path(source, scope)
        if not os.path.exists(target_dir):
            return
        shutil.rmtree(target_dir, ignore_errors=True)
        self._prune_empty_git_parents(target_dir, self._get_git_install_root(scope))

    def _prune_empty_git_parents(self, target_dir: str, install_root: str | None) -> None:
        if install_root is None:
            return
        resolved_root = os.path.abspath(install_root)
        current = os.path.dirname(target_dir)
        while current.startswith(resolved_root) and current != resolved_root:
            if not os.path.exists(current):
                current = os.path.dirname(current)
                continue
            try:
                if os.listdir(current):
                    break
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)

    def _collect_package_resources(
        self,
        package_root: str,
        accumulator: _Accumulator,
        package_filter: PackageFilter | None,
        metadata: PathMetadata,
    ) -> bool:
        if package_filter:
            for resource_type in RESOURCE_TYPES:
                target = self._get_target_map(accumulator, resource_type)
                patterns = package_filter.get(resource_type)
                if patterns is not None:
                    self._apply_package_filter(package_root, patterns, resource_type, target, metadata)
                else:
                    self._collect_default_resources(package_root, resource_type, target, metadata)
            return True

        manifest = _read_harn_manifest(package_root)
        if manifest:
            for resource_type in RESOURCE_TYPES:
                self._add_manifest_entries(
                    manifest.get(resource_type),
                    package_root,
                    resource_type,
                    self._get_target_map(accumulator, resource_type),
                    metadata,
                )
            return True

        has_any_dir = False
        for resource_type in RESOURCE_TYPES:
            directory = os.path.join(package_root, resource_type)
            if not os.path.exists(directory):
                continue
            has_any_dir = True
            for file_path in _collect_resource_files(directory, resource_type):
                self._add_resource(self._get_target_map(accumulator, resource_type), file_path, metadata, True)
        return has_any_dir

    def _collect_default_resources(
        self,
        package_root: str,
        resource_type: ResourceType,
        target: dict[str, tuple[PathMetadata, bool]],
        metadata: PathMetadata,
    ) -> None:
        manifest = _read_harn_manifest(package_root)
        entries = manifest.get(resource_type) if manifest else None
        if entries:
            self._add_manifest_entries(entries, package_root, resource_type, target, metadata)
            return
        directory = os.path.join(package_root, resource_type)
        if not os.path.exists(directory):
            return
        for file_path in _collect_resource_files(directory, resource_type):
            self._add_resource(target, file_path, metadata, True)

    def _apply_package_filter(
        self,
        package_root: str,
        user_patterns: list[str],
        resource_type: ResourceType,
        target: dict[str, tuple[PathMetadata, bool]],
        metadata: PathMetadata,
    ) -> None:
        all_files = self._collect_manifest_files(package_root, resource_type).allFiles
        if not user_patterns:
            for file_path in all_files:
                self._add_resource(target, file_path, metadata, False)
            return
        enabled_paths = _apply_patterns(all_files, user_patterns, package_root)
        for file_path in all_files:
            self._add_resource(target, file_path, metadata, file_path in enabled_paths)

    def _collect_manifest_files(self, package_root: str, resource_type: ResourceType) -> _ManifestFiles:
        manifest = _read_harn_manifest(package_root)
        entries = manifest.get(resource_type) if manifest else None
        if entries:
            all_files = self._collect_files_from_manifest_entries(entries, package_root, resource_type)
            manifest_patterns = [entry for entry in entries if _is_override_pattern(entry)]
            enabled_by_manifest = (
                _apply_patterns(all_files, manifest_patterns, package_root)
                if manifest_patterns
                else set(all_files)
            )
            return _ManifestFiles(allFiles=list(enabled_by_manifest), enabledByManifest=enabled_by_manifest)
        convention_dir = os.path.join(package_root, resource_type)
        if not os.path.exists(convention_dir):
            return _ManifestFiles(allFiles=[], enabledByManifest=set())
        all_files = _collect_resource_files(convention_dir, resource_type)
        return _ManifestFiles(allFiles=all_files, enabledByManifest=set(all_files))

    def _add_manifest_entries(
        self,
        entries: list[str] | None,
        root: str,
        resource_type: ResourceType,
        target: dict[str, tuple[PathMetadata, bool]],
        metadata: PathMetadata,
    ) -> None:
        if not entries:
            return
        all_files = self._collect_files_from_manifest_entries(entries, root, resource_type)
        patterns = [entry for entry in entries if _is_override_pattern(entry)]
        enabled_paths = _apply_patterns(all_files, patterns, root)
        for file_path in all_files:
            if file_path in enabled_paths:
                self._add_resource(target, file_path, metadata, True)

    def _collect_files_from_manifest_entries(
        self,
        entries: list[str],
        root: str,
        resource_type: ResourceType,
    ) -> list[str]:
        source_entries = [entry for entry in entries if not _is_override_pattern(entry)]
        resolved_paths: list[str] = []
        for entry in source_entries:
            if _has_glob_pattern(entry):
                resolved_paths.extend(
                    os.path.abspath(os.path.join(root, match))
                    for match in wc_glob.glob(entry, root_dir=root, flags=_GLOB_MATCH_FLAGS)
                )
            else:
                resolved_paths.append(os.path.abspath(os.path.join(root, entry)))
        return self._collect_files_from_paths(resolved_paths, resource_type)

    def _resolve_local_entries(
        self,
        entries: list[str],
        resource_type: ResourceType,
        target: dict[str, tuple[PathMetadata, bool]],
        metadata: PathMetadata,
        base_dir: str,
    ) -> None:
        if not entries:
            return
        plain, patterns = _split_patterns(entries)
        resolved_plain = [self._resolve_path_from_base(path, base_dir) for path in plain]
        all_files = self._collect_files_from_paths(resolved_plain, resource_type)
        enabled_paths = _apply_patterns(all_files, patterns, base_dir)
        for file_path in all_files:
            self._add_resource(target, file_path, metadata, file_path in enabled_paths)

    def _add_auto_discovered_resources(
        self,
        accumulator: _Accumulator,
        global_settings: dict[str, Any],
        project_settings: dict[str, Any],
        global_base_dir: str,
        project_base_dir: str,
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
        user_agents_skills_dir = os.path.join(_get_home_dir(), ".agents", "skills")
        project_agents_skill_dirs = [
            directory
            for directory in _collect_ancestor_agents_skill_dirs(self.cwd)
            if os.path.abspath(directory) != os.path.abspath(user_agents_skills_dir)
        ]

        def add_resources(
            resource_type: ResourceType,
            paths: list[str],
            metadata: PathMetadata,
            overrides: list[str],
            base_dir: str,
        ) -> None:
            target = self._get_target_map(accumulator, resource_type)
            for path in paths:
                self._add_resource(target, path, metadata, self._is_enabled_by_overrides(path, overrides, base_dir))

        add_resources(
            "extensions",
            _collect_auto_extension_entries(project_dirs["extensions"]),
            project_metadata,
            project_overrides["extensions"],
            project_base_dir,
        )
        add_resources(
            "skills",
            _collect_auto_skill_entries(project_dirs["skills"], "harn"),
            project_metadata,
            project_overrides["skills"],
            project_base_dir,
        )
        for agents_dir in project_agents_skill_dirs:
            agents_base_dir = os.path.dirname(agents_dir)
            add_resources(
                "skills",
                _collect_auto_skill_entries(agents_dir, "agents"),
                {
                    "source": "auto",
                    "scope": "project",
                    "origin": "top-level",
                    "baseDir": agents_base_dir,
                },
                project_overrides["skills"],
                agents_base_dir,
            )
        add_resources(
            "prompts",
            _collect_auto_prompt_entries(project_dirs["prompts"]),
            project_metadata,
            project_overrides["prompts"],
            project_base_dir,
        )
        add_resources(
            "themes",
            _collect_auto_theme_entries(project_dirs["themes"]),
            project_metadata,
            project_overrides["themes"],
            project_base_dir,
        )

        add_resources(
            "extensions",
            _collect_auto_extension_entries(user_dirs["extensions"]),
            user_metadata,
            user_overrides["extensions"],
            global_base_dir,
        )
        add_resources(
            "skills",
            _collect_auto_skill_entries(user_dirs["skills"], "harn"),
            user_metadata,
            user_overrides["skills"],
            global_base_dir,
        )
        user_agents_base_dir = os.path.dirname(user_agents_skills_dir)
        add_resources(
            "skills",
            _collect_auto_skill_entries(user_agents_skills_dir, "agents"),
            {
                "source": "auto",
                "scope": "user",
                "origin": "top-level",
                "baseDir": user_agents_base_dir,
            },
            user_overrides["skills"],
            user_agents_base_dir,
        )
        add_resources(
            "prompts",
            _collect_auto_prompt_entries(user_dirs["prompts"]),
            user_metadata,
            user_overrides["prompts"],
            global_base_dir,
        )
        add_resources(
            "themes",
            _collect_auto_theme_entries(user_dirs["themes"]),
            user_metadata,
            user_overrides["themes"],
            global_base_dir,
        )

    def _collect_files_from_paths(self, paths: list[str], resource_type: ResourceType) -> list[str]:
        files: list[str] = []
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                if os.path.isfile(path):
                    files.append(path)
                elif os.path.isdir(path):
                    files.extend(_collect_resource_files(path, resource_type))
            except OSError:
                continue
        return files

    def _get_target_map(
        self,
        accumulator: _Accumulator,
        resource_type: ResourceType,
    ) -> dict[str, tuple[PathMetadata, bool]]:
        return cast(dict[str, tuple[PathMetadata, bool]], getattr(accumulator, resource_type))

    def _add_resource(
        self,
        target: dict[str, tuple[PathMetadata, bool]],
        path: str,
        metadata: PathMetadata,
        enabled: bool,
    ) -> None:
        if path and path not in target:
            target[path] = (metadata, enabled)

    def _is_enabled_by_overrides(self, path: str, patterns: list[str], base_dir: str) -> bool:
        if not patterns:
            return True
        patterns = [pattern for pattern in patterns if _is_override_pattern(pattern)]
        enabled = True
        excludes = [pattern[1:] for pattern in patterns if pattern.startswith("!")]
        force_includes = [pattern[1:] for pattern in patterns if pattern.startswith("+")]
        force_excludes = [pattern[1:] for pattern in patterns if pattern.startswith("-")]
        if excludes and _matches_any_pattern(path, excludes, base_dir):
            enabled = False
        if force_includes and _matches_any_exact_pattern(path, force_includes, base_dir):
            enabled = True
        if force_excludes and _matches_any_exact_pattern(path, force_excludes, base_dir):
            enabled = False
        return enabled

    def _to_resolved_paths(self, accumulator: _Accumulator) -> ResolvedPaths:
        def materialize(entries: dict[str, tuple[PathMetadata, bool]]) -> list[ResolvedResource]:
            resolved = [
                ResolvedResource(path=path, metadata=metadata, enabled=enabled)
                for path, (metadata, enabled) in entries.items()
            ]
            resolved.sort(key=lambda entry: _resource_precedence_rank(entry.metadata))
            seen: set[str] = set()
            deduped: list[ResolvedResource] = []
            for entry in resolved:
                canonical = canonicalize_path(entry.path)
                if canonical in seen:
                    continue
                seen.add(canonical)
                deduped.append(entry)
            return deduped

        return ResolvedPaths(
            extensions=materialize(accumulator.extensions),
            skills=materialize(accumulator.skills),
            prompts=materialize(accumulator.prompts),
            themes=materialize(accumulator.themes),
        )

    async def _run_with_concurrency(
        self,
        tasks: list[Callable[[], Awaitable[_T]]],
        limit: int,
    ) -> list[_T]:
        if not tasks:
            return []

        results: list[_T | None] = [None] * len(tasks)
        next_index = 0
        lock = asyncio.Lock()
        worker_count = max(1, min(limit, len(tasks)))

        async def worker() -> None:
            nonlocal next_index
            while True:
                async with lock:
                    index = next_index
                    next_index += 1
                if index >= len(tasks):
                    return
                results[index] = await tasks[index]()

        await asyncio.gather(*(worker() for _ in range(worker_count)))
        return [cast(_T, result) for result in results]

    def _spawn_command(
        self,
        command: str,
        args: list[str],
        *,
        cwd: str | None = None,
    ) -> subprocess.Popen[bytes] | subprocess.Popen[str]:
        env = dict(_get_env())
        if isStdoutTakenOver():
            return subprocess.Popen(
                [command, *args],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
        return subprocess.Popen([command, *args], cwd=cwd, env=env)

    def _spawn_capture_command(
        self,
        command: str,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        merged_env = dict(_get_env())
        if env:
            merged_env.update(env)
        return subprocess.Popen(
            [command, *args],
            cwd=cwd,
            env=merged_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    async def _run_command(self, command: str, args: list[str], *, cwd: str | None = None) -> None:
        child = self._spawn_command(command, args, cwd=cwd)
        code = await asyncio.to_thread(child.wait)
        if code == 0:
            return
        raise RuntimeError(f"{self._format_command_display(command, args)} failed with code {code}")

    def _run_command_sync(self, command: str, args: list[str]) -> str:
        result = spawn_process_sync(
            command,
            args,
            env=dict(_get_env()),
            encoding="utf-8",
            stdio=("ignore", "pipe", "pipe"),
        )
        if result.error is not None or result.status != 0:
            details = str(result.error) if result.error is not None else None
            raise RuntimeError(
                f"Failed to run {self._format_command_display(command, args)}: "
                f"{details or result.stderr or result.stdout}"
            )
        return (result.stdout or result.stderr or "").strip()

    def _format_command_display(self, command: str, args: list[str]) -> str:
        return " ".join([command, *args])

__all__ = [
    "PathMetadata",
    "ResolvedResource",
    "ResolvedPaths",
    "MissingSourceAction",
    "ProgressEvent",
    "ProgressCallback",
    "PackageUpdate",
    "ConfiguredPackage",
    "PackageManager",
    "DefaultPackageManager",
]
