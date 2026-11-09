"""Translated from pi's ``packages/ai/src/utils/node-http-proxy.ts`` (v1.1.0).

MISAKA fork: ``urllib.parse`` stands in for the WHATWG ``URL``; a URL whose port is not a number in range parses here
but has no port, which ``_parseProxyTargetUrl`` treats as the ``new URL`` failure it is upstream.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import ParseResult, urlparse

from misaka.ai.types import ProviderEnv
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import jsonStringify

_DEFAULT_PROXY_PORTS: dict[str, int] = {
    "ftp": 21,
    "gopher": 70,
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}


def _getProxyEnv(key: str, env: ProviderEnv | None = None) -> str:
    lowercaseKey = key.lower()
    uppercaseKey = key.upper()
    return (
        (env.get(lowercaseKey) if env else None)
        or (env.get(uppercaseKey) if env else None)
        or getProviderEnvValue(lowercaseKey)
        or getProviderEnvValue(uppercaseKey)
        or ""
    )


def _parseProxyTargetUrl(targetUrl: str | ParseResult) -> ParseResult | None:
    if isinstance(targetUrl, ParseResult):
        return targetUrl

    parsed = urlparse(targetUrl)
    try:
        _ = parsed.port
    except ValueError:
        return None
    return parsed if parsed.scheme and parsed.netloc else None


def _stripBrackets(host: str) -> str:
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def _parseInt(text: str) -> int | None:
    """MISAKA fork: ``Number.parseInt(text, 10)``: the leading integer, or ``None`` for NaN."""
    match = re.match(r"\s*([+-]?\d+)", text)
    return int(match.group(1)) if match else None


def _parseNoProxyEntry(entry: str) -> dict[str, Any] | None:
    trimmed = entry.strip().lower()
    if not trimmed:
        return None

    if trimmed.startswith("["):
        closingBracket = trimmed.find("]")
        if closingBracket != -1:
            host = trimmed[1:closingBracket]
            rest = trimmed[closingBracket + 1 :]
            if rest.startswith(":"):
                port = _parseInt(rest[1:])
                return {"host": host, "port": 0 if port is None else port}
            return {"host": host, "port": 0}

    if ":" in trimmed and len(trimmed.split(":")) > 2:
        return {"host": trimmed, "port": 0}

    colonIndex = trimmed.rfind(":")
    if colonIndex != -1 and colonIndex == trimmed.find(":"):
        host = trimmed[:colonIndex]
        port = _parseInt(trimmed[colonIndex + 1 :])
        if port is not None:
            return {"host": host, "port": port}

    return {"host": trimmed, "port": 0}


def _shouldProxyHostname(hostname: str, port: int, env: ProviderEnv | None = None) -> bool:
    noProxy = _getProxyEnv("no_proxy", env).lower()
    if not noProxy:
        return True
    if noProxy == "*":
        return False

    normalizedTargetHost = _stripBrackets(hostname.lower())

    def keeps(entry: str) -> bool:
        parsed = _parseNoProxyEntry(entry)
        if not parsed:
            return True

        if parsed["port"] and parsed["port"] != port:
            return True

        domain = _stripBrackets(parsed["host"])
        if domain.startswith("*."):
            domain = domain[2:]
        elif domain.startswith(".") or domain.startswith("*"):  # noqa: PIE810
            domain = domain[1:]

        if not domain:
            return True

        if normalizedTargetHost == domain:
            return False

        if normalizedTargetHost.endswith(f".{domain}"):  # noqa: SIM103
            return False

        return True

    return all(keeps(entry) for entry in re.split(r"[,\s]", noProxy))


def _getProxyForUrl(targetUrl: str | ParseResult, env: ProviderEnv | None = None) -> str:
    parsedUrl = _parseProxyTargetUrl(targetUrl)
    if parsedUrl is None or not parsedUrl.scheme or not parsedUrl.netloc:
        return ""

    protocol = parsedUrl.scheme
    hostname = _stripBrackets(parsedUrl.hostname or re.sub(r":\d*$", "", parsedUrl.netloc))
    port = parsedUrl.port or _DEFAULT_PROXY_PORTS.get(protocol) or 0
    if not _shouldProxyHostname(hostname, port, env):
        return ""

    proxy = _getProxyEnv(f"{protocol}_proxy", env) or _getProxyEnv("all_proxy", env)
    if proxy and "://" not in proxy:
        proxy = f"{protocol}://{proxy}"
    return proxy


UNSUPPORTED_PROXY_PROTOCOL_MESSAGE = (
    "Unsupported proxy protocol. SOCKS and PAC proxy URLs are not supported; use an HTTP or HTTPS proxy URL."
)


def resolveHttpProxyUrlForTarget(targetUrl: str | ParseResult, env: ProviderEnv | None = None) -> ParseResult | None:
    proxy = _getProxyForUrl(targetUrl, env)
    if not proxy:
        return None

    proxyUrl = urlparse(proxy)
    try:
        _ = proxyUrl.port
        if not proxyUrl.scheme or not proxyUrl.netloc:
            raise ValueError("Invalid URL")
    except ValueError:
        raise RuntimeError(f"Invalid proxy URL {jsonStringify(proxy)}: Invalid URL") from None

    if proxyUrl.scheme not in ("http", "https"):
        raise RuntimeError(f"{UNSUPPORTED_PROXY_PROTOCOL_MESSAGE} Got {proxyUrl.scheme}:")

    return proxyUrl


__all__ = ["UNSUPPORTED_PROXY_PROTOCOL_MESSAGE", "resolveHttpProxyUrlForTarget"]
