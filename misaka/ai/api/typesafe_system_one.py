"""Translated from pi's ``packages/ai/src/api/typesafe-system-one.ts`` (v1.1.0).

MISAKA fork: the request URL is a string (no ``URL`` type), and ``output`` is a named function because a lambda cannot
raise.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from misaka.ai.api.classifier_shared import isRecord
from misaka.ai.api.system_one_shared import SystemOneTransport, classifySystemOne
from misaka.ai.types import ClassifierContext, ClassifierModel, ClassifierResult


def _output(body: Any) -> dict[str, Any]:
    if not isRecord(body):
        raise RuntimeError("System One API returned an unexpected response")
    return body


# TypeSafe's native System One protocol. OpenRouter serves the same protocol,
# so both providers use this API with different base URLs.
_transport = SystemOneTransport(
    api="typesafe-system-one",
    label="System One API",
    url=lambda model: urljoin(f"{model.baseUrl.rstrip('/')}/", "systemone"),
    payload=lambda model, request: {"model": model.id, **request},
    output=_output,
)


async def classify(model: ClassifierModel, context: ClassifierContext, options: Any = None) -> ClassifierResult:
    """TypeSafe System One classification with public `bool` values mapped to wire-level `noul`."""
    return await classifySystemOne(_transport, model, context, options)


__all__ = ["classify"]
