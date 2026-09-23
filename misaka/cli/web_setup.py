"""One settings menu for ``misaka web`` and the Web section of ``misaka setup``.

Use the existing provider schemas, writers, login and install commands. Merely opening
this menu neither writes a config nor connects to a service.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import httpx

from misaka.cli import setup_ui as ui
from misaka.core.web import config, dispatch, registry
from misaka.core.web.scope import current_scope

# Names here are runtime settings, not another provider list. Providers and credentials
# come from the registries so trusted extensions get the same setup path as built-ins.
BROWSER_KEYS = (
    'enabled', 'cloud_provider', 'backend', 'engine', 'command', 'exec_command',
    'executable_path', 'lightpanda_path', 'headed', 'use_real_profile', 'real_profile_path',
    'cdp_url', 'controller_command', 'controller_capabilities', 'vision_model',
    'command_timeout', 'open_timeout', 'record_sessions', 'recording_retention',
    'camofox_managed_persistence', 'camofox_adopt_existing_tab', 'camofox_rewrite_loopback_urls',
    'camofox_session_key', 'camofox_loopback_host_alias',
)
EXTRA_ENV = (
    'BROWSERBASE_KEEP_ALIVE', 'BROWSERBASE_PROXIES', 'BROWSERBASE_ADVANCED_STEALTH',
    'BROWSERBASE_SESSION_TIMEOUT', 'FIRECRAWL_BROWSER_TTL', 'CAMOFOX_SESSION_KEY',
    'OP_SERVICE_ACCOUNT_TOKEN', 'OP_CONNECT_HOST', 'OP_CONNECT_TOKEN',
)
CHOICES = {
    'browser.backend': ('auto', 'off', 'agent-browser', 'browser-use'),
    'browser.engine': ('auto', 'chrome', 'lightpanda'),
    'x_search.reasoning_effort': ('low', 'medium', 'high', 'xhigh'),
}


def arguments(op, key=None, **kwargs):
    return SimpleNamespace(op=op, key=key, value=None, profile=current_scope().profile_dir,
                           extension=[], capability=None, tier=None, install=False,
                           login=False, yes=False, **kwargs)


def choose(title, names):
    index = ui.prompt_choice(title, ['Back', *names])
    return index - 1 if index else None


def _value(key):
    value = config.web_config(strict=True)
    for part in key.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _sensitive(key):
    return key.startswith(('env.', 'vault.')) or key in {
        'browser.cdp_url', 'browser.controller_command', 'browser.camofox_session_key',
    }


def _validate(document):
    """Validate proposed values without writing or doing a network readiness test."""
    from misaka.core.web.browser import settings
    from misaka.core.web.timeouts import (
        OPERATION_DEFAULTS,
        _seconds,
        http_timeout,
        operation_seconds,
    )

    for section in config._NESTED_KEYS:
        if section in document and not isinstance(document[section], dict):
            raise ValueError(f'{section} must be an object')
    for key in ('cache_ttl_minutes', 'extract_char_limit', 'extract_timeout'):
        if key in document:
            value = document[key]
            if isinstance(value, bool):
                raise ValueError(f'{key} takes a positive number')
            _seconds(float(value), key, positive=True)
            # The runtime uses int(), not float(): "2000.0" otherwise silently
            # falls back to 15000 despite a successful-looking save.
            if key == 'extract_char_limit' and int(value) != float(value):
                raise ValueError('extract_char_limit takes an integer')
    for key in ('backend', 'search_backend', 'extract_backend'):
        if name := document.get(key):
            provider = registry.get_provider(name, include_disabled=True)
            if provider is None:
                raise ValueError(f'Unknown Web provider: {name}')
            if key != 'backend' and not getattr(provider, 'supports_' + key.removesuffix('_backend'))():
                raise ValueError(f'{name} does not support {key.removesuffix("_backend")}')
    scope = replace(current_scope(), config=document, config_error=None)
    with scope.activate():
        http_timeout('default', 60)
        for key in OPERATION_DEFAULTS:
            operation_seconds(key)
        cfg = settings.config()
        for key, values in CHOICES.items():
            section, field = key.split('.')
            value = document.get(section, {}).get(field)
            if value is not None and value not in values:
                raise ValueError(f'{key} takes {", ".join(values)}')
        from misaka.core.web.browser.providers import providers
        if cfg.get('cloud_provider') not in {None, 'local', 'cdp', 'camofox', *providers()}:
            raise ValueError('Unknown browser.cloud_provider')
        x = document.get('x_search', {})
        if 'timeout_seconds' in x:
            _seconds(x['timeout_seconds'], 'x_search.timeout_seconds', positive=True)
            if x['timeout_seconds'] < 30:
                raise ValueError('x_search.timeout_seconds must be >= 30')
        if 'retries' in x and (type(x['retries']) is not int or not 0 <= x['retries'] <= 10):
            raise ValueError('x_search.retries must be an integer from 0 to 10')
        if 'xai' in document and 'timeout' in document['xai']:
            _seconds(float(document['xai']['timeout']), 'xai.timeout', positive=True)
        vault = document.get('vault', {})
        for name in ('onepassword', 'bitwarden'):
            if name in vault:
                if not isinstance(vault[name], dict):
                    raise ValueError(f'vault.{name} must be an object')
                if 'enabled' in vault[name] and type(vault[name]['enabled']) is not bool:
                    raise ValueError(f'vault.{name}.enabled must be true or false')


def save(changes, *, remove=()):
    # Build the preview from layers, not from effective settings: deleting a profile
    # override must reveal the inherited value, just as the real writer does.
    from misaka.core.settings_manager import deep_merge_settings

    own = config.own_section()
    for key in remove:
        parts = key.split('.')
        parent = own if len(parts) == 1 else own.get(parts[0], {})
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
    for key, value in changes.items():
        config._set_value(own, key, value)
    shared = config.load_config() if current_scope().profile_dir else {}
    prospective = deep_merge_settings(shared, own)
    _validate(prospective)
    ui.print_info('Pending settings: ' + ', '.join([*changes, *(f'remove {key}' for key in remove)]))
    with replace(current_scope(), config=prospective, config_error=None).activate():
        for label, resolve in [('Search', dispatch.resolve_provider), ('Extraction', dispatch.resolve_extractor)]:
            provider, backend, error = resolve()
            if error or not registry.provider_is_ready(provider):
                ui.print_warning(f'{label}: {backend or "none"} is not ready locally; credentials or installation may be missing.')
    if not ui.prompt_yes_no('Save these settings? (No requests are sent)', False):
        return False
    path = config.update_config(changes, remove=tuple(remove))
    ui.print_success(f'Saved in {path}. Account access and network availability have not been tested.')
    return True


def _edit(key):
    if key in {'vault.onepassword', 'vault.bitwarden'}:
        _vault(key)
        return
    value = _value(key)
    shown = 'not set (inherits defaults)' if value is None else (
        'configured (hidden)' if _sensitive(key) else config.redact_secrets(json.dumps(value, ensure_ascii=False)))
    ui.print_info(f'{key}: {shown}')
    action = ui.prompt_choice('Setting', ['Keep', 'Change', 'Remove this layer override', 'Set empty value'])
    if action == 0:
        return
    if action == 2:
        save({}, remove=(key,))
        return
    boolean = key in config._BOOL_KEYS or tuple(key.split('.')) in config._NESTED_BOOL_KEYS or key == 'vault.enabled'
    if action == 3:
        if boolean or key in CHOICES or key.startswith(('provider_tier.', 'http_timeout.', 'operation_timeout.')):
            raise ValueError('Use Change or Remove for this setting')
        raw = '[]' if key in {'browser.controller_command', 'browser.controller_capabilities'} else ''
    elif boolean:
        raw = 'true' if ui.prompt_yes_no(key, bool(value)) else 'false'
    elif key in CHOICES or key.startswith('provider_tier.'):
        values = CHOICES.get(key, ('auto', 'free', 'paid'))
        raw = values[ui.prompt_choice(key, list(values), values.index(value) if value in values else 0)]
    else:
        if key in config._LIST_KEYS or tuple(key.split('.')) in config._NESTED_LIST_KEYS:
            ui.print_info('Use comma-separated entries.')
        elif key in {'browser.controller_command', 'browser.controller_capabilities'}:
            ui.print_info('Use a JSON array of strings, e.g. ["program", "argument"]. No shell expansion.')
        elif key.startswith('http_timeout.'):
            ui.print_info('Seconds, null, or a JSON object with connect/read/write/pool; ddgs needs positive seconds.')
        elif key.startswith('vault.') and key != 'vault.enabled':
            ui.print_info('JSON object: enabled, binary_path; 1Password also account, service_account_token_env. Enter keeps current.')
        raw = ui.prompt(f'{key} (Enter keeps current)', password=_sensitive(key))
        if not raw:
            return
    if _sensitive(key):
        config.remember_secret(raw)
    if key.startswith('env.') and key[4:] in os.environ:
        ui.print_warning('A process environment value overrides this file, even when its export is empty.')
    save({key: raw})


def _vault(key):
    name = key.split('.')[1]
    fields = ['enabled', 'binary_path'] + (['account', 'service_account_token_env'] if name == 'onepassword' else [])
    index = choose(key, fields)
    if index is None:
        return
    field = fields[index]
    action = ui.prompt_choice('Setting', ['Keep', 'Change', 'Remove this layer override'])
    if action == 0:
        return
    own = config.own_section().get('vault', {}).get(name, {})
    own = dict(own)
    if action == 2:
        own.pop(field, None)
    elif field == 'enabled':
        own[field] = ui.prompt_yes_no(f'Enable {name}?', (_value(key) or {}).get('enabled', True))
    else:
        value = ui.prompt(f'{key}.{field} (Enter keeps current)')
        if not value:
            return
        own[field] = value
    save({key: json.dumps(own)})


def _search(capability):
    from misaka.cli.web import _setup

    caps = ('search', 'extract') if capability == 'both' else (capability,)
    providers = [p for p in registry.list_providers(include_disabled=True)
                 if all(getattr(p, 'supports_' + cap)() for cap in caps)]
    automatic = ('Automatic routing (credentials first, then free fallback)' if capability == 'both'
                 else 'Use shared/default routing')
    index = choose('Provider', [automatic,
                               *[p.name + (' [disabled]' if config.provider_disabled(p.name) else '') for p in providers]])
    if index is None:
        return
    if index == 0:
        # Empty scalars mask inherited capability pins. One capability falls through
        # to the shared backend; resetting both also clears that shared selection.
        changes = {f'{cap}_backend': '' for cap in caps}
        if capability == 'both':
            changes['backend'] = ''
        save(changes)
        return
    provider = providers[index - 1]
    if config.provider_disabled(provider.name):
        ui.print_warning('Enable this provider in Provider enable/disable first.')
        return
    args = arguments('setup', provider.name)
    args.capability = capability
    _setup(args, interactive=True)


def _browser():
    from misaka.core.web.browser.providers import providers

    names = ['local', 'cdp', 'camofox', 'controller', *providers()]
    index = choose('Browser connection', ['Disable browser tools', *names])
    if index is None:
        return
    if index == 0:
        save({'browser.enabled': 'false'})
        return
    name = names[index - 1]
    if name != 'cdp' and os.environ.get('BROWSER_CDP_URL'):
        raise ValueError('BROWSER_CDP_URL is exported and overrides this route; unset it in the launching shell first.')
    changes = {'browser.enabled': 'true', 'browser.controller_command': '[]',
               'browser.cdp_url': '', 'env.BROWSER_CDP_URL': '',
               'browser.cloud_provider': 'local' if name == 'controller' else name}
    if name == 'controller':
        command = ui.prompt('Controller argv as a JSON array (Enter cancels)', password=True)
        if not command:
            return
        changes['browser.controller_command'] = command
    elif name == 'cdp':
        endpoint = ui.prompt('Explicit CDP discovery/WebSocket URL (Enter keeps current)', password=True)
        endpoint = endpoint or config.provider_env('BROWSER_CDP_URL') or _value('browser.cdp_url')
        if not endpoint:
            raise ValueError('CDP needs an explicit endpoint; nothing saved.')
        from urllib.parse import urlsplit
        if urlsplit(endpoint).scheme not in {'http', 'https', 'ws', 'wss'} or not urlsplit(endpoint).hostname:
            raise ValueError('CDP needs an HTTP(S) or WS(S) endpoint')
        config.remember_secret(endpoint)
        changes['browser.cdp_url'] = endpoint
    elif name == 'local':
        keys = CHOICES['browser.engine']
        changes['browser.engine'] = keys[ui.prompt_choice('Local browser engine', list(keys))]
        if changes['browser.engine'] == 'lightpanda':
            path = ui.prompt('Lightpanda executable path (Enter keeps current)') or _value('browser.lightpanda_path')
            if not path:
                raise ValueError('Lightpanda needs an executable path; nothing saved.')
            changes['browser.lightpanda_path'] = path
            changes.update({'browser.headed': 'false', 'browser.use_real_profile': 'false'})
    else:
        from misaka.cli.web import _provider_rows
        rows = _provider_rows(providers()[name]) if name != 'camofox' else [
            {'name': 'Camofox', 'env_vars': [{'key': key} for key in ('CAMOFOX_URL', 'CAMOFOX_API_KEY', 'CAMOFOX_USER_ID')]}]
        row = rows[ui.prompt_choice('Browser service', [r['name'] for r in rows])]
        if row.get('tag'):
            ui.print_info(row['tag'])
        for variable in row.get('env_vars', []):
            key = variable['key']
            value = ui.prompt(f'{variable.get("prompt", key)} (Enter keeps current)', password=True)
            if value:
                config.remember_secret(value)
                changes['env.' + key] = value
                if key in os.environ:
                    ui.print_warning(f'{key} is overridden by the process environment.')
    save(changes)


def _providers():
    names = [p.name for p in registry.list_providers(include_disabled=True)]
    index = choose('Provider enable/disable', [name + (' [disabled]' if config.provider_disabled(name) else ' [enabled]') for name in names])
    if index is None:
        return
    name = names[index]
    enabled = not config.provider_disabled(name)
    if ui.prompt_yes_no(('Disable ' if enabled else 'Enable ') + name + '?', False):
        config.set_provider_enabled(name, not enabled)
        ui.print_success('Updated. Disabling a selected provider does not silently select a replacement.')


def groups():
    from misaka.core.web.browser.providers import providers
    from misaka.core.web.network import PROXY_VARIABLES, TLS_VARIABLES
    from misaka.core.web.timeouts import OPERATION_DEFAULTS

    names = [p.name for p in registry.list_providers(include_disabled=True)]
    http_names = ['default', *names, 'exa-mcp', 'parallel-mcp', 'keenable-keyless', 'nous-oauth',
                  'nous-account', 'camofox', 'browser-cdp-discovery', *('browser-' + n for n in providers())]
    return {
        'Routing, fallback, cache and extraction': sorted(config._SCALAR_KEYS | config._BOOL_KEYS | config._LIST_KEYS),
        'Service tiers': ['provider_tier.' + name for name in names],
        'Browser engines, profiles and recording': ['browser.' + key for key in BROWSER_KEYS],
        'Network proxy and TLS': ['env.' + name for name in (*PROXY_VARIABLES, *TLS_VARIABLES)],
        'Website blocklist': ['website_blocklist.' + key for key in ('enabled', 'domains', 'shared_files')],
        'HTTP timeouts': ['http_timeout.' + name for name in dict.fromkeys(http_names)],
        'Whole-operation timeouts': ['operation_timeout.' + name for name in OPERATION_DEFAULTS],
        'xAI web search': ['xai.' + key for key in ('model', 'timeout', 'allowed_domains', 'excluded_domains')],
        'X search': ['x_search.' + key for key in ('model', 'reasoning_effort', 'timeout_seconds', 'retries')],
        'Browser password vault': ['vault.' + key for key in ('enabled', 'onepassword', 'bitwarden')],
    }


def _fields(title, keys):
    index = choose(title, keys)
    if index is not None:
        _edit(keys[index])


def _advanced():
    fields = groups()
    names = [*fields, 'Other supported setting / extension setting']
    index = choose('Advanced settings', names)
    if index is None:
        return
    if index == len(fields):
        key = ui.prompt('Setting key (Enter cancels)')
        if key:
            _edit(key)
    else:
        _fields(names[index], fields[names[index]])


def _credentials():
    names = sorted(config.provider_variables() | set(EXTRA_ENV) | set(config.web_config().get('env', {})))
    _fields('Credentials and endpoints (values hidden)', ['env.' + name for name in names])


def _accounts():
    from misaka.cli import web, web_browser

    operations = ['accounts', 'login', 'logout', 'gateway-login', 'gateway-logout', 'gateway-status']
    labels = ['List xAI accounts (local)', 'Sign in to xAI (network)', 'Remove an xAI account (local)',
              'Sign in to Nous (network)', 'Remove Nous login (local)', 'Check Nous entitlement (network)']
    index = choose('Accounts and login', labels)
    if index is None:
        return
    op = operations[index]
    account = ui.prompt('xAI account label (Enter = default)') or None if op in {'login', 'logout'} else None
    if op != 'accounts' and not ui.prompt_yes_no(labels[index] + '?', False):
        return
    args = arguments(op, account)
    (web_browser.run if op.startswith('gateway-') else web._accounts)(args)


def _install():
    from misaka.cli import web, web_browser

    names = ['ddgs', 'agent-browser', 'browser-use']
    index = choose('Install optional tools (downloads packages)', names)
    if index is None or not ui.prompt_yes_no(f'Download and install {names[index]}?', False):
        return
    if names[index] == 'ddgs':
        args = arguments('setup', 'ddgs')
        args.install = True
        web._post_setup({'post_setup': 'ddgs'}, args)
    else:
        args = arguments('browser-install', names[index])
        args.yes = True
        web_browser.run(args)


def configure():
    from misaka.cli.web import _status

    ui.print_header('Web tools settings')
    ui.print_info(f'Editing: {config._config_path()}',
                  'Shared settings apply to Last Order and Sisters unless overridden by their profile.',
                  'Use --profile DIR to edit only that profile; removing an override reveals shared settings.',
                  'Nothing is sent or installed unless you explicitly choose it. Values save per confirmed action.')
    actions = [('Search provider', lambda: _search('search')),
               ('Page extraction provider', lambda: _search('extract')),
               ('Search and extraction together', lambda: _search('both')),
               ('Browser connection', _browser), ('Provider enable/disable', _providers),
               ('Credentials and endpoints', _credentials), ('Advanced settings', _advanced),
               ('Accounts and login', _accounts), ('Install optional tools', _install),
               ('Status (local configuration only)', _status)]
    while True:
        selected = ui.prompt_choice('Web tools', ['Done', *[name for name, _action in actions]])
        if not selected:
            return
        try:
            actions[selected - 1][1]()
        except ui.SetupGoBack:
            continue
        except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, httpx.HTTPError) as error:
            ui.print_error(config.redact_secrets(str(error)))
