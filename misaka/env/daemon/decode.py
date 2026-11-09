"""WHATWG UTF-8 decoding that matches Node's ``new TextDecoder().decode(bytes)`` of a whole input when the input arrives
in pieces (Durable ``env/decode.ts``): invalid sequences become U+FFFD by the maximal-subpart rule, and only a byte-order
mark at the very start is dropped.

MISAKA fork: Rust uses ``encoding_rs``; this is the WHATWG decoder's state machine (``misaka.utils.js.Utf8Decoder``),
copied because the daemon imports nothing outside its package.
"""

from __future__ import annotations


class Decoder:
    """``encoding_rs::Decoder`` for UTF-8 in streaming use. ``remove_bom``: drop a byte-order mark at the start of the
    stream (``new_decoder_with_bom_removal``); otherwise no BOM handling at all."""

    def __init__(self, remove_bom: bool) -> None:
        self._remove_bom = remove_bom
        self._started = False
        self._needed = 0
        self._seen = 0
        self._lower = 0x80
        self._upper = 0xBF
        self._code_point = 0

    def _reset(self) -> None:
        self._needed = self._seen = self._code_point = 0
        self._lower, self._upper = 0x80, 0xBF

    def _consume(self, data: bytes, position: int, out: list[str]) -> int:
        """Run the state machine from ``position`` until it is at a character boundary again; returns the position."""
        while position < len(data):
            byte = data[position]
            if self._needed == 0:
                position += 1
                if byte <= 0x7F:
                    out.append(chr(byte))
                    return position
                if 0xC2 <= byte <= 0xDF:
                    self._needed, self._code_point = 1, byte & 0x1F
                elif 0xE0 <= byte <= 0xEF:
                    if byte == 0xE0:
                        self._lower = 0xA0
                    elif byte == 0xED:
                        self._upper = 0x9F
                    self._needed, self._code_point = 2, byte & 0xF
                elif 0xF0 <= byte <= 0xF4:
                    if byte == 0xF0:
                        self._lower = 0x90
                    elif byte == 0xF4:
                        self._upper = 0x8F
                    self._needed, self._code_point = 3, byte & 0x7
                else:
                    out.append("�")
                    return position
                continue
            if not self._lower <= byte <= self._upper:
                # The byte is not part of the sequence: it is replaced, and read again as the start of the next one.
                self._reset()
                out.append("�")
                return position
            self._lower, self._upper = 0x80, 0xBF
            self._code_point = (self._code_point << 6) | (byte & 0x3F)
            self._seen += 1
            position += 1
            if self._seen == self._needed:
                out.append(chr(self._code_point))
                self._reset()
                return position
        return position

    def decode_to_string(self, data: bytes, last: bool) -> str:
        out: list[str] = []
        position = 0
        while position < len(data):
            if self._needed == 0:
                try:
                    out.append(data[position:].decode("utf-8"))
                    position = len(data)
                    break
                except UnicodeDecodeError as error:
                    if error.start > 0:
                        out.append(data[position : position + error.start].decode("utf-8"))
                        position += error.start
            position = self._consume(data, position, out)
        if last and self._needed != 0:
            self._reset()
            out.append("�")
        text = "".join(out)
        if self._remove_bom and not self._started and text:
            self._started = True
            # U+FEFF encodes only as EF BB BF, so a leading U+FEFF is exactly a byte-order mark at the stream's start.
            text = text.removeprefix("﻿")
        return text


class StreamDecoder:
    """Decodes one stream chunk by chunk, dropping a leading byte-order mark."""

    def __init__(self) -> None:
        # Removes a UTF-8 BOM only; it never sniffs UTF-16 BOMs and switches encodings.
        self.decoder = Decoder(remove_bom=True)

    def decode(self, data: bytes, last: bool) -> str:
        """Text for ``data``, holding back an incomplete character; with ``last``, the end of the stream."""
        return decode_into(self.decoder, data, last)


def range_decoder() -> Decoder:
    """A decoder for a byte range that does not start at the beginning of its input: no BOM handling at all."""
    return Decoder(remove_bom=False)


def decode_into(decoder: Decoder, data: bytes, last: bool) -> str:
    return decoder.decode_to_string(data, last)


def starts_with_bom(data: bytes) -> bool:
    """Whether decoding the whole input drops its first three bytes as a byte-order mark."""
    return data[:3] == b"\xef\xbb\xbf"
