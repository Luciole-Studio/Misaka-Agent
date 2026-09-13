"""Bound stdio browser controller and an actual MISAKA client entrypoint.

One process is one controller lane. Requests carry an unguessable binding and an
id; a disconnected or mismatched lane never falls back to a different browser.
Run the reference client with python -m misaka.core.web.browser.controller.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import signal
import sys
import time
import uuid

from misaka.core.web.browser import settings
from misaka.core.web.browser.process import terminate
from misaka.utils.async_lifecycle import settle

_LIMIT = 32 * 1024 * 1024


class Controller:
    def __init__(self, cwd, cfg):
        self.cwd, self.cfg = cwd, cfg
        self.binding = uuid.uuid4().hex
        self.proc = None
        self.lock = asyncio.Lock()
        self.capabilities = set()
        self.receipts = {}
        self.receipt_bytes = 0
        self.closed = False
        self.pending_action = None
        self.last_used = time.monotonic()
        self.failed = False

    async def exchange(self, value, timeout=60):
        if self.proc is None or self.proc.returncode is not None or self.failed:
            raise RuntimeError('Bound browser controller is disconnected; action was not replayed')
        packet = json.dumps({'binding': self.binding, **value}).encode() + b'\n'
        if len(packet) > _LIMIT:
            raise ValueError('Browser controller request exceeds transport budget')
        async with asyncio.timeout(timeout):
            self.proc.stdin.write(packet)
            await self.proc.stdin.drain()
            line = await self.proc.stdout.readline()
        try:
            result = json.loads(line)
        except ValueError:
            self.failed = True
            raise RuntimeError('Bound browser controller returned no valid response; action outcome is unknown') from None
        if result.get('binding') != self.binding or result.get('id') != value['id']:
            self.failed = True
            raise RuntimeError('Browser controller binding or request identity mismatch')
        if result.get('error'):
            raise RuntimeError(str(result['error']))
        return result['result']

    async def perform(self, name, args, call_id):
        async with self.lock:
            if self.closed or self.failed:
                raise RuntimeError('Bound browser controller is closed; no fallback browser was used')
            self.last_used = time.monotonic()
            digest = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
            if call_id and call_id in self.receipts:
                previous, result, _ = self.receipts[call_id]
                if previous != digest:
                    raise ValueError('A browser tool call id was reused with different arguments')
                return copy.deepcopy(result)
            if self.proc is None:
                argv = self.cfg['controller_command']
                if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
                    raise ValueError('browser.controller_command must be an explicit executable/argument JSON array')
                # The controller resolves cloud accounts; the engine it launches does not.
                env = settings.subprocess_env() | settings.provider_environment()
                self.proc, cancelled = await settle(asyncio.create_task(asyncio.create_subprocess_exec(*argv, cwd=self.cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=_LIMIT, start_new_session=os.name == 'posix')))
                if cancelled:
                    await self.close()
                    raise cancelled
                from misaka.config.product import CFG
                from misaka.core.web.scope import current_scope
                handshake = await self.exchange({'id': 'hello', 'op': 'hello', 'profile_dir': current_scope().profile_dir,
                                                  'cwd': self.cwd, 'web_config': CFG['web_config']}, timeout=15)
                self.capabilities = set(handshake.get('capabilities', []))
            if name not in self.capabilities:
                raise ValueError('The bound browser controller does not advertise ' + name)
            key = call_id or uuid.uuid4().hex
            try:
                result = await self.exchange({'id': key, 'op': 'call', 'action': name, 'arguments': args},
                                             timeout=float(args.get('timeout_s', self.cfg.get('command_timeout', 60))))
            except BaseException:
                self.failed = True
                raise
            if 'image_base64' in result:
                result['image_bytes'] = base64.b64decode(result.pop('image_base64'), validate=True)
            if call_id:
                size = len(json.dumps({key: value for key, value in result.items() if key != 'image_bytes'}).encode()) + len(result.get('image_bytes', b''))
                # Bound image receipts as well as count; never discard idempotency silently.
                if len(self.receipts) >= 256 or self.receipt_bytes + size > _LIMIT:
                    self.failed = True
                    raise RuntimeError('Controller receipt budget exhausted after this action; do not replay it')
                self.receipts[call_id] = (digest, copy.deepcopy(result), size)
                self.receipt_bytes += size
            return result

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.proc is not None and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                # Reference client handles TERM by cancelling and draining its owner.
                self.proc.terminate()
                async with asyncio.timeout(30):
                    await self.proc.wait()
            except (ProcessLookupError, TimeoutError):
                pass
            _, cancelled = await settle(asyncio.create_task(terminate(self.proc)))
            if cancelled:
                raise cancelled


async def serve():
    from misaka.core.web.browser import BrowserManager
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    owner = None
    binding = None
    manager = None
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if os.name == 'posix':
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    reader = asyncio.StreamReader(limit=_LIMIT)
    input_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    output_transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(output_transport, protocol, None, loop)
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                return
            if len(raw) > _LIMIT:
                raise ValueError('Controller frame exceeds budget')
            request = json.loads(raw)
            response = {'binding': request.get('binding'), 'id': request.get('id')}
            try:
                if binding is None:
                    if request.get('op') != 'hello':
                        raise ValueError('Controller requires a binding handshake')
                    binding = request['binding']
                    from misaka.config.product import CFG
                    CFG['web_config'] = request['web_config']
                    scope = WebScope(request.get('profile_dir'))
                    owner = WebRuntime(scope)
                    manager = BrowserManager(request['cwd'])
                    owner.browser = manager
                    # Each call loads its profile snapshot, then disables controller
                    # routing on that snapshot only. Never edit the user's configuration.
                    with scope.activate(snapshot=True) as view:
                        view.config = copy.deepcopy(view.config)
                        view.config.setdefault('browser', {}).pop('controller_command', None)
                        caps = settings.available_tools(include_fallback=True)
                    response['result'] = {'capabilities': sorted(caps)}
                else:
                    if request.get('binding') != binding or request.get('op') != 'call':
                        raise ValueError('Wrong controller binding or operation')
                    async def call(request=request, manager=manager):
                        from misaka.core.web.scope import current_scope
                        view = current_scope()
                        view.config = copy.deepcopy(view.config)
                        view.config.setdefault('browser', {}).pop('controller_command', None)
                        return await manager.perform(request['action'], request.get('arguments', {}), request['id'])
                    result = await owner.run(call)
                    if 'image_bytes' in result:
                        result['image_base64'] = base64.b64encode(result.pop('image_bytes')).decode()
                    response['result'] = result
            except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
                response['error'] = type(error).__name__ + ': ' + str(error)
            packet = (json.dumps(response) + '\n').encode()
            if len(packet) > _LIMIT:
                raise ValueError('Controller response exceeds budget')
            writer.write(packet)
            await writer.drain()
    finally:
        input_transport.close()
        writer.close()
        if owner:
            await owner.close()


if __name__ == '__main__':
    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
