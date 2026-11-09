"""pi-ai's ``openrouter-images.test.ts`` plus the image cases of ``openrouter-oauth.test.ts``,
``telemetry-options.test.ts`` and ``provider-error-body-passthrough.test.ts``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import openai
import pytest
from openai.resources.chat.completions import AsyncCompletions

from misaka.ai.api import _openai_sdk
from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.types import (
    ApiKeyAuth,
    AuthResult,
    ModelAuth,
    OAuthCredential,
    ProviderAuth,
)
from misaka.ai.images import generateImages
from misaka.ai.images_api_registry import ImagesApiProvider, registerImagesApiProvider
from misaka.ai.models import (
    CreateModelsOptions,
    CreateProviderOptions,
    createModels,
    createProvider,
)
from misaka.ai.providers.openrouter import openrouterProvider
from misaka.ai.types import (
    AssistantImages,
    ImageModel,
    ImagesContext,
    ProviderImagesOptions,
)
from misaka.utils.js import AbortController

COST = {"input": 0.015, "output": 0.03, "cacheRead": 0, "cacheWrite": 0}
CONTEXT = ImagesContext.model_validate({"input": [{"type": "text", "text": "Generate a dog"}]})


def _model(id: str, output: list[str], **extra) -> ImageModel:
    return ImageModel.model_validate({
        "type": "image", "id": id, "name": id, "api": "openrouter-images", "provider": "openrouter",
        "baseUrl": "https://openrouter.ai/api/v1", "input": ["text", "image"], "output": output, "cost": COST, **extra,
    })


class _Fake:
    """The OpenAI SDK as the images module drives it."""

    def __init__(self, fail: Exception | None = None, usage_details=None, choices=True, hold=None):
        self.params = None
        self.options: dict = {}
        self.clients: list[dict] = []
        self.fail = fail
        self.usage_details = usage_details
        self.choices = choices
        self.hold = hold
        self.cancelled = False

    def factory(self, **kwargs):
        self.clients.append(kwargs)
        fake = self

        class Completions:
            # The SDK's signature: split_sdk_params reads it to route undeclared keys.
            create = AsyncCompletions.create

            class with_raw_response:
                @staticmethod
                async def create(**params):
                    fake.params = params
                    if fake.hold is not None:
                        try:
                            await fake.hold.wait()
                        except asyncio.CancelledError:
                            fake.cancelled = True
                            raise
                    if fake.fail is not None:
                        raise fake.fail
                    response = type("Response", (), {
                        "id": "img-1",
                        "usage": type("Usage", (), {
                            "prompt_tokens": 12, "completion_tokens": 34,
                            "prompt_tokens_details": fake.usage_details if fake.usage_details is not None
                            else type("Details", (), {"cached_tokens": 0, "cache_write_tokens": 0})(),
                        })(),
                        "choices": [type("Choice", (), {"message": type("Message", (), {
                            "content": "Here is your image.",
                            "images": [{"image_url": "data:image/png;base64,ZmFrZS1wbmc="}],
                        })()})()] if fake.choices else None,
                    })()
                    return type("Raw", (), {
                        "parse": lambda self: response,
                        "http_response": httpx.Response(200),
                    })()

        def with_options(self, **options):
            # Per-request SDK options (`max_retries`, `timeout`), as `_openai_sdk._create_with_response` sets them.
            fake.options = options
            return self

        return type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})(), "with_options": with_options})()


@pytest.fixture
def fake(monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(_openai_sdk, "AsyncOpenAI", fake.factory)
    return fake


async def test_returns_text_plus_images_in_final_output(fake):
    model = _model("google/gemini-3.1-flash-image-preview", ["text", "image"], headers={"HTTP-Referer": "https://example.com"})

    output = await generateImages(model, CONTEXT, ProviderImagesOptions(apiKey="test"))

    assert output.stopReason == "stop"
    assert output.responseId == "img-1"
    assert (output.output[0].type, output.output[0].text) == ("text", "Here is your image.")
    assert (output.output[1].type, output.output[1].mimeType, output.output[1].data) == ("image", "image/png", "ZmFrZS1wbmc=")
    assert fake.params["stream"] is False
    assert fake.params["modalities"] == ["image", "text"]
    assert fake.params["messages"][0]["content"][0] == {"type": "text", "text": "Generate a dog"}
    # The SDK's own retries are off; retryProviderRequest retries.
    assert fake.options["max_retries"] == 0
    assert fake.clients[0]["default_headers"] == {"HTTP-Referer": "https://example.com"}


async def test_passes_through_abort_signal_and_returns_aborted_result(fake):
    controller = AbortController()
    controller.abort()

    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                                   ProviderImagesOptions(apiKey="test", signal=controller.signal))

    assert output.stopReason == "aborted"
    assert output.errorMessage == "Request aborted"


async def test_generate_images_resolves_the_final_assistant_images_result(fake):
    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                                   ProviderImagesOptions(apiKey="test"))

    assert any(item.type == "image" for item in output.output)
    # Image-only models must not request text output.
    assert fake.params["modalities"] == ["image"]


async def test_the_bare_path_needs_the_api_key_passed_in(fake):
    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT)

    assert output.stopReason == "error"
    assert output.errorMessage == "No API key for provider: openrouter"


# --- MISAKA regressions (B4 review) ---------------------------------------------------------


async def test_usage_details_without_cache_fields_do_not_fail_a_billed_generation(monkeypatch):
    @dataclass
    class Details:  # OpenRouter left both cache counters out
        cached_tokens: int | None = None
        cache_write_tokens: int | None = None

    fake = _Fake(usage_details=Details())
    monkeypatch.setattr(_openai_sdk, "AsyncOpenAI", fake.factory)

    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                                   ProviderImagesOptions(apiKey="test"))

    assert output.stopReason == "stop"
    assert (output.usage.cacheRead, output.usage.cacheWrite, output.usage.input) == (0, 0, 12)


async def test_payload_keys_the_sdk_does_not_declare_travel_in_extra_body(fake):
    def onPayload(params, _model):
        return {**params, "provider": {"order": ["x"]}}

    await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                          ProviderImagesOptions(apiKey="test", onPayload=onPayload))

    assert fake.params["extra_body"] == {"provider": {"order": ["x"]}}
    assert fake.params["modalities"] == ["image"]


async def test_the_sdk_timeout_is_left_alone_unless_timeout_ms_is_given(fake):
    await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT, ProviderImagesOptions(apiKey="test"))
    assert "timeout" not in fake.options

    await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                          ProviderImagesOptions(apiKey="test", timeoutMs=2500))
    assert fake.options["timeout"] == 2.5


async def test_a_flag_only_signal_aborts_the_request_in_flight(monkeypatch):
    fake = _Fake(hold=asyncio.Event())
    monkeypatch.setattr(_openai_sdk, "AsyncOpenAI", fake.factory)

    class FlagSignal:  # the duck-typed shape with no wait()
        aborted = False

    signal = FlagSignal()
    task = asyncio.ensure_future(generateImages(
        _model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT, ProviderImagesOptions(apiKey="test", signal=signal)
    ))
    await asyncio.sleep(0.05)
    signal.aborted = True
    output = await asyncio.wait_for(task, 5)

    assert output.stopReason == "aborted"
    assert fake.cancelled


async def test_a_response_without_choices_is_an_error_not_an_empty_success(monkeypatch):
    fake = _Fake(choices=False)
    monkeypatch.setattr(_openai_sdk, "AsyncOpenAI", fake.factory)

    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                                   ProviderImagesOptions(apiKey="test"))

    assert output.stopReason == "error"


# --- provider-error-body-passthrough.test.ts: openrouter images -------------------------------


async def test_surfaces_the_http_body_reason_instead_of_the_opaque_sdk_message(monkeypatch):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    fake = _Fake(fail=openai.PermissionDeniedError(
        "403 status code (no body)",
        response=httpx.Response(403, text="blocked by gateway WAF", request=request),
        body="blocked by gateway WAF",
    ))
    monkeypatch.setattr(_openai_sdk, "AsyncOpenAI", fake.factory)

    output = await generateImages(_model("black-forest-labs/flux.2-pro", ["image"]), CONTEXT,
                                   ProviderImagesOptions(apiKey="test"))

    assert output.stopReason == "error"
    assert "403" in output.errorMessage
    assert "blocked by gateway WAF" in output.errorMessage
    assert output.errorMessage != "403 status code (no body)"


# --- telemetry-options.test.ts: survives direct and Models image dispatch ---------------------


async def test_telemetry_context_survives_direct_and_models_image_dispatch():
    model = ImageModel.model_validate({
        "type": "image", "id": "telemetry-image", "name": "telemetry-image", "api": "telemetry-images",
        "provider": "telemetry", "baseUrl": "https://example.test", "input": ["text"], "output": ["image"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    })
    telemetryContext = object()
    observed: list = []

    async def generate(requestModel, _context, options=None):
        observed.append(options.telemetryContext)
        return AssistantImages.model_validate({
            "api": requestModel.api, "provider": requestModel.provider, "model": requestModel.id,
            "output": [], "stopReason": "stop", "timestamp": 0,
        })

    registerImagesApiProvider(ImagesApiProvider(api=model.api, generateImages=generate))
    await generateImages(model, CONTEXT, ProviderImagesOptions(telemetryContext=telemetryContext))

    async def resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth())

    class Images:
        generateImages = staticmethod(generate)

    models = createModels()
    models.setProvider(createProvider(CreateProviderOptions(
        id=model.provider, auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=resolve)),
        models=[model], images={model.api: Images()},
    )))
    await models.generateImages(model, CONTEXT, {"telemetryContext": telemetryContext})

    assert observed == [telemetryContext, telemetryContext]


# --- openrouter-oauth.test.ts: image models share the provider's OAuth key --------------------


def test_openrouter_oauth_is_exposed_alongside_api_key_auth():
    provider = openrouterProvider()
    assert provider.auth.apiKey is not None
    assert provider.auth.oauth is not None
    assert provider.auth.oauth.loginLabel == "Sign in with OpenRouter"


async def test_resolves_the_same_stored_oauth_key_for_chat_and_image_models():
    credentials = InMemoryCredentialStore()

    async def store(_current):
        return OAuthCredential(type="oauth", access="sk-or-stored", refresh="", expires=2**53 - 1)

    await credentials.modify("openrouter", store)
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(openrouterProvider())
    chatModel = models.getModels("openrouter")[0]
    imageModel = models.getModelsOfType("image", "openrouter")[0]

    assert (await models.getAuth(chatModel)).auth.apiKey == "sk-or-stored"
    assert (await models.getAuth(imageModel)).auth.apiKey == "sk-or-stored"
