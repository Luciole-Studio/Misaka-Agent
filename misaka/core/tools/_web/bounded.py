"""The trust boundary every outbound agent fetch crosses: SSRF vetting, per-hop
redirect checks, address pinning, and bounded body reads.

A URL reaching these helpers is attacker-controlled input -- the model may have
copied it out of a page it just read -- so nothing here fails open. The defences
are inseparable: vetting only the first URL is defeated by a 302 to a metadata
endpoint, vetting a *name* and then letting the client resolve it again is
defeated by a resolver that answers twice, and an unbounded read turns any URL
into a memory bomb.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx

#: Bytes of one response body a caller keeps by default. Callers that stream to
#: disk pass their own ceiling.
DEFAULT_MAX_FETCH_BYTES = 2 * 1024 * 1024

#: Redirect hops a fetch may follow. Well below httpx's own 20: every hop is a
#: fresh DNS resolution plus a fresh chance to be pointed somewhere private.
MAX_REDIRECT_HOPS = 5

# Headers that authenticate the caller. httpx drops these itself when it follows
# a cross-origin redirect; a hand-rolled hop loop has to do it explicitly or the
# credential leaks to whatever host the first origin names.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

# Response types whose bytes are text worth handing to a model. An allowlist,
# not a blocklist of known-binary types: an unrecognised type is far more likely
# to be a new blob format than a new text format.
_TEXT_CONTENT_TYPES = frozenset({
    "application/ecmascript",
    "application/javascript",
    "application/json",
    "application/x-javascript",
    "application/x-ndjson",
    "application/x-yaml",
    "application/xml",
    "application/yaml",
})


class UnsafeUrlError(ValueError):
    """A URL, or a redirect hop, that must not be fetched."""


def _is_public_address(raw: str) -> bool:
    """Whether one resolved address may be dialled.

    ``is_global`` alone is not the test, in either direction: CPython reports
    ``is_global`` True for multicast (``224.0.0.1``, ``ff02::1``) and for
    deprecated IPv6 site-local (``fec0::/10``, an intranet range), so every
    non-public class is named explicitly rather than assumed to be covered.
    ``is_site_local`` exists only on IPv6 addresses, hence the ``getattr``.
    """
    address = ipaddress.ip_address(raw.split("%", 1)[0])
    return address.is_global and not (
        address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
    )


async def _resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


def _ipv4_first(addresses: list[str]) -> tuple[str, ...]:
    """Vetted addresses ordered IPv4 first.

    :func:`open_checked_stream` dials only the first one, and a container with
    no IPv6 route is the common case; plain string sort would put ``2606::``
    ahead of ``93.184.…`` and strand those callers.
    """
    return tuple(
        sorted(addresses, key=lambda raw: (ipaddress.ip_address(raw.split("%", 1)[0]).version, raw))
    )


async def vet_public_url(url: str) -> tuple[str, ...]:
    """The addresses *url* resolves to, once every one of them is public.

    Fail-closed: a name that cannot be parsed or resolved cannot be vetted, and
    a single private answer rejects the URL, so a split-horizon resolver cannot
    smuggle one private address through in a multi-address reply.

    Parsing is ``httpx.URL`` rather than ``urllib.parse`` because httpx is what
    eventually dials: the two disagree about hosts (``foo。bar`` IDNA-folds to
    ``foo.bar`` for one and not the other), and a check that parses the URL
    differently from the dialler is a check that can be walked around.

    Raises :class:`UnsafeUrlError` with the reason. The addresses come back
    ordered IPv4 first, for :func:`open_checked_stream` to pin the socket to.
    """
    try:
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii").rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (httpx.InvalidURL, UnicodeDecodeError) as error:
        raise UnsafeUrlError(f"URL is not valid: {error}") from error
    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsafeUrlError("URL must use http or https")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL credentials are not allowed")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("URL resolves to a local address")
    try:
        addresses = await _resolve_host(host, port)
    except (OSError, UnicodeError) as error:
        raise UnsafeUrlError(f"host resolution failed: {error}") from error
    if not addresses:
        raise UnsafeUrlError("host did not resolve")
    for raw in addresses:
        if not _is_public_address(raw):
            raise UnsafeUrlError("URL resolves to a local or private address")
    return _ipv4_first(addresses)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = httpx.URL(url)
    return (parsed.scheme, parsed.host, parsed.port)


def _strip_cross_origin_credentials(
    headers: dict[str, str], from_url: str, to_url: str
) -> dict[str, str]:
    if _origin(from_url) == _origin(to_url):
        return headers
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _CREDENTIAL_HEADERS
    }


def _pin_to_address(
    url: str, address: str, headers: dict[str, str]
) -> tuple[str, dict[str, str], dict[str, object]]:
    """Rewrite one hop to dial *address*, the one vetting looked at.

    The hostname survives twice over: in ``Host`` (so virtual-host routing still
    works) and in the ``sni_hostname`` extension, which httpcore feeds to
    ``server_hostname`` -- so TLS certificate verification still runs against
    the name and a pinned HTTPS request still fails closed on a mismatch.

    A no-op when the URL already names the address, i.e. no DNS was consulted.
    """
    parsed = httpx.URL(url)
    host = parsed.raw_host.decode("ascii").rstrip(".")
    if not host or host == address:
        return url, headers, {}
    return (
        str(parsed.copy_with(host=address)),
        {**headers, "Host": parsed.netloc.decode("ascii")},
        {"sni_hostname": host},
    )


@asynccontextmanager
async def open_checked_stream(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_redirects: int = MAX_REDIRECT_HOPS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[httpx.Response]:
    """Walk *url*'s redirect chain by hand and yield the final open response.

    Every hop is vetted and then dialled at the address that vetting resolved,
    so a resolver that answers publicly for the check and privately for the
    connection (DNS rebinding) has nothing to rebind: the socket never consults
    it a second time.

    The body is not read here: the caller reads it inside this context (with
    :func:`read_bounded`) so an oversized response can be abandoned mid-transfer.
    ``response.url`` is the logical URL of the final hop, not the pinned one.

    The client is built here rather than accepted from the caller because two of
    its settings are load-bearing: ``follow_redirects=False`` is what makes
    per-hop vetting possible at all, and ``trust_env=False`` keeps a proxy
    environment variable from routing the request past the address that was
    checked. ``transport`` exists so tests can answer without a network.

    Raises :class:`UnsafeUrlError` for a hop that fails vetting -- never a quiet
    "no more redirects", which a caller would report as a successful fetch --
    and ``httpx.TooManyRedirects`` past ``max_redirects``.

    # ponytail: only the first vetted address is dialled, with no failover to
    # the rest, because failover inside the hop loop means driving
    # ``client.stream``'s context by hand. IPv4-first ordering covers the case
    # that actually bites (an IPv6 answer on a host with no IPv6 route);
    # upgrade path is a retry loop over the returned addresses on ConnectError.
    """
    request_headers = dict(headers or {})
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=timeout, transport=transport
    ) as client:
        for _hop in range(max_redirects + 1):
            addresses = await vet_public_url(current)
            dial_url, dial_headers, extensions = _pin_to_address(
                current, addresses[0], request_headers
            )
            async with client.stream(
                method, dial_url, headers=dial_headers, extensions=extensions
            ) as response:
                location = response.headers.get("location", "").strip() if response.is_redirect else ""
                if not location:
                    # Undo the pin for the caller: it reports and resolves
                    # against the URL it asked for, not the socket's address.
                    response.request.url = httpx.URL(current)
                    yield response
                    return
                # No InvalidURL guard: httpx parses the Location header itself
                # while building response.next_request and reports a bad one as
                # RemoteProtocolError before this line is ever reached.
                target = str(httpx.URL(current).join(location))
            request_headers = _strip_cross_origin_credentials(request_headers, current, target)
            current = target
    raise httpx.TooManyRedirects(f"more than {max_redirects} redirects")


async def read_bounded(
    response: httpx.Response, max_bytes: int = DEFAULT_MAX_FETCH_BYTES
) -> tuple[bytes, bool]:
    """``(body, truncated)`` for a streaming response, reading at most one chunk
    past ``max_bytes``.

    Must be called inside :func:`open_checked_stream`. Returning early closes the
    connection, so a 2GB URL costs the cap, not the file.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            return b"".join(chunks)[:max_bytes], True
    return b"".join(chunks), False


def is_text_content_type(content_type: str | None) -> bool:
    """Whether a Content-Type names text a model can read.

    A missing type counts as text: servers omit it far more often than they send
    an unlabelled blob, and the byte cap plus replacement decoding bound what a
    wrong guess costs.
    """
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    if not bare:
        return True
    return (
        bare.startswith("text/")
        or bare in _TEXT_CONTENT_TYPES
        or bare.endswith(("+json", "+xml"))
    )


def decode_body(response: httpx.Response, body: bytes) -> str:
    """Decode a bounded body with the response's declared charset.

    ``errors="replace"`` because a multi-byte sequence cut at the cap boundary
    must not raise. No sniffing: an absent or unknown charset falls back to
    UTF-8, which is what the web overwhelmingly is.
    """
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


__all__ = [
    "DEFAULT_MAX_FETCH_BYTES",
    "MAX_REDIRECT_HOPS",
    "UnsafeUrlError",
    "decode_body",
    "is_text_content_type",
    "open_checked_stream",
    "read_bounded",
    "vet_public_url",
]
