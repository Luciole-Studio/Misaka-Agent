# ruff: noqa: UP040 - the aliases below are spelled with `TypeAlias`, not the PEP 695
# `type` statement.

"""The auth data model, translated from pi's ``packages/ai/src/auth/types.ts``.

Three shapes live here and they are deliberately different in kind:

* **Records** (``ModelAuth``, the credentials, ``AuthResult``) are data, so they are
  pydantic models like the rest of ``misaka.ai.types``.
* **Interfaces** (``CredentialStore``, ``AuthContext``, ``AuthInteraction``) are
  implemented by classes, so they are ``Protocol``s.
* **Auth methods** (``ApiKeyAuth``, ``OAuthAuth``) are neither: upstream builds them as
  object literals whose properties are functions, and a provider supplies whichever
  subset it has. They are dataclasses holding callables, which keeps "this provider has
  no interactive login" expressible as ``login=None`` -- the thing pi writes ``login?``.

Field names stay camelCase, as everywhere else in this package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Provider-scoped environment overrides. Values take precedence over os.environ.
ProviderEnv: TypeAlias = dict[str, str]
# A null value means "remove this header", which is why the value is optional.
ProviderHeaders: TypeAlias = dict[str, str | None]

AuthType: TypeAlias = Literal["api_key", "oauth"]


class _AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelAuth(_AuthModel):
    """Request auth for a single model request.

    If a value cannot be expressed as ``apiKey``, ``headers`` or ``baseUrl``, it is
    provider config, not auth.
    """

    apiKey: str | None = None
    headers: ProviderHeaders | None = None
    baseUrl: str | None = None


class ApiKeyCredential(_AuthModel):
    """Stored api-key credential.

    ``env`` holds provider-scoped environment/config values such as Cloudflare
    account/gateway ids.
    """

    type: Literal["api_key"] = "api_key"
    key: str | None = None
    env: ProviderEnv | None = None


class OAuthCredential(BaseModel):
    """Stored canonical OAuth credential.

    Upstream types this as ``OAuthCredentials`` with an index signature plus a
    ``type`` tag; the index signature is why this one model allows extra keys where
    every other record here forbids them. Providers really do stash their own fields
    here -- GitHub Copilot's ``enterpriseUrl``, OpenAI Codex's ``accountId``, Radius's
    ``scope`` -- and dropping them on a read-modify-write would strand the credential on
    the wrong endpoint or account.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["oauth"] = "oauth"
    refresh: str
    access: str
    expires: int


CredentialValue: TypeAlias = ApiKeyCredential | OAuthCredential
# One type-tagged credential per provider -- the shape of today's auth.json.
Credential: TypeAlias = Annotated[CredentialValue, Field(discriminator="type")]


class CredentialInfo(_AuthModel):
    """Non-secret credential metadata for account/status enumeration."""

    providerId: str
    type: AuthType


class AuthResult(_AuthModel):
    """Result of resolving auth for a model."""

    auth: ModelAuth
    # Provider-scoped values resolved from credentials and ambient context.
    env: ProviderEnv | None = None
    # Human-readable label for status UI: "ANTHROPIC_API_KEY", "OAuth", "~/.aws/credentials".
    source: str | None = None


class AuthCheck(_AuthModel):
    source: str | None = None
    type: AuthType


class AuthInfoLink(_AuthModel):
    url: str
    label: str | None = None


class _Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # Cancels this one prompt when an out-of-band event resolves the step -- a
    # `manual_code` prompt raced against a callback server, aborted when the callback
    # wins. Distinct from AuthInteraction.signal, which aborts the whole login.
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


AuthPromptValue: TypeAlias = TextPrompt | SecretPrompt | SelectPrompt | ManualCodePrompt
AuthPrompt: TypeAlias = Annotated[AuthPromptValue, Field(discriminator="type")]


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
    intervalSeconds: int | None = None
    expiresInSeconds: int | None = None


class ProgressEvent(_AuthModel):
    type: Literal["progress"] = "progress"
    message: str


AuthEventValue: TypeAlias = InfoEvent | AuthUrlEvent | DeviceCodeEvent | ProgressEvent
AuthEvent: TypeAlias = Annotated[AuthEventValue, Field(discriminator="type")]


class AuthOperationOptions(BaseModel):
    """Optional cancellation for public auth and credential operations."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: Any | None = None


class AuthContext(Protocol):
    """Environment access for auth resolution. Injectable for tests and browsers."""

    async def env(self, name: str) -> str | None: ...

    async def fileExists(self, path: str) -> bool:
        """Whether a file exists. Supports a leading ``~``."""
        ...


class AuthInteraction(Protocol):
    """Login interaction callbacks serving both api-key and OAuth flows.

    ``prompt()`` returns the entered/selected string (``select`` returns the option id)
    and raises on cancel/abort. ``signal`` aborts the whole login flow; per-prompt
    cancellation uses ``AuthPrompt.signal``.
    """

    signal: Any | None

    async def prompt(self, prompt: AuthPromptValue) -> str: ...

    def notify(self, event: AuthEventValue) -> None: ...


class ProviderAuthInteraction(AuthInteraction, Protocol):
    """Normalized interaction passed to provider login implementations.

    Same as ``AuthInteraction`` except that ``signal`` is declared without ``| None``.
    """

    signal: Any


class CredentialStore(Protocol):
    """App-owned credential storage, keyed by provider id, one credential per provider.

    ``modify`` is the only write path, so every mutation is a serialized
    read-modify-write; ``Models.getAuth()`` runs OAuth refresh inside ``modify`` so
    concurrent requests cannot double-refresh a rotated token. Login/logout
    orchestration is app-owned.

    Error semantics: ``read`` resolves ``None`` for missing entries. Methods raise only
    on storage failure, and ``Models`` wraps such failures in ``ModelsError`` with code
    ``"auth"``. Stores that serve an in-memory view are valid implementations; upstream
    names coding-agent's ``AuthStorage`` as the example. misaka's ``core.auth_storage``
    is that kind of store for reads -- ``reload()`` parks a failure in ``loadError`` and
    ``get()`` answers from memory -- while a failed *write* raises out of
    ``persistProviderChange`` instead of being recorded internally.
    """

    async def read(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> CredentialValue | None:
        """The stored credential, possibly expired.

        Display and status use this; resolved request auth comes from ``Models.getAuth()``.
        """
        ...

    async def list(
        self, options: AuthOperationOptions | None = None
    ) -> list[CredentialInfo]:
        """Stored credential metadata, without resolving or exposing secrets.

        Implementations must not execute configured API-key commands while listing.
        """
        ...

    async def modify(
        self,
        providerId: str,
        fn: Callable[[CredentialValue | None], Awaitable[CredentialValue | None]],
        options: AuthOperationOptions | None = None,
    ) -> CredentialValue | None:
        """Serialized write -- the only write path.

        ``fn`` sees the current credential because correct writes (refresh,
        login-during-refresh) depend on it; it returns the new credential, or ``None`` to
        leave the entry unchanged. Mutual exclusion is per provider id, cross-process too
        where the backing store supports it. Returns the post-write credential;
        exceptions from ``fn`` propagate.
        """
        ...

    async def delete(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> None:
        """Remove a credential (logout). Implementations serialize this against ``modify``."""
        ...


# The single-object argument pi passes to `check`/`resolve`, kept as keyword parameters so
# the three names survive the translation without inventing a record type upstream lacks.
class ApiKeyCheck(Protocol):
    async def __call__(
        self, *, ctx: AuthContext, credential: ApiKeyCredential | None, signal: Any
    ) -> AuthCheck | None: ...


class ApiKeyResolve(Protocol):
    async def __call__(
        self, *, ctx: AuthContext, credential: ApiKeyCredential | None, signal: Any
    ) -> AuthResult | None: ...


@dataclass(slots=True)
class ApiKeyAuth:
    """Api-key auth: stored key/provider env plus ambient sources.

    Ambient sources are env vars, AWS profiles, ADC files. Ambient-only providers omit
    ``login``.
    """

    # Display name, e.g. "Anthropic API key".
    name: str
    # Resolve auth from the stored credential and/or ambient sources, merging per field
    # (`credential.key or env(...)`). None = not configured. Resolution is
    # provider-scoped; model-specific endpoint preparation happens after it.
    resolve: ApiKeyResolve
    # Interactive setup (prompt for key/provider env). None = ambient-only.
    login: Callable[[ProviderAuthInteraction], Awaitable[ApiKeyCredential]] | None = None
    # Optional side-effect-free availability check, for when `resolve()` would execute
    # commands or do other request-time work. None means Models checks availability by
    # resolving auth instead.
    check: ApiKeyCheck | None = None


@dataclass(slots=True)
class OAuthAuth:
    """OAuth auth.

    The ``refresh``/``toAuth`` split is what lets ``Models`` own the locked refresh
    pattern: ``refresh`` produces a credential, ``toAuth`` derives request auth from
    whatever credential ends up stored.
    """

    # Display name, e.g. "Anthropic (Claude Pro/Max)".
    name: str
    login: Callable[[ProviderAuthInteraction], Awaitable[OAuthCredential]]
    # Exchange the refresh token. Network call; raises on failure (invalid_grant etc.).
    # Models runs this under the store lock.
    refresh: Callable[[OAuthCredential, Any], Awaitable[OAuthCredential]]
    # Side-effect-free derivation of request auth from a valid credential. Covers
    # per-credential baseUrl (GitHub Copilot). Async so lazy wrappers can load the
    # implementation on first use.
    toAuth: Callable[[OAuthCredential], Awaitable[ModelAuth]]
    # Whether access through this auth method is backed by a provider subscription.
    isSubscription: bool | None = None
    # Selector label for the OAuth login option, e.g. "Sign in with SuperGrok or X Premium".
    loginLabel: str | None = None


@dataclass(slots=True)
class ProviderAuth:
    """Provider auth. At least one of ``apiKey``/``oauth`` must be present.

    Even ambient-credential providers and keyless local servers provide ``apiKey`` auth,
    whose ``resolve()`` reports whether the provider is configured.
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
    "AuthEventValue",
    "AuthInfoLink",
    "AuthInteraction",
    "AuthOperationOptions",
    "AuthPrompt",
    "AuthPromptValue",
    "AuthResult",
    "AuthType",
    "AuthUrlEvent",
    "Credential",
    "CredentialInfo",
    "CredentialStore",
    "CredentialValue",
    "DeviceCodeEvent",
    "InfoEvent",
    "ManualCodePrompt",
    "ModelAuth",
    "OAuthAuth",
    "OAuthCredential",
    "ProgressEvent",
    "ProviderAuth",
    "ProviderAuthInteraction",
    "ProviderEnv",
    "ProviderHeaders",
    "SecretPrompt",
    "SelectOption",
    "SelectPrompt",
    "TextPrompt",
]
