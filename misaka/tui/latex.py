"""LaTeX → 终端 Unicode 文本（pi 05e89b4 的功能对齐）。

pi 的 latex.ts 是 1225 行自研渲染器——因为 JS 没有现成库。Python 有 pylatexenc
（成熟、纯 Python），符号层（希腊字母/∑√∫/ℝ/≤≠…）它全覆盖，我们只补一层
**安全的上下标映射**（^2→² 这类；Unicode 没有对应字符的保持字面，绝不硬造）。
# ponytail: 复杂布局（多行矩阵/嵌套分数的二维排版）不做——pi 自研版有,
# 要的话按 aa601d7 的矩阵布局逐函数移植;当前输出退化为单行文本,可读不丢内容。
"""
from __future__ import annotations

import re

_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))

# pylatexenc 输出裸上下标(`_{12}`→`_12`、`^{n}`→`^n`),数字串连转,其余单字符
_SUP_RE = re.compile(r"\^(\{[^{}]+\}|\d+|[^\s^_{}])")
_SUB_RE = re.compile(r"_(\{[^{}]+\}|\d+|[^\s^_{}])")


def _map_script(match: re.Match[str], table: dict[str, str]) -> str:
    body = match.group(1)
    if body.startswith("{"):
        body = body[1:-1]
    mapped = "".join(table.get(ch, "\x00") for ch in body)
    if "\x00" in mapped:          # 任一字符映射不了 → 整段保持原样（诚实降级）
        return match.group(0)
    return mapped


def _apply_scripts(text: str) -> str:
    text = _SUP_RE.sub(lambda m: _map_script(m, _SUP), text)
    text = _SUB_RE.sub(lambda m: _map_script(m, _SUB), text)
    return text


def render_latex(source: str) -> str | None:
    """渲染数学内容（不含定界符）。失败返回 None，调用方回退原文（pi 同款容错）。"""
    src = source.strip()
    if not src:
        return None
    try:
        from pylatexenc.latex2text import LatexNodes2Text

        text = LatexNodes2Text(math_mode="text").latex_to_text(f"${src}$")
    except Exception:  # noqa: BLE001 —— 渲染器炸了绝不拖垮 markdown
        return None
    text = _apply_scripts(text.strip())
    return text or None


if __name__ == "__main__":
    checks = [
        (r"\alpha + \beta", "α + β"),
        (r"E = mc^2", "E = mc²"),
        (r"x_i + y_{12}", "xᵢ + y₁₂"),
        (r"x^{10}", "x¹⁰"),
        (r"a \leq b \neq c", "a ≤ b ≠ c"),
        (r"\mathbb{R}", "ℝ"),
        (r"\sqrt{2}", "√(2)"),
    ]
    for src, want in checks:
        got = render_latex(src)
        assert got == want, f"{src!r}: {got!r} != {want!r}"
    # 映射不了的上标整段保字面,不硬造
    assert render_latex(r"x^Q") == "x^Q"
    assert render_latex(r"\sum_{i=1}^{n} x_i") == "∑ᵢ=1ⁿ xᵢ"
    assert render_latex("") is None
    assert render_latex(r"\newcommand{\x}{") in (None, r"\newcommand{\x}{")  # 坏输入不炸
    print("latex selfcheck ok — 符号/上下标/诚实降级/坏输入不炸")
