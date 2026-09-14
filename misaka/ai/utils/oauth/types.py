"""Type definitions for OAuth-based provider authentication flows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import ConfigDict

from misaka.ai.types import Model, SchemaModel


class OAuthCredentials(SchemaModel):
    model_config = ConfigDict(extra="allow")

    refresh: str
    access: str
    expires: int | float


OAuthProviderId = str
OAuthProvider = OAuthProviderId


class OAuthPrompt(SchemaModel):
    message: str
    placeholder: str | None = None
    allowEmpty: bool | None = None


class OAuthAuthInfo(SchemaModel):
    url: str
    instructions: str | None = None


class OAuthDeviceCodeInfo(SchemaModel):
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
    onAuth: Callable[[OAuthAuthInfo], None]
    onDeviceCode: Callable[[OAuthDeviceCodeInfo], None]
    onPrompt: Callable[[OAuthPrompt], Awaitable[str]]
    onProgress: Callable[[str], None] | None
    # Takes the prompt, as upstream's `prompt({type: "manual_code", message,
    # placeholder, signal})` does: the wording belongs to the flow that asks, not to
    # whichever surface happens to draw the box, and the three flows do not word it
    # alike (`openrouter.py` signs in, the other two log in).
    onManualCodeInput: Callable[[OAuthPrompt], Awaitable[str]] | None
    onSelect: Callable[[OAuthSelectPrompt], Awaitable[str | None]]
    signal: Any | None


class OAuthProviderInterface(Protocol):
    id: OAuthProviderId
    name: str
    usesCallbackServer: bool | None
    modifyModels: Callable[[list[Model], OAuthCredentials], list[Model]] | None

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials: ...

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials: ...

    def getApiKey(self, credentials: OAuthCredentials) -> str: ...


class OAuthProviderInfo(SchemaModel):
    id: OAuthProviderId
    name: str
    available: bool


__all__ = [
    "OAuthAuthInfo",
    "OAuthCredentials",
    "OAuthDeviceCodeInfo",
    "OAuthLoginCallbacks",
    "OAuthPrompt",
    "OAuthProvider",
    "OAuthProviderId",
    "OAuthProviderInfo",
    "OAuthProviderInterface",
    "OAuthSelectOption",
    "OAuthSelectPrompt",
]
