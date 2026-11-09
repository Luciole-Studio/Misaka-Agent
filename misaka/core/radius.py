"""Radius constants shared by the interactive mode (pi ``core/radius.ts``)."""

from __future__ import annotations

import os

from misaka.ai.providers.radius_config import (
    DEFAULT_RADIUS_GATEWAY,
    normalizeRadiusGatewayUrl,
)

RADIUS_PROVIDER_ID = "radius"
ENV_RADIUS_GATEWAY = "PI_RADIUS_GATEWAY"
# MCP endpoint of the gateway the built-in Radius provider signs in to.
RADIUS_MCP_URL = f"{normalizeRadiusGatewayUrl(DEFAULT_RADIUS_GATEWAY)}/mcp"


def getRadiusGatewayUrl() -> str:
    """Radius gateway origin, honoring the ``PI_RADIUS_GATEWAY`` override."""
    return normalizeRadiusGatewayUrl(os.environ.get(ENV_RADIUS_GATEWAY, DEFAULT_RADIUS_GATEWAY))


__all__ = ["ENV_RADIUS_GATEWAY", "RADIUS_MCP_URL", "RADIUS_PROVIDER_ID", "getRadiusGatewayUrl"]
