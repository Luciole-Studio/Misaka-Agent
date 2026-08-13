"""PKCE helper generation."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class _PKCECodes:
    verifier: str
    challenge: str


async def generate_pkce() -> _PKCECodes:
    verifier = _base64url_encode(secrets.token_bytes(32))
    challenge = _base64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    return _PKCECodes(verifier=verifier, challenge=challenge)


generatePKCE = generate_pkce

__all__ = ["generatePKCE", "generate_pkce"]
