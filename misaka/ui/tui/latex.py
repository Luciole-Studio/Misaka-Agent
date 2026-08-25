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


def render_latex(source: str) -> str | None:
    """Render math content (without delimiters). Returns None on failure so the caller falls back to the source text (same tolerance as pi)."""
    src = source.strip()
    if not src:
        return None
    try:
        from pylatexenc.latex2text import LatexNodes2Text

        text = LatexNodes2Text(math_mode="text").latex_to_text(f"${src}$")
    except Exception:  # noqa: BLE001 - a renderer failure must never take markdown down
        return None
    text = _apply_scripts(text.strip())
    return text or None
