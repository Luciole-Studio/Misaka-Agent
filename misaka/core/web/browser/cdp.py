"""One loop-owned CDP connection: responses, page/OOPIF sessions and dialogs.

No background thread, reconnect, or automatic replay of an uncertain command.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlsplit, urlunsplit

from misaka.core.web.network import proxy_for_url, tls_verify
from misaka.core.web.runtime import api_client


async def resolve_endpoint(endpoint, *, direct=False):
    parts = urlsplit(endpoint)
    if parts.scheme in {'ws', 'wss'} and (direct or parts.path not in {'', '/'}):
        return endpoint  # Signed query belongs to the connection, not /json/version.
    if parts.scheme not in {'http', 'https', 'ws', 'wss'} or not parts.hostname:
        raise ValueError('CDP endpoint requires an HTTP(S) discovery root or WS(S) URL')
    scheme = {'ws': 'http', 'wss': 'https'}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip('/')
    if not path.endswith('/json/version'):
        path += '/json/version'
    url = urlunsplit((scheme, parts.netloc, path, parts.query, ''))
    async with api_client('browser-cdp-discovery', url, timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        result = response.json().get('webSocketDebuggerUrl')
    if not isinstance(result, str) or urlsplit(result).scheme not in {'ws', 'wss'}:
        raise ValueError('CDP discovery returned no WebSocket endpoint')
    return result




class Supervisor:
    def __init__(self, check_url=None):
        self.check_url = check_url
        self.network_slots = asyncio.Semaphore(8)
        self.blocked_requests = []
        self.ws = None
        self.reader = None
        self.sequence = 0
        self.pending = {}
        self.sessions = {}  # target/frame -> flattened session ID
        self.frames = {}
        self.dialogs = {}
        self.dialog_ready = asyncio.Event()
        self.children = set()
        self.root = None
        self.error = None
        self.dialog_lock = asyncio.Lock()
        self.relay = None
        self.peers = set()

    async def connect(self, endpoint, *, target_id=None, own_tab=False, direct=False):
        from websockets.asyncio.client import connect

        endpoint = await resolve_endpoint(endpoint, direct=direct)
        parts = urlsplit(endpoint)
        http_url = urlunsplit(('https' if parts.scheme == 'wss' else 'http', parts.netloc, parts.path, parts.query, ''))
        opts = {'proxy': None if direct else proxy_for_url(http_url, api=True)}
        if parts.scheme == 'wss':
            verify = tls_verify()
            if verify is not True:
                opts['ssl'] = verify
        self.ws = await connect(endpoint, open_timeout=15, close_timeout=3, max_size=16 * 1024 * 1024, **opts)
        self.reader = asyncio.create_task(self._read())
        if own_tab:
            target_id = (await self.call('Target.createTarget', {'url': 'about:blank'}))['targetId']
        if target_id is None:
            targets = (await self.call('Target.getTargets'))['targetInfos']
            target_id = next((t['targetId'] for t in targets if t['type'] == 'page'), None)
        if not target_id:
            raise RuntimeError('CDP has no page target')
        self.root = target_id
        await self.attach(target_id)
        return target_id

    async def attach(self, target_id):
        if target_id not in self.sessions:
            result = await self.call('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
            self.sessions[target_id] = result['sessionId']
        sid = self.sessions[target_id]
        await self.call('Page.enable', session=sid)
        if self.check_url:
            await self.call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]}, session=sid)
        await self.call('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': False,
                                               'flatten': True}, session=sid)
        tree = await self.call('Page.getFrameTree', session=sid)
        self._frame_tree(tree.get('frameTree', {}))
        return sid

    async def _request(self, params, sid):
        request_id = params['requestId']
        url = params.get('request', {}).get('url', '')
        try:
            async with self.network_slots:
                try:
                    # Renderer-local data/blob URLs carry no network destination.
                    if not url.startswith(('data:', 'blob:', 'about:blank')):
                        await self.check_url(url)
                except (OSError, ValueError) as error:
                    self.blocked_requests.append({'url': url[:1000], 'reason': str(error)[:500]})
                    del self.blocked_requests[:-32]
                    await self.call('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'BlockedByClient'}, session=sid)
                else:
                    await self.call('Fetch.continueRequest', {'requestId': request_id}, session=sid)
        except (RuntimeError, TimeoutError):
            pass

    def _frame_tree(self, row):
        frame = row.get('frame', {})
        if 'id' in frame:
            self.frames[frame['id']] = self.frames.get(frame['id'], {}) | frame
        for child in row.get('childFrames', []):
            self._frame_tree(child)

    async def call(self, method, params=None, *, session=None, timeout=30):
        if self.error is not None or self.ws is None:
            raise RuntimeError('CDP connection is closed; the action was not replayed')
        self.sequence += 1
        call_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        request = {'id': call_id, 'method': method, 'params': params or {}}
        if session is not None:
            request['sessionId'] = session
        try:
            await self.ws.send(json.dumps(request))
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self.pending.pop(call_id, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                future = self.pending.get(message.get('id'))
                if future is not None and not future.done():
                    if 'error' in message:
                        future.set_exception(RuntimeError(str(message['error'].get('message', 'CDP command failed'))))
                    else:
                        future.set_result(message.get('result', {}))
                    continue
                if 'id' in message:
                    continue  # Late/cancelled response is not an event for another relay client.
                if self.peers and message.get('method') != 'Fetch.requestPaused':
                    from websockets.asyncio.server import broadcast
                    broadcast(self.peers, raw)
                if message.get('sessionId') and message['sessionId'] not in self.sessions.values() and message.get('method') != 'Fetch.requestPaused':
                    continue  # Events for a relayed CLI attachment are not a second owner dialog.
                params, method = message.get('params', {}), message.get('method')
                if method == 'Fetch.requestPaused':
                    if len(self.children) >= 256:
                        raise RuntimeError('CDP resource-check queue exceeded its budget; connection closed')
                    task = asyncio.create_task(self._request(params, message.get('sessionId')))
                    self.children.add(task)
                    task.add_done_callback(self.children.discard)
                elif method == 'Page.javascriptDialogOpening':
                    key = uuid.uuid4().hex
                    self.dialogs[key] = {'id': key, 'type': params.get('type'), 'message': params.get('message'),
                                         'default_prompt': params.get('defaultPrompt'), 'session': message.get('sessionId')}
                    self.dialog_ready.set()
                elif method == 'Page.javascriptDialogClosed':
                    self.dialogs = {key: value for key, value in self.dialogs.items()
                                    if value['session'] != message.get('sessionId')}
                    if not self.dialogs:
                        self.dialog_ready.clear()
                elif method == 'Page.frameNavigated':
                    frame = params.get('frame', {})
                    if 'id' in frame:
                        self.frames[frame['id']] = self.frames.get(frame['id'], {}) | frame
                elif method == 'Page.frameAttached':
                    fid = params['frameId']
                    self.frames[fid] = self.frames.get(fid, {}) | {'id': fid, 'parentId': params['parentFrameId']}
                elif method == 'Page.frameDetached':
                    # Process promotion is not removal; a live OOPIF still owns this frame.
                    fid = params.get('frameId')
                    if params.get('reason') != 'swap' and fid not in self.sessions:
                        self._detach_frame(fid)
                elif method == 'Target.attachedToTarget':
                    info = params.get('targetInfo', {})
                    if info.get('type') == 'iframe':
                        self.sessions[info['targetId']] = params['sessionId']
                        task = asyncio.create_task(self._enable_child(params['sessionId']))
                        self.children.add(task)
                        task.add_done_callback(self.children.discard)
                elif method == 'Target.detachedFromTarget':
                    self.sessions = {key: value for key, value in self.sessions.items() if value != params.get('sessionId')}
        except Exception:  # noqa: BLE001 - tool or transport boundary reports the failure
            self.error = 'CDP transport disconnected'
            if self.ws is not None:
                await self.ws.close()
        finally:
            self.error = self.error or 'CDP transport closed'
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(self.error + '; action outcome may be unknown'))

    async def _enable_child(self, sid):
        try:
            await self.call('Page.enable', session=sid)
            if self.check_url:
                await self.call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]}, session=sid)
            await self.call('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': False,
                                                   'flatten': True}, session=sid)
            tree = await self.call('Page.getFrameTree', session=sid)
            self._frame_tree(tree.get('frameTree', {}))
        except Exception:  # noqa: BLE001, S110 - a child that detached during setup is not an error
            pass

    def _detach_frame(self, frame_id):
        removed = {frame_id}
        while children := {fid for fid, row in self.frames.items() if row.get('parentId') in removed} - removed:
            removed.update(children)
        for fid in removed:
            self.frames.pop(fid, None)
            self.sessions.pop(fid, None)

    def state(self):
        return {'pending_dialogs': [{key: value for key, value in row.items() if key != 'session'}
                                    for row in self.dialogs.values()],
                'blocked_requests': list(self.blocked_requests),
                'frame_tree': self.frame_tree(), 'frame_tree_limit': 30, 'cdp_connected': self.error is None}

    def frame_tree(self):
        remaining = 30
        def build(fid, ancestors):
            nonlocal remaining
            remaining -= 1
            return {**self.frames[fid], 'is_oopif': fid in self.sessions and fid != self.root,
                    'childFrames': [build(child, ancestors | {fid}) for child, row in self.frames.items()
                                    if remaining > 0 and len(ancestors) < 8 and row.get('parentId') == fid and child not in ancestors | {fid}]}
        return [build(fid, set()) for fid, row in self.frames.items() if remaining > 0 and row.get('parentId') not in self.frames]

    async def command(self, method, params, *, target_id=None, frame_id=None, timeout=30):
        if target_id and frame_id:
            raise ValueError('Use target_id or frame_id, not both')
        target = frame_id or target_id
        if target and target not in self.sessions and not (frame_id and frame_id in self.frames):
            raise ValueError('Target/frame is not owned by this browser session; read its current frame_tree')
        if frame_id:
            visited = set()
            while target not in self.sessions and target not in visited:
                visited.add(target)
                target = self.frames.get(target, {}).get('parentId', self.root)
        session = self.sessions.get(target) if target else (
            None if method.split('.')[0] in {'Browser', 'Target', 'Storage'} else self.sessions.get(self.root))
        if frame_id and method == 'Runtime.evaluate':
            world = await self.call('Page.createIsolatedWorld', {'frameId': frame_id, 'worldName': 'misaka'}, session=session)
            params = params | {'contextId': world['executionContextId']}
        elif frame_id and method == 'Page.navigate':
            params = params | {'frameId': frame_id}
        return await self.call(method, params, session=session, timeout=timeout)

    async def dialog(self, action, prompt_text='', dialog_id=None):
        if action not in {'accept', 'dismiss'}:
            raise ValueError('Dialog action takes accept or dismiss')
        async with self.dialog_lock:
            if dialog_id is None:
                if len(self.dialogs) != 1:
                    raise ValueError('Choose a pending dialog_id from browser_snapshot')
                dialog_id = next(iter(self.dialogs))
            row = self.dialogs.get(dialog_id)
            if row is None:
                raise ValueError('That dialog is no longer pending')
            await self.call('Page.handleJavaScriptDialog', {'accept': action == 'accept', 'promptText': prompt_text},
                            session=row['session'])
            self.dialogs.pop(dialog_id, None)
            if not self.dialogs:
                self.dialog_ready.clear()
            return {'success': True, 'action': action, 'dialog_id': dialog_id}

    async def share(self):
        """One Lightpanda context lives on one upstream connection. Share that
        actual connection with native CLI clients, not a second empty context.
        The relay is owner-private loopback, and never creates a new upstream.
        """
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
        path = '/misaka/' + uuid.uuid4().hex
        async def forward(peer):
            if peer.request.path != path or len(self.peers) >= 2:
                await peer.close(code=1008)
                return
            self.peers.add(peer)
            try:
                async for raw in peer:
                    request = json.loads(raw)
                    if 'id' not in request:
                        continue
                    try:
                        method, params = request['method'], request.get('params') or {}
                        if method == 'Target.attachToTarget' and params.get('targetId') in self.sessions:
                            # Lightpanda emits page events on the first attachment.
                            # All clients share that attachment as well as its socket.
                            result = {'sessionId': self.sessions[params['targetId']]}
                        elif method == 'Target.detachFromTarget' and params.get('sessionId') in self.sessions.values():
                            result = {}  # A peer disconnect does not detach the owner.
                        else:
                            result = await self.call(method, params, session=request.get('sessionId'))
                        response = {'id': request['id'], 'result': result}
                    except (RuntimeError, TimeoutError) as error:
                        response = {'id': request['id'], 'error': {'code': -32000, 'message': str(error)}}
                    await peer.send(json.dumps(response))
            except ConnectionClosed:
                pass
            finally:
                self.peers.discard(peer)
        self.relay = await serve(forward, '127.0.0.1', 0, max_size=16 * 1024 * 1024, close_timeout=2)
        return f'ws://127.0.0.1:{self.relay.sockets[0].getsockname()[1]}{path}'

    async def close(self, *, close_tab=False):
        try:
            if close_tab and self.root and self.error is None:
                await self.call('Target.closeTarget', {'targetId': self.root}, timeout=5)
        finally:
            if self.relay:
                self.relay.close()
                await self.relay.wait_closed()
                self.relay = None
            if self.ws is not None:
                await self.ws.close()
            tasks = [*self.children, *([self.reader] if self.reader else [])]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.ws = None
