"""The worker thread of ``createNodePhotonImages()``: images are prepared here, off the caller's event loop.

Translated from pi's ``packages/durable/src/images/node-worker.ts`` (v1.3.0). MISAKA fork: a Node worker thread is a
module that listens on its ``parentPort``; here the worker is a Python thread (see ``node.py``) that calls ``handle()``
for each request it takes from its queue, and the reply goes back to the caller's event loop. Pillow releases the GIL
while it decodes, resizes and encodes, so the loop keeps running meanwhile.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ..errors import errorMessage
from ..tools.image_processor import ImageLimits
from . import prepare


class PrepareRequest(TypedDict):
    """One request: the image to prepare, and its ID for the reply."""

    id: int
    bytes: bytes
    mimeType: str
    limits: ImageLimits


class PrepareReply(TypedDict):
    """The reply to a request: its ``result`` (``None`` when the image cannot be prepared), or why it failed
    (``error``)."""

    id: int
    result: NotRequired[Any]
    error: NotRequired[str]


def handle(request: PrepareRequest) -> PrepareReply:
    """Prepare one request's image, on the worker thread."""
    try:
        return {"id": request["id"], "result": prepare(request["bytes"], request["mimeType"], request["limits"])}
    except Exception as error:  # noqa: BLE001 - a failing image is that request's reply
        return {"id": request["id"], "error": errorMessage(error)}


__all__ = ["PrepareReply", "PrepareRequest", "handle"]
