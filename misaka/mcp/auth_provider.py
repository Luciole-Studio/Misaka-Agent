"""The auth provider an MCP HTTP transport asks for bearer tokens.

Translated from pi's ``packages/mcp/src/auth-provider.ts``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NotRequired, Protocol, TypedDict

from misaka.utils.whatwg_fetch import Response
from misaka.utils.whatwg_url import URL

# `fetch(input, init?)`: `misaka.utils.whatwg_fetch.fetch`, or whatever a caller injects in its place.
type McpFetch = Callable[..., Awaitable[Response]]


class UnauthorizedContext(TypedDict):
    # The 401 response, or a 403 response whose challenge reports `insufficient_scope`.
    response: Response
    serverUrl: URL
    fetch: McpFetch
    # Access token the rejected request carried, if any. A different current token means another
    # request already refreshed it.
    token: NotRequired[str]


class AuthProvider(Protocol):
    """Supplies bearer tokens to an MCP HTTP transport and may refresh them after a 401 response.

    ``onUnauthorized(context: UnauthorizedContext)`` is optional; transports look it up with
    ``getattr``.
    """

    async def token(self) -> str | None:
        """Token for the next request. May refresh it over the network first, so the transport calls it only
        before requests whose answer it waits for. Closing a session reuses the token of the last request.
        """
        ...


__all__ = ["AuthProvider", "McpFetch", "UnauthorizedContext"]
