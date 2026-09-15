"""Nous service policy on the native OAuth/credential lifecycle.

Only AuthStorage writes grants. A refreshed single-use refresh token is returned
for durable storage before getApiKey validates the new inference JWT.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import ssl
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from misaka.ai.utils.abort import race_with_abort_signal
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthDeviceCodeInfo,
)

from ..native import nous_auth as policy
from . import catalog, llm

_ACCOUNT = contextvars.ContextVar('lcm_nous_account', default=None)


@contextmanager
def account_scope(credential):
    token = _ACCOUNT.set(credential)
    try:
        yield
    finally:
        _ACCOUNT.reset(token)


def credential_state(credential=None):
    if credential is None:
        credential = _ACCOUNT.get()
    if credential is None:
        registry = llm._REGISTRY.get()
        credential = registry.authStorage.get('nous') if registry and hasattr(registry, 'authStorage') else None
    raw = credential.model_dump() if hasattr(credential, 'model_dump') else dict(credential or {})
    return {**raw, 'access_token': raw.get('access', ''), 'refresh_token': raw.get('refresh', ''),
            'expires_at': datetime.fromtimestamp(raw['expires'] / 1000, UTC).isoformat() if raw.get('expires') else None,
            'client_id': raw.get('client_id') or policy.DEFAULT_NOUS_CLIENT_ID,
            'portal_base_url': raw.get('portal_base_url') or os.getenv('NOUS_PORTAL_BASE_URL') or policy.DEFAULT_NOUS_PORTAL_URL,
            'inference_base_url': raw.get('inference_base_url') or policy.DEFAULT_NOUS_INFERENCE_URL,
            'scope': raw.get('scope') or policy.DEFAULT_NOUS_SCOPE}


def _credentials(state):
    expires = policy._parse_iso_timestamp(policy._nous_jwt_expires_at(state.get('access_token'), state.get('expires_at')))
    return OAuthCredentials(refresh=state.get('refresh_token') or '', access=state.get('access_token') or '',
        expires=int((expires or 0) * 1000), **{key: state[key] for key in (
            'client_id', 'portal_base_url', 'inference_base_url', 'scope', 'token_type', 'tls', 'last_auth_error') if key in state})


async def _post(path, state, signal, **kwargs):
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, verify=policy._resolve_verify(auth_state=state)) as client:
        return await race_with_abort_signal(client.post(state['portal_base_url'].rstrip('/') + path, **kwargs), signal)


async def refresh_token(credential, signal=None):
    state = credential_state(credential)
    if not state['refresh_token']:
        raise RuntimeError('No Nous refresh token; sign in with /login nous')
    response = await _post('/api/oauth/token', state, signal,
        headers={'x-nous-refresh-token': state['refresh_token']},
        data={'grant_type': 'refresh_token', 'client_id': state['client_id']})
    payload = response.json()
    if response.status_code != 200:
        code = str(payload.get('error', 'invalid_grant'))
        message = str(payload.get('error_description') or code)
        error = policy.AuthError(f'Nous OAuth refresh: {message}', provider='nous', code=code,
            relogin_required=code in policy._OAUTH_GRANT_DEAD_CODES or 'reuse' in message.lower())
        if error.relogin_required and code in policy._OAUTH_GRANT_DEAD_CODES:
            # Commit the terminal marker through the SAME native refresh transaction;
            # throwing before returning would replay the dead single-use grant forever.
            state.update(access_token='', refresh_token='', expires_at=None)
            state['last_auth_error'] = policy._last_auth_error_marker('nous', error, reason='runtime_refresh_failure')
            return _credentials(state)
        raise error
    if not isinstance(payload.get('access_token'), str) or not payload['access_token']:
        raise RuntimeError('Nous refresh response missing access_token')
    policy._apply_nous_refreshed_tokens(state, payload, state['refresh_token'],
        inference_base_url=policy._healed_nous_inference_url(payload))
    return _credentials(state)


def api_key(credential):
    state = credential_state(credential)
    if state.get('last_auth_error'):
        raise RuntimeError(f"{state['last_auth_error']['message']}; sign in with /login nous")
    reason = policy._nous_invoke_jwt_status(state['access_token'], scope=state['scope'], expires_at=state['expires_at'])
    if reason is not None:
        raise RuntimeError(f'Nous inference JWT: {reason}; sign in with /login nous')
    return state['access_token']


def base_url(credential):
    # Explicit native operator configuration wins; server-returned URLs are validated.
    return os.getenv('NOUS_INFERENCE_BASE_URL') or policy._healed_nous_inference_url(credential_state(credential))


async def login(callbacks):
    state = credential_state({})
    signal = getattr(callbacks, 'signal', None)
    response = await _post('/api/oauth/device/code', state, signal,
        data={'client_id': state['client_id'], 'scope': state['scope']})
    response.raise_for_status()
    device = response.json()
    for field in ('device_code', 'user_code', 'verification_uri', 'verification_uri_complete', 'expires_in', 'interval'):
        if field not in device:
            raise ValueError(f'Nous device response missing {field}')
    callbacks.onAuth(OAuthAuthInfo(url=device['verification_uri_complete']))
    callbacks.onDeviceCode(OAuthDeviceCodeInfo(userCode=device['user_code'], verificationUri=device['verification_uri'],
        intervalSeconds=1, expiresInSeconds=device['expires_in']))

    interval = 1

    async def poll():
        nonlocal interval
        response = await _post('/api/oauth/token', state, signal, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'client_id': state['client_id'], 'device_code': device['device_code']})
        data = response.json()
        if response.status_code == 200:
            if not isinstance(data.get('access_token'), str) or not data['access_token']:
                raise ValueError('Nous token response missing access_token')
            return {'status': 'complete', 'value': data}
        if data.get('error') == 'authorization_pending':
            return {'status': 'pending'}
        if data.get('error') == 'slow_down':
            interval = min(interval + 1, 30)
            return {'status': 'slow_down', 'intervalSeconds': interval}
        return {'status': 'failed', 'message': f"{data.get('error')}: {data.get('error_description', '')}"}

    token = await poll_oauth_device_code_flow(poll=poll, intervalSeconds=1,
        expiresInSeconds=device['expires_in'], signal=signal)
    policy._apply_nous_refreshed_tokens(state, token, token.get('refresh_token') or '',
        inference_base_url=policy._healed_nous_inference_url(token))
    return _credentials(state)


flow = SimpleNamespace(id='nous', name='Nous Research', usesCallbackServer=False,
    login=login, refreshToken=refresh_token, getApiKey=api_key, getBaseUrl=base_url, modifyModels=None)


def account_info(state=None, *, force_fresh=False):
    from ..native.nous_account import _info_from_account_payload, _info_from_valid_jwt
    state = state or credential_state()
    portal = state['portal_base_url'].rstrip('/')
    token = state.get('access_token')
    if not token:
        raise RuntimeError('Nous account information requires a native OAuth login')
    if not force_fresh:
        claims = _info_from_valid_jwt(token, state, portal, policy.NOUS_INVOKE_JWT_MIN_TTL_SECONDS)
        if claims is not None:
            return claims
    cache = vars(catalog.origin()).setdefault('accounts', {})
    key = (portal, hashlib.sha256(token.encode()).hexdigest())
    now = time.monotonic()
    cached = cache.get(key)
    if not force_fresh and cached and now - cached[1] < 60:
        return cached[0]
    verify = policy._resolve_verify(auth_state=state)
    tls_context = ssl._create_unverified_context() if verify is False else verify if isinstance(verify, ssl.SSLContext) else None
    payload = catalog._get_json(portal + '/api/oauth/account', timeout=8, ssl_context=tls_context,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    info = _info_from_account_payload(payload, state=state, portal_base_url=portal)
    cache[key] = (info, now)
    return info
