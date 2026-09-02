"""Credential persistence and resolution for coding-agent providers."""

from __future__ import annotations

import asyncio
import json
import os
import random
import stat
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout

from misaka.ai.auth.types import (
    ApiKeyCredential as RuntimeApiKeyCredential,
)
from misaka.ai.auth.types import (
    AuthOperationOptions,
    CredentialInfo,
    CredentialValue,
)
from misaka.ai.auth.types import (
    OAuthCredential as RuntimeOAuthCredential,
)
from misaka.ai.env_api_keys import find_env_keys, get_env_api_key
from misaka.ai.utils.abort import race_with_abort_signal
from misaka.ai.utils.abort import sleep as abortable_sleep
from misaka.ai.utils.oauth import (
    OAuthCredentials,
    getOAuthApiKey,
    getOAuthProvider,
    getOAuthProviders,
    oauthCredentialsExpireSoon,
)
from misaka.config import get_auth_path
from misaka.core.resolve_config_value import resolveConfigValue
from misaka.utils import atomic
from misaka.utils.paths import get_file_revision, normalize_path
from misaka.utils.values import signal_aborted

type ApiKeyCredential = dict[str, Any]
type OAuthCredential = dict[str, Any]
type AuthCredential = ApiKeyCredential | OAuthCredential
type AuthStorageData = dict[str, AuthCredential]
type FileRevision = tuple[int, int, int, int, int]
type AuthStatusSource = Literal[
    "stored",
    "runtime",
    "environment",
    "fallback",
    "models_json_key",
    "models_json_command",
]


def _file_revision_from_stat(value: os.stat_result) -> FileRevision:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass(slots=True)
class AuthStatus:
    configured: bool
    source: AuthStatusSource | None = None
    label: str | None = None


@dataclass(slots=True)
class LockResult:
    result: Any
    next: str | None = None
    publish: Callable[[], None] | None = None


@dataclass(slots=True)
class _AuthFileReload:
    task: asyncio.Task[tuple[AuthStorageData, FileRevision | None]]
    readers: int = 0


_auth_file_reloads: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, _AuthFileReload]
] = weakref.WeakKeyDictionary()
_auth_file_reloads_lock = threading.Lock()


def _once_callback(callback: Callable[[], None]) -> Callable[[], None]:
    lock = threading.Lock()
    called = False

    def call() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        callback()

    return call


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
        self.authPath = normalize_path(authPath or get_auth_path())
        self._current_revision: FileRevision | None = None

    def _lock_path(self) -> str:
        return f"{self.authPath}.lock"

    def ensureParentDir(self) -> None:
        parent_dir = Path(self.authPath).parent
        if parent_dir.exists():
            return
        parent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _validate_file_stat(value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise RuntimeError("Auth storage must be a regular file")
        if value.st_nlink != 1:
            raise RuntimeError("Auth storage must not be hard-linked")
        if value.st_size == 0:
            raise RuntimeError("Auth storage is empty; restore or remove it before retrying")

    def _open_existing(self) -> int:
        before = os.lstat(self.authPath)
        self._validate_file_stat(before)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.authPath, flags)
        try:
            opened = os.fstat(fd)
            self._validate_file_stat(opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError("Auth storage changed while it was being opened")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _restrict_file_mode(self, fd: int) -> None:
        if os.fstat(fd).st_mode & 0o077 and hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)

    def ensureFileExists(self) -> None:
        try:
            fd = self._open_existing()
        except FileNotFoundError:
            fd = None
        else:
            try:
                self._restrict_file_mode(fd)               # stores from before 0600 was enforced
            finally:
                os.close(fd)
            return

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.authPath, flags, 0o600)
        except FileExistsError:                            # another creator won; trust it only after validation
            fd = self._open_existing()
            try:
                self._restrict_file_mode(fd)
            finally:
                os.close(fd)
            return

        created = None
        try:
            created = os.fstat(fd)
            if hasattr(os, "fchmod"):
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
                current = os.lstat(self.authPath)
                if created is not None and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(self.authPath)
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

    def _acquire_lock_sync_with_retry(self) -> FileLock:
        max_attempts = 10
        delay_ms = 20
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            lock = self._create_lock()
            try:
                lock.acquire(timeout=0)
                return lock
            except Timeout as error:
                last_error = error
                if attempt == max_attempts:
                    raise
                time.sleep(delay_ms / 1000)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to acquire auth storage lock")

    async def _acquire_lock_async_with_retry(
        self, options: AuthOperationOptions | None = None
    ) -> FileLock:
        retries = 10
        factor = 2
        min_timeout = 0.1
        max_timeout = 10.0

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            _throw_if_aborted(options)
            lock = self._create_lock()
            try:
                lock.acquire(timeout=0)
                try:
                    _throw_if_aborted(options)
                except BaseException:
                    lock.release()
                    raise
                return lock
            except Exception as error:
                _throw_if_aborted(options)
                if not isinstance(error, Timeout):
                    raise
                last_error = error
                if attempt == retries:
                    raise
                delay = min(min_timeout * (factor**attempt), max_timeout)
                await abortable_sleep(
                    delay * (1 + random.random()) * 1000,
                    options.signal if options is not None else None,
                )

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to acquire auth storage lock")

    def withLock(self, fn: Callable[[str | None], LockResult]) -> Any:
        self.ensureParentDir()
        lock = self._acquire_lock_sync_with_retry()
        try:
            self.ensureFileExists()
            current, self._current_revision = self._read_file()
            outcome = fn(current)
            if outcome.next is not None:
                atomic.write_text(self.authPath, outcome.next, mode=0o600)
            if outcome.publish is not None:
                outcome.publish()
            return outcome.result
        finally:
            self._current_revision = None
            lock.release()

    async def withLockAsync(
        self,
        fn: Callable[[str | None], Awaitable[LockResult]],
        options: AuthOperationOptions | None = None,
    ) -> Any:
        _throw_if_aborted(options)
        self.ensureParentDir()
        lock = await self._acquire_lock_async_with_retry(options)
        expected_signature = self._lock_signature()
        try:
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            self.ensureFileExists()
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            current, self._current_revision = self._read_file()
            _throw_if_aborted(options)
            outcome = await fn(current)
            self._assert_lock_uncompromised(expected_signature)
            _throw_if_aborted(options)
            if outcome.next is not None:
                atomic.write_text(self.authPath, outcome.next, mode=0o600)
            self._assert_lock_uncompromised(expected_signature)
            if outcome.publish is not None:
                outcome.publish()
            return outcome.result
        finally:
            self._current_revision = None
            try:
                lock.release()
            except Exception:  # noqa: BLE001, S110 - releasing an already-released lock is fine
                pass


class InMemoryAuthStorageBackend(AuthStorageBackend):
    def __init__(self) -> None:
        self.value: str | None = None

    def withLock(self, fn: Callable[[str | None], LockResult]) -> Any:
        outcome = fn(self.value)
        if outcome.next is not None:
            self.value = outcome.next
        if outcome.publish is not None:
            outcome.publish()
        return outcome.result

    async def withLockAsync(
        self,
        fn: Callable[[str | None], Awaitable[LockResult]],
        options: AuthOperationOptions | None = None,
    ) -> Any:
        async def operation() -> Any:
            _throw_if_aborted(options)
            outcome = await fn(self.value)
            _throw_if_aborted(options)
            if outcome.next is not None:
                self.value = outcome.next
            if outcome.publish is not None:
                outcome.publish()
            return outcome.result

        return await race_with_abort_signal(
            operation(), options.signal if options is not None else None
        )


async def _with_lock_async(
    backend: AuthStorageBackend,
    fn: Callable[[str | None], Awaitable[LockResult]],
    options: AuthOperationOptions | None,
) -> Any:
    if options is None:
        return await backend.withLockAsync(fn)
    return await backend.withLockAsync(fn, options)


def _coerce_oauth_credentials(value: dict[str, Any]) -> OAuthCredentials:
    return OAuthCredentials.model_validate({key: item for key, item in value.items() if key != "type"})


def _coerce_storage_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(index): item for index, item in enumerate(value)}
    if isinstance(value, str):
        return {str(index): item for index, item in enumerate(value)}
    return {}


class AuthStorage:
    def __init__(self, storage: AuthStorageBackend):
        self.data: Any = {}
        self.runtimeOverrides: dict[str, str] = {}
        self.fallbackResolver: Callable[[str], str | None] | None = None
        self.loadError: Exception | None = None
        self.storage = storage
        self._auth_path = storage.authPath if isinstance(storage, FileAuthStorageBackend) else None
        self._file_revision: FileRevision | None = None
        self._read_state_lock = threading.Lock()
        self._read_state_version = 0
        self._read_attempt_generation = 0
        self._read_applied_generation = 0
        self.reload()

    @classmethod
    def create(cls, authPath: str | None = None) -> AuthStorage:
        return cls(FileAuthStorageBackend(authPath))

    @classmethod
    def fromStorage(cls, storage: AuthStorageBackend) -> AuthStorage:
        return cls(storage)

    @classmethod
    def inMemory(cls, data: AuthStorageData | None = None) -> AuthStorage:
        storage = InMemoryAuthStorageBackend()
        storage.withLock(lambda _current: LockResult(result=None, next=json.dumps(data or {}, indent=2)))
        return cls.fromStorage(storage)

    def setRuntimeApiKey(self, provider: str, apiKey: str) -> None:
        self.runtimeOverrides[provider] = apiKey

    def removeRuntimeApiKey(self, provider: str) -> None:
        self.runtimeOverrides.pop(provider, None)

    def setFallbackResolver(self, resolver: Callable[[str], str | None]) -> None:
        self.fallbackResolver = resolver

    def _parse_storage_data(self, content: str | None) -> AuthStorageData:
        if not content:
            return {}
        return json.loads(content.removeprefix("\ufeff"))

    def reload(self) -> None:
        content: str | None = None
        revision: FileRevision | None = None
        generation = (
            self._reserve_read_generation() if self._auth_path is not None else 0
        )

        def capture(current: str | None) -> LockResult:
            nonlocal content, revision
            content = current
            revision = (
                self.storage._current_revision
                if isinstance(self.storage, FileAuthStorageBackend)
                else None
            )
            return LockResult(result=None)

        try:
            self.storage.withLock(capture)
            data = self._parse_storage_data(content)
            if self._auth_path is None:
                self.data = data
                self.loadError = None
            else:
                self._publish_file_snapshot(data, revision, generation)
        except Exception as error:  # noqa: BLE001
            self._record_file_load_error(error, generation)

    def _reserve_read_generation(self) -> int:
        with self._read_state_lock:
            self._read_attempt_generation += 1
            return self._read_attempt_generation

    def _record_file_load_error(self, error: Exception, generation: int) -> bool:
        if self._auth_path is None:
            self.loadError = error
            return True
        with self._read_state_lock:
            if generation < self._read_applied_generation:
                return False
            self._read_applied_generation = generation
            if self.loadError is error and self._file_revision is None:
                return True
            self.loadError = error
            self._file_revision = None
            self._read_state_version += 1
            return True

    def _publish_file_snapshot(
        self,
        data: AuthStorageData,
        revision: FileRevision | None,
        generation: int,
    ) -> AuthStorageData:
        """Publish only a snapshot that still matches the path's current revision."""
        if self._auth_path is None:  # pragma: no cover - file reloads only
            self.data = data
            return data

        with self._read_state_lock:
            if generation < self._read_applied_generation:
                return self.data
            current_revision = get_file_revision(self._auth_path)
            self._read_applied_generation = generation
            if revision != current_revision:
                if current_revision is not None and self._file_revision == current_revision:
                    return self.data
                self._file_revision = None
                return data
            if (
                self.data is data
                and self._file_revision == revision
                and self.loadError is None
            ):
                return self.data
            self.data = data
            self._file_revision = revision
            self.loadError = None
            self._read_state_version += 1
            return data

    def _replace_cached_data(self, data: Any) -> None:
        with self._read_state_lock:
            self._read_attempt_generation += 1
            self._read_applied_generation = self._read_attempt_generation
            self._read_state_version += 1
            self.data = data
            self._file_revision = None
            self.loadError = None

    async def _reload_from_storage_async(
        self,
    ) -> tuple[AuthStorageData, FileRevision | None]:
        async def capture(current: str | None) -> LockResult:
            data = self._parse_storage_data(current)
            revision = (
                self.storage._current_revision
                if isinstance(self.storage, FileAuthStorageBackend)
                else None
            )
            return LockResult(result=(data, revision))

        return await self.storage.withLockAsync(capture)

    async def _join_file_reload(
        self, signal: Any = None
    ) -> tuple[AuthStorageData, FileRevision | None]:
        if self._auth_path is None:  # pragma: no cover - guarded by readLatestData
            return self.data, None

        loop = asyncio.get_running_loop()
        with _auth_file_reloads_lock:
            reloads = _auth_file_reloads.setdefault(loop, {})
            reload = reloads.get(self._auth_path)
            if reload is None:
                reload = _AuthFileReload(loop.create_task(self._reload_from_storage_async()))
                reload.task.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                reloads[self._auth_path] = reload
            reload.readers += 1

        cancel_reload = False
        try:
            return await race_with_abort_signal(asyncio.shield(reload.task), signal)
        finally:
            with _auth_file_reloads_lock:
                reload.readers -= 1
                reloads = _auth_file_reloads.get(loop)
                if reload.readers == 0 and reloads is not None and reloads.get(self._auth_path) is reload:
                    reloads.pop(self._auth_path, None)
                    if not reloads:
                        _auth_file_reloads.pop(loop, None)
                    cancel_reload = not reload.task.done()
            if cancel_reload:
                reload.task.cancel()

    async def readLatestData(
        self, options: AuthOperationOptions | None = None
    ) -> AuthStorageData:
        """Return the latest safe file snapshot without polling or watching in the background."""
        _throw_if_aborted(options)
        if self._auth_path is None:
            return self.data

        generation = self._reserve_read_generation()
        revision = get_file_revision(self._auth_path)
        if revision is not None:
            with self._read_state_lock:
                if revision == self._file_revision and revision == get_file_revision(
                    self._auth_path
                ):
                    self._read_applied_generation = max(
                        self._read_applied_generation, generation
                    )
                    return self.data

        try:
            data, revision = await self._join_file_reload(
                options.signal if options is not None else None
            )
        except Exception as error:  # A signal-bearing read reports storage errors.
            if options is not None and signal_aborted(options.signal):
                raise
            self._record_file_load_error(error, generation)
            if options is not None and options.signal is not None:
                raise
            return self.data

        data = self._publish_file_snapshot(data, revision, generation)
        _throw_if_aborted(options)
        return data

    def persistProviderChange(
        self, provider: str, credential: AuthCredential | None
    ) -> None:
        """One provider's change, written through the lock. A store that could not be read is never
        overwritten, and a failed write raises: the caller reports it instead of saying "saved"."""
        if self.loadError is not None:
            raise RuntimeError(f"the credential store could not be read ({self.loadError}); not overwriting it")

        publish: Callable[[], None] | None = None

        def persist(current: str | None) -> LockResult:
            nonlocal publish
            current_data = _coerce_storage_object(self._parse_storage_data(current))
            merged = dict(current_data)
            if credential is None:
                merged.pop(provider, None)
            else:
                merged[provider] = credential
            publish = _once_callback(lambda: self._replace_cached_data(merged))
            return LockResult(
                result=None,
                next=json.dumps(merged, indent=2),
                publish=publish,
            )

        self.storage.withLock(persist)
        if publish is not None:
            publish()  # Backends predating the locked publish hook still update the snapshot.

    def get(self, provider: str) -> AuthCredential | None:
        return _coerce_storage_object(self.data).get(provider)

    def set(self, provider: str, credential: AuthCredential) -> None:
        self.persistProviderChange(provider, credential)

    def remove(self, provider: str) -> None:
        self.persistProviderChange(provider, None)

    def list(self) -> list[str]:
        return list(_coerce_storage_object(self.data).keys())

    def has(self, provider: str) -> bool:
        return provider in _coerce_storage_object(self.data)

    def hasAuth(self, provider: str) -> bool:
        if provider in self.runtimeOverrides:
            return True
        if _coerce_storage_object(self.data).get(provider):
            return True
        if find_env_keys(provider) or get_env_api_key(provider):
            return True
        return bool(self.fallbackResolver and self.fallbackResolver(provider))

    def getAuthStatus(self, provider: str) -> AuthStatus:
        # pi model-runtime.ts:561-571: every branch that finds a usable credential reports
        # `configured: true`; only the no-credential fall-through is false. Reporting `False`
        # alongside a `source` contradicted `hasAuth` right above and made env-var-only setups
        # look unconfigured to extensions.
        # The runtime override is checked first, as pi does and as `getApiKey` below already
        # did: with both `--api-key` and a stored credential present the request uses the
        # runtime key, so reporting "stored" named the wrong source.
        if provider in self.runtimeOverrides:
            return AuthStatus(configured=True, source="runtime", label="--api-key")
        if _coerce_storage_object(self.data).get(provider):
            return AuthStatus(configured=True, source="stored")
        env_keys = find_env_keys(provider)
        if env_keys and env_keys[0]:
            return AuthStatus(configured=True, source="environment", label=env_keys[0])
        if self.fallbackResolver and self.fallbackResolver(provider):
            return AuthStatus(configured=True, source="fallback", label="custom provider config")
        return AuthStatus(configured=False)

    def getAll(self) -> AuthStorageData:
        return dict(_coerce_storage_object(self.data))

    async def login(self, providerId: str, callbacks: Any) -> None:
        provider = getOAuthProvider(providerId)
        if provider is None:
            raise RuntimeError(f"Unknown OAuth provider: {providerId}")
        credentials = await provider.login(callbacks)
        self.set(providerId, {"type": "oauth", **credentials.model_dump(exclude_none=False)})

    def logout(self, provider: str) -> None:
        self.remove(provider)

    @staticmethod
    def _oauthExpiresSoon(credentials: OAuthCredentials) -> bool:
        """Whether an OAuth token is inside pi's five-minute refresh window.

        pi auth/resolve.ts DEFAULT_OAUTH_MINIMUM_VALIDITY_MS: refreshing only once the
        token has already expired hands a token to a request that may outlive it, and
        every in-flight request then fails together. The optimistic check, the
        authoritative one under the lock and ``getOAuthApiKey`` itself all use this one
        window -- a narrower check anywhere in the chain would make the lock decide to
        refresh and the refresher decline to.
        """
        return oauthCredentialsExpireSoon(credentials)

    async def refreshOAuthTokenWithLock(
        self,
        providerId: str,
        options: AuthOperationOptions | None = None,
    ) -> dict[str, Any] | None:
        _throw_if_aborted(options)
        provider = getOAuthProvider(providerId)
        if provider is None:
            return None
        publish: Callable[[], None] | None = None

        async def refresh(current: str | None) -> LockResult:
            nonlocal publish
            current_data_raw = self._parse_storage_data(current)
            current_data = _coerce_storage_object(current_data_raw)
            credential = current_data.get(providerId)
            if not isinstance(credential, dict) or credential.get("type") != "oauth":
                publish = _once_callback(
                    lambda: self._replace_cached_data(current_data_raw)
                )
                return LockResult(
                    result=None,
                    publish=publish,
                )

            oauth_credential = _coerce_oauth_credentials(credential)
            if not self._oauthExpiresSoon(oauth_credential):
                publish = _once_callback(
                    lambda: self._replace_cached_data(current_data_raw)
                )
                return LockResult(
                    result={
                        "apiKey": provider.getApiKey(oauth_credential),
                        "newCredentials": oauth_credential,
                    },
                    publish=publish,
                )

            oauth_credentials: dict[str, OAuthCredentials] = {}
            for key, value in current_data.items():
                if isinstance(value, dict) and value.get("type") == "oauth":
                    oauth_credentials[key] = _coerce_oauth_credentials(value)

            refreshed = await getOAuthApiKey(
                providerId,
                oauth_credentials,
                options.signal if options is not None else None,
            )
            if refreshed is None:
                publish = _once_callback(
                    lambda: self._replace_cached_data(current_data_raw)
                )
                return LockResult(
                    result=None,
                    publish=publish,
                )

            merged = dict(current_data)
            merged[providerId] = {
                "type": "oauth",
                **refreshed["newCredentials"].model_dump(exclude_none=False),
            }
            publish = _once_callback(lambda: self._replace_cached_data(merged))
            return LockResult(
                result=refreshed,
                next=json.dumps(merged, indent=2),
                publish=publish,
            )

        result = await _with_lock_async(self.storage, refresh, options)
        if publish is not None:
            publish()
        return result

    async def getApiKey(self, providerId: str, options: dict[str, Any] | None = None) -> str | None:
        signal = options.get("signal") if options is not None else None
        operation_options = (
            AuthOperationOptions(signal=signal) if signal is not None else None
        )
        _throw_if_aborted(operation_options)
        runtime_key = self.runtimeOverrides.get(providerId)
        if runtime_key:
            return runtime_key

        credential = _coerce_storage_object(
            await self.readLatestData(operation_options)
        ).get(providerId)
        if isinstance(credential, dict) and credential.get("type") == "api_key":
            key = credential.get("key")
            return (
                resolveConfigValue(key, credential.get("env"))
                if isinstance(key, str)
                else None
            )

        if isinstance(credential, dict) and credential.get("type") == "oauth":
            provider = getOAuthProvider(providerId)
            if provider is None:
                return None

            oauth_credential = _coerce_oauth_credentials(credential)
            needs_refresh = self._oauthExpiresSoon(oauth_credential)
            if needs_refresh:
                try:
                    refreshed = await race_with_abort_signal(
                        self.refreshOAuthTokenWithLock(
                            providerId, operation_options
                        ),
                        signal,
                    )
                    if refreshed is not None:
                        return refreshed["apiKey"]
                except Exception:  # noqa: BLE001 - another process may have refreshed it meanwhile
                    _throw_if_aborted(operation_options)
                    self.reload()
                    updated = _coerce_storage_object(self.data).get(providerId)
                    if isinstance(updated, dict) and updated.get("type") == "oauth":
                        updated_credentials = _coerce_oauth_credentials(updated)
                        if not self._oauthExpiresSoon(updated_credentials):
                            return provider.getApiKey(updated_credentials)
                    return None
            return provider.getApiKey(oauth_credential)

        env_key = get_env_api_key(providerId)
        if env_key:
            return env_key

        include_fallback = True
        if options is not None and options.get("includeFallback") is False:
            include_fallback = False
        if include_fallback and self.fallbackResolver is not None:
            return self.fallbackResolver(providerId) or None
        return None

    def getOAuthProviders(self) -> list[Any]:
        return getOAuthProviders()


def _throw_if_aborted(options: AuthOperationOptions | None) -> None:
    if options is not None and signal_aborted(options.signal):
        raise RuntimeError("Request was aborted")
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _to_runtime_credential(
    value: Any, *, resolve_api_key: bool = True
) -> CredentialValue | None:
    if not isinstance(value, dict):
        return None
    if value.get("type") == "api_key":
        credential = RuntimeApiKeyCredential.model_validate(value)
        if credential.key is None or not resolve_api_key:
            return credential
        resolved = resolveConfigValue(credential.key, credential.env)
        if resolved is None:
            raise ValueError("Stored API key could not be resolved")
        return credential.model_copy(update={"key": resolved})
    if value.get("type") == "oauth":
        return RuntimeOAuthCredential.model_validate(value)
    return None


class AuthStorageCredentialStore:
    """Expose the legacy coding-agent store through pi-ai's CredentialStore contract."""

    def __init__(self, storage: AuthStorage) -> None:
        self._storage = storage

    async def read(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> CredentialValue | None:
        _throw_if_aborted(options)
        runtime_key = self._storage.runtimeOverrides.get(providerId)
        if runtime_key:
            return RuntimeApiKeyCredential(key=runtime_key)
        credential = _to_runtime_credential(
            _coerce_storage_object(await self._storage.readLatestData(options)).get(providerId)
        )
        _throw_if_aborted(options)
        return credential

    async def list(
        self, options: AuthOperationOptions | None = None
    ) -> list[CredentialInfo]:
        _throw_if_aborted(options)
        latest = await self._storage.readLatestData(options)
        entries = {
            providerId: CredentialInfo(providerId=providerId, type=credential.type)
            for providerId, value in _coerce_storage_object(latest).items()
            if (
                credential := _to_runtime_credential(value, resolve_api_key=False)
            ) is not None
        }
        for providerId in self._storage.runtimeOverrides:
            entries[providerId] = CredentialInfo(providerId=providerId, type="api_key")
        _throw_if_aborted(options)
        return list(entries.values())

    async def modify(
        self,
        providerId: str,
        fn: Callable[[CredentialValue | None], Awaitable[CredentialValue | None]],
        options: AuthOperationOptions | None = None,
    ) -> CredentialValue | None:
        _throw_if_aborted(options)
        publish: Callable[[], None] | None = None

        async def update(current: str | None) -> LockResult:
            nonlocal publish
            current_data = _coerce_storage_object(self._storage._parse_storage_data(current))
            produced = await fn(
                _to_runtime_credential(
                    current_data.get(providerId), resolve_api_key=False
                )
            )
            _throw_if_aborted(options)
            if produced is None:
                publish = _once_callback(
                    lambda: self._storage._replace_cached_data(current_data)
                )
                return LockResult(
                    result=_to_runtime_credential(
                        current_data.get(providerId), resolve_api_key=False
                    ),
                    publish=publish,
                )
            latest_data = {
                **current_data,
                providerId: produced.model_dump(exclude_none=False),
            }
            publish = _once_callback(
                lambda: self._storage._replace_cached_data(latest_data)
            )
            return LockResult(
                result=produced,
                next=json.dumps(latest_data, indent=2),
                publish=publish,
            )

        result = await _with_lock_async(self._storage.storage, update, options)
        if publish is not None:
            publish()
        return result

    async def delete(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> None:
        _throw_if_aborted(options)
        publish: Callable[[], None] | None = None

        async def remove(current: str | None) -> LockResult:
            nonlocal publish
            latest_data = _coerce_storage_object(self._storage._parse_storage_data(current))
            latest_data.pop(providerId, None)
            _throw_if_aborted(options)

            def publish_snapshot() -> None:
                self._storage._replace_cached_data(latest_data)
                self._storage.runtimeOverrides.pop(providerId, None)

            publish = _once_callback(publish_snapshot)
            return LockResult(
                result=None,
                next=json.dumps(latest_data, indent=2),
                publish=publish,
            )

        await _with_lock_async(self._storage.storage, remove, options)
        if publish is not None:
            publish()


__all__ = [
    "ApiKeyCredential",
    "AuthCredential",
    "AuthStatus",
    "AuthStorage",
    "AuthStorageBackend",
    "AuthStorageCredentialStore",
    "AuthStorageData",
    "FileAuthStorageBackend",
    "InMemoryAuthStorageBackend",
    "OAuthCredential",
]
