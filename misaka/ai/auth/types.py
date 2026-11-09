# ruff: noqa: UP040 - the aliases below are spelled with `TypeAlias`, not the PEP 695
# `type` statement.

"""Translated from pi's ``packages/ai/src/auth/types.ts`` (v1.1.0).

MISAKA fork, in the representation only: records are pydantic models, interfaces are ``Protocol``s, and the auth
methods (``ApiKeyAuth``, ``OAuthAuth``), which upstream builds as object literals of functions, are dataclasses holding
callables, so "no interactive login" stays expressible as ``login=None``. The anonymous members of the ``AuthPrompt``
and ``AuthEvent`` unions get class names (``TextPrompt``, ``DeviceCodeEvent``...), and the single object ``check`` and
``resolve`` take is spelled as keyword parameters (``ApiKeyCheck``, ``ApiKeyResolve``).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from misaka.ai.types import ProviderEnv, ProviderHeaders


class _AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelAuth(_AuthModel):
    """Request auth for a single model request. If a value cannot be expressed as
    `apiKey`, `headers`, or `baseUrl`, it is provider config, not auth.
    """

    apiKey: str | None = None
    headers: ProviderHeaders | None = None
    baseUrl: str | None = None


class ApiKeyCredential(_AuthModel):
    """Stored api-key credential. `env` holds provider-scoped environment/config
    values such as Cloudflare account/gateway ids.
    """

    type: Literal["api_key"] = "api_key"
    key: str | None = None
    env: ProviderEnv | None = None


class OAuthCredentials(BaseModel):
    """OAuth token data returned by extension compatibility flows."""

    # The `[key: string]: unknown` index signature: providers keep their own fields here.
    model_config = ConfigDict(extra="allow")

    refresh: str
    access: str
    expires: int

    @field_validator("expires", mode="before")
    @classmethod
    def _wholeMilliseconds(cls, value: Any) -> Any:
        # MISAKA fork: a JavaScript number may carry a fraction (`Date.now() + expires_in * 1000`); the stored
        # expiry is whole milliseconds.
        if isinstance(value, float) and math.isfinite(value):
            return math.floor(value)
        return value


class OAuthCredential(OAuthCredentials):
    """Stored canonical OAuth credential."""

    type: Literal["oauth"] = "oauth"


# One type-tagged credential per provider — the shape of today's auth.json.
Credential: TypeAlias = ApiKeyCredential | OAuthCredential


class CredentialInfo(_AuthModel):
    """Non-secret credential metadata for account/status enumeration."""

    providerId: str
    type: Literal["api_key", "oauth"]


class AuthOperationOptions(BaseModel):
    """Optional cancellation for public auth and credential operations."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: Any | None = None


class CredentialStore(Protocol):
    """App-owned credential storage, keyed by `Provider.id`, one credential per
    provider. `modify` is the only write path, so every mutation is a
    serialized read-modify-write; `Models.getAuth()` runs OAuth refresh inside
    `modify` so concurrent requests cannot double-refresh a rotated token. The
    app persists a credential after login via
    `modify(provider.id, async () => credential)`. Login/logout orchestration
    is app-owned.

    Error semantics: `read` resolves `undefined` for missing entries. Methods
    reject only on storage failure; `Models` wraps such rejections in
    `ModelsError` with code "auth". Best-effort stores that serve an in-memory
    view and record persistence errors internally (like coding-agent's
    AuthStorage) are valid implementations.
    """

    async def read(self, providerId: str, options: AuthOperationOptions | None = None) -> Credential | None:
        """Read the stored credential, possibly expired. Display/status use;
        resolved request auth comes from `Models.getAuth()`.
        """
        ...

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        """List stored credential metadata without resolving or exposing secrets.
        Implementations must not execute configured API-key commands while listing.
        """
        ...

    async def modify(
        self,
        providerId: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
        options: AuthOperationOptions | None = None,
    ) -> Credential | None:
        """Serialized write — the only write path. `fn` sees the current credential
        because correct writes (refresh, login-during-refresh) depend on it;
        return the new credential, or undefined to leave the entry unchanged.
        Mutual exclusion per provider id, cross-process too where the backing
        store supports it (e.g. a file lock). Resolves with the post-write
        credential. Rejections from `fn` propagate.
        """
        ...

    async def delete(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        """Remove a credential (logout). Implementations serialize this against `modify`."""
        ...


class AuthContext(Protocol):
    """Environment access for auth resolution. Injectable for tests and browsers."""

    async def env(self, name: str) -> str | None: ...

    async def fileExists(self, path: str) -> bool:
        """Check whether a file exists. Supports a leading `~`. Always false in browsers."""
        ...


class AuthResult(_AuthModel):
    """Result of resolving auth for a model."""

    auth: ModelAuth
    # Provider-scoped environment/config values resolved from credentials and ambient context.
    env: ProviderEnv | None = None
    # Human-readable label for status UI: "ANTHROPIC_API_KEY", "OAuth", "~/.aws/credentials".
    source: str | None = None


class AuthCheck(_AuthModel):
    source: str | None = None
    type: Literal["api_key", "oauth"]


AuthType: TypeAlias = Literal["api_key", "oauth"]


class _Prompt(BaseModel):
    """Prompt shown to the user during login. `signal` lets the flow cancel a
    pending prompt when an out-of-band event resolves the step, e.g. a
    `manual_code` prompt raced against a callback server, aborted when the
    callback wins.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: Any | None = None


class TextPrompt(_Prompt):
    type: Literal["text"] = "text"
    message: str
    placeholder: str | None = None


class SecretPrompt(_Prompt):
    type: Literal["secret"] = "secret"
    message: str
    placeholder: str | None = None


class SelectOption(_AuthModel):
    id: str
    label: str
    description: str | None = None


class SelectPrompt(_Prompt):
    type: Literal["select"] = "select"
    message: str
    options: list[SelectOption]


class ManualCodePrompt(_Prompt):
    type: Literal["manual_code"] = "manual_code"
    message: str
    placeholder: str | None = None


AuthPrompt: TypeAlias = TextPrompt | SecretPrompt | SelectPrompt | ManualCodePrompt


class AuthInfoLink(_AuthModel):
    url: str
    label: str | None = None


class InfoEvent(_AuthModel):
    type: Literal["info"] = "info"
    message: str
    links: list[AuthInfoLink] | None = None


class AuthUrlEvent(_AuthModel):
    type: Literal["auth_url"] = "auth_url"
    url: str
    instructions: str | None = None


class DeviceCodeEvent(_AuthModel):
    type: Literal["device_code"] = "device_code"
    userCode: str
    verificationUri: str
    intervalSeconds: int | float | None = None
    expiresInSeconds: int | float | None = None


class ProgressEvent(_AuthModel):
    type: Literal["progress"] = "progress"
    message: str


AuthEvent: TypeAlias = InfoEvent | AuthUrlEvent | DeviceCodeEvent | ProgressEvent


class AuthInteraction(Protocol):
    """Login interaction callbacks serving both api-key and OAuth flows.

    `prompt()` returns the entered/selected string (`select` returns the option
    id). Rejects on cancel/abort. `signal` aborts the whole login flow;
    per-prompt cancellation uses `AuthPrompt.signal`.
    """

    signal: Any | None

    async def prompt(self, prompt: AuthPrompt) -> str: ...

    def notify(self, event: AuthEvent) -> None: ...


class ProviderAuthInteraction(AuthInteraction, Protocol):
    """Normalized interaction passed to provider login implementations."""

    signal: Any


class ApiKeyCheck(Protocol):
    async def __call__(
        self, *, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any
    ) -> AuthCheck | None: ...


class ApiKeyResolve(Protocol):
    async def __call__(
        self, *, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any
    ) -> AuthResult | None: ...


@dataclass(slots=True)
class ApiKeyAuth:
    """Api-key auth: stored key/provider env plus ambient sources (env vars, AWS
    profiles, ADC files). Ambient-only providers omit `login`.
    """

    # Display name, e.g. "Anthropic API key".
    name: str
    # Resolve auth from the stored credential and/or ambient sources, merging
    # per field (`credential.key ?? env("...")`, `credential.env?.NAME ?? env("...")`).
    # undefined = not configured. Resolution is provider-scoped; model-specific
    # endpoint preparation happens after auth has been resolved.
    resolve: ApiKeyResolve
    # Interactive setup (prompt for key/provider env). Absent = ambient-only.
    login: Callable[[ProviderAuthInteraction], Awaitable[ApiKeyCredential]] | None = None
    # Optional side-effect-free availability check. Use this when `resolve()` may
    # execute commands or perform other request-time work. Missing means Models
    # checks availability by resolving auth.
    check: ApiKeyCheck | None = None


@dataclass(slots=True)
class LoginOptions:
    """App-supplied context for `Models.login`."""

    # Returns the stable ID of this app installation, e.g. sent to OpenAI as its
    # agent host ID. Called only by login flows that need it, so apps can create
    # the ID on first use and must return the same ID on every later call.
    getDeviceId: Callable[[], str] | None = None
    # Name this app introduces itself with during login, e.g. OpenAI's agent name hint and
    # Codex originator. Defaults to pi's own name.
    agentName: str | None = None


@dataclass(slots=True)
class OAuthAuth:
    """OAuth auth. The `refresh`/`toAuth` split lets `Models` own the locked
    refresh pattern: `refresh` produces a credential, `toAuth` derives request
    auth from whatever credential ends up stored.
    """

    # Display name, e.g. "Anthropic (Claude Pro/Max)".
    name: str
    # Called as `login(interaction, options)`.
    login: Callable[..., Awaitable[OAuthCredential]]
    # Exchange the refresh token. Network call; throws on failure
    # (invalid_grant etc.). `Models` runs this under the store lock.
    refresh: Callable[[OAuthCredential, Any], Awaitable[OAuthCredential]]
    # Side-effect-free derivation of request auth from a valid credential.
    # Covers per-credential baseUrl (GitHub Copilot). Async so lazy wrappers
    # can load the implementation on first use.
    toAuth: Callable[[OAuthCredential], Awaitable[ModelAuth]]
    # Whether access through this auth method is backed by a provider subscription.
    isSubscription: bool | None = None
    # Selector label for the OAuth login option, e.g. "Sign in with SuperGrok or X Premium".
    loginLabel: str | None = None


@dataclass(slots=True)
class ProviderAuth:
    """Provider auth. At least one of `apiKey`/`oauth` must be present: even
    ambient-credential providers and keyless local servers provide `apiKey`
    auth whose `resolve()` reports whether the provider is configured.
    """

    apiKey: ApiKeyAuth | None = None
    oauth: OAuthAuth | None = None


__all__ = [
    "ApiKeyAuth",
    "ApiKeyCheck",
    "ApiKeyCredential",
    "ApiKeyResolve",
    "AuthCheck",
    "AuthContext",
    "AuthEvent",
    "AuthInfoLink",
    "AuthInteraction",
    "AuthOperationOptions",
    "AuthPrompt",
    "AuthResult",
    "AuthType",
    "AuthUrlEvent",
    "Credential",
    "CredentialInfo",
    "CredentialStore",
    "DeviceCodeEvent",
    "InfoEvent",
    "LoginOptions",
    "ManualCodePrompt",
    "ModelAuth",
    "OAuthAuth",
    "OAuthCredential",
    "OAuthCredentials",
    "ProgressEvent",
    "ProviderAuth",
    "ProviderAuthInteraction",
    "SecretPrompt",
    "SelectOption",
    "SelectPrompt",
    "TextPrompt",
]
