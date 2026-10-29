"""Translated from pi's ``packages/coding-agent/src/core/trust-manager.ts`` (v1.1.0).

MISAKA forks:
- What needs trust is what MISAKA loads from a project: its MISAKA home's settings, MCP servers, prompts and themes,
  and Sisters agent definitions in that home or an ancestor's (``hasTrustRequiringProjectResources``). Project
  extensions, skills and system prompts are never loaded, so they need none.
- The store lives in the home's state (``home.path("trust")``) when the agent directory is the home; an embedder's
  own directory keeps ``trust.json`` beside its settings. It is written atomically.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from filelock import FileLock, Timeout

from misaka.config import home
from misaka.utils.paths import canonicalizePath, homedir, resolvePath
from misaka.utils.text import stripBom

type ProjectTrustDecision = bool | None


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


type TrustFile = dict[str, bool | None]

# MISAKA fork: the entries of a project's MISAKA home that MISAKA loads.
TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES = ("settings.json", "mcp.json", "prompts", "themes")


def normalizeCwd(cwd: str) -> str:
    return canonicalizePath(resolvePath(cwd))


def findNearestTrustEntry(data: TrustFile, cwd: str) -> ProjectTrustStoreEntry | None:
    currentDir = normalizeCwd(cwd)
    while True:
        value = data.get(currentDir)
        if value is True or value is False:
            return ProjectTrustStoreEntry(path=currentDir, decision=value)

        parentDir = os.path.dirname(currentDir)
        if parentDir == currentDir:
            return None
        currentDir = parentDir


def getProjectTrustParentPath(cwd: str) -> str | None:
    trustPath = normalizeCwd(cwd)
    parentDir = os.path.dirname(trustPath)
    return None if parentDir == trustPath else parentDir


class ProjectTrustOptions(TypedDict, total=False):
    includeSessionOnly: bool


def getProjectTrustOptions(cwd: str, options: ProjectTrustOptions | None = None) -> list[ProjectTrustOption]:
    trustPath = normalizeCwd(cwd)
    trustOptions = [
        ProjectTrustOption(
            label="Trust", trusted=True, updates=[ProjectTrustUpdate(path=trustPath, decision=True)], savedPath=trustPath
        ),
    ]
    parentPath = getProjectTrustParentPath(cwd)
    if parentPath is not None:
        trustOptions.append(
            ProjectTrustOption(
                label=f"Trust parent folder ({parentPath})",
                trusted=True,
                updates=[
                    ProjectTrustUpdate(path=parentPath, decision=True),
                    ProjectTrustUpdate(path=trustPath, decision=None),
                ],
                savedPath=parentPath,
            )
        )
    if options and options.get("includeSessionOnly"):
        trustOptions.append(ProjectTrustOption(label="Trust (this session only)", trusted=True, updates=[]))
    trustOptions.append(
        ProjectTrustOption(
            label="Do not trust",
            trusted=False,
            updates=[ProjectTrustUpdate(path=trustPath, decision=False)],
            savedPath=trustPath,
        )
    )
    if options and options.get("includeSessionOnly"):
        trustOptions.append(ProjectTrustOption(label="Do not trust (this session only)", trusted=False, updates=[]))
    return trustOptions


def readTrustFile(path: str) -> TrustFile:
    if not os.path.exists(path):
        return {}

    try:
        parsed = json.loads(stripBom(Path(path).read_bytes().decode("utf-8", errors="replace")))
    except Exception as error:
        raise RuntimeError(f"Failed to read trust store {path}: {error}") from error

    if not isinstance(parsed, dict):
        raise TypeError(f"Invalid trust store {path}: expected an object")

    data: TrustFile = {}
    for key, value in parsed.items():
        if value is not True and value is not False and value is not None:
            raise TypeError(f"Invalid trust store {path}: value for {json.dumps(key)} must be true, false, or null")
        data[key] = value
    return data


def writeTrustFile(path: str, data: TrustFile) -> None:
    sortedData: TrustFile = {}
    for key in sorted(data):
        value = data[key]
        if value is True or value is False or value is None:
            sortedData[key] = value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(f"{json.dumps(sortedData, indent=2, ensure_ascii=False)}\n", encoding="utf-8", newline="")


def acquireTrustLockSync(path: str) -> Callable[[], None]:
    trustDir = os.path.dirname(path)
    os.makedirs(trustDir, exist_ok=True)
    maxAttempts = 10
    delayMs = 20
    lastError: BaseException | None = None

    for attempt in range(1, maxAttempts + 1):
        lock = FileLock(f"{path}.lock", timeout=0)
        try:
            lock.acquire()
            return lock.release
        except Timeout as error:
            if attempt == maxAttempts:
                raise
            lastError = error
            # Sleep synchronously to avoid changing trust store callers to async.
            time.sleep(delayMs / 1000)

    if lastError is not None:
        raise lastError
    raise RuntimeError("Failed to acquire trust store lock")


def withTrustFileLock[T](path: str, fn: Callable[[], T]) -> T:
    release = acquireTrustLockSync(path)
    try:
        return fn()
    finally:
        release()


def hasTrustRequiringProjectResources(cwd: str) -> bool:
    """Returns true when cwd has project-local resources that must be gated by
    project trust: trust-requiring entries in the project's MISAKA home, or Sisters agent definitions in that home or
    one of its ancestors'. Returns false when no such project resources exist.

    MISAKA fork: pi walks every ancestor for ``.agents/skills`` and skips the user's own. Here the walk looks for
    ``<home>/subagents``, and stops at a repository root or $HOME: without that it reaches $HOME, finds the user's own
    definitions, and asks "Trust project folder?" in every non-git directory under $HOME -- training users to click
    Trust reflexively. ``home.project_dir`` draws that line too: the home is never a project.
    """
    homeDir = Path(normalizeCwd(homedir()))
    currentDir = Path(normalizeCwd(cwd))

    configDir = home.project_dir(currentDir)
    if configDir is not None and any((configDir / entry).exists() for entry in TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES):
        return True

    while True:
        configDir = home.project_dir(currentDir)
        if configDir is not None and (configDir / home.SUBAGENTS_DIR).is_dir():
            return True

        if (currentDir / ".git").exists() or currentDir == homeDir or currentDir.parent == currentDir:
            return False
        currentDir = currentDir.parent


class ProjectTrustStore:
    def __init__(self, agentDir: str) -> None:
        # MISAKA fork: the home keeps trust.json with its state; an embedder's own directory keeps it beside its
        # settings.
        directory = Path(os.path.realpath(resolvePath(agentDir)))
        self.trustPath = str(home.path("trust") if directory == home.home() else directory / "trust.json")

    def get(self, cwd: str) -> ProjectTrustDecision:
        entry = self.getEntry(cwd)
        return entry.decision if entry is not None else None

    def getEntry(self, cwd: str) -> ProjectTrustStoreEntry | None:
        def read() -> ProjectTrustStoreEntry | None:
            data = readTrustFile(self.trustPath)
            return findNearestTrustEntry(data, cwd)

        return withTrustFileLock(self.trustPath, read)

    def set(self, cwd: str, decision: ProjectTrustDecision) -> None:
        self.setMany([ProjectTrustUpdate(path=cwd, decision=decision)])

    def setMany(self, decisions: list[ProjectTrustUpdate]) -> None:
        def write() -> None:
            data = readTrustFile(self.trustPath)
            for update in decisions:
                key = normalizeCwd(update.path)
                if update.decision is None:
                    data.pop(key, None)
                else:
                    data[key] = update.decision
            writeTrustFile(self.trustPath, data)

        withTrustFileLock(self.trustPath, write)


__all__ = [
    "ProjectTrustDecision",
    "ProjectTrustOption",
    "ProjectTrustStore",
    "ProjectTrustStoreEntry",
    "ProjectTrustUpdate",
    "getProjectTrustOptions",
    "getProjectTrustParentPath",
    "hasTrustRequiringProjectResources",
]
