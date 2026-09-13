"""Explicit URL-tool network policy; no global environment changes or new transport."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl

import httpx
from httpx._utils import URLPattern, is_ipv4_hostname, is_ipv6_hostname

from misaka.core.web import config
from misaka.core.web.scope import current_scope

PROXY_VARIABLES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')
TLS_VARIABLES = ('SSL_CERT_FILE', 'SSL_CERT_DIR')


def proxy_dns_enabled() -> bool:
    value = config.web_config(strict=True).get('proxy_dns', False)
    if not isinstance(value, bool):
        raise ValueError('proxy_dns must be true or false')  # noqa: TRY004 - invalid config document
    return value


def proxy_environment() -> dict[str, str]:
    """Process (even empty) wins over profile; lowercase wins within each layer."""
    environment = current_scope().environment
    environment = os.environ if environment is None else environment
    stored = config.web_config().get('env', {})
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for name in PROXY_VARIABLES:
        value = ''
        for source in (environment, stored):
            key = next((key for key in (name.lower(), name) if key in source), None)
            if key is not None:
                # Like urllib/HTTPX, do not interpret a CGI Proxy header as HTTP_PROXY.
                value = '' if source is environment and key == 'HTTP_PROXY' and 'REQUEST_METHOD' in source else source[key]
                break
        if not isinstance(value, str):
            raise ValueError(f'{name} must be a proxy setting string')  # noqa: TRY004 - config document
        result[name] = value.strip()
    return result


def _proxy_url(raw: str, name: str) -> str:
    from misaka.core.tools._web.url_safety import (
        always_blocked_address,
        always_blocked_host,
    )

    try:
        url = httpx.URL(raw if '://' in raw else 'http://' + raw)
        if (url.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not url.host
                or url.path not in {'', '/'} or url.query or url.fragment or url.port == 0
                or always_blocked_host(url.host) or always_blocked_address(url.host)):
            raise ValueError('invalid proxy endpoint')
        httpx.Proxy(url)  # Validate through the client that will consume it.
        return str(url)
    except (ValueError, httpx.InvalidURL):
        raise ValueError(f'{name} must be an HTTP(S) or SOCKS5 proxy URL without a path/query/fragment') from None


def proxy_for_url(url: str, *, api: bool = False) -> str | None:
    # API transports have always honored proxies. The opt-in flag controls
    # source-URL DNS delegation, not the provider API's network route.
    if not api and not proxy_dns_enabled():
        return None
    values = proxy_environment()
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return None  # The common vetter retains the structured invalid-URL error.
    raw = values.get(target.scheme.upper() + '_PROXY') or values['ALL_PROXY']
    if not raw:
        return None
    # HTTPX 0.28's own matching primitive, not a second hostname/glob implementation.
    # We supply a scope snapshot rather than its process-global get_environment_proxies().
    for host in values['NO_PROXY'].split(','):
        host = host.strip()
        if host == '*':
            return None
        if not host:
            continue
        if '://' in host:
            pattern = host
        elif is_ipv6_hostname(host):
            pattern = f'all://[{host}]'
        elif is_ipv4_hostname(host) or host.lower() == 'localhost':
            pattern = f'all://{host}'
        else:
            pattern = f'all://*{host}'
        try:
            if URLPattern(pattern).matches(target):
                return None
        except (ValueError, httpx.InvalidURL):
            raise ValueError('NO_PROXY contains an invalid host/URL rule') from None
    return _proxy_url(raw, target.scheme.upper() + '_PROXY/ALL_PROXY')


def trusted_private_hosts(value=None) -> tuple[str, ...]:
    from misaka.core.tools._web.url_safety import always_blocked_host, literal_address

    if value is None:
        value = config.web_config(strict=True).get('trusted_private_hosts', [])
    if not isinstance(value, list):
        raise ValueError('trusted_private_hosts must be a list of exact HTTPS hostnames')  # noqa: TRY004
    hosts = set()
    for raw in value:
        if (not isinstance(raw, str) or not raw.strip() or any(c in raw for c in '/:@*?#%[]\\')
                or any(c.isspace() for c in raw.strip())):
            raise ValueError('trusted_private_hosts accepts hostnames only, without wildcards, ports or URL syntax')
        try:
            host = httpx.URL('https://' + raw.strip()).raw_host.decode('ascii').rstrip('.')
            if literal_address(host) is not None:
                raise ValueError('IP literals are not hostname grants')
            if not host or host == 'localhost' or host.endswith('.localhost') or always_blocked_host(host):
                raise ValueError('reserved hostname')
        except (ValueError, httpx.InvalidURL):
            raise ValueError('trusted_private_hosts contains an invalid, literal or reserved hostname') from None
        hosts.add(host)
    return tuple(sorted(hosts))


def policy_key() -> str:
    from misaka.core.tools._web.url_safety import allow_private_urls

    enabled = proxy_dns_enabled()
    values = (enabled, proxy_environment() if enabled else {}, trusted_private_hosts(), allow_private_urls(),
              {name: config.provider_env(name) for name in TLS_VARIABLES})
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def tls_verify() -> bool | ssl.SSLContext:
    """Honor explicit CA settings without enabling implicit environment proxy mounts."""
    cafile, capath = (config.provider_env(name) for name in TLS_VARIABLES)
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    if capath:
        return ssl.create_default_context(capath=capath)
    return True  # HTTPX's existing default trust store, never disabled verification.


def api_network_key() -> str:
    values = (proxy_environment(), {name: config.provider_env(name) for name in TLS_VARIABLES})
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def api_network_options(endpoint: str) -> dict:
    verify = tls_verify()
    proxy = proxy_for_url(endpoint, api=True)
    if proxy and httpx.URL(proxy).scheme == 'https':
        proxy = httpx.Proxy(proxy, ssl_context=verify if isinstance(verify, ssl.SSLContext) else None)
    return {'trust_env': False, 'verify': verify, 'proxy': proxy}


def proxy_secrets() -> list[str]:
    """Protect raw/decoded URL auth and reflected Proxy-Authorization headers."""
    values = []
    for name, raw in proxy_environment().items():
        if name == 'NO_PROXY' or '@' not in raw:
            continue
        values.append(raw)
        try:
            url = httpx.URL(raw if '://' in raw else 'http://' + raw)
            auth = f'{url.username}:{url.password}'
            values.extend((url.userinfo.decode(), auth, url.password,
                           'Basic ' + base64.b64encode(auth.encode()).decode()))
        except (ValueError, httpx.InvalidURL):
            pass  # Invalid proxy text is still redacted as a whole above.
    return values


def status() -> str:
    enabled, hosts = proxy_dns_enabled(), trusted_private_hosts()
    configured = '(none)'
    if enabled:
        values = proxy_environment()
        configured = ', '.join(name for name, raw in values.items() if raw and name != 'NO_PROXY') or '(none)'
        for name, raw in values.items():
            if raw and name != 'NO_PROXY':
                _proxy_url(raw, name)
        # Validate NO_PROXY through the actual selector without resolving or connecting.
        proxy_for_url('http://status.invalid/')
        proxy_for_url('https://status.invalid/')
    return (f"URL proxy DNS: {'on; matching proxy owns final DNS/egress' if enabled else 'off; direct IP pinning'}; "
            f"proxy variables: {configured}; trusted private HTTPS hosts: {', '.join(hosts) or '(none)'}")
