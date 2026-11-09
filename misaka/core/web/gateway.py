import json

"""Nous managed Firecrawl/Browser Use: profile auth, device flow and entitlement.

No Hermes auth-file migration. Use MISAKA's existing authoritative OAuth lock.
Availability reads local state only; account/refresh requests happen on explicit use.
"""
import hashlib
import math
import time
from urllib.parse import urlsplit

from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.ai.auth.resolve import refreshStoredOAuthCredential
from misaka.ai.auth.types import DeviceCodeEvent, ModelAuth, OAuthAuth, OAuthCredential
from misaka.ai.utils.abort import operationSignal
from misaka.core.auth_storage import AuthStorage
from misaka.core.tools._common import run_with_abort
from misaka.core.web import config
from misaka.core.web.accounting import account_call
from misaka.core.web.backends.xai import _auth_path
from misaka.core.web.runtime import api_client
from misaka.core.web.scope import current_scope
from misaka.utils.async_lifecycle import run_in_thread

CLIENT_ID = 'hermes-cli'


def portal():
    return _endpoint(config.provider_env('NOUS_PORTAL_URL') or 'https://portal.nousresearch.com')


def _endpoint(raw):
    parts = urlsplit(raw)
    if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError('Gateway endpoints require an HTTP(S) URL without credentials, query or fragment')
    if parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Gateway credentials require HTTPS except on explicit loopback fixtures')
    return raw.rstrip('/')


def origin(vendor):
    name = vendor.upper().replace('-', '_') + '_GATEWAY_URL'
    explicit = config.provider_env(name)
    if explicit:
        return _endpoint(explicit)
    scheme = config.provider_env('TOOL_GATEWAY_SCHEME') or 'https'
    domain = config.provider_env('TOOL_GATEWAY_DOMAIN') or 'nousresearch.com'
    if '/' in domain or '@' in domain:
        raise ValueError('TOOL_GATEWAY_DOMAIN must be a hostname, optionally with a port')
    return _endpoint(f'{scheme}://{vendor}-gateway.{domain}')


def peek_token():
    if explicit := config.provider_env('TOOL_GATEWAY_USER_TOKEN'):
        return explicit
    try:
        with open(_auth_path(), encoding='utf-8-sig') as handle:
            stored = json.load(handle)
        data = stored.get('nous', {}) if isinstance(stored, dict) else {}
        return data.get('access', '') if isinstance(data, dict) and data.get('type') == 'oauth' else ''
    except (OSError, ValueError):
        return ''


def available():
    return bool(peek_token())


async def _form(base, path, fields, headers=None, signal=None):
    async def send():
        async with api_client('nous-oauth', base, timeout=30) as client, account_call('oauth', 'nous', path):
            response = await client.post(base + path, data=fields, headers=headers or {})
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError('Nous OAuth returned a non-object response')  # noqa: TRY004 - surface protocol/configuration failure
        return response, body
    return (await run_with_abort(send(), signal))[0]


def _credentials(body, base, previous=None):
    access, refresh = body.get('access_token'), body.get('refresh_token') or (previous.refresh if previous else '')
    seconds = body.get('expires_in', 3600)
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise ValueError('Nous token response is missing access_token/refresh_token')
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Nous token expires_in must be positive and finite')
    config.remember_secret(access)
    config.remember_secret(refresh)
    return OAuthCredential(access=access, refresh=refresh, expires=int((time.time() + seconds) * 1000),
                           portal_base_url=base, client_id=CLIENT_ID)


async def _login(interaction, options=None):
    base, signal = portal(), interaction.signal
    response, body = await _form(base, '/api/oauth/device/code',
        {'client_id': CLIENT_ID, 'scope': 'inference:invoke'}, signal=signal)
    response.raise_for_status()
    for name in ('device_code', 'user_code', 'verification_uri'):
        if not isinstance(body.get(name), str) or not body[name]:
            raise ValueError('Nous device response is missing ' + name)
    verification = _endpoint(body['verification_uri'])
    if urlsplit(verification).netloc != urlsplit(base).netloc:
        raise ValueError('Nous device verification changed the configured portal origin')
    seconds, interval = body.get('expires_in'), body.get('interval', 5)
    if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in (seconds, interval)):
        raise ValueError('Nous device response expiry/interval must be positive and finite')
    interaction.notify(DeviceCodeEvent(userCode=body['user_code'], verificationUri=verification,
                                       intervalSeconds=interval, expiresInSeconds=seconds))
    async def poll():
        response, token = await _form(base, '/api/oauth/token', {'client_id': CLIENT_ID,
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': body['device_code']}, signal=signal)
        error = token.get('error')
        if error == 'authorization_pending':
            return {'status': 'pending'}
        if error == 'slow_down':
            return {'status': 'slow_down'}
        response.raise_for_status()
        if error:
            raise ValueError('Nous device approval failed: ' + str(error))
        return {'status': 'complete', 'value': _credentials(token, base)}
    return await pollOAuthDeviceCodeFlow({'poll': poll, 'intervalSeconds': interval, 'expiresInSeconds': seconds,
                                          'signal': signal})


async def _refresh(credential, signal):
    base = _endpoint(getattr(credential, 'portal_base_url', None) or portal())
    config.remember_secret(credential.refresh)
    response, body = await _form(base, '/api/oauth/token', {'grant_type': 'refresh_token', 'client_id': CLIENT_ID},
                                {'x-nous-refresh-token': credential.refresh}, signal)
    response.raise_for_status()
    return _credentials(body, base, credential)


async def _toAuth(credential):
    return ModelAuth(apiKey=credential.access)


nousOAuth = OAuthAuth(name='Nous Tool Gateway', login=_login, refresh=_refresh, toAuth=_toAuth)

# pi's auth/resolve.ts DEFAULT_OAUTH_MINIMUM_VALIDITY_MS.
_OAUTH_MINIMUM_VALIDITY_MS = 5 * 60 * 1000


def _expires_soon(credential):
    return time.time() * 1000 + _OAUTH_MINIMUM_VALIDITY_MS >= credential.expires


def storage():
    return AuthStorage.create(_auth_path())


async def token(*, rejected=None):
    if explicit := config.provider_env('TOOL_GATEWAY_USER_TOKEN'):
        config.remember_secret(explicit)
        if rejected == explicit:
            raise ValueError('Configured Nous gateway token was rejected; replace it or log in')
        return explicit, portal()
    store = await run_in_thread(storage)
    credential = await store.read('nous')
    if credential is None or credential.type != 'oauth':
        raise ValueError('Nous gateway login is missing; run misaka web gateway-login')

    def needs_refresh(current):
        # Re-checked under the lock: a replacement another session wrote is read back without refreshing.
        return current.access == rejected or _expires_soon(current)

    if needs_refresh(credential):
        credential = await refreshStoredOAuthCredential(store, 'nous', nousOAuth, needs_refresh, operationSignal())
        if credential is None:
            raise ValueError('Nous gateway login is missing; run misaka web gateway-login')
    access = (await nousOAuth.toAuth(credential)).apiKey
    config.remember_secret(access)
    return access, getattr(credential, 'portal_base_url', None) or portal()


async def account(*, force=False):
    access, base = await token()
    identity = hashlib.sha256((base + access).encode()).hexdigest()
    scope = current_scope()
    cached = scope.gateway_accounts.get(identity)
    if not force and cached and cached[0] > time.monotonic():
        return access, cached[1]
    for attempt in range(2):
        async with api_client('nous-account', base, identity, timeout=8) as client, account_call('account', 'nous', '/api/oauth/account'):
            response = await client.get(base + '/api/oauth/account', headers={'Authorization': 'Bearer ' + access})
        if response.status_code != 401 or attempt:
            break
        access, base = await token(rejected=access)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload or payload.get('error'):
        raise ValueError('Nous account response did not contain a valid entitlement')
    paid, pool = payload.get('paid_service_access', {}), payload.get('tool_access', {})
    paid = paid if isinstance(paid, dict) else {}
    pool = pool if isinstance(pool, dict) else {}
    coverage = pool.get('coverage', {})
    result = {'paid': (paid['allowed'] if paid.get('allowed') is not None else paid.get('paid_access')) is True,
              'pool_enabled': pool.get('enabled') is True,
              'coverage': {key: value is True for key, value in coverage.items()} if isinstance(coverage, dict) else {}}
    identity = hashlib.sha256((base + access).encode()).hexdigest()
    scope.gateway_accounts[identity] = (time.monotonic() + 300, result)
    while len(scope.gateway_accounts) > 16:
        scope.gateway_accounts.pop(next(iter(scope.gateway_accounts)))
    return access, result


async def resolve(vendor):
    access, entitlement = await account()
    category = {'firecrawl': 'firecrawl', 'browser-use': 'browser-use'}[vendor]
    if not (entitlement['paid'] or (entitlement['pool_enabled'] and entitlement['coverage'].get(category))):
        raise ValueError(f'Nous account has no current {vendor} tool entitlement; check portal credits/access')
    return access, origin(vendor)
