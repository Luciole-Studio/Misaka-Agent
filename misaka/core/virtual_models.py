"""Virtual models are catalog entries that route each request to a physical model.

Translated from pi's ``coding-agent/src/core/virtual-models.ts``.

The selection (``model_change``, ``agent.state.model``, ``ctx.model``) may name a virtual model.
Everything below the routing step only sees physical models: providers stream them and
assistant messages record them. A virtual model never reaches a provider.

Virtual models belong to a provider id but are not provider models. ``ModelRuntime`` keeps them
separately and adds them to the provider's catalog with ``withVirtualModels()``, so any provider,
including one with physical models, can list several virtual models.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, TypedDict

from misaka.ai.api.lazy import lazyStream
from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEventStream,
    Message,
    Model,
    ModelThinkingLevel,
)
from misaka.ai.utils.model_operations import isModelType
from misaka.utils.values import read_field

# API id of virtual catalog entries. Requests for it fail unless routed first.
VIRTUAL_MODEL_API = "pi-virtual"

# Custom entry type that stores router state on the session branch.
VIRTUAL_MODEL_STATE_ENTRY = "pi.virtual-model-state"


class VirtualModelStateData(TypedDict):
    """Data of a ``pi.virtual-model-state`` custom entry."""

    provider: str
    modelId: str
    state: Any


@dataclass(slots=True)
class PreviousRoute:
    model: Model
    thinkingLevel: ModelThinkingLevel | None = None


@dataclass(slots=True)
class FailedRoute:
    model: Model
    message: AssistantMessage
    thinkingLevel: ModelThinkingLevel | None = None

THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# Why a request is being routed.
# - `user`: first request after a message the user wrote (prompt, steering, or follow-up)
# - `continuation`: any other request in the agent loop, e.g. after tool results or extension messages
# - `retry`: automatic retry after a failed request, including after compaction for a context overflow
# - `direct`: a request outside the agent loop, e.g. a compaction summary or an extension call
type ModelRouteReason = Literal["user", "continuation", "retry", "direct"]


@dataclass(slots=True)
class ModelRouteRequest:
    # The selected virtual model.
    model: Model
    # The selected thinking level. Its meaning is up to the router.
    thinkingLevel: ModelThinkingLevel
    reason: ModelRouteReason
    # Conversation for this request, including system messages.
    messages: Sequence[Message]
    # Physical model and thinking level of the latest successful response in `messages`.
    previous: PreviousRoute | None = None
    # For `retry`: the failed request, which `messages` no longer contains. `message` carries its
    # `stopReason` and `errorMessage`. Absent when the router itself failed.
    failed: FailedRoute | None = None
    # Router state last returned on this session branch. None before the first state and for
    # `direct` requests.
    state: Any = None
    signal: Any = None


@dataclass(slots=True)
class ModelRoute:
    """Physical model and thinking level for one request."""

    model: Model
    thinkingLevel: ModelThinkingLevel
    # New router state, stored on the session branch unless it is `request.state` itself. Return
    # `request.state` or None to keep the current state. Must be JSON-serializable. Ignored for
    # `direct` requests.
    state: Any = None


@dataclass(slots=True)
class VirtualModelDefinition:
    # Provider the virtual model is listed under. May be a provider with physical models.
    provider: str
    # Model id. Must not be the id of a physical model of `provider`.
    id: str
    name: str
    # Pick the physical model, which must have credentials, and thinking level for one request.
    route: Callable[[ModelRouteRequest], ModelRoute | Awaitable[ModelRoute]]
    # Thinking levels offered for selection. Defaults to `["off"]`.
    thinkingLevels: Sequence[ModelThinkingLevel] | None = None
    # Limits shown before the first response. Afterwards, misaka uses the limits of the physical
    # model that answered. Unset limits are unknown (0).
    contextWindow: int | None = None
    maxTokens: int | None = None
    # Input types accepted for selection. Defaults to text and images; routed models without image
    # support get placeholders.
    input: list[Literal["text", "image"]] | None = None


def isVirtualModel(model: Any) -> bool:
    """Whether a model or message names a virtual model. Failed routing leaves the virtual model on its message."""
    return read_field(model, "api") == VIRTUAL_MODEL_API


def answersAsOtherModels(model: Any) -> bool:
    """MISAKA fork: whether the responses of a selection name other models. Besides virtual models,
    MoA presets (``core/moa``) answer through their aggregator. MoA keeps its own stream, so only
    what reads a selection back (the branch's selection, the model behind a response) treats a
    preset like a virtual model; routing does not."""
    return isVirtualModel(model) or read_field(model, "api") == "moa"


def findLatestResponse(messages: Sequence[Any]) -> Any:
    """Latest successful response. Its model is physical: failed or aborted requests, including
    failed routing, are skipped."""
    for message in reversed(messages):
        if read_field(message, "role") == "assistant" and read_field(message, "stopReason") not in ("error", "aborted"):
            return message
    return None


def getBranchSelection(
    branch: Sequence[Any],
    getModel: Callable[[str, str], Model | None],
) -> dict[str, str] | None:
    """The model selection a session branch records. A virtual ``model_change`` holds until the
    next ``model_change``, because responses name the physical models it routed to. Otherwise the
    latest physical response wins, as in sessions without virtual models. A virtual model that is
    no longer registered does not hold, so the selection falls back to the physical model that
    answered last.

    Only the last ``model_change`` can hold, so this looks up at most one model in the catalog."""
    for i in range(len(branch) - 1, -1, -1):
        entry = branch[i]
        if read_field(entry, "type") == "model_change":
            return {"provider": read_field(entry, "provider"), "modelId": read_field(entry, "modelId")}
        if read_field(entry, "type") == "message":
            message = read_field(entry, "message")
            # MISAKA fork: an MoA preset's responses name other models too, like a virtual model's,
            # and a preset holds like a virtual model (`answersAsOtherModels` for both tests).
            if read_field(message, "role") == "assistant" and not answersAsOtherModels(message):
                response = {"provider": read_field(message, "provider"), "modelId": read_field(message, "model")}
                change = _findLastModelChange(branch, i)
                model = getModel(change["provider"], change["modelId"]) if change else None
                return change if change and model is not None and answersAsOtherModels(model) else response
    return None


def _findLastModelChange(branch: Sequence[Any], before: int) -> dict[str, str] | None:
    for i in range(before - 1, -1, -1):
        entry = branch[i]
        if read_field(entry, "type") == "model_change":
            return {"provider": read_field(entry, "provider"), "modelId": read_field(entry, "modelId")}
    return None


def getVirtualModelState(branch: Sequence[Any], provider: str, modelId: str) -> Any:
    """Latest router state a session branch stores for a virtual model."""
    for entry in reversed(branch):
        if read_field(entry, "type") != "custom" or read_field(entry, "customType") != VIRTUAL_MODEL_STATE_ENTRY:
            continue
        data = read_field(entry, "data")
        if read_field(data, "provider") == provider and read_field(data, "modelId") == modelId:
            return read_field(data, "state")
    return None


def createVirtualModel(definition: VirtualModelDefinition) -> Model:
    """Build the catalog entry of a virtual model."""
    levels = list(definition.thinkingLevels) if definition.thinkingLevels is not None else ["off"]
    thinkingLevelMap = {level: level if level in levels else None for level in THINKING_LEVELS}
    return Model.model_validate({
        "id": definition.id,
        "name": definition.name,
        "api": VIRTUAL_MODEL_API,
        "provider": definition.provider,
        "baseUrl": "",
        "reasoning": any(level != "off" for level in levels),
        "thinkingLevelMap": thinkingLevelMap,
        "input": definition.input if definition.input is not None else ["text", "image"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": definition.contextWindow if definition.contextWindow is not None else 0,
        "maxTokens": definition.maxTokens if definition.maxTokens is not None else 0,
    })


def _unroutedStream(model: Model, *_args: Any) -> AssistantMessageEventStream:
    """Stream for a virtual model that was not routed, e.g. ``stream()`` with API-specific options."""

    async def setup() -> Any:
        raise RuntimeError(f"Virtual model {model.provider}/{model.id} must be routed before streaming")

    return lazyStream(model, setup)


async def _virtualResolve(**_input: Any) -> AuthResult:
    return AuthResult(auth=ModelAuth(), source="virtual")


class _WithVirtualModels:
    """``{...provider, getModels, getAllModels, filterModels, filterAllModels, stream, streamSimple}``."""

    def __init__(self, provider: Any, virtualModels: Sequence[Model]) -> None:
        self._provider = provider
        self._virtualModels = list(virtualModels)
        self._ids = {model.id for model in virtualModels}
        filterAllModels = getattr(provider, "filterAllModels", None)
        self.filterAllModels = (
            (lambda models, credential: [*filterAllModels(self._physical(models), credential), *self._virtual(models)])
            if filterAllModels is not None
            else None
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def _physical(self, models: Sequence[Model]) -> list[Model]:
        return [
            model
            for model in models
            if not isVirtualModel(model) and not (isModelType(model, "chat") and model.id in self._ids)
        ]

    @staticmethod
    def _virtual(models: Sequence[Model]) -> list[Model]:
        return [model for model in models if isVirtualModel(model)]

    def getModels(self) -> list[Model]:
        return [*self._physical(self._provider.getModels()), *self._virtualModels]

    def getAllModels(self) -> list[Model]:
        getAllModels = getattr(self._provider, "getAllModels", None)
        return [
            *self._physical(getAllModels() if getAllModels is not None else self._provider.getModels()),
            *self._virtualModels,
        ]

    def filterModels(self, models: Sequence[Model], credential: Any) -> list[Model]:
        real = self._physical(models)
        filterModels = getattr(self._provider, "filterModels", None)
        return [*(filterModels(real, credential) if filterModels is not None else real), *self._virtual(models)]

    def stream(self, model: Model, context: Any, options: Any = None) -> AssistantMessageEventStream:
        return _unroutedStream(model) if isVirtualModel(model) else self._provider.stream(model, context, options)

    def streamSimple(self, model: Model, context: Any, options: Any = None) -> AssistantMessageEventStream:
        return (
            _unroutedStream(model) if isVirtualModel(model) else self._provider.streamSimple(model, context, options)
        )


def withVirtualModels(providerId: str, provider: Any, virtualModels: Sequence[Model]) -> Any:
    """Add virtual models to a provider's catalog. Without a provider, the result is a keyless
    provider that only lists the virtual models. A virtual model hides a physical chat model with
    the same id, which a catalog refresh can add after registration. Availability follows the
    provider's auth."""
    if provider is None:
        return SimpleNamespace(
            id=providerId,
            name=providerId,
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Virtual model", resolve=_virtualResolve)),
            getModels=lambda: list(virtualModels),
            stream=_unroutedStream,
            streamSimple=_unroutedStream,
        )
    return _WithVirtualModels(provider, virtualModels)


__all__ = [
    "VIRTUAL_MODEL_API",
    "VIRTUAL_MODEL_STATE_ENTRY",
    "FailedRoute",
    "ModelRoute",
    "ModelRouteReason",
    "ModelRouteRequest",
    "PreviousRoute",
    "VirtualModelDefinition",
    "VirtualModelStateData",
    "answersAsOtherModels",
    "createVirtualModel",
    "findLatestResponse",
    "getBranchSelection",
    "getVirtualModelState",
    "isVirtualModel",
    "withVirtualModels",
]
