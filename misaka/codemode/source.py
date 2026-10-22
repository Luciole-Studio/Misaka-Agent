"""Codemode source format: JavaScript, optionally preceded by one options line.

Translated from pi's ``packages/codemode/src/source.ts``.

.. code-block:: js

    // @options: {"max_output_tokens": 2000, "timeout_ms": 30000}
    const text = await tools.read({ path: "package.json" });
    text(JSON.parse(text).name);
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from misaka.utils.js import UNDEFINED, jsonParse, jsTrim, jsTrimStart, objectKeys

CODEMODE_OPTIONS_PREFIX = "// @options:"

SUPPORTED_FIELDS = ("max_output_tokens", "timeout_ms")
SUPPORTED_FIELDS_TEXT = "`max_output_tokens` and `timeout_ms`"
# Largest delay `setTimeout` supports, which bounds `timeout_ms`.
MAX_TIMEOUT_MS = 2_147_483_647
_MAX_SAFE_INTEGER = 2**53 - 1

# Lark grammar for providers with grammar-constrained tool input. It only fixes the shape of the
# options line; the options JSON and the code are checked by `parseCodemodeSource`.
CODEMODE_SOURCE_GRAMMAR = r"""
start: options_source | plain_source
options_source: OPTIONS_LINE NEWLINE SOURCE
plain_source: SOURCE

OPTIONS_LINE: /[ \t]*\/\/ @options:[^\r\n]*/
NEWLINE: /\r?\n/
SOURCE: /[\s\S]+/
"""


class CodemodeSourceOptions(TypedDict, total=False):
    # Token budget for the script's output.
    maxOutputTokens: int
    # Hard deadline for the whole script in milliseconds, including tool calls.
    timeoutMs: int


class ParsedCodemodeSource(TypedDict):
    # The script with the options line replaced by an empty line, so line numbers are unchanged.
    code: str
    options: CodemodeSourceOptions


class CodemodeSourceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.name = "CodemodeSourceError"


def _isSafeInteger(value: Any) -> bool:
    """``typeof value === "number" && Number.isSafeInteger(value) && value >= 0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # An integer literal beyond the double range is an exact `int` here, where `JSON.parse` makes it
    # `Infinity`; it is out of range either way.
    if isinstance(value, int):
        return 0 <= value <= _MAX_SAFE_INTEGER
    return value.is_integer() and 0 <= value <= _MAX_SAFE_INTEGER


def _parseOptions(directive: str) -> CodemodeSourceOptions:
    if directive == "":
        raise CodemodeSourceError(f"@options must be a JSON object with supported fields {SUPPORTED_FIELDS_TEXT}")
    try:
        value: Any = jsonParse(directive)
    except ValueError as error:
        raise CodemodeSourceError(
            f"@options must be valid JSON with supported fields {SUPPORTED_FIELDS_TEXT}: {error}"
        ) from None
    if not isinstance(value, dict):
        raise CodemodeSourceError(f"@options must be a JSON object with supported fields {SUPPORTED_FIELDS_TEXT}")
    fields: dict[str, Any] = value
    # MISAKA fork: the syntax error detail is Python's, not V8's.
    for key in objectKeys(fields):
        if key not in SUPPORTED_FIELDS:
            raise CodemodeSourceError(f"@options only supports {SUPPORTED_FIELDS_TEXT}; got `{key}`")
    options: CodemodeSourceOptions = {}
    max_output_tokens = fields.get("max_output_tokens", UNDEFINED)
    timeout_ms = fields.get("timeout_ms", UNDEFINED)
    if max_output_tokens is not UNDEFINED:
        if not _isSafeInteger(max_output_tokens):
            raise CodemodeSourceError("@options field `max_output_tokens` must be a non-negative safe integer")
        options["maxOutputTokens"] = int(max_output_tokens)
    if timeout_ms is not UNDEFINED:
        if not _isSafeInteger(timeout_ms) or timeout_ms == 0 or timeout_ms > MAX_TIMEOUT_MS:
            raise CodemodeSourceError(f"@options field `timeout_ms` must be a positive integer up to {MAX_TIMEOUT_MS}")
        options["timeoutMs"] = int(timeout_ms)
    return options


def parseCodemodeSource(input: str) -> ParsedCodemodeSource:
    """Split an optional first-line ``// @options: {...}`` from the script. Raises
    :class:`CodemodeSourceError` for empty input, invalid options, and an options line that is not the
    first line, which would otherwise be ignored without notice."""
    if jsTrim(input) == "":
        raise CodemodeSourceError(
            'Expected JavaScript source text (non-empty). Provide JS only, optionally with a first line `// @options: {"max_output_tokens": 1000}`.'
        )
    lines = input.split("\n")
    misplaced = next(
        (index for index, line in enumerate(lines) if index > 0 and jsTrimStart(line).startswith(CODEMODE_OPTIONS_PREFIX)),
        -1,
    )
    if misplaced != -1:
        raise CodemodeSourceError(
            f"The {CODEMODE_OPTIONS_PREFIX} line must be the first line of the input; found one on line {misplaced + 1}"
        )
    newline = input.find("\n")
    firstLine = re.sub(r"\r$", "", lines[0])
    trimmed = jsTrimStart(firstLine)
    if not trimmed.startswith(CODEMODE_OPTIONS_PREFIX):
        return {"code": input, "options": {}}
    code = "" if newline == -1 else input[newline:]
    if jsTrim(code) == "":
        raise CodemodeSourceError("The @options line must be followed by JavaScript source on subsequent lines")
    return {"code": code, "options": _parseOptions(jsTrim(trimmed[len(CODEMODE_OPTIONS_PREFIX):]))}


__all__ = [
    "CODEMODE_OPTIONS_PREFIX",
    "CODEMODE_SOURCE_GRAMMAR",
    "CodemodeSourceError",
    "CodemodeSourceOptions",
    "ParsedCodemodeSource",
    "parseCodemodeSource",
]
