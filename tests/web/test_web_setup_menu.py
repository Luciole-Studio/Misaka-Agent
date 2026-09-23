"""Real settings writers and both entrypoints, with no user state/network access."""
import stat
from types import SimpleNamespace

import pytest
from webconf import read_web

from misaka.cli import app, setup, web
from misaka.cli import setup_ui as ui
from misaka.cli import web_setup as menu
from misaka.config import home
from misaka.core.web import config, registry
from misaka.core.web.scope import WebScope


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MISAKA_HOME', str(tmp_path))
    for name in config.provider_variables():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ui, 'prompt_yes_no', lambda *a, **k: True)
    monkeypatch.setattr(ui, 'prompt', lambda *a, **k: '')
    with WebScope().activate():
        registry.ensure_backends_registered()
        yield


def pick(monkeypatch, *labels):
    remaining = iter(labels)

    def choice(question, choices, default=0, **kwargs):
        label = next(remaining)
        if isinstance(label, int):
            return label
        found = [i for i, text in enumerate(choices) if text == label or text.startswith(label)]
        assert len(found) == 1, (label, choices)
        return found[0]
    monkeypatch.setattr(ui, 'prompt_choice', choice)


def document():
    return config.load_config()


def test_bare_web_tty_and_setup_use_same_menu(monkeypatch):
    calls = []
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    monkeypatch.setattr(menu, 'configure', lambda: calls.append('menu'))
    app.main(['web'])
    app.main(['web', 'configure'])
    setup.Wizard().web()
    assert calls == ['menu'] * 3
    assert document() == {}


def test_pipe_keeps_status_and_explicit_menu_needs_tty(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    calls = []
    monkeypatch.setattr(web, '_status', lambda: calls.append('status'))
    app.main(['web'])
    assert calls == ['status']
    with pytest.raises(SystemExit) as error:
        app.main(['web', 'configure'])
    assert error.value.code == 2
    assert 'need a terminal' in capsys.readouterr().err
    assert document() == {}


def test_done_and_status_are_read_only(monkeypatch):
    pick(monkeypatch, 'Status', 'Done')
    menu.configure()
    assert document() == {}


def test_cancel_does_not_write_or_start_install(monkeypatch):
    pick(monkeypatch, 'Search provider', 'exa', 'Exa - Paid')
    monkeypatch.setattr(ui, 'prompt', lambda *a, **k: (_ for _ in ()).throw(ui.SetupCancelled()))
    with pytest.raises(ui.SetupCancelled):
        menu.configure()
    assert document() == {}


def test_setup_cancel_propagates_to_whole_wizard(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    monkeypatch.setattr(menu, 'configure', lambda: (_ for _ in ()).throw(ui.SetupCancelled()))
    with pytest.raises(ui.SetupCancelled):
        setup.Wizard().web()


@pytest.mark.parametrize('name', ['exa', 'parallel', 'keenable', 'tavily', 'firecrawl', 'brave-free', 'searxng', 'ddgs', 'xai', 'perplexity', 'nous'])
def test_all_registered_search_providers_have_a_working_setup_path(name, monkeypatch):
    provider = registry.get_provider(name)
    pick(monkeypatch, name, 0)
    monkeypatch.setattr(ui, 'prompt', lambda *a, **k: 'test-secret-credential')
    menu._search('search')
    assert document()['search_backend'] == name
    assert 'extract_backend' not in document()
    assert provider is not None


def test_paid_exa_is_masked_scoped_atomic_and_preserves_sibling(monkeypatch, tmp_path, capsys):
    config.update_config({'extract_backend': 'firecrawl', 'env.TAVILY_API_KEY': 'shared-other-secret'})
    profile = tmp_path / '10032'
    with WebScope(str(profile)).activate():
        registry.ensure_backends_registered()
        pick(monkeypatch, 'exa', 'Exa - Paid')
        monkeypatch.setattr(ui, 'prompt', lambda *a, **k: 'private-exa-secret')
        menu._search('search')
        doc = read_web(profile)
        assert doc['search_backend'] == 'exa'
        assert doc['provider_tier']['exa'] == 'paid'
        assert doc['env'] == {'EXA_API_KEY': 'private-exa-secret'}
        assert config.config_name('extract_backend') == 'firecrawl'
        assert 'extract_backend' not in doc
        assert stat.S_IMODE(home.path('env', profile).stat().st_mode) == 0o600
    output = capsys.readouterr()
    assert 'private-exa-secret' not in output.out + output.err
    assert 'shared-other-secret' not in output.out + output.err
    assert document()['env'] == {'TAVILY_API_KEY': 'shared-other-secret'}


def test_no_save_preserves_byte_identical_settings(monkeypatch):
    config.set_config('backend', 'tavily')
    before = home.path('settings').read_bytes()
    monkeypatch.setattr(ui, 'prompt_yes_no', lambda *a, **k: False)
    assert not menu.save({'backend': 'exa'})
    assert home.path('settings').read_bytes() == before


def test_free_tier_reenables_keyless_without_keys(monkeypatch):
    config.set_config('keyless_fallback', 'false')
    pick(monkeypatch, 'exa', 'Exa - Free')
    monkeypatch.setattr(ui, 'prompt', lambda *a, **k: pytest.fail('free tier must not prompt for credentials'))
    menu._search('both')
    assert document()['search_backend'] == document()['extract_backend'] == 'exa'
    assert document()['provider_tier']['exa'] == 'free'
    assert document()['keyless_fallback'] is True


def test_search_only_choices_do_not_offer_extract_incapable_providers(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, 'prompt_choice', lambda title, choices, *a, **k: seen.extend(choices) or 0)
    menu._search('extract')
    for provider in registry.list_providers():
        assert (provider.name in seen) == provider.supports_extract()


def test_auto_one_capability_preserves_other_and_both_masks_inherited_pins(monkeypatch, tmp_path):
    config.update_config({'backend': 'tavily', 'search_backend': 'exa', 'extract_backend': 'parallel', 'env.EXA_API_KEY': 'keep-me'})
    with WebScope(str(tmp_path / 'sis')).activate():
        registry.ensure_backends_registered()
        pick(monkeypatch, 'Use shared/default')
        menu._search('search')
        assert config.config_name('backend') == 'tavily'
        assert config.config_name('search_backend') == ''
        assert config.config_name('extract_backend') == 'parallel'
        pick(monkeypatch, 'Automatic routing')
        menu._search('both')
        assert not registry.selection_stored()
        assert config.has_env('EXA_API_KEY')
        assert '' not in config.web_config().get('provider_tier', {})
    assert document()['search_backend'] == 'exa'


@pytest.mark.parametrize('name', ['local', 'cdp', 'camofox', 'controller', 'browser-use', 'browserbase', 'firecrawl', 'nous'])
def test_browser_routes_clear_conflicting_inherited_bindings(name, monkeypatch, tmp_path):
    from misaka.core.web.browser import settings
    config.update_config({'browser.controller_command': '["old-controller"]', 'browser.cdp_url': 'ws://old.test/',
                          'env.BROWSER_CDP_URL': 'ws://old.test/', 'env.CAMOFOX_URL': 'http://camofox.test'})
    with WebScope(str(tmp_path / 'sis')).activate():
        registry.ensure_backends_registered()
        pick(monkeypatch, name, 0)
        value = '["new-controller"]' if name == 'controller' else 'ws://new.test/' if name == 'cdp' else ''
        monkeypatch.setattr(ui, 'prompt', lambda *a, **k: value)
        menu._browser()
        assert settings.route() == name
    assert document()['browser']['controller_command'] == ['old-controller']


def test_browser_env_override_blocks_misleading_route_switch(monkeypatch):
    monkeypatch.setenv('BROWSER_CDP_URL', 'ws://exported.test/')
    pick(monkeypatch, 'local')
    with pytest.raises(ValueError, match='overrides this route'):
        menu._browser()
    assert document() == {}


def test_cancel_cdp_and_lightpanda_do_not_write(monkeypatch):
    pick(monkeypatch, 'cdp')
    with pytest.raises(ValueError, match='explicit endpoint'):
        menu._browser()
    pick(monkeypatch, 'local', 'lightpanda')
    with pytest.raises(ValueError, match='executable path'):
        menu._browser()
    assert document() == {}


@pytest.mark.parametrize('key,value', [
    ('cache_ttl_minutes', '-1'), ('extract_char_limit', '1.5'), ('browser.backend', 'typo'),
    ('browser.engine', 'typo'), ('browser.controller_command', '[1]'),
    ('x_search.timeout_seconds', '2'), ('x_search.retries', '11'),
    ('operation_timeout.web_search', '-1'), ('http_timeout.default', '{"typo":2}'),
    ('backend', 'typo'), ('extract_backend', 'ddgs'), ('vault.onepassword', '[]'),
])
def test_invalid_advanced_values_never_write(key, value):
    with pytest.raises((ValueError, TypeError)):
        menu.save({key: value})
    assert document() == {}


def test_advanced_settings_roundtrip_and_unset_inherits(monkeypatch, tmp_path):
    config.set_config('cache_enabled', 'true')
    with WebScope(str(tmp_path / 'profile')).activate():
        registry.ensure_backends_registered()
        menu.save({'cache_enabled': 'false', 'operation_timeout.web_search': '400',
                   'http_timeout.default': '{"connect":5,"read":null}',
                   'browser.controller_command': '["controller", "serve"]',
                   'vault.bitwarden': '{"enabled":false}', 'website_blocklist.domains': 'ads.test,track.test'})
        assert config.web_config()['cache_enabled'] is False
        pick(monkeypatch, 'Remove this layer override')
        menu._edit('cache_enabled')
        assert config.web_config()['cache_enabled'] is True
        assert config.web_config()['http_timeout']['default']['read'] is None


def test_environment_input_hidden_and_override_reported(monkeypatch, capsys):
    config.set_config('env.EXA_API_KEY', 'existing-secret')
    monkeypatch.setenv('EXA_API_KEY', '')
    pick(monkeypatch, 'Change')
    def prompt(*args, **kwargs):
        assert kwargs.get('password') is True
        return 'replacement-secret'
    monkeypatch.setattr(ui, 'prompt', prompt)
    menu._edit('env.EXA_API_KEY')
    output = capsys.readouterr().out
    assert 'overrides this file' in output
    assert 'existing-secret' not in output and 'replacement-secret' not in output


def test_no_implicit_install_login_or_remote_check(monkeypatch):
    from misaka.cli import web_browser
    monkeypatch.setattr(web, '_post_setup', lambda *a: pytest.fail('unexpected install'))
    monkeypatch.setattr(web, '_accounts', lambda *a: pytest.fail('unexpected login'))
    monkeypatch.setattr(web_browser, 'run', lambda *a: pytest.fail('unexpected external operation'))
    monkeypatch.setattr(ui, 'prompt_yes_no', lambda *a, **k: False)
    pick(monkeypatch, 'ddgs')
    menu._install()
    pick(monkeypatch, 'Check Nous')
    menu._accounts()
    pick(monkeypatch, 'Sign in to xAI')
    menu._accounts()


def test_install_and_oauth_delegation_after_confirmation(monkeypatch):
    from misaka.cli import web_browser
    seen = []
    monkeypatch.setattr(web, '_post_setup', lambda row, args: seen.append(('install', args.key, args.install)))
    monkeypatch.setattr(web, '_accounts', lambda args: seen.append(('xai', args.op, args.key)))
    monkeypatch.setattr(web_browser, 'run', lambda args: seen.append(('browser', args.op, args.key)))
    pick(monkeypatch, 'ddgs')
    menu._install()
    pick(monkeypatch, 'browser-use')
    menu._install()
    pick(monkeypatch, 'Sign in to xAI')
    menu._accounts()
    assert seen == [('install', 'ddgs', True), ('browser', 'browser-install', 'browser-use'), ('xai', 'login', None)]


def test_all_core_setting_sections_and_scalars_are_reachable():
    fields = {key for values in menu.groups().values() for key in values}
    assert config._SCALAR_KEYS | config._BOOL_KEYS | config._LIST_KEYS <= fields
    assert config._NESTED_KEYS - {'env'} <= {key.split('.')[0] for key in fields if '.' in key}
    assert {'browser.' + key for key in menu.BROWSER_KEYS} <= fields


def test_menu_error_is_recoverable_and_back_returns_to_menu(monkeypatch):
    pick(monkeypatch, 'Browser connection', 'cdp', 'Done')
    menu.configure()  # Missing endpoint is shown, not an exit or a partial save.
    assert document() == {}
    pick(monkeypatch, 'Search provider', 'Done')
    monkeypatch.setattr(menu, '_search', lambda *a: (_ for _ in ()).throw(ui.SetupGoBack()))
    menu.configure()


def test_corrupt_config_is_preserved(tmp_path):
    path = home.path('settings')
    path.write_text('{broken')
    with pytest.raises(ValueError):
        menu.save({'backend': 'exa'})
    assert path.read_text() == '{broken'


def test_extension_provider_and_credentials_are_discovered(monkeypatch):
    from misaka.core.web.provider import WebSearchProvider
    class Custom(WebSearchProvider):
        name = 'extension-example'
        def is_available(self):
            return config.has_env('EXTENSION_TOKEN')
        def get_setup_schema(self):
            return {'name': self.name, 'env_vars': [{'key': 'EXTENSION_TOKEN'}]}
    registry.replace_extension_providers([SimpleNamespace(path='fixture', webProviders={'extension-example': Custom()})])
    assert 'EXTENSION_TOKEN' in config.provider_variables()
    pick(monkeypatch, 'extension-example', 0)
    monkeypatch.setattr(ui, 'prompt', lambda *a, **k: 'extension-secret')
    menu._search('search')
    assert document()['search_backend'] == 'extension-example'


def test_vault_fields_do_not_need_json_or_copy_shared_settings(monkeypatch, tmp_path):
    config.set_config('vault.onepassword', '{"account":"shared","binary_path":"/shared/op"}')
    with WebScope(str(tmp_path / 'profile')).activate():
        registry.ensure_backends_registered()
        pick(monkeypatch, 'enabled', 'Change')
        menu._edit('vault.onepassword')
        own = read_web(tmp_path / 'profile')
        assert own['vault']['onepassword'] == {'enabled': True}
        assert config.web_config()['vault']['onepassword']['account'] == 'shared'


def test_menu_uses_no_network_during_real_entrypoint_setup(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('unexpected network request'))
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    pick(monkeypatch, 'Search and extraction together', 'exa', 'Exa - Free', 'Done')
    app.main(['web'])
    assert document()['search_backend'] == document()['extract_backend'] == 'exa'
