"""Native lifecycle, UI and permission checks around the Hermes vault port."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.config import home
from misaka.core.web import WebPart, config
from misaka.core.web.browser import settings
from misaka.core.web.browser.vault import host, tools
from misaka.core.web.browser.vault.backends import unlock
from misaka.core.web.browser.vault.store import VaultStore
from misaka.core.web.scope import WebScope


@pytest.fixture
def part(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir(exist_ok=True)
    (profile / 'web.json').write_text(json.dumps({'vault': {
        'onepassword': {'enabled': False}, 'bitwarden': {'enabled': False}}}))
    monkeypatch.setattr(settings, 'available_tools', lambda **kw: set(settings.VAULT_TOOLS) | set(settings.CDP_TOOLS))
    return WebPart(SimpleNamespace(profile_dir=str(profile), workspace=str(tmp_path)))


class Page:
    def __init__(self, origin='https://example.test', *, otp=False):
        self.origin, self.otp = origin, otp
        self.expressions = []

    async def perform(self, name, args, call_id=''):
        if name == '_vault_focus':
            return {'ok': True, 'url': self.origin}
        assert name == 'browser_cdp'  # Never a CLI/argv secret path.
        expression = args['params']['expression']
        self.expressions.append(expression)
        if expression == 'window.location.href':
            value = self.origin + '/login'
        elif 'const fills =' in expression:
            value = json.dumps({'filled': 1})
        else:
            value = json.dumps([{'index': 0, 'type': 'text' if self.otp else 'password',
                'autocomplete': 'one-time-code' if self.otp else 'current-password', 'label': '', 'name': 'otp' if self.otp else 'pw'}])
        return {'success': True, 'result': {'result': {'value': value}}}

    async def close(self):
        pass


def login(part, password='fixture-vault-password'):
    store = VaultStore(home.path('vault', part.scope.profile_dir))
    return store.add_item('login', 'Fixture', {'identifier': 'fixture-user',
        'identifier_type': 'username', 'password': password,
        'otp_secret': 'JBSWY3DPEHPK3PXP'}, origin='https://example.test')


def tool(part, name):
    return next(t for t in part.tools if t.name == name)


async def test_native_fill_and_subsequent_readback_are_secret_blind(part):
    meta = login(part)
    page = part.runtime.browser = Page()
    try:
        result = await tool(part, 'browser_vault_fill').execute('fill', {'handle': meta.id}, None, None, None)
        assert '"filled_fields": 1' in result['content'][0]['text']
        assert 'fixture-vault-password' not in json.dumps(result)
        assert any('fixture-vault-password' in expression for expression in page.expressions)
        with part.scope.activate():
            assert config.redact_secrets('fixture-vault-password') == config.REDACTED
        listed = await tool(part, 'browser_vault_list').execute('list', {}, None, None, None)
        assert meta.id in json.dumps(listed) and 'fixture-vault-password' not in json.dumps(listed)
    finally:
        await part.runtime.close()


async def test_saved_otp_is_bound_to_its_login_origin(part):
    meta = login(part)
    page = part.runtime.browser = Page('https://another.test', otp=True)
    try:
        with pytest.raises(RuntimeError, match='origin_mismatch'):
            await tool(part, 'browser_vault_enter_code').execute('otp', {'handle': meta.id}, None, None, None)
        assert all('const fills =' not in expression for expression in page.expressions)
    finally:
        await part.runtime.close()


@pytest.mark.parametrize('value', ['123', '246810', 'short', 'fixture-long-password'])
def test_registered_vault_values_are_redacted_even_below_api_key_length(value):
    host.register_vault_redaction_value(value)
    assert value not in config.redact_secrets('before ' + value + ' after')


def test_vault_redaction_survives_api_key_rotation_and_covers_manager_tokens():
    host.register_vault_redaction_value('fixture-password')
    unlock.store_session_token('fixture', 'fixture-manager-token')
    for index in range(130):
        config.remember_secret(f'rotated-api-key-{index}')
    assert config.redact_secrets('fixture-password fixture-manager-token') == '<redacted> <redacted>'


def test_secret_eval_without_native_owner_has_no_fallback():
    assert tools._eval_js_secret('fixture', 'anything')['error_type'] == 'supervisor_required'


async def test_masked_save_login_uses_ui_not_transcript(part):
    page = part.runtime.browser = Page()
    rendered = []
    class UI:
        async def input(self, title):
            return 'fixture-user'
        async def custom(self, factory):
            future = asyncio.get_running_loop().create_future()
            component = factory(None, None, None, lambda value: future.set_result(value))
            component.input.handleInput('fixture-masked-password')
            rendered.extend(component.render(80))
            component.handleInput('\n')
            value = await future
            component.dispose()
            assert component.input.getValue() == ''
            assert component.input.undoStack.length == component.input.killRing.length == 0
            return value
    try:
        result = await tool(part, 'browser_vault_save_login').execute('save', {}, None, None,
            SimpleNamespace(hasUI=True, ui=UI()))
        assert 'fixture-masked-password' not in ''.join(rendered)
        assert 'fixture-masked-password' not in json.dumps(result)
        assert any('fixture-masked-password' in expression for expression in page.expressions)
    finally:
        await part.runtime.close()


async def test_headless_save_never_prompts_or_writes(part):
    part.runtime.browser = Page()
    try:
        with pytest.raises(RuntimeError, match='prompt_unavailable'):
            await tool(part, 'browser_vault_save_login').execute('save', {}, None, None, None)
        assert not (Path(part.scope.profile_dir) / 'vault/vault.json.enc').exists()
    finally:
        await part.runtime.close()


@pytest.mark.parametrize('mode', ['cancel', 'repeated_cancel', 'signal', 'close'])
async def test_vault_cancellation_drains_loop_cleanup_before_return(part, mode):
    from misaka.agent.agent import AbortController

    entered, cleaned = asyncio.Event(), asyncio.Event()
    class Waiting(Page):
        async def perform(self, name, args, call_id=''):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.03)
                cleaned.set()
    part.runtime.browser = Waiting()
    controller = AbortController()
    call = asyncio.create_task(tool(part, 'browser_vault_save_login').execute('save', {}, controller.signal, None, None))
    await entered.wait()
    try:
        if mode == 'signal':
            controller.abort()
            with pytest.raises(RuntimeError, match='aborted'):
                await call
        else:
            if mode == 'close':
                await part.runtime.close()
            else:
                call.cancel()
                if mode == 'repeated_cancel':
                    await asyncio.sleep(.01)
                    call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        assert cleaned.is_set()
    finally:
        await part.runtime.close()


@pytest.mark.parametrize('url', ['about:blank', 'data:text/html,fixture', 'file:///tmp/fixture',
                                 'ftp://example.test', 'https://', 'https://example.test:bad'])
async def test_vault_focus_skips_non_web_and_malformed_targets(url):
    from misaka.core.web.browser.cdp import Supervisor

    supervisor = Supervisor()
    attached = []
    async def call(method, *args, **kwargs):
        assert method == 'Target.getTargets'
        return {'targetInfos': [{'targetId': 'bad', 'type': 'page', 'url': url},
                               {'targetId': 'good', 'type': 'page', 'url': 'https://example.test/login'}]}
    async def attach(target):
        attached.append(target)
        return target
    supervisor.call, supervisor.attach = call, attach
    result = await supervisor.focus_page('https://example.test', owned_browser=True)
    assert result['ok'] and attached == ['good'] and supervisor.root == 'good'


async def test_unlock_tokens_are_session_owned_and_released(part):
    def handler(args, **kw):
        unlock.store_session_token('fixture', 'fixture-session-token')
        assert unlock.is_unlocked('fixture')
        return 'ok'
    try:
        assert await part.runtime.run(host.invoke, handler, {}, part.runtime, None) == 'ok'
        # A sibling owner in this same profile does not borrow the token.
        from misaka.core.web.runtime import WebRuntime
        sibling = WebRuntime(part.scope)
        try:
            assert await sibling.run(host.invoke, lambda *a, **k: unlock.is_unlocked('fixture'), {}, sibling, None) is False
        finally:
            await sibling.close()
        assert any(owner == part.runtime.vault_id for owner in unlock._owner_session.values())
    finally:
        await part.runtime.close()
    assert part.runtime.vault_id not in unlock._owner_session.values()


def test_private_material_read_and_index_guards(tmp_path):
    from misaka.core.documents.index import ingest
    from misaka.core.tools.path_utils import resolve_read_path
    original = tmp_path / 'web-evidence/originals/original.md'
    original.parent.mkdir(parents=True)
    original.write_text('fixture-private')
    key = tmp_path / 'vault/vault.key'
    key.parent.mkdir()
    key.write_text('fixture-key')
    link = tmp_path / 'alias.md'
    link.symlink_to(original)
    for path in (original, key, link):
        with pytest.raises(ValueError, match='Private Web material'):
            resolve_read_path(str(path), str(tmp_path))
        with pytest.raises(ValueError, match='Private Web material'):
            ingest(str(path), workspace=str(tmp_path))
    public = tmp_path / 'vaults-notes/note.md'
    public.parent.mkdir()
    public.write_text('ordinary material')
    assert resolve_read_path(str(public), str(tmp_path)) == str(public)


def test_private_material_guard_checks_resolved_fallback_alias(tmp_path):
    from misaka.core.tools.path_utils import resolve_read_path

    private = tmp_path / 'vault/vault.key'
    private.parent.mkdir()
    private.write_text('fixture-key')
    (tmp_path / 'alias\u2019.md').symlink_to(private)
    with pytest.raises(ValueError, match='Private Web material'):
        resolve_read_path("alias'.md", str(tmp_path))


@pytest.mark.parametrize('name', settings.VAULT_TOOLS[1:])
def test_vault_mutation_is_classified_and_denied_in_plan(name, tmp_path):
    from misaka.core.subagent.policy import _permission_action
    assert _permission_action('plan', [], name, {}, str(tmp_path), frozenset({name}))[0] == 'deny'
    assert _permission_action('dontAsk', [], name, {}, str(tmp_path), frozenset({name}))[0] == 'deny'


async def test_external_manager_unlock_uses_native_owned_process(part, tmp_path, monkeypatch):
    from misaka.core.web.browser.vault.backends.bitwarden import BitwardenLoginBackend
    cli = tmp_path / 'bw'
    cli.write_text('#!' + sys.executable + '\nimport os,sys\n'
        'assert "fixture-master" not in repr(sys.argv)\n'
        'assert os.environ[sys.argv[sys.argv.index("--passwordenv")+1]] == "fixture-master"\n'
        'print("fixture-bw-session")\n')
    cli.chmod(0o700)
    backend = BitwardenLoginBackend({'binary_path': str(cli)})
    def handler(args, **kw):
        backend.unlock('fixture-master')
        assert backend.is_unlocked()
        assert unlock.get_session_token('bitwarden') == 'fixture-bw-session'
        return 'ok'
    try:
        assert await part.runtime.run(host.invoke, handler, {}, part.runtime, None) == 'ok'
        assert 'HERMES_BW_MASTER' not in os.environ
    finally:
        await part.runtime.close()


def test_native_browser_gate_exposes_all_five_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'executable', lambda name: '/fixture/agent-browser' if name == 'agent-browser' else None)
    with WebScope(str(tmp_path)).activate() as scope:
        scope.config, scope.environment = {'browser': {'backend': 'agent-browser'}}, {}
        assert set(settings.VAULT_TOOLS) <= settings.available_tools()
        scope.config['vault'] = {'enabled': False}
        assert not set(settings.VAULT_TOOLS) & settings.available_tools()
