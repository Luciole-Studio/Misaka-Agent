"""MISAKA fork: the ``fetch`` pi-ai's sign-in flows call (no upstream file).

The flows are translated line for line and call ``fetch`` as Node's global; this is that global: the WHATWG shim in
``misaka.utils.whatwg_fetch`` (undici's connect and idle timeouts, no total one; an aborted signal cancels the request
and rejects with the signal's reason), plus one MISAKA feature. Inside ``with_http_options(route)``, every request goes
out over the client ``route(url)`` describes: xAI web search refreshes its accounts over its own network route
(``core/web/network.api_network_options``). Tests replace a flow module's ``fetch``, as upstream's stub the global.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from misaka.core.http_dispatcher import dispatcherIdleTimeout
from misaka.utils import whatwg_fetch
from misaka.utils.whatwg_fetch import RequestInit, Response
from misaka.utils.whatwg_url import URL

# undici's default connect timeout, as `whatwg_fetch.createClient` keeps it.
_CONNECT_TIMEOUT_S = 10.0

_http_options: ContextVar[Callable[[str], dict[str, Any]] | None] = ContextVar("oauth_http_options", default=None)


@contextmanager
def with_http_options(options: Callable[[str], dict[str, Any]]) -> Iterator[None]:
    """Let the host supply its network route without importing host code into ai."""
    token = _http_options.set(options)
    try:
        yield
    finally:
        _http_options.reset(token)


async def fetch(input: str | URL, init: RequestInit | None = None) -> Response:
    """``globalThis.fetch``, over the host's network route when one is set."""
    options = _http_options.get()
    if options is None:
        return await whatwg_fetch.fetch(input, init)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=dispatcherIdleTimeout(), write=None, pool=None),
        **options(str(input)),
    )
    try:
        return await whatwg_fetch.fetchWith(client, input, init, owned=True)
    except BaseException:
        await client.aclose()
        raise


__all__ = ["RequestInit", "Response", "fetch", "with_http_options"]
