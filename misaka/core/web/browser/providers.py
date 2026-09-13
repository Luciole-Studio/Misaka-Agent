"""Hermes cloud browser protocols; creation has no network-error retry/fallback."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import quote

from misaka.core.web.accounting import account_call
from misaka.core.web.config import provider_env
from misaka.core.web.runtime import api_client
from misaka.core.web.scope import current_scope


class BrowserProvider(ABC):
    @property
    @abstractmethod
    def name(self): ...

    @abstractmethod
    def is_available(self):
        """Local configuration check only; no connection or credential refresh."""

    @abstractmethod
    async def create_session(self, owner_id):
        """Return an owned lease with id, cdp_url, features, expires_at and async close()."""

    def get_setup_schema(self):
        return {'name': self.name, 'env_vars': []}


@dataclass
class CloudLease:
    id: str
    cdp_url: str
    features: dict
    expires_at: str | None
    endpoint: str
    headers: dict
    method: str
    body: dict | None
    provider: str
    external_call_id: str | None = None
    closed: bool = False

    async def close(self):
        if self.closed:
            return
        async with api_client('browser-' + self.provider, self.endpoint, tuple(self.headers.items()), timeout=15) as client, account_call('browser_close', self.provider, self.id):
            response = await client.request(self.method, self.endpoint, headers=self.headers,
                                            **({'json': self.body} if self.body is not None else {}))
            if response.status_code not in {404, 410}:
                response.raise_for_status()
        self.closed = True


class CloudProvider(BrowserProvider):
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    def is_available(self):
        if self.name == "nous":
            from misaka.core.web.gateway import available
            return available()
        names = {'browserbase': ('BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID'),
                 'browser-use': ('BROWSER_USE_API_KEY',), 'firecrawl': ('FIRECRAWL_API_KEY',)}
        return all(provider_env(key) for key in names[self.name])

    def get_setup_schema(self):
        if self.name == "nous":
            return {"name": "Nous managed Browser Use", "badge": "managed", "env_vars": [],
                    "tag": "Run misaka web gateway-login before selecting nous."}
        keys = {'browserbase': ['BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID'],
                'browser-use': ['BROWSER_USE_API_KEY'], 'firecrawl': ['FIRECRAWL_API_KEY']}[self.name]
        return {'name': self.name, 'badge': 'paid', 'env_vars': [{'key': key, 'prompt': key} for key in keys]}

    async def create_session(self, owner_id):
        if not self.is_available():
            raise ValueError(f'{self.name} credentials are not configured; see misaka web browser-providers')
        if self.name == 'browserbase':
            base = (provider_env('BROWSERBASE_BASE_URL') or 'https://api.browserbase.com').rstrip('/')
            headers = {'X-BB-API-Key': provider_env('BROWSERBASE_API_KEY')}
            path, method = '/v1/sessions', 'POST'
            body = {'projectId': provider_env('BROWSERBASE_PROJECT_ID')}
            release = {'projectId': body['projectId'], 'status': 'REQUEST_RELEASE'}
            for key, env in [('keepAlive', 'BROWSERBASE_KEEP_ALIVE'), ('proxies', 'BROWSERBASE_PROXIES')]:
                if provider_env(env).lower() != 'false':
                    body[key] = True
            if provider_env('BROWSERBASE_ADVANCED_STEALTH').lower() == 'true':
                body['browserSettings'] = {'advancedStealth': True}
            if provider_env('BROWSERBASE_SESSION_TIMEOUT'):
                body['timeout'] = max(1, min(21600, int(provider_env('BROWSERBASE_SESSION_TIMEOUT'))))
        elif self.name in {'browser-use', 'nous'}:
            base = (provider_env('BROWSER_USE_BASE_URL') or 'https://api.browser-use.com/api/v3').rstrip('/')
            headers = {'X-Browser-Use-API-Key': provider_env('BROWSER_USE_API_KEY')}
            path, body, method, release = '/browsers', {}, 'PATCH', {'action': 'stop'}
            if self.name == 'nous':
                from misaka.core.web.gateway import resolve
                token, base = await resolve('browser-use')
                headers = {'X-Browser-Use-API-Key': token, 'X-Idempotency-Key': 'browser-use-session-create:' + owner_id}
                body = {'timeout': 5, 'proxyCountryCode': 'us'}
        else:
            base = (provider_env('FIRECRAWL_API_URL') or 'https://api.firecrawl.dev').rstrip('/')
            headers = {'Authorization': 'Bearer ' + provider_env('FIRECRAWL_API_KEY')}
            path, method, release = '/v2/browser', 'DELETE', None
            body = {'ttl': max(1, int(provider_env('FIRECRAWL_BROWSER_TTL') or '300'))}
        headers['Content-Type'] = 'application/json'
        async with api_client('browser-' + self.name, base, tuple(headers.items()), timeout=30) as client:
            async def create():
                async with account_call('browser_create', self.name, owner_id, managed=self.name == 'nous') as facts:
                    response = await client.post(base + path, json=body, headers=headers)
                    if response.headers.get('x-external-call-id'):
                        facts['external_call_id'] = response.headers['x-external-call-id'][:200]
                    return response
            response = await create()
            if self.name == 'browserbase':
                for feature in ('keepAlive', 'proxies'):
                    if response.status_code == 402 and feature in body:
                        body.pop(feature)
                        response = await create()  # Explicit rejection, not an unknown creation outcome.
            response.raise_for_status()
            data = response.json()
        session_id = str(data.get('id') or '')
        if not session_id:
            raise ValueError(f'{self.name} created response contains no session id; creation outcome is unknown')
        features = ({'basic_stealth': True, 'proxies': bool(body.get('proxies')), 'keep_alive': bool(body.get('keepAlive')),
                     'advanced_stealth': bool(body.get('browserSettings')), 'custom_timeout': 'timeout' in body}
                    if self.name == 'browserbase' else {self.name.replace('-', '_'): True})
        return CloudLease(session_id, data.get('cdpUrl') or data.get('connectUrl') or '', features,
                          data.get('timeoutAt'), base + path + '/' + quote(session_id, safe=''), headers,
                          method, release, self.name, response.headers.get('x-external-call-id'))


def validate_provider(provider):
    if not isinstance(provider, BrowserProvider):
        raise TypeError('registerBrowserProvider requires BrowserProvider')
    name = provider.name
    if not isinstance(name, str) or not name or name != name.strip().lower() or any(c.isspace() for c in name):
        raise ValueError('Browser provider names must be lowercase without spaces')
    from misaka.core.web.registry import validate_setup_schema
    validate_setup_schema(provider.get_setup_schema())
    return name


def providers():
    builtins = {name: CloudProvider(name) for name in ('browser-use', 'browserbase', 'firecrawl', 'nous')}
    return builtins | current_scope().browser_providers
