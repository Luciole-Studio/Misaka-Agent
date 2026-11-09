"""Translated from pi's ``packages/ai/src/api/cloudflare-workers-ai-system-one.ts`` (v1.1.0).

MISAKA fork: the request URL is a string (no ``URL`` type), and ``output`` is a named function because a lambda cannot
raise.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from misaka.ai.api.classifier_shared import isRecord
from misaka.ai.api.system_one_shared import SystemOneTransport, classifySystemOne
from misaka.ai.types import ClassifierContext, ClassifierModel, ClassifierResult
from misaka.utils.js import UNDEFINED, jsString

_LABEL = "Cloudflare Workers AI"


def _cloudflareErrorMessage(errors: Any) -> str:
    if isinstance(errors, list):
        messages = [
            error["message"] for error in errors if isRecord(error) and isinstance(error.get("message"), str)
        ]
        if len(messages) > 0:
            return f"{_LABEL} error: {'; '.join(messages)}"
    return f"{_LABEL} request failed"


def _output(body: Any) -> dict[str, Any]:
    if not isRecord(body):
        raise RuntimeError(f"{_LABEL} returned an unexpected response")
    if body.get("success") is False:
        raise RuntimeError(_cloudflareErrorMessage(body.get("errors")))
    result = body.get("result")
    if not isRecord(result):
        raise RuntimeError(f"{_LABEL} returned an unexpected response")
    if "answers" in result:
        return result
    if result.get("state") != "Completed":
        raise RuntimeError(f"{_LABEL} run did not complete (state: {jsString(result.get('state', UNDEFINED))})")
    if not isRecord(result.get("result")):
        raise RuntimeError(f"{_LABEL} returned an unexpected response")
    return result["result"]


# System One models on the Workers AI REST endpoint:
# `POST /accounts/{account}/ai/run` with `{ model, input }`. The REST API wraps the model
# output in Cloudflare's API envelope. Third-party models such as `typesafe/jev` add a run record:
# `{ success, result: { state: "Completed", result: { answers, usage } } }`.
# https://developers.cloudflare.com/ai/models/typesafe/jev/
# Cloudflare-hosted models such as `@cf/cloudflare/clef` return the output directly:
# `{ success, result: { model, answers, usage } }`.
# https://developers.cloudflare.com/workers-ai/models/clef/
_transport = SystemOneTransport(
    api="cloudflare-workers-ai-system-one",
    label=_LABEL,
    url=lambda model: urljoin(f"{model.baseUrl.rstrip('/')}/", "run"),
    payload=lambda model, request: {"model": model.id, "input": request},
    output=_output,
)


async def classify(model: ClassifierModel, context: ClassifierContext, options: Any = None) -> ClassifierResult:
    """Cloudflare Workers AI System One classification with public `bool` values mapped to wire-level `noul`."""
    return await classifySystemOne(_transport, model, context, options)


__all__ = ["classify"]
