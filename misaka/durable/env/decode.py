"""Decoding that matches ``new TextDecoder().decode(bytes)`` of the whole input when the input arrives in pieces. Node's
streaming decoder with BOM handling can drop a U+FEFF that follows a chunk boundary, not only a leading byte-order
mark, so these decoders turn BOM handling off and drop a leading mark themselves.

Ported from: pi packages/durable/src/env/decode.ts @ v1.3.0
"""

from __future__ import annotations

from misaka.utils.js import Utf8Decoder


def rangeDecoder() -> Utf8Decoder:
    """A streaming decoder for a byte range; callers that start at the beginning of a file skip a leading mark themselves."""
    return Utf8Decoder(ignoreBOM=True)


def startsWithBom(firstBytes: bytes) -> bool:
    """Whether decoding the whole input drops its first three bytes as a byte-order mark."""
    return firstBytes[:3] == b"\xef\xbb\xbf"


class StreamDecoder:
    """Decodes one stream chunk by chunk exactly like decoding all of it at once."""

    def __init__(self) -> None:
        self._decoder = rangeDecoder()
        self._started = False

    def decode(self, bytes_: bytes | None = None) -> str:
        """Text for ``bytes_``, holding back an incomplete character; without ``bytes_``, the end of the stream."""
        text = self._decoder.decode(b"", final=True) if bytes_ is None else self._decoder.decode(bytes_)
        if self._started or text == "":
            return text
        self._started = True
        # U+FEFF encodes only as EF BB BF, so a leading U+FEFF is exactly a byte-order mark at the stream's start.
        return text.removeprefix("﻿")


__all__ = ["StreamDecoder", "rangeDecoder", "startsWithBom"]
