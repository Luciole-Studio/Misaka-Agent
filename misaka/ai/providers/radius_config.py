"""Radius gateway catalog config, translated from pi's ``providers/radius-config.ts``.

A Radius gateway serves its own model catalog from ``GET /v1/config``: a base URL plus a
list of models, which the provider turns into ``pi-messages`` models owned by whichever
provider id is asking. The same shape also travels inside an OAuth credential
(``gatewayConfig``), which is how the pre-ModelsStore Radius implementation cached a
catalog; ``get_radius_models`` is the importer for those.

Three translations are worth naming, because "port the type guard" is not the whole job:

* **Two guards instead of one.** Upstream's ``isRadiusGatewayModel`` is a ``typeof``
  check and the object then flows into ``Model`` unvalidated -- TypeScript's ``Model`` is
  erased at runtime, so nothing else ever looks. misaka's ``Model`` is a pydantic model
  that raises, and a raise here would take the whole refresh down over one malformed
  entry. So the ``typeof`` checks are ported verbatim (``_is_radius_gateway_model``) to
  keep upstream's acceptance set, and a pydantic validation follows to keep a model that
  misaka cannot *represent* -- a fractional ``contextWindow``, a ``cost`` missing
  ``cacheWrite`` -- out of the catalog. Both rejections do what upstream's filter does:
  drop that one model and keep the rest.
* **Unknown keys are dropped, not carried.** Upstream spreads (``{...model}``) so a key
  the gateway invented rides along into ``Model``. misaka's ``Model`` forbids extras, so
  carrying one would fail validation for every model that had it. Keys are therefore
  narrowed to what ``Model`` declares, which still passes through the fields upstream's
  ``RadiusGatewayModel`` type omits but ``Model`` has (``headers``, ``compat``,
  ``samplingParams``).
* **``new URL(path, gateway)`` is ``urljoin`` plus a check.** Both replace the whole path
  when the right-hand side is absolute, so a gateway configured as ``https://host/base``
  is asked for ``https://host/v1/config`` in either language -- but on a *malformed*
  gateway ``new URL`` throws while ``urljoin`` returns a host-less ``/v1/config``. The
  base is validated so both languages refuse the same input.

The HTTP call is a parameter with a default rather than a hard-wired ``httpx`` call, so
the catalog path can be exercised without a socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from misaka.ai.types import InputModality, Model, ModelCost, ThinkingLevelMap
from misaka.ai.utils.abort import race_with_abort_signal
from misaka.utils.values import read_field

DEFAULT_RADIUS_GATEWAY = "https://radius.pi.dev"
# Every Radius model streams through the pi-messages protocol.
RADIUS_API = "pi-messages"
CONFIG_PATH = "/v1/config"
# Upstream leaves the timeout to the runtime's `fetch`. httpx does have a default -- 5s on
# each of connect/read/write/pool -- which is tight for a gateway round trip, so the bound
# is stated here instead: long enough for a slow config fetch, short enough that a gateway
# which never answers does not pin a refresh forever.
REQUEST_TIMEOUT_MS = 30 * 1000
BODY_EXCERPT_LIMIT = 512

# What a gateway model may carry through to `Model`. `api`, `provider` and `baseUrl` are
# excluded because this module supplies them: a catalog that names a `provider` must not
# be able to claim models for someone else. `get_radius_models_from_config` writes the
# same three last for the same reason, so either line alone would do -- and a catalog that
# reaches this provider is a catalog that can already choose the base URL its requests go
# to, which is why the cheap half of the defence is kept rather than deduplicated.
_PASSTHROUGH_FIELDS = set(Model.model_fields) - {"api", "provider", "baseUrl"}


@dataclass(slots=True)
class RadiusHttpResponse:
    """The slice of a response this package reads: a status and an already-read body.

    Shared with ``ai/utils/oauth/radius.py``, which speaks to the same gateway.
    """

    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


RadiusFetch = Callable[[str, dict[str, str], Any], Awaitable[RadiusHttpResponse]]


class RadiusGatewayModel(BaseModel):
    """Upstream's ``RadiusGatewayModel``: the eight fields a gateway catalog entry declares."""

    # `allow` so a `Model` field this type does not declare -- `headers`, `compat`,
    # `samplingParams` -- survives into the catalog the way upstream's spread carries it.
    # Keys are already narrowed to `Model`'s own before validation, so nothing else lands here.
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


def _is_number(value: Any) -> bool:
    """``typeof value === "number"``. JSON booleans are not numbers; Python's are ints."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_radius_gateway_model(value: Any) -> bool:
    """Upstream's ``isRadiusGatewayModel``, field for field.

    ``Mapping`` rather than ``dict`` covers the "object, not null, not an array" test:
    JSON objects arrive as mappings and JSON arrays as lists, so the array exclusion is
    the type check itself.
    """
    if not isinstance(value, Mapping):
        return False
    return (
        isinstance(value.get("id"), str)
        and isinstance(value.get("name"), str)
        and isinstance(value.get("reasoning"), bool)
        and isinstance(value.get("input"), list)
        and isinstance(value.get("cost"), Mapping)
        and _is_number(value.get("contextWindow"))
        and _is_number(value.get("maxTokens"))
    )


def _coerce_gateway_model(value: Any) -> RadiusGatewayModel | None:
    """One catalog entry, or ``None`` when it is not usable as a model."""
    if not _is_radius_gateway_model(value):
        return None
    try:
        return RadiusGatewayModel.model_validate(
            {key: item for key, item in value.items() if key in _PASSTHROUGH_FIELDS}
        )
    except ValidationError:
        # Passes upstream's `typeof` checks but cannot become a `Model`. Dropping the one
        # entry is what upstream's filter does; raising would lose the whole catalog.
        return None


def sanitize_radius_gateway_config(config: Any) -> RadiusGatewayConfig | None:
    """Upstream's ``sanitizeRadiusGatewayConfig``: a config, or ``None`` if unusable."""
    if not isinstance(config, Mapping):
        return None
    baseUrl = config.get("baseUrl")
    models = config.get("models")
    if not isinstance(baseUrl, str) or not isinstance(models, list):
        return None
    coerced = [model for model in (_coerce_gateway_model(entry) for entry in models) if model is not None]
    return RadiusGatewayConfig(baseUrl=baseUrl, models=coerced)


def normalize_radius_gateway_url(value: str) -> str:
    """Add the default scheme, drop trailing slashes.

    ``\\Z``, not ``$``: upstream's ``/\\/+$/u`` (no ``m`` flag) anchors at the very end of
    the string, while Python's ``$`` also matches just before a trailing newline -- which
    would leave a gateway configured as ``"https://host/\\n"`` with the newline and
    without the slash.
    """
    with_scheme = value if re.match(r"^https?://", value, re.IGNORECASE) else f"https://{value}"
    return re.sub(r"/+\Z", "", with_scheme)


def get_radius_credential_config(credential: Any) -> RadiusGatewayConfig | None:
    """The catalog a legacy Radius OAuth credential carries, if it carries a usable one.

    ``read_field`` because the credential arrives as an ``OAuthCredential`` (extras are
    attributes) from the runtime and as a plain mapping from storage and tests.
    """
    return sanitize_radius_gateway_config(read_field(credential, "gatewayConfig"))


def get_radius_models_from_config(providerId: str, config: RadiusGatewayConfig) -> list[Model]:
    """The gateway's catalog as models owned by ``providerId``."""
    return [
        Model.model_validate(
            {
                **model.model_dump(),
                "api": RADIUS_API,
                "provider": providerId,
                "baseUrl": config.baseUrl,
            }
        )
        for model in config.models
    ]


def get_radius_models(providerId: str, credential: Any) -> list[Model]:
    config = get_radius_credential_config(credential)
    return get_radius_models_from_config(providerId, config) if config is not None else []


def truncate_http_body(body: str) -> str:
    """Trim, then cut to 512 characters with an ellipsis.

    Upstream's ``slice`` counts UTF-16 code units and this counts code points; they agree
    on everything below U+10000, and the value is a diagnostic excerpt either way.
    """
    trimmed = body.strip()
    return f"{trimmed[:BODY_EXCERPT_LIMIT]}…" if len(trimmed) > BODY_EXCERPT_LIMIT else trimmed



def _gateway_url(gateway: str, path: str) -> str:
    """``new URL(path, gateway)``: an absolute path replaces the gateway's own.

    ``urljoin`` agrees with ``new URL`` on every well-formed absolute base, but not on a
    malformed one: ``new URL`` throws where ``urljoin`` quietly returns a host-less string
    like ``/v1/config``, which then goes out as a request to nowhere. The base is checked
    so a bad gateway fails at configuration rather than at an unexplained request.
    """
    parsed = urlparse(gateway)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Radius gateway must be an absolute URL: {gateway!r}")
    return urljoin(gateway, path)

async def _default_fetch(url: str, headers: dict[str, str], signal: Any) -> RadiusHttpResponse:
    """The real transport, raced against the caller's signal.

    Upstream hands the ``AbortSignal`` to ``fetch`` and the in-flight request dies with
    it. misaka's signals are duck-typed ``aborted`` flags that httpx knows nothing about,
    so the race is the equivalent -- and the client's own timeout is what bounds the
    request the race abandons.
    """

    async def request() -> RadiusHttpResponse:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000) as client:
            response = await client.get(url, headers=headers)
        return RadiusHttpResponse(status=response.status_code, body=response.text)

    return await race_with_abort_signal(request(), signal)


def _parse_json(body: str) -> Any:
    """``await response.json()``.

    A body that is not JSON reaches ``sanitize`` as ``None`` and becomes the "Invalid
    Radius config" error. Upstream ends up somewhere else -- ``await response.json()``
    rejects with a ``SyntaxError`` that propagates straight out of
    ``loadRadiusGatewayConfig``, never reaching its own "Invalid Radius config" throw.
    Folding the two together is deliberate: one error for one broken gateway, rather than
    two spellings of it.
    """
    try:
        return json.loads(body)
    except ValueError:
        return None


async def load_radius_gateway_config(
    gateway: str,
    apiKey: str | None = None,
    signal: Any = None,
    *,
    fetch: RadiusFetch | None = None,
) -> RadiusGatewayConfig:
    """Fetch and validate ``GET /v1/config`` from a gateway."""
    send = fetch or _default_fetch
    headers = {"accept": "application/json"}
    if apiKey:
        headers["authorization"] = f"Bearer {apiKey}"
    response = await send(_gateway_url(gateway, CONFIG_PATH), headers, signal)
    if not response.ok:
        raise RuntimeError(
            f"Could not load Radius config from {gateway}: "
            f"{response.status}: {truncate_http_body(response.body)}"
        )
    config = sanitize_radius_gateway_config(_parse_json(response.body))
    if config is None:
        raise RuntimeError(f"Invalid Radius config from {gateway}")
    return config


__all__ = [
    "BODY_EXCERPT_LIMIT",
    "CONFIG_PATH",
    "DEFAULT_RADIUS_GATEWAY",
    "RADIUS_API",
    "RadiusFetch",
    "RadiusGatewayConfig",
    "RadiusGatewayModel",
    "RadiusHttpResponse",
    "get_radius_credential_config",
    "get_radius_models",
    "get_radius_models_from_config",
    "load_radius_gateway_config",
    "normalize_radius_gateway_url",
    "sanitize_radius_gateway_config",
    "truncate_http_body",
]
