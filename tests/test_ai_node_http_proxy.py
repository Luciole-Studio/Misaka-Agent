"""Translated from pi's ``packages/ai/test/node-http-proxy.test.ts`` (v1.1.0).

MISAKA fork: the resolved proxy is a ``urllib`` parse result, so a test compares its scheme and authority where
upstream compares ``URL.toString()`` (which adds the root path).
"""

from __future__ import annotations

import pytest

from misaka.ai.utils.node_http_proxy import (
    UNSUPPORTED_PROXY_PROTOCOL_MESSAGE,
    resolveHttpProxyUrlForTarget,
)

PROXY_ENV_KEYS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "npm_config_http_proxy",
    "npm_config_https_proxy",
    "npm_config_proxy",
    "npm_config_no_proxy",
]


@pytest.fixture(autouse=True)
def resetProxyEnv(monkeypatch):
    for key in PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _origin(target: str, env: dict[str, str] | None = None) -> str | None:
    """``resolveHttpProxyUrlForTarget(...)?.toString()`` without the root path."""
    proxyUrl = resolveHttpProxyUrlForTarget(target, env)
    return None if proxyUrl is None else f"{proxyUrl.scheme}://{proxyUrl.netloc}"


def test_respects_no_proxy_exclusions(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "bedrock-runtime.us-east-1.amazonaws.com")

    assert resolveHttpProxyUrlForTarget("https://bedrock-runtime.us-east-1.amazonaws.com") is None


def test_resolves_http_and_https_proxy_urls(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    assert _origin("https://bedrock-runtime.us-east-1.amazonaws.com") == "http://proxy.example:8080"


def test_prefers_scoped_proxy_env_aliases_before_process_env_aliases(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://process-proxy.example:8080")

    assert (
        _origin("https://bedrock-runtime.us-east-1.amazonaws.com", {"HTTPS_PROXY": "http://scoped-proxy.example:8080"})
        == "http://scoped-proxy.example:8080"
    )


def test_rejects_socks_and_pac_proxy_urls_explicitly(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://proxy.example:1080")

    with pytest.raises(Exception, match=UNSUPPORTED_PROXY_PROTOCOL_MESSAGE.replace(".", r"\.")):
        resolveHttpProxyUrlForTarget("https://bedrock-runtime.us-east-1.amazonaws.com")


def test_handles_subdomain_wildcards_ipv6_and_ports_in_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "example.com, .wildcard.org, *.star.net, ::1, [2001:db8::1], 127.0.0.1:8080")

    assert resolveHttpProxyUrlForTarget("https://example.com") is None
    assert resolveHttpProxyUrlForTarget("https://api.example.com") is None
    assert resolveHttpProxyUrlForTarget("https://wildcard.org") is None
    assert resolveHttpProxyUrlForTarget("https://api.wildcard.org") is None
    assert resolveHttpProxyUrlForTarget("https://star.net") is None
    assert resolveHttpProxyUrlForTarget("https://api.star.net") is None
    assert _origin("https://notexample.com") == "http://proxy.example:8080"

    assert resolveHttpProxyUrlForTarget("https://[::1]:80") is None
    assert resolveHttpProxyUrlForTarget("https://[2001:db8::1]") is None
    assert resolveHttpProxyUrlForTarget("https://127.0.0.1:8080") is None
    assert _origin("https://127.0.0.1:3000") == "http://proxy.example:8080"
