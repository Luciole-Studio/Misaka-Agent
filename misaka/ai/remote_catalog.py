"""A persisted remote overlay on a static provider's model catalog.

Translated from pi's ``coding-agent/src/core/remote-catalog-provider.ts``. The built-in
catalog is a transcription of upstream's data at one moment; providers add models
continuously, and without a refresh path the table drifts silently. It already had: when
this port was measured against ``@earendil-works/pi-ai@0.84.3`` the hand-maintained table
was missing 497 models and priced 253 of them wrong, and nothing in the repository could
have reported that.

**The endpoint is not defaulted.** Upstream points at ``https://pi.dev``, its own service.
Defaulting to it here would send this install's traffic to somebody else's infrastructure
and make startup depend on their uptime -- the same reasoning that gave
``core/provider_attribution.py`` its own client name. So the overlay is inert until a
caller supplies ``catalog_base_url``: misaka gains the capability without quietly
acquiring the dependency.

What the overlay guarantees, which is the part worth stating: a fetched catalog is applied
only when it is *newer* than the built-in generation stamp, so a stale or empty endpoint
can never subtract models that shipped.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.types import Model
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import signal_aborted

REMOTE_CATALOG_REFRESH_INTERVAL_MS = 4 * 60 * 60 * 1000
REMOTE_CATALOG_ATTEMPT_TIMEOUT_MS = 4_000

FetchCatalog = Callable[[str, dict[str, str], Any], Awaitable[httpx.Response]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _merge_models(baseline: Sequence[Model], dynamic: Sequence[Model]) -> list[Model]:
    """The overlay replaces a baseline model by id and appends what is new."""
    merged = list(baseline)
    by_id = {model.id: index for index, model in enumerate(merged)}
    for model in dynamic:
        index = by_id.get(model.id)
        if index is None:
            by_id[model.id] = len(merged)
            merged.append(model)
        else:
            merged[index] = model
    return merged


def parse_catalog(provider_id: str, value: Any) -> list[Model]:
    """Read a catalog body in any of the three shapes upstream accepts."""
    if isinstance(value, list):
        entries: Any = value
    elif isinstance(value, dict) and isinstance(value.get("models"), list):
        entries = value["models"]
    elif isinstance(value, dict):
        entries = list(value.values())
    else:
        # A malformed body from a catalog endpoint is a protocol failure, not a caller
        # type error, and the refresh path reports every other failure the same way.
        raise RuntimeError(f'Invalid model catalog for provider "{provider_id}"')  # noqa: TRY004
    return [
        Model.model_validate({**entry, "provider": provider_id})
        for entry in entries
        if isinstance(entry, dict) and "id" in entry
    ]


def _remote_models(entry: ModelsStoreEntry | None, local_generated_at: int | None) -> list[Model]:
    """What of a stored entry may be applied over the built-in catalog.

    Nothing, when the remote data is no newer than what shipped -- an endpoint that has
    fallen behind must not roll the catalog backwards.
    """
    if entry is None:
        return []
    if local_generated_at is not None and (
        entry.lastModified is None or entry.lastModified <= local_generated_at
    ):
        return []
    return list(entry.models)


async def _default_fetch(url: str, headers: dict[str, str], signal: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=REMOTE_CATALOG_ATTEMPT_TIMEOUT_MS / 1000) as client:
        return await client.get(url, headers=headers)


def with_remote_catalog(
    provider: Any,
    catalog_base_url: str | None = None,
    local_generated_at: int | None = None,
    fetch: FetchCatalog | None = None,
) -> Any:
    """Wrap ``provider`` so its catalog can be topped up from a remote endpoint.

    With no ``catalog_base_url`` the provider is returned unchanged -- there is no
    endpoint this project may fetch from by default, and a wrapper that fetches from
    nowhere is worse than no wrapper.
    """
    if not catalog_base_url:
        return provider

    send = fetch or _default_fetch
    dynamic_models: list[Model] = []

    class _RemoteCatalogProvider:
        """Delegates everything except the two catalog methods."""

        def __getattr__(self, name: str) -> Any:
            return getattr(provider, name)

        def getModels(self) -> list[Model]:
            return _merge_models(provider.getModels(), dynamic_models)

        async def refreshModels(self, context: Any) -> None:
            nonlocal dynamic_models
            stored = context.stored
            restored = [
                model
                for model in _remote_models(stored, local_generated_at)
                if model.provider == provider.id
            ]

            def _apply_restored() -> None:
                nonlocal dynamic_models
                dynamic_models = restored

            from misaka.ai.models_runtime import ModelsPublication

            if not await context.publish(ModelsPublication(update=_apply_restored)):
                return
            if not context.allowNetwork or signal_aborted(context.signal):
                return
            if (
                not context.force
                and stored is not None
                and stored.checkedAt is not None
                and stored.lastModified is not None
                and _now_ms() - stored.checkedAt < REMOTE_CATALOG_REFRESH_INTERVAL_MS
            ):
                return

            # Revalidate only when a cached body backs the validator, so a 304 can never
            # leave the overlay empty.
            validator = stored.etag if stored is not None and stored.models else None
            url = urljoin(catalog_base_url, f"/api/models/providers/{quote(provider.id, safe='')}")
            headers = {"accept": "application/json", "User-Agent": get_misaka_user_agent()}
            if validator:
                headers["if-none-match"] = validator
            response = await send(url, headers, context.signal)
            if signal_aborted(context.signal):
                return
            checked_at = _now_ms()

            if response.status_code == 304 and stored is not None:
                # Unchanged: the overlay already holds the stored models, so only the
                # freshness window moves.
                await context.publish(
                    ModelsPublication(persist=stored.model_copy(update={"checkedAt": checked_at}))
                )
                return
            if response.status_code in (404, 501):
                base = stored or ModelsStoreEntry(models=[])
                await context.publish(
                    ModelsPublication(
                        persist=base.model_copy(
                            update={"checkedAt": checked_at, "lastModified": 0, "etag": None}
                        )
                    )
                )
                return
            if not response.is_success:
                # Transient: the cached body and its validator stay valid, so the next
                # refresh revalidates instead of downloading the whole catalog again.
                base = stored or ModelsStoreEntry(models=[])
                await context.publish(
                    ModelsPublication(persist=base.model_copy(update={"checkedAt": checked_at}))
                )
                raise RuntimeError(
                    f"Model catalog request failed for {provider.id}: {response.status_code}"
                )

            refreshed = parse_catalog(provider.id, response.json())
            last_modified = _parse_http_date_ms(response.headers.get("last-modified"))
            if signal_aborted(context.signal):
                return
            entry = ModelsStoreEntry(
                models=refreshed,
                checkedAt=checked_at,
                lastModified=last_modified,
                etag=response.headers.get("etag"),
            )
            published = _remote_models(entry, local_generated_at)

            def _apply_published() -> None:
                nonlocal dynamic_models
                dynamic_models = published

            await context.publish(ModelsPublication(persist=entry, update=_apply_published))

    return _RemoteCatalogProvider()


def _parse_http_date_ms(value: str | None) -> int:
    """``Date.parse`` of a Last-Modified header, or 0 when it is missing or unreadable."""
    if not value:
        return 0
    from email.utils import parsedate_to_datetime

    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "REMOTE_CATALOG_REFRESH_INTERVAL_MS",
    "parse_catalog",
    "with_remote_catalog",
]
