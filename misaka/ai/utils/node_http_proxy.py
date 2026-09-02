"""Environment-driven HTTP proxy resolution helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, urlparse

DEFAULT_PROXY_PORTS: dict[str, int] = {
    "ftp": 21,
    "gopher": 70,
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}

UNSUPPORTED_PROXY_PROTOCOL_MESSAGE = (
    "Unsupported proxy protocol. SOCKS and PAC proxy URLs are not supported; "
    "use an HTTP or HTTPS proxy URL."
)


@dataclass(frozen=True, slots=True)
class NodeHttpProxyAgents:
    httpAgent: str
    httpsAgent: str


def _get_proxy_env(key: str, env: Any = None) -> str:
    """The request-scoped ``env`` wins over the process environment, either case.

    ``ProviderRequestOptions.env`` is documented upstream as carrying proxy variables, so a
    proxy configured per provider (auth.json ``env`` / ``AuthResult.env``) has to be looked
    at before ``os.environ``.
    """
    lower, upper = key.lower(), key.upper()
    if env:
        scoped = _read_env_value(env, lower) or _read_env_value(env, upper)
        if scoped:
            return scoped
    return os.environ.get(lower, "") or os.environ.get(upper, "")


def _read_env_value(env: Any, key: str) -> str:
    getter = getattr(env, "get", None)
    value = getter(key) if callable(getter) else getattr(env, key, None)
    return value if isinstance(value, str) else ""


def _parse_proxy_target_url(target_url: str | ParseResult) -> ParseResult | None:
    parsed = target_url if isinstance(target_url, ParseResult) else urlparse(target_url)
    try:
        _ = parsed.port
    except ValueError:
        # `new URL("https://host:abc/v1")` throws in Node, and pi's parseProxyTargetUrl
        # turns that into `undefined` -> no proxy. `urlparse` accepts the string and defers
        # the error to `.port`, so a hand-typed bad port in models.json used to raise out of
        # every request instead. Same verdict as an unparseable URL: this target is not proxied.
        return None
    return parsed if parsed.scheme and parsed.netloc else None


def _should_proxy_hostname(hostname: str, port: int, env: Any = None) -> bool:
    no_proxy = _get_proxy_env("no_proxy", env).lower()
    if not no_proxy:
        return True
    if no_proxy == "*":
        return False

    for proxy in re.split(r"[,\s]", no_proxy):
        if not proxy:
            continue
        proxy_hostname = proxy
        proxy_port = 0
        parsed_proxy = re.match(r"^(.+):(\d+)$", proxy)
        if parsed_proxy is not None:
            proxy_hostname = parsed_proxy.group(1)
            proxy_port = int(parsed_proxy.group(2))
        if proxy_port and proxy_port != port:
            continue

        if not proxy_hostname.startswith(("*", ".")):
            if hostname == proxy_hostname:
                return False
            continue

        normalized = proxy_hostname.removeprefix("*")
        if hostname.endswith(normalized):
            return False

    return True


def _get_proxy_for_url(target_url: str | ParseResult, env: Any = None) -> str:
    parsed_url = _parse_proxy_target_url(target_url)
    if parsed_url is None or not parsed_url.scheme or not parsed_url.netloc:
        return ""

    protocol = parsed_url.scheme
    hostname = parsed_url.hostname or ""
    port = parsed_url.port or DEFAULT_PROXY_PORTS.get(protocol, 0)
    if not _should_proxy_hostname(hostname, port, env):
        return ""

    proxy = _get_proxy_env(f"{protocol}_proxy", env) or _get_proxy_env("all_proxy", env)
    if proxy and "://" not in proxy:
        proxy = f"{protocol}://{proxy}"
    return proxy


def resolve_http_proxy_url_for_target(
    target_url: str | ParseResult, env: Any = None
) -> ParseResult | None:
    proxy = _get_proxy_for_url(target_url, env)
    if not proxy:
        return None

    proxy_url = urlparse(proxy)
    if not proxy_url.scheme or not proxy_url.netloc:
        raise RuntimeError(f"Invalid proxy URL {json.dumps(proxy)}: Invalid URL")
    if proxy_url.scheme not in {"http", "https"}:
        raise RuntimeError(f"{UNSUPPORTED_PROXY_PROTOCOL_MESSAGE} Got {proxy_url.scheme}:")
    return proxy_url


def create_http_proxy_agents_for_target(
    target_url: str | ParseResult, env: Any = None
) -> NodeHttpProxyAgents | None:
    proxy_url = resolve_http_proxy_url_for_target(target_url, env)
    if proxy_url is None:
        return None
    proxy = proxy_url.geturl()
    return NodeHttpProxyAgents(httpAgent=proxy, httpsAgent=proxy)


__all__ = [
    "UNSUPPORTED_PROXY_PROTOCOL_MESSAGE",
    "NodeHttpProxyAgents",
    ]
