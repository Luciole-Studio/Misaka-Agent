"""Ported from pi's ``packages/coding-agent/test/auth-storage.test.ts`` (v1.1.0).

pi spies on proper-lockfile's ``lock``; here the same seams are the backend's own: ``acquireLockAsync``
(one async acquisition), ``_create_lock`` (one attempt at the lock file) and ``_lock_signature`` (how a compromised
lock is noticed).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from misaka.ai.auth.types import (
    ApiKeyCredential,
    AuthOperationOptions,
    ModelAuth,
    OAuthAuth,
    ProviderAuth,
)
from misaka.ai.models import CreateModelsOptions, createModels
from misaka.ai.utils.models_error import ModelsError
from misaka.core import auth_storage
from misaka.core.auth_storage import AuthStorage, FileAuthStorageBackend, LockResult
from misaka.utils.js import AbortController, DOMException, startTask


@pytest.fixture
def authJsonPath(tmp_path):
    return tmp_path / "auth.json"


def writeAuthJson(path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _moveRevisionOn(path) -> None:
    """A coarse kernel clock can give two same-size writes in one tick the same revision; on a fine-grained clock the
    second write is a change, so move its mtime on."""
    stats = os.stat(path)
    os.utime(path, ns=(stats.st_atime_ns, stats.st_mtime_ns + 1_000_000))


def _plain(credential):
    """``toEqual`` ignores undefined properties."""
    return None if credential is None else credential.model_dump(exclude_none=True)


def _options(controller: AbortController) -> AuthOperationOptions:
    return AuthOperationOptions(signal=controller.signal)


async def _rejectsWithAbortError(awaitable) -> None:
    with pytest.raises(DOMException) as caught:
        await awaitable
    assert caught.value.name == "AbortError"


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Release:
    """What proper-lockfile's ``lock`` resolves to: a release that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, timeout=None) -> None:
        pass

    def release(self) -> None:
        self.calls += 1


class _Spy:
    def __init__(self, implementation=None) -> None:
        self.calls = 0
        self.implementation = implementation

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.implementation is not None:
            return await self.implementation(*args, **kwargs)
        return LockResult(result=None)


def _spyOnAsyncLock(monkeypatch, implementation=None) -> _Spy:
    original = FileAuthStorageBackend.acquireLockAsync
    spy = _Spy()

    async def lock(self, options=None):
        spy.calls += 1
        if implementation is not None:
            return await implementation()
        return await original(self, options)

    monkeypatch.setattr(FileAuthStorageBackend, "acquireLockAsync", lock)
    return spy


def _spyOnLockAttempts(monkeypatch, *locks) -> _Spy:
    """Each attempt at the lock file; ``locks`` replace the first attempts' locks, in order."""
    original = FileAuthStorageBackend._create_lock
    queue = list(locks)
    spy = _Spy()

    def create(self):
        spy.calls += 1
        return queue.pop(0) if queue else original(self)

    monkeypatch.setattr(FileAuthStorageBackend, "_create_lock", create)
    return spy


async def test_reads_and_resolves_stored_api_key_credentials(authJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_AUTH_STORAGE_KEY", "environment-key")
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "$TEST_AUTH_STORAGE_KEY"}})
    storage = AuthStorage.create(str(authJsonPath))
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "environment-key"}


async def test_resolves_command_backed_api_key_credentials(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "!printf 'command-key'"}})
    storage = AuthStorage.create(str(authJsonPath))
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "command-key"}


async def test_returns_oauth_credentials_unchanged():
    credential = {"type": "oauth", "access": "access-token", "refresh": "refresh-token", "expires": _now_ms() + 60_000}
    storage = AuthStorage.inMemory({"anthropic": credential})
    assert _plain(await storage.read("anthropic")) == credential


async def test_credential_scoped_env_takes_precedence_and_remains_inspectable(authJsonPath):
    writeAuthJson(authJsonPath, {
        "anthropic": {
            "type": "api_key",
            "key": "$SCOPED_KEY",
            "env": {"SCOPED_KEY": "scoped-value", "REGION": "test-region"},
        },
    })
    storage = AuthStorage.create(str(authJsonPath))
    credential = await storage.read("anthropic")
    assert credential.key == "scoped-value"
    assert credential.env == {"SCOPED_KEY": "scoped-value", "REGION": "test-region"}


async def test_coalesces_file_reloads_across_concurrent_readers_and_storage_instances(authJsonPath, tmp_path, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "old"}})
    first = AuthStorage.create(str(authJsonPath))
    second = AuthStorage.create(str(authJsonPath))
    lockSpy = _spyOnAsyncLock(monkeypatch)

    writeAuthJson(authJsonPath, {
        "anthropic": {"type": "api_key", "key": "new"},
        "openai": {"type": "api_key", "key": "openai-key"},
    })

    anthropic, openai, credentials = await asyncio.gather(
        first.read("anthropic", _options(AbortController())),
        second.read("openai", _options(AbortController())),
        first.list(_options(AbortController())),
    )
    assert _plain(anthropic) == {"type": "api_key", "key": "new"}
    assert _plain(openai) == {"type": "api_key", "key": "openai-key"}
    assert [(entry.providerId, entry.type) for entry in credentials] == [
        ("anthropic", "api_key"),
        ("openai", "api_key"),
    ]
    assert lockSpy.calls == 1

    assert _plain(await second.read("anthropic")) == {"type": "api_key", "key": "new"}
    assert lockSpy.calls == 1

    otherPath = tmp_path / "other-auth.json"
    otherPath.write_text(json.dumps({"other": {"type": "api_key", "key": "other-key"}}), encoding="utf-8")
    otherFirst = AuthStorage.create(str(otherPath))
    otherSecond = AuthStorage.create(str(otherPath))
    await otherFirst.read("other")
    await otherSecond.read("other")
    await otherFirst.list()
    assert lockSpy.calls == 1

    third = AuthStorage.create(str(authJsonPath))
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "newest"}})
    firstReload, thirdReload = await asyncio.gather(first.read("anthropic"), third.read("anthropic"))
    assert _plain(firstReload) == {"type": "api_key", "key": "newest"}
    assert _plain(thirdReload) == {"type": "api_key", "key": "newest"}
    assert lockSpy.calls == 2


async def test_keeps_a_coalesced_reload_alive_while_another_credential_reader_is_waiting(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "old"}})
    storage = AuthStorage.create(str(authJsonPath))
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "new"}})
    _moveRevisionOn(authJsonPath)
    lockGranted = asyncio.Event()
    release = _Release()

    async def grant():
        await lockGranted.wait()
        return release

    lockSpy = _spyOnAsyncLock(monkeypatch, grant)
    firstController = AbortController()
    secondController = AbortController()
    first = startTask(storage.read("anthropic", _options(firstController)))
    second = startTask(storage.read("anthropic", _options(secondController)))

    firstController.abort()
    await _rejectsWithAbortError(first)
    lockGranted.set()
    assert _plain(await second) == {"type": "api_key", "key": "new"}
    assert lockSpy.calls == 1
    assert release.calls == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_creates_new_auth_files_with_owner_only_permissions(authJsonPath):
    AuthStorage.create(str(authJsonPath))

    assert authJsonPath.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_preserves_the_mode_of_an_existing_auth_file(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "old"}})
    os.chmod(authJsonPath, 0o660)
    storage = AuthStorage.create(str(authJsonPath))

    async def replace(_current):
        return ApiKeyCredential(key="new")

    await storage.modify("anthropic", replace)

    assert authJsonPath.stat().st_mode & 0o777 == 0o660


async def test_modify_persists_a_credential_while_preserving_unrelated_external_edits(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "old"}})
    storage = AuthStorage.create(str(authJsonPath))
    writeAuthJson(authJsonPath, {
        "anthropic": {"type": "api_key", "key": "old"},
        "openai": {"type": "api_key", "key": "external"},
    })

    async def replace(_current):
        return ApiKeyCredential(key="new")

    await storage.modify("anthropic", replace)

    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "new"},
        "openai": {"type": "api_key", "key": "external"},
    }


async def test_modify_with_undefined_leaves_the_current_credential_unchanged(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    storage = AuthStorage.create(str(authJsonPath))

    async def keep(_current):
        return None

    assert _plain(await storage.modify("anthropic", keep)) == {"type": "api_key", "key": "stored"}
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "stored"}


async def test_serializes_concurrent_modifications(authJsonPath):
    writeAuthJson(authJsonPath, {})
    first = AuthStorage.create(str(authJsonPath))
    second = AuthStorage.create(str(authJsonPath))

    async def anthropic(_current):
        return ApiKeyCredential(key="anthropic-key")

    async def openai(_current):
        return ApiKeyCredential(key="openai-key")

    await asyncio.gather(first.modify("anthropic", anthropic), second.modify("openai", openai))
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "anthropic-key"},
        "openai": {"type": "api_key", "key": "openai-key"},
    }


async def test_delete_removes_one_credential_while_preserving_others(authJsonPath):
    writeAuthJson(authJsonPath, {
        "anthropic": {"type": "api_key", "key": "anthropic-key"},
        "openai": {"type": "api_key", "key": "openai-key"},
    })
    storage = AuthStorage.create(str(authJsonPath))
    writeAuthJson(authJsonPath, {
        "anthropic": {"type": "api_key", "key": "anthropic-key"},
        "openai": {"type": "api_key", "key": "openai-key"},
        "google": {"type": "api_key", "key": "external-key"},
    })
    await storage.delete("anthropic")
    assert [(entry.providerId, entry.type) for entry in await storage.list()] == [
        ("openai", "api_key"),
        ("google", "api_key"),
    ]
    assert await storage.read("anthropic") is None
    assert _plain(await storage.read("openai")) == {"type": "api_key", "key": "openai-key"}
    assert _plain(await storage.read("google")) == {"type": "api_key", "key": "external-key"}


async def test_in_memory_storage_implements_the_same_credential_store_behavior():
    storage = AuthStorage.inMemory({"anthropic": {"type": "api_key", "key": "initial"}})
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "initial"}

    async def update(_current):
        return ApiKeyCredential(key="updated")

    await storage.modify("anthropic", update)
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "updated"}
    await storage.delete("anthropic")
    assert await storage.list() == []


async def test_does_not_write_after_lock_acquisition_failure_and_recovers_on_retry(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    storage = AuthStorage.create(str(authJsonPath))

    class _Unavailable(_Release):
        def acquire(self, timeout=None) -> None:
            raise RuntimeError("lock unavailable")

    _spyOnLockAttempts(monkeypatch, _Unavailable())

    async def new(_current):
        return ApiKeyCredential(key="new")

    with pytest.raises(RuntimeError, match="lock unavailable"):
        await storage.modify("openai", new)
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "stored"},
    }

    await storage.modify("openai", new)
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "stored"},
        "openai": {"type": "api_key", "key": "new"},
    }


async def test_retries_a_briefly_contended_file_lock(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    backend = FileAuthStorageBackend(str(authJsonPath))
    release = _Release()

    class _Locked(_Release):
        def acquire(self, timeout=None) -> None:
            raise Timeout(f"{authJsonPath}.lock")

    lockSpy = _spyOnLockAttempts(monkeypatch, _Locked(), release)
    monkeypatch.setattr(auth_storage.random, "random", lambda: 0)
    update = _Spy()

    await backend.withLockAsync(update)

    assert lockSpy.calls == 2
    assert update.calls == 1
    assert release.calls == 1


async def test_surfaces_a_compromised_file_storage_lock(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    backend = FileAuthStorageBackend(str(authJsonPath))

    async def mutation(_current):
        return LockResult(result=None, next=json.dumps({}))

    update = _Spy(mutation)
    # The lock is taken over as soon as it is held: every later look at it sees another lock.
    signatures = iter([(0, 0)])
    monkeypatch.setattr(FileAuthStorageBackend, "_lock_signature", lambda self: next(signatures, (1, 1)))

    with pytest.raises(RuntimeError, match="lock was compromised"):
        await backend.withLockAsync(update)
    assert update.calls == 0
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "stored"},
    }


async def test_pre_aborted_file_operations_do_not_create_the_backing_file_or_run_the_mutation(authJsonPath):
    backend = FileAuthStorageBackend(str(authJsonPath))
    controller = AbortController()
    controller.abort()

    async def mutation(_current):
        return LockResult(result=None, next=json.dumps({}))

    update = _Spy(mutation)

    await _rejectsWithAbortError(backend.withLockAsync(update, _options(controller)))
    assert update.calls == 0
    assert not authJsonPath.exists()


async def test_aborts_while_waiting_for_a_held_file_lock_without_running_the_mutation_later(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    held = FileLock(f"{authJsonPath}.lock")
    held.acquire()
    backend = FileAuthStorageBackend(str(authJsonPath))
    controller = AbortController()

    async def mutation(_current):
        return LockResult(result=None, next=json.dumps({}))

    update = _Spy(mutation)
    pending = startTask(backend.withLockAsync(update, _options(controller)))

    await asyncio.sleep(0.01)
    controller.abort()
    await _rejectsWithAbortError(pending)
    assert update.calls == 0

    held.release()
    await asyncio.sleep(0.15)
    assert update.calls == 0
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "stored"},
    }


async def test_releases_a_file_lock_acquired_concurrently_with_cancellation_before_mutation(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    backend = FileAuthStorageBackend(str(authJsonPath))
    controller = AbortController()

    class _AbortingWhileGranted(_Release):
        def acquire(self, timeout=None) -> None:
            controller.abort()

    release = _AbortingWhileGranted()
    _spyOnLockAttempts(monkeypatch, release)

    async def mutation(_current):
        return LockResult(result=None, next=json.dumps({}))

    update = _Spy(mutation)

    await _rejectsWithAbortError(backend.withLockAsync(update, _options(controller)))
    await asyncio.sleep(0)
    assert update.calls == 0
    assert release.calls == 1


async def test_holds_the_file_lock_until_a_cancelled_active_callback_settles_without_committing_it(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    backend = FileAuthStorageBackend(str(authJsonPath))
    controller = AbortController()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def cancelled(_current):
        started.set()
        await blocked.wait()
        return LockResult(result=None, next=json.dumps({"openai": {"type": "api_key", "key": "cancelled"}}))

    pending = startTask(backend.withLockAsync(cancelled, _options(controller)))

    await started.wait()
    controller.abort()

    async def committed(_current):
        return LockResult(result=None, next=json.dumps({"google": {"type": "api_key", "key": "committed"}}))

    competingMutation = _Spy(committed)
    competing = startTask(backend.withLockAsync(competingMutation))
    await asyncio.sleep(0.02)
    assert competingMutation.calls == 0

    blocked.set()
    await _rejectsWithAbortError(pending)
    await competing
    assert competingMutation.calls == 1
    assert json.loads(authJsonPath.read_text(encoding="utf-8")) == {
        "google": {"type": "api_key", "key": "committed"},
    }


async def test_cancels_a_signalled_credential_read_waiting_for_a_held_file_lock(authJsonPath, monkeypatch):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "old"}})
    storage = AuthStorage.create(str(authJsonPath))
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "new-value"}})
    held = FileLock(f"{authJsonPath}.lock")
    held.acquire()
    lockSpy = _spyOnLockAttempts(monkeypatch)
    controller = AbortController()
    pending = startTask(storage.read("anthropic", _options(controller)))

    await asyncio.sleep(0.01)
    controller.abort()
    await _rejectsWithAbortError(pending)
    held.release()
    await asyncio.sleep(0.15)
    assert lockSpy.calls == 1
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "new-value"}


async def test_serializes_in_memory_mutations_across_providers():
    storage = AuthStorage.inMemory()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def anthropic(_current):
        started.set()
        await blocked.wait()
        return ApiKeyCredential(key="anthropic-key")

    first = startTask(storage.modify("anthropic", anthropic))
    await started.wait()

    async def openai(_current):
        return ApiKeyCredential(key="openai-key")

    secondMutation = _Spy(openai)
    second = startTask(storage.modify("openai", secondMutation))
    await asyncio.sleep(0)
    assert secondMutation.calls == 0

    blocked.set()
    await asyncio.gather(first, second)
    assert _plain(await storage.read("anthropic")) == {"type": "api_key", "key": "anthropic-key"}
    assert _plain(await storage.read("openai")) == {"type": "api_key", "key": "openai-key"}


async def test_cancels_a_queued_in_memory_mutation_without_running_it_later():
    storage = AuthStorage.inMemory()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def anthropic(_current):
        started.set()
        await blocked.wait()
        return ApiKeyCredential(key="anthropic-key")

    first = startTask(storage.modify("anthropic", anthropic))
    await started.wait()
    controller = AbortController()

    async def openai(_current):
        return ApiKeyCredential(key="openai-key")

    secondMutation = _Spy(openai)
    second = startTask(storage.modify("openai", secondMutation, _options(controller)))

    controller.abort()
    await _rejectsWithAbortError(second)
    assert secondMutation.calls == 0
    blocked.set()
    await first
    await asyncio.sleep(0)
    assert secondMutation.calls == 0
    assert await storage.read("openai") is None


async def test_preserves_the_stored_credential_after_cancelling_an_active_refresh_mutation():
    previous = {"type": "oauth", "access": "expired", "refresh": "refresh-token", "expires": 0}
    storage = AuthStorage.inMemory({"oauth": previous})
    controller = AbortController()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def refresh(current):
        started.set()
        await blocked.wait()
        return current.model_copy(update={"access": "refreshed", "expires": _now_ms() + 60_000})

    pending = startTask(storage.modify("oauth", refresh, _options(controller)))

    await started.wait()
    controller.abort()
    await _rejectsWithAbortError(pending)

    async def other(_current):
        return ApiKeyCredential(key="other")

    competingMutation = _Spy(other)
    competing = startTask(storage.modify("other", competingMutation))
    await asyncio.sleep(0)
    assert competingMutation.calls == 0

    blocked.set()
    await competing
    assert competingMutation.calls == 1
    assert _plain(await storage.read("oauth")) == previous


async def test_translates_a_credential_store_refresh_failure_and_allows_a_later_retry():
    providerId = "oauth-provider"
    base = AuthStorage.inMemory({
        providerId: {"type": "oauth", "access": "expired-access", "refresh": "refresh-token", "expires": 0},
    })
    failNextModify = True

    class _Credentials:
        async def read(self, id, options=None):
            return await base.read(id)

        async def list(self, options=None):
            return await base.list()

        async def modify(self, id, fn, options=None):
            nonlocal failNextModify
            if failNextModify:
                failNextModify = False
                raise RuntimeError("credential store unavailable")
            return await base.modify(id, fn)

        async def delete(self, id, options=None):
            await base.delete(id)

    async def login(_interaction):
        raise RuntimeError("not used")

    async def refresh(credential, _signal=None):
        return credential.model_copy(update={"access": "refreshed-access", "expires": _now_ms() + 60_000})

    async def toAuth(credential):
        return ModelAuth(apiKey=credential.access)

    def unused(*_args, **_kwargs):
        raise RuntimeError("not used")

    provider = SimpleNamespace(
        id=providerId,
        name="OAuth Provider",
        auth=ProviderAuth(oauth=OAuthAuth(name="OAuth", login=login, refresh=refresh, toAuth=toAuth)),
        getModels=list,
        stream=unused,
        streamSimple=unused,
    )
    models = createModels(CreateModelsOptions(credentials=_Credentials()))
    models.setProvider(provider)

    with pytest.raises(ModelsError) as caught:
        await models.getAuth(providerId)
    assert caught.value.code == "auth"
    assert (await models.getAuth(providerId)).auth.apiKey == "refreshed-access"


async def test_does_not_overwrite_malformed_auth_files(authJsonPath):
    writeAuthJson(authJsonPath, {"anthropic": {"type": "api_key", "key": "stored"}})
    storage = AuthStorage.create(str(authJsonPath))
    authJsonPath.write_text("{invalid-json", encoding="utf-8")

    async def new(_current):
        return ApiKeyCredential(key="new")

    with pytest.raises(Exception):  # noqa: B017 - pi's bare `rejects.toThrow()`
        await storage.modify("openai", new)
    assert authJsonPath.read_text(encoding="utf-8") == "{invalid-json"
