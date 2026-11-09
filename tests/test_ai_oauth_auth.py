"""Translated from pi's ``packages/ai/test/oauth-auth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` stand-in."""

from __future__ import annotations

import time
from typing import Any

import misaka.ai.oauth as extensionOAuthCompatibility
from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.oauth import anthropic, github_copilot
from misaka.ai.auth.oauth.anthropic import anthropicOAuth
from misaka.ai.auth.oauth.github_copilot import githubCopilotOAuth
from misaka.ai.auth.oauth.kimi_coding import kimiCodingOAuth
from misaka.ai.auth.oauth.openai_chatgpt import openaiChatGPTOAuth
from misaka.ai.auth.oauth.openai_codex import openaiCodexOAuth
from misaka.ai.auth.oauth.openrouter import openRouterOAuth
from misaka.ai.auth.oauth.xai import xaiOAuth
from misaka.ai.auth.types import OAuthCredential
from misaka.ai.models import CreateModelsOptions, createModels
from misaka.ai.providers.anthropic import anthropicProvider
from misaka.ai.providers.github_copilot import githubCopilotProvider
from misaka.ai.providers.openai import openaiProvider
from misaka.utils.js import AbortController
from tests.oauth_fetch import jsonResponse, requestUrl, stubFetch

neverAbortedSignal = AbortController().signal
_MAX_SAFE_INTEGER = 9007199254740991


def credential(**fields: Any) -> OAuthCredential:
    return OAuthCredential.model_validate({"type": "oauth", **fields})


def authOf(auth: Any) -> dict[str, Any]:
    return auth.model_dump(exclude_none=True)


# --- OAuthAuth adapters ---------------------------------------------------------------------


def test_keeps_the_extension_oauth_barrel_free_of_built_in_flow_implementations():
    assert not hasattr(extensionOAuthCompatibility, "loginAnthropic")
    assert not hasattr(extensionOAuthCompatibility, "anthropicOAuth")


def test_identifies_only_subscription_backed_oauth_flows_as_subscriptions():
    for oauth in [anthropicOAuth, openaiChatGPTOAuth, openaiCodexOAuth, githubCopilotOAuth, kimiCodingOAuth, xaiOAuth]:
        assert oauth.isSubscription is True
    assert openRouterOAuth.isSubscription is not True


async def test_anthropic_to_auth_derives_the_api_key_from_the_access_token():
    auth = await anthropicOAuth.toAuth(credential(access="token", refresh="r", expires=0))
    assert authOf(auth) == {"apiKey": "token"}


def test_openai_exposes_chatgpt_oauth_alongside_api_key_auth():
    provider = openaiProvider()
    assert provider.auth.apiKey is not None
    oauth = provider.auth.oauth
    assert (oauth.name, oauth.isSubscription, oauth.loginLabel) == (
        "OpenAI (ChatGPT subscription)",
        True,
        "Sign in with ChatGPT",
    )


async def test_openai_codex_to_auth_derives_the_api_key_from_the_access_token():
    auth = await openaiCodexOAuth.toAuth(credential(access="token", refresh="r", expires=0))
    assert authOf(auth) == {"apiKey": "token"}


async def test_openrouter_derives_the_api_key_and_keeps_the_permanent_credential_on_refresh():
    permanent = credential(access="token", refresh="", expires=_MAX_SAFE_INTEGER)
    assert authOf(await openRouterOAuth.toAuth(permanent)) == {"apiKey": "token"}
    assert await openRouterOAuth.refresh(permanent, neverAbortedSignal) is permanent


async def test_xai_to_auth_derives_the_api_key_from_the_access_token():
    auth = await xaiOAuth.toAuth(credential(access="token", refresh="r", expires=0))
    assert authOf(auth) == {"apiKey": "token"}


async def test_github_copilot_to_auth_derives_base_url_from_the_token_proxy_endpoint():
    access = "tid=abc;exp=123;proxy-ep=proxy.enterprise.example;rest"
    auth = await githubCopilotOAuth.toAuth(credential(access=access, refresh="r", expires=0))
    assert authOf(auth) == {"apiKey": access, "baseUrl": "https://api.enterprise.example"}


async def test_github_copilot_to_auth_falls_back_to_the_enterprise_domain_then_the_individual_endpoint():
    enterprise = await githubCopilotOAuth.toAuth(
        credential(access="no-proxy-ep", refresh="r", expires=0, enterpriseUrl="https://company.ghe.com")
    )
    assert enterprise.baseUrl == "https://copilot-api.company.ghe.com"

    individual = await githubCopilotOAuth.toAuth(credential(access="no-proxy-ep", refresh="r", expires=0))
    assert individual.baseUrl == "https://api.individual.githubcopilot.com"


async def test_anthropic_refresh_exchanges_the_refresh_token_and_returns_a_typed_credential(monkeypatch):
    stubFetch(
        monkeypatch,
        anthropic,
        lambda *_: jsonResponse({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
    )

    refreshed = await anthropicOAuth.refresh(credential(access="old", refresh="old-r", expires=0), neverAbortedSignal)
    assert refreshed.type == "oauth"
    assert refreshed.access == "new-access"
    assert refreshed.refresh == "new-refresh"
    assert refreshed.expires > time.time() * 1000


async def test_github_copilot_refresh_preserves_the_enterprise_domain(monkeypatch):
    fetchedUrls: list[str] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        fetchedUrls.append(url)
        if url.endswith("/models"):
            return jsonResponse({"data": []})
        return jsonResponse({"token": "new-token", "expires_at": 9999999999})

    stubFetch(monkeypatch, github_copilot, fetchMock)

    refreshed = await githubCopilotOAuth.refresh(
        credential(access="old", refresh="gh-token", expires=0, enterpriseUrl="company.ghe.com"),
        neverAbortedSignal,
    )
    assert refreshed.access == "new-token"
    assert refreshed.enterpriseUrl == "company.ghe.com"
    assert "api.company.ghe.com" in fetchedUrls[0]


# --- OAuth through Models.getAuth (lazy load chain) -----------------------------------------


async def storeWith(providerId: str, stored: OAuthCredential) -> InMemoryCredentialStore:
    credentials = InMemoryCredentialStore()

    async def write(_current: Any) -> OAuthCredential:
        return stored

    await credentials.modify(providerId, write)
    return credentials


async def test_resolves_stored_anthropic_oauth_credentials_via_the_lazy_flow_import():
    credentials = await storeWith(
        "anthropic",
        # Keep this beyond getAuth()'s refresh window.
        credential(access="oauth-access-token", refresh="r", expires=time.time() * 1000 + 10 * 60_000),
    )
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(anthropicProvider())

    model = models.getModels("anthropic")[0]
    result = await models.getAuth(model.provider)
    assert result is not None
    assert result.auth.apiKey == "oauth-access-token"
    assert result.source == "OAuth"


async def test_resolves_stored_github_copilot_oauth_credentials_including_per_credential_base_url():
    access = "tid=abc;exp=123;proxy-ep=proxy.business.githubcopilot.com;rest"
    credentials = await storeWith(
        "github-copilot",
        # Keep this beyond getAuth()'s refresh window.
        credential(access=access, refresh="r", expires=time.time() * 1000 + 10 * 60_000),
    )
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(githubCopilotProvider())

    model = models.getModels("github-copilot")[0]
    result = await models.getAuth(model.provider)
    assert result is not None
    assert result.auth.apiKey == access
    assert result.auth.baseUrl == "https://api.business.githubcopilot.com"
