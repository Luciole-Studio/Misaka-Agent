"""pi chord's ``test/fixtures/counting.ts``: a link wrapper that counts what crosses it, per direction: frames by type and
bytes (JSON text length). It can also hold back the acknowledgements its own side sends (``paused``), to fill the peer's
flow-control window.

MISAKA adaptation: bytes are the length of ``JSON.stringify(frame)`` as JavaScript computes it, so a packed payload
counts as its ``Uint8Array`` would (an object of indexed bytes, ``frameLength``); ``jsonStringify`` refuses a ``Packed``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from misaka.chord.link import Link, Packed
from misaka.chord.wire import Frame
from misaka.utils.js import jsLength, jsonStringify


def _jsonShape(value: Any) -> Any:
    """What ``JSON.stringify`` sees: a ``Packed`` payload's ``Uint8Array`` is an object of its indexed bytes."""
    if isinstance(value, Packed):
        return {"bytes": {str(index): byte for index, byte in enumerate(value.bytes)}}
    if isinstance(value, dict):
        return {key: _jsonShape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonShape(item) for item in value]
    return value


def frameLength(frame: Frame) -> int:
    """``JSON.stringify(frame).length``."""
    return jsLength(jsonStringify(_jsonShape(frame)))


class CountingLink:
    def __init__(self, inner: Link) -> None:
        self.inner = inner
        # Frames this side sent, in order.
        self.sent: list[Frame] = []
        # Frames this side received, in order.
        self.received: list[Frame] = []
        self.sentBytes = 0
        self.receivedBytes = 0
        # Acknowledgements are held back (the peer's window fills up).
        self.paused = False
        self._held: Frame | None = None

    @property
    def closed(self) -> bool:
        return self.inner.closed

    def send(self, frame: Frame) -> int | None:
        if frame["t"] == "ack" and self.paused:
            self._held = frame
            return 0
        self.sent.append(frame)
        self.sentBytes += frameLength(frame)
        return self.inner.send(frame)

    def release(self) -> None:
        """Lets the held acknowledgement through and stops holding them."""
        self.paused = False
        held = self._held
        self._held = None
        if held is not None:
            self.inner.send(held)

    def receive(self, handler: Callable[[Frame], None]) -> None:
        def counted(frame: Frame) -> None:
            self.received.append(frame)
            self.receivedBytes += frameLength(frame)
            handler(frame)

        self.inner.receive(counted)

    def onClose(self, handler: Callable[[str], None]) -> None:
        self.inner.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self.inner.close(reason)

    def count(self, direction: str, t: str) -> int:
        """Frames of type ``t`` this side sent (``sent``) or received (``received``)."""
        return len([frame for frame in getattr(self, direction) if frame["t"] == t])

    def traffic(self, direction: str) -> list[Frame]:
        """Frames this side sent or received, except pings, pongs and acknowledgements."""
        return [frame for frame in getattr(self, direction) if frame["t"] not in ("ping", "pong", "ack")]


def stateFrames(frames: Sequence[Frame]) -> list[Frame]:
    return [frame for frame in frames if frame["t"] == "state"]


__all__ = ["CountingLink", "frameLength", "stateFrames"]
