"""Translated from pi's ``packages/coding-agent/src/core/auth-storage.ts`` (v1.3.0).

CredentialStore implementation backed by auth.json. Provider auth orchestration belongs to ModelRuntime and pi-ai Models.

MISAKA forks:
- The default store is the home's (``config.get_auth_path``), a role's own when the session runs as one;
  ``oauth_account_key`` names a second account of one provider.
- The file backend follows a symlinked auth.json, refuses one that is not a regular file the user owns or that is
  empty, checks the file it opened is the file it read, and writes atomically; a compromised lock is detected by the
  lock file's identity, as proper-lockfile's ``onCompromised`` does.
- Stored records are checked by the credential models: a record they cannot read is no credential.
"""

from __future__ import annotations

import asyncio
import copy
import math
import os
import random
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from pydantic import ValidationError

from misaka.ai.auth.types import (
    ApiKeyCredential as RuntimeApiKeyCredential,
)
from misaka.ai.auth.types import (
    AuthOperationOptions,
    Credential,
    CredentialInfo,
)
from misaka.ai.auth.types import (
    OAuthCredential as RuntimeOAuthCredential,
)
from misaka.ai.utils.sleep import sleep as abortable_sleep
from misaka.config import get_auth_path
from misaka.core.resolve_config_value import isCommandConfigValue, resolveConfigValue
from misaka.utils import atomic
from misaka.utils.abort import raceWithAbortSignal
from misaka.utils.js import (
    AbortController,
    addAbortListener,
    isNumber,
    jsonParse,
    jsonStringify,
    startTask,
    throwIfAborted,
)
from misaka.utils.paths import (
    file_revision_from_stat,
    getFileRevision,
    normalizePath,
)
from misaka.utils.text import stripBom

type ApiKeyCredential = dict[str, Any]
type OAuthCredential = dict[str, Any]
type AuthCredential = ApiKeyCredential | OAuthCredential
type AuthStorageData = dict[str, AuthCredential]
type FileRevision = tuple[int, int, int, int, int]


def _file_revision_from_stat(value: os.stat_result) -> FileRevision:
    # The same revision getFileRevision reads by path, or the two never match on Windows.
    return file_revision_from_stat(value)


@dataclass(slots=True)
class LockResult:
    result: Any
    next: str | None = None


@dataclass(slots=True)
class _AuthFileReload:
    controller: AbortController
    promise: asyncio.Task[AuthStorageData]
    # MISAKA fork: an asyncio task belongs to the event loop that created it, so a
    # reader on another loop starts its own reload instead of joining this one.
    loop: asyncio.AbstractEventLoop
    readers: int = 0


@dataclass(slots=True)
class _AuthFileReadState:
    data: AuthStorageData = field(default_factory=dict)
    revision: FileRevision | None = None
    reload: _AuthFileReload | None = None


@dataclass(slots=True)
class _SharedAuthFileReadState:
    authPath: str
    readState: _AuthFileReadState


_sharedAuthFileReadState: _SharedAuthFileReadState | None = None


class AuthStorageBackend:
    def withLock(self, fn: Callable[[str | None], LockResult]) -> Any:  # pragma: no cover - protocol-like
        raise NotImplementedError

    async def withLockAsync(
        self,
        fn: Callable[[str | None], Awaitable[LockResult]],
        options: AuthOperationOptions | None = None,
    ) -> Any:  # pragma: no cover
        raise NotImplementedError


class FileAuthStorageBackend(AuthStorageBackend):
    def __init__(self, authPath: str | None = None):
        self.authPath = normalizePath(authPath or get_auth_path())

    def _lock_path(self) -> str:
        return f"{self.authPath}.lock"

    def ensureParentDir(self) -> None:
        parent_dir = Path(self.authPath).parent
        if parent_dir.exists():
            return
        parent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _target(self) -> str:
        """The file the credentials live in. ``auth.json`` may be a symlink (a dotfiles repo),
        followed once here; every open and write then works on the file it names, so a write
        replaces that file and leaves the link in place. A second hard link is allowed too, but
        the atomic replace gives the target a new inode, which detaches the other name."""
        return os.path.realpath(self.authPath)

    @staticmethod
    def _validate_file_stat(value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise RuntimeError("Auth storage must be a regular file")
        if hasattr(os, "getuid") and value.st_uid != os.getuid():
            raise RuntimeError("Auth storage must be owned by the current user")
        if value.st_size == 0:
            raise RuntimeError("Auth storage is empty; restore or remove it before retrying")

    def _open_existing(self) -> int:
        target = self._target()
        before = os.lstat(target)
        self._validate_file_stat(before)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
        try:
            opened = os.fstat(fd)
            self._validate_file_stat(opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError("Auth storage changed while it was being opened")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def ensureFileExists(self) -> None:
        # The mode applies only on creation so administrator-managed modes and ACLs remain intact.
        try:
            os.close(self._open_existing())
            return
        except FileNotFoundError:
            pass

        target = self._target()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:                            # another creator won; trust it only after validation
            os.close(self._open_existing())
            return

        created = None
        try:
            created = os.fstat(fd)
            if os.name != "nt" and hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            payload = memoryview(b"{}")
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError("Could not initialize auth storage")
                payload = payload[written:]
            os.fsync(fd)
            os.close(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                current = os.lstat(target)
                if created is not None and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(target)
            except OSError:
                pass
            raise

    def _read_file(self) -> tuple[str, FileRevision]:
        fd = self._open_existing()
        before = _file_revision_from_stat(os.fstat(fd))
        with os.fdopen(fd, encoding="utf-8-sig") as handle:
            content = handle.read()
            after_stat = os.fstat(handle.fileno())
            self._validate_file_stat(after_stat)
            after = _file_revision_from_stat(after_stat)
            if after != before:
                raise RuntimeError("Auth storage changed while it was being read")
            return content, after

    def _create_lock(self) -> FileLock:
        return FileLock(self._lock_path(), timeout=0)

    def _lock_signature(self) -> tuple[int, int] | None:
        try:
            stat_result = os.stat(self._lock_path())
        except OSError:
            return None
        return (stat_result.st_dev, stat_result.st_ino)

    def _assert_lock_uncompromised(self, expected_signature: tuple[int, int] | None) -> None:
        if self._lock_signature() != expected_signature:
            raise RuntimeError("Auth storage lock was compromised")

    def acquireLockSyncWithRetry(self) -> FileLock:
        # MISAKA fork: pi 1.2 also retries EPERM on Windows (`getRetryableLockErrorCode`, capped by
        # `MAX_EPERM_RETRIES` in the async loop): a lock directory pending deletion fails mkdir until its
        # last handle closes. filelock's Windows lock already reports a name pending deletion or a sharing
        # conflict as "not acquired", which is the `Timeout` retried here, and fails fast on a real access
        # denial, so there is no EPERM case to retry.
        maxAttempts = 10
        delay_ms = 20
        attempt = 1
        while True:
            lock = self._create_lock()
            try:
                lock.acquire(timeout=0)
                return lock
            except Timeout:
                if attempt == maxAttempts:
                    raise
                time.sleep(delay_ms / 1000)
            attempt += 1

    def withLock(self, fn: Callable[[str | None], LockResult]) -> Any:
        self.ensureParentDir()
        lock = self.acquireLockSyncWithRetry()
        try:
            self.ensureFileExists()
            current, _revision = self._read_file()
            outcome = fn(current)
            if outcome.next is not None:
                atomic.write_text(self._target(), outcome.next)
            return outcome.result
        finally:
            lock.release()

    async def acquireLockAsync(
        self, options: AuthOperationOptions | None = None
    ) -> FileLock:
        """``acquireLockAsync``: retried with backoff until 30 s have passed, as long as another process holds it."""
        staleMs = 30_000
        maxDelayMs = 2_000
        deadline = time.monotonic() * 1000 + staleMs
        retry = 0
        while True:
            _throw_if_aborted(options)
            lock = self._create_lock()
            try:
                lock.acquire(timeout=0)
            except Timeout:
                _throw_if_aborted(options)
                remainingMs = deadline - time.monotonic() * 1000
                if remainingMs <= 0:
                    raise
                baseDelayMs = min(10 * 2**retry, maxDelayMs / 2)
                retry += 1
                delayMs = min(round(baseDelayMs * (1 + random.random())), remainingMs)
                await abortable_sleep(delayMs, options.signal if options is not None else None)
                continue
            try:
                _throw_if_aborted(options)
            except BaseException:
                lock.release()
                raise
            return lock

    async def withLockAsync(
        self,
        fn: Callable[[str | None], Awaitable[LockResult]],
        options: AuthOperationOptions | None = None,
    ) -> Any:
        _throw_if_aborted(options)
        self.ensureParentDir()
        lock = await self.acquireLockAsync(options)
        expected_signature = self._lock_signature()
        try:
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            self.ensureFileExists()
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            current, _revision = self._read_file()
            _throw_if_aborted(options)
            outcome = await fn(current)
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            if outcome.next is not None:
                atomic.write_text(self._target(), outcome.next)
            self._assert_lock_uncompromised(expected_signature)
            return outcome.result
        finally:
            try:
                lock.release()
            except Exception:  # noqa: BLE001, S110 - releasing an already-released lock is fine
                pass



class ReadOnlyAuthStorage:
    """A CredentialStore that reads auth.json once and never writes it (``ReadOnlyAuthStorage``)."""

    def __init__(self, authPath: str | None = None) -> None:
        self.authPath = FileAuthStorageBackend(authPath).authPath
        self.data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self.data is not None:
            return self.data
        try:
            with open(self.authPath, encoding="utf-8", errors="replace") as file:
                parsed = jsonParse(stripBom(file.read()))
        except FileNotFoundError:
            self.data = {}
            return self.data
        except Exception as error:
            raise RuntimeError(f"Failed to read auth.json: {error}") from error
        if not isinstance(parsed, dict):
            raise RuntimeError("Invalid auth.json: expected an object")  # noqa: TRY004 - upstream throws Error
        for providerId, credential in parsed.items():
            if not isinstance(credential, dict):
                raise RuntimeError(f'Invalid auth.json credential for provider "{providerId}"')  # noqa: TRY004
            if credential.get("type") == "api_key":
                env = credential.get("env")
                validKey = "key" not in credential or isinstance(credential["key"], str)
                validEnv = "env" not in credential or (
                    isinstance(env, dict) and all(isinstance(entry, str) for entry in env.values())
                )
                if validKey and validEnv:
                    continue
            elif (
                credential.get("type") == "oauth"
                and isinstance(credential.get("access"), str)
                and isinstance(credential.get("refresh"), str)
                and isNumber(credential.get("expires"))
                and math.isfinite(credential["expires"])
            ):
                continue
            raise RuntimeError(f'Invalid auth.json credential for provider "{providerId}"')
        self.data = parsed
        return self.data

    async def read(self, providerId: str, options: AuthOperationOptions | None = None) -> Credential | None:
        _throw_if_aborted(options)
        credential = self.load().get(providerId)
        _throw_if_aborted(options)
        if not credential:
            return None
        # A command key is not run: a read-only store answers what is stored.
        key = credential.get("key")
        resolve = credential.get("type") == "api_key" and bool(key) and not isCommandConfigValue(key)
        return _to_runtime_credential(copy.deepcopy(credential), resolve_api_key=resolve)

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        _throw_if_aborted(options)
        credentials = [
            CredentialInfo(providerId=providerId, type=credential["type"]) for providerId, credential in self.load().items()
        ]
        _throw_if_aborted(options)
        return credentials

    async def modify(
        self,
        _providerId: str,
        _fn: Callable[[Credential | None], Awaitable[Credential | None]],
        _options: AuthOperationOptions | None = None,
    ) -> Credential | None:
        raise RuntimeError("Read-only credential storage cannot modify auth.json")

    async def delete(self, _providerId: str, _options: AuthOperationOptions | None = None) -> None:
        raise RuntimeError("Read-only credential storage cannot modify auth.json")


class InMemoryAuthStorageBackend(AuthStorageBackend):
    def __init__(self) -> None:
        self.value: str | None = None
        self._asyncChain: asyncio.Future[Any] | None = None

    def withLock(self, fn: Callable[[str | None], LockResult]) -> Any:
        outcome = fn(self.value)
        if outcome.next is not None:
            self.value = outcome.next
        return outcome.result

    async def withLockAsync(
        self,
        fn: Callable[[str | None], Awaitable[LockResult]],
        options: AuthOperationOptions | None = None,
    ) -> Any:
        previous = self._asyncChain

        async def operation() -> Any:
            # `await previous.catch(() => {})`: the chain moves on whether the previous operation failed or not.
            if previous is not None and not previous.done():
                await asyncio.wait([previous])
            _throw_if_aborted(options)
            outcome = await fn(self.value)
            _throw_if_aborted(options)
            if outcome.next is not None:
                self.value = outcome.next
            return outcome.result

        task = startTask(operation())
        self._asyncChain = task
        _observe(task)
        return await raceWithAbortSignal(task, options.signal if options is not None else None)


def _observe(task: asyncio.Future[Any]) -> None:
    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)

async def _with_lock_async(
    backend: AuthStorageBackend,
    fn: Callable[[str | None], Awaitable[LockResult]],
    options: AuthOperationOptions | None,
) -> Any:
    if options is None:
        return await backend.withLockAsync(fn)
    return await backend.withLockAsync(fn, options)


def _coerce_storage_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(index): item for index, item in enumerate(value)}
    if isinstance(value, str):
        return {str(index): item for index, item in enumerate(value)}
    return {}


def oauth_account_key(provider: str, account: str | None = None) -> str:
    """Named grants share the existing atomic auth store and its refresh lock."""
    if account is None:
        return provider
    if not account or len(account) > 64 or any(not (c.isascii() and (c.isalnum() or c in "-_.")) for c in account):
        raise ValueError("OAuth account names take 1–64 ASCII letters, digits, dots, underscores or hyphens")
    return f"{provider}:{account}"


class AuthStorage:
    """Credential storage backed by a JSON file."""

    def __init__(self, storage: AuthStorageBackend, authPath: str | None = None):
        global _sharedAuthFileReadState
        self.storage = storage
        self.authPath = authPath
        self.readState = (
            _sharedAuthFileReadState.readState
            if authPath and _sharedAuthFileReadState is not None and _sharedAuthFileReadState.authPath == authPath
            else _AuthFileReadState()
        )
        if authPath and _sharedAuthFileReadState is None:
            _sharedAuthFileReadState = _SharedAuthFileReadState(authPath=authPath, readState=self.readState)
        if authPath:
            revision = getFileRevision(authPath)
            if revision is not None and revision == self.readState.revision:
                return
        self.reload()

    @classmethod
    def create(cls, authPath: str | None = None) -> AuthStorage:
        storage = FileAuthStorageBackend(authPath)
        return cls(storage, storage.authPath)

    @classmethod
    def fromStorage(cls, storage: AuthStorageBackend) -> AuthStorage:
        return cls(storage)

    @classmethod
    def inMemory(cls, data: AuthStorageData | None = None) -> AuthStorage:
        storage = InMemoryAuthStorageBackend()
        storage.withLock(lambda _current: LockResult(result=None, next=jsonStringify(data or {}, 2)))
        return cls.fromStorage(storage)

    def parseStorageData(self, content: str | None) -> AuthStorageData:
        if not content:
            return {}
        return jsonParse(stripBom(content))

    def updateReadState(self, data: AuthStorageData, revision: FileRevision | None = None) -> None:
        self.readState.data = data
        self.readState.revision = revision

    def reload(self) -> None:
        """Reload credentials from storage."""
        content: str | None = None
        revision: FileRevision | None = None

        def capture(current: str | None) -> LockResult:
            nonlocal content, revision
            content = current
            revision = getFileRevision(self.authPath) if self.authPath else None
            return LockResult(result=None)

        try:
            self.storage.withLock(capture)
            self.updateReadState(self.parseStorageData(content), revision)
        except Exception:  # noqa: BLE001, S110
            # Preserve the last valid in-memory snapshot.
            pass

    async def reloadFromStorageAsync(self, options: AuthOperationOptions | None = None) -> AuthStorageData:
        async def reload(content: str | None) -> LockResult:
            currentData = self.parseStorageData(content)
            revision = getFileRevision(self.authPath) if self.authPath else None
            self.updateReadState(currentData, revision)
            return LockResult(result=currentData)

        return await _with_lock_async(self.storage, reload, options)

    async def readLatestData(self, options: AuthOperationOptions | None = None) -> AuthStorageData:
        _throw_if_aborted(options)
        signal = options.signal if options is not None else None
        if not self.authPath:
            if signal is not None:
                return await self.reloadFromStorageAsync(options)
            try:
                return await self.reloadFromStorageAsync(options)
            except Exception:  # noqa: BLE001
                return self.readState.data
        revision = getFileRevision(self.authPath)
        if revision is not None and revision == self.readState.revision:
            return self.readState.data
        loop = asyncio.get_running_loop()
        if self.readState.reload is None or self.readState.reload.loop is not loop:
            controller = AbortController()
            started = _AuthFileReload(
                controller=controller,
                promise=loop.create_task(self.reloadFromStorageAsync(AuthOperationOptions(signal=controller.signal))),
                loop=loop,
            )
            self.readState.reload = started

            def settle(task: asyncio.Task[AuthStorageData]) -> None:
                if self.readState.reload is started:
                    self.readState.reload = None
                if not task.cancelled():
                    task.exception()

            started.promise.add_done_callback(settle)

        reload = self.readState.reload
        reload.readers += 1
        left = False

        def leave() -> None:
            nonlocal left
            if left:
                return
            left = True
            reload.readers -= 1
            if reload.readers == 0 and self.readState.reload is reload:
                self.readState.reload = None
                reload.controller.abort()

        # MISAKA fork: pi's reader leaves in the `finally` of its await, which on an abort runs as a microtask, before
        # any timer -- so the last reader stops the reload before its lock retry can fire again. asyncio has no
        # microtasks; the reader leaves the moment its signal aborts instead.
        removeAbortListener = addAbortListener(signal, leave) if signal is not None else None
        try:
            result = raceWithAbortSignal(asyncio.shield(reload.promise), signal)
            if signal is not None:
                return await result
            try:
                return await result
            except Exception:  # noqa: BLE001
                return self.readState.data
        finally:
            if removeAbortListener is not None:
                removeAbortListener()
            leave()

    # CredentialStore.

    async def read(self, provider: str, options: AuthOperationOptions | None = None) -> Credential | None:
        credential = _to_runtime_credential(_coerce_storage_object(await self.readLatestData(options)).get(provider))
        _throw_if_aborted(options)
        return credential

    async def modify(
        self,
        provider: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
        options: AuthOperationOptions | None = None,
    ) -> Credential | None:
        latestData = self.readState.data
        revision: FileRevision | None = None

        async def update(content: str | None) -> LockResult:
            nonlocal latestData, revision
            currentData = _coerce_storage_object(self.parseStorageData(content))
            next = await fn(_to_runtime_credential(currentData.get(provider), resolve_api_key=False))
            if next is None:
                latestData = currentData
                revision = getFileRevision(self.authPath) if self.authPath else None
                return LockResult(result=_to_runtime_credential(currentData.get(provider), resolve_api_key=False))

            merged = {**currentData, provider: _storedCredential(next)}
            latestData = merged
            return LockResult(result=next, next=jsonStringify(merged, 2))

        result = await _with_lock_async(self.storage, update, options)
        self.updateReadState(latestData, revision)
        return result

    async def delete(self, provider: str, options: AuthOperationOptions | None = None) -> None:
        latestData = self.readState.data

        async def remove(content: str | None) -> LockResult:
            nonlocal latestData
            currentData = _coerce_storage_object(self.parseStorageData(content))
            currentData.pop(provider, None)
            latestData = currentData
            return LockResult(result=None, next=jsonStringify(currentData, 2))

        await _with_lock_async(self.storage, remove, options)
        self.updateReadState(latestData)

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        """List credential metadata without resolving configured key values."""
        # pi lists every record by its type, readable or not, so it can still be logged out.
        # MISAKA fork: CredentialInfo carries the two credential types only.
        entries = [
            CredentialInfo(providerId=providerId, type=value["type"])
            for providerId, value in _coerce_storage_object(await self.readLatestData(options)).items()
            if isinstance(value, dict) and value.get("type") in ("api_key", "oauth")
        ]
        _throw_if_aborted(options)
        return entries


def _throw_if_aborted(options: AuthOperationOptions | None) -> None:
    if options is not None:
        throwIfAborted(options.signal)
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _storedCredential(credential: Credential) -> AuthCredential:
    """The record ``JSON.stringify`` writes for a credential: the fields it was given, none it was left without."""
    return {"type": credential.type, **credential.model_dump(exclude_unset=True)}

def _to_runtime_credential(
    value: Any, *, resolve_api_key: bool = True
) -> Credential | None:
    if not isinstance(value, dict):
        return None
    # MISAKA fork: pi hands the stored object on as it is and its readers take the fields
    # they know. The credential models here check them, so an api_key record keeps only the
    # fields they declare, and a record they still cannot read is no credential -- not an
    # error that stops every provider's availability, or the login that would replace it.
    try:
        if value.get("type") == "api_key":
            credential = RuntimeApiKeyCredential.model_validate(
                {key: item for key, item in value.items() if key in RuntimeApiKeyCredential.model_fields}
            )
            if credential.key is None or not resolve_api_key:
                return credential
            # pi auth-storage.ts `read`: an unresolvable key reads as no key, so the provider
            # reports unconfigured instead of failing every availability check.
            return credential.model_copy(update={"key": resolveConfigValue(credential.key, credential.env)})
        if value.get("type") == "oauth":
            return RuntimeOAuthCredential.model_validate(value)
    except ValidationError:
        return None
    return None


def readStoredCredential(providerId: str, authPath: str | None = None) -> AuthCredential | None:
    """One-off synchronous read of a stored credential from an auth.json file, without instantiating a store or
    resolving configured key values."""
    try:
        with open(FileAuthStorageBackend(authPath).authPath, encoding="utf-8", errors="replace") as file:
            data = jsonParse(stripBom(file.read()))
        return data.get(providerId) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - upstream `catch { return undefined; }`
        return None


__all__ = [
    "ApiKeyCredential",
    "AuthCredential",
    "AuthStorage",
    "AuthStorageBackend",
    "AuthStorageData",
    "FileAuthStorageBackend",
    "InMemoryAuthStorageBackend",
    "OAuthCredential",
    "ReadOnlyAuthStorage",
    "readStoredCredential",
]
