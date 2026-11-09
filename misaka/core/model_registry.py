"""The synchronous compatibility facade exposed to extensions.

Translated from pi's ``coding-agent/src/core/model-registry.ts``. misaka's own code uses
``ModelRuntime`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from misaka.ai.auth.types import AuthOperationOptions, AuthResult
from misaka.ai.models import ModelsRefreshOptions, ModelsRefreshResult
from misaka.ai.types import (
    AssistantImages,
    AssistantMessage,
    AssistantMessageEventStream,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    Context,
    ImageModel,
    ImagesContext,
    Model,
    ModelType,
    ProviderHeaders,
)
from misaka.core.provider_composer import (
    AuthStatus,
    ProviderConfigInput,
    clearApiKeyCache,
)

if TYPE_CHECKING:
    from misaka.ai.auth.resolve import AuthResolutionOverrides
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.virtual_models import VirtualModelDefinition


class _ResolvedRequestAuthOk(TypedDict):
    ok: Literal[True]
    apiKey: NotRequired[str | None]
    headers: NotRequired[ProviderHeaders | None]
    baseUrl: NotRequired[str]
    env: NotRequired[dict[str, str] | None]


class _ResolvedRequestAuthError(TypedDict):
    ok: Literal[False]
    error: str


type ResolvedRequestAuth = _ResolvedRequestAuthOk | _ResolvedRequestAuthError


class ModelRegistry:
    """Synchronous compatibility facade exposed to extensions.
    misaka's own code uses ModelRuntime directly."""

    def __init__(self, runtime: ModelRuntime) -> None:
        self._runtime = runtime

    # -- MISAKA fork: what extensions reach through the facade that pi's do not -------------

    @property
    def modelRuntime(self) -> ModelRuntime:
        return self._runtime

    @property
    def authStorage(self) -> AuthStorage | None:
        """The legacy store behind the credentials (LCM pools and the nous gateway read it)."""
        return self._runtime.authStorage

    # ---------------------------------------------------------------------------------------

    async def refresh(self, options: ModelsRefreshOptions | None = None) -> ModelsRefreshResult:
        """Reload models.json asynchronously. Await before making synchronous registry reads."""
        return await self._runtime.refresh(options)

    def getError(self) -> str | None:
        return self._runtime.getError()

    def getAll(self) -> list[Model]:
        return list(self._runtime.getModels())

    def getAvailable(self) -> list[Model]:
        return list(self._runtime.getAvailableSnapshot())

    def find(self, provider: str, modelId: str) -> Model | None:
        return self._runtime.getModel(provider, modelId)

    def findOfType(self, type: ModelType, provider: str, modelId: str) -> Any:
        """Find a model of a non-chat type, e.g. ``findOfType("classifier", "typesafe", "jev-latest")``."""
        return self._runtime.getModelOfType(type, provider, modelId)

    def hasConfiguredAuth(self, model: Model) -> bool:
        return self._runtime.hasConfiguredAuth(model.provider)

    async def getApiKeyAndHeaders(
        self, model: Model, overrides: AuthResolutionOverrides | None = None
    ) -> ResolvedRequestAuth:
        """MISAKA fork: ``overrides`` (LCM resolves with its own validity window)."""
        try:
            resolution = await self._runtime.getAuth(model, overrides)
            if resolution is None:
                compatibility = self._runtime.getCompatibilityRequestConfig(model)
                if compatibility.authHeader:
                    return {"ok": False, "error": f'No API key found for "{model.provider}"'}
                return {"ok": True, "headers": compatibility.headers}
            result: _ResolvedRequestAuthOk = {
                "ok": True,
                "apiKey": resolution.auth.apiKey,
                "headers": resolution.auth.headers,
            }
            if resolution.auth.baseUrl:
                result["baseUrl"] = resolution.auth.baseUrl
            result["env"] = resolution.env
            return result
        except Exception as error:  # noqa: BLE001 - reported to the caller as a failed resolution
            cause = error.__cause__
            message = str(cause) if isinstance(cause, Exception) else str(error)
            return {
                "ok": False,
                "error": (
                    f'No API key found for "{model.provider}"'
                    if message == "authHeader requires a resolved API key"
                    else message
                ),
            }

    def getProviderAuthStatus(self, provider: str) -> AuthStatus:
        return self._runtime.getProviderAuthStatus(provider)

    def getProvider(self, provider: str) -> Any:
        return self._runtime.getProvider(provider)

    def stream(self, model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        """Stream through the configured provider with request-time authentication."""
        return self._runtime.stream(model, context, options)

    def streamSimple(self, model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        """Stream with provider-neutral options and request-time authentication."""
        return self._runtime.streamSimple(model, context, options)

    async def complete(self, model: Model, context: Context, options: Any = None) -> AssistantMessage:
        return await self._runtime.complete(model, context, options)

    def getModelsOfType(self, type: ModelType, provider: str | None = None) -> list[Any]:
        """Every known model of a type (chat, image, classifier), optionally for one provider."""
        return self._runtime.getModelsOfType(type, provider)

    async def getAvailableOfType(
        self, type: ModelType, provider: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Any]:
        """Models of a type whose provider has working credentials."""
        return await self._runtime.getAvailableOfType(type, provider, options)

    def getModelOfType(self, type: ModelType, provider: str, modelId: str) -> Any:
        return self._runtime.getModelOfType(type, provider, modelId)

    async def classify(
        self, model: ClassifierModel, context: ClassifierContext, options: Any = None
    ) -> ClassifierResult:
        """Classify structured state with request-time authentication. Never raises."""
        return await self._runtime.classify(model, context, options)

    async def generateImages(self, model: ImageModel, context: ImagesContext, options: Any = None) -> AssistantImages:
        """Generate images with request-time authentication. Never raises."""
        return await self._runtime.generateImages(model, context, options)

    def getProviderDisplayName(self, provider: str) -> str:
        registered = self._runtime.getProvider(provider)
        name = registered.name if registered is not None else None
        return name if name is not None else provider

    async def getProviderAuth(self, provider: str) -> AuthResult | None:
        return await self._runtime.getAuth(provider)

    async def getApiKeyForProvider(self, provider: str) -> str | None:
        try:
            resolution = await self._runtime.getAuth(provider)
        except Exception:  # noqa: BLE001 - an unresolvable provider has no key to report
            return None
        return resolution.auth.apiKey if resolution is not None else None

    def isUsingOAuth(self, model: Model) -> bool:
        return self._runtime.isUsingOAuth(model.provider)

    def registerProvider(self, providerOrName: Any, config: ProviderConfigInput | None = None) -> None:
        if isinstance(providerOrName, str):
            if config is None:
                raise RuntimeError("Provider config is required when registering by name")
            self._runtime.registerProvider(providerOrName, config)
            return
        self._runtime.registerNativeProvider(providerOrName)

    def unregisterProvider(self, providerName: str) -> None:
        self._runtime.unregisterProvider(providerName)

    def registerVirtualModel(self, definition: VirtualModelDefinition) -> None:
        self._runtime.registerVirtualModel(definition)

    def unregisterVirtualModel(self, providerName: str, id: str) -> None:
        self._runtime.unregisterVirtualModel(providerName, id)

    def getRegisteredProviderConfig(self, providerName: str) -> ProviderConfigInput | None:
        return self._runtime.getRegisteredProviderConfig(providerName)

    def getRegisteredNativeProvider(self, providerName: str) -> Any:
        return self._runtime.getRegisteredNativeProvider(providerName)

    def getRegisteredProviderIds(self) -> list[str]:
        return list(self._runtime.getRegisteredProviderIds())


__all__ = [
    "ModelRegistry",
    "ProviderConfigInput",
    "ResolvedRequestAuth",
    "clearApiKeyCache",
]
