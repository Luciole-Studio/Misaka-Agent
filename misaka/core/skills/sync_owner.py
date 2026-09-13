"""One async sync scheduler per Skill session; no process-global timer or daemon."""
import asyncio
import dataclasses
import logging
import threading

from misaka.utils.async_lifecycle import run_in_thread, settle

from .scope import using_scope


class SyncOwner:
    def __init__(self, scope):
        self.scope = dataclasses.replace(scope, stop=threading.Event())
        self.task = None
        self.closed = False
        self.last_result = None
        self._lock = asyncio.Lock()

    def enabled(self):
        if self.scope.profile is None or self.closed:
            return False
        from .vendor.skills_sync_client import sync_feature_enabled
        with using_scope(self.scope):
            return sync_feature_enabled()

    async def _drain(self):
        self.scope.stop.set()
        task = self.task
        if task is not None:
            task.cancel()
            try:
                await settle(task)
            except asyncio.CancelledError:
                pass
            except Exception as error:
                self.last_result = {"success": False, "error": str(error)}
                logging.getLogger(__name__).exception("Owned Skill sync failed")
            finally:
                self.task = None

    async def stop(self):
        self.scope.stop.set()
        async with self._lock:
            await self._drain()

    async def close(self):
        self.closed = True
        # A cancelled closer still owns the drain, even behind a reschedule.
        await settle(asyncio.create_task(self.stop()))

    async def schedule(self, *, startup=False):
        async with self._lock:
            if self.closed or self.scope.profile is None or (not startup and not self.enabled()):
                return
            await self._drain()
            if self.closed:
                return
            self.scope.stop = threading.Event()
            self.task = asyncio.create_task(self._run(startup))

    async def _run(self, startup):
        if not startup:
            await asyncio.sleep(5.0)  # Exact native _SYNC_PUSH_DEBOUNCE_S.
        from . import distribution
        from .vendor import skills_sync_client as sync
        from .vendor import skills_sync_client_org as org
        def perform():
            with using_scope(self.scope):
                try:
                    identity = sync.resolve_identity()
                    if self.scope.stop.is_set():
                        return
                    if startup and not (identity.get('claims') or {}).get('org_role'):
                        self.last_result = distribution.execute('org-clear', scope=self.scope)
                    if not identity.get('nous_admin') or not sync.sync_feature_enabled():
                        return
                    if startup:
                        self.last_result = distribution.execute('sync-pull', scope=self.scope, identity=identity)
                        if not self.scope.stop.is_set():
                            try:
                                org_identity = org.resolve_org_identity()
                            except sync.SyncInertError:
                                return
                            self.last_result = distribution.execute('org-pull', scope=self.scope, identity=org_identity)
                    elif sync.list_synced_skill_names():
                        self.last_result = distribution.execute('sync-push', scope=self.scope, identity=identity)
                except Exception as error:  # noqa: BLE001 - malformed remote data remains an auxiliary receipt
                    self.last_result = {'success': False, 'error': str(error)}
        await run_in_thread(perform)
