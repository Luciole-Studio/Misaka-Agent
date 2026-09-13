"""Native catalogue/credential boundaries for Hermes' unchanged fallback selector."""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from misaka.utils.values import read_field

from . import config_bridge, llm

logger = logging.getLogger(__name__)
_STATE = contextvars.ContextVar('lcm_aux_route', default=None)
_HEALTH_LOCK = threading.Lock()


def _auth_digest(auth):
    # Headers and provider-scoped env can identify an account without an apiKey.
    identity = {field: auth.get(field) or None for field in ('apiKey', 'headers', 'env')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def update_auth(auth, base_url):
    state = _STATE.get()
    state.health, state.base_url = _auth_digest(auth), base_url


@contextmanager
def scope(registry, main_runtime, provider, base_url, auth):
    # Role/provider credentials may differ in one process. Health is never shared
    # across auth owners, and secrets never appear in a log or route receipt.
    key = _auth_digest(auth)
    state = SimpleNamespace(registry=registry, main=main_runtime, provider=provider,
                            base_url=base_url, health=key, unavailable=set())
    token = _STATE.set(state)
    try:
        yield state
    finally:
        _STATE.reset(token)


def provider_names():
    registry = llm._REGISTRY.get()
    if registry is None:
        from misaka.ai.models import get_providers
        return set(get_providers())
    return {model.provider for model in registry.getAll()}


def _read_main_provider():
    state = _STATE.get()
    return state.main.get('provider') or read_field(llm._MODEL.get(), 'provider', '') or state.provider


def _read_main_model():
    state = _STATE.get()
    return state.main.get('model') or read_field(llm._MODEL.get(), 'id', '')


def _normalize_chain_label(provider):
    from ..native.auxiliary_health import _normalize_chain_label as normalize
    return normalize(provider)


def _custom_health_base_url(provider, explicit_base_url=None):
    from misaka.ai.models import get_providers

    from ..native.route_identity import normalize_route_base_url
    provider = _normalize_chain_label(provider)
    if provider not in get_providers() or provider in {'custom', 'local/custom'}:
        state = _STATE.get()
        return normalize_route_base_url(explicit_base_url or (state.base_url if state.provider == provider else ''))
    return ''


def _health_cache(kind):
    state = _STATE.get()
    # Registry lifetime owns the cache. Object id reuse must not inherit a dead
    # owner's cooldown; rotating credentials gets a separate original cache.
    caches = vars(state.registry).setdefault('_lcm_aux_health', {})
    return caches.setdefault(state.health, {}).setdefault(kind, {})


def _is_provider_unhealthy(provider, base_url=None):
    from ..native.auxiliary_health import _is_provider_unhealthy as check
    with _HEALTH_LOCK:
        return check(provider, base_url)


def _mark_provider_unhealthy(provider, ttl=None, *, base_url=None):
    from ..native.auxiliary_health import _mark_provider_unhealthy as mark
    with _HEALTH_LOCK:
        mark(provider, ttl, base_url=base_url)


def _log_skip_unhealthy(label, task=None, *, base_url=None):
    from ..native.auxiliary_health import _log_skip_unhealthy as log
    with _HEALTH_LOCK:
        log(label, task, base_url=base_url)


def _recoverable_pool_provider(provider, client, *, main_runtime=None):
    # Pool policy uses native named accounts; Hermes' auth store is never opened.
    return read_field(read_field(client, 'model'), 'provider', provider)


def _record_route_info(info, provider, model):
    if info is not None:
        info.update(provider=provider, model=model)


def get_fallback_chain(config):
    chain = config.get('fallback_providers', [])
    return chain if isinstance(chain, list) else []


def resolve_provider_client(provider, model=None, explicit_base_url=None, explicit_api_key=None,
                            api_mode=None, **_):
    registry = _STATE.get().registry
    try:
        provider, model = llm._route(str(model or ''), provider, registry, {}, defer_nous=True)
        resolved = llm.resolve_model(provider, model, registry)
    except (ValueError, RuntimeError):
        return None, None
    from .pool import Pool
    from .profiles import get_provider_profile
    profile = get_provider_profile(provider)
    external = profile is not None and profile.auth_type == 'external_process'
    if not explicit_api_key and not external and Pool.configured(registry, provider) is None and not registry.hasConfiguredAuth(resolved):
        return None, None
    model = resolved.id if model else ''
    selection_key = (provider, model, explicit_base_url, explicit_api_key, api_mode)
    if selection_key in _STATE.get().unavailable:
        return None, None
    route = SimpleNamespace(model=resolved, provider=provider, selection_key=selection_key,
        base_url=explicit_base_url, api_key=explicit_api_key,
        api_mode=api_mode, timeout=None)
    return route, model


def _fallback_entry_api_key(entry):
    return entry.get('api_key') or config_bridge._scoped_key_env(entry.get('key_env') or entry.get('api_key_env'))


def _resolve_fallback_entry(entry):
    key = _fallback_entry_api_key(entry)
    route, model = resolve_provider_client(str(entry.get('provider') or ''), entry.get('model'),
        explicit_base_url=entry.get('base_url'), explicit_api_key=key,
        api_mode=entry.get('api_mode') or entry.get('transport'))
    if route is not None:
        from ..native.auxiliary_options import _coerce_positive_timeout
        route.timeout = _coerce_positive_timeout(entry.get('timeout'))
    return route, model


def _get_provider_chain():
    registry = _STATE.get().registry
    names = list(dict.fromkeys(model.provider for model in registry.getAll()))
    # Same discovery tiers as Hermes; Codex OAuth is explicit/main-only.
    from misaka.ai.models import get_providers
    builtin = set(get_providers())
    names.sort(key=lambda name: 0 if name == 'openrouter' else 1 if name == 'nous' else 2 if name not in builtin else 3)
    return [(name, lambda name=name: resolve_provider_client(name))
            for name in names if name != 'openai-codex']
