"""Hierarchical settings management for coding-agent runtime behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from filelock import FileLock, Timeout

from misaka.ai.types import Model, ModelThinkingLevel, Transport
from misaka.ai.utils.retry import DEFAULT_MAX_AGENT_RETRY_DELAY_MS
from misaka.config import get_agent_dir, home
from misaka.core.http_dispatcher import (
    DEFAULT_HTTP_IDLE_TIMEOUT_MS,
    parseHttpIdleTimeoutMs,
)
from misaka.utils import atomic
from misaka.utils.paths import normalize_path, resolve_path

DEFAULT_COMPACTION_TOKEN_SETTINGS = {
    "reserveTokens": 16384,
    "keepRecentTokens": 20000,
}
# Cache-warming profile. "idle" also warms between agent runs.
CACHE_WARMING_MODES = ("off", "streaming", "idle")
_MAX_SAFE_INTEGER = 2**53 - 1


def _is_non_negative_safe_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_SAFE_INTEGER

type CompactionSettings = dict[str, Any]
type BranchSummarySettings = dict[str, Any]
type ProviderRetrySettings = dict[str, Any]
type RetrySettings = dict[str, Any]
type TerminalSettings = dict[str, Any]
type ImageSettings = dict[str, Any]
type ThinkingBudgetsSettings = dict[str, Any]
type MarkdownSettings = dict[str, Any]
type WarningSettings = dict[str, Any]
type Settings = dict[str, Any]
type SettingsScope = Literal["global", "role", "project"]
# The keys a role may hold a value of its own for. A role session that sets one of these writes
# its own scope; every other key is the user's, shared by every role, and stays global. This
# table is what keeps Last Order's and a Sister's configuration apart -- not per-key special cases.
ROLE_KEYS = frozenset({"defaultProvider", "defaultModel", "mcpServers", "web"})
type TransportSetting = Transport
type DefaultProjectTrust = Literal["ask", "always", "never"]


class SettingsManagerOptions(TypedDict, total=False):
    projectTrusted: bool


def deep_merge_settings(base: Settings, overrides: Settings) -> Settings:
    """Deep merge settings: overrides take precedence, nested objects merge recursively.
    (pi 97f0ccd #7572: previously everything below the first level was replaced wholesale,
    dropping sibling keys from base.)"""
    result = dict(base)
    for key, override_value in overrides.items():
        base_value = base.get(key)
        result[key] = (
            deep_merge_settings(base_value, override_value)
            if isinstance(base_value, dict) and isinstance(override_value, dict)
            else override_value
        )
    return result


@dataclass(slots=True)
class SettingsError:
    scope: SettingsScope
    error: Exception
    path: str | None = None


def _to_settings_error(
    scope: SettingsScope,
    error: Exception | BaseException,
    path: str | None = None,
) -> SettingsError:
    normalized = error if isinstance(error, Exception) else Exception(str(error))
    return SettingsError(scope=scope, error=normalized, path=path)


async def _settle(pending: asyncio.Future[None]) -> None:
    """Wait for a write-queue link without inheriting its outcome.

    pi ``settings-manager.ts:598-609`` hangs a single ``.catch`` on the whole
    promise chain, so a link that rejects both records its error and resets the
    queue to a resolved promise -- the next write still runs. Python's ``await``
    has no such reset: awaiting a failed or *cancelled* predecessor re-raises
    inside the successor, which used to poison every later write (silently
    dropped) plus ``flush()``/``reload()`` (permanent ``CancelledError``).

    ``asyncio.wait`` reports the predecessor's outcome instead of re-raising it,
    while still propagating cancellation of the *caller* -- so Ctrl-C during
    ``flush()`` keeps working. Each link records its own failure through
    ``_run_write_task``; there is nothing left for the successor to report.
    """
    if pending.done():
        return
    if pending.get_loop() is not asyncio.get_running_loop():
        # The predecessor belongs to a different event loop (one SettingsManager
        # reused across two asyncio.run() calls). Waiting on it here would block
        # forever, so give up the ordering guarantee rather than the write.
        return
    await asyncio.wait({pending})


class SettingsStorage:
    def withLock(self, scope: SettingsScope, fn: Any) -> None:  # pragma: no cover - protocol-like
        raise NotImplementedError


class FileSettingsStorage(SettingsStorage):
    def __init__(self, cwd: str, agent_dir: str):
        resolved_cwd = resolve_path(cwd)
        resolved_agent_dir = resolve_path(agent_dir)
        self.globalSettingsPath = str(Path(resolved_agent_dir) / "settings.json")
        # None when ``cwd`` has no project scope of its own: its config directory is the home.
        project_dir = home.project_dir(resolved_cwd)
        self.projectSettingsPath = str(project_dir / "settings.json") if project_dir is not None else None

    def path_for(self, scope: SettingsScope) -> str | None:
        return self.globalSettingsPath if scope == "global" else self.projectSettingsPath

    @staticmethod
    def _lock_path(path: str) -> str:
        return f"{path}.lock"

    def acquireLockSyncWithRetry(self, path: str) -> Any:
        max_attempts = 10
        delay_ms = 20
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            lock = FileLock(self._lock_path(path), timeout=0)
            try:
                lock.acquire()
                return lock.release
            except Timeout as error:
                if attempt == max_attempts:
                    raise
                last_error = error
                time.sleep(delay_ms / 1000)      # the sibling credential store already sleeps here

        raise last_error or Exception("Failed to acquire settings lock")

    def withLock(self, scope: SettingsScope, fn: Any) -> None:
        path = self.path_for(scope)
        if path is None:
            if fn(None) is not None:
                raise ValueError(f"This session has no {scope} settings to write.")
            return
        directory = os.path.dirname(path)
        release_lock: Any = None

        try:
            os.makedirs(directory, exist_ok=True)
            release_lock = self.acquireLockSyncWithRetry(path)   # before reading: a first write races too
            current: str | None = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    current = handle.read()

            next_value = fn(current)
            if next_value is None:
                return
            atomic.write_text(path, next_value)
        finally:
            if release_lock is not None:
                release_lock()


class InMemorySettingsStorage(SettingsStorage):
    def __init__(self) -> None:
        self.global_value: str | None = None
        self.project_value: str | None = None

    def withLock(self, scope: SettingsScope, fn: Any) -> None:
        current = self.global_value if scope == "global" else self.project_value
        next_value = fn(current)
        if next_value is None:
            return
        if scope == "global":
            self.global_value = next_value
        else:
            self.project_value = next_value


class SettingsManager:
    def __init__(
        self,
        storage: SettingsStorage,
        initialGlobal: Settings,
        initialProject: Settings,
        globalLoadError: Exception | None = None,
        projectLoadError: Exception | None = None,
        initialErrors: list[SettingsError] | None = None,
        projectTrusted: bool = True,
        settingsPaths: dict[SettingsScope, str] | None = None,
    ) -> None:
        self.storage = storage
        self.globalSettings = copy.deepcopy(initialGlobal)
        self.projectSettings = copy.deepcopy(initialProject) if projectTrusted else {}
        # The role's own values (``bindRole``); None while this manager serves no role.
        self.roleSettings: Settings | None = None
        self.roleSettingsLoadError: Exception | None = None
        self.projectTrusted = projectTrusted
        self.settings = self._merged()
        self.modifiedFields: set[str] = set()
        self.modifiedNestedFields: dict[str, set[str]] = {}
        self.modifiedProjectFields: set[str] = set()
        self.modifiedProjectNestedFields: dict[str, set[str]] = {}
        self.globalSettingsLoadError = globalLoadError
        self.projectSettingsLoadError = projectLoadError
        self.errors: list[SettingsError] = list(initialErrors or [])
        self.settingsPaths = dict(settingsPaths or {})
        self.writeQueue: asyncio.Future[None] | None = None
        # A role's own settings live in its directory (``ROLE_KEYS``); credentials and every
        # other setting keep their scopes. The role is bound explicitly, never read from env.
        self._modelProfileRegistry: Any = None
        self._modelDefaultsReadOnly = False

    def _merged(self) -> Settings:
        """global <- project <- role: the role is the most specific owner of what it holds."""
        merged = deep_merge_settings(self.globalSettings, self.projectSettings)
        return deep_merge_settings(merged, self.roleSettings) if self.roleSettings else merged

    @classmethod
    def create(
        cls,
        cwd: str,
        agentDir: str | None = None,
        options: SettingsManagerOptions | None = None,
    ) -> SettingsManager:
        storage = FileSettingsStorage(cwd, get_agent_dir() if agentDir is None else agentDir)
        return cls._fromStorageWithPaths(
            storage,
            options,
            {
                "global": storage.globalSettingsPath,
                "project": storage.projectSettingsPath,
            },
        )

    @classmethod
    def forRole(cls, roleDir: str | None, cwd: str | None = None) -> SettingsManager:
        """The settings a module without a session reads for a role (or for the home when
        ``roleDir`` is None): global plus the role's own, never a project's -- the sections
        such modules own (web, mcp, moa, skills, allies) have no project layer."""
        manager = cls.create(cwd or os.getcwd(), None, {"projectTrusted": False})
        manager.bindRole(roleDir)
        return manager

    @classmethod
    def fromStorage(
        cls,
        storage: SettingsStorage,
        options: SettingsManagerOptions | None = None,
    ) -> SettingsManager:
        return cls._fromStorageWithPaths(storage, options)

    @classmethod
    def _fromStorageWithPaths(
        cls,
        storage: SettingsStorage,
        options: SettingsManagerOptions | None = None,
        settingsPaths: dict[SettingsScope, str] | None = None,
    ) -> SettingsManager:
        project_trusted = (options or {}).get("projectTrusted", True)
        settings_paths = {scope: path for scope, path in (settingsPaths or {}).items() if path}
        global_load = cls.tryLoadFromStorage(storage, "global")
        project_load = cls.tryLoadFromStorage(storage, "project", project_trusted)
        initial_errors: list[SettingsError] = []
        if global_load["error"] is not None:
            initial_errors.append(
                _to_settings_error(
                    "global", global_load["error"], settings_paths.get("global")
                )
            )
        if project_load["error"] is not None:
            initial_errors.append(
                _to_settings_error(
                    "project", project_load["error"], settings_paths.get("project")
                )
            )
        return cls(
            storage,
            global_load["settings"],
            project_load["settings"],
            global_load["error"],
            project_load["error"],
            initial_errors,
            project_trusted,
            settings_paths,
        )

    @classmethod
    def inMemory(
        cls,
        settings: dict[str, Any] | None = None,
        options: SettingsManagerOptions | None = None,
    ) -> SettingsManager:
        storage = InMemorySettingsStorage()
        initial_settings = copy.deepcopy(settings or {})
        storage.withLock("global", lambda _current: json.dumps(initial_settings, indent=2, ensure_ascii=False))
        return cls.fromStorage(storage, options)

    @classmethod
    def loadFromStorage(
        cls,
        storage: SettingsStorage,
        scope: SettingsScope,
        projectTrusted: bool = True,
    ) -> Settings:
        if scope == "project" and not projectTrusted:
            return {}

        # Custom storage backends keep the existing withLock-only contract.
        if type(storage) is FileSettingsStorage:
            path = storage.path_for(scope)
            if path is None:
                return {}
            try:
                os.stat(path)
            except FileNotFoundError:
                return {}

        content: str | None = None

        def capture(current: str | None) -> None:
            nonlocal content
            content = current

        storage.withLock(scope, capture)
        if not content:
            return {}
        return json.loads(content.removeprefix("\ufeff"))

    @classmethod
    def tryLoadFromStorage(
        cls,
        storage: SettingsStorage,
        scope: SettingsScope,
        projectTrusted: bool = True,
    ) -> dict[str, Any]:
        try:
            return {"settings": cls.loadFromStorage(storage, scope, projectTrusted), "error": None}
        except Exception as error:  # noqa: BLE001
            return {"settings": {}, "error": error}

    def getGlobalSettings(self) -> Settings:
        return copy.deepcopy(self.globalSettings)

    def getProjectSettings(self) -> Settings:
        return copy.deepcopy(self.projectSettings)

    def isProjectTrusted(self) -> bool:
        return self.projectTrusted

    def setProjectTrusted(self, trusted: bool) -> None:
        if self.projectTrusted == trusted:
            return

        self.projectTrusted = trusted
        self.modifiedProjectFields.clear()
        self.modifiedProjectNestedFields.clear()
        if not trusted:
            self.projectSettings = {}
            self.projectSettingsLoadError = None
            self.settings = self._merged()
            return

        project_load = self.tryLoadFromStorage(self.storage, "project", trusted)
        self.projectSettings = project_load["settings"]
        self.projectSettingsLoadError = project_load["error"]
        if project_load["error"] is not None:
            self.recordError("project", project_load["error"])
        self.settings = self._merged()

    async def reload(self) -> None:
        await self.flush()
        global_load = self.tryLoadFromStorage(self.storage, "global")
        if global_load["error"] is None:
            self.globalSettings = global_load["settings"]
            self.globalSettingsLoadError = None
        else:
            self.globalSettingsLoadError = global_load["error"]
            self.recordError("global", global_load["error"])

        self.modifiedFields.clear()
        self.modifiedNestedFields.clear()
        self.modifiedProjectFields.clear()
        self.modifiedProjectNestedFields.clear()

        project_load = self.tryLoadFromStorage(self.storage, "project", self.projectTrusted)
        if project_load["error"] is None:
            self.projectSettings = project_load["settings"]
            self.projectSettingsLoadError = None
        else:
            self.projectSettingsLoadError = project_load["error"]
            self.recordError("project", project_load["error"])

        if self.roleSettings is not None:
            self._load_role()

        self.settings = self._merged()

    def applyOverrides(self, overrides: Settings) -> None:
        self.settings = deep_merge_settings(self.settings, overrides)

    def markModified(self, field: str, nestedKey: str | None = None) -> None:
        self.modifiedFields.add(field)
        if nestedKey is not None:
            self.modifiedNestedFields.setdefault(field, set()).add(nestedKey)

    def markProjectModified(self, field: str, nestedKey: str | None = None) -> None:
        self.modifiedProjectFields.add(field)
        if nestedKey is not None:
            self.modifiedProjectNestedFields.setdefault(field, set()).add(nestedKey)

    def recordError(self, scope: SettingsScope, error: Exception | BaseException) -> None:
        self.errors.append(
            _to_settings_error(scope, error, self.settingsPaths.get(scope))
        )

    def clearModifiedScope(self, scope: SettingsScope) -> None:
        if scope == "global":
            self.modifiedFields.clear()
            self.modifiedNestedFields.clear()
            return

        self.modifiedProjectFields.clear()
        self.modifiedProjectNestedFields.clear()

    @staticmethod
    def _clone_modified_nested_fields(source: dict[str, set[str]]) -> dict[str, set[str]]:
        return {key: set(value) for key, value in source.items()}

    def _persistScopedSettings(
        self,
        scope: SettingsScope,
        snapshotSettings: Settings,
        modifiedFields: set[str],
        modifiedNestedFields: dict[str, set[str]],
    ) -> None:
        def persist(current: str | None) -> str:
            current_file_settings = json.loads(current.removeprefix("\ufeff")) if current else {}
            merged_settings: Settings = copy.deepcopy(current_file_settings)
            for field in modifiedFields:
                value = snapshotSettings.get(field)
                if field in modifiedNestedFields and isinstance(value, dict):
                    nested_modified = modifiedNestedFields[field]
                    base_nested = copy.deepcopy(current_file_settings.get(field) or {})
                    for nested_key in nested_modified:
                        base_nested[nested_key] = value.get(nested_key)
                    merged_settings[field] = base_nested
                else:
                    if value is None:
                        merged_settings.pop(field, None)
                    else:
                        merged_settings[field] = copy.deepcopy(value)
            return json.dumps(merged_settings, indent=2, ensure_ascii=False)

        self.storage.withLock(scope, persist)

    def _run_write_task(self, scope: SettingsScope, task: Any) -> None:
        try:
            if scope == "project":
                self._assertProjectTrustedForWrite()
            task()
            self.clearModifiedScope(scope)
        except Exception as error:  # noqa: BLE001
            self.recordError(scope, error)

    def enqueueWrite(self, scope: SettingsScope, task: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._run_write_task(scope, task)
            return

        previous = self.writeQueue

        async def runner() -> None:
            if previous is not None:
                try:
                    await _settle(previous)
                except asyncio.CancelledError:
                    # This link is being cancelled, not the predecessor (``_settle``
                    # never re-raises the predecessor's outcome). The write is lost;
                    # say so instead of dropping it silently, then let cancellation
                    # continue -- successors chain through ``_settle`` and survive.
                    self.recordError(scope, asyncio.CancelledError("settings write cancelled"))
                    raise
            self._run_write_task(scope, task)

        self.writeQueue = loop.create_task(runner())

    def save(self) -> None:
        self.settings = self._merged()
        if self.globalSettingsLoadError is not None:
            return

        snapshot_global_settings = copy.deepcopy(self.globalSettings)
        modified_fields = set(self.modifiedFields)
        modified_nested_fields = self._clone_modified_nested_fields(self.modifiedNestedFields)
        self.enqueueWrite(
            "global",
            lambda: self._persistScopedSettings("global", snapshot_global_settings, modified_fields, modified_nested_fields),
        )

    def saveProjectSettings(self, settings: Settings) -> None:
        self._assertProjectTrustedForWrite()
        self.projectSettings = copy.deepcopy(settings)
        self.settings = self._merged()
        if self.projectSettingsLoadError is not None:
            return

        snapshot_project_settings = copy.deepcopy(self.projectSettings)
        modified_fields = set(self.modifiedProjectFields)
        modified_nested_fields = self._clone_modified_nested_fields(self.modifiedProjectNestedFields)
        self.enqueueWrite(
            "project",
            lambda: self._persistScopedSettings("project", snapshot_project_settings, modified_fields, modified_nested_fields),
        )

    # ── the role scope ──────────────────────────────────────────────────────────────────
    # A role's settings.json is the manager's own business, whatever storage serves the global
    # and project scopes: it is read once at bind, written synchronously under the file's lock,
    # and the in-memory copy changes only after the file did. A write that fails raises to the
    # caller with nothing changed -- the role's file, the global file, and this manager alike.

    def _role_dir(self) -> str | None:
        path = self.settingsPaths.get("role")
        return os.path.dirname(path) if path else None

    def _load_role(self) -> None:
        from misaka.config import profiles

        try:
            self.roleSettings = profiles.role_settings(self._role_dir(), strict=True)
            self.roleSettingsLoadError = None
        except (OSError, ValueError) as error:
            self.roleSettings = {}
            self.roleSettingsLoadError = error
            self.recordError("role", error)

    def _write_role(self, apply: Callable[[dict[str, Any]], None]) -> None:
        from filelock import FileLock

        from misaka.config import profiles
        from misaka.utils import atomic

        role_dir = self._role_dir()
        if role_dir is None:
            raise RuntimeError("This session serves no role; there are no role settings to write")
        path = profiles.settings_path(role_dir)
        os.makedirs(role_dir, exist_ok=True)
        with FileLock(path + ".lock"):
            data = profiles.role_settings(role_dir, strict=True)
            apply(data)
            atomic.write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
        self.roleSettings = copy.deepcopy(data)
        self.roleSettingsLoadError = None
        self.settings = self._merged()

    def _owner_scope(self, key: str) -> SettingsScope:
        """Where a write of ``key`` from this session lands: the role's own file for a key the
        role may hold, the user's global file for everything else."""
        return "role" if self.roleSettings is not None and key in ROLE_KEYS else "global"

    def getSection(self, key: str) -> dict[str, Any]:
        """One named mapping of the merged settings (``web``, ``mcpServers``, ``moa``, ...)."""
        return copy.deepcopy(self._settings_object(key))

    def getScopedSection(self, scope: SettingsScope, key: str) -> dict[str, Any]:
        """The section as one scope alone holds it, for what must know which file said it."""
        source = {"global": self.globalSettings, "role": self.roleSettings or {}, "project": self.projectSettings}[scope]
        value = source.get(key)
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def setValue(self, key: str, value: Any) -> None:
        """Set one top-level key in the scope that owns it (``None`` removes it)."""
        if self._owner_scope(key) == "role":
            def apply(data: dict[str, Any]) -> None:
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            self._write_role(apply)
        else:
            self._require_writable_global()
            if value is None:
                self.globalSettings.pop(key, None)
            else:
                self.globalSettings[key] = value
            self._write_global_now(key)

    def updateSection(self, key: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Edit the mapping under ``key`` in the scope that owns it and persist it.

        ``mutate`` receives that scope's own copy of the section (not the merged view), so a
        role's edit never copies the shared defaults into the role's file.
        """
        result: dict[str, Any] = {}

        def apply(data: dict[str, Any]) -> None:
            section = copy.deepcopy(data.get(key)) if isinstance(data.get(key), dict) else {}
            mutate(section)
            if section:
                data[key] = section
            else:
                data.pop(key, None)
            result.update(section)

        if self._owner_scope(key) == "role":
            self._write_role(apply)
        else:
            self._require_writable_global()
            self._write_global_section(key, apply)
        return copy.deepcopy(result)

    def _write_global_section(self, key: str, apply: Callable[[dict[str, Any]], None]) -> None:
        """Edit ``key`` in the global file as it is NOW, under its lock, the way ``_write_role``
        does: this manager's copy may be older than the file (another process wrote the section
        since it loaded), and writing the copy back would undo that write."""
        def persist(current: str | None) -> str:
            data = json.loads(current.removeprefix("\ufeff")) if current else {}
            apply(data)
            self.globalSettings.pop(key, None)
            if key in data:
                self.globalSettings[key] = copy.deepcopy(data[key])
            return json.dumps(data, indent=2, ensure_ascii=False)

        self.storage.withLock("global", persist)
        self.modifiedFields.discard(key)
        self.modifiedNestedFields.pop(key, None)
        self.settings = self._merged()

    def _write_global_now(self, key: str) -> None:
        """Persist one global key at once, under the file's lock, merging into what the file
        holds now. ``save`` queues on the event loop, which is right for a session's own
        settings and wrong for a module that writes a section and reads it straight back."""
        self.settings = self._merged()
        self._persistScopedSettings("global", copy.deepcopy(self.globalSettings), {key}, {})
        self.modifiedFields.discard(key)
        self.modifiedNestedFields.pop(key, None)

    def _require_writable_global(self) -> None:
        """A settings file that did not parse is the user's to fix: writing over it would
        silently destroy every setting in it, and ``save`` would otherwise drop the write."""
        if self.globalSettingsLoadError is not None:
            raise ValueError(f"Refusing to write {self.settingsPaths.get('global')}: {self.globalSettingsLoadError}")

    async def flush(self) -> None:
        queue = self.writeQueue
        if queue is not None:
            await _settle(queue)

    def drainErrors(self) -> list[SettingsError]:
        drained = list(self.errors)
        self.errors = []
        return drained

    def _set_global_value(self, key: str, value: Any) -> None:
        if value is None:
            self.globalSettings.pop(key, None)
        else:
            self.globalSettings[key] = value
        self.markModified(key)
        self.save()

    def _assertProjectTrustedForWrite(self) -> None:
        if not self.projectTrusted:
            raise RuntimeError("Project is not trusted; refusing to write project settings")

    def _ensure_global_nested(self, key: str) -> dict[str, Any]:
        value = self.globalSettings.get(key)
        if not isinstance(value, dict):
            value = {}
            self.globalSettings[key] = value
        return value

    def _settings_object(self, key: str) -> dict[str, Any]:
        value = self.settings.get(key)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _nullish(value: Any, default: Any) -> Any:
        return default if value is None else value


    def getSessionDir(self) -> str | None:
        session_dir = self.settings.get("sessionDir")
        return normalize_path(session_dir) if session_dir else session_dir

    def bindRole(self, profile_dir: str | None) -> None:
        """Serve ``profile_dir`` as this session's role: its settings.json is read now and is
        where the role's own keys (``ROLE_KEYS``) are written from here on."""
        if not profile_dir:
            return
        from misaka.config import profiles

        self.settingsPaths["role"] = profiles.settings_path(os.path.abspath(profile_dir))
        self._load_role()
        self.settings = self._merged()

    def bindModelProfile(self, profile_dir: str, model_registry: Any) -> None:
        self.bindRole(profile_dir)
        self._modelProfileRegistry = model_registry

    def getModelProfile(self) -> str | None:
        return self._role_dir()

    def restrictModelDefaults(self) -> None:
        """A headless child edits its definition through /agents, never its parent's defaults."""
        self._modelDefaultsReadOnly = True

    def _role_pin(self) -> tuple[str, str] | None:
        """The role's own provider/model pair, validated: half a pin or an unreadable file is
        an error to report, never a quiet slide onto the shared default."""
        if self.roleSettings is None:
            return None
        if self.roleSettingsLoadError is not None:
            raise ValueError(f"Role settings could not be read: {self.roleSettingsLoadError}")
        provider = self.roleSettings.get("defaultProvider")
        model = self.roleSettings.get("defaultModel")
        if not provider and not model:
            return None
        if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
            raise ValueError(f"{self.settingsPaths.get('role')}: defaultProvider and defaultModel go together")
        if self._modelProfileRegistry is not None:
            from misaka.config import profiles

            provider, model = profiles.resolve_model_reference(
                f"{provider}/{model}", self._modelProfileRegistry).split("/", 1)
        return provider, model

    def hasPinnedModelDefault(self) -> bool:
        return self._role_pin() is not None

    def getDefaultModelPair(self) -> tuple[str | None, str | None]:
        pin = self._role_pin()
        if pin is not None:
            return pin
        merged = deep_merge_settings(self.globalSettings, self.projectSettings)
        return merged.get("defaultProvider"), merged.get("defaultModel")

    def getDefaultProvider(self) -> str | None:
        return self.getDefaultModelPair()[0]

    def getDefaultModel(self) -> str | None:
        return self.getDefaultModelPair()[1]

    def setDefaultProvider(self, provider: str) -> None:
        self.setDefaultModelAndProvider(provider, self.getDefaultModel())

    def setDefaultModel(self, modelId: str) -> None:
        self.setDefaultModelAndProvider(self.getDefaultProvider(), modelId)

    def setDefaultModelAndProvider(self, provider: str, modelId: str) -> None:
        if self._modelDefaultsReadOnly:
            raise ValueError("Use /agents to save this child agent's definition; model switches here are session-only")
        if self.roleSettings is not None:
            if not provider or not modelId:
                raise ValueError("Both provider and model are required")
            if self._modelProfileRegistry is not None:
                from misaka.config import profiles

                provider, modelId = profiles.resolve_model_reference(
                    f"{provider}/{modelId}", self._modelProfileRegistry).split("/", 1)

            def apply(data: dict[str, Any]) -> None:
                data["defaultProvider"], data["defaultModel"] = provider, modelId

            self._write_role(apply)
            return
        self.globalSettings["defaultProvider"] = provider
        self.globalSettings["defaultModel"] = modelId
        self.markModified("defaultProvider")
        self.markModified("defaultModel")
        self.save()

    def getSteeringMode(self) -> str:
        return self.settings.get("steeringMode") or "one-at-a-time"

    def setSteeringMode(self, mode: str) -> None:
        self._set_global_value("steeringMode", mode)

    def getFollowUpMode(self) -> str:
        return self.settings.get("followUpMode") or "one-at-a-time"

    def setFollowUpMode(self, mode: str) -> None:
        self._set_global_value("followUpMode", mode)

    def getDefaultTools(self) -> list[str] | None:
        """Built-in startup selection; extension/custom tools stay enabled. None when unset."""
        tools = self.settings.get("defaultTools")
        return list(tools) if isinstance(tools, list) else None

    def getTheme(self) -> str | None:
        return self.settings.get("theme")

    def setTheme(self, theme: str) -> None:
        self._set_global_value("theme", theme)

    def getDefaultThinkingLevel(self) -> str | None:
        return self.settings.get("defaultThinkingLevel")

    def setDefaultThinkingLevel(self, level: str) -> None:
        self._set_global_value("defaultThinkingLevel", level)

    def getDefaultProjectTrust(self) -> DefaultProjectTrust:
        value = self.globalSettings.get("defaultProjectTrust")
        return value if value in {"ask", "always", "never"} else "ask"

    def setDefaultProjectTrust(self, defaultProjectTrust: DefaultProjectTrust) -> None:
        self._set_global_value("defaultProjectTrust", defaultProjectTrust)

    def getModelThinkingLevel(self, provider: str, modelId: str) -> ModelThinkingLevel | None:
        levels = self.settings.get("modelThinkingLevels")
        return levels.get(f"{provider}/{modelId}") if isinstance(levels, dict) else None

    def getAllModelThinkingLevels(self) -> dict[str, ModelThinkingLevel]:
        levels = self.settings.get("modelThinkingLevels")
        return dict(levels) if isinstance(levels, dict) else {}

    def setModelThinkingLevel(self, provider: str, modelId: str, level: ModelThinkingLevel) -> None:
        levels = self.globalSettings.get("modelThinkingLevels")
        if not isinstance(levels, dict):
            levels = {}
            self.globalSettings["modelThinkingLevels"] = levels
        levels[f"{provider}/{modelId}"] = level
        self.markModified("modelThinkingLevels")
        self.save()

    def removeModelThinkingLevel(self, provider: str, modelId: str) -> None:
        levels = self.globalSettings.get("modelThinkingLevels")
        if not isinstance(levels, dict):
            return
        levels.pop(f"{provider}/{modelId}", None)
        if not levels:
            self.globalSettings.pop("modelThinkingLevels", None)
        self.markModified("modelThinkingLevels")
        self.save()

    def getTransport(self) -> TransportSetting:
        return self._nullish(self.settings.get("transport"), "auto")

    def setTransport(self, transport: TransportSetting) -> None:
        self._set_global_value("transport", transport)

    def getCompactionEnabled(self) -> bool:
        return self._nullish(self._settings_object("compaction").get("enabled"), True)

    def setCompactionEnabled(self, enabled: bool) -> None:
        compaction = self._ensure_global_nested("compaction")
        compaction["enabled"] = enabled
        self.markModified("compaction", "enabled")
        self.save()

    def getCompactionTokenSetting(self, field: str, model: Model | None = None) -> int:
        compaction = self._settings_object("compaction")
        ordinary = compaction.get(field)
        if ordinary is not None and not _is_non_negative_safe_integer(ordinary):
            raise ValueError(
                f"Invalid compaction.{field} setting: {ordinary}. Expected a non-negative safe integer."
            )
        model_key = f"{model.provider}/{model.id}" if model is not None else None
        model_overrides = compaction.get("modelOverrides")
        entry = (
            model_overrides.get(model_key)
            if model_key is not None and isinstance(model_overrides, dict)
            else None
        )
        if entry is not None and not isinstance(entry, dict):
            raise ValueError(
                f'Invalid compaction.modelOverrides["{model_key}"] setting: {entry}. Expected an object.'
            )
        override = entry.get(field) if entry is not None else None
        if override is not None and not _is_non_negative_safe_integer(override):
            raise ValueError(
                f'Invalid compaction.modelOverrides["{model_key}"].{field} setting: {override}. '
                "Expected a non-negative safe integer."
            )
        if override is not None:
            return override
        if ordinary is not None:
            return ordinary
        return DEFAULT_COMPACTION_TOKEN_SETTINGS[field]

    def getCompactionReserveTokens(self, model: Model | None = None) -> int:
        return self.getCompactionTokenSetting("reserveTokens", model)

    def getCompactionKeepRecentTokens(self, model: Model | None = None) -> int:
        return self.getCompactionTokenSetting("keepRecentTokens", model)

    def getCompactionSettings(self, model: Model | None = None) -> dict[str, Any]:
        """Resolve each token setting through model override, ordinary setting, then built-in default."""
        return {
            "enabled": self.getCompactionEnabled(),
            "reserveTokens": self.getCompactionReserveTokens(model),
            "keepRecentTokens": self.getCompactionKeepRecentTokens(model),
        }

    def getBranchSummarySettings(self) -> dict[str, Any]:
        branch_summary = self._settings_object("branchSummary")
        return {
            "reserveTokens": self._nullish(branch_summary.get("reserveTokens"), 16384),
            "skipPrompt": self._nullish(branch_summary.get("skipPrompt"), False),
        }

    def getBranchSummarySkipPrompt(self) -> bool:
        return self._nullish(self._settings_object("branchSummary").get("skipPrompt"), False)

    def getRetryEnabled(self) -> bool:
        return self._nullish(self._settings_object("retry").get("enabled"), True)

    def setRetryEnabled(self, enabled: bool) -> None:
        retry_settings = self._ensure_global_nested("retry")
        retry_settings["enabled"] = enabled
        self.markModified("retry", "enabled")
        self.save()

    def getRetrySettings(self) -> dict[str, Any]:
        retry_settings = self._settings_object("retry")
        return {
            "enabled": self.getRetryEnabled(),
            "maxRetries": self._nullish(retry_settings.get("maxRetries"), 3),
            "baseDelayMs": self._nullish(retry_settings.get("baseDelayMs"), 2000),
            "maxAgentDelayMs": self._nullish(retry_settings.get("maxAgentDelayMs"), DEFAULT_MAX_AGENT_RETRY_DELAY_MS),
        }

    def getHttpIdleTimeoutMs(self) -> int:
        if "httpIdleTimeoutMs" not in self.settings:
            return DEFAULT_HTTP_IDLE_TIMEOUT_MS
        value = self.settings["httpIdleTimeoutMs"]
        timeout_ms = parseHttpIdleTimeoutMs(value)
        if timeout_ms is None:
            raise ValueError(f"Invalid httpIdleTimeoutMs setting: {value}")
        return timeout_ms

    def setHttpIdleTimeoutMs(self, timeoutMs: float) -> None:
        if isinstance(timeoutMs, bool) or not isinstance(timeoutMs, int | float):
            raise TypeError(f"Invalid httpIdleTimeoutMs setting: {timeoutMs}")
        timeout_ms = parseHttpIdleTimeoutMs(timeoutMs)
        if timeout_ms is None:
            raise ValueError(f"Invalid httpIdleTimeoutMs setting: {timeoutMs}")
        self._set_global_value("httpIdleTimeoutMs", timeout_ms)

    def getCacheWarmingMode(self) -> str:
        """Read from global settings only because warming costs money."""
        mode = self.globalSettings.get("cacheWarming")
        return mode if mode is not None and mode in CACHE_WARMING_MODES else "streaming"

    def setCacheWarmingMode(self, mode: str) -> None:
        self._set_global_value("cacheWarming", mode)

    def getProviderRetrySettings(self) -> dict[str, Any]:
        provider = self._settings_object("retry").get("provider")
        provider_settings = provider if isinstance(provider, dict) else {}
        return {
            "timeoutMs": provider_settings.get("timeoutMs"),
            "maxRetries": provider_settings.get("maxRetries"),
            "maxRetryDelayMs": self._nullish(provider_settings.get("maxRetryDelayMs"), 60000),
        }

    def getHideThinkingBlock(self) -> bool:
        return self._nullish(self.settings.get("hideThinkingBlock"), False)

    def setHideThinkingBlock(self, hide: bool) -> None:
        self._set_global_value("hideThinkingBlock", hide)

    def getShellPath(self) -> str | None:
        return self.settings.get("shellPath")

    def setShellPath(self, path: str | None) -> None:
        self._set_global_value("shellPath", path)

    def getQuietStartup(self) -> bool:
        return self._nullish(self.settings.get("quietStartup"), False)

    def setQuietStartup(self, quiet: bool) -> None:
        self._set_global_value("quietStartup", quiet)

    def getEnableInstallTelemetry(self) -> bool:
        # The setting answers "may this install be identified to a service". All that rides
        # on it is `core/provider_attribution.py` naming the client "misaka" to aggregator
        # gateways; nothing else is sent. Upstream defaults it on; MISAKA defaults it off,
        # so being named is something the user turns on rather than something they find.
        return self._nullish(self.settings.get("enableInstallTelemetry"), False)

    def setEnableInstallTelemetry(self, enabled: bool) -> None:
        self._set_global_value("enableInstallTelemetry", enabled)

    def getShellCommandPrefix(self) -> str | None:
        return self.settings.get("shellCommandPrefix")

    def setShellCommandPrefix(self, prefix: str | None) -> None:
        self._set_global_value("shellCommandPrefix", prefix)


    def getCollapseChangelog(self) -> bool:
        return self._nullish(self.settings.get("collapseChangelog"), False)

    def setCollapseChangelog(self, collapse: bool) -> None:
        self._set_global_value("collapseChangelog", collapse)


    def getExtensionPaths(self) -> list[str]:
        return list(self.settings.get("extensions") or [])

    def setExtensionPaths(self, paths: list[str]) -> None:
        self._set_global_value("extensions", paths)

    def getPromptTemplatePaths(self) -> list[str]:
        return list(self.settings.get("prompts") or [])

    def setPromptTemplatePaths(self, paths: list[str]) -> None:
        self._set_global_value("prompts", paths)

    def setProjectPromptTemplatePaths(self, paths: list[str]) -> None:
        self._assertProjectTrustedForWrite()
        project_settings = copy.deepcopy(self.projectSettings)
        project_settings["prompts"] = paths
        self.markProjectModified("prompts")
        self.saveProjectSettings(project_settings)

    def getThemePaths(self) -> list[str]:
        return list(self.settings.get("themes") or [])

    def setThemePaths(self, paths: list[str]) -> None:
        self._set_global_value("themes", paths)

    def setProjectThemePaths(self, paths: list[str]) -> None:
        self._assertProjectTrustedForWrite()
        project_settings = copy.deepcopy(self.projectSettings)
        project_settings["themes"] = paths
        self.markProjectModified("themes")
        self.saveProjectSettings(project_settings)

    def getEnableSkillCommands(self) -> bool:
        return self._nullish(self.settings.get("enableSkillCommands"), True)

    def setEnableSkillCommands(self, enabled: bool) -> None:
        self._set_global_value("enableSkillCommands", enabled)

    def getThinkingBudgets(self) -> dict[str, Any] | None:
        budgets = self.settings.get("thinkingBudgets")
        return budgets if isinstance(budgets, dict) else None

    def getTerminalCapabilityOverrides(self) -> dict[str, Any]:
        """Explicit `terminal.images`/`trueColor`/`hyperlinks` settings; `"auto"`/absent leave detection alone."""
        terminal = self._settings_object("terminal")
        images = terminal.get("images")
        overrides: dict[str, Any] = {}
        if images in ("kitty", "iterm2"):
            overrides["images"] = images
        elif images is False:
            overrides["images"] = None
        for key in ("trueColor", "hyperlinks"):
            if isinstance(terminal.get(key), bool):
                overrides[key] = terminal[key]
        return overrides

    def getShowImages(self) -> bool:
        return self._nullish(self._settings_object("terminal").get("showImages"), True)

    def setShowImages(self, show: bool) -> None:
        terminal = self._ensure_global_nested("terminal")
        terminal["showImages"] = show
        self.markModified("terminal", "showImages")
        self.save()

    def getImageWidthCells(self) -> int:
        width = self._settings_object("terminal").get("imageWidthCells")
        if not isinstance(width, (int, float)) or isinstance(width, bool) or not float("-inf") < float(width) < float("inf"):
            return 60
        return max(1, int(width // 1))

    def setImageWidthCells(self, width: int) -> None:
        terminal = self._ensure_global_nested("terminal")
        terminal["imageWidthCells"] = max(1, int(width))
        self.markModified("terminal", "imageWidthCells")
        self.save()

    def getClearOnShrink(self) -> bool:
        terminal = self._settings_object("terminal")
        if terminal.get("clearOnShrink") is not None:
            return terminal["clearOnShrink"]
        return os.environ.get("MISAKA_CLEAR_ON_SHRINK") == "1"

    def setClearOnShrink(self, enabled: bool) -> None:
        terminal = self._ensure_global_nested("terminal")
        terminal["clearOnShrink"] = enabled
        self.markModified("terminal", "clearOnShrink")
        self.save()

    def getShowTerminalProgress(self) -> bool:
        return self._nullish(self._settings_object("terminal").get("showTerminalProgress"), False)

    def setShowTerminalProgress(self, enabled: bool) -> None:
        terminal = self._ensure_global_nested("terminal")
        terminal["showTerminalProgress"] = enabled
        self.markModified("terminal", "showTerminalProgress")
        self.save()

    def getImageAutoResize(self) -> bool:
        return self._nullish(self._settings_object("images").get("autoResize"), True)

    def setImageAutoResize(self, enabled: bool) -> None:
        images = self._ensure_global_nested("images")
        images["autoResize"] = enabled
        self.markModified("images", "autoResize")
        self.save()

    def getBlockImages(self) -> bool:
        return self._nullish(self._settings_object("images").get("blockImages"), False)

    def setBlockImages(self, blocked: bool) -> None:
        images = self._ensure_global_nested("images")
        images["blockImages"] = blocked
        self.markModified("images", "blockImages")
        self.save()

    def getEnabledModels(self) -> list[str] | None:
        return self.settings.get("enabledModels")

    def setEnabledModels(self, patterns: list[str] | None) -> None:
        self._set_global_value("enabledModels", patterns)

    def getDoubleEscapeAction(self) -> str:
        return self._nullish(self.settings.get("doubleEscapeAction"), "tree")

    def setDoubleEscapeAction(self, action: str) -> None:
        self._set_global_value("doubleEscapeAction", action)

    def getTreeFilterMode(self) -> str:
        mode = self.settings.get("treeFilterMode")
        valid = {"default", "no-tools", "user-only", "labeled-only", "all"}
        return mode if mode in valid else "default"

    def setTreeFilterMode(self, mode: str) -> None:
        self._set_global_value("treeFilterMode", mode)

    def getShowHardwareCursor(self) -> bool:
        if self.settings.get("showHardwareCursor") is not None:
            return self.settings["showHardwareCursor"]
        return os.environ.get("MISAKA_HARDWARE_CURSOR") == "1"

    def setShowHardwareCursor(self, enabled: bool) -> None:
        self._set_global_value("showHardwareCursor", enabled)

    def getEditorPaddingX(self) -> int:
        return self._nullish(self.settings.get("editorPaddingX"), 0)

    def setEditorPaddingX(self, padding: int) -> None:
        self._set_global_value("editorPaddingX", max(0, min(3, int(padding))))

    def getOutputPad(self) -> Literal[0, 1]:
        value = self.settings.get("outputPad")
        return 0 if type(value) in (int, float) and value == 0 else 1

    def setOutputPad(self, padding: Literal[0, 1]) -> None:
        self._set_global_value("outputPad", padding)

    def getAutocompleteMaxVisible(self) -> int:
        return self._nullish(self.settings.get("autocompleteMaxVisible"), 5)

    def setAutocompleteMaxVisible(self, maxVisible: int) -> None:
        self._set_global_value("autocompleteMaxVisible", max(3, min(20, int(maxVisible))))

    def getCodeBlockIndent(self) -> str:
        return self._nullish(self._settings_object("markdown").get("codeBlockIndent"), "  ")

    def getWarnings(self) -> dict[str, Any]:
        return dict(self._settings_object("warnings"))

    def setWarnings(self, warnings: dict[str, Any]) -> None:
        self._set_global_value("warnings", {**warnings})

__all__ = [
    "BranchSummarySettings",
    "CompactionSettings",
    "DefaultProjectTrust",
    "FileSettingsStorage",
    "ImageSettings",
    "InMemorySettingsStorage",
    "MarkdownSettings",
    "ProviderRetrySettings",
    "RetrySettings",
    "Settings",
    "SettingsError",
    "SettingsManager",
    "SettingsManagerOptions",
    "SettingsScope",
    "SettingsStorage",
    "TerminalSettings",
    "ThinkingBudgetsSettings",
    "TransportSetting",
    "WarningSettings",
]
