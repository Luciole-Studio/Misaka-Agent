"""AI Gateway transport over the Workers AI binding.

misaka's Cloudflare AI Gateway support speaks HTTPS
(``gateway.ai.cloudflare.com/v1/{account}/{gateway}/{provider}/...``, see
``providers/cloudflare.py`` and the ``cloudflare-ai-gateway`` provider definition), which
needs a Cloudflare API token even when the caller already runs inside the gateway's own
account. This module is the other transport for that same URL space: it does not add a
provider, an ``Api``, or a second base-URL resolver -- the model still carries the HTTPS
base URL that ``resolve_cloudflare_base_url`` expands, and this only changes what happens
to the request after the SDK has built it.

``create_gateway_binding_transport`` returns a transport that translates requests under a
gateway HTTPS prefix into calls to the Workers AI binding's universal endpoint,
``env.AI.gateway(id).run({provider, endpoint, headers, query})``. Binding calls are
pre-authenticated in-account and return the provider's native wire format as a regular
(streaming) response, so API implementations behave identically over either transport.

The result is the transport for one gateway-bound client, not a general-purpose one:
requests it cannot serve -- URLs outside the prefix, or in-prefix requests the universal
endpoint cannot express (non-POST, non-JSON body) -- raise a descriptive error. Transport
selection is the caller's job, per client: route such traffic over HTTPS with real gateway
auth instead of through this shim.

Why an ``httpx`` transport rather than pi's ``FetchFunction``: pi hands a replacement
``fetch`` to the vendor SDKs, and the Python spelling of that hook is ``http_client=``, a
client a transport is installed into -- ``http_client=httpx.AsyncClient(transport=...)``.
That is exact for ``anthropic`` (its ``http_client`` is typed ``httpx.AsyncClient``); the
installed ``openai`` 3.3.1 has moved to the separate ``httpx2`` distribution and types its
``http_client`` as ``httpx2.AsyncClient``, whose ``AsyncBaseTransport`` is a different class
from this module's, so an OpenAI client would need an httpx2-side equivalent. No misaka code
wires this transport into either SDK today.

``httpx.Request`` is also what collapses three of pi's branches -- it carries one
already-merged method, URL, header set and body, so the fetch-spec rules for
"``init.headers`` replaces the Request's", "``body: null`` clears it" and
"``signal: null`` clears it" have no counterpart here: there is only ever one source for
each.

The two transports are not the same *cut*, though, and that shows up twice. Headers: the
``fetch`` seam sits above undici's default headers, an ``AsyncBaseTransport`` sits below
``AsyncClient``'s, so this module has to strip more names than pi does -- see
``_STRIP_HEADERS``. Cancellation: pi forwards the ``fetch`` init's ``AbortSignal`` to
``run()``, and the only per-request bag an httpx transport is handed is
``request.extensions``, so a signal has to ride there
(``client.build_request(..., extensions={"signal": signal})`` then ``client.send(request)``,
which passes the ``extensions`` entry through to the transport). That is reachable only for
a caller that builds the ``httpx.Request`` itself: under the ``http_client=`` wiring above,
``anthropic`` and ``openai`` build their own requests and expose no caller-facing
``extensions`` hook (their ``_base_client`` sets ``extensions`` only for ``sni_hostname``),
so this transport sees no signal. ``pi_messages.py`` does build its own request, but it
passes no ``extensions`` either. Cancellation therefore arrives the Python way -- the caller
races the SDK call against its signal (``_await_with_signal`` in ``providers/_common.py``,
this repo's convention), whose cancellation propagates down the await chain into the
pending ``binding.gateway(...).run(...)``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

# Placeholder value for auth headers on binding-routed requests. misaka's API
# implementations refuse to dispatch without an API key (``No API key for provider: ...``;
# pi additionally accepts a bare ``authorization`` / ``cf-aig-authorization`` header in its
# ``getClientApiKey``, which this port does not have). Binding calls are pre-authenticated,
# so the sentinel is what gets passed as that API key -- for the ``cloudflare-ai-gateway``
# provider the adapters turn it into ``cf-aig-authorization: Bearer {api_key}``
# (``openai_completions.py``, ``openai_responses.py``, ``anthropic.py``), and the shim
# strips ``cf-aig-authorization`` before calling the binding. Pair it with dropped
# ``Authorization`` / ``x-api-key`` headers, so the SDKs' placeholder auth headers are not
# sent on to the gateway.
CLOUDFLARE_GATEWAY_BINDING_AUTH_SENTINEL = "cloudflare-gateway-binding"

# Never forwarded to the binding: headers that describe the local hop, and gateway auth
# (binding calls are pre-authenticated; the sentinel must not reach the wire).
#
# pi strips three names (`content-length`, `host`, `cf-aig-authorization`;
# cloudflare-gateway-binding.ts:71) because its seam is `fetch`: undici adds `accept`,
# `accept-encoding`, `connection`, `user-agent` and the body framing *below* that seam, so
# pi's collectHeaders never sees them. An httpx transport is cut on the other side of that
# line -- `AsyncClient` merges its default headers into the `Request` before handing it to
# `handle_async_request` -- so this seam really is handed `accept: */*`,
# `accept-encoding: gzip, deflate`, `connection: keep-alive`, `user-agent: python-httpx/...`
# and, for a streaming body, `transfer-encoding: chunked`. Each names something about the
# HTTP hop httpx would have made and never makes here: the request the provider sees is the
# one the gateway builds from `{provider, endpoint, headers, query}`. Forwarding them would
# be a wire-level lie -- `connection` and `transfer-encoding` are hop-by-hop headers
# (RFC 9110 s7.6.1) that must not be relayed at all, `accept-encoding` would negotiate a
# compressed provider response on behalf of an httpx hop that will never decode it, and
# `user-agent` would announce httpx as the client the provider is talking to. The set is
# therefore derived from httpx's seam, not copied from pi's.
#
# `accept` and `user-agent` are a different case and must not be stripped unconditionally:
# httpx *replaces* its defaults with whatever the caller set rather than merging them, so a
# caller's value is perfectly distinguishable here -- and misaka sets both for real
# (`providers/pi_messages.py` asks for `text/event-stream`, the OAuth flows ask for
# `application/json`). Dropping a caller's `accept` would turn an SSE request into a
# buffered one. Only httpx's own default form is removed.
_STRIP_HEADERS = frozenset(
    {
        "content-length",
        "host",
        "cf-aig-authorization",
        "accept-encoding",
        "connection",
        "transfer-encoding",
    }
)

_HTTPX_DEFAULT_ACCEPT = "*/*"
_HTTPX_USER_AGENT_PREFIX = "python-httpx/"


def _is_httpx_default(name: str, value: str) -> bool:
    """Whether this header is httpx's own default rather than something the caller meant."""
    if name == "accept":
        return value == _HTTPX_DEFAULT_ACCEPT
    if name == "user-agent":
        return value.startswith(_HTTPX_USER_AGENT_PREFIX)
    return False


@dataclass(slots=True)
class AiGatewayUniversalRequest:
    """One universal-endpoint request entry, as accepted by ``AiGateway.run()``.

    A dataclass rather than pi's object literal so the binding -- which is supplied by the
    caller and may be a stub -- gets a named, checkable shape instead of a bare mapping.
    """

    provider: str
    endpoint: str
    headers: dict[str, str]
    query: Any


@runtime_checkable
class AiGatewayBindingGateway(Protocol):
    """The one gateway handle returned by ``binding.gateway(id)``.

    pi's ``run(data, options?)`` takes an options bag whose only member is ``signal``;
    a keyword argument is the Python spelling of that, and omitting it is pi's ``{}``.
    """

    def run(
        self, data: AiGatewayUniversalRequest, *, signal: Any = None
    ) -> Awaitable[httpx.Response]: ...


@runtime_checkable
class AiGatewayBinding(Protocol):
    """Structural type for the Workers AI binding's gateway surface (``env.AI``).

    Structural so this module depends on no Cloudflare runtime package; any real ``Ai``
    binding satisfies it.
    """

    def gateway(self, id: str) -> AiGatewayBindingGateway: ...


@dataclass(slots=True)
class GatewayBindingOptions:
    binding: AiGatewayBinding
    """The Workers AI binding (e.g. ``env.AI``)."""

    base_url: str
    """Gateway HTTPS prefix every request must fall under, without a trailing slash:
    ``https://gateway.ai.cloudflare.com/v1/{accountId}/{gatewayName}``."""

    gateway: str
    """Gateway name passed to ``binding.gateway()``. Must match the ``base_url`` gateway."""


class _GatewayBindingTransport(httpx.AsyncBaseTransport):
    """Not exported: callers hold the ``httpx.AsyncBaseTransport`` the factory returns and
    install it into a client (``httpx.AsyncClient(transport=...)``)."""

    def __init__(self, options: GatewayBindingOptions) -> None:
        self._binding = options.binding
        self._gateway = options.gateway
        # Prefix matching runs on URL-normalized components (origin + path), not raw
        # strings: dot segments resolve away and fragments drop, matching what a real
        # client would put on the wire, so a lexical variant cannot split
        # provider/endpoint differently than HTTPS would. The path is taken percent-
        # encoded (``raw_path``) rather than decoded, so an encoded %2F inside a segment
        # cannot masquerade as the provider/endpoint separator.
        base = httpx.URL(options.base_url)
        self._base_origin = (base.scheme, base.host, base.port)
        base_path = _raw_path(base)[0]
        self._base_path = base_path if base_path.endswith("/") else f"{base_path}/"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        url = request.url
        path, search = _raw_path(url)

        # Out-of-prefix URLs are a configuration bug, not passthrough traffic: silently
        # forwarding would ship the auth sentinel to whatever host the URL names.
        if (url.scheme, url.host, url.port) != self._base_origin or not path.startswith(self._base_path):
            origin = httpx.URL(scheme=self._base_origin[0], host=self._base_origin[1], port=self._base_origin[2])
            raise RuntimeError(
                f"create_gateway_binding_transport: {method} {url} is outside the configured gateway "
                f"prefix ({origin}{self._base_path}); this transport only serves its gateway-bound client"
            )

        # In-prefix requests the universal endpoint cannot express always reject:
        # forwarding them over HTTPS would send the sentinel to the gateway and fail with
        # a misleading auth error instead of naming the real problem. Callers that need
        # such endpoints route them over HTTPS with real gateway auth themselves.
        def unexpressible(reason: str) -> RuntimeError:
            return RuntimeError(
                f"create_gateway_binding_transport: cannot express {method} {url} as a universal "
                f"gateway request ({reason}); route it over HTTPS with gateway auth instead"
            )

        if method != "POST":
            raise unexpressible("only POST is supported")

        rest = path[len(self._base_path) :]
        slash = rest.find("/")
        if slash <= 0:
            raise unexpressible("missing provider/endpoint path")
        provider = rest[:slash]
        # Keep the query string on the endpoint -- it is part of what HTTPS would have sent.
        endpoint = rest[slash + 1 :] + search

        # ``aread`` consumes a one-shot streaming body exactly once, which is pi's
        # ``request.clone().text()`` minus the clone: unexpressible requests reject rather
        # than replay, so nothing downstream needs the body again.
        body = await request.aread()
        if not body:
            # httpx has no "no body" state distinct from an empty one, so an empty body is
            # reported as pi's missing-body case rather than as its non-JSON one.
            raise unexpressible("missing body")
        try:
            # json.loads decodes bytes itself; both a bad encoding and bad syntax surface
            # as ValueError subclasses.
            query = json.loads(body)
        except ValueError:
            raise unexpressible("non-JSON body") from None

        data = AiGatewayUniversalRequest(
            provider=provider,
            endpoint=endpoint,
            headers=_collect_headers(request),
            query=query,
        )
        # httpx has no request-level cancellation of its own, so a signal for pi's
        # ``run(data, {signal})`` can only ride in ``extensions``, the per-request bag
        # ``Client.send`` hands to transports. Only a caller that builds the
        # ``httpx.Request`` itself can put one there; under the ``http_client=`` wiring in
        # the module docs the SDKs build their own requests, so this is ``None`` and
        # cancellation reaches the binding as asyncio cancellation of the awaiting task
        # instead. No in-repo caller populates the key today (only the tests do), but the
        # lookup keeps the pi-side signal path available rather than hardcoding ``None``.
        signal = request.extensions.get("signal")
        return await self._binding.gateway(self._gateway).run(data, signal=signal)


def _raw_path(url: httpx.URL) -> tuple[str, str]:
    """Split a URL's wire-form ``raw_path`` into its percent-encoded path and its search.

    ``httpx.URL.path`` is percent-*decoded* and drops the query, so it can neither be
    matched against the prefix nor re-attached to the endpoint faithfully.

    An empty query contributes nothing: ``httpx.URL("https://x/y?").raw_path`` keeps the
    trailing ``?``, while the ``URL.search`` pi appends to the endpoint is ``""`` for both
    ``/y`` and ``/y?`` (a lone ``?`` is not part of WHATWG's serialized query). Testing the
    query rather than the separator is what keeps a stray ``?`` off the endpoint.
    """
    path, _, query = url.raw_path.decode("ascii").partition("?")
    return path, f"?{query}" if query else ""


def _collect_headers(request: httpx.Request) -> dict[str, str]:
    """Entry header names are lowercased so case-variant duplicates collapse and stripping
    is uniform. ``httpx.Headers.items()`` is the faithful iteration: it yields each name
    once, lowercased, with repeats comma-joined -- which is exactly what pi's
    ``for (const [key, value] of request.headers)`` yields, because a WHATWG ``Headers``
    iterator combines same-name entries into one ``"1, 2"`` value too. Iterating
    ``.raw`` with last-one-wins would instead silently drop the earlier of two
    ``x-dup`` headers that pi would have forwarded joined.
    """
    return {
        name: value
        for name, value in request.headers.items()
        if name not in _STRIP_HEADERS and not _is_httpx_default(name, value)
    }


def create_gateway_binding_transport(options: GatewayBindingOptions) -> httpx.AsyncBaseTransport:
    """Create a transport that routes AI Gateway requests through the Workers AI binding.

    See the module docs for behavior and composition notes.
    """
    return _GatewayBindingTransport(options)
