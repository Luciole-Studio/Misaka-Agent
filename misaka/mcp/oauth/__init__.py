"""The MCP OAuth flow: discovery, PKCE authorization, token refresh, the stateful provider and the
loopback callback server.

Translated from pi's ``packages/mcp/src/oauth/index.ts`` (the ``@earendil-works/pi-mcp/oauth`` subpath).
"""

from misaka.mcp.oauth.callback import (
    OAuthCallback,
    OAuthCallbackPage,
    OAuthCallbackServer,
    OAuthCallbackServerOptions,
)
from misaka.mcp.oauth.discovery import (
    buildAuthorizationServerDiscoveryUrls,
    discoverAuthorizationServerMetadata,
    discoverOAuthServerInfo,
    discoverProtectedResourceMetadata,
    parseWwwAuthenticate,
    resourceUrlFromServerUrl,
    selectResource,
)
from misaka.mcp.oauth.errors import (
    McpOAuthAuthorizationRequiredError,
    OAuthError,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    OAuthRegistrationError,
)
from misaka.mcp.oauth.flow import (
    AddClientAuthentication,
    OAuthClientMetadataDocument,
    OAuthClientProvider,
    OAuthFlowOptions,
    OAuthFlowResult,
    TokenRequestOptions,
    adaptOAuthProvider,
    authorizeMcp,
    exchangeAuthorizationCode,
    refreshAuthorization,
    registerClient,
    startAuthorization,
    stepUpScope,
)
from misaka.mcp.oauth.provider import (
    McpOAuthProvider,
    McpOAuthProviderOptions,
    McpOAuthState,
    McpOAuthStateStore,
    MemoryOAuthStateStore,
)
from misaka.mcp.oauth.types import (
    AuthorizationServerMetadata,
    OAuthChallenge,
    OAuthClientInformation,
    OAuthClientInformationFull,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthDiscoveryState,
    OAuthProtectedResourceMetadata,
    OAuthServerInfo,
    OAuthTokens,
)

__all__ = [
    "AddClientAuthentication",
    "AuthorizationServerMetadata",
    "McpOAuthAuthorizationRequiredError",
    "McpOAuthProvider",
    "McpOAuthProviderOptions",
    "McpOAuthState",
    "McpOAuthStateStore",
    "MemoryOAuthStateStore",
    "OAuthCallback",
    "OAuthCallbackPage",
    "OAuthCallbackServer",
    "OAuthCallbackServerOptions",
    "OAuthChallenge",
    "OAuthClientInformation",
    "OAuthClientInformationFull",
    "OAuthClientInformationMixed",
    "OAuthClientMetadata",
    "OAuthClientMetadataDocument",
    "OAuthClientProvider",
    "OAuthDiscoveryState",
    "OAuthError",
    "OAuthFlowOptions",
    "OAuthFlowResult",
    "OAuthInsecureEndpointError",
    "OAuthIssuerMismatchError",
    "OAuthProtectedResourceMetadata",
    "OAuthRegistrationError",
    "OAuthServerInfo",
    "OAuthTokens",
    "TokenRequestOptions",
    "adaptOAuthProvider",
    "authorizeMcp",
    "buildAuthorizationServerDiscoveryUrls",
    "discoverAuthorizationServerMetadata",
    "discoverOAuthServerInfo",
    "discoverProtectedResourceMetadata",
    "exchangeAuthorizationCode",
    "parseWwwAuthenticate",
    "refreshAuthorization",
    "registerClient",
    "resourceUrlFromServerUrl",
    "selectResource",
    "startAuthorization",
    "stepUpScope",
]
