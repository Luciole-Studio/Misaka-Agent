"""Helpers for repairing and incrementally parsing provider JSON fragments."""

from __future__ import annotations

import json
from typing import Any, TypeVar

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


_PARTIAL_LITERALS = ("true", "false", "null")


def _unterminated_escape_start(text: str) -> int | None:
    """Index of a trailing ``\\uXXX`` that never got its fourth hex digit."""
    start = text.rfind("\\u")
    if start < 0 or len(text) - start > 5:
        return None
    # Only a real escape counts: `\\\\u` is a literal backslash followed by `u`.
    backslashes = len(text[:start + 1]) - len(text[:start + 1].rstrip("\\"))
    if backslashes % 2 == 0:
        return None
    return start if all(c in "0123456789abcdefABCDEF" for c in text[start + 2:]) else None


def _complete_json_prefix(partial_json: str) -> str | None:
    """Close a truncated JSON prefix the way ``partial-json`` does, at every depth.

    Three rules, applied uniformly rather than only to the outermost object -- which is
    what an earlier revision did, leaving nested values wrong:

    * a key with no value yet is dropped (``{"path`` -> ``{}``, ``{"a": {"k`` -> ``{"a": {}}``)
    * a value that has begun is completed (``{"a": "he`` -> ``{"a": "he"}``)
    * a truncated bare literal is finished (``"ok": t`` -> ``true``); an ambiguous prefix
      is dropped instead of guessed

    Returns ``None`` when the text is already complete or does not start a container, so
    the existing repair chain keeps handling everything this does not cover.
    """
    text = partial_json.strip()
    if not text:
        return None

    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escaped = False
    index = 0
    # Index in `out` just past the last element/pair that is safe to keep, per container.
    safe: list[int] = []

    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if stack and stack[-1] == "value":
                    stack.pop()
                    safe[-1] = len(out)
                elif stack and stack[-1] == "[":
                    # An array has elements, not keys: a finished string is complete.
                    safe[-1] = len(out)
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
        elif char in "{[":
            out.append(char)
            stack.append(char)
            safe.append(len(out))
        elif char in "}]":
            out.append(char)
            if stack and stack[-1] in "{[":
                stack.pop()
                safe.pop()
                # The container that just closed *was* the pending value, so that slot is
                # filled too. Leaving the marker makes the unwind close one bracket twice.
                if stack and stack[-1] == "value":
                    stack.pop()
                if safe:
                    safe[-1] = len(out)
        elif char == ":":
            out.append(char)
            stack.append("value")
        elif char == ",":
            if stack and stack[-1] == "value":
                stack.pop()
            out.append(char)
            if safe:
                safe[-1] = len(out) - 1
        elif char.isspace():
            out.append(char)
        else:
            # A bare literal or number: consume the whole run so it can be judged at once.
            run_end = index
            while run_end < len(text) and (text[run_end].isalnum() or text[run_end] in "+-.eE"):
                run_end += 1
            token = text[index:run_end]
            completed = _complete_bare_token(token, run_end >= len(text))
            if completed is None:
                index = run_end
                continue
            out.append(completed)
            if stack and stack[-1] == "value":
                stack.pop()
            if safe:
                safe[-1] = len(out)
            index = run_end
            continue
        index += 1

    if not stack and not in_string:
        # Nothing is open. Either the text was already well-formed -- leave it to the
        # caller's ordinary parse -- or a bare top-level token was completed (`tru`).
        produced = "".join(out)
        return None if produced == text or not produced else produced

    # Unwind: cut each open container back to its last safe point and close it.
    if in_string:
        # An escape cut in half (`"a \\` or `"a \\u00e`) cannot be closed -- appending the
        # quote would escape it, or leave a short \\u. Upstream keeps the text before it.
        text_so_far = "".join(out)
        trailing = len(text_so_far) - len(text_so_far.rstrip("\\"))
        if trailing % 2:
            del out[len(out) - 1:]
        else:
            short = _unterminated_escape_start(text_so_far)
            if short is not None:
                del out[short:]
        if not stack:
            # A bare top-level string (`"q\\`): there is no container, just the string.
            out.append('"')
        elif stack[-1] == "value":
            # A value that had begun gets closed -- upstream keeps what streamed so far.
            out.append('"')
            stack.pop()
            if safe:
                safe[-1] = len(out)
        elif stack and stack[-1] == "[":
            # Inside an array the streaming string is an element, so it is completed the
            # same way a value is.
            out.append('"')
            if safe:
                safe[-1] = len(out)
        # A *key* that is still streaming needs no branch: it has no value yet, so the
        # container unwind below cuts back past it anyway -- closing its quote here would
        # only leave `{"p"}`, which the same cut then removes.
    while stack:
        opener = stack.pop()
        if opener not in "{[":
            continue  # a pending value marker: nothing to close, the pair is cut instead
        cut = safe.pop()
        del out[cut:]
        out.append("}" if opener == "{" else "]")
        if safe:
            safe[-1] = len(out)
    return "".join(out)


def _complete_bare_token(token: str, at_end: bool) -> str | None:
    """A finished literal/number stays; a truncated one is completed or dropped."""
    if not at_end:
        return token
    for literal in _PARTIAL_LITERALS:
        if literal == token:
            return token
    matches = [literal for literal in _PARTIAL_LITERALS if literal.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None  # ambiguous prefix: drop rather than guess
    try:
        json.loads(token)
    except ValueError:
        # Only a half-written *exponent* is recoverable: upstream yields -2 for `-2e` and
        # 1.2 for `1.2e-`, but drops `1.` and `0.` entirely -- a fraction with no digits
        # invalidates the number rather than truncating it.
        mantissa, marker, _ = token.partition("e") if "e" in token else token.partition("E")
        if not marker:
            return None
        try:
            json.loads(mantissa)
        except ValueError:
            return None
        return mantissa
    return token


def _or_empty(value: Any) -> TJson:
    """Upstream's completion path ends in ``?? {}``: a completed *top-level* null is
    indistinguishable from "nothing parsed", so both become the empty object.

    Only the top level, and only on this path: a null nested inside a container is a real
    value and survives, and a document that is already complete keeps its own null.
    """
    return {} if value is None else value


def parse_streaming_json(partial_json: str | None) -> TJson:
    if partial_json is None or partial_json.strip() == "":
        return {}

    try:
        # A complete document parses as itself, null included -- the coalescing below
        # belongs to the completion path only.
        return parse_json_with_repair(partial_json)
    except Exception:  # noqa: BLE001 - JSONDecodeError normally, RecursionError when nesting is pathological
        trimmed = _complete_json_prefix(partial_json)
        if trimmed is not None:
            try:
                # No post-trimming: the completer already decided, escape-aware, how much
                # of each string survives. Re-clipping by raw character count would cut
                # `"line\n` back to `line`.
                return _or_empty(json.loads(trimmed))
            except ValueError:
                pass
        # Nothing recoverable. Upstream's parser also yields the empty object for a
        # prefix that has not produced a single complete value yet (`3.`, `-`).
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
