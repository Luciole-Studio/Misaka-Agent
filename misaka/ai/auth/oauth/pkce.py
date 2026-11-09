"""Translated from pi's ``packages/ai/src/auth/oauth/pkce.ts`` (v1.1.0).

PKCE utilities using Web Crypto API.
Works in both Node.js 20+ and browsers.

MISAKA fork: ``secrets`` and ``hashlib`` stand in for Web Crypto, and the returned object is ``_PKCECodes``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


def _base64urlEncode(bytes: bytes) -> str:
    """Encode bytes as base64url string."""
    return base64.b64encode(bytes).decode("ascii").replace("+", "-").replace("/", "_").replace("=", "")


@dataclass(frozen=True, slots=True)
class _PKCECodes:
    verifier: str
    challenge: str


async def generatePKCE() -> _PKCECodes:
    """Generate PKCE code verifier and challenge.
    Uses Web Crypto API for cross-platform compatibility.
    """
    # Generate random verifier
    verifierBytes = secrets.token_bytes(32)
    verifier = _base64urlEncode(verifierBytes)

    # Compute SHA-256 challenge
    data = verifier.encode("utf-8")
    hashBuffer = hashlib.sha256(data).digest()
    challenge = _base64urlEncode(hashBuffer)

    return _PKCECodes(verifier=verifier, challenge=challenge)


__all__ = ["generatePKCE"]
