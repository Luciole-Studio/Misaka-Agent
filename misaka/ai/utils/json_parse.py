"""Translated from pi's ``packages/ai/src/utils/json-parse.ts`` (v1.3.0).

MISAKA fork: ``partial-json``'s ``parse`` is ``misaka.ai.utils._partial_json.parse`` (no Python port of the npm
package); ``JSON.parse`` is ``misaka.utils.js.jsonParse``; ``parseJsonWithRepair`` has one more step,
``_escape_stray_quotes``, after pi's repair.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from misaka.ai.utils._partial_json import parse as partialParse
from misaka.utils.js import jsonParse, jsTrim

_VALID_JSON_ESCAPES = frozenset({'"', "\\", "/", "b", "f", "n", "r", "t"})


def _isControlCharacter(char: str) -> bool:
    codePoint = ord(char) if char else None
    return codePoint is not None and 0x00 <= codePoint <= 0x1F


def _escapeControlCharacter(char: str) -> str:
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


def repairJson(json: str) -> str:
    """Repairs malformed JSON string literals by:
    - escaping raw control characters inside strings
    - doubling backslashes before invalid escape characters
    """
    repaired: list[str] = []
    inString = False

    # MISAKA fork: a while loop, since a Python for loop cannot skip ahead by changing its index.
    index = 0
    while index < len(json):
        char = json[index]

        if not inString:
            repaired.append(char)
            if char == '"':
                inString = True
            index += 1
            continue

        if char == '"':
            repaired.append(char)
            inString = False
            index += 1
            continue

        if char == "\\":
            nextChar = json[index + 1] if index + 1 < len(json) else None
            if nextChar is None:
                repaired.append("\\\\")
                index += 1
                continue

            if nextChar == "u":
                unicodeDigits = json[index + 2 : index + 6]
                if len(unicodeDigits) == 4 and all(digit in "0123456789abcdefABCDEF" for digit in unicodeDigits):
                    repaired.append(f"\\u{unicodeDigits}")
                    index += 6
                    continue

            if nextChar in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{nextChar}")
                index += 2
                continue

            repaired.append("\\\\")
            index += 1
            continue

        repaired.append(_escapeControlCharacter(char) if _isControlCharacter(char) else char)
        index += 1

    return "".join(repaired)


def _escape_stray_quotes(json: str) -> str:
    """MISAKA fork: escape a double quote that sits inside a string value.

    A relaying gateway delivered Claude tool arguments whose string values held literal
    ``"`` where the wire needs ``\\"`` while every ``\\n`` in them was intact (2026-09-23,
    six `edit`/`office` calls through sub2api, all Chinese prose quoting a term). pi's repair
    covers escapes and control characters, not this. Inside a string, a quote that is not
    followed -- after optional whitespace -- by ``,`` ``:`` ``}`` ``]`` or the end of the text
    cannot be the closing quote, so it is escaped; everything else is left exactly as it was.
    Only reached after the strict parse and pi's repair have both failed.
    """
    out: list[str] = []
    inString = False
    index = 0
    length = len(json)
    while index < length:
        char = json[index]
        if not inString:
            if char == '"':
                inString = True
            out.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            out.append(json[index : index + 2])
            index += 2
            continue
        if char == '"':
            after = index + 1
            while after < length and json[after] in " \t\r\n":
                after += 1
            if after >= length or json[after] in ",:}]":
                inString = False
                out.append(char)
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parseJsonWithRepair(json: str) -> Any:
    try:
        return jsonParse(json)
    except ValueError:
        repairedJson = repairJson(json)
        if repairedJson != json:
            try:
                return jsonParse(repairedJson)
            except ValueError:
                pass
        # MISAKA fork: see _escape_stray_quotes.
        requoted = _escape_stray_quotes(repairedJson)
        if requoted != repairedJson:
            return jsonParse(requoted)
        raise


class _ToolCallArguments(TypedDict):
    """MISAKA fork: ``Pick<ToolCall, "arguments" | "argumentsError">``, named."""

    arguments: dict[str, Any]
    argumentsError: NotRequired[str]


def parseToolCallArguments(json: str | None) -> _ToolCallArguments:
    """Parses the model's complete arguments text of a finished tool call.

    Unlike :func:`parseStreamingJson`, this never keeps a partially parsed prefix: once the call is
    finished, text that is not a valid JSON object means the model sent malformed arguments. In that
    case `arguments` is `{}` and `argumentsError` describes the problem, including the text the
    model sent, so tool validation can report it back to the model.
    """
    if not json or not jsTrim(json):
        return {"arguments": {}}
    try:
        parsed = parseJsonWithRepair(json)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object")  # noqa: TRY004 - upstream's `throw new Error`
        return {"arguments": parsed}
    except Exception as error:  # noqa: BLE001 - a parse error normally, RecursionError when nesting is pathological
        return {"arguments": {}, "argumentsError": f"{error}\n\nReceived arguments:\n{json}"}


def parseStreamingJson(partialJson: str | None) -> Any:
    """Attempts to parse potentially incomplete JSON during streaming.
    Always returns a valid object, even if the JSON is incomplete.

    Returns the parsed object, or an empty object if parsing fails.
    """
    if not partialJson or jsTrim(partialJson) == "":
        return {}

    try:
        return parseJsonWithRepair(partialJson)
    except Exception:  # noqa: BLE001 - a parse error normally, RecursionError when nesting is pathological
        try:
            result = partialParse(partialJson)
            return {} if result is None else result
        except Exception:  # noqa: BLE001 - as above
            try:
                result = partialParse(repairJson(partialJson))
                return {} if result is None else result
            except Exception:  # noqa: BLE001 - as above
                return {}


__all__ = ["parseJsonWithRepair", "parseStreamingJson", "parseToolCallArguments", "repairJson"]
