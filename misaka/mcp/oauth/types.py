"""OAuth metadata, token and client shapes, and the structural validation of their wire forms.

Translated from pi's ``packages/mcp/src/oauth/types.ts``, whose header reads:

    Adapted from modelcontextprotocol/typescript-sdk v1.29.0.
    Copyright (c) 2024 Anthropic, PBC. Licensed under MIT; see LICENSES/.
    Modified to use dependency-free structural validation.

The MIT text is ``LICENSES/modelcontextprotocol-typescript-sdk.txt`` (indexed in THIRD_PARTY_NOTICES.md).

The shapes are ``TypedDict``s and plain ``dict``s at runtime: upstream spreads, key-tests, clones
and persists them. An absent field is a missing key, never ``None``; the validators tell the two
apart with ``UNDEFINED``, since JSON ``null`` is a value they reject or, for the fields servers
blank out, treat as absent.
"""

from __future__ import annotations

import math
from typing import Any, Required, TypedDict

from misaka.mcp.protocol.jsonrpc import isObject
from misaka.utils.js import UNDEFINED, isNumber, jsNumber
from misaka.utils.whatwg_url import URL


class OAuthProtectedResourceMetadata(TypedDict, total=False):
    resource: Required[str]
    authorization_servers: list[str]
    scopes_supported: list[str]
    # Further fields (`[key: string]: unknown`) are kept as the server sent them.


class AuthorizationServerMetadata(TypedDict, total=False):
    issuer: Required[str]
    authorization_endpoint: Required[str]
    token_endpoint: Required[str]
    registration_endpoint: str
    scopes_supported: list[str]
    response_types_supported: Required[list[str]]
    grant_types_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]
    code_challenge_methods_supported: list[str]
    client_id_metadata_document_supported: bool
    # Whether authorization responses carry an `iss` parameter (RFC 9207).
    authorization_response_iss_parameter_supported: bool
    # Further fields (`[key: string]: unknown`) are kept as the server sent them.


class OAuthTokens(TypedDict, total=False):
    access_token: Required[str]
    token_type: Required[str]
    expires_in: int | float
    scope: str
    refresh_token: str
    id_token: str


class OAuthClientMetadata(TypedDict, total=False):
    redirect_uris: Required[list[str]]
    token_endpoint_auth_method: str
    grant_types: list[str]
    response_types: list[str]
    # OpenID Connect client type, `native` or `web`. Dynamic client registration derives it from
    # `redirect_uris` when absent (MCP SEP-837).
    application_type: str
    client_name: str
    client_uri: str
    logo_uri: str
    scope: str
    contacts: list[str]
    tos_uri: str
    policy_uri: str
    jwks_uri: str
    jwks: Any
    software_id: str
    software_version: str
    software_statement: str


class OAuthClientInformation(TypedDict, total=False):
    client_id: Required[str]
    client_secret: str
    client_id_issued_at: int | float
    client_secret_expires_at: int | float


class OAuthClientInformationFull(OAuthClientInformation, OAuthClientMetadata):
    """``OAuthClientInformation & OAuthClientMetadata``."""


type OAuthClientInformationMixed = OAuthClientInformation | OAuthClientInformationFull


class OAuthDiscoveryState(TypedDict, total=False):
    authorizationServerUrl: Required[str]
    authorizationServerMetadata: AuthorizationServerMetadata
    resourceMetadata: OAuthProtectedResourceMetadata
    resourceMetadataUrl: str


class OAuthServerInfo(TypedDict, total=False):
    authorizationServerUrl: Required[str]
    authorizationServerMetadata: AuthorizationServerMetadata
    resourceMetadata: OAuthProtectedResourceMetadata


class OAuthChallenge(TypedDict, total=False):
    resourceMetadataUrl: URL
    scope: str
    error: str
    errorDescription: str


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isObject(value):
        raise RuntimeError(f"Invalid {name}")
    return value


def compact(value: dict[str, Any]) -> dict[str, Any]:
    """Drops ``undefined`` values so optional fields are absent rather than present-but-undefined."""
    return {key: item for key, item in value.items() if item is not UNDEFINED}


def requiredString(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) == 0:
        raise RuntimeError(f"Invalid {name}")
    return value


def absent(value: Any) -> bool:
    """Treats ``null`` and ``""`` as absent: servers send them for fields they have no value for,
    like ``scope: ""``."""
    return value is UNDEFINED or value is None or (isinstance(value, str) and value == "")


def optionalString(value: Any, name: str) -> Any:
    if absent(value):
        return UNDEFINED
    return requiredString(value, name)


def optionalStrings(value: Any, name: str) -> Any:
    if value is UNDEFINED or value is None:
        return UNDEFINED
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"Invalid {name}")
    return list(value)


def safeUrl(value: Any, name: str) -> str:
    """Accepts only ``http:`` and ``https:`` URLs. Servers supply these URLs, and the authorization endpoint is
    opened with the platform URL handler, so a ``file:`` or custom protocol URL would launch local programs."""
    text = requiredString(value, name)
    # URL parsing throws a `TypeError`, which discovery reserves for network failures.
    if not URL.canParse(text) or not isHttpUrl(URL(text)):
        raise RuntimeError(f"Invalid {name}")
    return text


def isHttpUrl(url: URL) -> bool:
    return url.protocol in ("http:", "https:")


def optionalUrl(value: Any, name: str) -> Any:
    return UNDEFINED if absent(value) else safeUrl(value, name)


def parseProtectedResourceMetadata(value: Any) -> OAuthProtectedResourceMetadata:
    input = _object(value, "OAuth protected resource metadata")
    return compact({  # type: ignore[return-value]
        **input,
        "resource": safeUrl(input.get("resource", UNDEFINED), "OAuth protected resource metadata resource"),
        "authorization_servers": (
            [safeUrl(url, "authorization server URL") for url in servers]
            if (servers := optionalStrings(input.get("authorization_servers", UNDEFINED), "authorization_servers"))
            is not UNDEFINED
            else UNDEFINED
        ),
        "scopes_supported": optionalStrings(input.get("scopes_supported", UNDEFINED), "scopes_supported"),
    })


def parseAuthorizationServerMetadata(value: Any) -> AuthorizationServerMetadata:
    input = _object(value, "authorization server metadata")
    responseTypes = optionalStrings(input.get("response_types_supported", UNDEFINED), "response_types_supported")
    if responseTypes is UNDEFINED:
        raise RuntimeError("Invalid response_types_supported")
    return compact({  # type: ignore[return-value]
        **input,
        "issuer": safeUrl(input.get("issuer", UNDEFINED), "authorization server issuer"),
        "authorization_endpoint": safeUrl(input.get("authorization_endpoint", UNDEFINED), "authorization endpoint"),
        "token_endpoint": safeUrl(input.get("token_endpoint", UNDEFINED), "token endpoint"),
        "registration_endpoint": optionalUrl(input.get("registration_endpoint", UNDEFINED), "registration endpoint"),
        "scopes_supported": optionalStrings(input.get("scopes_supported", UNDEFINED), "scopes_supported"),
        "response_types_supported": responseTypes,
        "grant_types_supported": optionalStrings(input.get("grant_types_supported", UNDEFINED), "grant_types_supported"),
        "token_endpoint_auth_methods_supported": optionalStrings(
            input.get("token_endpoint_auth_methods_supported", UNDEFINED),
            "token_endpoint_auth_methods_supported",
        ),
        "code_challenge_methods_supported": optionalStrings(
            input.get("code_challenge_methods_supported", UNDEFINED),
            "code_challenge_methods_supported",
        ),
        "client_id_metadata_document_supported": (
            input["client_id_metadata_document_supported"]
            if isinstance(input.get("client_id_metadata_document_supported"), bool)
            else UNDEFINED
        ),
        "authorization_response_iss_parameter_supported": (
            input["authorization_response_iss_parameter_supported"]
            if isinstance(input.get("authorization_response_iss_parameter_supported"), bool)
            else UNDEFINED
        ),
    })


def parseOAuthTokens(value: Any) -> OAuthTokens:
    input = _object(value, "OAuth token response")
    # `Number(null)` is 0, which would mark the token as expired at once.
    expires: Any = UNDEFINED if absent(input.get("expires_in", UNDEFINED)) else jsNumber(input["expires_in"])
    if expires is not UNDEFINED and not math.isfinite(expires):
        raise RuntimeError("Invalid expires_in")
    return compact({  # type: ignore[return-value]
        "access_token": requiredString(input.get("access_token", UNDEFINED), "access_token"),
        "token_type": requiredString(input.get("token_type", UNDEFINED), "token_type"),
        "expires_in": expires,
        "scope": optionalString(input.get("scope", UNDEFINED), "scope"),
        "refresh_token": optionalString(input.get("refresh_token", UNDEFINED), "refresh_token"),
        "id_token": optionalString(input.get("id_token", UNDEFINED), "id_token"),
    })


def parseClientInformation(value: Any) -> OAuthClientInformationFull:
    input = _object(value, "OAuth client registration response")
    return compact({  # type: ignore[return-value]
        **input,
        "client_id": requiredString(input.get("client_id", UNDEFINED), "client_id"),
        "client_secret": optionalString(input.get("client_secret", UNDEFINED), "client_secret"),
        "client_id_issued_at": (
            input["client_id_issued_at"] if isNumber(input.get("client_id_issued_at")) else UNDEFINED
        ),
        "client_secret_expires_at": (
            input["client_secret_expires_at"] if isNumber(input.get("client_secret_expires_at")) else UNDEFINED
        ),
        "redirect_uris": (
            redirectUris
            if (redirectUris := optionalStrings(input.get("redirect_uris", UNDEFINED), "redirect_uris"))
            is not UNDEFINED
            else []
        ),
    })


__all__ = [
    "AuthorizationServerMetadata",
    "OAuthChallenge",
    "OAuthClientInformation",
    "OAuthClientInformationFull",
    "OAuthClientInformationMixed",
    "OAuthClientMetadata",
    "OAuthDiscoveryState",
    "OAuthProtectedResourceMetadata",
    "OAuthServerInfo",
    "OAuthTokens",
    "isHttpUrl",
    "parseAuthorizationServerMetadata",
    "parseClientInformation",
    "parseOAuthTokens",
    "parseProtectedResourceMetadata",
]
