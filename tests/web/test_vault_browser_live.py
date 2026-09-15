"""Opt-in real Chromium/agent-browser test; only fresh profiles and loopback HTML.

Set MISAKA_TEST_BROWSER_COMMAND and MISAKA_TEST_CHROME to explicit executables.
No installation, account, external website or personal browser profile is used.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.web import WebPart
from misaka.core.web.browser.vault.classifier import build_fill_js, build_inspection_js
from misaka.core.web.browser.vault.store import VaultStore

pytestmark = pytest.mark.skipif(
    not (os.environ.get('MISAKA_TEST_BROWSER_COMMAND') and os.environ.get('MISAKA_TEST_CHROME')),
    reason='requires explicit disposable browser fixture executables')


async def test_real_vault_form_dialog_focus_and_cleanup(tmp_path):
    html = b'''<!doctype html><title>Fixture login</title><form>
      <label>User<input name="user" autocomplete="username"></label>
      <label>Password<input id="pw" type="password" autocomplete="current-password"></label>
      <label>OTP<input id="otp" autocomplete="one-time-code"></label></form>'''
    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b'\r\n\r\n')
            body = b'<!doctype html><title>No form</title>' if b'GET /blank ' in request else html
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' +
                         str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # Chromium may close speculative connections without a request.
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    other = await asyncio.start_server(serve, '127.0.0.1', 0)
    origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
    other_origin = f'http://127.0.0.1:{other.sockets[0].getsockname()[1]}'
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'web.json').write_text(json.dumps({
        'allow_private_urls': True,
        'env': {'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '', 'NO_PROXY': '*'},
        'browser': {'command': os.environ['MISAKA_TEST_BROWSER_COMMAND'], 'backend': 'agent-browser',
                    'executable_path': os.environ['MISAKA_TEST_CHROME']},
        'vault': {'onepassword': {'enabled': False}, 'bitwarden': {'enabled': False}}}))
    owner = WebPart(SimpleNamespace(profile_dir=str(profile), workspace=str(tmp_path)))
    definitions = {tool.name: tool for tool in owner.tools}
    secret = 'fixture-browser-password-59172'
    class UI:
        async def input(self, title):
            return 'fixture-user'
        async def custom(self, factory):
            answers = []
            component = factory(None, None, None, answers.append)
            component.input.handleInput(secret)
            assert secret not in ''.join(component.render(80))
            component.handleInput('\n')
            component.dispose()
            return answers[0]
    ctx = SimpleNamespace(hasUI=True, ui=UI())
    async def call(name, args, context=None):
        return await definitions[name].execute('fixture-' + name, args, None, None, context)
    async def script(supervisor, expression):
        result = await supervisor.command('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        assert not result.get('exceptionDetails'), result
        return result.get('result', {}).get('value')
    try:
        await call('browser_navigate', {'url': origin + '/login'})
        saved = await call('browser_vault_save_login', {}, ctx)
        assert '"filled_fields": 1' in json.dumps(saved) or 'filled_fields' in str(saved)
        assert secret not in json.dumps(saved)
        session = next(iter(owner.runtime.browser.sessions.values()))
        supervisor = session.supervisor
        assert await script(supervisor, "document.querySelector('#pw').value") == secret
        readback = await call('browser_cdp', {'method': 'Runtime.evaluate', 'params': {
            'expression': "document.querySelector('#pw').value", 'returnByValue': True}})
        assert secret not in json.dumps(readback) and '<redacted>' in str(readback)

        # Nonce-bound fields survive a DOM reorder without filling the new decoy.
        await script(supervisor, build_inspection_js('fixture-nonce'))
        await script(supervisor, "document.body.insertAdjacentHTML('afterbegin','<input id=decoy type=password>')")
        filled = await script(supervisor, build_fill_js([
            {'index': 1, 'token': 'current-password', 'value': secret}], origin, 'fixture-nonce'))
        assert json.loads(filled)['filled'] == 1
        assert await script(supervisor, "document.querySelector('#decoy').value") == ''

        meta = VaultStore(profile / 'vault').add_item('login', 'OTP fixture', {
            'identifier': 'fixture-user', 'identifier_type': 'username', 'password': secret,
            'otp_secret': 'JBSWY3DPEHPK3PXP'}, origin=origin)
        result = await call('browser_vault_enter_code', {'handle': meta.id})
        code = await script(supervisor, "document.querySelector('#otp').value")
        assert code.isdigit() and len(code) == 6 and code not in json.dumps(result)
        readback = await call('browser_cdp', {'method': 'Runtime.evaluate', 'params': {
            'expression': "document.querySelector('#otp').value", 'returnByValue': True}})
        assert code not in json.dumps(readback)

        pending = await call('browser_cdp', {'method': 'Runtime.evaluate', 'params': {
            'expression': 'alert(' + json.dumps(secret) + ')'}})
        assert 'pending_dialogs' in str(pending) and secret not in json.dumps(pending)
        await call('browser_dialog', {'action': 'dismiss'})

        # A cross-site iframe becomes an OOPIF; DOM removal retires its frame/session.
        frame_url = other_origin.replace('127.0.0.1', 'localhost') + '/login'
        await script(supervisor, "const frame = document.createElement('iframe'); frame.id = 'fixture-frame'; "
            "frame.src = " + json.dumps(frame_url) + "; document.body.appendChild(frame)")
        frame_id = None
        for _ in range(100):
            frame_id = next((fid for fid, row in supervisor.frames.items()
                             if row.get('url') == frame_url and fid in supervisor.sessions), None)
            if frame_id:
                break
            await asyncio.sleep(.02)
        assert frame_id is not None, supervisor.state()
        await script(supervisor, "document.querySelector('#fixture-frame').remove()")
        for _ in range(100):
            if frame_id not in supervisor.frames and frame_id not in supervisor.sessions:
                break
            await asyncio.sleep(.02)
        assert frame_id not in supervisor.frames and frame_id not in supervisor.sessions

        await call('browser_navigate', {'url': other_origin + '/login'})
        changed = await script(supervisor, build_fill_js([
            {'index': 1, 'token': 'current-password', 'value': secret}], origin, 'fixture-nonce'))
        assert json.loads(changed)['refused'] == 'origin_changed'
        assert await script(supervisor, "document.querySelector('#pw').value") == ''

        # Focus searches owned pages for the form; shared CDP mode keeps its root.
        target = (await supervisor.call('Target.createTarget', {'url': origin + '/login'}))['targetId']
        for _ in range(100):
            focused = await owner.runtime.run(supervisor.focus_page, origin,
                accept="!!document.querySelector('#pw')", owned_browser=True)
            if focused.get('ok'):
                break
            await asyncio.sleep(.02)
        assert focused['ok'] and supervisor.root == target
        retained = supervisor.root
        assert not (await owner.runtime.run(supervisor.focus_page, other_origin, owned_browser=False))['ok']
        assert supervisor.root == retained
        scratch = Path(session.root)
        pending = asyncio.create_task(call('browser_cdp', {'method': 'Runtime.evaluate', 'params': {
            'expression': 'new Promise(resolve => setTimeout(resolve, 5000))', 'awaitPromise': True}}))
        await asyncio.sleep(.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        await owner.runtime.close()
        server.close()
        other.close()
        await server.wait_closed()
        await other.wait_closed()
    assert not scratch.exists()
