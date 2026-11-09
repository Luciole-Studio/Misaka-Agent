"""Translated from pi's ``packages/coding-agent/test/footer-data-provider.test.ts`` (v1.1.0).

pi mocks ``child_process``: ``spawnSync`` is ``subprocess.run`` here (the provider's synchronous git call) and
``execFile`` is ``asyncio.create_subprocess_exec`` (its asynchronous one).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.core import footer_data_provider
from misaka.core.footer_data_provider import FooterDataProvider
from tests.oauth_fetch import useFakeTimers


class GitMocks:
    def __init__(self) -> None:
        self.resolvedBranch = "main"
        self.spawnSyncCalls: list[tuple[Any, dict[str, Any]]] = []
        self.execFileCalls: list[tuple[Any, dict[str, Any]]] = []

    def spawnSync(self, args: list[str], **options: Any) -> SimpleNamespace:
        self.spawnSyncCalls.append((args, options))
        if "symbolic-ref" in args:
            return SimpleNamespace(
                returncode=0 if self.resolvedBranch else 1,
                stdout=f"{self.resolvedBranch}\n" if self.resolvedBranch else "",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    async def execFile(self, *args: Any, **options: Any) -> Any:
        self.execFileCalls.append((args, options))
        resolvedBranch = self.resolvedBranch if "symbolic-ref" in args else ""

        async def communicate() -> tuple[bytes, bytes]:
            # pi's mock answers from a `setTimeout(..., 0)`.
            await asyncio.sleep(0)
            return (f"{resolvedBranch}\n".encode() if resolvedBranch else b"", b"")

        return SimpleNamespace(communicate=communicate, returncode=0 if resolvedBranch else 1)


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> GitMocks:
    mocks = GitMocks()
    monkeypatch.setattr(
        footer_data_provider,
        "subprocess",
        SimpleNamespace(run=mocks.spawnSync, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mocks.execFile)
    return mocks


def createPlainReftableRepo(tempDir: Path) -> Path:
    repoDir = tempDir / "repo"
    (repoDir / ".git" / "reftable").mkdir(parents=True)
    (repoDir / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")
    return repoDir


def createPlainRepo(tempDir: Path) -> Path:
    repoDir = tempDir / "repo"
    (repoDir / ".git").mkdir(parents=True)
    (repoDir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return repoDir


def createReftableWorktree(tempDir: Path) -> SimpleNamespace:
    repoDir = tempDir / "repo"
    commonGitDir = repoDir / ".git"
    gitDir = commonGitDir / "worktrees" / "src"
    worktreeDir = tempDir / "worktree"
    reftableDir = commonGitDir / "reftable"

    gitDir.mkdir(parents=True)
    reftableDir.mkdir(parents=True)
    worktreeDir.mkdir(parents=True)

    (worktreeDir / ".git").write_text(f"gitdir: {gitDir}\n", encoding="utf-8")
    (gitDir / "HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")
    (gitDir / "commondir").write_text("../..\n", encoding="utf-8")
    (reftableDir / "tables.list").write_text("0\n", encoding="utf-8")

    return SimpleNamespace(worktreeDir=worktreeDir, reftableDir=reftableDir)


def emitReftableChange(provider: FooterDataProvider) -> None:
    reftableWatcher = provider._reftableWatcher
    assert reftableWatcher is not None
    reftableWatcher.emit("change", "change", "tables.list")


async def waitFor(condition: Any, timeoutMs: int = 3000) -> None:
    startedAt = time.monotonic()
    while not condition():
        if (time.monotonic() - startedAt) * 1000 > timeoutMs:
            raise AssertionError("Timed out waiting for condition")
        await asyncio.sleep(0.01)


@pytest.fixture
def tempDir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # process.chdir is restored by monkeypatch.
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_uses_head_directly_in_a_regular_repo_from_a_nested_directory(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    repoDir = createPlainRepo(tempDir)
    nestedDir = repoDir / "src" / "nested"
    nestedDir.mkdir(parents=True)
    monkeypatch.chdir(nestedDir)

    provider = FooterDataProvider(str(nestedDir))
    try:
        assert provider.getGitBranch() == "main"
        assert git.spawnSyncCalls == []
    finally:
        provider.dispose()


async def test_resolves_the_branch_via_git_when_head_is_invalid_in_a_reftable_repo(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    repoDir = createPlainReftableRepo(tempDir)
    monkeypatch.chdir(repoDir)

    provider = FooterDataProvider(str(repoDir))
    try:
        assert provider.getGitBranch() == "main"
        assert len(git.spawnSyncCalls) == 1
        args, options = git.spawnSyncCalls[0]
        assert args == ["git", "--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"]
        assert str(options["cwd"]).endswith("repo")
        assert options["encoding"] == "utf-8"
        # stdio: ["ignore", "pipe", "ignore"]
        assert (options["stdin"], options["stdout"], options["stderr"]) == (
            subprocess.DEVNULL,
            subprocess.PIPE,
            subprocess.DEVNULL,
        )
    finally:
        provider.dispose()


async def test_resolves_the_branch_via_git_in_a_reftable_backed_worktree(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    worktreeDir = createReftableWorktree(tempDir).worktreeDir
    monkeypatch.chdir(worktreeDir)

    provider = FooterDataProvider(str(worktreeDir))
    try:
        assert provider.getGitBranch() == "main"
    finally:
        provider.dispose()


async def test_treats_an_unresolved_invalid_reftable_head_as_detached(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    repoDir = createPlainReftableRepo(tempDir)
    monkeypatch.chdir(repoDir)
    git.resolvedBranch = ""

    provider = FooterDataProvider(str(repoDir))
    try:
        assert provider.getGitBranch() == "detached"
    finally:
        provider.dispose()


# Drive debounce behavior explicitly; native fs.watch delivery can race watcher startup.
async def test_does_not_notify_listeners_when_reftable_updates_keep_the_same_branch(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    timers = useFakeTimers(monkeypatch)
    worktreeDir = createReftableWorktree(tempDir).worktreeDir
    monkeypatch.chdir(worktreeDir)

    provider = FooterDataProvider(str(worktreeDir))
    try:
        assert provider.getGitBranch() == "main"
        git.spawnSyncCalls.clear()
        onBranchChange: list[None] = []
        provider.onBranchChange(lambda: onBranchChange.append(None))

        emitReftableChange(provider)
        await timers.advanceAsync(501)

        assert len(git.execFileCalls) == 1
        assert git.spawnSyncCalls == []
        assert provider.getGitBranch() == "main"
        assert onBranchChange == []
    finally:
        provider.dispose()


async def test_debounces_rapid_reftable_updates_into_a_single_async_refresh(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    timers = useFakeTimers(monkeypatch)
    worktreeDir = createReftableWorktree(tempDir).worktreeDir
    monkeypatch.chdir(worktreeDir)

    provider = FooterDataProvider(str(worktreeDir))
    try:
        assert provider.getGitBranch() == "main"
        git.execFileCalls.clear()

        emitReftableChange(provider)
        emitReftableChange(provider)
        emitReftableChange(provider)
        await timers.advanceAsync(499)
        assert git.execFileCalls == []
        await timers.advanceAsync(2)
        assert len(git.execFileCalls) == 1
        await timers.advanceAsync(650)
        assert len(git.execFileCalls) == 1
    finally:
        provider.dispose()


async def test_updates_the_cached_branch_when_the_reftable_directory_changes(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = createReftableWorktree(tempDir)
    monkeypatch.chdir(fixture.worktreeDir)

    provider = FooterDataProvider(str(fixture.worktreeDir))
    try:
        assert provider.getGitBranch() == "main"
        git.resolvedBranch = "foo"
        onBranchChange: list[None] = []
        provider.onBranchChange(lambda: onBranchChange.append(None))

        tablesList = fixture.reftableDir / "tables.list"
        before = os.stat(tablesList)
        tablesList.write_text("1\n", encoding="utf-8")
        # MISAKA fork: the watchers poll stat (pi's use inotify). A same-size rewrite within the file system's timestamp
        # tick right after creation changes nothing a poll can see, so the modification time is moved on explicitly.
        os.utime(tablesList, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        # Real fs watcher events can arrive late under full-suite load.
        await waitFor(lambda: len(git.execFileCalls) == 1, 10_000)
        await waitFor(lambda: provider.getGitBranch() == "foo", 10_000)

        assert len(git.execFileCalls) == 1
        assert provider.getGitBranch() == "foo"
        assert len(onBranchChange) == 1
    finally:
        provider.dispose()


async def test_retries_git_watchers_5_seconds_after_an_async_fs_watch_error(
    git: GitMocks, tempDir: Path, monkeypatch: pytest.MonkeyPatch
):
    timers = useFakeTimers(monkeypatch)
    repoDir = createPlainRepo(tempDir)
    monkeypatch.chdir(repoDir)

    provider = FooterDataProvider(str(repoDir))
    try:
        originalWatcher = provider._headWatcher
        assert originalWatcher is not None
        assert originalWatcher.listenerCount("error") > 0

        originalWatcher.emit("error", RuntimeError("simulated EMFILE"))
        assert provider._headWatcher is None

        await timers.advanceAsync(4999)
        assert provider._headWatcher is None

        await timers.advanceAsync(1)
        assert provider._headWatcher is not None
        assert provider._headWatcher is not originalWatcher
    finally:
        provider.dispose()
