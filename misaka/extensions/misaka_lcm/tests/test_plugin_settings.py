"""Plugin-only settings, native dialogs, and ephemeral original recall policy."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import (
    config_bridge,
    preanswer,
    settings,
    storage,
)
from misaka.extensions.misaka_lcm.host import context_engine as ce


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    ce.close_all()
    monkeypatch.setenv('MISAKA_HOME', str(tmp_path))
    for spec in settings._FIELDS.values():
        monkeypatch.delenv(spec.env_key, raising=False)
    yield
    ce.close_all()


def context(tmp_path):
    return SimpleNamespace(cwd=str(tmp_path), model=None,
                           sessionManager=SessionManager.inMemory(str(tmp_path)))


def test_persist_refresh_false_values_and_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'check', lambda config: 384)
    ctx = context(tmp_path)
    built = ce.bound_engine(ctx)
    assert built._config.proactive_recall_enabled is False
    ctx.sessionManager.appendMessage({'role': 'user', 'content': 'keep cursor', 'timestamp': 1})
    ce.sync(ctx)
    cursor = built._ingest_cursor
    config = settings.configure(True)
    assert config.proactive_recall_enabled and config.embeddings_enabled
    assert config.embedding_provider == 'fastembed'
    assert settings.path().stat().st_mode & 0o777 == 0o600
    assert ce.bound_engine(ctx) is built and built._ingest_cursor == cursor
    assert built._config.proactive_recall_enabled
    settings.configure(False)
    assert not ce.bound_engine(ctx)._config.proactive_recall_enabled
    assert ce.bound_engine(ctx)._config.embeddings_enabled  # manual semantic tools remain enabled
    saved = settings.path().read_bytes()
    monkeypatch.setenv('LCM_PROACTIVE_RECALL_ENABLED', '0')
    assert not config_bridge.load_config(ctx=ctx).proactive_recall_enabled
    with pytest.raises(ValueError, match='environment overrides'):
        settings.configure(True)
    assert settings.path().read_bytes() == saved
    ce.close(ctx)
    ce.release_project(ctx)
    assert not storage.directory(storage.project(ctx)).exists()
    assert settings.path().read_bytes() == saved  # preferences are not task content


def test_failed_readiness_and_bad_json_do_not_overwrite_settings(monkeypatch):
    def fail(config):
        raise RuntimeError('fixture missing model')
    monkeypatch.setattr(settings, 'check', fail)
    with pytest.raises(RuntimeError, match='missing model'):
        settings.configure(True)
    assert not settings.path().exists()
    settings.path().write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        settings.configure(False)
    assert settings.path().read_text() == '{broken'


@pytest.mark.parametrize('saved', [[], {'proactive_recall_enabled': 'false'}, {'database_path': 'outside'}])
def test_strict_plugin_settings_schema(saved):
    settings.path().parent.mkdir(parents=True)
    settings.path().write_text(json.dumps(saved))
    with pytest.raises((ValueError, TypeError)):
        config_bridge.load_config()


async def test_menu_registered_and_toggles_through_extension_api(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host.extension import register
    commands = {}
    harn = SimpleNamespace(registerProvider=lambda *_: None, registerTool=lambda *_: None,
                           on=lambda *_: None, registerCommand=lambda name, spec: commands.__setitem__(name, spec))
    register(harn, kind='foreground', workspace=str(tmp_path))
    assert 'lcm-settings' in commands  # independent of LCM_ENABLE_SLASH_COMMAND
    notices, menus = [], []
    async def select(title, options):
        menus.append((title, options))
        return options[0]
    monkeypatch.setattr(settings, 'check', lambda config: 384)
    ctx = context(tmp_path)
    ctx.hasUI, ctx.mode = True, 'tui'
    ctx.ui = SimpleNamespace(select=select, notify=lambda text, kind: notices.append((text, kind)))
    command = commands['lcm-settings']['handler']
    await command('', ctx)
    assert 'Enable automatic recall' in menus[-1][1]
    assert settings.read()['proactive_recall_enabled'] is True
    await command('', ctx)
    assert 'Disable automatic recall' in menus[-1][1]
    assert settings.read()['proactive_recall_enabled'] is False
    await command('status', ctx)
    assert notices[-1][1] == 'info' and 'automatic recall: Off' in notices[-1][0]
    await command('unknown', ctx)
    assert notices[-1][1] == 'error'


async def test_headless_menu_and_cancel_have_no_setting_side_effect(tmp_path):
    commands, notices = {}, []
    settings.register(SimpleNamespace(registerCommand=lambda name, spec: commands.__setitem__(name, spec)), workspace=str(tmp_path))
    ctx = context(tmp_path)
    async def select(*_):
        return None
    ctx.ui = SimpleNamespace(select=select, notify=lambda *args: notices.append(args))
    ctx.hasUI, ctx.mode = False, 'print'
    await commands['lcm-settings']['handler']('', ctx)
    assert notices and not settings.path().exists()
    ctx.hasUI, ctx.mode = True, 'tui'
    await commands['lcm-settings']['handler']('', ctx)
    assert not settings.path().exists()


def test_native_request_reuses_original_recall_without_compaction_or_persistence(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.vendor import tools as upstream_tools
    monkeypatch.setattr(settings, 'check', lambda config: 384)
    monkeypatch.setattr(preanswer, '_local_index', lambda built: None)
    settings.configure(True)
    ctx = context(tmp_path)
    ctx.sessionManager.appendMessage({'role': 'user', 'content': 'find evidence', 'timestamp': 1})
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    calls = []
    def recall(*args, **kwargs):
        calls.append(args)
        return json.dumps({'hits': [{'from_current_session': False, 'score': 0.5,
                                     'snippet': 'OTHER_SESSION_EVIDENCE', 'timestamp': 1}]})
    monkeypatch.setattr(upstream_tools, 'lcm_recall', recall)
    messages = ctx.sessionManager.buildSessionContext().messages
    before = json.dumps(ctx.sessionManager.getEntries(), sort_keys=True)
    assert ce.prepare({'messages': messages, 'reason': 'auto', 'allowCompression': False}, ctx) is None
    for _ in range(2):
        result = preanswer.inject({'messages': messages}, ctx, active_tools=['lcm_recall'])
        assert 'OTHER_SESSION_EVIDENCE' in str(result)
        assert 'UNTRUSTED-DATA' in str(result)
    assert len(calls) == 1
    assert json.dumps(ctx.sessionManager.getEntries(), sort_keys=True) == before
    assert built._store._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    # The durable assembler does not retrieve/store a second copy of this block.
    assembled = built._assemble_context({'role': 'system', 'content': 'fixture'}, [{'role': 'user', 'content': 'find evidence'}])
    assert 'OTHER_SESSION_EVIDENCE' not in str(assembled) and len(calls) == 1
    settings.configure(False)
    assert preanswer.inject({'messages': messages}, ctx, active_tools=['lcm_recall']) is None
    assert len(calls) == 1


async def test_cancelled_setting_probe_does_not_save(tmp_path, monkeypatch):
    import threading

    from misaka.extensions.misaka_lcm.host import execution
    entered, release = threading.Event(), threading.Event()
    def probe(config):
        entered.set()
        release.wait(3)
        return 384
    monkeypatch.setattr(settings, 'check', probe)
    task = asyncio.create_task(execution.off_loop(settings.configure, True, ctx=context(tmp_path)))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)  # Deliver cancellation before letting the probe reach its commit check.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not settings.path().exists()


def test_local_index_skips_cloud_and_off_modes(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.vendor import command
    def forbidden(*args, **kwargs):
        pytest.fail('Automatic raw-text indexing reached a disabled or cloud route')
    monkeypatch.setattr(command, '_embedding_backfill_text', forbidden)
    built = ce.bound_engine(context(tmp_path))
    preanswer._local_index(built)
    built._config.proactive_recall_enabled = True
    built._config.embeddings_enabled = True
    built._config.embedding_provider = 'voyage'
    preanswer._local_index(built)
    assert not getattr(built, '_misaka_index_busy', False)


def test_cached_local_model_indexes_and_recalls_without_network(tmp_path, monkeypatch):
    import importlib.util
    import socket
    import time

    from misaka.extensions.misaka_lcm.vendor.embedding_provider import (
        FastembedProvider,
        ProviderNotWarmedUp,
    )
    if importlib.util.find_spec('fastembed') is None:
        pytest.skip('optional local FastEmbed dependency is absent')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    def no_network(*args, **kwargs):
        raise AssertionError('local recall must not open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    try:
        FastembedProvider(settings._LOCAL_MODEL, timeout=10).embed_query('readiness')
    except ProviderNotWarmedUp:
        pytest.skip('optional local model is not cached; tests never download it')
    settings.configure(True)
    old, current = context(tmp_path), context(tmp_path)
    evidence = ('Lantern orchard copper. The project canary is LOCAL_PROBE_92831. '
                + 'This project records source evidence and validates the copper lantern orchard design. ' * 4)
    for ctx, text in [(old, evidence), (current, 'Lantern orchard copper')]:
        ctx.sessionManager.appendMessage({'role': 'user', 'content': text, 'timestamp': int(time.time() * 1000)})
        ce.sync(ctx)
    messages = current.sessionManager.buildSessionContext().messages
    original = json.dumps(current.sessionManager.getEntries(), sort_keys=True)
    assert ce.prepare({'messages': messages, 'reason': 'auto', 'allowCompression': False}, current) is None
    result = preanswer.inject({'messages': messages}, current, active_tools=['lcm_recall'])
    assert 'LOCAL_PROBE_92831' in str(result)
    assert ce.bound_engine(current).compression_count == 0
    assert ce.bound_engine(current)._proactive_recall_injected_count == 1
    assert preanswer.inject({'messages': messages}, current, active_tools=['lcm_recall']) == result
    assert ce.bound_engine(current)._proactive_recall_injected_count == 1
    assert json.dumps(current.sessionManager.getEntries(), sort_keys=True) == original
    assert ce.bound_engine(current)._store._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
    settings.configure(False)
    assert preanswer.inject({'messages': messages}, current, active_tools=['lcm_recall']) is None
    ce.close_all()
    assert not storage.directory(tmp_path).exists()
    assert settings.path().exists()


def test_index_timeout_keeps_owner_and_freezes_worker_config(tmp_path, monkeypatch):
    import threading

    from misaka.extensions.misaka_lcm.host import execution
    from misaka.extensions.misaka_lcm.vendor import command, embedding_provider, tools
    monkeypatch.setattr(settings, 'check', lambda config: 2)
    settings.configure(True)
    ctx = context(tmp_path)
    built = ce.bound_engine(ctx)
    provider = SimpleNamespace(provider_id='fastembed', model_id=settings._LOCAL_MODEL,
                               embed_query=lambda _: [1.0, 0.0])
    monkeypatch.setattr(embedding_provider, 'resolve_provider', lambda _: provider)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def backfill(args, worker):
        observed.append(args)
        entered.set()
        release.wait(5)
        observed.append(worker._config.proactive_recall_enabled)
        return 'fixture complete'
    monkeypatch.setattr(command, '_embedding_backfill_text', backfill)
    original = tools._run_within_deadline
    monkeypatch.setattr(tools, '_run_within_deadline',
                        lambda fn, **kwargs: original(fn, **{**kwargs, 'remaining_s': 0.05}))
    with execution.worker_owner(built):
        preanswer._local_index(built)
    assert entered.is_set() and built._misaka_index_busy
    settings.configure(False)
    assert not ce.bound_engine(ctx)._config.proactive_recall_enabled
    def quit():
        ce.close(ctx)
        ce.release_project(ctx)
        closed.set()
    closer = threading.Thread(target=quit)
    closer.start()
    try:
        assert not closed.wait(0.03)
        assert storage.directory(tmp_path).exists()
    finally:
        release.set()
        closer.join(5)
    assert closed.is_set() and not built._misaka_index_busy
    assert observed == [['--corpus', 'both', '--apply', '--limit', '32'], True]
    assert not storage.directory(tmp_path).exists()
