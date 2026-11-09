"""Translated from pi's ``packages/ai/src/providers/typesafe.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.typesafe_system_one_lazy import typesafeSystemOneApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.typesafe_models import TYPESAFE_CLASSIFIER_MODELS


def typesafeProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="typesafe",
            name="TypeSafe",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("TypeSafe API key", ["TYPESAFE_API_KEY"]),
            ),
            models=list(TYPESAFE_CLASSIFIER_MODELS.values()),
            classifiers={
                "typesafe-system-one": typesafeSystemOneApi(),
            },
        )
    )


__all__ = ["typesafeProvider"]
