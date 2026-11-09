"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``7301-stalled-availability-refresh.test.ts``,
``7497-session-discovery-symlink.test.ts`` and ``7572-provider-retry-settings-merge.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.models import ModelsRefreshOptions
from misaka.core import session_manager
from misaka.core.provider_composer import AuthStatus
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager
from tests.suite_harness import createHarness

# --- issue #7301 stalled availability refresh ---------------------------------------------------


def createDeferred() -> SimpleNamespace:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def resolve() -> None:
        if not future.done():
            future.set_result(None)

    def reject(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    return SimpleNamespace(promise=future, resolve=resolve, reject=reject)


def stallNextCredentialList(harness: Any) -> SimpleNamespace:
    originalList = harness.authStorage.list
    started = createDeferred()
    gate = createDeferred()
    state = {"shouldStall": True, "callCount": 0}

    async def stalledList(*args: Any, **kwargs: Any) -> Any:
        entries = await originalList(*args, **kwargs)
        state["callCount"] += 1
        if not state["shouldStall"]:
            return entries
        state["shouldStall"] = False
        started.resolve()
        await gate.promise
        return entries

    harness.authStorage.list = stalledList

    return SimpleNamespace(
        started=started.promise,
        release=gate.resolve,
        fail=gate.reject,
        getCallCount=lambda: state["callCount"],
    )


async def waitFor(assertion: Callable[[], None], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            assertion()
            return
        except AssertionError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.005)


async def waitForRecoveryRefresh(stalledList: SimpleNamespace, harness: Any) -> None:
    refresh = asyncio.ensure_future(harness.session.modelRuntime.refresh(ModelsRefreshOptions(allowNetwork=False)))

    def calledTwice() -> None:
        assert stalledList.getCallCount() == 2

    await waitFor(calledTwice)
    await refresh


def _apiKey(key: str) -> Any:
    async def update(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key=key)

    return update


async def test_recovers_without_letting_the_stalled_refresh_overwrite_the_newer_snapshot():
    async with createHarness(withConfiguredAuth=False) as harness:
        runtime = harness.session.modelRuntime
        await harness.authStorage.modify("stale-provider", _apiKey("stale-key"))
        await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
        assert runtime.getProviderAuthStatus("stale-provider") == AuthStatus(configured=True, source="stored")

        stalledList = stallNextCredentialList(harness)
        try:
            staleRefresh = asyncio.ensure_future(runtime.getAvailable())
            await stalledList.started

            await harness.authStorage.delete("stale-provider")
            await harness.authStorage.modify("current-provider", _apiKey("current-key"))
            await waitForRecoveryRefresh(stalledList, harness)
            assert runtime.getProviderAuthStatus("stale-provider") == AuthStatus(configured=False)
            assert runtime.getProviderAuthStatus("current-provider") == AuthStatus(configured=True, source="stored")

            stalledList.release()
            await staleRefresh
            assert runtime.getProviderAuthStatus("stale-provider") == AuthStatus(configured=False)
            assert runtime.getProviderAuthStatus("current-provider") == AuthStatus(configured=True, source="stored")
        finally:
            stalledList.release()


async def test_does_not_let_a_stale_failure_overwrite_newer_availability_error_state():
    async with createHarness(withConfiguredAuth=False) as harness:
        runtime = harness.session.modelRuntime
        stalledList = stallNextCredentialList(harness)
        try:
            staleRefresh = asyncio.ensure_future(runtime.getAvailable())
            await stalledList.started

            await waitForRecoveryRefresh(stalledList, harness)
            assert runtime.getError() is None

            stalledList.fail(RuntimeError("stale credential list failure"))
            with pytest.raises(Exception, match="stale credential list failure"):
                await staleRefresh
            assert runtime.getError() is None
        finally:
            stalledList.release()


async def test_does_not_let_a_stale_provider_scoped_failure_overwrite_a_newer_availability_pass():
    async with createHarness() as harness:
        runtime = harness.session.modelRuntime
        # pi reaches the runtime's private `models` collection the same way (`Reflect.get(runtime, "models")`).
        models = runtime._models
        originalGetAvailable = models.getAvailable
        started = createDeferred()
        gate = createDeferred()
        state = {"stall": True}

        async def getAvailable(providerId: Any = None, options: Any = None) -> Any:
            if not providerId or not state["stall"]:
                return await originalGetAvailable(providerId, options)
            state["stall"] = False
            started.resolve()
            await gate.promise
            raise RuntimeError("stale provider availability failure")

        models.getAvailable = getAvailable

        staleRefresh = asyncio.ensure_future(runtime.getAvailable(harness.getModel().provider))
        await started.promise
        await runtime.getAvailable()
        assert runtime.getError() is None

        gate.resolve()
        with pytest.raises(Exception, match="stale provider availability failure"):
            await staleRefresh
        assert runtime.getError() is None


# --- regression #7497: discover sessions through symlinked directories --------------------------


def linkDirectory(target: Path, link: Path) -> None:
    """A directory link: a junction on Windows (pi's `"junction"`), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def sessionsDir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "agent" / "sessions"
    directory.mkdir(parents=True)
    # MISAKA fork: the default sessions store is the home's (`get_sessions_dir`), where pi reads the agent directory
    # from `PI_CODING_AGENT_DIR`.
    monkeypatch.setattr(session_manager, "get_sessions_dir", lambda: str(directory))
    return directory


def writeSession(tmp_path: Path, directory: Path, id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{id}.jsonl").write_text(
        json.dumps({
            "type": "session",
            "version": 3,
            "id": id,
            "timestamp": "2026-08-03T00:00:00.000Z",
            "cwd": str(tmp_path / "project"),
        })
        + "\n",
        encoding="utf-8",
    )


async def test_discovers_a_session_through_a_directory_link_and_preserves_the_alias_path(
    tmp_path: Path, sessionsDir: Path
):
    targetDir = tmp_path / "linked-sessions"
    writeSession(tmp_path, targetDir, "linked")
    aliasDir = sessionsDir / "--linked--"
    linkDirectory(targetDir, aliasDir)

    sessions = await SessionManager.listAll()

    assert [session.id for session in sessions] == ["linked"]
    assert sessions[0].path == os.path.join(str(aliasDir), "linked.jsonl")


async def test_ignores_a_broken_directory_link_without_hiding_valid_sessions(tmp_path: Path, sessionsDir: Path):
    writeSession(tmp_path, sessionsDir / "--regular--", "regular")
    targetDir = tmp_path / "removed-sessions"
    targetDir.mkdir()
    linkDirectory(targetDir, sessionsDir / "--broken--")
    shutil.rmtree(targetDir)

    sessions = await SessionManager.listAll()

    assert [session.id for session in sessions] == ["regular"]


@pytest.mark.skipif(sys.platform == "win32", reason="file symlinks need privileges on Windows")
async def test_ignores_links_to_files(tmp_path: Path, sessionsDir: Path):
    writeSession(tmp_path, sessionsDir / "--regular--", "regular")
    targetFile = tmp_path / "not-a-directory"
    targetFile.write_text("", encoding="utf-8")
    os.symlink(targetFile, sessionsDir / "--file--")

    sessions = await SessionManager.listAll()

    assert [session.id for session in sessions] == ["regular"]


# --- regression #7572: nested provider retry settings merge -------------------------------------


def test_preserves_global_provider_settings_not_overridden_by_the_project():
    storage = InMemorySettingsStorage()
    storage.withLock(
        "global", lambda _current: json.dumps({"retry": {"provider": {"timeoutMs": 30000, "maxRetryDelayMs": 45000}}})
    )
    storage.withLock("project", lambda _current: json.dumps({"retry": {"provider": {"maxRetries": 2}}}))

    settingsManager = SettingsManager.fromStorage(storage)

    assert settingsManager.getProviderRetrySettings() == {
        "timeoutMs": 30000,
        "maxRetries": 2,
        "maxRetryDelayMs": 45000,
    }
