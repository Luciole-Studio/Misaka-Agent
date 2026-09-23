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
from misaka.core.source_info import SourceScope
from misaka.utils.paths import (
    canonicalize_path,
    is_local_path,
    resolve_path,
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


def _to_posix_path(path: str) -> str:
    return path.replace(os.sep, "/")


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

    pattern = pattern.removeprefix("/")

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
            content = Path(ignore_path).read_text(encoding="utf-8-sig")
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
    return any(_match_pattern(pattern, rel, name, full) for pattern in patterns)


def _normalize_exact_pattern(pattern: str) -> str:
    if pattern.startswith(("./", ".\\")):
        pattern = pattern[2:]
    return _to_posix_path(pattern)


def _matches_any_exact_pattern(file_path: str, patterns: list[str], base_dir: str) -> bool:
    if not patterns:
        return False
    rel = _to_posix_path(os.path.relpath(file_path, base_dir))
    full = _to_posix_path(file_path)
    return any(_normalize_exact_pattern(pattern) in {rel, full} for pattern in patterns)


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
    if resource_type == "extensions":
        return _collect_auto_extension_entries(dir_path)
    if resource_type == "prompts":
        return _collect_files(dir_path, lambda name: name.endswith(".md"))
    return _collect_files(dir_path, lambda name: name.endswith(".json"))


class DefaultPackageManager:
    def __init__(self, options: PackageManagerOptions | dict[str, Any]) -> None:
        self.cwd = resolve_path(str(options["cwd"]))
        self.agentDir = resolve_path(str(options["agentDir"]))
        self.settingsManager = cast(SettingsManager, options["settingsManager"])


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
            target = self._get_target_map(accumulator, resource_type)
            if project_trusted and resource_type != "extensions":
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
            project_trusted=project_trusted,
        )
        return self._to_resolved_paths(accumulator)

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
        for source in sources:
            if not is_local_path(source):
                raise ValueError(f"Extension sources must be local paths (packages are not supported): {source}")
            self._resolve_local_source(
                source, accumulator, {"source": source, "scope": scope, "origin": "package"},
                self._get_base_dir_for_scope(scope),
            )
        return self._to_resolved_paths(accumulator)

    def _get_base_dir_for_scope(self, scope: SourceScope) -> str:
        if scope == "project":
            return str(home.project_dir(self.cwd) or self.cwd)
        if scope == "user":
            return self.agentDir
        return self.cwd

    def _resolve_path_from_base(self, value: str, base_dir: str) -> str:
        return resolve_path(value, base_dir, home_dir=_get_home_dir(), trim=True)

    def _resolve_path(self, value: str) -> str:
        return resolve_path(value, self.cwd, home_dir=_get_home_dir(), trim=True)


    def _resolve_local_source(
        self,
        source: str,
        accumulator: _Accumulator,
        metadata: PathMetadata,
        base_dir: str,
    ) -> None:
        resolved = self._resolve_path_from_base(source, base_dir)
        if not os.path.exists(resolved):
            return

        if os.path.isfile(resolved):
            metadata["baseDir"] = os.path.dirname(resolved)
            self._add_resource(accumulator.extensions, resolved, metadata, True)
            return

        metadata["baseDir"] = resolved
        if self._collect_package_resources(resolved, accumulator, metadata):
            return

        from misaka.core.extensions.loader import resolve_extension_entries

        entries = resolve_extension_entries(resolved)
        if entries is not None:
            for entry in entries:
                self._add_resource(accumulator.extensions, entry, metadata, True)
            return

        self._add_resource(accumulator.extensions, resolved, metadata, True)


    def _collect_package_resources(
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
            for file_path in _collect_resource_files(directory, resource_type):
                self._add_resource(self._get_target_map(accumulator, resource_type), file_path, metadata, True)
        return has_any_dir


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
            target = self._get_target_map(accumulator, resource_type)
            for path in paths:
                self._add_resource(target, path, metadata, self._is_enabled_by_overrides(path, overrides, base_dir))

        if project_trusted:
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
            prompts=materialize(accumulator.prompts),
            themes=materialize(accumulator.themes),
        )


__all__ = [
    "DefaultPackageManager",
    "PathMetadata",
    "ResolvedPaths",
    "ResolvedResource",
]
