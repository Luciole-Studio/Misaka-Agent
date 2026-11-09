"""Translated from pi's ``packages/ai/test/github-copilot-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.oauth import github_copilot
from misaka.ai.auth.oauth.github_copilot import githubCopilotOAuth
from misaka.ai.auth.types import OAuthCredential
from misaka.ai.models import CreateModelsOptions, createModels
from misaka.ai.providers.github_copilot import githubCopilotProvider
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL
from tests.oauth_fetch import (
    epochMs,
    interaction,
    jsonResponse,
    requestUrl,
    stubFetch,
    textResponse,
    useFakeTimers,
)

testCopilotAccessToken = "tid=test;exp=9999999999;proxy-ep=proxy.individual.githubcopilot.com;"
testCopilotModelsUrl = "https://api.individual.githubcopilot.com/models"


@pytest.fixture
def neverAbortedSignal():
    return AbortController().signal


def requireModelId(models: list[Any], index: int) -> str:
    if index >= len(models):
        raise RuntimeError(f"Expected a GitHub Copilot model at index {index}")
    return models[index].id


def stubGitHubCopilotLoginFetch(monkeypatch: pytest.MonkeyPatch, *, models: Any, policy: Any = None) -> None:
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url.endswith("/login/device/code"):
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 1,
                    "expires_in": 900,
                }
            )
        if url.endswith("/login/oauth/access_token"):
            return jsonResponse({"access_token": "ghu_refresh_token"})
        if "/copilot_internal/v2/token" in url:
            return jsonResponse({"token": testCopilotAccessToken, "expires_at": 9999999999})
        if url == testCopilotModelsUrl:
            return models()
        if url.startswith(f"{testCopilotModelsUrl}/") and url.endswith("/policy"):
            if not policy:
                raise RuntimeError(f"Unexpected policy request: {url}")
            return policy(url[len(f"{testCopilotModelsUrl}/") : -len("/policy")])
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)


def loginGitHubCopilotForTest(
    *, onDeviceCode: Any, onPrompt: Any, onProgress: Any = None, signal: Any = None
) -> asyncio.Future[Any]:
    async def prompt(request: Any) -> str:
        if request.type != "text":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        return await onPrompt({"message": request.message, "placeholder": request.placeholder, "allowEmpty": True})

    def notify(event: Any) -> None:
        if event.type == "device_code":
            onDeviceCode(event.model_dump(exclude={"type"}, exclude_none=True))
        if event.type == "progress" and onProgress is not None:
            onProgress(event.message)

    return asyncio.ensure_future(
        githubCopilotOAuth.login(
            interaction(signal=signal if signal is not None else AbortController().signal, prompt=prompt, notify=notify)
        )
    )


async def emptyPrompt(_prompt: Any) -> str:
    return ""


async def refreshGitHubCopilotModelsForTest(
    monkeypatch: pytest.MonkeyPatch, data: list[Any], proxyHost: str = "proxy.individual.githubcopilot.com"
) -> OAuthCredential:
    accessToken = f"tid=test;exp=9999999999;proxy-ep={proxyHost};"
    modelsUrl = f"https://{re.sub(r'^proxy[.]', 'api.', proxyHost)}/models"

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if "/copilot_internal/v2/token" in url:
            return jsonResponse({"token": accessToken, "expires_at": 9999999999})

        if url == modelsUrl:
            assert init["headers"]["Authorization"] == f"Bearer {accessToken}"
            return jsonResponse({"data": data})

        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)
    return await githubCopilotOAuth.refresh(
        OAuthCredential(type="oauth", access="old-access-token", refresh="ghu_refresh_token", expires=0),
        AbortController().signal,
    )


async def storeWith(credentials: OAuthCredential) -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()

    async def write(_current: Any) -> OAuthCredential:
        return credentials.model_copy(update={"type": "oauth"})

    await store.modify("github-copilot", write)
    return store


async def test_filters_models_to_the_authenticated_account_picker_catalog(monkeypatch):
    provider = githubCopilotProvider()
    providerModels = provider.getModels()
    pickerModelId = requireModelId(providerModels, 0)
    disabledModelId = requireModelId(providerModels, 1)
    hiddenModelId = requireModelId(providerModels, 2)

    credentials = await refreshGitHubCopilotModelsForTest(
        monkeypatch,
        [
            {"id": pickerModelId, "model_picker_enabled": True, "capabilities": {"supports": {"tool_calls": True}}},
            {
                "id": disabledModelId,
                "model_picker_enabled": True,
                "policy": {"state": "disabled"},
                "capabilities": {"supports": {"tool_calls": True}},
            },
            {
                "id": hiddenModelId,
                "model_picker_enabled": False,
                "policy": {"state": "enabled"},
                "capabilities": {"supports": {"tool_calls": True}},
            },
        ],
    )
    assert credentials.availableModelIds == [pickerModelId]

    models = createModels(CreateModelsOptions(credentials=await storeWith(credentials)))
    models.setProvider(provider)
    assert [model.id for model in await models.getAvailable("github-copilot")] == [pickerModelId]


async def test_falls_back_to_explicitly_enabled_policy_models_when_the_picker_catalog_is_empty(monkeypatch):
    provider = githubCopilotProvider()
    enabledModelId = requireModelId(provider.getModels(), 0)
    credentials = await refreshGitHubCopilotModelsForTest(
        monkeypatch,
        [
            {
                "id": enabledModelId,
                "model_picker_enabled": False,
                "policy": {"state": "enabled"},
                "capabilities": {"supports": {"tool_calls": True}},
            },
            {
                "id": "policy-disabled-model",
                "model_picker_enabled": False,
                "policy": {"state": "disabled"},
                "capabilities": {"supports": {"tool_calls": True}},
            },
            {
                "id": "unconfigured-model",
                "model_picker_enabled": False,
                "capabilities": {"supports": {"tool_calls": True}},
            },
            {
                "id": "tool-incapable-model",
                "model_picker_enabled": False,
                "policy": {"state": "enabled"},
                "capabilities": {"supports": {"tool_calls": False}},
            },
        ],
    )

    assert credentials.availableModelIds == [enabledModelId]

    models = createModels(CreateModelsOptions(credentials=await storeWith(credentials)))
    models.setProvider(provider)
    assert [model.id for model in await models.getAvailable("github-copilot")] == [enabledModelId]


async def test_does_not_fall_back_to_policy_models_for_non_individual_accounts(monkeypatch):
    credentials = await refreshGitHubCopilotModelsForTest(
        monkeypatch,
        [
            {
                "id": "gpt-4.1",
                "model_picker_enabled": False,
                "policy": {"state": "enabled"},
                "capabilities": {"supports": {"tool_calls": True}},
            },
        ],
        "proxy.business.githubcopilot.com",
    )

    assert credentials.availableModelIds == []


async def test_does_not_retry_model_catalog_throttling_during_credential_refresh(monkeypatch, neverAbortedSignal):
    catalogRequests: list[str] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if "/copilot_internal/v2/token" in url:
            return jsonResponse({"token": testCopilotAccessToken, "expires_at": 9999999999})
        if url == testCopilotModelsUrl:
            catalogRequests.append(url)
            return jsonResponse({"error": "too many requests"}, 429, {"Retry-After": "0"})
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)

    with pytest.raises(RuntimeError, match="429"):
        await githubCopilotOAuth.refresh(
            OAuthCredential(type="oauth", access="old-access-token", refresh="ghu_refresh_token", expires=0),
            neverAbortedSignal,
        )
    assert len(catalogRequests) == 1


def _standardLoginFetch(*, verificationUri: str = "https://github.com/login/device") -> Any:
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if url.endswith("/login/device/code"):
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": verificationUri,
                    "interval": 1,
                    "expires_in": 900,
                }
            )

        if url.endswith("/login/oauth/access_token"):
            return jsonResponse({"access_token": "ghu_refresh_token"})

        if "/copilot_internal/v2/token" in url:
            return jsonResponse(
                {
                    "token": "tid=test;exp=9999999999;proxy-ep=proxy.individual.githubcopilot.com;",
                    "expires_at": 9999999999,
                }
            )

        if url.endswith("/models"):
            return jsonResponse({"data": []})

        if "/models/" in url and url.endswith("/policy"):
            return textResponse("", 200)

        raise RuntimeError(f"Unexpected fetch URL: {url}")

    return fetchMock


async def test_reports_device_code_details_through_on_device_code(monkeypatch):
    timers = useFakeTimers(monkeypatch, epochMs("2026-03-09T00:00:00Z"))
    stubFetch(monkeypatch, github_copilot, _standardLoginFetch())

    onDeviceCode: list[dict[str, Any]] = []
    loginPromise = loginGitHubCopilotForTest(onDeviceCode=onDeviceCode.append, onPrompt=emptyPrompt)

    await timers.advanceAsync(0)

    assert onDeviceCode == [
        {
            "userCode": "ABCD-EFGH",
            "verificationUri": "https://github.com/login/device",
            "intervalSeconds": 1,
            "expiresInSeconds": 900,
        }
    ]
    await timers.advanceAsync(1000)
    await loginPromise


async def test_updates_only_known_tool_capable_unconfigured_account_model_policies(monkeypatch):
    timers = useFakeTimers(monkeypatch)

    providerModels = githubCopilotProvider().getModels()
    configuredModelId = requireModelId(providerModels, 0)
    unconfiguredModelId = requireModelId(providerModels, 1)
    toolIncapableModelId = requireModelId(providerModels, 2)
    catalogRequests: list[bool] = []
    policyModelIds: list[str] = []

    def models() -> Any:
        catalogRequests.append(True)
        return jsonResponse(
            {
                "data": [
                    {
                        "id": configuredModelId,
                        "model_picker_enabled": True,
                        "policy": {"state": "enabled"},
                        "capabilities": {"supports": {"tool_calls": True}},
                    },
                    {
                        "id": unconfiguredModelId,
                        "model_picker_enabled": True,
                        "policy": {"state": "unconfigured"},
                        "capabilities": {"supports": {"tool_calls": True}},
                    },
                    {
                        "id": "remote-only-model",
                        "model_picker_enabled": True,
                        "policy": {"state": "unconfigured"},
                        "capabilities": {"supports": {"tool_calls": True}},
                    },
                    {
                        "id": toolIncapableModelId,
                        "model_picker_enabled": True,
                        "policy": {"state": "unconfigured"},
                        "capabilities": {"supports": {"tool_calls": False}},
                    },
                ],
            }
        )

    def policy(modelId: str) -> Any:
        policyModelIds.append(modelId)
        return textResponse("", 200)

    stubGitHubCopilotLoginFetch(monkeypatch, models=models, policy=policy)

    loginPromise = loginGitHubCopilotForTest(onDeviceCode=lambda _info: None, onPrompt=emptyPrompt)
    await timers.advanceAsync(1000)
    await loginPromise

    assert len(catalogRequests) == 1
    assert policyModelIds == [unconfiguredModelId]


async def test_retries_a_throttled_policy_update_after_retry_after(monkeypatch):
    timers = useFakeTimers(monkeypatch)

    modelId = requireModelId(githubCopilotProvider().getModels(), 0)
    policyRequests: list[bool] = []

    def policy(_modelId: str) -> Any:
        policyRequests.append(True)
        return (
            jsonResponse({"error": "too many requests"}, 429, {"Retry-After": "1"})
            if len(policyRequests) == 1
            else textResponse("", 200)
        )

    stubGitHubCopilotLoginFetch(
        monkeypatch,
        models=lambda: jsonResponse(
            {"data": [{"id": modelId, "model_picker_enabled": True, "policy": {"state": "unconfigured"}}]}
        ),
        policy=policy,
    )

    loginPromise = loginGitHubCopilotForTest(onDeviceCode=lambda _info: None, onPrompt=emptyPrompt)
    await timers.advanceAsync(1000)
    assert len(policyRequests) == 1
    await timers.advanceAsync(999)
    assert len(policyRequests) == 1
    await timers.advanceAsync(1)
    await loginPromise

    assert len(policyRequests) == 2


async def test_continues_policy_updates_after_a_transport_failure(monkeypatch):
    timers = useFakeTimers(monkeypatch)

    providerModels = githubCopilotProvider().getModels()
    modelIds = [requireModelId(providerModels, 0), requireModelId(providerModels, 1)]
    policyModelIds: list[str] = []

    def policy(modelId: str) -> Any:
        policyModelIds.append(modelId)
        if len(policyModelIds) == 1:
            raise TypeError("fetch failed")
        return textResponse("", 200)

    stubGitHubCopilotLoginFetch(
        monkeypatch,
        models=lambda: jsonResponse(
            {
                "data": [
                    {"id": id, "model_picker_enabled": True, "policy": {"state": "unconfigured"}} for id in modelIds
                ]
            }
        ),
        policy=policy,
    )

    loginPromise = loginGitHubCopilotForTest(onDeviceCode=lambda _info: None, onPrompt=emptyPrompt)
    await timers.advanceAsync(1000)
    await loginPromise

    assert policyModelIds == modelIds


async def test_stops_policy_updates_and_persists_authentication_when_the_retry_delay_exceeds_the_login_budget(
    monkeypatch, neverAbortedSignal
):
    timers = useFakeTimers(monkeypatch)

    providerModels = githubCopilotProvider().getModels()
    firstModelId = requireModelId(providerModels, 0)
    secondModelId = requireModelId(providerModels, 1)
    policyModelIds: list[str] = []

    def policy(modelId: str) -> Any:
        policyModelIds.append(modelId)
        return jsonResponse({"error": "too many requests"}, 429, {"Retry-After": "5"})

    stubGitHubCopilotLoginFetch(
        monkeypatch,
        models=lambda: jsonResponse(
            {
                "data": [
                    {"id": firstModelId, "model_picker_enabled": True, "policy": {"state": "unconfigured"}},
                    {"id": secondModelId, "model_picker_enabled": True, "policy": {"state": "unconfigured"}},
                ],
            }
        ),
        policy=policy,
    )

    store = InMemoryCredentialStore()
    models = createModels(CreateModelsOptions(credentials=store))
    models.setProvider(githubCopilotProvider())
    loginPromise = asyncio.ensure_future(
        models.login("github-copilot", "oauth", interaction(signal=neverAbortedSignal, prompt=emptyPrompt))
    )

    await timers.advanceAsync(1000)
    credential = await loginPromise
    assert (credential.type, credential.access) == ("oauth", testCopilotAccessToken)
    assert policyModelIds == [firstModelId]
    assert await store.read("github-copilot") == credential


async def test_rejects_a_non_http_s_verification_uri_before_it_reaches_on_device_code(monkeypatch):
    # A malicious enterprise OAuth server could return a verification_uri that
    # the browser launcher would otherwise hand to the OS. Ensure such values
    # are rejected at the deserialization boundary.
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url.endswith("/login/device/code"):
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "$(id>/tmp/pwned)",
                    "interval": 1,
                    "expires_in": 900,
                }
            )
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)

    onDeviceCode: list[Any] = []
    with pytest.raises(RuntimeError, match="Untrusted verification_uri"):
        await loginGitHubCopilotForTest(onDeviceCode=onDeviceCode.append, onPrompt=emptyPrompt)
    assert onDeviceCode == []


async def test_normalizes_verification_uri_before_it_reaches_on_device_code(monkeypatch):
    timers = useFakeTimers(monkeypatch, epochMs("2026-03-09T00:00:00Z"))

    rawVerificationUri = "https://github.com/login/\x1b]8;;evil"
    normalizedVerificationUri = URL(rawVerificationUri).href
    assert normalizedVerificationUri != rawVerificationUri

    stubFetch(monkeypatch, github_copilot, _standardLoginFetch(verificationUri=rawVerificationUri))

    onDeviceCode: list[dict[str, Any]] = []
    loginPromise = loginGitHubCopilotForTest(onDeviceCode=onDeviceCode.append, onPrompt=emptyPrompt)

    await timers.advanceAsync(0)

    assert onDeviceCode == [
        {
            "userCode": "ABCD-EFGH",
            "verificationUri": normalizedVerificationUri,
            "intervalSeconds": 1,
            "expiresInSeconds": 900,
        }
    ]
    assert all(info["verificationUri"] != rawVerificationUri for info in onDeviceCode)

    await timers.advanceAsync(1000)
    await loginPromise


async def test_waits_before_polling_and_increases_the_interval_after_slow_down(monkeypatch):
    startTime = epochMs("2026-03-09T00:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)

    accessTokenPollTimes: list[float] = []
    accessTokenResponses = [
        jsonResponse({"error": "authorization_pending", "error_description": "pending"}),
        jsonResponse({"error": "slow_down", "error_description": "slow down", "interval": 7}),
        jsonResponse({"access_token": "ghu_refresh_token"}),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if url.endswith("/login/device/code"):
            assert init["method"] == "POST"
            assert init["headers"]["Accept"] == "application/json"
            assert init["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
            assert "client_id=" in str(init["body"])
            assert "scope=read%3Auser" in str(init["body"])
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 5,
                    "expires_in": 900,
                }
            )

        if url.endswith("/login/oauth/access_token"):
            accessTokenPollTimes.append(timers.now)
            assert init["method"] == "POST"
            assert init["headers"]["Accept"] == "application/json"
            assert init["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
            assert "client_id=" in str(init["body"])
            assert "device_code=device-code" in str(init["body"])
            assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in str(init["body"])
            if not accessTokenResponses:
                raise RuntimeError("Unexpected extra access token poll")
            return accessTokenResponses.pop(0)

        if "/copilot_internal/v2/token" in url:
            return jsonResponse(
                {
                    "token": "tid=test;exp=9999999999;proxy-ep=proxy.individual.githubcopilot.com;",
                    "expires_at": 9999999999,
                }
            )

        if url.endswith("/models"):
            return jsonResponse({"data": []})

        if "/models/" in url and url.endswith("/policy"):
            return textResponse("", 200)

        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)

    loginPromise = loginGitHubCopilotForTest(
        onDeviceCode=lambda _info: None, onPrompt=emptyPrompt, onProgress=lambda _message: None
    )

    await timers.advanceAsync(0)
    assert len(accessTokenPollTimes) == 0

    await timers.advanceAsync(4999)
    assert len(accessTokenPollTimes) == 0

    await timers.advanceAsync(1)
    assert len(accessTokenPollTimes) == 1

    await timers.advanceAsync(4999)
    assert len(accessTokenPollTimes) == 1

    await timers.advanceAsync(1)
    assert len(accessTokenPollTimes) == 2

    # slow_down carried a server-provided interval of 7 seconds.
    await timers.advanceAsync(6999)
    assert len(accessTokenPollTimes) == 2

    await timers.advanceAsync(1)
    await loginPromise

    assert accessTokenPollTimes == [startTime + 5000, startTime + 10000, startTime + 17000]


async def test_times_out_after_repeated_slow_down_responses(monkeypatch):
    startTime = epochMs("2026-03-09T00:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)

    accessTokenPollTimes: list[float] = []
    accessTokenResponses = [
        jsonResponse({"error": "slow_down", "error_description": "slow down"}),
        jsonResponse({"error": "slow_down", "error_description": "still too fast"}),
        jsonResponse({"error": "authorization_pending", "error_description": "pending"}),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if url.endswith("/login/device/code"):
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 5,
                    "expires_in": 25,
                }
            )

        if url.endswith("/login/oauth/access_token"):
            accessTokenPollTimes.append(timers.now)
            if not accessTokenResponses:
                raise RuntimeError("Unexpected extra access token poll")
            return accessTokenResponses.pop(0)

        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, github_copilot, fetchMock)

    loginPromise = loginGitHubCopilotForTest(onDeviceCode=lambda _info: None, onPrompt=emptyPrompt)

    await timers.advanceAsync(4999)
    assert accessTokenPollTimes == []

    await timers.advanceAsync(1)
    assert accessTokenPollTimes == [startTime + 5000]

    await timers.advanceAsync(9999)
    assert accessTokenPollTimes == [startTime + 5000]

    await timers.advanceAsync(1)
    assert accessTokenPollTimes == [startTime + 5000, startTime + 15000]

    await timers.advanceAsync(9999)
    assert accessTokenPollTimes == [startTime + 5000, startTime + 15000]

    await timers.advanceAsync(1)
    with pytest.raises(RuntimeError, match="Device flow timed out after one or more slow_down responses"):
        await loginPromise

    assert accessTokenPollTimes == [startTime + 5000, startTime + 15000]
