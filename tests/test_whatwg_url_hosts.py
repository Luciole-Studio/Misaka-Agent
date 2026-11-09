"""``misaka.utils.whatwg_url.URL`` against Node's ``new URL``: hosts (IPv4 shorthand, octal and hex parts,
IPv6 compression and embedded IPv4), special schemes without slashes, and what the constructor rejects.
The expectations in ``fixtures/whatwg_url_cases.json`` are Node 24's output for each input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misaka.core.mcp_servers import isLoopbackRedirectUri, validateMcpServerConfig
from misaka.utils.whatwg_url import URL

CASES = json.loads((Path(__file__).parent / "fixtures" / "whatwg_url_cases.json").read_text(encoding="utf-8"))


def _parse(text: str):
    try:
        url = URL(text)
    except TypeError:
        return "ERR"
    return [url.href, url.hostname]


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[case[0] for case in CASES])
def test_parses_like_node(text, expected):
    assert _parse(text) == expected


def test_mcp_validation_reads_hosts_the_way_node_does():
    # The IPv6 loopback in full is still loopback; a URL Node rejects is not an http URL.
    assert isLoopbackRedirectUri("http://[0:0:0:0:0:0:0:1]:5/callback")
    assert validateMcpServerConfig("s", {"url": "https://exa mple.com"}) == 'server "s": url must be an http or https URL'
    assert validateMcpServerConfig("s", {"url": "https:example.com/mcp"}) == {"url": "https:example.com/mcp"}
