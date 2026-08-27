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
    except Exception:  # noqa: BLE001 - json_repair raises assorted errors on garbage; the fallback chain handles it
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
    except Exception:  # noqa: BLE001 - json_repair raises assorted errors on garbage; the fallback chain handles it
        try:
            result = _partial_parse_json(partial_json)
            return _coalesce_partial_parse_result(result, partial_json)
        except Exception:  # noqa: BLE001 - json_repair raises assorted errors on garbage; the fallback chain handles it
            try:
                result = _partial_parse_json(repair_json(partial_json))
                return _coalesce_partial_parse_result(result, partial_json)
            except Exception:  # noqa: BLE001 - json_repair raises assorted errors on garbage; the fallback chain handles it
                return {}


STREAMING_PARSE_THRESHOLD_BYTES = 2048


class StreamingArgs:
    """A tool call's arguments while they stream in: kept raw, parsed sparingly.

    Providers deliver tool arguments as JSON fragments, and every adapter used to hand
    ``parse_streaming_json`` the *whole* accumulated buffer again on every fragment.
    That parse is two to three full scans of the buffer (``json.loads``, a
    character-by-character repair pass, then ``json_repair``), so a 38KB Write argument
    arriving in 20-character fragments was parsed ~1950 times over growing prefixes --
    measured at 31s of loop-thread CPU for one tool call.

    The raw string is the durable half and is always exact: the fragments are appended
    verbatim, and ``finish()`` parses the complete buffer at the end of the block, which
    is the value the tool actually runs with. ``arguments`` is the *live view* in
    between, refreshed often enough for the UI and rarely enough to stay cheap.
    """

    __slots__ = ("_parsed_length", "_raw", "_value")

    def __init__(self, initial: str = "") -> None:
        self._raw = initial
        self._parsed_length = -1  # nothing parsed yet: -1 so an initial buffer still earns one
        self._value: Any = {}

    @property
    def raw(self) -> str:
        """Every fragment seen so far, concatenated and unaltered."""
        return self._raw

    def append(self, delta: str) -> None:
        if delta:
            self._raw += delta

    @property
    def arguments(self) -> Any:
        """The last parsed value, re-parsed first if enough new bytes have arrived."""
        if self._is_stale():
            self._parse()
        return self._value

    def finish(self) -> Any:
        """Parse the complete buffer. What a tool is handed has to be exact."""
        if self._parsed_length != len(self._raw):
            self._parse()
        return self._value

    def _is_stale(self) -> bool:
        total = len(self._raw)
        pending = total - self._parsed_length
        if pending <= 0:
            return False
        if total <= STREAMING_PARSE_THRESHOLD_BYTES:
            # Parsing a buffer this small costs microseconds, and the TUI renders tool
            # arguments while they stream -- a tool call under the threshold (nearly all
            # of them) must keep showing its path the moment it is spelled out.
            return True
        # Above it, wait for a fixed slice *and* for a fraction of what is already there.
        # The fixed slice alone would still parse len/slice prefixes of average len/2,
        # which is the same quadratic with a smaller constant; the fraction is what keeps
        # the total work proportional to the argument's length.
        return pending >= max(STREAMING_PARSE_THRESHOLD_BYTES, total // 8)

    def _parse(self) -> None:
        self._value = parse_streaming_json(self._raw)
        self._parsed_length = len(self._raw)


__all__ = [
    ]
