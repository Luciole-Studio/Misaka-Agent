"""Translated from pi coding-agent's ``src/core/footer-data-provider.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from misaka.utils.fs_watch import (
    FS_WATCH_RETRY_DELAY_MS,
    FSWatcher,
    Stats,
    closeWatcher,
    unwatchFile,
    watchFile,
    watchWithErrorHandler,
)
from misaka.utils.js import UNDEFINED, Timeout, clearTimeout, jsTrim, setTimeout, spawn


@dataclass(slots=True)
class GitPaths:
    repoDir: str
    commonGitDir: str
    headPath: str


def findGitPaths(cwd: str) -> GitPaths | None:
    """Find git metadata paths by walking up from cwd.
    Handles both regular git repos (.git is a directory) and worktrees (.git is a file).
    """
    dir = cwd
    while True:
        gitPath = os.path.join(dir, ".git")
        if os.path.exists(gitPath):
            try:
                if os.path.isfile(gitPath):
                    content = jsTrim(Path(gitPath).read_text(encoding="utf-8"))
                    if content.startswith("gitdir: "):
                        gitDir = os.path.abspath(os.path.join(dir, jsTrim(content[8:])))
                        headPath = os.path.join(gitDir, "HEAD")
                        if not os.path.exists(headPath):
                            return None
                        commonDirPath = os.path.join(gitDir, "commondir")
                        commonGitDir = (
                            os.path.abspath(os.path.join(gitDir, jsTrim(Path(commonDirPath).read_text(encoding="utf-8"))))
                            if os.path.exists(commonDirPath)
                            else gitDir
                        )
                        return GitPaths(repoDir=dir, commonGitDir=commonGitDir, headPath=headPath)
                elif os.path.isdir(gitPath):
                    headPath = os.path.join(gitPath, "HEAD")
                    if not os.path.exists(headPath):
                        return None
                    return GitPaths(repoDir=dir, commonGitDir=gitPath, headPath=headPath)
            except (OSError, ValueError):
                return None
        parent = os.path.dirname(dir)
        if parent == dir:
            return None
        dir = parent


def resolveBranchWithGitSync(repoDir: str) -> str | None:
    """Ask git for the current branch. Returns None on detached HEAD or if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repoDir,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    branch = jsTrim(result.stdout) if result.returncode == 0 else ""
    return branch or None


async def resolveBranchWithGitAsync(repoDir: str) -> str | None:
    """Ask git for the current branch asynchronously. Returns None on detached HEAD or if git is unavailable."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "--no-optional-locks",
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            cwd=repoDir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    branch = jsTrim(stdout.decode("utf-8", errors="replace"))
    return branch or None


def isWslEnvironment() -> bool:
    return sys.platform == "linux" and bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


def isWindowsMountedRepoPath(repoDir: str) -> bool:
    return re.match(r"^/mnt/[a-z](?:/|$)", repoDir, re.IGNORECASE) is not None


def shouldPollGitHead(repoDir: str) -> bool:
    return isWslEnvironment() and isWindowsMountedRepoPath(repoDir)


class FooterDataProvider:
    """Provides git branch and extension statuses - data not otherwise accessible to extensions.
    Context usage on ctx.getContextUsage(), token stats on ctx.sessionManager.getEntries(), model info on ctx.model.
    """

    WATCH_DEBOUNCE_MS = 500

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self._extensionStatuses: dict[str, str] = {}
        self._cachedBranch: Any = UNDEFINED
        self._gitPaths: GitPaths | None = None
        self._headWatcher: FSWatcher | None = None
        self._headWatchFilePath: str | None = None
        self._headWatchFileListener: Callable[[Stats, Stats], None] | None = None
        self._reftableWatcher: FSWatcher | None = None
        self._reftableTablesListWatcher: FSWatcher | None = None
        self._reftableTablesListPath: str | None = None
        self._branchChangeCallbacks: dict[Callable[[], None], None] = {}
        self._availableProviderCount = 0
        self._refreshTimer: Timeout | None = None
        self._gitWatcherRetryTimer: Timeout | None = None
        self._refreshInFlight = False
        self._refreshPending = False
        self._disposed = False
        # MISAKA fork: holds the refresh tasks `void refreshGitBranchAsync()` starts (asyncio keeps tasks weakly).
        self._backgroundTasks: set[asyncio.Task[Any]] = set()
        self._gitPaths = findGitPaths(cwd)
        self._setupGitWatcher()

    def getGitBranch(self) -> str | None:
        """Current git branch, None if not in repo, "detached" if detached HEAD"""
        if self._cachedBranch is UNDEFINED:
            self._cachedBranch = self._resolveGitBranchSync()
        return self._cachedBranch

    def getExtensionStatuses(self) -> Mapping[str, str]:
        """Extension status texts set via ctx.ui.setStatus()"""
        return self._extensionStatuses

    def onBranchChange(self, callback: Callable[[], None]) -> Callable[[], bool]:
        """Subscribe to git branch changes. Returns unsubscribe function."""
        self._branchChangeCallbacks[callback] = None

        def unsubscribe() -> bool:
            return self._branchChangeCallbacks.pop(callback, UNDEFINED) is not UNDEFINED

        return unsubscribe

    def setExtensionStatus(self, key: str, text: str | None) -> None:
        """Internal: set extension status"""
        if text is None:
            self._extensionStatuses.pop(key, None)
        else:
            self._extensionStatuses[key] = text

    def clearExtensionStatuses(self) -> None:
        """Internal: clear extension statuses"""
        self._extensionStatuses.clear()

    def getAvailableProviderCount(self) -> int:
        """Number of unique providers with available models (for footer display)"""
        return self._availableProviderCount

    def setAvailableProviderCount(self, count: int) -> None:
        """Internal: update available provider count"""
        self._availableProviderCount = count

    def setCwd(self, cwd: str) -> None:
        if self._cwd == cwd:
            return

        self._cwd = cwd
        if self._refreshTimer:
            clearTimeout(self._refreshTimer)
            self._refreshTimer = None
        self._clearGitWatchers()
        self._cachedBranch = UNDEFINED
        self._gitPaths = findGitPaths(cwd)
        self._setupGitWatcher()
        self._notifyBranchChange()

    def dispose(self) -> None:
        """Internal: cleanup"""
        self._disposed = True
        if self._refreshTimer:
            clearTimeout(self._refreshTimer)
            self._refreshTimer = None
        self._clearGitWatchers()
        self._branchChangeCallbacks.clear()

    def _notifyBranchChange(self) -> None:
        # MISAKA fork: iterates a copy (a dict cannot change size while iterated; pi walks its Set live).
        for cb in list(self._branchChangeCallbacks):
            cb()

    def _scheduleRefresh(self) -> None:
        if self._disposed or self._refreshTimer:
            return
        if self._refreshInFlight:
            self._refreshPending = True
            return

        def fire() -> None:
            self._refreshTimer = None
            spawn(self._refreshGitBranchAsync(), self._backgroundTasks)

        self._refreshTimer = setTimeout(fire, FooterDataProvider.WATCH_DEBOUNCE_MS)

    async def _refreshGitBranchAsync(self) -> None:
        if self._disposed:
            return
        if self._refreshInFlight:
            self._refreshPending = True
            return

        self._refreshInFlight = True
        try:
            nextBranch = await self._resolveGitBranchAsync()
            if self._disposed:
                return
            if self._cachedBranch is not UNDEFINED and self._cachedBranch != nextBranch:
                self._cachedBranch = nextBranch
                self._notifyBranchChange()
                return
            self._cachedBranch = nextBranch
        finally:
            self._refreshInFlight = False
            if self._refreshPending and not self._disposed:
                self._refreshPending = False
                self._scheduleRefresh()

    def _resolveGitBranchSync(self) -> str | None:
        try:
            if not self._gitPaths:
                return None
            content = jsTrim(Path(self._gitPaths.headPath).read_text(encoding="utf-8"))
            if content.startswith("ref: refs/heads/"):
                branch = content[16:]
                return (resolveBranchWithGitSync(self._gitPaths.repoDir) or "detached") if branch == ".invalid" else branch
            return "detached"
        except (OSError, ValueError):
            return None

    async def _resolveGitBranchAsync(self) -> str | None:
        try:
            if not self._gitPaths:
                return None
            content = jsTrim(Path(self._gitPaths.headPath).read_text(encoding="utf-8"))
            if content.startswith("ref: refs/heads/"):
                branch = content[16:]
                return (
                    ((await resolveBranchWithGitAsync(self._gitPaths.repoDir)) or "detached")
                    if branch == ".invalid"
                    else branch
                )
            return "detached"
        except (OSError, ValueError):
            return None

    def _clearGitWatchers(self) -> None:
        closeWatcher(self._headWatcher)
        self._headWatcher = None
        if self._headWatchFilePath and self._headWatchFileListener:
            unwatchFile(self._headWatchFilePath, self._headWatchFileListener)
            self._headWatchFilePath = None
            self._headWatchFileListener = None
        closeWatcher(self._reftableWatcher)
        self._reftableWatcher = None
        closeWatcher(self._reftableTablesListWatcher)
        self._reftableTablesListWatcher = None
        if self._reftableTablesListPath:
            unwatchFile(self._reftableTablesListPath)
            self._reftableTablesListPath = None
        if self._gitWatcherRetryTimer:
            clearTimeout(self._gitWatcherRetryTimer)
            self._gitWatcherRetryTimer = None

    def _scheduleGitWatcherRetry(self) -> None:
        if self._disposed or self._gitWatcherRetryTimer:
            return

        def retry() -> None:
            self._gitWatcherRetryTimer = None
            self._setupGitWatcher()

        self._gitWatcherRetryTimer = setTimeout(retry, FS_WATCH_RETRY_DELAY_MS)

    def _handleGitWatcherError(self) -> None:
        self._clearGitWatchers()
        self._scheduleGitWatcherRetry()

    def _setupGitWatcher(self) -> None:
        self._clearGitWatchers()
        if not self._gitPaths:
            return
        # MISAKA fork: the debounce timer and the async git call need an event loop; a provider made outside one
        # reads the branch on demand (getGitBranch) and is not refreshed by watchers.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        pollGitHead = shouldPollGitHead(self._gitPaths.repoDir)

        # Watch the directory containing HEAD, not HEAD itself.
        # Git uses atomic writes (write temp, rename over HEAD), which changes the inode.
        # fs.watch on a file stops working after the inode changes.
        def onHeadDirectoryChange(_eventType: str, filename: str | None) -> None:
            if not filename or filename == "HEAD":
                self._scheduleRefresh()

        self._headWatcher = watchWithErrorHandler(
            os.path.dirname(self._gitPaths.headPath),
            onHeadDirectoryChange,
            lambda: self._handleGitWatcherError(),
        )
        if pollGitHead:
            self._headWatchFilePath = self._gitPaths.headPath

            def onHeadStat(current: Stats, previous: Stats) -> None:
                if (
                    current.mtimeMs != previous.mtimeMs
                    or current.ctimeMs != previous.ctimeMs
                    or current.size != previous.size
                ):
                    self._scheduleRefresh()

            self._headWatchFileListener = onHeadStat
            watchFile(self._headWatchFilePath, {"interval": 1000}, self._headWatchFileListener)
        if not self._headWatcher and not pollGitHead:
            return

        # In reftable repos, branch switches update files in the reftable directory
        # instead of HEAD. Watch it separately so the footer picks up those changes.
        reftableDir = os.path.join(self._gitPaths.commonGitDir, "reftable")
        if os.path.exists(reftableDir):
            self._reftableWatcher = watchWithErrorHandler(
                reftableDir,
                lambda *_: self._scheduleRefresh(),
                lambda: self._handleGitWatcherError(),
            )
            if not self._reftableWatcher:
                return

            tablesListPath = os.path.join(reftableDir, "tables.list")
            if os.path.exists(tablesListPath):
                self._reftableTablesListPath = tablesListPath
                self._reftableTablesListWatcher = watchWithErrorHandler(
                    tablesListPath,
                    lambda *_: self._scheduleRefresh(),
                    lambda: self._handleGitWatcherError(),
                )
                if not self._reftableTablesListWatcher:
                    return

                def onTablesListStat(current: Stats, previous: Stats) -> None:
                    if (
                        current.mtimeMs != previous.mtimeMs
                        or current.ctimeMs != previous.ctimeMs
                        or current.size != previous.size
                    ):
                        self._scheduleRefresh()

                watchFile(tablesListPath, {"interval": 250}, onTablesListStat)


class ReadonlyFooterDataProvider(Protocol):
    """Read-only view for extensions - excludes setExtensionStatus, setAvailableProviderCount and dispose"""

    def getGitBranch(self) -> str | None: ...

    def getExtensionStatuses(self) -> Mapping[str, str]: ...

    def getAvailableProviderCount(self) -> int: ...

    def onBranchChange(self, callback: Callable[[], None]) -> Callable[[], bool]: ...


__all__ = [
    "FooterDataProvider",
    "GitPaths",
    "ReadonlyFooterDataProvider",
    "findGitPaths",
]
