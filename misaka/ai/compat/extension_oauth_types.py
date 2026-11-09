"""Translated from pi's ``packages/ai/src/compat/extension-oauth-types.ts`` (v1.1.0).

MISAKA fork: the records are pydantic models and the callback surface is a ``Protocol``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from misaka.ai.auth.types import OAuthCredentials
from misaka.ai.types import SchemaModel


class OAuthPrompt(SchemaModel):
    """Legacy extension OAuth prompt."""

    message: str
    placeholder: str | None = None
    allowEmpty: bool | None = None


class OAuthAuthInfo(SchemaModel):
    """Legacy extension OAuth authorization link."""

    url: str
    instructions: str | None = None


class OAuthDeviceCodeInfo(SchemaModel):
    """Legacy extension OAuth device-code notification."""

    userCode: str
    verificationUri: str
    intervalSeconds: int | float | None = None
    expiresInSeconds: int | float | None = None


class OAuthSelectOption(SchemaModel):
    id: str
    label: str


class OAuthSelectPrompt(SchemaModel):
    message: str
    options: list[OAuthSelectOption]


class OAuthLoginCallbacks(Protocol):
    """Callback surface retained only for coding-agent extension compatibility."""

    onAuth: Callable[[OAuthAuthInfo], None]
    onDeviceCode: Callable[[OAuthDeviceCodeInfo], None]
    onPrompt: Callable[[OAuthPrompt], Awaitable[str]]
    onProgress: Callable[[str], None] | None
    onManualCodeInput: Callable[[], Awaitable[str]] | None
    onSelect: Callable[[OAuthSelectPrompt], Awaitable[str | None]]
    signal: Any | None


__all__ = [
    "OAuthAuthInfo",
    "OAuthCredentials",
    "OAuthDeviceCodeInfo",
    "OAuthLoginCallbacks",
    "OAuthPrompt",
    "OAuthSelectOption",
    "OAuthSelectPrompt",
]
