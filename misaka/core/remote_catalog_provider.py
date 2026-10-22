"""A persisted remote catalog overlay on a static built-in provider.

Translated from pi's ``coding-agent/src/core/remote-catalog-provider.ts`` (v1.3.0).
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from misaka.ai.models import ModelsPublication, RefreshModelsContext
from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.types import ANY_MODEL_ADAPTER, AnyModel
from misaka.ai.utils.model_operations import getModelType, isModelType
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.utils.management_http import FetchRetryOptions, fetchWithRetry
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL

# MISAKA fork: no default catalog endpoint (pi: "https://pi.dev"). misaka does not send users' traffic to pi's service;
# an embedder that runs a catalog server passes its URL as `catalogBaseUrl`.
DEFAULT_CATALOG_BASE_URL: str | None = None
REMOTE_CATALOG_ATTEMPT_TIMEOUT_MS = 4_000
REMOTE_CATALOG_REFRESH_INTERVAL_MS = 4 * 60 * 60 * 1000
# Model types this client can consume. Sent as `?types=` so the catalog server
# returns the full-type shard instead of the chat-only one served to clients
# that predate model types. A server that ignores the parameter still returns
# the chat-only shard, which this client handles unchanged.
REMOTE_CATALOG_MODEL_TYPES: tuple[str, ...] = ("chat", "image", "classifier")


def _now() -> int:
    return int(time.time() * 1000)


def _isSupportedModelType(model: Mapping[str, Any]) -> bool:
    return "type" not in model or (
        isinstance(model["type"], str) and model["type"] in REMOTE_CATALOG_MODEL_TYPES
    )


def _mergeModels(baseline: Sequence[AnyModel], dynamic: Sequence[AnyModel]) -> list[AnyModel]:
    merged: dict[tuple[str, str], AnyModel] = {}
    for model in [*baseline, *dynamic]:
        merged[(getModelType(model), model.id)] = model
    return list(merged.values())


def _toModel(entry: dict[str, Any]) -> AnyModel | None:
    # MISAKA fork: catalog entries become typed models here. A malformed entry is left out
    # instead of failing the whole catalog.
    try:
        return ANY_MODEL_ADAPTER.validate_python(entry)
    except ValidationError:
        return None


def _parseCatalog(providerId: str, value: Any) -> list[AnyModel]:
    entries = (
        value
        if isinstance(value, list)
        else value["models"]
        if isinstance(value, dict) and isinstance(value.get("models"), list)
        else list(value.values())
        if isinstance(value, dict)
        else None
    )
    if entries is None:
        raise RuntimeError(f'Invalid model catalog for provider "{providerId}"')
    models = (
        _toModel({**entry, "provider": providerId})
        for entry in entries
        if isinstance(entry, dict) and "id" in entry and _isSupportedModelType(entry)
    )
    return [model for model in models if model is not None]


def _parseHttpDate(value: str | None) -> float:
    """``Date.parse`` of an HTTP date: epoch milliseconds, NaN when unreadable."""
    try:
        parsed = parsedate_to_datetime(value or "")
    except (TypeError, ValueError):
        return float("nan")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000


@dataclass(slots=True)
class _Catalog:
    models: list[AnyModel]
    lastModified: int
    etag: str | None


async def _readCatalog(providerId: str, response: Any) -> _Catalog:
    """Read a successful pi.dev catalog response. A missing or invalid Last-Modified header is 0."""
    lastModified = _parseHttpDate(response.headers.get("last-modified"))
    return _Catalog(
        models=_parseCatalog(providerId, await response.json()),
        lastModified=0 if math.isnan(lastModified) else int(lastModified),
        etag=response.headers.get("etag"),
    )


def _isNewer(lastModified: int | None, localGeneratedAt: int | None) -> bool:
    """Whether a pi.dev catalog is newer than the model data shipped with pi."""
    return localGeneratedAt is None or (lastModified is not None and lastModified > localGeneratedAt)


def _remoteModels(entry: ModelsStoreEntry | None, localGeneratedAt: int | None) -> list[AnyModel]:
    return list(entry.models) if entry is not None and _isNewer(entry.lastModified, localGeneratedAt) else []


async def _requestCatalog(providerId: str, catalogBaseUrl: str, signal: Any, validator: str | None = None) -> Any:
    # `encodeURIComponent` leaves !~*'() alone too.
    url = URL(f"/api/models/providers/{quote(providerId, safe="!~*'()")}", catalogBaseUrl)
    url.searchParams.set("types", ",".join(REMOTE_CATALOG_MODEL_TYPES))
    return await fetchWithRetry(
        url,
        {
            "headers": {
                "accept": "application/json",
                # MISAKA fork: misaka's anonymous user agent (ai/utils/pi_user_agent.py), not pi's `pi/<version>` one.
                "User-Agent": getPiUserAgent(),
                **({"if-none-match": validator} if validator else {}),
            },
            "signal": signal,
        },
        FetchRetryOptions(attemptTimeoutMs=REMOTE_CATALOG_ATTEMPT_TIMEOUT_MS),
    )


# Replaces the catalog with a credential-specific model list.
type PersonalizeCatalog = Callable[[Sequence[AnyModel], RefreshModelsContext], Awaitable[Sequence[AnyModel]]]


def withRemoteCatalog(
    provider: Any,
    catalogBaseUrl: str | None = DEFAULT_CATALOG_BASE_URL,
    localGeneratedAt: int | None = None,
) -> Any:
    """Add a persisted remote catalog overlay to a static built-in provider.

    MISAKA fork: without a ``catalogBaseUrl`` there is no overlay and the provider is returned as it is, as
    ``withPersonalizedCatalog`` skips its catalog request (see ``DEFAULT_CATALOG_BASE_URL``)."""
    if catalogBaseUrl is None:
        return provider
    dynamicModels: list[AnyModel] = []

    class _RemoteCatalogProvider:
        def __getattr__(self, name: str) -> Any:
            return getattr(provider, name)

        def getModels(self) -> list[AnyModel]:
            return _mergeModels(
                provider.getModels(),
                [model for model in dynamicModels if isModelType(model, "chat")],
            )

        def getAllModels(self) -> list[AnyModel]:
            getAllModels = getattr(provider, "getAllModels", None)
            return _mergeModels(getAllModels() if getAllModels is not None else provider.getModels(), dynamicModels)

        async def refreshModels(self, context: RefreshModelsContext) -> None:
            stored = context.stored
            restored = [model for model in _remoteModels(stored, localGeneratedAt) if model.provider == provider.id]

            def applyRestored() -> None:
                nonlocal dynamicModels
                dynamicModels = restored

            if not await context.publish(ModelsPublication(update=applyRestored)):
                return
            if not context.allowNetwork or signal_aborted(context.signal):
                return
            # Skip the request if the catalog was checked less than REMOTE_CATALOG_REFRESH_INTERVAL_MS ago.
            # An entry without lastModified (saved by an older pi, or after a failed first request) is
            # always refetched: _remoteModels() ignores it, so waiting would leave the overlay empty.
            if (
                not context.force
                and stored is not None
                and stored.checkedAt is not None
                and stored.lastModified is not None
                and _now() - stored.checkedAt < REMOTE_CATALOG_REFRESH_INTERVAL_MS
            ):
                return

            # Only revalidate when a cached body backs the validator, so a 304 can never
            # leave the overlay empty.
            validator = stored.etag if stored is not None and len(stored.models) > 0 else None
            response = await _requestCatalog(provider.id, catalogBaseUrl, context.signal, validator)
            if signal_aborted(context.signal):
                return
            checkedAt = _now()
            # Unchanged: dynamicModels already holds the stored overlay, so only the
            # freshness window moves.
            if response.status == 304 and stored is not None:
                await context.publish(
                    ModelsPublication(persist=stored.model_copy(update={"checkedAt": checkedAt}), persist_is_set=True)
                )
                return
            if response.status == 404 or response.status == 501:
                await context.publish(
                    ModelsPublication(
                        persist=(stored or ModelsStoreEntry(models=[])).model_copy(
                            update={"checkedAt": checkedAt, "lastModified": 0, "etag": None}
                        ),
                        persist_is_set=True,
                    )
                )
                return
            if not response.ok:
                # Transient failure: the cached body and its validator stay valid, so keep the
                # etag and let the next refresh revalidate instead of downloading the catalog.
                await context.publish(
                    ModelsPublication(
                        persist=(stored or ModelsStoreEntry(models=[])).model_copy(update={"checkedAt": checkedAt}),
                        persist_is_set=True,
                    )
                )
                raise RuntimeError(f"Model catalog request failed for {provider.id}: {response.status}")
            catalog = await _readCatalog(provider.id, response)
            if signal_aborted(context.signal):
                return
            entry = ModelsStoreEntry(
                models=catalog.models, lastModified=catalog.lastModified, etag=catalog.etag, checkedAt=checkedAt
            )
            published = _remoteModels(entry, localGeneratedAt)

            def applyPublished() -> None:
                nonlocal dynamicModels
                dynamicModels = published

            await context.publish(ModelsPublication(persist=entry, persist_is_set=True, update=applyPublished))

    return _RemoteCatalogProvider()


def withPersonalizedCatalog(
    provider: Any,
    personalize: PersonalizeCatalog,
    catalogBaseUrl: str | None = None,
    localGeneratedAt: int | None = None,
) -> Any:
    """Like ``withRemoteCatalog``, but on each network refresh the catalog (built-in models plus a newer
    pi.dev catalog) is passed through ``personalize`` and the result is saved. The saved entry holds
    the personalized list, not the pi.dev catalog, and replaces the catalog, including offline.
    The refresh interval and etag revalidation are skipped. An empty list (nothing saved yet, or an
    empty catalog saved by an older pi after a failed or missing pi.dev catalog) falls back to the
    built-in models.

    MISAKA fork: ``catalogBaseUrl`` defaults to None, which skips the catalog request and personalizes the built-in
    catalog alone: misaka does not send users' traffic to pi's service, so the only request a refresh makes is
    ``personalize``'s own (OpenRouter's ``/models/user``, with the user's key).
    """
    personalModels: list[AnyModel] = []

    def builtinModels() -> list[AnyModel]:
        getAllModels = getattr(provider, "getAllModels", None)
        return list(getAllModels() if getAllModels is not None else provider.getModels())

    def getAllModels() -> list[AnyModel]:
        return list(personalModels) if len(personalModels) > 0 else builtinModels()

    class _PersonalizedCatalogProvider:
        def __getattr__(self, name: str) -> Any:
            return getattr(provider, name)

        def getModels(self) -> list[AnyModel]:
            return [model for model in getAllModels() if isModelType(model, "chat")]

        def getAllModels(self) -> list[AnyModel]:
            return getAllModels()

        async def refreshModels(self, context: RefreshModelsContext) -> None:
            restored = [
                model for model in (context.stored.models if context.stored is not None else []) if model.provider == provider.id
            ]

            def applyRestored() -> None:
                nonlocal personalModels
                personalModels = restored

            if not await context.publish(ModelsPublication(update=applyRestored)):
                return
            if not context.allowNetwork or signal_aborted(context.signal):
                return

            remote: list[AnyModel] = []
            # MISAKA fork: no catalog endpoint, no catalog request (see the docstring).
            if catalogBaseUrl is not None:
                response = await _requestCatalog(provider.id, catalogBaseUrl, context.signal)
                if signal_aborted(context.signal):
                    return
                if response.status != 404 and response.status != 501:
                    if not response.ok:
                        raise RuntimeError(f"Model catalog request failed for {provider.id}: {response.status}")
                    catalog = await _readCatalog(provider.id, response)
                    if _isNewer(catalog.lastModified, localGeneratedAt):
                        remote = catalog.models
            personalized = list(await personalize(_mergeModels(builtinModels(), remote), context))
            if signal_aborted(context.signal):
                return

            def applyPersonalized() -> None:
                nonlocal personalModels
                personalModels = personalized

            await context.publish(
                ModelsPublication(
                    persist=ModelsStoreEntry(models=personalized, checkedAt=_now()),
                    persist_is_set=True,
                    update=applyPersonalized,
                )
            )

    return _PersonalizedCatalogProvider()


__all__ = [
    "DEFAULT_CATALOG_BASE_URL",
    "REMOTE_CATALOG_MODEL_TYPES",
    "REMOTE_CATALOG_REFRESH_INTERVAL_MS",
    "PersonalizeCatalog",
    "withPersonalizedCatalog",
    "withRemoteCatalog",
]
