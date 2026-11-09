"""Translated from pi-tui's ``test/latex.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.ui.tui.latex import RenderLatexOptions, renderLatex

DISPLAY = RenderLatexOptions(display=True)


def defineCases(cases: list[tuple[str, str]]):
    return pytest.mark.parametrize(("source", "expected"), cases, ids=[repr(source) for source, _expected in cases])


# --- Jacobian conjecture session using dollar delimiters ------------------------------------------


@defineCases(
    [
        (r"\mathbb{C}^3 \to \mathbb{C}^3", "ℂ³ → ℂ³"),
        (
            r"\{3x+2y,\; 27x^2-4z-1,\; x(x-1)(x+1)\} \quad\Rightarrow\quad x \in \{0, \pm 1\},",
            "{3x+2y, 27x²-4z-1, x(x-1)(x+1)} ⇒ x ∈ {0, ± 1},",
        ),
        (r"F_1 = -\frac{1}{4x^2}.", "F₁ = -1/(4x²)."),
        ("-2", "-2"),
        ("(0,0,-1/4)", "(0,0,-1/4)"),
        ("(1,-3/2,13/2)", "(1,-3/2,13/2)"),
        ("(1,1,1)", "(1,1,1)"),
        ("(2,1,0)", "(2,1,0)"),
        ("(-1/4, 0, 0)", "(-1/4, 0, 0)"),
        (r"\{(0,0,-1/4), (1,-3/2,13/2), (-1,3/2,13/2)\}", "{(0,0,-1/4), (1,-3/2,13/2), (-1,3/2,13/2)}"),
        ("(2,1,1)", "(2,1,1)"),
        ("(7/3,-2/5,11/7)", "(7/3,-2/5,11/7)"),
        (r"\{y - p(x),\; q(x)\}", "{y - p(x), q(x)}"),
        (r"\deg q = 3", "deg q = 3"),
        (r"[\mathbb{C}(x,y,z):\mathbb{C}(F_1,F_2,F_3)] = 3", "[ℂ(x,y,z):ℂ(F₁,F₂,F₃)] = 3"),
        ("u = 1+xy", "u = 1+xy"),
        ("G = u^2 z + y^2(4+3xy)", "G = u² z + y²(4+3xy)"),
        ("F_1 = uG", "F₁ = uG"),
        ("F_2 = y + 3xG", "F₂ = y + 3xG"),
        ("x=0", "x = 0"),
        ("F_2 = F_3 = 0", "F₂ = F₃ = 0"),
        ("xy = -3/2", "xy = -3/2"),
        ("x^2 z = 13/2", "x² z = 13/2"),
        (r"\mathbb{C}^*", "ℂ^*"),
        (r"s \mapsto (s,\, -\tfrac{3}{2s},\, \tfrac{13}{2s^2})", "s ↦ (s, -3/(2s), 13/(2s²))"),
        ("X", "X"),
        (r"p_\pm", "p_±"),
        ("F(-x,-y,z) = (F_1, -F_2, -F_3)", "F(-x,-y,z) = (F₁, -F₂, -F₃)"),
        ("p_0", "p₀"),
        (r"s \to \infty", "s → ∞"),
        ("(0,0,0)", "(0,0,0)"),
        (r"\Rightarrow", "⇒"),
        (r"\ge 2", "≥ 2"),
        (r"\ge 3", "≥ 3"),
        ("1", "1"),
        (r"\mathrm{diag}(-1/2,1,1),\quad F_{\rm intrinsic}(\lambda)", "diag(-1/2,1,1), F_intrinsic(λ)"),
        ("4+3xy", "4+3xy"),
    ]
)
def test_jacobian_conjecture_session_using_dollar_delimiters(source, expected):
    assert renderLatex(source) == expected


# --- satellite calculation session using bracket delimiters ---------------------------------------


@defineCases(
    [
        (
            r"E \approx \frac{0.1\ \text{lux}}{100\ \text{lm/W}} = 0.001\ \text{W/m}^2",
            "E ≈ (0.1 lux)/(100 lm/W) = 0.001 W/m²",
        ),
        (r"\boxed{1\ \text{milliwatt per square metre}}", "[1 milliwatt per square metre]"),
        (r"5\ \text{km}^2 = 5{,}000{,}000\ \text{m}^2", "5 km² = 5,000,000 m²"),
        (
            r"""P_{\text{light}} = 0.001 \times 5{,}000{,}000
= \boxed{5{,}000\ \text{W}}""",
            "P_light = 0.001 × 5,000,000 = [5,000 W]",
        ),
        (
            r"""P_{\text{electric}} = 5\ \text{kW} \times 0.2
= \boxed{1\ \text{kW}}""",
            "P_electric = 5 kW × 0.2 = [1 kW]",
        ),
        (r"\pi(2.5\ \text{km})^2 = 19.6\ \text{km}^2", "π(2.5 km)² = 19.6 km²"),
        (
            r"""0.001\ \text{W/m}^2 \times 19.6 \times 10^6\ \text{m}^2
\approx \boxed{20\ \text{kW optical}}""",
            "0.001 W/m² × 19.6 × 10⁶ m² ≈ [20 kW optical]",
        ),
        (
            r"""1\ \text{kW} \times \frac{1}{3600}\ \text{hour}
= \boxed{0.28\ \text{Wh}}""",
            "1 kW × 1/3600 hour = [0.28 Wh]",
        ),
    ]
)
def test_satellite_calculation_session_using_bracket_delimiters(source, expected):
    assert renderLatex(source) == expected


# --- Jacobian conjecture sessions using parenthesis and bracket delimiters ------------------------


@defineCases(
    [
        (
            r"\det\!\left(\frac{\partial(F_1,F_2,F_3)}{\partial(x,y,z)}\right)=-2.",
            "det((∂(F₁,F₂,F₃))/(∂(x,y,z))) = -2.",
        ),
        (
            r"""\begin{aligned}
F(0,0,-\tfrac14)&=(-\tfrac14,0,0),\\
F(1,-\tfrac32,\tfrac{13}2)&=(-\tfrac14,0,0),\\
F(-1,\tfrac32,\tfrac{13}2)&=(-\tfrac14,0,0).
\end{aligned}""",
            "F(0,0,-1/4) = (-1/4,0,0),\nF(1,-3/2,13/2) = (-1/4,0,0),\nF(-1,3/2,13/2) = (-1/4,0,0).",
        ),
        ("F=(F_1,F_2,F_3)", "F = (F₁,F₂,F₃)"),
        ("F", "F"),
        ("3", "3"),
    ]
)
def test_jacobian_conjecture_sessions_using_parenthesis_and_bracket_delimiters(source, expected):
    assert renderLatex(source) == expected


# --- Jacobian matrix session using dollar delimiters ----------------------------------------------


@defineCases(
    [
        (
            r"""J = \begin{pmatrix}
\frac{\partial f_1}{\partial x} & \frac{\partial f_1}{\partial y} & \frac{\partial f_1}{\partial z} \\
\frac{\partial f_2}{\partial x} & \frac{\partial f_2}{\partial y} & \frac{\partial f_2}{\partial z} \\
\frac{\partial f_3}{\partial x} & \frac{\partial f_3}{\partial y} & \frac{\partial f_3}{\partial z}
\end{pmatrix}""",
            "J = ⎛ (∂ f₁)/(∂ x) │ (∂ f₁)/(∂ y) │ (∂ f₁)/(∂ z) ⎞\n    ⎜ (∂ f₂)/(∂ x) │ (∂ f₂)/(∂ y) │ (∂ f₂)/(∂ z) ⎟\n    ⎝ (∂ f₃)/(∂ x) │ (∂ f₃)/(∂ y) │ (∂ f₃)/(∂ z) ⎠",
        ),
        (
            r"""\begin{aligned}
f_1 &= (1+xy)^3 z + y^2(1+xy)(4+3xy) \\
f_2 &= y + 3x(1+xy)^2 z + 3xy^2(4+3xy) \\
f_3 &= 2x - 3x^2y - x^3z
\end{aligned}""",
            "f₁ = (1+xy)³ z + y²(1+xy)(4+3xy)\nf₂ = y + 3x(1+xy)² z + 3xy²(4+3xy)\nf₃ = 2x - 3x²y - x³z",
        ),
        ("x, y, z", "x, y, z"),
        ("(x, y, z)", "(x, y, z)"),
        (r"(0,\; 0,\; -\tfrac14)", "(0, 0, -1/4)"),
        (r"(-\tfrac14,\; 0,\; 0)", "(-1/4, 0, 0)"),
        (r"(1,\; -\tfrac32,\; \tfrac{13}{2})", "(1, -3/2, 13/2)"),
        (r"(-1,\; \tfrac32,\; \tfrac{13}{2})", "(-1, 3/2, 13/2)"),
        (r"(-\frac14, 0, 0)", "(-1/4, 0, 0)"),
        (r"F: \mathbb{C}^3 \to \mathbb{C}^3", "F: ℂ³ → ℂ³"),
        (
            r"F(0,0,-\tfrac14) = F(1,-\tfrac32,\tfrac{13}{2}) = F(-1,\tfrac32,\tfrac{13}{2}) = (-\tfrac14, 0, 0)",
            "F(0,0,-1/4) = F(1,-3/2,13/2) = F(-1,3/2,13/2) = (-1/4, 0, 0)",
        ),
        (r"\mathbb{C}^3", "ℂ³"),
        (
            r"""\begin{aligned}
f_1 &= \frac{f_1^{\text{ut}}(u,t)}{x^2}, \quad
f_2 = \frac{f_2^{\text{ut}}(u,t)}{x}, \quad
f_3 = x\,(2 - 3u - t)
\end{aligned}""",
            "f₁ = (f₁ᵘᵗ(u,t))/(x²), f₂ = (f₂ᵘᵗ(u,t))/x, f₃ = x (2 - 3u - t)",
        ),
        (r"\det J_F", "det J_F"),
        (r"(-\tfrac14, 0, 0)", "(-1/4, 0, 0)"),
        ("u = xy", "u = xy"),
        ("t = x^2z", "t = x²z"),
        (r"x \neq 0", "x ≠ 0"),
        (r"f_1^{\text{ut}}, f_2^{\text{ut}}", "f₁ᵘᵗ, f₂ᵘᵗ"),
        ("u,t", "u,t"),
        ("x", "x"),
        ("x, x^2", "x, x²"),
        (r"\mathbb{C}^n \to \mathbb{C}^n", "ℂⁿ → ℂⁿ"),
        (r"n \geq 2", "n ≥ 2"),
        (r"\mathbb{P}^3", "ℙ³"),
    ]
)
def test_jacobian_matrix_session_using_dollar_delimiters(source, expected):
    assert renderLatex(source) == expected


# --- extended formulas from a renderer stress-test session ----------------------------------------


@defineCases(
    [
        (r"e^{i\pi}+1=0", "e^(iπ)+1 = 0"),
        (
            r"""\boxed{
\mathcal{Z}(\beta)
=
\int_{\mathcal M}
\exp\!\left(
-\beta\left[
\frac12 g^{ij}(x)\,\partial_i\phi\,\partial_j\phi
+V(\phi)
\right]\right)
\mathcal D\phi
}""",
            "[Z(β) = ∫_M exp( -β[ 1/2 gⁱʲ(x) ∂ᵢϕ ∂ⱼϕ +V(ϕ) ]) Dϕ]",
        ),
        (
            r"""\begin{aligned}
\nabla_\mu T^{\mu\nu}
&=
\frac{1}{\sqrt{-g}}
\partial_\mu\!\left(\sqrt{-g}\,T^{\mu\nu}\right)
+\Gamma^\nu_{\mu\lambda}T^{\mu\lambda}
=0, \\[4pt]
R_{\mu\nu}-\frac12 Rg_{\mu\nu}+\Lambda g_{\mu\nu}
&=
\frac{8\pi G}{c^4}T_{\mu\nu}.
\end{aligned}""",
            "∇_μ T^(μν) = 1/(√(-g)) ∂_μ(√(-g) T^(μν)) +Γ^ν_(μλ)T^(μλ) = 0,\nR_(μν)-1/2 Rg_(μν)+Λ g_(μν) = (8π G)/(c⁴)T_(μν).",
        ),
        (
            r"""f(z)
=
\frac{1}{2\pi i}
\oint_{\gamma}
\frac{f(\zeta)}{\zeta-z}\,d\zeta,
\qquad
\det\!\begin{pmatrix}
\lambda-a & -b & 0\\
-c & \lambda-d & -e\\
0 & -f & \lambda-g
\end{pmatrix}
=0.""",
            "\n".join(
                [
                    "f(z) = 1/(2π i) ∮_γ (f(ζ))/(ζ-z) dζ, det⎛ λ-a │ -b  │ 0   ⎞ = 0.",
                    f"{' ' * 40}⎜ -c  │ λ-d │ -e  ⎟",
                    f"{' ' * 40}⎝ 0   │ -f  │ λ-g ⎠",
                ]
            ),
        ),
        (
            r"""\Psi(x,t)=
\sum_{n=1}^{\infty}
\underbrace{
c_n
\sqrt{\frac{2}{L}}
\sin\!\left(\frac{n\pi x}{L}\right)
}_{\text{spatial eigenmode}}
\exp\!\left(-\frac{i\hbar n^2\pi^2}{2mL^2}t\right),
\qquad
|\Psi(x,t)|^2
=
\begin{cases}
\Psi^\ast\Psi, & 0<x<L,\\
0, & \text{otherwise}.
\end{cases}""",
            f"{' ' * 97}⎧ Ψ^∗Ψ if 0 < x < L,\nΨ(x,t) = ∑ₙ₌₁^∞ cₙ √(2/L) sin((nπ x)/L)_(spatial eigenmode) exp(-(iℏ n²π²)/(2mL²)t), |Ψ(x,t)|² = ⎨\n{' ' * 97}⎩ 0    otherwise.",
        ),
        (r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}", "x = (-b±√(b²-4ac))/(2a)"),
        (r"\int_0^\infty e^{-x^2}\,dx=\frac{\sqrt{\pi}}{2}", "∫₀^∞ e^(-x²) dx = (√π)/2"),
        (r"e^{i\theta}=\cos\theta+i\sin\theta", "e^(iθ) = cos θ+i sin θ"),
        (r"\sum_{n=1}^{\infty}\frac{1}{n^2}=\frac{\pi^2}{6}", "∑ₙ₌₁^∞1/(n²) = π²/6"),
        (r"\lim_{x\to 0}\frac{\sin x}{x}=1", "lim[x→0] (sin x)/x = 1"),
        (
            r"""\lim_{n\to\infty}
\left(1+\frac{1}{n}\right)^n=e""",
            "lim[n→∞] (1+1/n)ⁿ = e",
        ),
        (
            r"""\int_0^1 \frac{x^2}{1+x^3}\,dx
=\frac{1}{3}\ln 2""",
            "∫₀¹ x²/(1+x³) dx = 1/3 ln 2",
        ),
        (
            r"""\sum_{k=1}^{n}\frac{k}{k+1}
=n+1-H_{n+1}""",
            "∑ₖ₌₁ⁿk/(k+1) = n+1-Hₙ₊₁",
        ),
        (
            r"""\frac{
  \displaystyle \frac{x^2+1}{x-1}
  -
  \displaystyle \frac{2x}{x+1}
}{
  \displaystyle \frac{x}{x^2-1}
}""",
            "((x²+1)/(x-1) - 2x/(x+1))/(x/(x²-1))",
        ),
        (
            r"""\lim_{x\to 0}
\frac{
  \displaystyle \frac{\sin x}{x}-1
}{
  \displaystyle \frac{e^x-1}{x}-1
}
=0""",
            "lim[x→0] ((sin x)/x-1)/((eˣ-1)/x-1) = 0",
        ),
        (
            r"""\frac{
  1+\displaystyle\frac{1}{1+\frac{1}{x}}
}{
  1-\displaystyle\frac{1}{1-\frac{1}{x}}
}""",
            "(1+1/(1+1/x))/(1-1/(1-1/x))",
        ),
        (
            r"""\sum_{n=1}^{\infty}
\frac{
  \displaystyle \frac{1}{n}-\frac{1}{n+1}
}{
  \displaystyle 1+\frac{1}{n^2}
}""",
            "∑ₙ₌₁^∞ (1/n-1/(n+1))/(1+1/(n²))",
        ),
    ]
)
def test_extended_formulas_from_a_renderer_stress_test_session(source, expected):
    assert renderLatex(source) == expected


# --- single cases ---------------------------------------------------------------------------------


def test_renders_common_symbols_roots_sums_and_integrals():
    assert (
        renderLatex(r"\sum_{i=0}^n \alpha_i + \int_0^\infty e^{-x^2}\,dx = \sqrt{\pi}")
        == "∑ᵢ₌₀ⁿ αᵢ + ∫₀^∞ e^(-x²) dx = √π"
    )


def test_renders_common_accents_and_binomial_notation():
    assert renderLatex(r"\binom{n}{k}+\vec{x}+\hat{y}+\overline{AB}") == "(n choose k)+x\u20d7+y\u0302+overline(AB)"


def test_renders_extended_symbols_and_negated_relations():
    assert (
        renderLatex(r"\epsilon+\varepsilon+\varsigma+\varkappa+\oplus+\otimes+\therefore+\because") == "ϵ+ε+ς+ϰ+⊕+⊗+∴+∵"
    )
    assert renderLatex(r"A\not\subseteq B,\quad x\not\in X") == "A ⊈ B, x ∉ X"


def test_renders_relational_algebra_join_operators():
    assert renderLatex(r"R\bowtie S,\quad R\Join S") == "R ⋈ S, R ⋈ S"
    assert renderLatex(r"R\ltimes S,\quad R\rtimes S") == "R ⋉ S, R ⋊ S"
    assert renderLatex(r"R\leftouterjoin S,\quad R\rightouterjoin S,\quad R\fullouterjoin S") == "R ⟕ S, R ⟖ S, R ⟗ S"


def test_renders_delimiter_commands_and_invisible_delimiters():
    assert renderLatex(r"\lvert{x}\rvert+\lVert{v}\rVert+\left.\frac{dy}{dx}\right|_{x=0}") == "|x|+‖v‖+dy/(dx)|ₓ₌₀"
    assert renderLatex(r"\left\lbrace x \middle| x>0 \right\rbrace") == "{ x | x > 0 }"


def test_renders_named_modular_overlaid_and_underlaid_operators():
    assert renderLatex(r"\operatorname*{arg\,max}_{x\in X} f(x)") == "arg max[x∈X] f(x)"
    assert renderLatex(r"a\bmod n,\quad a\equiv b\pmod n") == "a mod n, a ≡ b (mod n)"
    assert renderLatex(r"\overset{!}{=}+\underset{n}{x}+\stackrel{def}{=}") == "=^!+xₙ+=ᵈᵉᶠ"


def test_renders_indexed_roots_and_additional_accents_and_wrappers():
    assert renderLatex(r"\sqrt[2]{x}+\sqrt[3]{x}+\sqrt[4]{x}+\sqrt[n]{x}+\sqrt[k]{x+1}") == "√x+∛x+∜x+ⁿ√x+ᵏ√(x+1)"
    assert (
        renderLatex(r"\acute{x}+\grave{y}+\widehat{xyz}+\overrightarrow{AB}")
        == "x\u0301+y\u0300+widehat(xyz)+overrightarrow(AB)"
    )
    assert (
        renderLatex(
            r"\textnormal{hello}+\mbox{world}+\boldsymbol{x}+{\rm roman}+{\bf bold}+{\it italic}+{\sf sans}+{\tt mono}+{\cal calligraphic}+{\sl slanted}",
        )
        == "hello+world+x+roman+bold+italic+sans+mono+calligraphic+slanted"
    )


def test_renders_additional_display_environments():
    assert renderLatex(r"\begin{equation}\begin{split}a&=b\\&=c\end{split}\end{equation}") == "a = b\n= c"
    assert renderLatex(r"\begin{alignedat}{2}a&=b&\quad c&=d\\e&=f&g&=h\end{alignedat}") == "a = b c = d\ne = f g = h"


def test_uses_natural_case_conditions_and_aligns_matrix_columns():
    assert (
        renderLatex(r"f(x)=\begin{cases}a & x<0 \\ b & \text{if }x=0 \\ c & \text{otherwise}\end{cases}")
        == "       ⎧ a if x < 0\nf(x) = ⎨ b if x = 0\n       ⎩ c otherwise"
    )
    assert renderLatex(r"\begin{pmatrix}1&200\\3000&4\end{pmatrix}") == "⎛ 1    │ 200 ⎞\n⎝ 3000 │ 4   ⎠"


def test_composes_matrices_with_fractions_and_adjacent_matrices():
    assert (
        renderLatex(
            r"""R\left(\frac{\pi}{4}\right)
=
\begin{pmatrix}
\frac{\sqrt{2}}{2} & -\frac{\sqrt{2}}{2}\\
\frac{\sqrt{2}}{2} & \frac{\sqrt{2}}{2}
\end{pmatrix}.""",
            DISPLAY,
        )
        == "   π\nR( ─ ) = ⎛ (√2)/2 │ -(√2)/2 ⎞\n   4     ⎝ (√2)/2 │ (√2)/2  ⎠."
    )
    assert (
        renderLatex(
            r"""\mathbf w
=
R\left(\frac{\pi}{4}\right)
\begin{pmatrix}1\\0\end{pmatrix}
=
\begin{pmatrix}\frac{\sqrt{2}}{2}\\\frac{\sqrt{2}}{2}\end{pmatrix}.""",
            DISPLAY,
        )
        == "       π\nw = R( ─ ) ⎛ 1 ⎞ = ⎛ (√2)/2 ⎞\n       4   ⎝ 0 ⎠   ⎝ (√2)/2 ⎠."
    )
    assert (
        renderLatex(
            r"A\mathbf e_1=\begin{pmatrix}\pi\\0\end{pmatrix},\qquad A\mathbf e_2=\begin{pmatrix}0\\\frac{1}{\pi}\end{pmatrix}.",
            DISPLAY,
        )
        == "Ae₁ = ⎛ π ⎞, Ae₂ = ⎛ 0   ⎞\n      ⎝ 0 ⎠        ⎝ 1/π ⎠."
    )
    assert (
        renderLatex(r"\sum_{i=0}^n x_i=\begin{pmatrix}a&b\\c&d\end{pmatrix}.", DISPLAY)
        == " n\n ∑  xᵢ = ⎛ a │ b ⎞\ni=0      ⎝ c │ d ⎠."
    )


def test_normalizes_relation_multiplication_and_named_operator_spacing():
    for source in ["x=y", "x =y", "x=\ny", "x\n=\ny"]:
        assert renderLatex(source) == "x = y"
    assert renderLatex("x_{i=0}") == "xᵢ₌₀"
    assert renderLatex(r"x\neq0") == "x ≠ 0"
    assert renderLatex(r"A\to B") == "A → B"
    assert renderLatex(r"\pi\cdot\frac{1}{\pi}") == "π · 1/π"
    assert renderLatex(r"\sin\theta") == "sin θ"
    assert renderLatex(r"\sin^2 x") == "sin² x"
    assert renderLatex(r"-\sin\theta") == "-sin θ"
    assert renderLatex(r"i\sin\theta") == "i sin θ"
    assert renderLatex(r"\det(A)") == "det(A)"


def test_treats_a_backslash_followed_by_a_line_ending_as_control_space():
    source = r"""\boxed{
(1,1,1),\ (1,1,2),\ (1,2,5),\ (1,5,13),\ (2,5,29),\
(1,13,34),\ (1,34,89)
}."""
    assert renderLatex(source, DISPLAY) == "[(1,1,1), (1,1,2), (1,2,5), (1,5,13), (2,5,29), (1,13,34), (1,34,89)]."
    assert renderLatex("a\\\r\nb") == "a b"


def test_stacks_operator_limits_in_display_mode():
    assert renderLatex(r"\sum_{i=0}^n x_i", DISPLAY) == " n\n ∑  xᵢ\ni=0"
    assert renderLatex(r"\min_{x\in X} f(x)", DISPLAY) == "min f(x)\nx∈X"
    assert renderLatex(r"\operatorname*{arg\,max}_{x\in X} f(x)", DISPLAY) == "arg max f(x)\n  x∈X"
    assert renderLatex(r"\int\nolimits_0^1 f(x)\,dx", DISPLAY) == "∫₀¹ f(x) dx"
    assert renderLatex(r"\int\limits_0^1 f(x)\,dx", DISPLAY) == "1\n∫ f(x) dx\n0"


def test_centers_even_case_rows_around_a_middle_brace():
    assert (
        renderLatex(r"f(x) = \begin{cases} x^{2} & x \geq 0 \\ -x & x < 0 \end{cases}")
        == "       ⎧ x² if x ≥ 0\nf(x) = ⎨\n       ⎩ -x if x < 0"
    )


def test_stacks_fractions_in_display_mode():
    assert renderLatex(r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}", DISPLAY) == "    -b±√(b²-4ac)\nx = ────────────\n         2a"
    assert renderLatex(r"\frac{x^2+1}{x-1}", DISPLAY) == "x²+1\n────\nx-1"
    assert renderLatex("\\frac{1}\n{2}", DISPLAY) == "1\n─\n2"


def test_keeps_nested_display_fractions_linear():
    cases = [
        (
            r"\frac{\frac{x^2+1}{x-1}-\frac{2x}{x+1}}{\frac{x}{x^2-1}}",
            "(x²+1)/(x-1)-2x/(x+1)\n─────────────────────\n      x/(x²-1)",
        ),
        (
            r"\lim_{x\to 0}\frac{\frac{\sin x}{x}-1}{\frac{e^x-1}{x}-1}=0",
            "     (sin x)/x-1\nlim  ─────────── = 0\nx→0  (eˣ-1)/x-1",
        ),
        (
            r"\frac{1+\frac{1}{1+\frac{1}{x}}}{1-\frac{1}{1-\frac{1}{x}}}",
            "1+1/(1+1/x)\n───────────\n1-1/(1-1/x)",
        ),
    ]
    for source, expected in cases:
        assert renderLatex(source, DISPLAY) == expected


def test_lays_out_unsupported_and_nested_scripts_while_keeping_script_fractions_linear():
    assert (
        renderLatex(r"\partial_tU_2(t,0)=Aj_*(1-t)^{-A-1}.\qquad x^{n^2}+x_{i_j}", DISPLAY)
        == "                            2\n                    -A-1   n\n∂ₜU₂(t,0) = Aj (1-t)    . x  +x\n              *                i\n                                j"
    )
    assert renderLatex(r"e^{\frac{1}{2}}+\tfrac{1}{2}", DISPLAY) == "e^(1/2)+1/2"


def test_returns_undefined_for_unsupported_commands():
    assert renderLatex(r"x + \unknown{y}") is None


def test_returns_undefined_for_malformed_groups_and_environments():
    malformed = [r"\frac{1}{x", "x}", r"\begin{matrix}1 & 2", "x\\"]
    for source in malformed:
        assert renderLatex(source) is None


def test_an_empty_matrix_or_cases_body_renders_as_nothing():
    # MISAKA: `Math.max(0, ...[])` is 0; Python's `max(0, *())` raised TypeError. Values recorded from upstream.
    for display in (False, True):
        options = RenderLatexOptions(display=display)
        assert renderLatex(r"\begin{array}{cc}\scriptstyle{}\end{array}", options) == ""
        assert renderLatex(r"\begin{pmatrix}\end{pmatrix}", options) == ""
        assert renderLatex(r"x\begin{cases}\end{cases}y", options) == "xy"
