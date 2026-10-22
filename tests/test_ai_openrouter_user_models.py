"""Translated from pi's ``packages/ai/test/openrouter-user-models.test.ts`` (v1.3.0).

MISAKA fork: ``globalThis.fetch`` is the ``fetch`` ``misaka.ai.providers.openrouter`` imports, replaced here. The
cases after the upstream ones pin the request itself: the configured base URL, the credential, and the errors.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import ModelsPublication, RefreshModelsContext
from misaka.ai.providers import openrouter
from misaka.ai.providers.openrouter import personalizeOpenRouterCatalog
from misaka.ai.types import ClassifierModel, ImageModel, Model
from misaka.utils.js import AbortController
from misaka.utils.whatwg_fetch import Response

zeroCost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def chatModel(id: str) -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": "openai-completions",
            "provider": "openrouter",
            "baseUrl": "https://openrouter.ai/api/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 0},
            "contextWindow": 200_000,
            "maxTokens": 50_000,
        }
    )


imageModel = ImageModel.model_validate(
    {
        "type": "image",
        "id": "image",
        "name": "image",
        "api": "openrouter-images",
        "provider": "openrouter",
        "baseUrl": "https://openrouter.ai/api/v1",
        "input": ["text"],
        "output": ["image"],
        "cost": zeroCost,
    }
)

classifierModel = ClassifierModel.model_validate(
    {
        "type": "classifier",
        "id": "classifier",
        "name": "classifier",
        "api": "typesafe-system-one",
        "provider": "openrouter",
        "baseUrl": "https://openrouter.ai/api/v1",
        "input": ["text"],
        "cost": zeroCost,
        "contextWindow": 64_000,
    }
)


async def _publish(_publication: ModelsPublication) -> bool:
    return True


def refreshContext(**overrides: Any) -> RefreshModelsContext:
    fields: dict[str, Any] = {
        "credential": {"type": "api_key", "key": "sk-or-test"},
        "publish": _publish,
        "allowNetwork": True,
        "signal": AbortController().signal,
        **overrides,
    }
    return RefreshModelsContext(**fields)


@pytest.fixture
def requests(monkeypatch) -> SimpleNamespace:
    """``vi.spyOn(globalThis, "fetch")``: ``requests.response`` answers every request; ``requests.seen`` records
    each one's URL and init."""
    state = SimpleNamespace(seen=[], response=Response(json.dumps({"data": []})))

    async def fetch(url: str, init: dict[str, Any]) -> Response:
        state.seen.append((url, init))
        return state.response

    monkeypatch.setattr(openrouter, "fetch", fetch)
    return state


def stubUserModels(requests: SimpleNamespace, response: Response) -> None:
    requests.response = response


async def test_keeps_only_the_keys_chat_models_with_the_keys_limits_and_prices(requests):
    data = [
        {
            "id": "listed",
            "context_length": 1_000_000,
            "top_provider": {"context_length": 262_144, "max_completion_tokens": 32_768},
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        # A missing limit keeps the catalog value, so generator fixes for missing OpenRouter data survive.
        {"id": "no-limits", "context_length": 0, "top_provider": {"context_length": None, "max_completion_tokens": None}},
        # Router models report -1, meaning the price depends on the model they pick.
        {"id": "router", "pricing": {"prompt": "-1", "completion": "-1"}},
    ]
    stubUserModels(requests, Response(json.dumps({"data": data})))
    catalog = [
        chatModel("listed"),
        chatModel("no-limits"),
        chatModel("router"),
        chatModel("blocked"),
        imageModel,
        classifierModel,
    ]

    result = await personalizeOpenRouterCatalog(catalog, refreshContext())

    assert [model.id for model in result] == ["listed", "no-limits", "router", "image", "classifier"]
    assert (result[0].contextWindow, result[0].maxTokens) == (262_144, 32_768)
    assert result[0].cost.model_dump(exclude_none=True) == {"input": 3, "output": 15, "cacheRead": 0, "cacheWrite": 0}
    assert (result[1].contextWindow, result[1].maxTokens, result[1].cost) == (200_000, 50_000, chatModel("x").cost)
    assert result[2].cost.model_dump(exclude_none=True) == zeroCost
    # /models/user lists text models only, so image and classifier models are not filtered.
    assert result[3] is imageModel
    assert result[4] is classifierModel


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_returns_the_catalog_unchanged_when_the_endpoint_rejects_the_key_or_is_missing(requests, status):
    # For example a proxy configured in models.json that rejects the key or has no /models/user:
    # the user keeps the full list instead of a failing refresh.
    stubUserModels(requests, Response("", status=status))
    catalog = [chatModel("a")]

    assert await personalizeOpenRouterCatalog(catalog, refreshContext()) is catalog


# --- MISAKA: the request -------------------------------------------------------------------------


async def test_asks_the_configured_base_url_with_the_keys_bearer_token(requests):
    await personalizeOpenRouterCatalog([chatModel("a")], refreshContext(baseUrl="https://us.openrouter.ai/api/v1//"))

    ((url, init),) = requests.seen
    assert url == "https://us.openrouter.ai/api/v1/models/user"
    assert init["headers"] == {"accept": "application/json", "authorization": "Bearer sk-or-test"}
    assert init["signal"] is not None


async def test_defaults_to_openrouter_and_signs_in_with_an_oauth_access_token(requests):
    await personalizeOpenRouterCatalog(
        [chatModel("a")], refreshContext(credential={"type": "oauth", "access": "oauth-access", "refresh": "r", "expires": 0})
    )

    ((url, init),) = requests.seen
    assert url == "https://openrouter.ai/api/v1/models/user"
    assert init["headers"]["authorization"] == "Bearer oauth-access"


async def test_sends_nothing_without_a_credential(requests):
    catalog = [chatModel("a")]

    assert await personalizeOpenRouterCatalog(catalog, refreshContext(credential=None)) is catalog
    assert requests.seen == []


async def test_fails_on_other_http_errors_and_invalid_bodies(requests):
    stubUserModels(requests, Response("", status=500))
    with pytest.raises(RuntimeError, match="OpenRouter model list request failed: 500"):
        await personalizeOpenRouterCatalog([chatModel("a")], refreshContext())

    stubUserModels(requests, Response(json.dumps({"models": []})))
    with pytest.raises(RuntimeError, match="Invalid OpenRouter model list response"):
        await personalizeOpenRouterCatalog([chatModel("a")], refreshContext())


@pytest.mark.parametrize("status", [200, 401, 404, 500])
async def test_every_answer_releases_its_connection(monkeypatch, status):
    # The fetch shim keeps a client per request until its body is read or cancelled: the early returns and the
    # error must cancel it, or every refresh of a rejected key leaks a connection.
    from misaka.utils import whatwg_fetch
    from tests.http_capture import Reply, captureServer

    clients = []
    createClient = whatwg_fetch.createClient

    def tracking():
        client = createClient()
        clients.append(client)
        return client

    monkeypatch.setattr(whatwg_fetch, "createClient", tracking)
    body = b'{"data": []}' if status == 200 else b'{"error": "x"}'
    with captureServer(lambda _request: Reply(status=status, headers={"content-type": "application/json"}, body=body)) as server:
        try:
            await personalizeOpenRouterCatalog([chatModel("a")], refreshContext(baseUrl=server.baseUrl))
        except RuntimeError:
            assert status == 500

    assert len(clients) == 1
    assert clients[0].is_closed
