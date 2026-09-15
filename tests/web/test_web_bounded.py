"""The outbound-fetch trust boundary: address vetting, pinning, hops, byte caps."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import pytest

from misaka.core.tools._web import bounded

PUBLIC = "93.184.216.34"
OTHER_PUBLIC = "23.192.228.80"


@dataclass
class _AbortSignal:
    """The duck type ``misaka.utils.values.signal_aborted`` reads."""

    aborted: bool = False


class _Resolver(dict):
    """host -> addresses, plus the log of names actually looked up."""

    asked: list[str]


@pytest.fixture
def resolver(monkeypatch):
    """Replace DNS with a host -> addresses table; no test may touch the network.

    ``asked`` records every name handed to the resolver, which is how the tests
    below tell "vetted the name httpx will dial" from "vetted some other name".
    """
    table = _Resolver()
    table.asked = []

    async def _resolve_host(host, _port):
        table.asked.append(host)
        if host not in table:
            raise OSError(f"unknown host {host}")
        return table[host]

    monkeypatch.setattr(bounded, "_resolve_host", _resolve_host)
    return table


def _recorder(handler):
    """``(transport, sent)``, where *sent* snapshots each request as it goes out.

    Snapshots, not live ``httpx.Request`` objects: ``open_checked_stream``
    restores the logical URL onto the final request afterwards, so a retained
    reference would report a URL that was never dialled.
    """
    sent: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "extensions": dict(request.extensions),
        })
        return handler(request)

    return httpx.MockTransport(record), sent


NON_PUBLIC = [
    ("private-v4", "10.0.0.1"),
    ("loopback-v4", "127.0.0.1"),
    ("link-local-metadata", "169.254.169.254"),
    ("multicast-v4", "224.0.0.1"),
    ("reserved-v4", "240.0.0.1"),
    ("unspecified-v4", "0.0.0.0"),
    ("cgnat-v4", "100.64.0.1"),
    ("loopback-v6", "::1"),
    ("link-local-v6", "fe80::1"),
    ("unique-local-v6", "fc00::1"),
    ("multicast-v6", "ff02::1"),
    ("mapped-loopback", "::ffff:127.0.0.1"),
    # CPython reports is_global True for these two: the predicate must not lean
    # on is_global alone.
    ("site-local-v6", "fec0::1"),
    ("site-local-v6-top", "feff::1"),
]


@pytest.mark.parametrize("address", [case[1] for case in NON_PUBLIC], ids=[case[0] for case in NON_PUBLIC])
async def test_vet_public_url_refuses_every_non_public_address(resolver, address):
    resolver["host.example"] = [address]
    with pytest.raises(bounded.UnsafeUrlError, match="local or private"):
        await bounded.vet_public_url("https://host.example/path")


@pytest.mark.parametrize("address", [PUBLIC, "2606:4700::1111"])
async def test_vet_public_url_accepts_public_addresses(resolver, address):
    resolver["host.example"] = [address]
    assert await bounded.vet_public_url("https://host.example/path") == (address,)


async def test_vet_public_url_refuses_a_split_horizon_answer(resolver):
    resolver["host.example"] = [PUBLIC, "192.168.1.5"]
    with pytest.raises(bounded.UnsafeUrlError):
        await bounded.vet_public_url("https://host.example/")


async def test_vetted_addresses_come_back_ipv4_first(resolver):
    resolver["host.example"] = ["2606:4700::1111", PUBLIC]
    assert await bounded.vet_public_url("https://host.example/") == (PUBLIC, "2606:4700::1111")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "http or https"),
        ("ftp://host.example/x", "http or https"),
        ("https://", "http or https"),
        ("not a url", "http or https"),
        ("https://user:pw@host.example/", "credentials"),
        ("http://localhost:8080/", "local address"),
        ("http://svc.localhost/", "local address"),
        ("https://nowhere.invalid/", "resolution failed"),
        ("http://host.example:notaport/", "not valid"),
        ("http://host.example\t/", "not valid"),
    ],
)
async def test_vet_public_url_refuses_malformed_or_local_urls(resolver, url, reason):
    resolver["host.example"] = [PUBLIC]
    with pytest.raises(bounded.UnsafeUrlError, match=reason):
        await bounded.vet_public_url(url)


CGNAT_AND_METADATA = [
    ("cgnat", "100.64.1.1"),
    ("aws-metadata", "169.254.169.254"),
    ("aws-metadata-v4-mapped", "::ffff:169.254.169.254"),
    ("ecs-task-metadata", "169.254.170.2"),
    ("alibaba-metadata", "100.100.100.200"),
]


@pytest.mark.parametrize(
    "address",
    [case[1] for case in CGNAT_AND_METADATA],
    ids=[case[0] for case in CGNAT_AND_METADATA],
)
async def test_cgnat_and_metadata_addresses_are_refused_by_default(resolver, address):
    """CGNAT reports neither is_private nor is_global; it needs its own test."""
    resolver["host.example"] = [address]
    with pytest.raises(bounded.UnsafeUrlError):
        await bounded.vet_public_url("https://host.example/")


async def test_allow_private_urls_opens_the_private_classes(monkeypatch, resolver):
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", "true")
    resolver["dev.example"] = ["10.0.0.7"]
    assert await bounded.vet_public_url("http://dev.example:8080/") == ("10.0.0.7",)


async def test_allow_private_urls_also_reaches_localhost(monkeypatch, resolver):
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", "1")
    resolver["localhost"] = ["127.0.0.1"]
    assert await bounded.vet_public_url("http://localhost:3000/") == ("127.0.0.1",)


@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "169.254.1.1"])
async def test_the_metadata_floor_survives_allow_private_urls(monkeypatch, resolver, address):
    """The opt-out exists for corporate DNS, never for instance credentials."""
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", "true")
    resolver["host.example"] = [address]
    with pytest.raises(bounded.UnsafeUrlError):
        await bounded.vet_public_url("https://host.example/")


async def test_a_metadata_hostname_is_refused_before_any_resolution(monkeypatch, resolver):
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", "true")
    with pytest.raises(bounded.UnsafeUrlError, match="metadata"):
        await bounded.vet_public_url("http://metadata.google.internal/computeMetadata/v1/")
    assert resolver.asked == []


async def test_vet_public_url_refuses_an_empty_resolution(resolver):
    resolver["host.example"] = []
    with pytest.raises(bounded.UnsafeUrlError, match="did not resolve"):
        await bounded.vet_public_url("https://host.example/")


async def test_the_vetted_name_is_the_one_httpx_would_dial(resolver):
    """U+3002 folds to '.' for httpx but not for urllib -- vetting must agree
    with the dialler, or the check runs against a name nobody connects to."""
    resolver["foo.bar.example"] = [PUBLIC]
    assert await bounded.vet_public_url("http://foo。bar.example/") == (PUBLIC,)
    assert resolver.asked == ["foo.bar.example"]


async def test_the_socket_is_pinned_to_the_vetted_address(resolver):
    resolver["public.example"] = [PUBLIC]
    transport, sent = _recorder(lambda _r: httpx.Response(200, text="ok"))
    async with bounded.open_checked_stream(
        "https://public.example/a?q=1", transport=transport
    ) as response:
        assert response.status_code == 200
        # The caller still sees the URL it asked for, not the pinned one.
        assert str(response.url) == "https://public.example/a?q=1"
    assert sent[0]["url"] == f"https://{PUBLIC}/a?q=1"
    assert sent[0]["headers"]["host"] == "public.example"
    # Without this the TLS handshake would verify the certificate against the IP
    # literal, so every pinned https fetch would fail -- or, worse, be made to
    # pass by relaxing verification.
    assert sent[0]["extensions"]["sni_hostname"] == "public.example"
    # One resolution only: nothing is left for a rebinding resolver to answer.
    assert resolver.asked == ["public.example"]


async def test_a_literal_ip_url_is_not_rewritten(resolver):
    resolver[PUBLIC] = [PUBLIC]
    transport, sent = _recorder(lambda _r: httpx.Response(200, text="ok"))
    async with bounded.open_checked_stream(f"https://{PUBLIC}/", transport=transport):
        pass
    assert sent[0]["url"] == f"https://{PUBLIC}/"
    assert sent[0]["headers"]["host"] == PUBLIC
    assert "sni_hostname" not in sent[0]["extensions"]


async def test_every_hop_is_pinned_to_its_own_vetted_address(resolver):
    resolver["public.example"] = [PUBLIC]
    resolver["other.example"] = [OTHER_PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "public.example":
            return httpx.Response(302, headers={"location": "https://other.example/next"})
        return httpx.Response(200, text="ok")

    transport, sent = _recorder(handler)
    async with bounded.open_checked_stream(
        "https://public.example/", transport=transport
    ) as response:
        assert str(response.url) == "https://other.example/next"
    assert [record["url"] for record in sent] == [
        f"https://{PUBLIC}/",
        f"https://{OTHER_PUBLIC}/next",
    ]
    assert resolver.asked == ["public.example", "other.example"]


async def test_a_redirect_into_the_private_network_is_refused_before_it_is_dialled(resolver):
    resolver["public.example"] = [PUBLIC]
    resolver["metadata.example"] = ["169.254.169.254"]
    transport, sent = _recorder(
        lambda _r: httpx.Response(302, headers={"location": "http://metadata.example/latest/meta-data/"})
    )
    with pytest.raises(bounded.UnsafeUrlError, match="local or private"):
        async with bounded.open_checked_stream("https://public.example/", transport=transport):
            pytest.fail("the refused hop must not yield a response")
    assert [record["headers"]["host"] for record in sent] == ["public.example"]


async def test_a_malformed_redirect_target_stays_an_http_error(resolver):
    """A hostile Location header must not escape as a bare httpx.InvalidURL,
    which is not an HTTPError and so slips past a caller's error handling."""
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(302, headers={"location": "http://[::1"})
    )
    with pytest.raises(httpx.HTTPError):
        async with bounded.open_checked_stream("https://public.example/", transport=transport):
            pytest.fail("a malformed hop must not yield a response")


async def test_a_cross_origin_redirect_drops_the_caller_credentials(resolver):
    resolver["public.example"] = [PUBLIC]
    resolver["other.example"] = [OTHER_PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "public.example":
            return httpx.Response(302, headers={"location": "https://other.example/next"})
        return httpx.Response(200, text="ok")

    transport, sent = _recorder(handler)
    async with bounded.open_checked_stream(
        "https://public.example/",
        headers={"Authorization": "Bearer secret", "Cookie": "sid=1", "Accept": "text/html"},
        transport=transport,
    ) as response:
        assert response.status_code == 200
    first, second = sent
    assert first["headers"]["authorization"] == "Bearer secret"
    assert "authorization" not in second["headers"]
    assert "cookie" not in second["headers"]
    assert second["headers"]["accept"] == "text/html"


async def test_a_same_origin_redirect_keeps_the_caller_credentials(resolver):
    resolver["public.example"] = [PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/moved"})
        return httpx.Response(200, text="ok")

    transport, sent = _recorder(handler)
    async with bounded.open_checked_stream(
        "https://public.example/",
        headers={"Authorization": "Bearer secret"},
        transport=transport,
    ) as response:
        assert response.status_code == 200
    assert sent[1]["headers"]["authorization"] == "Bearer secret"


async def test_a_redirect_loop_stops_at_the_hop_limit(resolver):
    resolver["public.example"] = [PUBLIC]
    transport, sent = _recorder(lambda _r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(httpx.TooManyRedirects):
        async with bounded.open_checked_stream(
            "https://public.example/", max_redirects=3, transport=transport
        ):
            pytest.fail("a looping chain must not yield a response")
    assert len(sent) == 4


async def test_the_body_is_read_only_up_to_the_cap(resolver):
    resolver["public.example"] = [PUBLIC]
    pulled = 0

    async def chunks():
        nonlocal pulled
        for _ in range(10):
            pulled += 1
            yield b"x" * 64

    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=chunks()))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        body, truncated = await bounded.read_bounded(response, max_bytes=100)
    assert truncated
    assert body == b"x" * 100
    # The point of the cap is that the rest is never transferred: 2 * 64 crosses
    # 100, so the remaining eight chunks must stay on the wire.
    assert pulled == 2


async def test_a_body_exactly_at_the_cap_is_not_marked_truncated(resolver):
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 100))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        assert await bounded.read_bounded(response, max_bytes=100) == (b"x" * 100, False)


async def test_a_body_under_the_cap_is_not_marked_truncated(resolver):
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"short"))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        assert await bounded.read_bounded(response, max_bytes=100) == (b"short", False)


CONTENT_TYPES = [
    ("text/html; charset=utf-8", True),
    ("TEXT/PLAIN", True),
    ("application/json", True),
    ("application/ld+json", True),
    ("application/xhtml+xml", True),
    ("application/xml", True),
    (None, True),
    ("", True),
    ("image/png", False),
    ("application/pdf", False),
    ("application/octet-stream", False),
    ("video/mp4", False),
    ("application/zip; name=x.zip", False),
]


@pytest.mark.parametrize(("content_type", "is_text"), CONTENT_TYPES)
def test_content_types_are_classified(content_type, is_text):
    assert bounded.is_text_content_type(content_type) is is_text


@pytest.mark.parametrize(
    ("charset", "raw", "expected"),
    [
        ("utf-8", "héllo".encode(), "héllo"),
        ("shift_jis", "日本".encode("shift_jis"), "日本"),
        ("definitely-not-a-charset", b"plain", "plain"),
    ],
)
async def test_the_body_is_decoded_with_the_declared_charset(resolver, charset, raw, expected):
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=raw, headers={"content-type": f"text/plain; charset={charset}"})
    )
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        body, _truncated = await bounded.read_bounded(response)
        assert bounded.decode_body(response, body) == expected


async def test_a_multibyte_sequence_cut_at_the_cap_decodes_to_a_replacement(resolver):
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content="ok日".encode()))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        body, truncated = await bounded.read_bounded(response, max_bytes=3)
        assert truncated
        assert bounded.decode_body(response, body) == "ok�"


async def test_a_dribbling_server_is_stopped_by_the_total_deadline(resolver):
    """The per-operation timeout never fires for a server that keeps sending.

    Without a deadline the loop below runs to the byte cap no matter how slowly the
    bytes arrive, which is the shape that can hang a session for hours.
    """
    resolver["public.example"] = [PUBLIC]
    pulled = 0

    async def chunks():
        nonlocal pulled
        for _ in range(1000):
            pulled += 1
            yield b"x"

    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=chunks()))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        with pytest.raises(httpx.ReadTimeout):
            # A deadline already in the past: the first chunk is enough to trip it.
            await bounded.read_bounded(
                response, max_bytes=100_000, deadline=time.monotonic() - 1
            )
    assert pulled == 1


async def test_an_abort_stops_the_read_mid_body(resolver):
    resolver["public.example"] = [PUBLIC]
    pulled = 0
    signal = _AbortSignal()

    async def chunks():
        nonlocal pulled
        for _ in range(100):
            pulled += 1
            if pulled == 3:
                signal.aborted = True
            yield b"x" * 8

    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=chunks()))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        with pytest.raises(RuntimeError, match="aborted"):
            await bounded.read_bounded(response, max_bytes=100_000, signal=signal)
    assert pulled == 3


async def test_a_read_without_a_deadline_or_signal_is_unchanged(resolver):
    """The two new parameters are opt-in; download paths that pass neither must not
    acquire a ceiling by accident."""
    resolver["public.example"] = [PUBLIC]
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"body"))
    async with bounded.open_checked_stream("https://public.example/", transport=transport) as response:
        assert await bounded.read_bounded(response) == (b"body", False)
