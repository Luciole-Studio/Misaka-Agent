"""Make misaka's OAuth flows usable through pi's ``OAuthAuth`` contract.

The two generations describe the same conversation in different shapes. misaka's flows
(``ai/utils/oauth/``) take a callbacks record -- ``onAuth``, ``onDeviceCode``, ``onPrompt``,
``onSelect``, ``onProgress``, ``onManualCodeInput`` -- and pi's ``AuthInteraction`` takes
two methods, ``prompt()`` and ``notify()``, over tagged prompt and event records. Every
callback maps onto one of those two; ``OAuthDeviceCodeInfo`` and ``DeviceCodeEvent``
carry the same four fields. ``OAuthPrompt`` also carries ``allowEmpty``
(``github_copilot.py:226`` passes it) and ``TextPrompt`` has no such field, so it is
dropped here.

The split is the same one pi's ``OAuthAuth`` documents: ``refresh`` produces a credential
and ``toAuth`` derives request auth from whatever ends up stored, which is what lets
``Models`` own the locked refresh instead of each flow rolling its own.

Callback payloads are read with ``read_field`` rather than dot access, because misaka's
own flows disagree about the shape. ``ai/utils/oauth/types.py`` declares objects
(``Callable[[OAuthAuthInfo], None]``), and ``kimi_coding``/``xai``/``openrouter``/
``radius`` pass them -- while ``anthropic.py``, ``openai_codex.py`` and
``github_copilot.py`` pass bare dicts. ``interactive_mode.py`` reads these same payloads
the same tolerant way (``read_field`` at ``interactive_mode.py:3708``).

Upstream ships seven OAuth flows (``auth/oauth/``: anthropic, github-copilot,
kimi-coding, openai-codex, openrouter, xai, radius). ``oauth_auth_for_provider`` covers
the six that ``ai/utils/oauth`` registers by id; radius is not one of them -- it is
parameterised by gateway, so ``ai/radius_provider.py`` builds its flow and bridges it
through ``oauth_auth_from_flow`` directly. ``oauth_auth_for_provider`` returns ``None``
for a provider with no registered flow, so a missing one says so instead of failing
somewhere less obvious.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.auth.types import (
    AuthEventValue,
    AuthUrlEvent,
    DeviceCodeEvent,
    ManualCodePrompt,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProgressEvent,
    SelectOption,
    SelectPrompt,
    TextPrompt,
)
from misaka.ai.utils.oauth import _call_refresh_token, get_oauth_provider
from misaka.ai.utils.oauth.types import OAuthCredentials
from misaka.utils.values import read_field


def _to_flow_credentials(credential: OAuthCredential) -> OAuthCredentials:
    """Drop the discriminator the flows never had; keep every provider-added field."""
    data = credential.model_dump()
    data.pop("type", None)
    return OAuthCredentials.model_validate(data)


def _from_flow_credentials(credentials: OAuthCredentials) -> OAuthCredential:
    return OAuthCredential.model_validate({**credentials.model_dump(), "type": "oauth"})


class _InteractionCallbacks:
    """pi's ``AuthInteraction`` wearing the callbacks record misaka's flows expect."""

    def __init__(self, interaction: Any) -> None:
        self._interaction = interaction
        self.signal = getattr(interaction, "signal", None)
        self.usesCallbackServer = None

    def _notify(self, event: AuthEventValue) -> None:
        self._interaction.notify(event)

    def onAuth(self, info: Any) -> None:
        self._notify(
            AuthUrlEvent(
                url=read_field(info, "url", ""),
                instructions=read_field(info, "instructions"),
            )
        )

    def onDeviceCode(self, info: Any) -> None:
        self._notify(
            DeviceCodeEvent(
                userCode=read_field(info, "userCode", ""),
                verificationUri=read_field(info, "verificationUri", ""),
                intervalSeconds=read_field(info, "intervalSeconds"),
                expiresInSeconds=read_field(info, "expiresInSeconds"),
            )
        )

    def onProgress(self, message: str) -> None:
        self._notify(ProgressEvent(message=message))

    async def onPrompt(self, prompt: Any) -> str:
        return await self._interaction.prompt(
            TextPrompt(
                message=read_field(prompt, "message", ""),
                placeholder=read_field(prompt, "placeholder"),
            )
        )

    async def onManualCodeInput(self) -> str:
        return await self._interaction.prompt(
            ManualCodePrompt(message="Paste the authorization code")
        )

    async def onSelect(self, prompt: Any) -> str | None:
        return await self._interaction.prompt(
            SelectPrompt(
                message=read_field(prompt, "message", ""),
                options=[
                    SelectOption(
                        id=read_field(option, "id", ""),
                        label=read_field(option, "label", ""),
                        description=read_field(option, "description"),
                    )
                    for option in (read_field(prompt, "options") or [])
                ],
            )
        )


def oauth_auth_from_flow(
    flow: Any,
    *,
    name: str | None = None,
    isSubscription: bool | None = None,
    loginLabel: str | None = None,
) -> OAuthAuth:
    """Wrap one of misaka's OAuth flows as an ``OAuthAuth``.

    ``name`` defaults to the flow's own display name; callers pass upstream's wording when
    they want the two projects' login screens to read the same.
    """

    async def login(interaction: Any) -> OAuthCredential:
        credentials = await flow.login(_InteractionCallbacks(interaction))
        return _from_flow_credentials(credentials)

    async def refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
        refreshed = await _call_refresh_token(
            flow.refreshToken, _to_flow_credentials(credential), signal
        )
        return _from_flow_credentials(refreshed)

    async def toAuth(credential: OAuthCredential) -> ModelAuth:
        # `getApiKey` is side-effect free, which is what lets `Models` call it outside the
        # store lock after a refresh has already settled.
        #
        # `getBaseUrl` is optional; `github_copilot.py` is the one flow in
        # `ai/utils/oauth/` that defines it, and upstream returns that URL from `toAuth`
        # alongside the key (`auth/oauth/github-copilot.ts:501-506`). Without this branch
        # the bridged `ModelAuth` would carry no `baseUrl` at all.
        flowCredentials = _to_flow_credentials(credential)
        getBaseUrl = getattr(flow, "getBaseUrl", None)
        baseUrl = getBaseUrl(flowCredentials) if callable(getBaseUrl) else None

        # A flow that supplies its own auth headers is sending the credential a different
        # way than `x-api-key`, so the key is left unset rather than sent alongside: pi's
        # Kimi `toAuth` returns `{headers: {Authorization: "Bearer ..."}}` and no `apiKey`.
        getAuthHeaders = getattr(flow, "getAuthHeaders", None)
        headers = getAuthHeaders(flowCredentials) if callable(getAuthHeaders) else None
        if headers:
            return ModelAuth(headers=headers, baseUrl=baseUrl)
        return ModelAuth(apiKey=flow.getApiKey(flowCredentials), baseUrl=baseUrl)

    return OAuthAuth(
        name=name or flow.name,
        login=login,
        refresh=refresh,
        toAuth=toAuth,
        isSubscription=isSubscription,
        loginLabel=loginLabel,
    )


def oauth_auth_for_provider(
    providerId: str,
    *,
    name: str | None = None,
    isSubscription: bool | None = None,
    loginLabel: str | None = None,
) -> OAuthAuth | None:
    """The bridged ``OAuthAuth`` for a provider id, or ``None`` when no flow exists."""
    flow = get_oauth_provider(providerId)
    if flow is None:
        return None
    return oauth_auth_from_flow(
        flow, name=name, isSubscription=isSubscription, loginLabel=loginLabel
    )


__all__ = ["oauth_auth_for_provider", "oauth_auth_from_flow"]
