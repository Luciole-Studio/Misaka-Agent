"""Persistent project-trust decisions with nearest-parent inheritance."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, TypeVar

from filelock import FileLock, Timeout

from misaka.config import APP_NAME, CONFIG_DIR_NAME
from misaka.utils import atomic
from misaka.utils.paths import canonicalize_path, resolve_path

type ProjectTrustDecision = bool | None

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ProjectTrustStoreEntry:
    path: str
    decision: bool


@dataclass(frozen=True, slots=True)
class ProjectTrustUpdate:
    path: str
    decision: ProjectTrustDecision


@dataclass(frozen=True, slots=True)
class ProjectTrustOption:
    label: str
    trusted: bool
    updates: list[ProjectTrustUpdate]
    savedPath: str | None = None


class ProjectTrustOptions(TypedDict, total=False):
    includeSessionOnly: bool


def _normalize_cwd(cwd: str) -> str:
    return canonicalize_path(resolve_path(cwd))


def get_project_trust_parent_path(cwd: str) -> str | None:
    trust_path = _normalize_cwd(cwd)
    parent = os.path.dirname(trust_path)
    return None if parent == trust_path else parent


def get_project_trust_options(
    cwd: str,
    options: ProjectTrustOptions | None = None,
) -> list[ProjectTrustOption]:
    trust_path = _normalize_cwd(cwd)
    trust_options = [
        ProjectTrustOption(
            label="Trust",
            trusted=True,
            updates=[ProjectTrustUpdate(path=trust_path, decision=True)],
            savedPath=trust_path,
        )
    ]
    parent_path = get_project_trust_parent_path(cwd)
    if parent_path is not None:
        trust_options.append(
            ProjectTrustOption(
                label=f"Trust parent folder ({parent_path})",
                trusted=True,
                updates=[
                    ProjectTrustUpdate(path=parent_path, decision=True),
                    ProjectTrustUpdate(path=trust_path, decision=None),
                ],
                savedPath=parent_path,
            )
        )
    if options and options.get("includeSessionOnly"):
        trust_options.append(
            ProjectTrustOption(
                label="Trust (this session only)",
                trusted=True,
                updates=[],
            )
        )
    trust_options.append(
        ProjectTrustOption(
            label="Do not trust",
            trusted=False,
            updates=[ProjectTrustUpdate(path=trust_path, decision=False)],
            savedPath=trust_path,
        )
    )
    if options and options.get("includeSessionOnly"):
        trust_options.append(
            ProjectTrustOption(
                label="Do not trust (this session only)",
                trusted=False,
                updates=[],
            )
        )
    return trust_options


def has_trust_requiring_project_resources(cwd: str) -> bool:
    current = Path(_normalize_cwd(cwd))
    config_dir = current / CONFIG_DIR_NAME
    if any(
        (config_dir / entry).exists()
        for entry in ("settings.json", "prompts", "themes")
    ):
        return True

    home = Path(_normalize_cwd(str(Path.home())))
    while True:
        if (current / CONFIG_DIR_NAME / "agents").is_dir():
            return True
        if (current / ".git").exists() or current == home or current.parent == current:
            return False
        current = current.parent


def _find_nearest_trust_entry(
    data: dict[str, ProjectTrustDecision], cwd: str
) -> ProjectTrustStoreEntry | None:
    current = _normalize_cwd(cwd)
    while True:
        value = data.get(current)
        if isinstance(value, bool):
            return ProjectTrustStoreEntry(path=current, decision=value)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _read_trust_file(path: str) -> dict[str, ProjectTrustDecision]:
    if not os.path.exists(path):
        return {}
    try:
        parsed = json.loads(
            Path(path).read_text(encoding="utf-8").removeprefix("\ufeff")
        )
    except Exception as error:
        raise RuntimeError(f"Failed to read trust store {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise TypeError(f"Invalid trust store {path}: expected an object")
    for key, value in parsed.items():
        if not isinstance(key, str) or not (isinstance(value, bool) or value is None):
            raise TypeError(
                f"Invalid trust store {path}: value for {json.dumps(key)} must be true, false, or null"
            )
    return parsed


def _write_trust_file(path: str, data: dict[str, ProjectTrustDecision]) -> None:
    sorted_data = {key: data[key] for key in sorted(data)}
    atomic.write_text(
        path, json.dumps(sorted_data, indent=2, ensure_ascii=False) + "\n"
    )


class ProjectTrustStore:
    def __init__(self, agent_dir: str) -> None:
        self.trust_path = str(Path(resolve_path(agent_dir)) / "trust.json")
        self.trustPath = self.trust_path

    def _acquire_lock(self) -> FileLock:
        os.makedirs(os.path.dirname(self.trust_path), exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(1, 11):
            lock = FileLock(f"{self.trust_path}.lock", timeout=0)
            try:
                lock.acquire()
                return lock
            except Timeout as error:
                if attempt == 10:
                    raise
                last_error = error
                time.sleep(0.02)
        raise last_error or RuntimeError("Failed to acquire trust store lock")

    def _with_lock(self, fn: Callable[[], _T]) -> _T:
        lock = self._acquire_lock()
        try:
            return fn()
        finally:
            lock.release()

    def get(self, cwd: str) -> ProjectTrustDecision:
        entry = self.get_entry(cwd)
        return entry.decision if entry is not None else None

    def get_entry(self, cwd: str) -> ProjectTrustStoreEntry | None:
        return self._with_lock(
            lambda: _find_nearest_trust_entry(_read_trust_file(self.trust_path), cwd)
        )

    def set(self, cwd: str, decision: ProjectTrustDecision) -> None:
        self.set_many([ProjectTrustUpdate(path=cwd, decision=decision)])

    def set_many(self, decisions: Iterable[ProjectTrustUpdate]) -> None:
        updates = list(decisions)
        for update in updates:
            if not (isinstance(update.decision, bool) or update.decision is None):
                raise TypeError("Project trust decision must be true, false, or null")

        def write() -> None:
            data = _read_trust_file(self.trust_path)
            for update in updates:
                key = _normalize_cwd(update.path)
                if update.decision is None:
                    data.pop(key, None)
                else:
                    data[key] = update.decision
            _write_trust_file(self.trust_path, data)

        self._with_lock(write)

    getEntry = get_entry
    setMany = set_many


def _format_project_trust_prompt(cwd: str) -> str:
    return (
        f"Trust project folder?\n{cwd}\n\n"
        f"This allows {APP_NAME} to load {CONFIG_DIR_NAME}/settings.json, "
        f"{CONFIG_DIR_NAME}/prompts, {CONFIG_DIR_NAME}/themes, and Sisters project "
        f"agent definitions from {CONFIG_DIR_NAME}/agents."
    )


def _member(source: Any, name: str) -> Any:
    return source[name] if isinstance(source, Mapping) else getattr(source, name)


async def resolve_project_trusted(options: dict[str, Any]) -> bool:
    trust_override = options.get("trustOverride")
    if trust_override is not None:
        return trust_override

    cwd = options["cwd"]
    if not has_trust_requiring_project_resources(cwd):
        return True

    trust_store: ProjectTrustStore = options["trustStore"]
    extensions_result = options.get("extensionsResult")
    if extensions_result is not None:
        from misaka.core.extensions.runner import emit_project_trust_event

        emitted = await emit_project_trust_event(
            extensions_result,
            {"type": "project_trust", "cwd": cwd},
            options["projectTrustContext"],
        )
        on_extension_error = options.get("onExtensionError")
        if on_extension_error is not None:
            for error in emitted["errors"]:
                on_extension_error(
                    f'Extension "{error.extensionPath}" project_trust error: {error.error}'
                )
        result = emitted.get("result")
        if result is not None:
            trusted = result["trusted"] == "yes"
            if result.get("remember") is True:
                trust_store.set(cwd, trusted)
            return trusted

    decision = trust_store.get(cwd)
    if decision is not None:
        return decision

    default_project_trust = options.get("defaultProjectTrust", "ask")
    if default_project_trust == "always":
        return True
    if default_project_trust == "never":
        return False

    context = options["projectTrustContext"]
    if not _member(context, "hasUI"):
        return False

    trust_options = get_project_trust_options(cwd, {"includeSessionOnly": True})
    select = _member(_member(context, "ui"), "select")
    selected_label = await select(
        _format_project_trust_prompt(cwd),
        [option.label for option in trust_options],
    )
    selected = next(
        (option for option in trust_options if option.label == selected_label),
        None,
    )
    if selected is None:
        return False
    if selected.updates:
        trust_store.set_many(selected.updates)
    return selected.trusted


hasTrustRequiringProjectResources = has_trust_requiring_project_resources

__all__ = [
    "ProjectTrustDecision",
    "ProjectTrustOption",
    "ProjectTrustOptions",
    "ProjectTrustStore",
    "ProjectTrustStoreEntry",
    "ProjectTrustUpdate",
    "get_project_trust_options",
    "get_project_trust_parent_path",
    "hasTrustRequiringProjectResources",
    "has_trust_requiring_project_resources",
    "resolve_project_trusted",
]
