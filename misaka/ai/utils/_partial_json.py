"""MISAKA fork: a stand-in for the ``partial-json`` npm package's ``parse``, which pi uses to read a JSON document
that is still streaming in. There is no Python port of it; this completes a truncated prefix the way it does and parses
the result.
"""

from __future__ import annotations

import json

from misaka.utils.js import jsonParse

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
            if run_end == index:
                # Reject unknown syntax; skipping it would silently change the value.
                return None
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


def parse(text: str) -> object:
    """``partialParse(text)``: the value of a JSON document, completing a truncated prefix first. Raises when the text
    is not JSON and cannot be completed into JSON."""
    completed = _complete_json_prefix(text)
    return jsonParse(text if completed is None else completed)


__all__ = ["parse"]
