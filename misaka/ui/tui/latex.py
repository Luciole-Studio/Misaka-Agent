"""Translated from pi-tui's ``latex.ts`` (v1.1.0): render basic LaTeX math as terminal-friendly Unicode text.

MISAKA fork: string positions count code points (Python strings), where upstream counts UTF-16 code units; every
position is taken and used on the same string, so the results agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from misaka.ui.tui.utils import visibleWidth
from misaka.utils import js_regexp
from misaka.utils.js import jsIndex, jsTrim, jsTrimEnd, jsTrimStart
from misaka.utils.js_regexp import RegExp

SYMBOLS: dict[str, str] = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ϵ",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "varkappa": "ϰ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ϱ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "ϕ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "pm": "±",
    "mp": "∓",
    "times": "×",
    "div": "÷",
    "cdot": "·",
    "ast": "∗",
    "star": "⋆",
    "circ": "∘",
    "bullet": "•",
    "oplus": "⊕",
    "ominus": "⊖",
    "otimes": "⊗",
    "oslash": "⊘",
    "odot": "⊙",
    "bigcirc": "○",
    "dagger": "†",
    "ddagger": "‡",
    "amalg": "⨿",
    "uplus": "⊎",
    "sqcap": "⊓",
    "sqcup": "⊔",
    "bowtie": "⋈",
    "Join": "⋈",
    "ltimes": "⋉",
    "rtimes": "⋊",
    "leftouterjoin": "⟕",
    "rightouterjoin": "⟖",
    "fullouterjoin": "⟗",
    "triangleleft": "◁",
    "triangleright": "▷",
    "wr": "≀",
    "cap": "∩",
    "cup": "∪",
    "bigcap": "⋂",
    "bigcup": "⋃",
    "bigwedge": "⋀",
    "bigvee": "⋁",
    "bigsqcup": "⨆",
    "biguplus": "⨄",
    "bigoplus": "⨁",
    "bigotimes": "⨂",
    "bigodot": "⨀",
    "setminus": "∖",
    "in": "∈",
    "notin": "∉",
    "ni": "∋",
    "subset": "⊂",
    "supset": "⊃",
    "subseteq": "⊆",
    "supseteq": "⊇",
    "sqsubset": "⊏",
    "sqsupset": "⊐",
    "sqsubseteq": "⊑",
    "sqsupseteq": "⊒",
    "prec": "≺",
    "preceq": "≼",
    "succ": "≻",
    "succeq": "≽",
    "ll": "≪",
    "gg": "≫",
    "le": "≤",
    "leq": "≤",
    "leqslant": "≤",
    "ge": "≥",
    "geq": "≥",
    "geqslant": "≥",
    "ne": "≠",
    "neq": "≠",
    "equiv": "≡",
    "approx": "≈",
    "sim": "∼",
    "simeq": "≃",
    "cong": "≅",
    "asymp": "≍",
    "doteq": "≐",
    "propto": "∝",
    "parallel": "∥",
    "perp": "⊥",
    "mid": "∣",
    "vdash": "⊢",
    "dashv": "⊣",
    "models": "⊨",
    "Vdash": "⊩",
    "Vvdash": "⊪",
    "nvdash": "⊬",
    "nvDash": "⊭",
    "forall": "∀",
    "exists": "∃",
    "nexists": "∄",
    "neg": "¬",
    "land": "∧",
    "wedge": "∧",
    "lor": "∨",
    "vee": "∨",
    "to": "→",
    "rightarrow": "→",
    "longrightarrow": "→",
    "leftarrow": "←",
    "longleftarrow": "←",
    "gets": "←",
    "leftrightarrow": "↔",
    "longleftrightarrow": "↔",
    "hookleftarrow": "↩",
    "hookrightarrow": "↪",
    "twoheadleftarrow": "↞",
    "twoheadrightarrow": "↠",
    "leftharpoonup": "↼",
    "leftharpoondown": "↽",
    "rightharpoonup": "⇀",
    "rightharpoondown": "⇁",
    "rightleftharpoons": "⇌",
    "leftrightharpoons": "⇋",
    "nearrow": "↗",
    "searrow": "↘",
    "swarrow": "↙",
    "nwarrow": "↖",
    "rightsquigarrow": "⇝",
    "leadsto": "⇝",
    "Rightarrow": "⇒",
    "Longrightarrow": "⇒",
    "Leftarrow": "⇐",
    "Longleftarrow": "⇐",
    "Leftrightarrow": "⇔",
    "Longleftrightarrow": "⇔",
    "implies": "⇒",
    "iff": "⇔",
    "mapsto": "↦",
    "longmapsto": "↦",
    "uparrow": "↑",
    "downarrow": "↓",
    "partial": "∂",
    "nabla": "∇",
    "int": "∫",
    "iint": "∬",
    "iiint": "∭",
    "oint": "∮",
    "sum": "∑",
    "prod": "∏",
    "coprod": "∐",
    "infty": "∞",
    "emptyset": "∅",
    "varnothing": "∅",
    "angle": "∠",
    "therefore": "∴",
    "because": "∵",
    "aleph": "ℵ",
    "beth": "ℶ",
    "gimel": "ℷ",
    "daleth": "ℸ",
    "top": "⊤",
    "bot": "⊥",
    "triangle": "△",
    "square": "□",
    "lozenge": "◊",
    "checkmark": "✓",
    "complement": "∁",
    "wp": "℘",
    "prime": "′",
    "ldots": "…",
    "dots": "…",
    "cdots": "⋯",
    "vdots": "⋮",
    "ddots": "⋱",
    "ell": "ℓ",
    "hbar": "ℏ",
    "Im": "ℑ",
    "Re": "ℜ",
    "langle": "⟨",
    "rangle": "⟩",
    "vert": "|",
    "lvert": "|",
    "rvert": "|",
    "Vert": "‖",
    "lVert": "‖",
    "rVert": "‖",
    "lbrace": "{",
    "rbrace": "}",
    "backslash": "\\",
    "lfloor": "⌊",
    "rfloor": "⌋",
    "lceil": "⌈",
    "rceil": "⌉",
    "colon": ":",
}

NAMED_OPERATORS: frozenset[str] = frozenset(
    {
        "arccos",
        "arcsin",
        "arctan",
        "arg",
        "cos",
        "cosh",
        "cot",
        "coth",
        "csc",
        "deg",
        "det",
        "dim",
        "exp",
        "gcd",
        "hom",
        "inf",
        "ker",
        "lg",
        "lim",
        "liminf",
        "limsup",
        "ln",
        "log",
        "max",
        "min",
        "Pr",
        "sec",
        "sin",
        "sinh",
        "sup",
        "tan",
        "tanh",
    }
)

LIMIT_OPERATORS: frozenset[str] = frozenset(
    {
        "argmax",
        "argmin",
        "inf",
        "injlim",
        "lim",
        "liminf",
        "limsup",
        "max",
        "min",
        "projlim",
        "sup",
    }
)

DISPLAY_LIMIT_SYMBOLS: frozenset[str] = frozenset(
    {
        "bigcap",
        "bigcup",
        "bigodot",
        "bigoplus",
        "bigotimes",
        "bigsqcup",
        "biguplus",
        "bigvee",
        "bigwedge",
        "coprod",
        "int",
        "iint",
        "iiint",
        "oint",
        "prod",
        "sum",
    }
)

RELATION_COMMANDS: frozenset[str] = frozenset(
    {
        "Leftarrow",
        "Leftrightarrow",
        "Longleftarrow",
        "Longleftrightarrow",
        "Longrightarrow",
        "Rightarrow",
        "Join",
        "Vdash",
        "Vvdash",
        "approx",
        "asymp",
        "bowtie",
        "cong",
        "dashv",
        "fullouterjoin",
        "doteq",
        "downarrow",
        "equiv",
        "ge",
        "geq",
        "geqslant",
        "gets",
        "gg",
        "hookleftarrow",
        "hookrightarrow",
        "iff",
        "implies",
        "in",
        "leadsto",
        "le",
        "leftarrow",
        "leftharpoondown",
        "leftharpoonup",
        "leftrightarrow",
        "leftrightharpoons",
        "leftouterjoin",
        "leq",
        "leqslant",
        "ll",
        "longleftarrow",
        "longleftrightarrow",
        "longmapsto",
        "longrightarrow",
        "ltimes",
        "mapsto",
        "mid",
        "models",
        "ne",
        "nearrow",
        "neq",
        "ni",
        "notin",
        "nvdash",
        "nvDash",
        "nwarrow",
        "parallel",
        "perp",
        "prec",
        "preceq",
        "propto",
        "rightharpoondown",
        "rightharpoonup",
        "rightleftharpoons",
        "rightouterjoin",
        "rightarrow",
        "rightsquigarrow",
        "rtimes",
        "searrow",
        "sim",
        "simeq",
        "sqsubset",
        "sqsubseteq",
        "sqsupset",
        "sqsupseteq",
        "subset",
        "subseteq",
        "succ",
        "succeq",
        "supset",
        "supseteq",
        "swarrow",
        "to",
        "triangleleft",
        "triangleright",
        "twoheadleftarrow",
        "twoheadrightarrow",
        "uparrow",
        "vdash",
    }
)

NEGATED_SYMBOLS: dict[str, str] = {
    "<": "≮",
    ">": "≯",
    "=": "≠",
    "∈": "∉",
    "∋": "∌",
    "∣": "∤",
    "∥": "∦",
    "∼": "≁",
    "≃": "≄",
    "≅": "≇",
    "≈": "≉",
    "≡": "≢",
    "≤": "≰",
    "≥": "≱",
    "≺": "⊀",
    "≻": "⊁",
    "⊂": "⊄",
    "⊃": "⊅",
    "⊆": "⊈",
    "⊇": "⊉",
    "⊢": "⊬",
    "⊨": "⊭",
    "↔": "↮",
    "←": "↚",
    "→": "↛",
    "⇒": "⇏",
    "⇐": "⇍",
    "⇔": "⇎",
    "≼": "⋠",
    "≽": "⋡",
}

BLACKBOARD: dict[str, str] = {
    "C": "ℂ",
    "H": "ℍ",
    "N": "ℕ",
    "P": "ℙ",
    "Q": "ℚ",
    "R": "ℝ",
    "Z": "ℤ",
}

SUPERSCRIPTS: dict[str, str] = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "a": "ᵃ",
    "b": "ᵇ",
    "c": "ᶜ",
    "d": "ᵈ",
    "e": "ᵉ",
    "f": "ᶠ",
    "g": "ᵍ",
    "h": "ʰ",
    "i": "ⁱ",
    "j": "ʲ",
    "k": "ᵏ",
    "l": "ˡ",
    "m": "ᵐ",
    "n": "ⁿ",
    "o": "ᵒ",
    "p": "ᵖ",
    "r": "ʳ",
    "s": "ˢ",
    "t": "ᵗ",
    "u": "ᵘ",
    "v": "ᵛ",
    "w": "ʷ",
    "x": "ˣ",
    "y": "ʸ",
    "z": "ᶻ",
}

SUBSCRIPTS: dict[str, str] = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
}

SPACING_COMMANDS: frozenset[str] = frozenset(
    {
        ",",
        ":",
        ";",
        " ",
        ">",
        "enspace",
        "enskip",
        "medspace",
        "quad",
        "qquad",
        "thickspace",
        "thinspace",
    }
)
NEGATIVE_SPACING_COMMANDS: frozenset[str] = frozenset({"!", "negmedspace", "negthickspace", "negthinspace"})
NEGATIVE_SPACE = "\u0000"
FONT_SWITCH_COMMANDS: frozenset[str] = frozenset({"bf", "cal", "it", "rm", "sf", "sl", "tt"})
IGNORED_COMMANDS: frozenset[str] = frozenset(
    {
        "displaystyle",
        "limits",
        "nolimits",
        "scriptstyle",
        "scriptscriptstyle",
        "textstyle",
    }
)
SIZE_COMMANDS: frozenset[str] = frozenset(
    {
        "big",
        "Big",
        "bigg",
        "Bigg",
        "bigl",
        "Bigl",
        "biggl",
        "Biggl",
        "bigr",
        "Bigr",
        "biggr",
        "Biggr",
    }
)
PLAIN_WRAPPERS: frozenset[str] = frozenset(
    {
        "emph",
        "mathcal",
        "mathbf",
        "mathfrak",
        "mathit",
        "mathrm",
        "mathnormal",
        "mathscr",
        "mathsf",
        "mathtt",
        "mathup",
        "mbox",
        "overbrace",
        "pmb",
        "smash",
        "substack",
        "text",
        "textbf",
        "textit",
        "textmd",
        "textnormal",
        "textrm",
        "textsc",
        "textsf",
        "textsl",
        "texttt",
        "textup",
        "underbrace",
        "bm",
        "boldsymbol",
    }
)
ACCENTS: dict[str, str] = {
    "acute": "\u0301",
    "bar": "\u0305",
    "breve": "\u0306",
    "check": "\u030c",
    "ddot": "\u0308",
    "dot": "\u0307",
    "grave": "\u0300",
    "hat": "\u0302",
    "mathring": "\u030a",
    "overleftarrow": "\u20d6",
    "overleftrightarrow": "\u20e1",
    "overline": "\u0305",
    "overrightarrow": "\u20d7",
    "tilde": "\u0303",
    "underline": "\u0332",
    "vec": "\u20d7",
    "widehat": "\u0302",
    "widetilde": "\u0303",
}


def replaceCharacters(value: str, replacements: dict[str, str]) -> str | None:
    result = ""
    for character in value:
        replacement = replacements.get(character)
        if replacement is None:
            return None
        result += replacement
    return result


def normalizeScriptValue(value: str) -> str:
    return js_regexp.replace(jsTrim(value), RegExp(r"\s*([=+-])\s*", "g"), "$1")


def formatUnicodeScript(value: str, kind: Literal["sub", "sup"]) -> str | None:
    return replaceCharacters(normalizeScriptValue(value), SUBSCRIPTS if kind == "sub" else SUPERSCRIPTS)


def formatScript(value: str, kind: Literal["sub", "sup"]) -> str:
    value = normalizeScriptValue(value)
    unicode = formatUnicodeScript(value, kind)
    if unicode is not None:
        return unicode

    prefix = "_" if kind == "sub" else "^"
    if len(value) == 1 or (kind == "sub" and RegExp(r"^[A-Za-z]+$").test(value)):
        return f"{prefix}{value}"
    return f"{prefix}({value})"


def formatFraction(numerator: str, denominator: str) -> str:
    numerator = jsTrim(numerator)
    denominator = jsTrim(denominator)
    simpleNumerator = RegExp(r"^[\p{L}\p{N}.]+$", "u").test(numerator)
    simpleDenominator = RegExp(r"^[\p{N}.]+$", "u").test(denominator) or len(denominator) == 1
    return f"{numerator if simpleNumerator else f'({numerator})'}/{denominator if simpleDenominator else f'({denominator})'}"


def formatRoot(value: str, symbol: str = "√") -> str:
    value = jsTrim(value)
    return f"{symbol}{value}" if RegExp(r"^[\p{L}\p{N}.]+$", "u").test(value) else f"{symbol}({value})"


NAMED_OPERATOR_START = "\U000f0004"
NAMED_OPERATOR_END = "\U000f0005"
NAMED_OPERATOR_LEFT_SPACING_PATTERN = RegExp(r"(?<=[\p{L}\p{N})\]}\u{f0001}])\u{f0004}", "gu")
NAMED_OPERATOR_RIGHT_SPACING_PATTERN = RegExp(r"\u{f0005}(?=[\p{L}\p{N}√\u{f0000}])", "gu")


def normalizeOutput(value: str) -> str:
    replaced = js_regexp.replace(value, NAMED_OPERATOR_LEFT_SPACING_PATTERN, " ").replace(NAMED_OPERATOR_START, "")
    replaced = js_regexp.replace(replaced, NAMED_OPERATOR_RIGHT_SPACING_PATTERN, " ").replace(NAMED_OPERATOR_END, "")
    lines = [jsTrim(js_regexp.replace(line, RegExp(r"[ \t]+", "g"), " ")) for line in replaced.split("\n")]
    kept = [line for index, line in enumerate(lines) if len(line) > 0 or (index > 0 and index < len(lines) - 1)]
    return jsTrim("\n".join(kept))


class FractionNode(TypedDict):
    type: Literal["fraction"]
    numerator: str
    denominator: str


class OperatorNode(TypedDict):
    type: Literal["operator"]
    operator: str
    lower: NotRequired[str | None]
    upper: NotRequired[str | None]


class ScriptNode(TypedDict):
    type: Literal["script"]
    lower: NotRequired[str | None]
    upper: NotRequired[str | None]


class MatrixNode(TypedDict):
    type: Literal["matrix"]
    lines: list[str]
    baseline: int


type LayoutNode = FractionNode | OperatorNode | ScriptNode | MatrixNode


class Layout(TypedDict):
    lines: list[str]
    width: int
    baseline: int


LAYOUT_MARKER_START = "\U000f0000"
LAYOUT_MARKER_END = "\U000f0001"
LAYOUT_MARKER_PATTERN = RegExp(r"\u{f0000}(\d+)\u{f0001}", "gu")
TRAILING_LAYOUT_MARKER_PATTERN = RegExp(r"\u{f0000}(\d+)\u{f0001}$", "u")
PROTECTED_SPACE = "\U000f0002"


def padLayoutLine(line: str, width: int, centered: bool = False) -> str:
    padding = max(0, width - visibleWidth(line))
    left = padding // 2 if centered else 0
    return f"{' ' * left}{line}{' ' * (padding - left)}"


def joinLayouts(layouts: list[Layout]) -> Layout:
    if len(layouts) == 0:
        return {"lines": [""], "width": 0, "baseline": 0}
    baseline = max(layout["baseline"] for layout in layouts)
    below = max(len(layout["lines"]) - layout["baseline"] - 1 for layout in layouts)
    lines: list[str] = []
    for row in range(baseline + below + 1):
        line = ""
        for layout in layouts:
            sourceRow = row - baseline + layout["baseline"]
            line += (
                padLayoutLine(jsIndex(layout["lines"], sourceRow) or "", layout["width"])
                if sourceRow >= 0 and sourceRow < len(layout["lines"])
                else " " * layout["width"]
            )
        lines.append(jsTrimEnd(line))
    return {
        "lines": lines,
        "width": sum(layout["width"] for layout in layouts),
        "baseline": baseline,
    }


def renderLayout(source: str, nodes: list[LayoutNode]) -> Layout:
    renderedLines: list[str] = []
    firstBaseline = 0
    for sourceLine in source.split("\n"):
        layouts: list[Layout] = []
        position = 0
        previousNode: LayoutNode | None = None
        for match in js_regexp.matchAll(sourceLine, LAYOUT_MARKER_PATTERN):
            index = match.index
            node = jsIndex(nodes, int(match[1]))
            if not node:
                continue
            if index > position:
                sliced = sourceLine[position:index]
                trimmed = jsTrimEnd(jsTrimStart(sliced) if previousNode else sliced)
                preserveLeadingSpace = previousNode is not None and previousNode["type"] == "matrix" and RegExp(r"^\s").test(sliced)
                preserveTrailingSpace = node["type"] == "matrix" and RegExp(r"\s$").test(sliced)
                text = (
                    f"{' ' if preserveLeadingSpace else ''}{trimmed}{' ' if preserveTrailingSpace else ''}"
                    if trimmed
                    else " "
                    if preserveLeadingSpace or preserveTrailingSpace
                    else ""
                )
                layouts.append({"lines": [text], "width": visibleWidth(text), "baseline": 0})
            if node["type"] == "fraction":
                numerator = renderLayout(node["numerator"], nodes)
                denominator = renderLayout(node["denominator"], nodes)
                contentWidth = max(numerator["width"], denominator["width"], 1)
                width = contentWidth + 2
                layouts.append(
                    {
                        "lines": [
                            *(padLayoutLine(line, width, True) for line in numerator["lines"]),
                            f" {'─' * contentWidth} ",
                            *(padLayoutLine(line, width, True) for line in denominator["lines"]),
                        ],
                        "width": width,
                        "baseline": len(numerator["lines"]),
                    }
                )
            elif node["type"] == "operator":
                lower = node.get("lower")
                upper = node.get("upper")
                contentWidth = max(
                    visibleWidth(node["operator"]),
                    0 if lower is None else visibleWidth(lower),
                    0 if upper is None else visibleWidth(upper),
                )
                lines: list[str] = []
                if upper is not None:
                    lines.append(f"{padLayoutLine(upper, contentWidth, True)} ")
                lines.append(f"{padLayoutLine(node['operator'], contentWidth, True)} ")
                if lower is not None:
                    lines.append(f"{padLayoutLine(lower, contentWidth, True)} ")
                layouts.append(
                    {
                        "lines": lines,
                        "width": contentWidth + 1,
                        "baseline": 0 if upper is None else 1,
                    }
                )
            elif node["type"] == "script":
                nodeUpper = node.get("upper")
                nodeLower = node.get("lower")
                upperLayout = None if nodeUpper is None else renderLayout(nodeUpper, nodes)
                lowerLayout = None if nodeLower is None else renderLayout(nodeLower, nodes)
                width = max(
                    upperLayout["width"] if upperLayout is not None else 0,
                    lowerLayout["width"] if lowerLayout is not None else 0,
                )
                layouts.append(
                    {
                        "lines": [
                            *((padLayoutLine(line, width) for line in upperLayout["lines"]) if upperLayout else []),
                            " " * width,
                            *((padLayoutLine(line, width) for line in lowerLayout["lines"]) if lowerLayout else []),
                        ],
                        "width": width,
                        "baseline": len(upperLayout["lines"]) if upperLayout is not None else 0,
                    }
                )
            else:
                width = max([0, *(visibleWidth(line) for line in node["lines"])])
                layouts.append(
                    {
                        "lines": [padLayoutLine(line, width) for line in node["lines"]],
                        "width": width,
                        "baseline": node["baseline"],
                    }
                )
            position = index + len(match[0])
            previousNode = node
        if position < len(sourceLine):
            sliced = sourceLine[position:]
            trimmed = jsTrimStart(sliced) if previousNode else sliced
            text = (
                f" {trimmed}"
                if previousNode is not None and previousNode["type"] == "matrix" and RegExp(r"^\s").test(sliced)
                else trimmed
            )
            layouts.append({"lines": [text], "width": visibleWidth(text), "baseline": 0})
        lineLayout = joinLayouts(layouts)
        if len(renderedLines) == 0:
            firstBaseline = lineLayout["baseline"]
        renderedLines.extend(lineLayout["lines"])
    return {
        "lines": renderedLines,
        "width": max([0, *(visibleWidth(line) for line in renderedLines)]),
        "baseline": firstBaseline,
    }


class _Scripts(TypedDict, total=False):
    sub: str
    sup: str


class LatexParser:
    def __init__(self, source: str, layoutNodes: list[LayoutNode], display: bool) -> None:
        self.position = 0
        self.supported = True
        self.stackFractions = True
        self.scriptDepth = 0
        self.source = source
        self.layoutNodes = layoutNodes
        self.display = display

    def render(self) -> str | None:
        rendered = self.parseSequence()
        if not self.supported or self.position != len(self.source):
            return None
        return normalizeOutput(rendered)

    def parseSequence(self, endCharacter: str | None = None) -> str:
        result = ""
        while self.position < len(self.source):
            character = self.source[self.position]
            if endCharacter and character == endCharacter:
                self.position += 1
                return result

            if character == "}":
                self.supported = False
                return result

            if character == "{":
                self.position += 1
                result += self.parseSequence("}")
                continue

            if character == "\\":
                command = self.parseCommand()
                if command == NEGATIVE_SPACE:
                    result = jsTrimEnd(result)
                    result = result.removesuffix(NAMED_OPERATOR_END)
                else:
                    result += command
                continue

            if character == "^" or character == "_":
                self.position += 1
                result = jsTrimEnd(result)
                script = self.parseScripts(character)
                if result.endswith(NAMED_OPERATOR_END):
                    result = f"{result[: -len(NAMED_OPERATOR_END)]}{script}{NAMED_OPERATOR_END}"
                else:
                    result += script
                continue

            if RegExp(r"\s").test(character):
                result += self.parseWhitespace()
                continue

            if character == "=" or character == "<" or character == ">":
                result = f"{jsTrimEnd(result)} {character} "
                self.position += 1
                continue

            if character == "&":
                self.position += 1
                continue

            if character == "~":
                self.position += 1
                result += " "
                continue

            if character == ".":
                marker = TRAILING_LAYOUT_MARKER_PATTERN.exec(result)
                node = jsIndex(self.layoutNodes, int(marker[1])) if marker else None
                if node is not None and node["type"] == "matrix":
                    lastLine = len(node["lines"]) - 1
                    node["lines"][lastLine] = f"{jsIndex(node['lines'], lastLine) or ''}{character}"
                    self.position += 1
                    continue

            result += character
            self.position += 1

        if endCharacter:
            self.supported = False
        return result

    def parseScripts(self, initialMarker: str) -> str:
        scripts: _Scripts = {}
        order: list[Literal["sub", "sup"]] = []

        def parse(marker: str) -> None:
            kind: Literal["sub", "sup"] = "sub" if marker == "_" else "sup"
            self.scriptDepth += 1
            try:
                scripts[kind] = self.parseRequiredArgument(False)
            finally:
                self.scriptDepth -= 1
            order.append(kind)

        parse(initialMarker)
        nextPosition = self.position
        while nextPosition < len(self.source) and RegExp(r"\s").test(jsIndex(self.source, nextPosition) or ""):
            nextPosition += 1
        nextMarker = jsIndex(self.source, nextPosition)
        if (nextMarker == "^" or nextMarker == "_") and nextMarker != initialMarker:
            self.position = nextPosition + 1
            parse(nextMarker)

        sub = scripts.get("sub")
        sup = scripts.get("sup")
        subUnicode = None if sub is None else formatUnicodeScript(sub, "sub")
        supUnicode = None if sup is None else formatUnicodeScript(sup, "sup")
        canUseLayout = not any(
            value is not None
            and (
                "/" in value
                or (LAYOUT_MARKER_START not in value and len(value) > 1 and not RegExp(r"[A-Z*∗]").test(value))
            )
            for value in [sub, sup]
        )
        needsLayout = (
            self.display
            and canUseLayout
            and (self.scriptDepth > 0 or (sub is not None and subUnicode is None) or (sup is not None and supUnicode is None))
        )
        if not needsLayout:
            return "".join(
                (subUnicode if subUnicode is not None else formatScript(sub if sub is not None else "", kind))
                if kind == "sub"
                else (supUnicode if supUnicode is not None else formatScript(sup if sup is not None else "", kind))
                for kind in order
            )

        self.layoutNodes.append(
            {
                "type": "script",
                "lower": None if sub is None else normalizeOutput(sub),
                "upper": None if sup is None else normalizeOutput(sup),
            }
        )
        index = len(self.layoutNodes) - 1
        return f"{LAYOUT_MARKER_START}{index}{LAYOUT_MARKER_END}"

    def parseWhitespace(self) -> str:
        while self.position < len(self.source) and RegExp(r"\s").test(jsIndex(self.source, self.position) or ""):
            self.position += 1
        return " "

    def parseCommand(self) -> str:
        self.position += 1
        if self.position >= len(self.source):
            self.supported = False
            return ""

        command = ""
        first = jsIndex(self.source, self.position) or ""
        if first == "\n" or first == "\r":
            self.position += 1
            if first == "\r" and jsIndex(self.source, self.position) == "\n":
                self.position += 1
            return " "
        if RegExp(r"[A-Za-z]").test(first):
            start = self.position
            while self.position < len(self.source) and RegExp(r"[A-Za-z]").test(jsIndex(self.source, self.position) or ""):
                self.position += 1
            command = self.source[start : self.position]
        else:
            command = first
            self.position += 1

        if command == "\\":
            return "\n"
        if command in SPACING_COMMANDS:
            return " "
        if command in NEGATIVE_SPACING_COMMANDS:
            return NEGATIVE_SPACE
        if command in FONT_SWITCH_COMMANDS:
            while self.position < len(self.source) and RegExp(r"\s").test(jsIndex(self.source, self.position) or ""):
                self.position += 1
            return ""
        if command in IGNORED_COMMANDS:
            return ""
        if command in ("{", "}", "$", "%", "#", "_", "&"):
            return command
        if command == "|":
            return "‖"
        if command == "not":
            value = jsTrim(self.parseRequiredArgument(False))
            negated = NEGATED_SYMBOLS.get(value)
            if negated is not None:
                return f" {negated} "
            characters = list(value)
            if len(characters) == 0:
                self.supported = False
                return ""
            return f" {characters[0]}̸{''.join(characters[1:])} "
        if command in LIMIT_OPERATORS:
            return self.parseOperator(command, "bracket", True, True)

        symbol = SYMBOLS.get(command)
        if symbol is not None:
            if command in DISPLAY_LIMIT_SYMBOLS:
                return self.parseOperator(symbol, "script", True)
            return f" {symbol} " if command == "cdot" or command == "times" or command in RELATION_COMMANDS else symbol
        if command in NAMED_OPERATORS:
            return f"{NAMED_OPERATOR_START}{command}{NAMED_OPERATOR_END}"
        if command in SIZE_COMMANDS:
            return ""
        if command == "left" or command == "middle" or command == "right":
            if jsIndex(self.source, self.position) == ".":
                self.position += 1
            return ""
        if command == "frac" or command == "dfrac" or command == "tfrac":
            shouldStack = self.display and self.stackFractions and command != "tfrac"
            numerator = self.parseRequiredArgument(not shouldStack)
            denominator = self.parseRequiredArgument(not shouldStack)
            if shouldStack:
                self.layoutNodes.append(
                    {
                        "type": "fraction",
                        "numerator": normalizeOutput(numerator),
                        "denominator": normalizeOutput(denominator),
                    }
                )
                index = len(self.layoutNodes) - 1
                return f"{LAYOUT_MARKER_START}{index}{LAYOUT_MARKER_END}"
            return formatFraction(numerator, denominator)
        if command == "sqrt":
            optional = self.parseOptionalArgument()
            degree = jsTrim(optional) if optional is not None else None
            value = self.parseRequiredArgument()
            if degree is None or degree == "2":
                return formatRoot(value)
            if degree == "3":
                return formatRoot(value, "∛")
            if degree == "4":
                return formatRoot(value, "∜")
            return f"{formatScript(degree, 'sup')}{formatRoot(value)}"
        if command == "boxed" or command == "fbox":
            return f"[{jsTrim(self.parseRequiredArgument())}]"
        if command == "binom" or command == "dbinom" or command == "tbinom":
            return f"({self.parseRequiredArgument()} choose {self.parseRequiredArgument()})"
        accent = ACCENTS.get(command)
        if accent is not None:
            value = self.parseRequiredArgument()
            return f"{value}{accent}" if len(value) == 1 else f"{command}({value})"
        if command == "mathbb":
            value = self.parseRequiredArgument()
            return "".join(BLACKBOARD.get(character, character) for character in value)
        if command == "operatorname":
            starred = jsIndex(self.source, self.position) == "*"
            if starred:
                self.position += 1
            operator = jsTrim(normalizeOutput(self.parseRequiredArgument()))
            return self.parseOperator(operator, "bracket", starred, True)
        if command == "mod" or command == "bmod":
            return " mod "
        if command == "pmod" or command == "pod":
            value = jsTrim(self.parseRequiredArgument())
            return f" (mod {value})" if command == "pmod" else f" ({value})"
        if command == "overset" or command == "stackrel":
            upper = self.parseRequiredArgument()
            value = jsTrim(self.parseRequiredArgument())
            return f"{value}{formatScript(upper, 'sup')}"
        if command == "underset":
            lower = self.parseRequiredArgument()
            value = jsTrim(self.parseRequiredArgument())
            return f"{value}{formatScript(lower, 'sub')}"
        if command in PLAIN_WRAPPERS:
            value = self.parseRequiredArgument()
            return value if command.startswith("text") or command == "mbox" else jsTrim(value)
        if command == "begin":
            return self.parseEnvironment()
        if command == "end":
            self.supported = False
            return ""

        self.supported = False
        return f"\\{command}"

    def parseOperator(
        self,
        operator: str,
        inlineLowerStyle: Literal["bracket", "script"],
        displayLimits: bool,
        spaced: bool = False,
    ) -> str:
        useDisplayLimits = displayLimits
        modifierPosition = self.position
        while modifierPosition < len(self.source) and RegExp(r"[ \t]").test(jsIndex(self.source, modifierPosition) or ""):
            modifierPosition += 1
        modifier = RegExp(r"^\\(limits|nolimits)(?![A-Za-z])").exec(self.source[modifierPosition:])
        if modifier:
            useDisplayLimits = modifier[1] == "limits"
            self.position = modifierPosition + len(modifier[0])

        lower: str | None = None
        upper: str | None = None
        while True:
            scriptPosition = self.position
            while scriptPosition < len(self.source) and RegExp(r"[ \t]").test(jsIndex(self.source, scriptPosition) or ""):
                scriptPosition += 1
            kind = jsIndex(self.source, scriptPosition)
            if kind != "_" and kind != "^":
                break
            self.position = scriptPosition + 1
            value = normalizeOutput(self.parseRequiredArgument(False)).replace(" ", "")
            if kind == "_":
                if lower is not None:
                    self.supported = False
                lower = value
            else:
                if upper is not None:
                    self.supported = False
                upper = value

        if self.display and useDisplayLimits and (lower is not None or upper is not None):
            self.layoutNodes.append({"type": "operator", "operator": operator, "lower": lower, "upper": upper})
            index = len(self.layoutNodes) - 1
            return f"{LAYOUT_MARKER_START}{index}{LAYOUT_MARKER_END}"

        rendered = operator
        if lower is not None:
            rendered += f"[{lower}]" if inlineLowerStyle == "bracket" else formatScript(lower, "sub")
        if upper is not None:
            rendered += formatScript(upper, "sup")
        return f" {rendered} " if spaced else rendered

    def parseRequiredArgument(self, stackFractions: bool = True) -> str:
        previousStackFractions = self.stackFractions
        self.stackFractions = previousStackFractions and stackFractions
        value = self.parseRequiredArgumentValue()
        self.stackFractions = previousStackFractions
        return value

    def parseRequiredArgumentValue(self) -> str:
        while self.position < len(self.source) and RegExp(r"\s").test(jsIndex(self.source, self.position) or ""):
            self.position += 1
        if self.position >= len(self.source):
            self.supported = False
            return ""
        if jsIndex(self.source, self.position) == "{":
            self.position += 1
            return self.parseSequence("}")
        if jsIndex(self.source, self.position) == "\\":
            return self.parseCommand()
        value = jsIndex(self.source, self.position) or ""
        self.position += 1
        return value

    def parseOptionalArgument(self) -> str | None:
        while self.position < len(self.source) and RegExp(r"[ \t]").test(jsIndex(self.source, self.position) or ""):
            self.position += 1
        if jsIndex(self.source, self.position) != "[":
            return None
        end = self.source.find("]", self.position + 1)
        if end < 0:
            self.supported = False
            return None
        value = self.source[self.position + 1 : end]
        self.position = end + 1
        return self.renderNested(value)

    def readRawGroup(self) -> str | None:
        while self.position < len(self.source) and RegExp(r"[ \t]").test(jsIndex(self.source, self.position) or ""):
            self.position += 1
        if jsIndex(self.source, self.position) != "{":
            self.supported = False
            return None

        self.position += 1
        start = self.position
        depth = 1
        while self.position < len(self.source):
            character = self.source[self.position]
            if character == "\\":
                self.position += 2
                continue
            if character == "{":
                depth += 1
            if character == "}":
                depth -= 1
            if depth == 0:
                value = self.source[start : self.position]
                self.position += 1
                return value
            self.position += 1
        self.supported = False
        return None

    def splitEnvironmentRows(self, body: str) -> list[str]:
        return js_regexp.split(body, RegExp(r"\\\\(?:\[[^\]\n]*\])?"))

    def parseEnvironment(self) -> str:
        environment = self.readRawGroup()
        if not environment:
            return ""
        endMarker = f"\\end{{{environment}}}"
        end = self.source.find(endMarker, self.position)
        if end < 0:
            self.supported = False
            return ""
        body = self.source[self.position : end]
        self.position = end + len(endMarker)

        if environment == "equation" or environment == "equation*" or environment == "displaymath":
            return jsTrim(self.renderNested(body))

        if environment in (
            "aligned",
            "align",
            "align*",
            "alignedat",
            "alignat",
            "alignat*",
            "gather",
            "gathered",
            "multline",
            "multline*",
            "split",
        ):
            alignedAt = environment in ["alignedat", "alignat", "alignat*"]
            alignedBody = js_regexp.replace(body, RegExp(r"^\s*\{[^}]*\}"), "") if alignedAt else body

            def renderRow(row: str) -> str:
                cells = row.split("&")
                source = (
                    " ".join("".join(cells[index * 2 : index * 2 + 2]) for index in range(math.ceil(len(cells) / 2)))
                    if alignedAt
                    else "".join(cells)
                )
                return jsTrim(self.renderNested(source))

            return "\n".join(row for row in (renderRow(row) for row in self.splitEnvironmentRows(alignedBody)) if row)

        if environment == "cases" or environment == "cases*":
            return self.renderCases(body)

        if environment in ["array", "matrix", "smallmatrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix"]:
            matrixBody = js_regexp.replace(body, RegExp(r"^\s*\{[^}]*\}"), "") if environment == "array" else body
            return self.renderMatrix(environment, matrixBody)

        self.supported = False
        return body

    def renderCases(self, body: str) -> str:
        rows = [
            row
            for row in ([jsTrim(self.renderNested(cell, False)) for cell in row.split("&")] for row in self.splitEnvironmentRows(body))
            if any(row)
        ]
        valueWidth = max([0, *(visibleWidth(js_regexp.replace(jsIndex(row, 0) or "", RegExp(r",\s*$"), "")) for row in rows)])
        contents: list[str] = []
        for row in rows:
            value = js_regexp.replace(jsIndex(row, 0) or "", RegExp(r",\s*$"), "")
            condition = jsIndex(row, 1) or ""
            if not condition:
                contents.append(value)
                continue
            conditionPrefix = " " if RegExp(r"^(?:if|when|for|otherwise)\b", "i").test(condition) else " if "
            contents.append(
                f"{value}{PROTECTED_SPACE * (valueWidth - visibleWidth(value))}{conditionPrefix}{condition}"
            )
        if len(contents) <= 1:
            return "" if len(contents) == 0 else f"⎧ {contents[0]}"

        middle = len(contents) // 2
        visualRows: list[str | None] = [*contents]
        if len(contents) % 2 == 0:
            visualRows.insert(middle, None)
        lines: list[str] = []
        for index, content in enumerate(visualRows):
            delimiter = "⎧" if index == 0 else "⎩" if index == len(visualRows) - 1 else "⎨"
            lines.append(delimiter if content is None else f"{delimiter} {content}")
        self.layoutNodes.append({"type": "matrix", "lines": lines, "baseline": middle})
        index = len(self.layoutNodes) - 1
        return f"{LAYOUT_MARKER_START}{index}{LAYOUT_MARKER_END}"

    def renderMatrix(self, environment: str, body: str) -> str:
        matrix = [
            row
            for row in ([jsTrim(self.renderNested(cell, False)) for cell in row.split("&")] for row in self.splitEnvironmentRows(body))
            if any(row)
        ]
        columnCount = max([0, *(len(row) for row in matrix)])
        columnWidths = [max([0, *(visibleWidth(jsIndex(row, column) or "") for row in matrix)]) for column in range(columnCount)]
        rows = [
            " │ ".join(
                f"{cell}{PROTECTED_SPACE * max(0, (jsIndex(columnWidths, column) or 0) - visibleWidth(cell))}"
                for column in range(columnCount)
                for cell in [jsIndex(row, column) or ""]
            )
            for row in matrix
        ]

        lines: list[str]
        if environment == "array" or environment == "matrix" or environment == "smallmatrix":
            lines = rows
        else:
            delimiters: dict[str, tuple[str, str, str, str, str, str]] = {
                "pmatrix": ("⎛", "⎞", "⎜", "⎟", "⎝", "⎠"),
                "bmatrix": ("⎡", "⎤", "⎢", "⎥", "⎣", "⎦"),
                "Bmatrix": ("⎧", "⎫", "⎨", "⎬", "⎩", "⎭"),
                "vmatrix": ("│", "│", "│", "│", "│", "│"),
                "Vmatrix": ("║", "║", "║", "║", "║", "║"),
            }
            delimiter = delimiters.get(environment)
            if not delimiter:
                self.supported = False
                return "\n".join(rows)
            lines = []
            for index, row in enumerate(rows):
                left = delimiter[0] if index == 0 else delimiter[4] if index == len(rows) - 1 else delimiter[2]
                right = delimiter[1] if index == 0 else delimiter[5] if index == len(rows) - 1 else delimiter[3]
                lines.append(f"{left} {row} {right}")

        if len(lines) <= 1:
            return jsIndex(lines, 0) or ""
        self.layoutNodes.append({"type": "matrix", "lines": lines, "baseline": 0})
        index = len(self.layoutNodes) - 1
        return f"{LAYOUT_MARKER_START}{index}{LAYOUT_MARKER_END}"

    def renderNested(self, source: str, stackFractions: bool = True) -> str:
        rendered = LatexParser(source, self.layoutNodes, self.display and stackFractions).render()
        if rendered is None:
            self.supported = False
            return source
        return rendered


@dataclass(slots=True)
class RenderLatexOptions:
    display: bool | None = None
    """Stack fractions and operator limits vertically for display math (default: false)."""


def renderLatex(source: str, options: RenderLatexOptions | None = None) -> str | None:
    """Render a basic LaTeX math expression as terminal-friendly Unicode text.
    Returns undefined when the expression contains unsupported or malformed syntax.
    """
    if options is None:
        options = RenderLatexOptions()
    layoutNodes: list[LayoutNode] = []
    rendered = LatexParser(source, layoutNodes, options.display is True).render()
    if rendered is None:
        return None
    if len(layoutNodes) == 0:
        return rendered.replace(PROTECTED_SPACE, " ")
    lines = renderLayout(rendered, layoutNodes)["lines"]
    indents = [len(line) - len(jsTrimStart(line)) for line in lines if jsTrim(line)]
    # `Math.min()` of no values is Infinity, which slices every line to "".
    indentation = min(indents) if indents else max((len(line) for line in lines), default=0)
    return jsTrimEnd("\n".join(jsTrimEnd(line[indentation:]) for line in lines)).replace(PROTECTED_SPACE, " ")


__all__ = ["RenderLatexOptions", "renderLatex"]
