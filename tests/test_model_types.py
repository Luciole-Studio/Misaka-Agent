"""pi-ai's ``model-types.test.ts``, ``images-models.test.ts`` and ``classifier-models.test.ts``:
one model surface for chat, image and classifier models (``misaka/ai/models_runtime.py``)."""

from __future__ import annotations

import json

import httpx
import pytest

from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.compat import getModels as getCompatModels
from misaka.ai.models import (
    CreateModelsOptions,
    CreateProviderOptions,
    ModelsRefreshOptions,
    createModels,
    createProvider,
    getModelType,
    hasApi,
    isModelType,
    modelsAreEqual,
)
from misaka.ai.models_store import InMemoryModelsStore, ModelsStoreEntry
from misaka.ai.providers.all import (
    builtinModels,
    getAllBuiltinModels,
    getBuiltinClassifierModel,
    getBuiltinClassifierModels,
    getBuiltinImageModel,
    getBuiltinImageModels,
    getBuiltinModel,
    getBuiltinModels,
)
from misaka.ai.types import (
    AssistantImages,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    ImageModel,
    ImagesContext,
    Model,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.utils.js import AbortController

COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def chatModel(provider: str, id: str) -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": "test-chat", "provider": provider, "baseUrl": "https://example.test/v1",
        "reasoning": False, "input": ["text"], "cost": COST, "contextWindow": 1000, "maxTokens": 100,
    })


def imageModel(provider: str, id: str) -> ImageModel:
    return ImageModel.model_validate({
        "type": "image", "id": id, "name": id, "api": "test-images", "provider": provider,
        "baseUrl": "https://example.test/v1", "input": ["text"], "output": ["image"], "cost": COST,
    })


def classifierModel(provider: str, id: str) -> ClassifierModel:
    return ClassifierModel.model_validate({
        "type": "classifier", "id": id, "name": id, "api": "test-classifier", "provider": provider,
        "baseUrl": "https://example.test/v1", "input": ["text"], "cost": COST, "contextWindow": 1000,
    })


class _Streams:
    def stream(self, *_args, **_kwargs):
        return AssistantMessageEventStream()

    def streamSimple(self, *_args, **_kwargs):
        return AssistantMessageEventStream()


async def _no_auth(*, ctx, credential, signal):
    return AuthResult(auth=ModelAuth())


NO_AUTH = ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_no_auth))


class _AuthContext:
    def __init__(self, env: dict[str, str]):
        self._env = env

    async def env(self, name):
        return self._env.get(name)

    async def fileExists(self, _path):
        return False


def _ok(model: ImageModel) -> AssistantImages:
    return AssistantImages.model_validate({
        "api": model.api, "provider": model.provider, "model": model.id,
        "output": [{"type": "image", "data": "aGk=", "mimeType": "image/png"}], "stopReason": "stop", "timestamp": 0,
    })


class _Images:
    def __init__(self, calls: list | None = None):
        self.calls = calls if calls is not None else []

    async def generateImages(self, model, _context, options=None):
        self.calls.append((model, options))
        return _ok(model)


def _testProvider(id: str, models=None, envVar: str | None = None, calls: list | None = None, images=None):
    implementation = _Images(calls)

    async def resolve(*, ctx, credential, signal):
        if envVar is None:
            return AuthResult(auth=ModelAuth())
        key = (credential.key if credential is not None else None) or await ctx.env(envVar)
        return AuthResult(auth=ModelAuth(apiKey=key), source="stored" if credential else envVar) if key else None

    return createProvider(CreateProviderOptions(
        id=id,
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test key", resolve=resolve)),
        models=models if models is not None else [imageModel(id, "model-a")],
        api={"test-chat": _Streams()},
        images={api: implementation for api in (images or ["test-images"])},
    ))


CONTEXT = ImagesContext.model_validate({"input": [{"type": "text", "text": "a red circle"}]})


# --- model-types.test.ts ----------------------------------------------------------------------


async def test_chat_models_without_a_type_work_through_a_handwritten_provider_without_get_all_models():
    model = chatModel("handwritten", "faux")

    class Handwritten:
        id = "handwritten"
        name = "Handwritten"
        auth = NO_AUTH

        def getModels(self):
            return [model]

        def stream(self, *_args):
            return AssistantMessageEventStream()

        def streamSimple(self, *_args):
            return AssistantMessageEventStream()

    models = createModels()
    models.setProvider(Handwritten())

    found = models.getModel("handwritten", "faux")
    assert found is not None and found.type is None
    assert getModelType(found) == "chat"
    assert hasApi(found, found.api)
    assert modelsAreEqual(found, found.model_copy(update={"type": "chat"}))
    assert not modelsAreEqual(found, imageModel(found.provider, found.id))
    assert models.getModelsOfType("chat", "handwritten") == [model]
    assert models.getAllModels("handwritten") == [model]
    assert models.getModelsOfType("image", "handwritten") == []


def test_chat_models_without_a_type_narrow_mixed_lists_with_is_model_type():
    mixed = [chatModel("p", "c"), chatModel("p", "typed").model_copy(update={"type": "chat"}), imageModel("p", "i")]
    assert [model.id for model in mixed if isModelType(model, "chat")] == ["c", "typed"]
    assert [model.id for model in mixed if isModelType(model, "image")] == ["i"]
    assert [model for model in mixed if isModelType(model, "classifier")] == []


def test_built_in_catalog_getters_return_the_model():
    assert getBuiltinModel("openai", "gpt-4o").id == "gpt-4o"


async def test_stored_and_fetched_models_of_unknown_types_are_dropped_instead_of_failing_the_refresh():
    modelsStore = InMemoryModelsStore()
    # A store written by a newer build; the models a newer build knows have no class here.
    stored = ModelsStoreEntry.model_construct(models=[
        chatModel("dyn", "stored-chat"),
        imageModel("dyn", "stored-image"),
        Model.model_construct(**{**chatModel("dyn", "future-embedding").__dict__, "type": "embedding"}),
        ImageModel.model_construct(**{**imageModel("dyn", "future-video").__dict__, "type": "video"}),
    ])
    modelsStore._entries["dyn"] = stored
    fetched: list = []

    async def fetchModels(_context):
        return fetched

    models = createModels(CreateModelsOptions(modelsStore=modelsStore))
    models.setProvider(createProvider(CreateProviderOptions(
        id="dyn", auth=NO_AUTH, models=[], fetchModels=fetchModels, api=_Streams(),
    )))

    restored = await models.refresh(ModelsRefreshOptions(providers=["dyn"], allowNetwork=False))
    assert restored.errors == {}
    assert [model.id for model in models.getAllModels("dyn")] == ["stored-chat", "stored-image"]

    fetched[:] = [
        chatModel("dyn", "fetched-chat"),
        ImageModel.model_construct(**{**imageModel("dyn", "fetched-video").__dict__, "type": "video"}),
    ]
    refreshed = await models.refresh(ModelsRefreshOptions(providers=["dyn"]))
    assert refreshed.errors == {}
    assert [model.id for model in models.getAllModels("dyn")] == ["fetched-chat"]
    assert [model.id for model in (await modelsStore.read("dyn")).models] == ["fetched-chat"]


# --- images-models.test.ts --------------------------------------------------------------------


def test_treats_models_without_a_type_as_chat_models():
    chat = chatModel("p", "c")
    image = imageModel("p", "i")

    assert chat.type is None
    assert getModelType(chat) == "chat"
    assert getModelType(chat.model_copy(update={"type": "chat"})) == "chat"
    assert getModelType(image) == "image"
    assert isModelType(chat, "chat") and not isModelType(chat, "image") and isModelType(image, "image")
    # hasApi never matches an image model, even on an equal api string
    assert not hasApi(image.model_copy(update={"api": "test-chat"}), "test-chat")
    assert hasApi(chat, "test-chat")


async def test_lists_models_without_a_type_as_chat_models_at_create_provider_boundaries():
    async def fetchModels(_context):
        return [chatModel("legacy", "dynamic")]

    provider = createProvider(CreateProviderOptions(
        id="legacy", auth=NO_AUTH, models=[chatModel("legacy", "static"), imageModel("legacy", "static")],
        fetchModels=fetchModels, api=_Streams(),
    ))
    models = createModels()
    models.setProvider(provider)

    assert [model.id for model in provider.getModels()] == ["static"]
    await models.refresh(ModelsRefreshOptions(providers=[provider.id]))
    assert [model.id for model in provider.getModels()] == ["static", "dynamic"]
    assert [model.type for model in provider.getModels()] == [None, None]
    assert [model.id for model in models.getModelsOfType("image", "legacy")] == ["static"]


def test_lists_chat_image_and_all_models_through_typed_accessors():
    models = createModels()
    models.setProvider(_testProvider("p1", models=[chatModel("p1", "c1"), imageModel("p1", "i1"), imageModel("p1", "i2")]))
    models.setProvider(_testProvider("p2", models=[imageModel("p2", "i3")]))

    assert [m.id for m in models.getModels()] == ["c1"]
    assert [m.id for m in models.getModelsOfType("chat")] == ["c1"]
    assert [m.id for m in models.getModelsOfType("image")] == ["i1", "i2", "i3"]
    assert [m.id for m in models.getModelsOfType("image", "p1")] == ["i1", "i2"]
    assert models.getModelsOfType("classifier") == []
    assert [m.id for m in models.getAllModels()] == ["c1", "i1", "i2", "i3"]

    assert models.getModel("p1", "c1").id == "c1"
    assert models.getModel("p1", "i1") is None
    assert models.getModelOfType("chat", "p1", "c1").id == "c1"
    assert models.getModelOfType("image", "p1", "i1").id == "i1"
    assert models.getModelOfType("image", "p1", "c1") is None


async def test_splits_available_models_by_type():
    models = createModels(CreateModelsOptions(authContext=_AuthContext({"KEY": "k"})))
    models.setProvider(_testProvider("p1", envVar="KEY", models=[chatModel("p1", "c1"), imageModel("p1", "i1")]))
    models.setProvider(_testProvider("p2", envVar="MISSING", models=[imageModel("p2", "i2")]))

    assert [m.id for m in await models.getAvailable()] == ["c1"]
    assert [m.id for m in await models.getAvailableOfType("chat")] == ["c1"]
    assert [m.id for m in await models.getAvailableOfType("image")] == ["i1"]
    assert [m.id for m in await models.getAllAvailable()] == ["c1", "i1"]


async def test_resolves_auth_through_the_provider_and_merges_it_into_image_requests_explicit_options_win():
    calls: list = []
    models = createModels(CreateModelsOptions(authContext=_AuthContext({"TEST_KEY": "env-key"})))
    models.setProvider(_testProvider("p1", envVar="TEST_KEY", calls=calls))
    model = models.getModelOfType("image", "p1", "model-a")

    from misaka.ai.auth.resolve import AuthResolutionOverrides

    assert (await models.getAuth(model)).auth.apiKey == "env-key"
    assert (await models.getAuth(model.provider)).auth.apiKey == "env-key"
    assert (await models.getAuth(model, AuthResolutionOverrides(apiKey="explicit-key"))).auth.apiKey == "explicit-key"

    result = await models.generateImages(model, CONTEXT)
    assert result.stopReason == "stop"
    assert calls[0][1].apiKey == "env-key"

    await models.generateImages(model, CONTEXT, {"apiKey": "explicit"})
    assert calls[1][1].apiKey == "explicit"


async def test_merges_provider_resolved_env_and_applies_header_transforms():
    calls: list = []

    async def resolve(*, ctx, credential, signal):
        return AuthResult(
            auth=ModelAuth(apiKey="provider-key", headers={"x-base": "1"}),
            env={"PROVIDER_ONLY": "provider", "SHARED": "provider"},
        )

    models = createModels()
    models.setProvider(createProvider(CreateProviderOptions(
        id="p1", auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test key", resolve=resolve)),
        models=[imageModel("p1", "model-a")], images={"test-images": _Images(calls)},
    )))
    model = models.getModelOfType("image", "p1", "model-a")

    await models.generateImages(model, CONTEXT, {
        "apiKey": "request-key",
        "env": {"REQUEST_ONLY": "request", "SHARED": "request"},
        "transformHeaders": lambda headers: {**headers, "x-extra": "2"},
    })

    options = calls[0][1]
    assert options.apiKey == "request-key"
    assert options.env == {"PROVIDER_ONLY": "provider", "REQUEST_ONLY": "request", "SHARED": "request"}
    assert options.headers == {"x-base": "1", "x-extra": "2"}


async def test_returns_error_results_instead_of_rejecting():
    models = createModels(CreateModelsOptions(authContext=_AuthContext({})))

    ghost = await models.generateImages(imageModel("ghost", "m"), CONTEXT)
    assert ghost.stopReason == "error" and "Unknown provider: ghost" in ghost.errorMessage

    # Unconfigured auth is an error, matching stream().
    calls: list = []
    models.setProvider(_testProvider("p1", envVar="MISSING", calls=calls))
    model = models.getModelOfType("image", "p1", "model-a")
    assert await models.getAuth(model) is None
    unconfigured = await models.generateImages(model, CONTEXT)
    assert unconfigured.stopReason == "error" and "not configured" in unconfigured.errorMessage
    assert calls == []

    controller = AbortController()
    controller.abort()
    cancelled = await models.generateImages(model, CONTEXT, {"signal": controller.signal})
    assert cancelled.stopReason == "aborted"
    assert calls == []

    # A provider without any images implementation rejects image models it lists.
    models.setProvider(createProvider(CreateProviderOptions(
        id="chat-only", auth=NO_AUTH, models=[imageModel("chat-only", "i")], api=_Streams(),
    )))
    unsupported = await models.generateImages(models.getModelOfType("image", "chat-only", "i"), CONTEXT)
    assert unsupported.stopReason == "error" and "does not support image generation" in unsupported.errorMessage

    # An images map without the model's api yields a provider error result.
    models.setProvider(_testProvider("wrong-api", models=[imageModel("wrong-api", "i")], images=["other-images"]))
    missingApi = await models.generateImages(models.getModelOfType("image", "wrong-api", "i"), CONTEXT)
    assert missingApi.stopReason == "error"
    assert 'no image generation implementation for "test-images"' in missingApi.errorMessage


async def test_rejects_chat_models_at_the_image_entry_point_at_runtime():
    models = createModels()
    chat = chatModel("p1", "chat")
    models.setProvider(_testProvider("p1", models=[chat], images=["test-chat"]))

    result = await models.generateImages(chat, CONTEXT)
    assert result.stopReason == "error" and "is not an image model" in result.errorMessage


async def test_rejects_image_models_at_the_stream_entry_points_at_runtime():
    models = createModels()
    models.setProvider(_testProvider("p1"))
    image = models.getModelOfType("image", "p1", "model-a")

    result = await models.streamSimple(image, {"messages": []}).result()
    assert result.stopReason == "error" and "is not a chat model" in result.errorMessage


@pytest.mark.parametrize("implementations", [{}, {"api": {}}, {"images": {}}, {"classifiers": {}}])
def test_requires_at_least_one_concrete_operation_implementation(implementations):
    with pytest.raises(RuntimeError, match='at least one of "api", "images", or "classifiers"'):
        createProvider(CreateProviderOptions(id="empty", auth=NO_AUTH, models=[], **implementations))


async def test_supports_dynamic_providers_listing_image_models_via_refresh():
    fetches = 0

    async def fetchModels(_context):
        nonlocal fetches
        fetches += 1
        return [imageModel("dyn", "listed"), chatModel("dyn", "chat")]

    modelsStore = InMemoryModelsStore()
    models = createModels(CreateModelsOptions(modelsStore=modelsStore))
    models.setProvider(createProvider(CreateProviderOptions(
        id="dyn", auth=NO_AUTH, models=[], fetchModels=fetchModels, images={"test-images": _Images()},
    )))

    assert models.getAllModels("dyn") == []
    result = await models.refresh(ModelsRefreshOptions(providers=["dyn"]))
    assert result.errors == {}
    assert fetches == 1
    assert models.getModelOfType("image", "dyn", "listed") is not None
    assert models.getModel("dyn", "chat") is not None
    assert [model.id for model in (await modelsStore.read("dyn")).models] == ["listed", "chat"]


def test_keeps_existing_built_in_and_compat_model_reads_chat_only():
    chat = getBuiltinModels("openrouter")
    images = getBuiltinImageModels("openrouter")
    everything = getAllBuiltinModels("openrouter")

    assert all(isModelType(model, "chat") for model in chat)
    assert all(isModelType(model, "image") for model in images)
    assert any(isModelType(model, "image") for model in everything)
    assert getCompatModels("openrouter") == chat
    assert all(model.contextWindow > 0 for model in chat)
    assert len(chat) + len(images) + len(getBuiltinClassifierModels("openrouter")) == len(everything)
    assert getBuiltinImageModel("openrouter", "black-forest-labs/flux.2-pro").type == "image"


async def test_builtin_models_exposes_openrouter_image_models_under_the_openrouter_provider():
    models = builtinModels(CreateModelsOptions(authContext=_AuthContext({"OPENROUTER_API_KEY": "or-key"})))
    provider = models.getProvider("openrouter")
    images = models.getModelsOfType("image", "openrouter")
    assert images
    assert all(isModelType(model, "chat") for model in provider.getModels())
    assert any(isModelType(model, "image") for model in provider.getAllModels())
    assert all(m.type == "image" and m.api == "openrouter-images" for m in images)
    assert all(m.provider == "openrouter" for m in models.getModelsOfType("image"))

    # One upstream id can expose separate chat and image operations.
    chat = models.getModel("openrouter", "google/gemini-3-pro-image")
    image = models.getModelOfType("image", "openrouter", "google/gemini-3-pro-image")
    assert chat.api == "openai-completions"
    assert image.api == "openrouter-images"

    # One credential covers both.
    assert (await models.getAuth(images[0])).auth.apiKey == "or-key"
    assert (await models.getAuth(chat)).auth.apiKey == "or-key"
    assert provider.generateImages is not None


# --- classifier-models.test.ts ----------------------------------------------------------------

CLASSIFIER_CONTEXT = ClassifierContext.model_validate({
    "state": {"text": "yes"},
    "questions": {
        "approved": {
            "type": "bool",
            "instructions": "Does this express approval?",
            "criteria": {"true": "Approval", "false": "No approval"},
        },
    },
})


async def test_keeps_chat_and_classifier_entries_with_the_same_provider_and_id_separate():
    chat = chatModel("test", "shared")
    classifier = classifierModel("test", "shared")

    class Classifier:
        async def classify(self, model, _context, _options=None):
            return ClassifierResult.model_validate({
                "api": model.api, "provider": model.provider, "model": model.id,
                "answers": {"approved": {"type": "bool", "probability": 0.9}}, "stopReason": "stop", "timestamp": 0,
            })

    models = createModels()
    models.setProvider(createProvider(CreateProviderOptions(
        id="test", auth=NO_AUTH, models=[chat, classifier], api={"test-chat": _Streams()},
        classifiers={"test-classifier": Classifier()},
    )))

    assert getModelType(models.getModel("test", "shared")) == "chat"
    assert models.getModelOfType("classifier", "test", "shared").type == "classifier"
    assert models.getModelsOfType("classifier") == [classifier]
    assert len(models.getAllModels()) == 2
    assert await models.getAvailableOfType("classifier") == [classifier]
    answer = (await models.classify(classifier, CLASSIFIER_CONTEXT)).answers["approved"]
    assert answer.model_dump() == {"type": "bool", "probability": 0.9}


async def test_rejects_chat_models_at_the_classifier_entry_point_at_runtime():
    chat = chatModel("test", "chat")
    models = createModels()
    models.setProvider(createProvider(CreateProviderOptions(id="test", auth=NO_AUTH, models=[chat], api=_Streams())))

    result = await models.classify(chat, CLASSIFIER_CONTEXT)
    assert result.stopReason == "error" and "is not a classifier model" in result.errorMessage


def test_exposes_jev_only_through_classifier_catalog_accessors():
    jev = getBuiltinClassifierModel("typesafe", "jev-latest")
    assert (jev.type, jev.api, jev.provider, jev.contextWindow) == ("classifier", "typesafe-system-one", "typesafe", 64000)
    assert getBuiltinClassifierModels("typesafe") == [jev]
    assert getAllBuiltinModels("typesafe") == [jev]

    models = builtinModels()
    assert models.getModel("typesafe", "jev-latest") is None
    assert models.getModelOfType("classifier", "typesafe", "jev-latest") == jev


def _capture_posts(monkeypatch, reply: dict) -> list:
    requests: list = []

    async def post(self, url, *, headers=None, content=None, **_kwargs):
        requests.append({"url": str(url), "body": json.loads(content), "authorization": (headers or {}).get("authorization")})
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return requests


@pytest.mark.parametrize(("provider", "id", "url"), [
    ("vercel-ai-gateway", "typesafe-ai/jev", "https://ai-gateway.vercel.sh/typesafe/v1/systemone"),
    ("opencode", "jev-1.13", "https://opencode.ai/inference/v1/systemone"),
    ("opencode", "jev-1.13-free", "https://opencode.ai/inference/v1/systemone"),
])
async def test_routes_jev_to_its_typesafe_compatible_endpoint(monkeypatch, provider, id, url):
    models = builtinModels()
    jev = models.getModelOfType("classifier", provider, id)
    assert (jev.api, jev.contextWindow) == ("typesafe-system-one", 32000)
    assert models.getModel(provider, id) is None
    requests = _capture_posts(monkeypatch, {"model": id, "answers": {"approved": {"type": "noul", "noul": 0.8}}})

    result = await models.classify(jev, CLASSIFIER_CONTEXT, {"apiKey": "secret"})

    assert [(request["url"], request["body"]["model"], request["body"]["state"], request["authorization"])
            for request in requests] == [(url, id, CLASSIFIER_CONTEXT.state, "Bearer secret")]
    assert result.stopReason == "stop"
    assert result.answers["approved"].model_dump() == {"type": "bool", "probability": 0.8}


async def test_rejects_images_for_classifier_models_without_image_input_before_calling_the_provider():
    classifier = classifierModel("test", "text-only")
    calls: list[str] = []

    class Classifier:
        async def classify(self, model, _context, _options=None):
            calls.append(model.id)
            return ClassifierResult.model_validate({
                "api": classifier.api, "provider": classifier.provider, "model": classifier.id,
                "answers": {}, "stopReason": "stop", "timestamp": 0,
            })

    models = createModels()
    models.setProvider(createProvider(CreateProviderOptions(
        id="test", auth=NO_AUTH, models=[classifier], classifiers={"test-classifier": Classifier()},
    )))

    image = {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
    result = await models.classify(
        classifier, ClassifierContext.model_validate({**CLASSIFIER_CONTEXT.model_dump(), "images": [image]})
    )
    withoutImages = await models.classify(
        classifier, ClassifierContext.model_validate({**CLASSIFIER_CONTEXT.model_dump(), "images": []})
    )

    assert result.stopReason == "error"
    assert result.errorMessage == "Model test/text-only does not accept image input"
    assert withoutImages.stopReason == "stop"
    assert calls == ["text-only"]


async def test_routes_openai_gpt_6_luna_through_the_decisions_api_with_images(monkeypatch):
    models = builtinModels()
    luna = models.getModelOfType("classifier", "openai", "gpt-6-luna")
    assert luna is not None, "missing OpenAI Decisions model"
    assert (luna.api, luna.input, luna.contextWindow) == ("openai-decisions", ["text", "image"], 922000)
    # The chat entry with the same id stays separate.
    assert models.getModel("openai", "gpt-6-luna").api == "openai-responses"

    requests = _capture_posts(monkeypatch, {"answers": [{"type": "predicate", "name": "approved", "probability": 0.8}]})
    image = {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
    result = await models.classify(
        luna,
        ClassifierContext.model_validate({**CLASSIFIER_CONTEXT.model_dump(), "images": [image]}),
        {"apiKey": "secret"},
    )

    assert [request["url"] for request in requests] == ["https://api.openai.com/v1/decisions"]
    assert result.stopReason == "stop"
    assert result.answers["approved"].model_dump() == {"type": "bool", "probability": 0.8}


async def test_lists_openai_decisions_models_only_for_api_key_credentials():
    import time

    from misaka.ai.auth.credential_store import InMemoryCredentialStore
    from misaka.ai.auth.types import ApiKeyCredential, OAuthCredential

    async def store(credential):
        credentials = InMemoryCredentialStore()

        async def write(_current):
            return credential

        await credentials.modify("openai", write)
        return credentials

    apiKeyStore = await store(ApiKeyCredential(type="api_key", key="secret"))
    oauthStore = await store(
        OAuthCredential(type="oauth", access="access", refresh="refresh", expires=int(time.time() * 1000) + 3_600_000)
    )

    withApiKey = builtinModels(CreateModelsOptions(credentials=apiKeyStore))
    withOAuth = builtinModels(CreateModelsOptions(credentials=oauthStore))

    assert [model.id for model in await withApiKey.getAvailableOfType("classifier", "openai")] == ["gpt-6-luna"]
    assert await withOAuth.getAvailableOfType("classifier", "openai") == []
    # Chat models stay available with ChatGPT OAuth.
    assert any(model.id == "gpt-6-luna" for model in await withOAuth.getAvailable("openai"))


def test_routes_openrouter_classifier_models_through_the_system_one_api():
    models = builtinModels()
    for model in getBuiltinClassifierModels("openrouter"):
        assert (model.api, model.baseUrl) == ("typesafe-system-one", "https://openrouter.ai/api/v1")
        assert models.getModel("openrouter", model.id) is None
        assert models.getModelOfType("classifier", "openrouter", model.id) == model


# --- models-runtime.test.ts: keeps chat reads independent from the all-model catalog -----------


async def test_keeps_chat_reads_independent_from_the_all_model_catalog():
    provider = _testProvider("chat-only", models=[chatModel("chat-only", "model-a")])

    def broken():
        raise RuntimeError("all models unavailable")

    provider.getAllModels = broken
    models = createModels()
    models.setProvider(provider)

    assert [model.id for model in models.getModels("chat-only")] == ["model-a"]
    assert models.getModel("chat-only", "model-a").id == "model-a"
    assert [model.id for model in await models.getAvailable("chat-only")] == ["model-a"]
    assert models.getAllModels("chat-only") == []
