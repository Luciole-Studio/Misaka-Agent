"""Translated from pi's ``packages/ai/test/radius-provider.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` stand-in (``vi.spyOn(globalThis, "fetch")``)."""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.models import CreateModelsOptions, ModelsRefreshOptions, createModels
from misaka.ai.models_store import InMemoryModelsStore, ModelsStoreEntry
from misaka.ai.providers import radius_config
from misaka.ai.providers.radius import RadiusProviderOptions, radiusProvider
from misaka.ai.providers.radius_config import (
    RadiusGatewayConfig,
    getRadiusModelsFromConfig,
)
from tests.oauth_fetch import jsonResponse, stubFetch


def radiusConfig() -> dict[str, Any]:
    return {
        "baseUrl": "https://radius.example/v1",
        "models": [
            {
                "id": "balanced",
                "name": "Fresh Balanced",
                "reasoning": True,
                "input": ["text"],
                "cost": {"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 0},
                "contextWindow": 424242,
                "maxTokens": 32000,
            },
            {
                "id": "organization-only",
                "name": "Organization Only",
                "reasoning": False,
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 128000,
                "maxTokens": 16000,
            },
        ],
    }


async def storedKey() -> InMemoryCredentialStore:
    credentials = InMemoryCredentialStore()

    async def write(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="radius-key")

    await credentials.modify("radius", write)
    return credentials


def test_ships_a_static_public_catalog_for_the_default_gateway():
    provider = radiusProvider()
    assert len(provider.getModels()) > 0
    assert any(
        (model.id, model.provider, model.api) == ("balanced", "radius", "pi-messages") for model in provider.getModels()
    )


def test_does_not_apply_the_public_radius_catalog_to_custom_gateways():
    provider = radiusProvider(RadiusProviderOptions(id="radius-dev", gateway="http://localhost:8788"))
    assert provider.getModels() == []


async def test_replaces_the_static_public_catalog_with_refreshed_models(monkeypatch):
    stubFetch(monkeypatch, radius_config, lambda *_: jsonResponse(radiusConfig()))
    models = createModels(CreateModelsOptions(credentials=await storedKey()))
    models.setProvider(radiusProvider())

    result = await models.refresh(ModelsRefreshOptions(providers=["radius"]))

    assert result.errors == {}
    balanced = models.getModel("radius", "balanced")
    assert (balanced.name, balanced.baseUrl, balanced.contextWindow) == (
        "Fresh Balanced",
        "https://radius.example/v1",
        424242,
    )
    assert models.getModel("radius", "organization-only") is not None
    # Models disabled by the organization must not reappear from the shipped catalog.
    assert [model.id for model in models.getModels("radius")] == ["balanced", "organization-only"]


async def test_replaces_the_static_public_catalog_with_a_cached_catalog_without_network_access():
    store = InMemoryModelsStore()
    await store.write(
        "radius",
        ModelsStoreEntry(
            models=getRadiusModelsFromConfig("radius", RadiusGatewayConfig.model_validate(radiusConfig())),
            checkedAt=int(time.time() * 1000),
        ),
    )
    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(radiusProvider())

    await models.refresh(ModelsRefreshOptions(providers=["radius"], allowNetwork=False))

    assert models.getModel("radius", "balanced").name == "Fresh Balanced"
    assert models.getModel("radius", "organization-only") is not None
    assert [model.id for model in models.getModels("radius")] == ["balanced", "organization-only"]


async def test_exposes_no_models_when_the_organization_disabled_all_of_them(monkeypatch):
    stubFetch(monkeypatch, radius_config, lambda *_: jsonResponse({"baseUrl": "https://radius.example/v1", "models": []}))
    models = createModels(CreateModelsOptions(credentials=await storedKey()))
    models.setProvider(radiusProvider())

    result = await models.refresh(ModelsRefreshOptions(providers=["radius"]))

    assert result.errors == {}
    assert models.getModels("radius") == []
