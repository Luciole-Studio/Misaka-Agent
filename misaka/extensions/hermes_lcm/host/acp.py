"""The pinned ACP transport under native cancellation and tool-permission ownership."""
from __future__ import annotations

import asyncio
import logging
import threading

from misaka.core.platform import processes
from misaka.utils.async_lifecycle import run_in_thread, settle
from misaka.utils.values import signal_aborted

from ..native.copilot_acp_client import (
    CopilotACPClient,
)

logger = logging.getLogger(__name__)


class OwnedACPClient(CopilotACPClient):
    """One physical request, one process tree; closing fences a concurrent launch."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._launch_lock = threading.RLock()
        self._stopped = False

    def _spawn(self):
        with self._launch_lock:
            if self._stopped:
                raise RuntimeError('ACP request was cancelled before process launch')
            return super()._spawn()

    def close(self):
        with self._launch_lock:
            self._stopped = True
            with self._active_process_lock:
                proc = self._active_process
            if proc is not None and proc.poll() is None:
                processes.terminate(proc.pid)
            super().close()


async def complete(*, messages, model, timeout, cwd, signal=None, **kwargs):
    from misaka.ai.utils.abort import wait_for_abort
    if signal_aborted(signal):
        raise asyncio.CancelledError('ACP request cancelled')
    client = OwnedACPClient(acp_cwd=cwd)
    worker = asyncio.create_task(asyncio.to_thread(client.chat.completions.create,
        messages=messages, model=model, timeout=timeout, tools=[], **kwargs))
    aborting = asyncio.create_task(wait_for_abort(signal)) if signal is not None else None
    try:
        done, _ = await asyncio.wait([worker, *([aborting] if aborting else [])], return_when=asyncio.FIRST_COMPLETED)
        if aborting in done:
            raise asyncio.CancelledError('ACP request cancelled')
        return worker.result()
    finally:
        async def cleanup():
            if aborting is not None:
                aborting.cancel()
                await asyncio.gather(aborting, return_exceptions=True)
            if not worker.done():
                await run_in_thread(client.close)
                # A terminated child can report a transport error. The cancelling
                # owner wins, after the original pumps/pipes have drained.
                try:
                    await settle(worker)
                except Exception:
                    logger.debug("ACP child terminated during cancellation", exc_info=True)
        _, cancelled = await settle(asyncio.create_task(cleanup()))
        if cancelled is not None:
            raise cancelled
