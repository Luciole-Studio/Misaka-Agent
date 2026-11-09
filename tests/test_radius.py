"""Ported from pi's ``packages/coding-agent/test/radius.test.ts`` (v1.1.0). ``vi.spyOn(globalThis, "fetch")`` is the
``FetchMock`` of ``tests/oauth_fetch.py`` over the Radius config loader's ``fetch``."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.providers import radius_config
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime
from misaka.core.radius import RADIUS_PROVIDER_ID
from misaka.utils.whatwg_fetch import Response
from tests.oauth_fetch import jsonResponse, requestUrl, stubFetch


@pytest.fixture(autouse=True)
def allowNetwork(monkeypatch):
    """Enable network code paths; every fetch here is answered by a stand-in."""
    monkeypatch.delenv("MISAKA_OFFLINE", raising=False)


def _hourFromNowMs() -> int:
    return int(time.time() * 1000) + 60 * 60 * 1000


def radiusOAuthCredential(gatewayBaseUrl: str) -> dict[str, Any]:
    return {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": _hourFromNowMs(),
        "gatewayConfig": radiusConfig(gatewayBaseUrl),
    }


def radiusConfig(baseUrl: str) -> dict[str, Any]:
    return {
        "baseUrl": baseUrl,
        "models": [
            {
                "id": "auto",
                "name": "Radius Auto",
                "reasoning": False,
                "input": ["text"],
                "cost": {"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 0.2},
                "contextWindow": 128000,
                "maxTokens": 16384,
            },
        ],
    }


async def test_restores_the_legacy_credential_catalog_without_network_access():
    runtime = await ModelRuntime.create({"credentials": AuthStorage.inMemory({
            RADIUS_PROVIDER_ID: radiusOAuthCredential("https://radius.example.com/v1"),
        }), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})

    model = runtime.getModel(RADIUS_PROVIDER_ID, "auto")
    assert (model.api, model.baseUrl) == ("pi-messages", "https://radius.example.com/v1")
    assert runtime.getProvider(RADIUS_PROVIDER_ID).name == "Radius"
    assert runtime.hasConfiguredAuth(RADIUS_PROVIDER_ID) is True


async def test_fetches_and_stores_the_catalog_for_configured_radius_auth(monkeypatch):
    fetch = stubFetch(monkeypatch, radius_config, lambda *_: jsonResponse(radiusConfig("https://radius.example.com/v1")))
    modelsStore = InMemoryModelsStore()
    credentials = AuthStorage.inMemory({
        RADIUS_PROVIDER_ID: {
            "type": "oauth",
            "access": "access-token",
            "refresh": "refresh-token",
            "expires": _hourFromNowMs(),
        },
    })
    runtime = await ModelRuntime.create({"credentials": credentials, "modelsStore": modelsStore, "modelsPath": None, "allowModelNetwork": True})

    assert runtime.getModel(RADIUS_PROVIDER_ID, "auto") is not None
    assert len((await modelsStore.read(RADIUS_PROVIDER_ID)).models) == 1
    radiusRequest = next(
        (call for call in fetch.calls if requestUrl(call[0]) == "https://radius.pi.dev/v1/config"),
        None,
    )
    assert radiusRequest is not None
    assert radiusRequest[1]["headers"]["authorization"] == "Bearer access-token"


async def test_does_not_refresh_catalogs_over_the_network_by_default(monkeypatch):
    def unexpected(*_args):
        raise RuntimeError("unexpected catalog fetch")

    fetchSpy = stubFetch(monkeypatch, radius_config, unexpected)
    runtime = await ModelRuntime.create({"credentials": AuthStorage.inMemory({
            RADIUS_PROVIDER_ID: radiusOAuthCredential("https://radius.example.com/v1"),
        }), "modelsStore": InMemoryModelsStore(), "modelsPath": None})

    assert runtime.getModel(RADIUS_PROVIDER_ID, "auto") is not None
    assert fetchSpy.calls == []


async def test_does_not_fetch_or_make_radius_models_available_without_configured_auth(monkeypatch):
    def unexpected(*_args):
        raise RuntimeError("unexpected Radius config fetch")

    fetchSpy = stubFetch(monkeypatch, radius_config, unexpected)
    runtime = await ModelRuntime.create({"credentials": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": True})

    assert [model for model in runtime.getAvailableSnapshot() if model.provider == RADIUS_PROVIDER_ID] == []
    assert not any("radius.pi.dev/v1/config" in requestUrl(url) for url, _init in fetchSpy.calls)


async def test_supports_custom_radius_gateways_from_models_json(monkeypatch, tmp_path):
    stubFetch(
        monkeypatch,
        radius_config,
        lambda *_: Response(json.dumps(radiusConfig("http://localhost:8788/v1")), status=200),
    )
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(
        json.dumps({
            "providers": {"radius-dev": {"name": "Radius (dev)", "baseUrl": "http://localhost:8788", "oauth": "radius"}},
        }),
        encoding="utf-8",
    )
    runtime = await ModelRuntime.create({"credentials": AuthStorage.inMemory({
            "radius-dev": {
                "type": "oauth",
                "access": "access-token",
                "refresh": "refresh-token",
                "expires": _hourFromNowMs(),
            },
        }), "modelsStore": InMemoryModelsStore(), "modelsPath": str(modelsPath), "allowModelNetwork": True})

    model = runtime.getModel("radius-dev", "auto")
    assert (model.api, model.baseUrl) == ("pi-messages", "http://localhost:8788/v1")
    assert runtime.getProvider("radius-dev").name == "Radius (dev)"


async def test_requires_base_url_for_custom_radius_gateways(tmp_path):
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(json.dumps({"providers": {"radius-dev": {"oauth": "radius"}}}), encoding="utf-8")
    runtime = await ModelRuntime.create({"credentials": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": str(modelsPath), "allowModelNetwork": False})

    assert '"baseUrl" is required when "oauth" is set' in runtime.getError()
