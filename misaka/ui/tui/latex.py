"""LaTeX to terminal Unicode text (feature parity with pi 05e89b4).

pi's latex.ts is a 1225-line hand-written renderer because JS has no ready-made
library. Python has pylatexenc (mature, pure Python), which covers the symbol layer
(Greek letters, sum/sqrt/integral, blackboard bold, <=, !=, ...). We only add a safe
superscript/subscript mapping on top (^2 -> ², etc.); anything without a Unicode
counterpart is left literal, never invented.

Complex layout (multi-line matrices, 2-D nested fractions) is deliberately not done.
pi's renderer has it; port the matrix layout from aa601d7 function by function if it
is ever needed. For now such input degrades to single-line text: readable, nothing lost.
"""
from __future__ import annotations

import re

_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))

# pylatexenc emits bare scripts (`_{12}` -> `_12`, `^{n}` -> `^n`): map digit runs
# as a whole, otherwise a single character
_SUP_RE = re.compile(r"\^(\{[^{}]+\}|\d+|[^\s^_{}])")
_SUB_RE = re.compile(r"_(\{[^{}]+\}|\d+|[^\s^_{}])")

_MAX_SCRIPT_DEPTH = 6
# Private-use sentinels for a group that fell back to `^(body)`. The fallback text contains a
# literal `^` followed by `(`, which `_SUP_RE` would otherwise happily read as the script `⁽`.
_HOLD_OPEN = "\ue000"
_HOLD_CLOSE = "\ue001"
_HOLD_RE = re.compile(f"{_HOLD_OPEN}(\\d+){_HOLD_CLOSE}")


def _map_script(match: re.Match[str], table: dict[str, str]) -> str:
    body = match.group(1)
    if body.startswith("{"):
        body = body[1:-1]
    mapped = "".join(table.get(ch, "\x00") for ch in body)
    if "\x00" in mapped:          # any unmappable character: keep the whole fragment literal
        return match.group(0)
    return mapped


def _apply_scripts(text: str) -> str:
    text = _SUP_RE.sub(lambda m: _map_script(m, _SUP), text)
    text = _SUB_RE.sub(lambda m: _map_script(m, _SUB), text)
    return text


def _matching_brace(text: str, open_index: int) -> int:
    """Index of the `}` closing the `{` at ``open_index``, or -1 when it is unbalanced."""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == "}" and text[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _resolve_script_groups(source: str, holds: list[str], depth: int) -> str:
    """Turn `^{...}` / `_{...}` into their rendered form *before* pylatexenc sees them.

    pylatexenc strips the braces (`x^{-1}` -> `x^-1`), after which `_SUP_RE` can only reach
    the first character of the group and the rest drops back to the baseline: `x⁻1`, which
    reads as "x to the minus, times one". Grouping has to be resolved while the braces are
    still there. A group whose rendered body maps entirely into the script table becomes real
    superscript/subscript characters; anything else degrades to `^(body)`, matching pi's
    `formatScript` (latex.ts:598) rather than inventing a half-raised form.
    """
    if depth >= _MAX_SCRIPT_DEPTH or ("^{" not in source and "_{" not in source):
        return source

    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "^_" and index + 1 < length and source[index + 1] == "{":
            end = _matching_brace(source, index + 1)
            if end == -1:
                out.append(char)
                index += 1
                continue
            rendered = _render_fragment(source[index + 2 : end], depth + 1)
            table = _SUP if char == "^" else _SUB
            mapped = "".join(table.get(ch, "\x00") for ch in rendered)
            if rendered:
                # Both outcomes go back as a sentinel rather than as text. The mapped form
                # would otherwise glue itself onto a preceding macro name (`\sum` + `ᵢ` is one
                # unknown macro, and the sum sign vanishes), and the `^(body)` form would be
                # re-read by `_SUP_RE` as the script `⁽`.
                holds.append(mapped if "\x00" not in mapped else f"{char}({rendered})")
                out.append(f"{_HOLD_OPEN}{len(holds) - 1}{_HOLD_CLOSE}")
            # `x^{}` renders to nothing: there is no script to raise
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _latex_to_text(source: str) -> str | None:
    try:
        from pylatexenc.latex2text import LatexNodes2Text

        return LatexNodes2Text(math_mode="text").latex_to_text(f"${source}$")
    except Exception:  # noqa: BLE001 - a renderer failure must never take markdown down
        return None


def _restore_holds(text: str, holds: list[str]) -> str:
    if not holds:
        return text
    return _HOLD_RE.sub(lambda m: holds[int(m.group(1))] if int(m.group(1)) < len(holds) else m.group(0), text)


def _render_fragment(source: str, depth: int) -> str:
    """The full pipeline applied to a script group's body, so `e^{i\\pi}` sees `iπ`."""
    holds: list[str] = []
    resolved = _resolve_script_groups(source, holds, depth)
    text = _latex_to_text(resolved)
    if text is None:
        text = resolved
    return _restore_holds(_apply_scripts(text.strip()), holds)


def render_latex(source: str) -> str | None:
    """Render math content (without delimiters). Returns None on failure so the caller falls back to the source text (same tolerance as pi)."""
    src = source.strip()
    if not src:
        return None
    holds: list[str] = []
    src = _resolve_script_groups(src, holds, 0)
    text = _latex_to_text(src)
    if text is None:
        return None
    text = _restore_holds(_apply_scripts(text.strip()), holds)
    return text or None
