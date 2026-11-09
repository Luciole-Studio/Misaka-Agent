"""Translated from pi's ``packages/ai/src/providers/radius-config.ts`` (v1.1.0).

MISAKA fork: pi's ``RadiusGatewayModel`` flows into ``Model`` unvalidated (TypeScript's types are erased); misaka's
``Model`` is a pydantic model that raises. ``_isRadiusGatewayModel`` keeps upstream's ``typeof`` checks, and an entry
that passes them but cannot become a ``Model`` (a fractional ``contextWindow``, a ``cost`` missing ``cacheWrite``, a key
``Model`` does not declare) is dropped on its own, the way upstream's filter drops a malformed one, instead of failing
the whole catalog. ``fetch`` is ``misaka.utils.whatwg_fetch.fetch``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from misaka.ai.auth.types import OAuthCredential
from misaka.ai.types import InputModality, Model, ModelCost, ThinkingLevelMap
from misaka.utils.js import isNumber, jsLength, jsSlice, jsTrim
from misaka.utils.values import read_field
from misaka.utils.whatwg_fetch import fetch
from misaka.utils.whatwg_url import URL

DEFAULT_RADIUS_GATEWAY = "https://radius.pi.dev"


class RadiusGatewayModel(BaseModel):
    # MISAKA fork: `allow` so a `Model` field this type does not declare (`headers`, `compat`, `samplingParams`)
    # survives into the catalog the way upstream's spread carries it; keys are narrowed to `Model`'s own first.
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    reasoning: bool
    thinkingLevelMap: ThinkingLevelMap | None = None
    input: list[InputModality]
    cost: ModelCost
    contextWindow: int
    maxTokens: int


class RadiusGatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseUrl: str
    models: list[RadiusGatewayModel]


# `OAuthCredential & { gatewayConfig?: RadiusGatewayConfig }`; OAuthCredential keeps extra fields.
RadiusOAuthCredential = OAuthCredential

# MISAKA fork: what a gateway model may carry through to `Model`. `api`, `provider` and `baseUrl` are supplied by
# `getRadiusModelsFromConfig`, so a catalog that names a `provider` cannot claim models for someone else.
_PASSTHROUGH_FIELDS = set(Model.model_fields) - {"api", "provider", "baseUrl"}


def _isRadiusGatewayModel(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    model = value
    return (
        isinstance(model.get("id"), str)
        and isinstance(model.get("name"), str)
        and isinstance(model.get("reasoning"), bool)
        and isinstance(model.get("input"), list)
        and isinstance(model.get("cost"), Mapping)
        and isNumber(model.get("contextWindow"))
        and isNumber(model.get("maxTokens"))
    )


def _coerceGatewayModel(value: Any) -> RadiusGatewayModel | None:
    """MISAKA fork: one catalog entry as a model, or ``None`` when it cannot be one (see the module docstring)."""
    try:
        return RadiusGatewayModel.model_validate({key: item for key, item in value.items() if key in _PASSTHROUGH_FIELDS})
    except ValidationError:
        return None


def _sanitizeRadiusGatewayConfig(config: Any) -> RadiusGatewayConfig | None:
    if not isinstance(config, Mapping):
        return None
    baseUrl, models = config.get("baseUrl"), config.get("models")
    if not isinstance(baseUrl, str) or not isinstance(models, list):
        return None
    return RadiusGatewayConfig(
        baseUrl=baseUrl,
        models=[
            model
            for model in (_coerceGatewayModel(entry) for entry in models if _isRadiusGatewayModel(entry))
            if model is not None
        ],
    )


def normalizeRadiusGatewayUrl(value: str) -> str:
    # `\Z`, not `$`: JavaScript's `$` (no `m` flag) anchors at the very end; Python's also matches before a final newline.
    withScheme = value if re.match(r"^https?://", value, re.IGNORECASE) else f"https://{value}"
    return re.sub(r"/+\Z", "", withScheme)


def getRadiusCredentialConfig(credential: OAuthCredential | None) -> RadiusGatewayConfig | None:
    return _sanitizeRadiusGatewayConfig(read_field(credential, "gatewayConfig"))


def getRadiusModelsFromConfig(providerId: str, config: RadiusGatewayConfig) -> list[Model]:
    return [
        Model.model_validate(
            {
                **model.model_dump(),
                "api": "pi-messages",
                "provider": providerId,
                "baseUrl": config.baseUrl,
            }
        )
        for model in config.models
    ]


def getRadiusModels(providerId: str, credential: OAuthCredential | None) -> list[Model]:
    config = getRadiusCredentialConfig(credential)
    return getRadiusModelsFromConfig(providerId, config) if config else []


def _truncateHttpBody(body: str) -> str:
    trimmed = jsTrim(body)
    return f"{jsSlice(trimmed, 0, 512)}…" if jsLength(trimmed) > 512 else trimmed


async def loadRadiusGatewayConfig(
    gateway: str,
    apiKey: str | None = None,
    signal: Any = None,
) -> RadiusGatewayConfig:
    headers: dict[str, str] = {"accept": "application/json"}
    if apiKey:
        headers["authorization"] = f"Bearer {apiKey}"
    response = await fetch(URL("/v1/config", gateway), {"headers": headers, "signal": signal})
    if not response.ok:
        raise RuntimeError(
            f"Could not load Radius config from {gateway}: {response.status}: {_truncateHttpBody(await response.text())}"
        )
    config = _sanitizeRadiusGatewayConfig(await response.json())
    if not config:
        raise RuntimeError(f"Invalid Radius config from {gateway}")
    return config


__all__ = [
    "DEFAULT_RADIUS_GATEWAY",
    "RadiusGatewayConfig",
    "RadiusGatewayModel",
    "RadiusOAuthCredential",
    "getRadiusCredentialConfig",
    "getRadiusModels",
    "getRadiusModelsFromConfig",
    "loadRadiusGatewayConfig",
    "normalizeRadiusGatewayUrl",
]
