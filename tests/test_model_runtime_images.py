"""pi's ``model-runtime-images.test.ts`` and ``model-runtime-classifiers.test.ts``
(``misaka/core/model_runtime.py``)."""

from __future__ import annotations

import json

import httpx
import pytest

from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.models import CreateProviderOptions, createProvider, isModelType
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.types import (
    AssistantImages,
    ClassifierModel,
    ClassifierResult,
    DeferredHandle,
    ImageModel,
    Model,
)
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime
from misaka.utils.js import AbortController

COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def imageModel(provider: str, id: str) -> ImageModel:
    return ImageModel.model_validate({
        "type": "image", "id": id, "name": id, "api": "test-images", "provider": provider,
        "baseUrl": "https://images.test/v1", "input": ["text"], "output": ["image"], "cost": COST,
    })


def classifierModel(provider: str, id: str) -> ClassifierModel:
    return ClassifierModel.model_validate({
        "type": "classifier", "id": id, "name": id, "api": "test-classifier", "provider": provider,
        "baseUrl": "https://classifier.test/v1", "input": ["text"], "cost": COST, "contextWindow": 1000,
    })


CONTEXT = {"input": [{"type": "text", "text": "a red circle"}]}
CLASSIFIER_CONTEXT = {
    "state": {"text": "Looks good"},
    "questions": {
        "approved": {"type": "bool", "instructions": "Does this express approval?",
                     "criteria": {"true": "Approval", "false": "No approval"}},
    },
}


def okImageResult(model) -> AssistantImages:
    return AssistantImages.model_validate({
        "api": model.api, "provider": model.provider, "model": model.id,
        "output": [{"type": "image", "data": "aGk=", "mimeType": "image/png"}], "stopReason": "stop", "timestamp": 0,
    })


class _Images:
    def __init__(self, observe=None):
        self.observe = observe

    async def generateImages(self, model, _context, options=None):
        if self.observe is not None:
            self.observe(model, options)
        return okImageResult(model)


async def _stored_key_resolve(*, ctx, credential, signal):
    return AuthResult(auth=ModelAuth(apiKey=credential.key), source="stored") if credential and credential.key else None


async def _open_resolve(*, ctx, credential, signal):
    return AuthResult(auth=ModelAuth())


async def createRuntime(tmp_path, modelsJson: dict | None = None) -> ModelRuntime:
    modelsPath = None
    if modelsJson is not None:
        modelsPath = str(tmp_path / "models.json")
        (tmp_path / "models.json").write_text(json.dumps(modelsJson), encoding="utf-8")
    return await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": modelsPath, "allowModelNetwork": False})


async def test_lists_built_in_openrouter_image_models_separately_from_chat_models(tmp_path):
    runtime = await createRuntime(tmp_path)
    images = runtime.getModelsOfType("image", "openrouter")
    assert images
    assert all(isModelType(model, "chat") for model in runtime.getModels("openrouter"))
    assert runtime.getModelOfType("image", "openrouter", images[0].id) is images[0]
    assert runtime.getModel("openrouter", "google/gemini-3-pro-image").api == "openai-completions"
    assert runtime.getModelOfType("image", "openrouter", "google/gemini-3-pro-image").type == "image"
    classifiers = runtime.getModelsOfType("classifier", "openrouter")
    assert len(runtime.getAllModels("openrouter")) == len(runtime.getModels("openrouter")) + len(images) + len(classifiers)


async def test_extension_model_lists_replace_undeclared_models_of_every_operation(tmp_path):
    runtime = await createRuntime(tmp_path)
    chat = Model.model_validate({
        "id": "built-in-chat", "name": "Built-in chat", "api": "test-chat", "provider": "mixed",
        "baseUrl": "https://built-in.test/v1", "reasoning": False, "input": ["text"], "cost": COST,
        "contextWindow": 1000, "maxTokens": 100,
    })
    runtime.registerNativeProvider(createProvider(CreateProviderOptions(
        id="mixed",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Mixed key", resolve=_open_resolve)),
        models=[chat, imageModel("mixed", "built-in-image"), classifierModel("mixed", "built-in-classifier")],
        images={"test-images": _Images()},
    )))

    runtime.registerProvider("mixed", {
        "apiKey": "extension-secret",
        "models": [{**chat.model_dump(exclude_none=True), "id": "extension-chat", "name": "Extension chat",
                    "baseUrl": "https://chat-proxy.test/v1"}],
    })

    assert [(model.type or "chat", model.id) for model in runtime.getAllModels("mixed")] == [("chat", "extension-chat")]
    assert runtime.getModel("mixed", "extension-chat").baseUrl == "https://chat-proxy.test/v1"
    assert runtime.getModelsOfType("image", "mixed") == []
    assert runtime.getModelsOfType("classifier", "mixed") == []


async def test_registers_extension_image_and_classifier_models_with_their_implementations(tmp_path):
    runtime = await createRuntime(tmp_path)
    observed: list = []

    class Classifier:
        async def classify(self, model, _context, options=None):
            observed.append({"apiKey": options.apiKey, "headers": options.headers})
            return ClassifierResult.model_validate({
                "api": model.api, "provider": model.provider, "model": model.id,
                "answers": {"approved": {"type": "bool", "probability": 0.9}}, "stopReason": "stop", "timestamp": 0,
            })

    runtime.registerProvider("extension-operations", {
        "apiKey": "extension-secret",
        "models": [
            {**imageModel("ignored", "shared").model_dump(exclude_none=True), "headers": {"X-Operation": "image"}},
            {**classifierModel("ignored", "shared").model_dump(exclude_none=True), "headers": {"X-Operation": "classifier"}},
        ],
        "images": {"test-images": _Images(lambda _model, options: observed.append(
            {"apiKey": options.apiKey, "headers": options.headers}))},
        "classifiers": {"test-classifier": Classifier()},
    })

    image = runtime.getModelOfType("image", "extension-operations", "shared")
    classifier = runtime.getModelOfType("classifier", "extension-operations", "shared")
    assert (await runtime.generateImages(image, CONTEXT)).stopReason == "stop"
    assert (await runtime.classify(classifier, CLASSIFIER_CONTEXT)).stopReason == "stop"
    assert observed == [
        {"apiKey": "extension-secret", "headers": {"X-Operation": "image"}},
        {"apiKey": "extension-secret", "headers": {"X-Operation": "classifier"}},
    ]


async def test_generates_images_through_a_native_provider_with_runtime_resolved_auth(tmp_path):
    runtime = await createRuntime(tmp_path)
    calls: list = []
    runtime.registerNativeProvider(createProvider(CreateProviderOptions(
        id="pixels",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Pixels key", resolve=_stored_key_resolve)),
        models=[imageModel("pixels", "flux")],
        images={"test-images": _Images(lambda model, options: calls.append((model, options)))},
    )))
    model = runtime.getModelOfType("image", "pixels", "flux")

    unconfigured = await runtime.generateImages(model, CONTEXT)
    assert unconfigured.stopReason == "error" and "not configured" in unconfigured.errorMessage
    assert calls == []

    controller = AbortController()
    controller.abort()
    cancelled = await runtime.generateImages(model, CONTEXT, {"signal": controller.signal})
    assert cancelled.stopReason == "aborted"
    assert calls == []

    await runtime.setRuntimeApiKey("pixels", "sk-pixels")
    assert [entry.id for entry in await runtime.getAvailableOfType("image", "pixels")] == ["flux"]
    result = await runtime.generateImages(model, CONTEXT)
    assert result.stopReason == "stop"
    assert calls[0][1].apiKey == "sk-pixels"


async def test_rejects_image_models_at_every_chat_entry_point_before_provider_dispatch(tmp_path):
    runtime = await createRuntime(tmp_path)
    chatDispatches = 0

    def rejectDispatch(*_args, **_kwargs):
        nonlocal chatDispatches
        chatDispatches += 1
        raise RuntimeError("image reached chat adapter")

    class Streams:
        stream = staticmethod(rejectDispatch)
        streamSimple = staticmethod(rejectDispatch)
        fetchDeferred = staticmethod(rejectDispatch)

        @staticmethod
        async def cancelDeferred(*args):
            rejectDispatch(*args)

    runtime.registerNativeProvider(createProvider(CreateProviderOptions(
        id="hybrid",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Hybrid key", resolve=_stored_key_resolve)),
        models=[imageModel("hybrid", "shared")],
        api=Streams(),
    )))
    await runtime.setRuntimeApiKey("hybrid", "sk-hybrid")
    image = runtime.getModelOfType("image", "hybrid", "shared")
    chatContext = {"messages": []}
    handle = DeferredHandle(provider="hybrid", modelId=image.id, api=image.api, id="response-1")

    results = [
        await runtime.stream(image, chatContext).result(),
        await runtime.complete(image, chatContext),
        await runtime.streamSimple(image, chatContext).result(),
        await runtime.completeSimple(image, chatContext),
        await runtime.streamDeferred(image, handle).result(),
        await runtime.fetchDeferred(image, handle),
    ]
    for result in results:
        assert result.stopReason == "error"
        assert "is not a chat model" in result.errorMessage
    with pytest.raises(Exception, match="is not a chat model"):
        await runtime.cancelDeferred(image, handle)
    assert chatDispatches == 0


async def test_keeps_image_generation_on_a_built_in_provider_composed_with_models_json_overrides(tmp_path):
    runtime = await createRuntime(tmp_path, {"providers": {"openrouter": {
        "headers": {"X-Title": "pi"},
        "modelOverrides": {
            "openrouter/auto": {"name": "Auto (renamed)"},
            "google/gemini-3-pro-image": {"headers": {"X-Chat-Only": "yes"}},
        },
    }}})
    provider = runtime.getProvider("openrouter")
    assert getattr(provider, "generateImages", None) is not None
    assert runtime.getModel("openrouter", "openrouter/auto").name == "Auto (renamed)"
    assert runtime.getModelsOfType("image", "openrouter")

    # Auth is provider-scoped: the same key serves chat and image models.
    await runtime.setRuntimeApiKey("openrouter", "sk-or")
    chat = runtime.getModel("openrouter", "google/gemini-3-pro-image")
    image = runtime.getModelOfType("image", "openrouter", "google/gemini-3-pro-image")
    chatAuth = await runtime.getAuth(chat)
    imageAuth = await runtime.getAuth(image)
    assert imageAuth.auth.apiKey == "sk-or"
    assert {"X-Title": "pi", "X-Chat-Only": "yes"}.items() <= chatAuth.auth.headers.items()
    assert imageAuth.auth.headers == {"X-Title": "pi"}


async def test_does_not_add_image_generation_or_classification_to_composed_chat_only_providers(tmp_path):
    runtime = await createRuntime(tmp_path, {"providers": {"anthropic": {"headers": {"X-Title": "pi"}}}})
    provider = runtime.getProvider("anthropic")
    assert getattr(provider, "generateImages", None) is None
    assert getattr(provider, "classify", None) is None
    assert getattr(runtime.getProvider("openrouter"), "generateImages", None) is not None
    assert getattr(runtime.getProvider("typesafe"), "classify", None) is not None


# --- model-runtime-classifiers.test.ts --------------------------------------------------------


async def test_lists_jev_separately_and_classifies_with_runtime_resolved_auth(tmp_path, monkeypatch):
    runtime = await createRuntime(tmp_path)
    jev = runtime.getModelOfType("classifier", "typesafe", "jev-latest")
    assert jev.type == "classifier"
    assert runtime.getModel("typesafe", "jev-latest") is None

    unconfigured = await runtime.classify(jev, CLASSIFIER_CONTEXT)
    assert unconfigured.stopReason == "error" and "not configured" in unconfigured.errorMessage

    await runtime.setRuntimeApiKey("typesafe", "sk-typesafe")
    assert await runtime.getAvailableOfType("classifier", "typesafe") == [jev]
    authorizations: list = []

    async def post(self, url, *, headers=None, content=None, **_kwargs):
        authorizations.append((headers or {}).get("authorization"))
        return httpx.Response(200, json={"answers": {"approved": {"type": "noul", "noul": 0.8}}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await runtime.classify(jev, CLASSIFIER_CONTEXT)

    assert authorizations == ["Bearer sk-typesafe"]
    assert result.answers["approved"].model_dump() == {"type": "bool", "probability": 0.8}
