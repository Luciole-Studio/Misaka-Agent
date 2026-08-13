"""Helpers for repairing and incrementally parsing provider JSON fragments."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from json_repair import repair_json as repair_json_with_library

TJson = TypeVar("TJson")

_VALID_JSON_ESCAPES = frozenset({'"', "\\", "/", "b", "f", "n", "r", "t", "u"})


def _is_control_character(char: str) -> bool:
    if not char:
        return False
    code_point = ord(char)
    return 0x00 <= code_point <= 0x1F


def _escape_control_character(char: str) -> str:
    if char == "\b":
        return "\\b"
    if char == "\f":
        return "\\f"
    if char == "\n":
        return "\\n"
    if char == "\r":
        return "\\r"
    if char == "\t":
        return "\\t"
    return f"\\u{ord(char):04x}"


def repair_json(json_string: str) -> str:
    repaired: list[str] = []
    in_string = False
    index = 0

    while index < len(json_string):
        char = json_string[index]

        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            index += 1
            continue

        if char == "\\":
            next_char = json_string[index + 1] if index + 1 < len(json_string) else None
            if next_char is None:
                repaired.append("\\\\")
                index += 1
                continue

            if next_char == "u":
                unicode_digits = json_string[index + 2 : index + 6]
                if len(unicode_digits) == 4 and all(digit in "0123456789abcdefABCDEF" for digit in unicode_digits):
                    repaired.append(f"\\u{unicode_digits}")
                    index += 6
                    continue

            if next_char in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{next_char}")
                index += 2
                continue

            repaired.append("\\\\")
            index += 1
            continue

        repaired.append(_escape_control_character(char) if _is_control_character(char) else char)
        index += 1

    return "".join(repaired)


def parse_json_with_repair(json_string: str) -> TJson:
    try:
        return json.loads(json_string)
    except json.JSONDecodeError:
        repaired_json = repair_json(json_string)
        if repaired_json != json_string:
            return json.loads(repaired_json)
        raise


def _partial_parse_json(json_string: str) -> Any:
    return repair_json_with_library(
        json_string,
        return_objects=True,
        skip_json_loads=True,
        stream_stable=True,
    )


def _recover_partial_top_level_string(json_string: str) -> str | None:
    trimmed = json_string.lstrip()
    if not trimmed or trimmed[0] != '"':
        return None
    if trimmed.rstrip().endswith('"'):
        return None
    try:
        recovered = parse_json_with_repair(f'{json_string}"')
    except Exception:
        return None
    return recovered if isinstance(recovered, str) else None


def _coalesce_partial_parse_result(result: Any, partial_json: str) -> Any:
    if result is None:
        return {}
    if result == "":
        recovered = _recover_partial_top_level_string(partial_json)
        if recovered is not None:
            return recovered
    return result


def parse_streaming_json(partial_json: str | None) -> TJson:
    if partial_json is None or partial_json.strip() == "":
        return {}

    try:
        return parse_json_with_repair(partial_json)
    except Exception:
        try:
            result = _partial_parse_json(partial_json)
            return _coalesce_partial_parse_result(result, partial_json)
        except Exception:
            try:
                result = _partial_parse_json(repair_json(partial_json))
                return _coalesce_partial_parse_result(result, partial_json)
            except Exception:
                return {}


repairJson = repair_json
parseJsonWithRepair = parse_json_with_repair
parseStreamingJson = parse_streaming_json

__all__ = [
    "repairJson",
    "parseJsonWithRepair",
    "parseStreamingJson",
]
