"""Translated from pi's ``packages/ai/src/providers/cloudflare-stream.ts`` (v1.1.0).

MISAKA fork: the returned ``ProviderStreams``/``ProviderClassifier`` objects are ``SimpleNamespace``s, the plain objects
upstream builds; ``options.env`` is read whether the options are a mapping or a pydantic model.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from misaka.ai.types import ProviderClassifier, ProviderEnv, ProviderStreams
from misaka.utils.js import nullishCoalesce
from misaka.utils.values import read_field

_CLOUDFLARE_ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
_CLOUDFLARE_GATEWAY_ID = "CLOUDFLARE_GATEWAY_ID"


def resolveCloudflareModel[TModel](model: TModel, env: ProviderEnv | None) -> TModel:
    if not env:
        return model
    baseUrl = model.baseUrl.replace(  # type: ignore[attr-defined]
        f"{{{_CLOUDFLARE_ACCOUNT_ID}}}", nullishCoalesce(env.get(_CLOUDFLARE_ACCOUNT_ID), f"{{{_CLOUDFLARE_ACCOUNT_ID}}}")
    ).replace(
        f"{{{_CLOUDFLARE_GATEWAY_ID}}}", nullishCoalesce(env.get(_CLOUDFLARE_GATEWAY_ID), f"{{{_CLOUDFLARE_GATEWAY_ID}}}")
    )
    return model if baseUrl == model.baseUrl else model.model_copy(update={"baseUrl": baseUrl})  # type: ignore[attr-defined]


def cloudflareStreams(streams: ProviderStreams) -> ProviderStreams:
    """Wrap an API implementation so Cloudflare account/gateway endpoint
    placeholders materialize from the resolved provider env before dispatch.
    """
    return SimpleNamespace(  # type: ignore[return-value]
        stream=lambda model, context, options=None: streams.stream(
            resolveCloudflareModel(model, read_field(options, "env")), context, options
        ),
        streamSimple=lambda model, context, options=None: streams.streamSimple(
            resolveCloudflareModel(model, read_field(options, "env")), context, options
        ),
    )


def cloudflareClassifier(classifier: ProviderClassifier) -> ProviderClassifier:
    """Classifier counterpart of ``cloudflareStreams``."""

    def classify(model: Any, context: Any, options: Any = None) -> Any:
        return classifier.classify(resolveCloudflareModel(model, read_field(options, "env")), context, options)

    return SimpleNamespace(classify=classify)  # type: ignore[return-value]


__all__ = ["cloudflareClassifier", "cloudflareStreams", "resolveCloudflareModel"]
