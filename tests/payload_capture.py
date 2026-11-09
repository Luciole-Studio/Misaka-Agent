"""pi's ``capturePayload`` test helper: run a request through the stream facade and stop it at ``onPayload``."""

from __future__ import annotations

from typing import Any

from misaka.ai.compat import stream, streamSimple
from misaka.ai.types import Context, Model, SimpleStreamOptions


class PayloadCaptured(Exception):
    def __init__(self) -> None:
        super().__init__("payload captured")


def _capturing(options: dict[str, Any] | None, captured: list[Any]) -> dict[str, Any]:
    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured()

    return {**(options or {}), "apiKey": "fake-key", "onPayload": onPayload}


async def captureSimplePayload(model: Model, context: Context, options: dict[str, Any] | None = None) -> Any:
    """``streamSimple(model, context, {...options, apiKey: "fake-key", onPayload})``."""
    captured: list[Any] = []
    await streamSimple(model, context, SimpleStreamOptions(**_capturing(options, captured))).result()
    if not captured:
        raise RuntimeError("Expected payload to be captured before request failure")
    return captured[0]


async def capturePayload(model: Model, context: Context, options: dict[str, Any] | None = None) -> Any:
    """``stream(model, context, {...options, apiKey: "fake-key", onPayload})``: the API's own options."""
    captured: list[Any] = []
    await stream(model, context, _capturing(options, captured)).result()
    if not captured:
        raise RuntimeError("Expected payload to be captured before request failure")
    return captured[0]
