"""Footer data provider for git branch and extension footer state."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from misaka.utils.fs_watch import (
    FS_WATCH_RETRY_DELAY_MS,
    FSWatcher,
    close_watcher,
    watch_with_error_handler,
)

_TABLES_LIST_POLL_INTERVAL_SECONDS = 0.25
_UNSET = object()


@dataclass(slots=True)
class _GitPaths:
    repoDir: str
    commonGitDir: str
    headPath: str


def find_git_paths(cwd: str) -> _GitPaths | None:
    dir_path = cwd
    while True:
        git_path = os.path.join(dir_path, ".git")
        if os.path.exists(git_path):
            try:
                if os.path.isfile(git_path):
                    content = Path(git_path).read_text(encoding="utf-8").strip()
                    if content.startswith("gitdir: "):
                        git_dir = os.path.abspath(os.path.join(dir_path, content[8:].strip()))
                        head_path = os.path.join(git_dir, "HEAD")
                        if not os.path.exists(head_path):
                            return None
                        common_dir_path = os.path.join(git_dir, "commondir")
                        common_git_dir = (
                            os.path.abspath(
                                os.path.join(
                                    git_dir,
                                    Path(common_dir_path).read_text(encoding="utf-8").strip(),
                                )
                            )
                            if os.path.exists(common_dir_path)
                            else git_dir
                        )
                        return _GitPaths(repoDir=dir_path, commonGitDir=common_git_dir, headPath=head_path)
                elif os.path.isdir(git_path):
                    head_path = os.path.join(git_path, "HEAD")
                    if not os.path.exists(head_path):
                        return None
                    return _GitPaths(repoDir=dir_path, commonGitDir=git_path, headPath=head_path)
            except OSError:
                return None
        parent = os.path.dirname(dir_path)
        if parent == dir_path:
            return None
        dir_path = parent


def _resolve_branch_with_git_sync(repo_dir: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo_dir,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    branch = result.stdout.strip() if result.returncode == 0 else ""
    return branch or None


class FooterDataProvider:
    WATCH_DEBOUNCE_MS = 500

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.extensionStatuses: dict[str, str] = {}
        self.cachedBranch: object | str | None = _UNSET
        self.gitPaths: _GitPaths | None = find_git_paths(cwd)
        self.headWatcher: FSWatcher | None = None
        self.reftableWatcher: FSWatcher | None = None
        self.reftableTablesListWatcher: FSWatcher | None = None
        self.reftableTablesListPath: str | None = None
        self.branchChangeCallbacks: set[Callable[[], None]] = set()
        self.availableProviderCount = 0
        self.refreshTimer: threading.Timer | None = None
        self.gitWatcherRetryTimer: threading.Timer | None = None
        self.refreshInFlight = False
        self.refreshPending = False
        self.disposed = False
        self._lock = threading.RLock()
        self._tablesListPollStop = threading.Event()
        self._tablesListPollThread: threading.Thread | None = None
        self.setupGitWatcher()

    def getGitBranch(self) -> str | None:
        with self._lock:
            if self.cachedBranch is _UNSET:
                self.cachedBranch = self.resolveGitBranchSync()
            return None if self.cachedBranch is _UNSET else self.cachedBranch

    def getExtensionStatuses(self) -> Mapping[str, str]:
        return self.extensionStatuses

    def onBranchChange(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self.branchChangeCallbacks.add(callback)

        def unsubscribe() -> None:
            with self._lock:
                self.branchChangeCallbacks.discard(callback)

        return unsubscribe

    def setExtensionStatus(self, key: str, text: str | None) -> None:
        if text is None:
            self.extensionStatuses.pop(key, None)
        else:
            self.extensionStatuses[key] = text

    def clearExtensionStatuses(self) -> None:
        self.extensionStatuses.clear()

    def getAvailableProviderCount(self) -> int:
        return self.availableProviderCount

    def setAvailableProviderCount(self, count: int) -> None:
        self.availableProviderCount = count

    def setCwd(self, cwd: str) -> None:
        if self.cwd == cwd:
            return

        self.cwd = cwd
        with self._lock:
            if self.refreshTimer is not None:
                self.refreshTimer.cancel()
                self.refreshTimer = None
        self.clearGitWatchers()
        self.cachedBranch = _UNSET
        self.gitPaths = find_git_paths(cwd)
        self.setupGitWatcher()
        self.notifyBranchChange()

    def dispose(self) -> None:
        with self._lock:
            self.disposed = True
            if self.refreshTimer is not None:
                self.refreshTimer.cancel()
                self.refreshTimer = None
        self.clearGitWatchers()
        with self._lock:
            self.branchChangeCallbacks.clear()

    def notifyBranchChange(self) -> None:
        with self._lock:
            callbacks = list(self.branchChangeCallbacks)
        for callback in callbacks:
            callback()

    def scheduleRefresh(self) -> None:
        with self._lock:
            if self.disposed or self.refreshTimer is not None:
                return
            if self.refreshInFlight:
                self.refreshPending = True
                return
            self.refreshTimer = threading.Timer(self.WATCH_DEBOUNCE_MS / 1000, self.refreshGitBranchAsync)
            self.refreshTimer.daemon = True
            self.refreshTimer.start()

    def refreshGitBranchAsync(self) -> None:
        with self._lock:
            if self.disposed:
                return
            self.refreshTimer = None
            if self.refreshInFlight:
                self.refreshPending = True
                return
            self.refreshInFlight = True
        try:
            next_branch = self.resolveGitBranchAsync()
            with self._lock:
                if self.disposed:
                    return
                if self.cachedBranch is not _UNSET and self.cachedBranch != next_branch:
                    self.cachedBranch = next_branch
                    self.notifyBranchChange()
                    return
                self.cachedBranch = next_branch
        finally:
            with self._lock:
                self.refreshInFlight = False
                if self.refreshPending and not self.disposed:
                    self.refreshPending = False
                    self.scheduleRefresh()

    def resolveGitBranchSync(self) -> str | None:
        try:
            if self.gitPaths is None:
                return None
            content = Path(self.gitPaths.headPath).read_text(encoding="utf-8").strip()
            if content.startswith("ref: refs/heads/"):
                branch = content[16:]
                return _resolve_branch_with_git_sync(self.gitPaths.repoDir) or "detached" if branch == ".invalid" else branch
            return "detached"
        except OSError:
            return None

    def resolveGitBranchAsync(self) -> str | None:
        try:
            if self.gitPaths is None:
                return None
            content = Path(self.gitPaths.headPath).read_text(encoding="utf-8").strip()
            if content.startswith("ref: refs/heads/"):
                branch = content[16:]
                return _resolve_branch_with_git_sync(self.gitPaths.repoDir) or "detached" if branch == ".invalid" else branch
            return "detached"
        except OSError:
            return None

    def clearGitWatchers(self) -> None:
        close_watcher(self.headWatcher)
        self.headWatcher = None
        close_watcher(self.reftableWatcher)
        self.reftableWatcher = None
        close_watcher(self.reftableTablesListWatcher)
        self.reftableTablesListWatcher = None
        self._stopTablesListPoll()
        self.reftableTablesListPath = None
        if self.gitWatcherRetryTimer is not None:
            self.gitWatcherRetryTimer.cancel()
            self.gitWatcherRetryTimer = None

    def scheduleGitWatcherRetry(self) -> None:
        with self._lock:
            if self.disposed or self.gitWatcherRetryTimer is not None:
                return
            self.gitWatcherRetryTimer = threading.Timer(FS_WATCH_RETRY_DELAY_MS / 1000, self._retryGitWatcher)
            self.gitWatcherRetryTimer.daemon = True
            self.gitWatcherRetryTimer.start()

    def _retryGitWatcher(self) -> None:
        with self._lock:
            self.gitWatcherRetryTimer = None
        self.setupGitWatcher()

    def handleGitWatcherError(self) -> None:
        self.clearGitWatchers()
        self.scheduleGitWatcherRetry()

    def setupGitWatcher(self) -> None:
        self.clearGitWatchers()
        if self.gitPaths is None:
            return

        def on_head_change(_event_type: str, filename: str | None = None) -> None:
            if filename is None or filename == "HEAD":
                self.scheduleRefresh()

        # The polling watcher resolves this path again on every tick, so it follows
        # Git's atomic HEAD replacement without binding to the previous inode.
        self.headWatcher = watch_with_error_handler(
            self.gitPaths.headPath,
            on_head_change,
            lambda: self.handleGitWatcherError(),
        )
        if self.headWatcher is None:
            return

        reftable_dir = os.path.join(self.gitPaths.commonGitDir, "reftable")
        if os.path.exists(reftable_dir):
            self.reftableWatcher = watch_with_error_handler(
                reftable_dir,
                lambda _event_type, _filename=None: self.scheduleRefresh(),
                lambda: self.handleGitWatcherError(),
            )
            if self.reftableWatcher is None:
                return

            tables_list_path = os.path.join(reftable_dir, "tables.list")
            if os.path.exists(tables_list_path):
                self.reftableTablesListPath = tables_list_path
                self.reftableTablesListWatcher = watch_with_error_handler(
                    tables_list_path,
                    lambda _event_type, _filename=None: self.scheduleRefresh(),
                    lambda: self.handleGitWatcherError(),
                )
                if self.reftableTablesListWatcher is None:
                    return
                self._startTablesListPoll(tables_list_path)

    def _startTablesListPoll(self, path: str) -> None:
        self._stopTablesListPoll()
        self._tablesListPollStop = threading.Event()
        previous = self._tables_list_stat(path)

        def watch() -> None:
            nonlocal previous
            while not self._tablesListPollStop.wait(_TABLES_LIST_POLL_INTERVAL_SECONDS):
                current = self._tables_list_stat(path)
                if current != previous:
                    previous = current
                    self.scheduleRefresh()

        self._tablesListPollThread = threading.Thread(
            target=watch,
            name="footer-tables-list-watch",
            daemon=True,
        )
        self._tablesListPollThread.start()

    def _stopTablesListPoll(self) -> None:
        self._tablesListPollStop.set()
        if self._tablesListPollThread is not None and threading.current_thread() is not self._tablesListPollThread:
            self._tablesListPollThread.join(timeout=0.5)
        self._tablesListPollThread = None
        self._tablesListPollStop = threading.Event()

    def _tables_list_stat(self, path: str) -> tuple[int, int, int] | None:
        try:
            stat_result = os.stat(path)
        except OSError:
            return None
        return (stat_result.st_mtime_ns, stat_result.st_ctime_ns, stat_result.st_size)


__all__ = [
    "FooterDataProvider",
    "find_git_paths",
]
