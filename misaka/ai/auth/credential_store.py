"""The default credential store, translated from pi's ``auth/credential-store.ts``.

Apps inject persistent stores; this one is the fallback and the reference for what
``CredentialStore`` promises. The promise upstream cares about is serialization: writes
for one provider happen one at a time, because ``Models.getAuth()`` runs OAuth refresh
inside ``modify`` and two concurrent requests must not both spend the same refresh token.

Upstream serializes by chaining promises per provider id and hands the caller a race
between its own chain entry and the signal, so giving up on the wait neither breaks the
chain nor publishes an active task's late result. Here that is an ``asyncio.Lock`` per
provider id plus the same outer race: ``_acquire`` removes an aborted queued waiter,
while an active callback keeps the lock until it settles and then fails its write guard.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from misaka.ai.auth.types import (
    AuthOperationOptions,
    CredentialInfo,
    CredentialValue,
)
from misaka.ai.utils.abort import race_with_abort_signal, wait_for_abort
from misaka.utils.values import signal_aborted


def _throwIfAborted(options: AuthOperationOptions | None) -> None:
    if options is not None and signal_aborted(options.signal):
        raise RuntimeError("Request was aborted")
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


class InMemoryCredentialStore:
    """Keyed by provider id, one credential per provider. Writes serialized per key."""

    def __init__(self) -> None:
        self._credentials: dict[str, CredentialValue] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, providerId: str) -> asyncio.Lock:
        lock = self._locks.get(providerId)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[providerId] = lock
        return lock

    async def _acquire(self, providerId: str, options: AuthOperationOptions | None) -> asyncio.Lock:
        """Wait for the provider's turn, but stop waiting if the caller gives up.

        A caller that aborts while queued raises here instead of blocking until an
        unrelated refresh finishes -- upstream's `raceWithAbortSignal` around the queued task.
        """
        _throwIfAborted(options)
        lock = self._lock(providerId)
        signal = options.signal if options is not None else None
        if signal is None:
            await lock.acquire()
            return lock
        acquisition = asyncio.ensure_future(lock.acquire())
        aborted = asyncio.ensure_future(wait_for_abort(signal))

        def _release_acquisition(task: asyncio.Task[bool]) -> None:
            if task.cancelled():
                return
            try:
                acquired = task.result()
            except Exception:  # noqa: BLE001 - failed acquisition owns no lock to release
                return
            if acquired:
                lock.release()

        def _abandon_acquisition() -> None:
            # Whatever ends this wait without the lock must not leave `acquisition`
            # running: it would win the lock later with nobody left to release it,
            # wedging every subsequent writer for this provider. `Lock.acquire` is
            # cancellation-safe (a cancelled acquire wakes the next waiter), and the
            # callback covers the race where it completed before the cancel landed.
            if acquisition.done():
                _release_acquisition(acquisition)
                return
            acquisition.add_done_callback(_release_acquisition)
            acquisition.cancel()

        try:
            done, _ = await asyncio.wait(
                {acquisition, aborted}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # The caller's own task was cancelled while queued -- the path the abort
            # signal never sees. This used to leak `acquisition`, which then took the
            # lock with nobody left to release it: every later writer deadlocked.
            _abandon_acquisition()
            raise
        finally:
            aborted.cancel()
        if signal_aborted(signal) or aborted in done:
            _abandon_acquisition()
            raise RuntimeError("Request was aborted")
        if acquisition in done:
            acquisition.result()
            return lock
        # The signal won.
        _abandon_acquisition()
        raise RuntimeError("Request was aborted")

    async def read(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> CredentialValue | None:
        _throwIfAborted(options)
        return self._credentials.get(providerId)

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        _throwIfAborted(options)
        return [
            CredentialInfo(providerId=providerId, type=credential.type)
            for providerId, credential in self._credentials.items()
        ]

    async def modify(
        self,
        providerId: str,
        fn: Callable[[CredentialValue | None], Awaitable[CredentialValue | None]],
        options: AuthOperationOptions | None = None,
    ) -> CredentialValue | None:
        async def operation() -> CredentialValue | None:
            lock = await self._acquire(providerId, options)
            try:
                current = self._credentials.get(providerId)
                produced = await fn(current)
                _throwIfAborted(options)
                if produced is not None:
                    self._credentials[providerId] = produced
                # `next ?? current`: declining to write returns what is still stored, so a
                # caller cannot tell "I wrote this" from "someone else's write stands".
                return produced if produced is not None else current
            finally:
                lock.release()

        signal = options.signal if options is not None else None
        return await race_with_abort_signal(operation(), signal)

    async def delete(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        lock = await self._acquire(providerId, options)
        try:
            self._credentials.pop(providerId, None)
        finally:
            lock.release()


__all__ = ["InMemoryCredentialStore"]
