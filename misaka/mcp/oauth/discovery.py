"""OAuth discovery: an MCP server's protected-resource metadata, its authorization server's metadata,
and the ``WWW-Authenticate`` challenge that points at them.

Translated from pi's ``packages/mcp/src/oauth/discovery.ts``, whose header reads:

    Adapted from modelcontextprotocol/typescript-sdk v1.29.0 src/client/auth.ts.
    Copyright (c) 2024 Anthropic, PBC. Licensed under MIT; see LICENSES/.
    Modified to remove Zod/CORS shims and enforce authorization-server issuer validation.

The MIT text is ``LICENSES/modelcontextprotocol-typescript-sdk.txt`` (indexed in THIRD_PARTY_NOTICES.md).

``TypeError`` is load-bearing here: ``discoverOAuthServerInfo`` swallows every failure of the
protected-resource lookup except a ``TypeError``, which in fetch means an invalid URL or a network
failure. ``misaka.utils.whatwg_url.URL`` and ``misaka.utils.whatwg_fetch`` raise exactly those.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, Literal, TypedDict

from misaka.mcp.auth_provider import McpFetch
from misaka.mcp.oauth.errors import OAuthIssuerMismatchError
from misaka.mcp.oauth.types import (
    AuthorizationServerMetadata,
    OAuthChallenge,
    OAuthProtectedResourceMetadata,
    OAuthServerInfo,
    parseAuthorizationServerMetadata,
    parseProtectedResourceMetadata,
)
from misaka.mcp.protocol.types import LATEST_PROTOCOL_VERSION
from misaka.utils import whatwg_fetch as _fetch
from misaka.utils.js import (
    JS_WHITESPACE_CLASS,
    defined,
    jsSplitWhitespace,
    jsString,
    jsTrimStart,
)
from misaka.utils.whatwg_url import URL


class DiscoverProtectedResourceMetadataOptions(TypedDict, total=False):
    resourceMetadataUrl: str | URL
    protocolVersion: str
    fetch: McpFetch
    signal: Any


class DiscoverAuthorizationServerMetadataOptions(TypedDict, total=False):
    fetch: McpFetch
    protocolVersion: str
    skipIssuerValidation: bool
    signal: Any


class DiscoverOAuthServerInfoOptions(TypedDict, total=False):
    resourceMetadataUrl: URL
    # Metadata document to use instead of discovery. It is trusted as configured, so its issuer is not checked.
    authorizationServerMetadataUrl: URL
    fetch: McpFetch
    skipIssuerValidation: bool
    signal: Any


class AuthorizationServerDiscoveryUrl(TypedDict):
    url: URL
    type: Literal["oauth", "oidc"]


async def discard(response: _fetch.Response | None) -> None:
    # MISAKA fork: awaited, since an unread httpx body holds its pooled connection until it closes.
    if response is not None and response.body is not None:
        with contextlib.suppress(Exception):
            await response.body.cancel()


def isDiscoveryMiss(status: int) -> bool:
    """4xx and 502 mean "not here", so discovery tries the next candidate URL."""
    return (status >= 400 and status < 500) or status == 502


def pathSuffix(pathname: str) -> str:
    """Path suffix for ``/.well-known/<kind><path>``; empty for the root path."""
    return pathname.removesuffix("/")


def field(header: str, name: str) -> str | None:
    # JavaScript's `\s`, and a case-insensitive match that never maps a non-ASCII letter to an ASCII one.
    match = re.search(
        rf'(?:^|[,{JS_WHITESPACE_CLASS}]){name}=(?:"([^"]*)"|([^{JS_WHITESPACE_CLASS},]+))',
        header,
        re.IGNORECASE | re.ASCII,
    )
    # An empty value (`scope=""`) carries no information, so it counts as absent.
    return (match.group(1) or match.group(2) or None) if match is not None else None


def parseWwwAuthenticate(header: str | None) -> OAuthChallenge:
    if not header:
        return {}
    scheme = jsSplitWhitespace(jsTrimStart(header), 1)[0].lower()
    if scheme != "bearer" and scheme != "dpop":
        return {}
    resourceMetadata = field(header, "resource_metadata")
    resourceMetadataUrl: URL | None = None
    if resourceMetadata:
        with contextlib.suppress(Exception):
            resourceMetadataUrl = URL(resourceMetadata)
    return defined({  # type: ignore[return-value]
        "resourceMetadataUrl": resourceMetadataUrl,
        "scope": field(header, "scope"),
        "error": field(header, "error"),
        "errorDescription": field(header, "error_description"),
    })


async def fetchMetadata(url: URL, fetch: McpFetch, protocolVersion: str, signal: Any) -> _fetch.Response:
    return await fetch(
        url,
        defined({"headers": {"Accept": "application/json", "MCP-Protocol-Version": protocolVersion}, "signal": signal}),  # type: ignore[arg-type]
    )


async def discoverProtectedResourceMetadata(
    serverUrl: str | URL, options: DiscoverProtectedResourceMetadataOptions | None = None
) -> OAuthProtectedResourceMetadata:
    options = options if options is not None else {}
    server = URL(serverUrl)
    fetch = options.get("fetch")
    if fetch is None:
        fetch = _fetch.fetch
    version = options.get("protocolVersion")
    if version is None:
        version = LATEST_PROTOCOL_VERSION
    resourceMetadataUrl = options.get("resourceMetadataUrl")
    response = await fetchMetadata(
        URL(resourceMetadataUrl)
        if resourceMetadataUrl
        else URL(f"/.well-known/oauth-protected-resource{pathSuffix(server.pathname)}", server.origin),
        fetch,
        version,
        options.get("signal"),
    )
    if not resourceMetadataUrl and server.pathname != "/" and isDiscoveryMiss(response.status):
        await discard(response)
        response = await fetchMetadata(
            URL("/.well-known/oauth-protected-resource", server.origin),
            fetch,
            version,
            options.get("signal"),
        )
    if not response.ok:
        await discard(response)
        raise RuntimeError(f"HTTP {response.status} loading OAuth protected resource metadata")
    return parseProtectedResourceMetadata(await response.json())


def buildAuthorizationServerDiscoveryUrls(authorizationServerUrl: str | URL) -> list[AuthorizationServerDiscoveryUrl]:
    issuer = URL(authorizationServerUrl)
    path = pathSuffix(issuer.pathname)
    urls: list[AuthorizationServerDiscoveryUrl] = [
        {"url": URL(f"/.well-known/oauth-authorization-server{path}", issuer.origin), "type": "oauth"},
        {"url": URL(f"/.well-known/openid-configuration{path}", issuer.origin), "type": "oidc"},
    ]
    if path:
        urls.append({"url": URL(f"{path}/.well-known/openid-configuration", issuer.origin), "type": "oidc"})
    return urls


async def discoverAuthorizationServerMetadata(
    authorizationServerUrl: str | URL, options: DiscoverAuthorizationServerMetadataOptions | None = None
) -> AuthorizationServerMetadata | None:
    options = options if options is not None else {}
    fetch = options.get("fetch")
    if fetch is None:
        fetch = _fetch.fetch
    for candidate in buildAuthorizationServerDiscoveryUrls(authorizationServerUrl):
        url = candidate["url"]
        protocolVersion = options.get("protocolVersion")
        response = await fetchMetadata(
            url,
            fetch,
            protocolVersion if protocolVersion is not None else LATEST_PROTOCOL_VERSION,
            options.get("signal"),
        )
        if not response.ok:
            await discard(response)
            if isDiscoveryMiss(response.status):
                continue
            raise RuntimeError(f"HTTP {response.status} loading authorization server metadata from {url}")
        metadata = parseAuthorizationServerMetadata(await response.json())
        if not options.get("skipIssuerValidation"):
            expected = jsString(authorizationServerUrl)

            # URL parsing adds a trailing slash to bare origins, so compare without one on either side.
            def trim(value: str) -> str:
                return value.removesuffix("/")

            if trim(metadata["issuer"]) != trim(expected):
                raise OAuthIssuerMismatchError(expected, metadata["issuer"])
        return metadata
    return None


async def discoverOAuthServerInfo(
    serverUrl: str | URL, options: DiscoverOAuthServerInfoOptions | None = None
) -> OAuthServerInfo:
    options = options if options is not None else {}
    resourceMetadata: OAuthProtectedResourceMetadata | None = None
    try:
        resourceMetadata = await discoverProtectedResourceMetadata(
            serverUrl,
            defined({  # type: ignore[arg-type]
                "resourceMetadataUrl": options.get("resourceMetadataUrl"),
                "fetch": options.get("fetch"),
                "signal": options.get("signal"),
            }),
        )
    except Exception as error:
        if isinstance(error, TypeError):
            raise
    authorizationServerMetadataUrl = options.get("authorizationServerMetadataUrl")
    if authorizationServerMetadataUrl:
        url = authorizationServerMetadataUrl
        fetch = options.get("fetch")
        response = await fetchMetadata(
            url,
            fetch if fetch is not None else _fetch.fetch,
            LATEST_PROTOCOL_VERSION,
            options.get("signal"),
        )
        if not response.ok:
            await discard(response)
            raise RuntimeError(f"HTTP {response.status} loading authorization server metadata from {url}")
        metadata = parseAuthorizationServerMetadata(await response.json())
        return defined({  # type: ignore[return-value]
            "authorizationServerUrl": metadata["issuer"],
            "authorizationServerMetadata": metadata,
            "resourceMetadata": resourceMetadata,
        })
    authorizationServers = resourceMetadata.get("authorization_servers") if resourceMetadata is not None else None
    authorizationServerUrl = authorizationServers[0] if authorizationServers else jsString(URL("/", serverUrl))
    return defined({  # type: ignore[return-value]
        "authorizationServerUrl": authorizationServerUrl,
        "authorizationServerMetadata": await discoverAuthorizationServerMetadata(
            authorizationServerUrl,
            defined({  # type: ignore[arg-type]
                "fetch": options.get("fetch"),
                "skipIssuerValidation": options.get("skipIssuerValidation"),
                "signal": options.get("signal"),
            }),
        ),
        "resourceMetadata": resourceMetadata,
    })


def resourceUrlFromServerUrl(value: str | URL) -> URL:
    url = URL(value)
    url.hash = ""
    return url


def selectResource(serverUrl: str | URL, metadata: OAuthProtectedResourceMetadata | None = None) -> str | None:
    if metadata is None:
        return None
    requested = resourceUrlFromServerUrl(serverUrl)
    configured = URL(metadata["resource"])
    if requested.origin != configured.origin:
        raise RuntimeError(f"Protected resource {metadata['resource']} does not match MCP server {requested}")
    requestedPath = requested.pathname if requested.pathname.endswith("/") else f"{requested.pathname}/"
    configuredPath = configured.pathname if configured.pathname.endswith("/") else f"{configured.pathname}/"
    if not requestedPath.startswith(configuredPath):
        raise RuntimeError(f"Protected resource {metadata['resource']} does not match MCP server {requested}")
    return metadata["resource"]


__all__ = [
    "AuthorizationServerDiscoveryUrl",
    "DiscoverAuthorizationServerMetadataOptions",
    "DiscoverOAuthServerInfoOptions",
    "DiscoverProtectedResourceMetadataOptions",
    "buildAuthorizationServerDiscoveryUrls",
    "discoverAuthorizationServerMetadata",
    "discoverOAuthServerInfo",
    "discoverProtectedResourceMetadata",
    "parseWwwAuthenticate",
    "resourceUrlFromServerUrl",
    "selectResource",
]
