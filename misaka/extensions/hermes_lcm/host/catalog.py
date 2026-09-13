"""Hermes catalog semantics scoped to the native registry and its actual endpoint.

No second model registry or credential store. Raw discovery metadata complements
native Model (which does not represent mandatory reasoning / unknown support).
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from misaka.config import get_agent_dir
from misaka.utils.atomic import write_text

from . import llm

_ROUTE = contextvars.ContextVar('lcm_catalog_route', default=None)
_LOCK = threading.RLock()


@contextmanager
def route(model, auth):
    token = _ROUTE.set((model, auth))
    try:
        yield
    finally:
        _ROUTE.reset(token)


def endpoint(provider):
    current = _ROUTE.get()
    if current is not None and current[0].provider == provider:
        return current[0].baseUrl.rstrip('/')
    registry = llm._REGISTRY.get()
    models = [m for m in registry.getAll() if m.provider == provider] if registry else []
    if models:
        return models[0].baseUrl.rstrip('/')
    return {'nous': 'https://inference-api.nousresearch.com/v1',
            'openrouter': 'https://openrouter.ai/api/v1',
            'github-copilot': 'https://api.githubcopilot.com'}[provider]


def catalog_url(provider):
    return endpoint(provider) + '/models'


def cache_path(name):
    return Path(get_agent_dir()) / 'cache' / name


def _read_json_cache(path, **_):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_cache(path, value, **options):
    write_text(path, json.dumps(value, **options), mode=0o600)


def open_url(request, *, timeout, ssl_context=None):
    from ..native.urllib_security import open_credentialed_url
    return open_credentialed_url(request, timeout=timeout, ssl_context=ssl_context)


def _get_json(url, *, timeout, headers=None, ssl_context=None):
    with open_url(urllib.request.Request(url, headers=headers or {}), timeout=timeout, ssl_context=ssl_context) as response:
        return json.loads(response.read().decode())


def origin():
    registry = llm._REGISTRY.get()
    if registry is None:
        # Calls outside a native owner must be explicit, not borrow another registry.
        raise RuntimeError('Model catalog access requires a native registry')
    key = (catalog_url('nous'), catalog_url('openrouter'))
    with _LOCK:
        states = vars(registry).setdefault('_lcm_catalog_state', {})
        if key not in states:
            states[key] = SimpleNamespace(_HERMES_USER_AGENT='misaka-lcm',
                _urlopen_model_catalog_request=open_url,
                **{f'_{name}_{suffix}': value for name in ('nous', 'openrouter') for suffix, value in (
                    ('reasoning_caps_cache', None), ('reasoning_caps_failed_at', None),
                    ('caps_disk_checked', False), ('caps_warm_started', False))})
        return states[key]


def warm(refresh):
    # Preserve provider/config identity in the original bounded background fetch.
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(refresh,), name='lcm-catalog-warm', daemon=True).start()


def _resolve_nous_portal_url():
    from .nous import credential_state
    return credential_state().get('portal_base_url') or 'https://portal.nousresearch.com'


def fetch_nous_recommended_models(portal_base_url='', timeout=5.0, *, force_refresh=False):
    base = (portal_base_url or _resolve_nous_portal_url()).rstrip('/')
    state = origin()
    cache = vars(state).setdefault('recommendations', {})
    now = time.monotonic()
    cached = cache.get(base)
    if not force_refresh and cached and now - cached[1] < 600:
        return cached[0]
    path = cache_path('nous_recommended_cache.json')
    try:
        data = _get_json(base + '/api/nous/recommended-models', timeout=timeout, headers={'Accept': 'application/json'})
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    with _LOCK:
        disk = _read_json_cache(path) or {}
        if data:
            disk[base] = {'data': data, 'ts': time.time()}
            try:
                _write_json_cache(path, disk, indent=2)
            except OSError:
                pass  # An unwritable cache does not invalidate the live result.
        else:
            entry = disk.get(base, {})
            data = entry.get('data', {}) if isinstance(entry, dict) else {}
            data = data if isinstance(data, dict) else {}
        cache[base] = (data, now)
    return data


def check_nous_free_tier(*, force_fresh=False, cached_only=False):
    from .nous import account_info, credential_state
    state = credential_state()
    key = (state.get('portal_base_url'), hashlib.sha256(str(state.get('access_token', '')).encode()).hexdigest())
    cache = vars(origin()).setdefault('tiers', {})
    now = time.monotonic()
    cached = cache.get(key)
    if not force_fresh and cached and now - cached[1] < 180:
        return cached[0]
    if cached_only:
        return False
    try:
        free = account_info(state, force_fresh=force_fresh).is_free_tier
    except (OSError, ValueError, RuntimeError):
        free = False
    cache[key] = (free, now)
    return free


def fetch_github_model_catalog(api_key=None, timeout=5.0):
    from ..native.copilot_catalog import _copilot_text_models, _payload_items
    base = endpoint('github-copilot')
    cache = vars(origin()).setdefault('copilot', {})
    key = (base, hashlib.sha256(str(api_key or '').encode()).hexdigest())
    now = time.monotonic()
    cached = cache.get(key)
    if cached and now - cached[1] < 300:
        return copy.deepcopy(cached[0])
    headers = {'Editor-Version': 'vscode/1.104.1', 'User-Agent': 'misaka-lcm',
               'Openai-Intent': 'conversation-edits', 'x-initiator': 'agent'}
    for extra in ([{'Authorization': f'Bearer {api_key}'}, {}] if api_key else [{}]):
        try:
            items = _payload_items(_get_json(base + '/models', timeout=timeout, headers={**headers, **extra}))
        except (OSError, ValueError):
            continue
        models = _copilot_text_models(items)
        if not models and items:
            models = _copilot_text_models(items, ignore_picker_flag=True)
        if models:
            cache[key] = (copy.deepcopy(models), now)
            return models
    return None
