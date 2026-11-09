"""pi's ``remote-catalog-provider.test.ts`` cases (v1.3.0) (``misaka/core/remote_catalog_provider.py``)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthResult,
    ModelAuth,
    ProviderAuth,
)
from misaka.ai.models import (
    CreateModelsOptions,
    CreateProviderOptions,
    ModelsRefreshOptions,
    RefreshModelsContext,
    createModels,
    createProvider,
)
from misaka.ai.models_store import InMemoryModelsStore, ModelsStoreEntry
from misaka.ai.types import Model
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.core.remote_catalog_provider import (
    REMOTE_CATALOG_MODEL_TYPES,
    withPersonalizedCatalog,
    withRemoteCatalog,
)
from misaka.utils import management_http
from misaka.utils.js import AbortController
from misaka.utils.whatwg_fetch import Response


def model(id: str) -> dict:
    return {
        "id": id, "name": id, "api": "openai-completions", "provider": "test-provider",
        "baseUrl": "https://example.test/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100,
    }


async def _resolve(*, ctx, credential, signal):
    return AuthResult(auth=ModelAuth())


class _UnusedApi:
    def stream(self, *_args):
        raise RuntimeError("not used")

    def streamSimple(self, *_args):
        raise RuntimeError("not used")


def _provider(localGeneratedAt: int | None = None):
    return withRemoteCatalog(
        createProvider(CreateProviderOptions(
            id="test-provider",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_resolve)),
            models=[Model.model_validate(model("static"))],
            api=_UnusedApi(),
        )),
        "https://catalog.example.test",
        localGeneratedAt,
    )


async def refreshProvider(provider, store, *, allowNetwork=True, force=None, signal=None) -> None:
    async def publish(publication) -> bool:
        if publication.persist_is_set and publication.persist is None:
            await store.delete(provider.id)
        elif publication.persist_is_set:
            await store.write(provider.id, publication.persist)
        if publication.update is not None:
            publication.update()
        return True

    await provider.refreshModels(RefreshModelsContext(
        credential=ApiKeyCredential(),
        stored=await store.read(provider.id),
        publish=publish,
        allowNetwork=allowNetwork,
        force=force,
        signal=signal or AbortController().signal,
    ))


def _ids(models) -> list[str]:
    return [entry.id for entry in models]


def _json(value, *, status=200, headers=None) -> Response:
    return Response(json.dumps(value).encode(), status=status, headers=headers or {})


def _mock_fetch(monkeypatch, respond) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fetch(input, init=None):
        calls.append((str(input), dict(init.get("headers") or {})))
        return await respond()

    monkeypatch.setattr(management_http, "fetch", fetch)
    return calls


def _queue(monkeypatch, responses: list[Response]) -> list[tuple[str, dict]]:
    async def respond():
        return responses.pop(0)

    return _mock_fetch(monkeypatch, respond)


async def test_parses_keyed_catalogs_sends_headers_observes_the_ttl_and_supports_forced_refreshes(monkeypatch):
    async def respond():
        return _json({"dynamic": model("dynamic")}, headers={"content-type": "application/json"})

    calls = _mock_fetch(monkeypatch, respond)
    provider = _provider()
    store = InMemoryModelsStore()
    await refreshProvider(provider, store)
    await refreshProvider(provider, store)
    await refreshProvider(provider, store, force=True)

    assert _ids(provider.getModels()) == ["static", "dynamic"]
    assert _ids((await store.read(provider.id)).models) == ["dynamic"]
    assert len(calls) == 2
    assert calls[0][1]["User-Agent"] == getPiUserAgent()
    requested = urlsplit(calls[0][0])
    assert requested.path == "/api/models/providers/test-provider"
    assert parse_qs(requested.query)["types"] == [",".join(REMOTE_CATALOG_MODEL_TYPES)]


async def test_overlays_image_and_classifier_models_and_drops_unknown_model_types(monkeypatch):
    cost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}

    async def respond():
        return _json({
            "chat": {**model("chat"), "type": "chat"},
            "flux": {"type": "image", "id": "flux", "name": "FLUX", "api": "openrouter-images",
                     "provider": "test-provider", "baseUrl": "https://example.test/v1", "input": ["text"],
                     "output": ["image"], "cost": cost},
            "jev": {"type": "classifier", "id": "jev", "name": "Jev", "api": "typesafe-system-one",
                    "provider": "test-provider", "baseUrl": "https://example.test/v1", "input": ["text"],
                    "cost": cost, "contextWindow": 64000},
            "clip": {**model("clip"), "type": "video"},
        }, headers={"content-type": "application/json"})

    _mock_fetch(monkeypatch, respond)
    provider = _provider()
    store = InMemoryModelsStore()
    await refreshProvider(provider, store)

    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(provider)
    assert _ids(models.getAllModels("test-provider")) == ["static", "chat", "flux", "jev"]
    assert models.getModelOfType("image", "test-provider", "flux").type == "image"
    assert models.getModelOfType("classifier", "test-provider", "jev").type == "classifier"
    assert models.getModel("test-provider", "flux") is None
    assert _ids((await store.read(provider.id)).models) == ["chat", "flux", "jev"]


async def test_prefers_the_newer_of_the_generated_and_remote_catalogs(monkeypatch):
    localGeneratedAt = int(datetime(2026, 7, 23, 10, tzinfo=UTC).timestamp() * 1000)

    def header(ms: int) -> str:
        return format_datetime(datetime.fromtimestamp(ms / 1000, UTC), usegmt=True)

    newerHeader = header(localGeneratedAt + 60_000)
    _queue(monkeypatch, [
        _json({"old": model("old")}, headers={"last-modified": header(localGeneratedAt - 60_000)}),
        _json({"newer": model("newer")}, headers={"last-modified": newerHeader}),
    ])
    provider = _provider(localGeneratedAt)
    store = InMemoryModelsStore()

    await refreshProvider(provider, store)
    assert _ids(provider.getModels()) == ["static"]

    await refreshProvider(provider, store, force=True)
    assert _ids(provider.getModels()) == ["static", "newer"]
    assert (await store.read(provider.id)).lastModified == localGeneratedAt + 60_000


async def test_revalidates_a_stored_catalog_with_its_etag_and_keeps_the_overlay_on_304(monkeypatch):
    calls = _queue(monkeypatch, [
        _json({"dynamic": model("dynamic")}, headers={"content-type": "application/json", "etag": '"catalog-1"'}),
        Response(None, status=304, headers={"etag": '"catalog-1"'}),
    ])
    provider = _provider()
    store = InMemoryModelsStore()

    await refreshProvider(provider, store)
    assert "if-none-match" not in calls[0][1]
    assert (await store.read(provider.id)).etag == '"catalog-1"'

    checkedAt = (await store.read(provider.id)).checkedAt
    await refreshProvider(provider, store, force=True)

    assert calls[1][1]["if-none-match"] == '"catalog-1"'
    assert _ids(provider.getModels()) == ["static", "dynamic"]
    stored = await store.read(provider.id)
    assert _ids(stored.models) == ["dynamic"]
    assert stored.etag == '"catalog-1"'
    assert stored.checkedAt >= (checkedAt or 0)


async def test_drops_a_stale_etag_when_the_overlay_becomes_unavailable(monkeypatch):
    _queue(monkeypatch, [
        _json({"dynamic": model("dynamic")}, headers={"content-type": "application/json", "etag": '"catalog-1"'}),
        Response("not implemented", status=501),
    ])
    provider = _provider()
    store = InMemoryModelsStore()

    await refreshProvider(provider, store)
    await refreshProvider(provider, store, force=True)

    assert (await store.read(provider.id)).etag is None


async def test_keeps_the_etag_and_overlay_after_a_transient_failure(monkeypatch):
    calls = _queue(monkeypatch, [
        _json({"dynamic": model("dynamic")}, headers={"content-type": "application/json", "etag": '"catalog-1"'}),
        Response("rate limited", status=429),
        Response("rate limited", status=429),
        Response("rate limited", status=429),
        Response(None, status=304, headers={"etag": '"catalog-1"'}),
    ])
    provider = _provider()
    store = InMemoryModelsStore()

    await refreshProvider(provider, store)
    with pytest.raises(RuntimeError, match="429"):
        await refreshProvider(provider, store, force=True)

    stored = await store.read(provider.id)
    assert stored.etag == '"catalog-1"'
    assert _ids(stored.models) == ["dynamic"]

    await refreshProvider(provider, store, force=True)
    assert calls[4][1]["if-none-match"] == '"catalog-1"'
    assert _ids(provider.getModels()) == ["static", "dynamic"]


async def test_lets_a_newer_catalog_request_bypass_a_stalled_older_request_without_stale_publication(monkeypatch):
    firstStarted = asyncio.Event()
    firstResponse: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
    calls = 0

    async def respond():
        nonlocal calls
        calls += 1
        if calls == 1:
            firstStarted.set()
            return await firstResponse
        return _json({"newer": model("newer")}, headers={"content-type": "application/json"})

    _mock_fetch(monkeypatch, respond)
    provider = _provider()
    store = InMemoryModelsStore()
    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(provider)

    first = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(providers=[provider.id], force=True)))
    await firstStarted.wait()
    await models.refresh(ModelsRefreshOptions(providers=[provider.id], force=True))
    await first
    assert _ids(provider.getModels()) == ["static", "newer"]

    firstResponse.set_result(_json({"older": model("older")}, headers={"content-type": "application/json"}))
    await asyncio.sleep(0.01)

    assert _ids(provider.getModels()) == ["static", "newer"]
    assert _ids((await store.read(provider.id)).models) == ["newer"]


async def test_treats_unimplemented_catalog_routes_as_an_unavailable_overlay(monkeypatch):
    async def respond():
        return Response("not implemented", status=501)

    _mock_fetch(monkeypatch, respond)
    provider = _provider()
    store = InMemoryModelsStore()

    assert await refreshProvider(provider, store) is None
    assert _ids(provider.getModels()) == ["static"]
    stored = await store.read(provider.id)
    assert stored.models == [] and isinstance(stored.checkedAt, int)


def test_merge_models_keeps_first_occurrence_order_and_lets_the_last_value_win():
    from misaka.core.remote_catalog_provider import _mergeModels

    def catalog_model(id: str, **overrides) -> Model:
        return Model.model_validate({
            "id": id, "name": id, "api": "openai-completions", "provider": "remote",
            "baseUrl": "https://remote.test/v1", "reasoning": False, "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 1000, "maxTokens": 100, **overrides,
        })

    baseline = [catalog_model("a"), catalog_model("b"), catalog_model("a", name="a-dup")]
    dynamic = [catalog_model("b", name="b-dynamic"), catalog_model("c")]

    merged = _mergeModels(baseline, dynamic)

    assert [(model.id, model.name) for model in merged] == [("a", "a-dup"), ("b", "b-dynamic"), ("c", "c")]


# personalized catalog provider (pi 1.2, #10672)

_imageModel = {
    "type": "image", "id": "image", "name": "image", "api": "openrouter-images", "provider": "test-provider",
    "baseUrl": "https://example.test/v1", "input": ["text"], "output": ["image"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
}


def _builtinProvider():
    return createProvider(CreateProviderOptions(
        id="test-provider",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_resolve)),
        models=[Model.model_validate(model("static"))],
        api=_UnusedApi(),
    ))


@pytest.mark.parametrize("catalogBaseUrl", ["https://catalog.example.test", None])
async def test_keeps_the_saved_list_and_saves_nothing_when_personalization_fails(monkeypatch, catalogBaseUrl):
    # Saving the catalog without personalization would replace the key's list with every model.
    calls = _queue(monkeypatch, [Response(b"not found", status=404)])
    store = InMemoryModelsStore()
    saved = ModelsStoreEntry(models=[Model.model_validate(model("allowed"))], checkedAt=1)
    await store.write("test-provider", saved)

    async def personalize(_catalog, _context):
        raise RuntimeError("user models request failed")

    provider = withPersonalizedCatalog(_builtinProvider(), personalize, catalogBaseUrl)

    with pytest.raises(RuntimeError, match="user models request failed"):
        await refreshProvider(provider, store)
    assert _ids(provider.getModels()) == ["allowed"]
    assert await store.read(provider.id) == saved
    # MISAKA fork: no catalog endpoint, no pi.dev request.
    assert len(calls) == (1 if catalogBaseUrl else 0)


async def test_restores_a_saved_list_as_is_and_falls_back_to_the_built_in_models_only_when_it_is_empty():
    async def personalize(_catalog, _context):
        raise RuntimeError("not used offline")

    store = InMemoryModelsStore()
    provider = withPersonalizedCatalog(_builtinProvider(), personalize)

    # pi versions before per-key lists stored the plain pi.dev catalog here, and saved an empty list
    # after a missing pi.dev catalog or a failed first request. Only such an entry can be empty.
    await store.write(provider.id, ModelsStoreEntry(models=[], checkedAt=1))
    await refreshProvider(provider, store, allowNetwork=False)
    assert _ids(provider.getModels()) == ["static"]

    # A key that may use no chat models keeps its image models and shows no chat models.
    await store.write(provider.id, ModelsStoreEntry(models=[_imageModel], checkedAt=1))
    await refreshProvider(provider, store, allowNetwork=False)
    assert provider.getModels() == []
    assert _ids(provider.getAllModels()) == ["image"]


async def test_without_a_catalog_endpoint_the_built_in_catalog_is_personalized_and_saved(monkeypatch):
    # MISAKA fork: catalogBaseUrl None (ModelRuntime's default) never asks pi.dev.
    calls = _queue(monkeypatch, [])
    seen: list[list[str]] = []

    async def personalize(catalog, context):
        seen.append(_ids(catalog))
        assert context.credential is not None
        return [entry for entry in catalog if entry.id == "static"]

    store = InMemoryModelsStore()
    provider = withPersonalizedCatalog(_builtinProvider(), personalize)
    await refreshProvider(provider, store)

    assert calls == [] and seen == [["static"]]
    stored = await store.read(provider.id)
    assert _ids(stored.models) == ["static"] and isinstance(stored.checkedAt, int)
    assert stored.lastModified is None and stored.etag is None
    assert _ids(provider.getModels()) == ["static"]


async def test_a_newer_pi_dev_catalog_is_merged_before_personalization(monkeypatch):
    calls = _queue(monkeypatch, [_json({"dynamic": model("dynamic")})])
    seen: list[list[str]] = []

    async def personalize(catalog, _context):
        seen.append(_ids(catalog))
        return list(catalog)

    store = InMemoryModelsStore()
    provider = withPersonalizedCatalog(_builtinProvider(), personalize, "https://catalog.example.test")
    await refreshProvider(provider, store)

    assert len(calls) == 1 and "if-none-match" not in calls[0][1]
    assert seen == [["static", "dynamic"]]
    assert _ids(provider.getModels()) == ["static", "dynamic"]


async def test_model_runtime_narrows_openrouter_to_the_keys_list_and_keeps_it_offline(tmp_path, monkeypatch):
    """MISAKA fork: the only request is OpenRouter's /models/user, with the user's key and models.json's base URL;
    the list lands in the runtime's models store and a later offline runtime (a daemon card) reads it."""
    from misaka.ai.providers import openrouter
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime, personalizedCatalogHint
    from misaka.core.models_store import FileModelsStore

    monkeypatch.delenv("MISAKA_OFFLINE", raising=False)
    builtin = [entry.id for entry in openrouter.openrouterProvider().getModels()]
    allowed = builtin[:2]
    requests: list[tuple[str, dict]] = []

    async def userModels(url, init):
        requests.append((url, init))
        return _json({"data": [{"id": modelId} for modelId in allowed]})

    async def piDev(*_args, **_kwargs):
        raise AssertionError("no pi.dev catalog request")

    monkeypatch.setattr(openrouter, "fetch", userModels)
    monkeypatch.setattr(management_http, "fetch", piDev)
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(json.dumps({"providers": {"openrouter": {"baseUrl": "https://proxy.example/api/v1"}}}))
    storePath = tmp_path / "state" / "models-store.json"
    auth = AuthStorage.inMemory({"openrouter": {"type": "api_key", "key": "sk-or-test"}})

    runtime = await ModelRuntime.create({
        "authStorage": auth, "modelsPath": str(modelsPath), "modelsStorePath": str(storePath),
        "allowModelNetwork": True,
    })

    assert [url for url, _init in requests] == ["https://proxy.example/api/v1/models/user"]
    assert requests[0][1]["headers"]["authorization"] == "Bearer sk-or-test"
    assert [entry.id for entry in runtime.getModels() if entry.provider == "openrouter"] == allowed
    stored = await FileModelsStore(str(storePath)).read("openrouter")
    assert [entry.id for entry in stored.models if entry.type == "chat"] == allowed

    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    offline = await ModelRuntime.create({
        "authStorage": auth, "modelsPath": str(modelsPath), "modelsStorePath": str(storePath),
        "allowModelNetwork": True,
    })
    assert len(requests) == 1
    assert [entry.id for entry in offline.getModels() if entry.provider == "openrouter"] == allowed
    assert offline.getModel("openrouter", builtin[-1]) is None
    assert "OpenRouter API key" in personalizedCatalogHint("openrouter")
    assert personalizedCatalogHint("anthropic") == ""


def test_without_a_catalog_endpoint_there_is_no_remote_overlay():
    # MISAKA fork: no default endpoint (pi: https://pi.dev), so nothing can fall back to pi's service. An embedder
    # that runs a catalog server still passes its URL as `catalogBaseUrl`.
    from misaka.core.remote_catalog_provider import DEFAULT_CATALOG_BASE_URL

    builtin = _builtinProvider()

    assert DEFAULT_CATALOG_BASE_URL is None
    assert withRemoteCatalog(builtin) is builtin
    assert withRemoteCatalog(builtin, None, 0) is builtin
    assert withRemoteCatalog(builtin, "https://catalog.example.test") is not builtin
