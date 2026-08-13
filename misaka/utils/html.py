"""Small HTML entity decoding helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DecodedHtmlEntity:
    text: str
    length: int


def decode_html_entity(entity: str) -> str | None:
    if entity == "amp":
        return "&"
    if entity == "lt":
        return "<"
    if entity == "gt":
        return ">"
    if entity == "quot":
        return '"'
    if entity == "apos":
        return "'"
    if entity.startswith(("#x", "#X")):
        try:
            return _decode_code_point(int(entity[2:], 16))
        except ValueError:
            return None
    if entity.startswith("#"):
        try:
            return _decode_code_point(int(entity[1:], 10))
        except ValueError:
            return None
    return None


def decode_html_entity_at(html: str, index: int) -> DecodedHtmlEntity | None:
    semicolon_index = html.find(";", index + 1)
    if semicolon_index == -1 or semicolon_index - index > 16:
        return None

    entity = html[index + 1 : semicolon_index]
    decoded = decode_html_entity(entity)
    if decoded is None:
        return None
    return DecodedHtmlEntity(text=decoded, length=semicolon_index - index + 1)


def _decode_code_point(code_point: int) -> str | None:
    if code_point < 0 or code_point > 0x10FFFF:
        return None
    try:
        return chr(code_point)
    except ValueError:
        return None


decodeHtmlEntity = decode_html_entity
decodeHtmlEntityAt = decode_html_entity_at

__all__ = [
    "DecodedHtmlEntity",
    "decodeHtmlEntity",
    "decodeHtmlEntityAt",
]
