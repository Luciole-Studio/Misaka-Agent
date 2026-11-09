"""Translated from pi's ``packages/ai/src/auth/credential-store.ts`` (v1.1.0).

MISAKA fork: a queued task is an asyncio Task the caller's wait is raced against, shielded from the caller's own
cancellation the way a JavaScript promise cannot be cancelled by an awaiter. A caller whose task is cancelled before its
turn comes is treated like one whose signal aborted: its task does not run later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from misaka.ai.auth.types import AuthOperationOptions, Credential, CredentialInfo
from misaka.ai.utils.abort import operationSignal, raceWithAbortSignal
from misaka.utils.js import throwIfAborted


class InMemoryCredentialStore:
    """Default in-memory credential store. Apps inject persistent stores.
    Keyed by `Provider.id`, one credential per provider; see `CredentialStore`.
    Writes are serialized per provider through a promise chain.
    """

    def __init__(self) -> None:
        self._credentials: dict[str, Credential] = {}
        self._chains: dict[str, asyncio.Future[None]] = {}

    def _enqueue[T](
        self, providerId: str, task: Callable[[], Awaitable[T]], options: AuthOperationOptions | None = None
    ) -> asyncio.Future[T]:
        """Serialize tasks per provider id without releasing the chain before active work settles."""
        signal = operationSignal(options.signal if options is not None else None)
        previous = self._chains.get(providerId)
        abandoned = False

        async def run() -> T:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            throwIfAborted(signal)
            if abandoned:
                raise asyncio.CancelledError
            return await task()

        queued = asyncio.ensure_future(run())
        tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def settle(done: asyncio.Future[T]) -> None:
            if not done.cancelled():
                done.exception()
            tail.set_result(None)
            if self._chains.get(providerId) is tail:
                del self._chains[providerId]

        queued.add_done_callback(settle)
        self._chains[providerId] = tail
        waiting = raceWithAbortSignal(asyncio.shield(queued), signal)

        def onWaitingDone(done: asyncio.Future[T]) -> None:
            nonlocal abandoned
            if done.cancelled():
                abandoned = True

        waiting.add_done_callback(onWaitingDone)
        return waiting

    async def read(self, providerId: str, options: AuthOperationOptions | None = None) -> Credential | None:
        if options is not None:
            throwIfAborted(options.signal)
        return self._credentials.get(providerId)

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        if options is not None:
            throwIfAborted(options.signal)
        return [
            CredentialInfo(providerId=providerId, type=credential.type)
            for providerId, credential in self._credentials.items()
        ]

    def modify(
        self,
        providerId: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
        options: AuthOperationOptions | None = None,
    ) -> asyncio.Future[Credential | None]:
        async def task() -> Credential | None:
            current = self._credentials.get(providerId)
            next = await fn(current)
            if options is not None:
                throwIfAborted(options.signal)
            if next is not None:
                self._credentials[providerId] = next
            return next if next is not None else current

        return self._enqueue(providerId, task, options)

    def delete(self, providerId: str, options: AuthOperationOptions | None = None) -> asyncio.Future[None]:
        async def task() -> None:
            self._credentials.pop(providerId, None)

        return self._enqueue(providerId, task, options)


__all__ = ["InMemoryCredentialStore"]
