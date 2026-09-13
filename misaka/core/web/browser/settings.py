"""Cheap browser configuration and executable discovery, with no implicit installation."""
import os
import shutil
from pathlib import Path

from misaka.config import get_agent_dir
from misaka.core.web.config import provider_env, web_config
from misaka.core.web.scope import current_scope

BASE_TOOLS = ('browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_scroll',
              'browser_back', 'browser_press', 'browser_get_images', 'browser_vision', 'browser_console')
CDP_TOOLS = ('browser_cdp', 'browser_dialog')


def config():
    cfg = web_config(strict=True).get('browser', {})
    if not isinstance(cfg, dict):
        raise ValueError('browser configuration must be an object')  # noqa: TRY004 - surface protocol/configuration failure
    for key in ('enabled', 'headed', 'record_sessions', 'use_real_profile', 'camofox_managed_persistence',
                'camofox_adopt_existing_tab', 'camofox_rewrite_loopback_urls'):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f'browser.{key} must be true or false')
    from misaka.core.web.timeouts import _seconds
    for key in ('command_timeout', 'open_timeout', 'recording_retention'):
        if key in cfg:
            _seconds(cfg[key], 'browser.' + key, positive=True)
    return cfg


def executable(name):
    cfg = config()
    key = 'command' if name == 'agent-browser' else 'exec_command'
    custom = cfg.get(key)
    if custom:
        if not isinstance(custom, str):
            raise ValueError(f'browser.{key} must be an executable path (not a shell command)')
        return shutil.which(str(Path(custom).expanduser()))
    managed = Path(current_scope().profile_dir or get_agent_dir()) / 'web-tools' / 'node_modules' / '.bin'
    return shutil.which(name, path=str(managed)) or shutil.which(name)


def route():
    cfg = config()
    if cfg.get('controller_command'):
        return 'controller'
    if provider_env('BROWSER_CDP_URL') or cfg.get('cdp_url'):
        return 'cdp'
    selected = cfg.get('cloud_provider')
    if selected is not None:
        return selected
    if provider_env('CAMOFOX_URL'):
        return 'camofox'
    from misaka.core.web.browser.providers import providers
    for name in ('browser-use', 'browserbase', 'nous'):
        if providers()[name].is_available():
            return name
    return 'local'


def provider_environment():
    """Resolved browser-account settings, shared by identity and the controller."""
    from misaka.core.web.config import provider_variables
    scope = current_scope()
    names = {name for name in provider_variables() if name.startswith(('BROWSER', 'CAMOFOX', 'TOOL_GATEWAY', 'NOUS', 'FIRECRAWL'))}
    for provider in scope.browser_providers.values():
        schema = provider.get_setup_schema()
        for row in [schema, *schema.get('variants', [])]:
            names.update(item['key'] for item in row.get('env_vars', []))
    return {name: provider_env(name) for name in sorted(names)}


def identity(cfg):
    """Bound browser state is never silently reused after endpoint/account changes."""
    import hashlib
    import json

    from misaka.core.web.network import api_network_key, policy_key
    from misaka.core.web.scope import auth_identity

    scope = current_scope()
    state = (cfg, provider_environment(), auth_identity(),
             api_network_key(), policy_key(), sorted((name, id(provider)) for name, provider in scope.browser_providers.items()))
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def available_tools(*, include_fallback=False):
    cfg = config()
    if cfg.get('enabled', True) is False:
        return set()
    kind = route()
    if kind == 'controller':
        return set(cfg.get('controller_capabilities', BASE_TOOLS)) & {*BASE_TOOLS, *CDP_TOOLS, 'browser_exec'}
    if kind == 'camofox':
        return set(BASE_TOOLS) if provider_env('CAMOFOX_URL') else set()
    from misaka.core.web.browser.providers import providers
    if kind not in {'local', 'cdp'}:
        if kind not in providers():
            raise ValueError(f'Unknown browser.cloud_provider: {kind}')
        if not providers()[kind].is_available():
            return set()
    if kind == 'cdp' and not (provider_env('BROWSER_CDP_URL') or cfg.get('cdp_url')):
        return set()
    if kind == 'local' and cfg.get('engine') == 'lightpanda' and not cfg.get('lightpanda_path'):
        return set()
    import importlib.util
    if not executable('agent-browser') or importlib.util.find_spec('websockets') is None:
        return set()
    tools = set(BASE_TOOLS)
    tools.update(CDP_TOOLS)
    backend = cfg.get('backend', 'auto')
    if backend not in {'auto', 'off', 'browser-use', 'agent-browser'}:
        raise ValueError('browser.backend takes auto, off, agent-browser or browser-use')
    if backend in {'browser-use', 'auto'} and executable('browser-use'):
        return {'browser_exec', *(tools if include_fallback else CDP_TOOLS)}
    return tools


def subprocess_env():
    from misaka.core.web.config import without_credentials
    from misaka.core.web.network import proxy_environment

    snapshot = current_scope().environment
    env = without_credentials(dict(os.environ if snapshot is None else snapshot))
    # No accidental personal profile / auto-attach / extension / TLS bypass from
    # another tool's global AGENT_BROWSER_* settings. Explicit settings follow.
    env = {key: value for key, value in env.items() if not key.startswith(('AGENT_BROWSER_', 'BU_', 'BH_'))}
    for key, value in proxy_environment().items():
        env.pop(key.lower(), None)
        env[key] = value
    for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
        if value := provider_env(key):
            env[key] = value
    return env
