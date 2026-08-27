"""The last mile between a report and its evidence.

The ledger (``misaka/research/ledger.py``) already pins every claim to a sha-locked verbatim
quote, so the evidence end of the chain is hard. The other end was not checked at all: a report
could cite ``[7]`` when only five sources exist, cite a source whose URL is not the one it names,
or state a figure that appears in none of the evidence it points at. Prompts asked for care;
nothing measured it.

These are pure functions over ``(body, sources)`` -- no IO, no network, no database -- so the gate
is testable without a research run, and the caller decides what a failure costs (rewrite, or
deliver with the list attached).

What the numeric check looks at is deliberately narrow. A gate that misfires daily gets switched
off, so this one under-reports on purpose: only numbers that carry a unit, a magnitude, a
thousands separator, a currency mark, or scientific notation are treated as value claims. Bare
integers and decimals (page numbers, section numbers, counts, versions, list markers) and anything
inside code spans or a URL are left alone. A year is treated more gently still: it is usually the
scope of a sentence rather than a claim, and the one sentence a ledger quote holds is often not the
one carrying the date, so a year is only ever reported when another cited source does carry it --
which is citation drift, not fabrication. See ``_REPORT_NUMBER_RE``.

Messages here quote the report back at the caller, and a report can carry text a page wanted the
model to repeat. Anything rendered from ``Problem`` into a model-facing prompt must go through
``misaka.platform.prompt_guard.untrusted`` at the call site.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Source:
    """One numbered entry of the report's source list; its position in the sequence is its ``[N]``.

    ``evidence`` is the sha-locked text the citation stands on -- a ledger claim's quote, or the
    whole artifact when the caller wants the harder check. ``label`` and ``url`` are what the
    reader is told the source is, and the URL is what a misalignment is measured against.
    """

    evidence: str
    url: str = ""
    label: str = ""


ProblemKind = Literal[
    "citation_missing",     # sources exist, the body cites none of them
    "citation_dangling",    # [N] has no ledger entry behind it
    "url_mismatch",         # the URL written next to [N] is not [N]'s URL in the ledger
    "number_uncited",       # a value claim with no source to check it against
    "number_ungrounded",    # the number is in none of the evidence it cites
    "number_wrong_source",  # the number is in another cited source's evidence, not this one
]


@dataclass(frozen=True, slots=True)
class Problem:
    """One actionable defect: what is wrong, and where in the body to look."""

    kind: ProblemKind
    message: str
    line: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class Audit:
    """``body`` and ``sources`` are the repaired report and the source list that goes with it."""

    body: str
    sources: tuple[Source, ...]
    problems: tuple[Problem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


# -- citation markers ---------------------------------------------------------------------------

# A canonical marker, and a group that may hold several ("[1, 3]" / "[1、3]"). The group body is
# digits and separators only, so "[citation needed]" and "[1, foo]" are left as prose.
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
_CITE_GROUP_RE = re.compile(r"\[(\s*\d[\d,;、\s]*)\]")
_FULLWIDTH_GROUP_RE = re.compile(r"【(\s*\d[\d,;、\s]*)】")
# A citation written with descriptive junk inside the bracket -- "[2的相关背景]", "[2 - see above]".
# Borrowed from FrontierAgent's normalize_malformed_citations: the leading 1-3 digits are a
# plausible index, and the character after them must be neither a digit nor a separator, so a
# multi-citation ("[2, 3]") and a four-digit year ("[2024年]") are both left untouched.
# Two guards FrontierAgent does not have, both against turning prose into a citation that then
# binds numbers to a source: the tail may not cross a newline (an unclosed "[1 " otherwise eats
# whole paragraphs up to the next "]"), and a markdown link ("[1. 背景](#bg)") is left alone.
_MALFORMED_RE = re.compile(r"\[\s*(\d{1,3})\s*[^\d,;、\s\]\n][^\]\n]*\](?!\()")
# A markdown footnote the model minted for a claim it had no source for. It never writes the
# matching definition, so the marker resolves to nothing and renders as literal "[^x]" text; the
# contract says an unsourced claim stays as analysis without a marker, which is what stripping it
# leaves behind. One leading space is eaten so "word [^x] more" does not collapse to a double
# space; CJK text has no such space, so nothing is over-consumed there.
_FOOTNOTE_REF_RE = re.compile(r" ?\[\^([A-Za-z0-9_-]+)\]")
_FOOTNOTE_DEF_RE = re.compile(r"(?m)^[ \t]*\[\^([A-Za-z0-9_-]+)\]:.*(?:\n|$)")


def _normalize_markers(text: str) -> str:
    """Bring off-contract citation shapes back to ``[N]`` and drop the ones that resolve to nothing."""
    # Both rewrites are held off code spans and fences: "d[2 if x else 3]" in a snippet is not a
    # malformed citation, and collapsing it to "d[2]" corrupts the report on the way out.
    text = _sub_unmasked(_FULLWIDTH_GROUP_RE, lambda m: f"[{m.group(1)}]", text)
    text = _sub_unmasked(_MALFORMED_RE, lambda m: f"[{m.group(1)}]", text)
    defined = {m.group(1) for m in _FOOTNOTE_DEF_RE.finditer(text)}
    return _FOOTNOTE_REF_RE.sub(lambda m: m.group(0) if m.group(1) in defined else "", text)


def _group_numbers(body: str, limit: int) -> list[int]:
    """The citation indices a bracket group carries, or nothing when it is not a citation at all.

    A lone ``[7]`` is a citation attempt however wrong the number is -- that is what the dangling
    check is for. A group of several numbers is only a citation when every one of them resolves:
    reports quote numeric ranges and id pairs verbatim from source tables (``[684, 821]``,
    ``[1,000]``), and reading those as citations invents dangling markers that no rewrite can fix.
    The cost is that a genuine ``[1, 99]`` goes unread as a citation; the numbers around it are
    then reported as uncited instead, which still stops the report.
    """
    numbers = [int(n) for n in re.findall(r"\d+", body)]
    if len(numbers) > 1 and not all(1 <= n <= limit for n in numbers):
        return []
    return numbers


def _markers(text: str, limit: int) -> list[tuple[int, int, int]]:
    """Every citation occurrence as ``(index, start, end)``, multi-citation groups included.

    Callers pass the masked body (see ``_mask``): a ``[7]`` inside a code span is a subscript, not
    a citation, and reading it as one both invents a dangling marker and lets renumbering rewrite
    someone's code on the way out.
    """
    out: list[tuple[int, int, int]] = []
    for match in _CITE_GROUP_RE.finditer(text):
        for number in _group_numbers(match.group(1), limit):
            out.append((number, match.start(), match.end()))
    return out


def _sub_unmasked(pattern: re.Pattern[str], repl, text: str) -> str:
    """``pattern.sub`` that skips matches falling inside code spans, fences and URLs."""
    masked = _mask(text)
    return pattern.sub(
        lambda m: repl(m) if masked[m.start():m.end()] == m.group(0) else m.group(0), text)


def _renumber(text: str, sources: Sequence[Source]) -> tuple[str, tuple[Source, ...]]:
    """Renumber by first appearance and drop the sources the body never cites.

    Reserved for a body that passed clean: rewriting numbers while a dangling one is still in the
    text would retarget it onto a real source instead of leaving it visibly broken, and rewriting
    them while a *defect* is still in the text hands the caller a report numbered in one space and
    a defect list numbered in another (see ``audit_report``).
    """
    limit = len(sources)

    def expand(match: re.Match[str]) -> str:
        numbers = _group_numbers(match.group(1), limit)
        if len(numbers) <= 1:
            return match.group(0)
        return "".join(f"[{n}]" for n in numbers)

    text = _sub_unmasked(_CITE_GROUP_RE, expand, text)
    order: list[int] = []
    for match in _CITE_RE.finditer(_mask(text)):
        number = int(match.group(1))
        if number not in order and 1 <= number <= limit:
            order.append(number)
    if not order:
        return text, ()
    mapping = {old: new for new, old in enumerate(order, start=1)}
    text = _sub_unmasked(
        _CITE_RE, lambda m: f"[{mapping[int(m.group(1))]}]" if int(m.group(1)) in mapping
        else m.group(0), text)
    return text, tuple(sources[old - 1] for old in order)


# -- URLs ---------------------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s)\]>,;'\"，、]+")
# How far from a marker a URL still reads as that marker's own claim about where it came from:
# a reference row ("[3] Title — https://x") and an inline "(https://x) [3]" both fit.
_URL_WINDOW = 60


def _norm_url(url: str) -> str:
    """Loose canonicalisation: we are looking for a clearly different URL, not for byte equality."""
    url = re.sub(r"^https?://", "", url.strip().rstrip(".,;:)]"), flags=re.IGNORECASE)
    return re.sub(r"^www\.", "", url, flags=re.IGNORECASE).rstrip("/").lower()


def _url_problems(text: str, masked: str, sources: Sequence[Source]) -> list[tuple[int, Problem]]:
    out: list[tuple[int, Problem]] = []
    for number, start, end in _markers(masked, len(sources)):
        if not 1 <= number <= len(sources):
            continue
        recorded = _norm_url(sources[number - 1].url)
        if not recorded:
            continue
        # Scoped to the marker's own line so a reference row two lines down is not read as this
        # marker's link, and so the window never cuts a URL in half.
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end < 0 else line_end
        written = [_norm_url(m.group(0)) for m in _URL_RE.finditer(text, line_start, line_end)
                   if m.start() - end <= _URL_WINDOW and start - m.end() <= _URL_WINDOW]
        if not written or any(u in recorded or recorded in u for u in written):
            continue
        line, excerpt = _locate(text, start)
        out.append((start, Problem(
            "url_mismatch",
            f"引用 [{number}] 旁边写的链接是 {written[0]},但台账里 [{number}] 的来源是 "
            f"{_norm_url(sources[number - 1].url)}。改用台账的编号,或删掉写错的链接。",
            line, excerpt)))
    return out


# -- numbers ------------------------------------------------------------------------------------

# 兆 is ambiguous (mainland 10^12, Taiwan 10^6); the mainland reading is used.
_MAGNITUDE: dict[str, float] = {
    "万亿": 1e12, "兆": 1e12, "千亿": 1e11, "百亿": 1e10, "亿": 1e8,
    "千万": 1e7, "百万": 1e6, "万": 1e4, "千": 1e3,
    "trillion": 1e12, "billion": 1e9, "million": 1e6,
    # Single letters are only reachable behind a currency mark (see _REPORT_NUMBER_RE), so "5m"
    # of prose never parses as five million.
    "t": 1e12, "b": 1e9, "m": 1e6,
}
_MAGNITUDE_ORDER: tuple[str, ...] = tuple(sorted(_MAGNITUDE, key=len, reverse=True))
_CN_ALT = "|".join(w for w in _MAGNITUDE_ORDER if not w.isascii())
# The same ladder minus a bare 千, for numerals spelled out in Chinese: "5千" is unambiguous, but
# "一千" is as often an ordinal ("第一千个用户") as a quantity, and 10^3 is too small a claim to be
# worth the false positives. A Chinese numeral still reaches 千 through 千万 / 千亿 / 一千五百万.
_CN_NUMERAL_ALT = "|".join(w for w in _MAGNITUDE_ORDER if not w.isascii() and w != "千")
_EN_ALT = "trillion|billion|million"
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_SECTIONS = {"万": 10**4, "亿": 10**8}
_CN_CLASS = "".join((*_CN_DIGITS, *_CN_UNITS, *_CN_SECTIONS))
# Every repetition below is bounded, and that is load-bearing rather than tidiness. Evidence text
# comes off a fetched page, so its length and shape are the attacker's to choose; an unbounded
# ``\d+`` or ``[<cjk>]*` followed by a magnitude that never arrives backtracks once per length at
# every start offset, which is quadratic -- 16k digits took 10s, 64k would take minutes. Bounding
# each run makes the work per offset constant. The bounds are far past any real value claim
# (10^18, 12 decimals, 7 comma groups, 16 Chinese numeral characters); a number beyond them is not
# a figure a reader checks, so declining to extract it is the right way to be wrong.
_NUM = r"\d{1,3}(?:,\d{3}){1,7}(?:\.\d{1,12})?|\d{1,18}(?:\.\d{1,12})?"
_CN_RUN = "{0,16}"

# What counts as a value claim in the report body. Every branch requires a unit, a magnitude, a
# currency mark, a thousands separator, or an exponent -- a bare "3" or "1.5" is not a claim this
# gate can tell apart from a page number, so it is not extracted at all.
#
# Branch order is load-bearing: the percent and magnitude branches must precede the plain-currency
# branch, or "$1.5万亿" is consumed as "$1.5" and loses its magnitude. The Chinese-numeral branch
# must start with a digit word so that "千万别" and "万一" are not read as 10^7 and 10^4.
_NUMBER_BRANCHES = (
    rf"百分之(?:{_NUM}|[{_CN_CLASS}]{_CN_RUN})",
    rf"(?:{_NUM})\s*(?:%|％|个百分点)",
    r"(?<![\w.])\d{1,18}(?:\.\d{1,12})?[eE][+-]?\d{1,3}",
    rf"(?:[$¥€£]\s*)?(?:{_NUM})\s*(?:{_CN_ALT})",
    rf"(?:[$¥€£]\s*)?(?:{_NUM})\s*(?:{_EN_ALT})\b",
    rf"[{''.join(_CN_DIGITS)}十][{_CN_CLASS}]{_CN_RUN}(?:{_CN_NUMERAL_ALT})",
    rf"[$¥€£]\s*(?:{_NUM})\s*[BMTbmt]?\b",
    r"(?<![\d.,])\d{1,3}(?:,\d{3}){1,7}(?:\.\d{1,12})?",
    r"(?<![\d.,])\d{4}\s*年",
)
_REPORT_NUMBER_RE = re.compile("|".join(_NUMBER_BRANCHES), re.IGNORECASE)
# The evidence side is the haystack, so it reads bare numbers too: a wider haystack can only make
# the gate quieter, which is the direction we want to be wrong in.
_EVIDENCE_NUMBER_RE = re.compile(
    "|".join((*_NUMBER_BRANCHES, r"(?<![\d.,])\d{1,18}(?:\.\d{1,12})?(?![\d,])")), re.IGNORECASE)

# Sentence-sized binding window. A number is checked against the sources cited in its own
# sentence: the markers that follow it, or -- for "根据[3],2024年营收为15亿" -- the ones before it.
_SEGMENT_RE = re.compile(r"[^\n。！？；;!?]+")
_CLUSTER_GAP = 4   # "[3][7]" and "[3] [7]" are one citation; text between them ends it
_SENTENCE_TAIL = 6  # so a marker parked after the full stop ("...15亿。[3]") still binds


def _cn_number(text: str) -> float | None:
    """Parse a Chinese numeral ("十五" -> 15, "一千五百万" -> 15000000). Digits-only forms return None."""
    total = section = current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (current or 1) * _CN_UNITS[char]
            current = 0
        elif char in _CN_SECTIONS:
            total += (section + current) * _CN_SECTIONS[char]
            section = current = 0
        else:
            return None
    return float(total + section + current)


def _key(value: float) -> float:
    """Comparison key: 1.5万 and 15,000 must land on the same float despite different arithmetic."""
    return float(f"{value:.9g}")


def _sig_round(value: float, digits: int) -> float:
    if value == 0:
        return 0.0
    return round(value, digits - 1 - math.floor(math.log10(abs(value))))


def _parse(token: str) -> tuple[str, float, int] | None:
    """``(kind, value, significant digits)`` for a matched token, or None when it does not parse."""
    text = token.strip().replace(",", "").replace(" ", "").replace("　", "")
    kind = "plain"
    if text.startswith("百分之"):
        kind, text = "percent", text[3:]
    elif text.endswith(("%", "％")):
        kind, text = "percent", text[:-1]
    elif text.endswith("个百分点"):
        kind, text = "percent", text[:-4]
    elif text.endswith("年"):
        kind, text = "year", text[:-1]
    text = text.lstrip("$¥€£")
    arabic = re.fullmatch(r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(.*)", text)
    if arabic:
        number, suffix = arabic.group(1), arabic.group(2)
        multiplier = 1.0 if not suffix else _MAGNITUDE.get(suffix.lower(), 0.0)
        if not multiplier:
            return None
        mantissa = re.split("[eE]", number)[0].replace(".", "").lstrip("0")
        value = float(number) * multiplier
        if not math.isfinite(value):
            # "1e999" off a fetched page parses to inf, and inf reaches math.floor() in
            # _sig_round, which raises OverflowError and takes the whole gate down with it.
            return None
        return kind, value, len(mantissa) or 1
    # A Chinese numeral carries its own magnitudes ("一千五百万"), so it is parsed whole: stripping
    # a "百万" suffix off the end would eat half of the numeral in front of it.
    value = _cn_number(text)
    if value is None:
        return None
    return kind, value, len(f"{int(value)}".rstrip("0")) or 1


def _evidence_values(text: str) -> tuple[set[float], set[float]]:
    """``(plain, percent)`` value keys occurring in one source's evidence."""
    plain: set[float] = set()
    percent: set[float] = set()
    for match in _EVIDENCE_NUMBER_RE.finditer(text):
        parsed = _parse(match.group(0))
        if parsed is None:
            continue
        kind, value, _digits = parsed
        (percent if kind == "percent" else plain).add(_key(value))
    return plain, percent


def _grounded(parsed: tuple[str, float, int], values: tuple[set[float], set[float]]) -> bool:
    """Whether a report number occurs in one source's evidence, across notations.

    Cross-notation equality is done on values rather than on generated strings, which is what makes
    1.5万 / 15000 / 15,000 / 1.5e4 one number for free. The percent axis is explicit (15% grounds
    against 百分之十五, "15 percent" and 0.15); the rest is arithmetic.
    """
    kind, value, digits = parsed
    plain, percent = values
    pairs = ([(value, percent), (value, plain), (value / 100, plain)] if kind == "percent"
             else [(value, plain), (value * 100, percent)])
    if any(_key(candidate) in pool for candidate, pool in pairs):
        return True
    # A writer who rounds ("15.2%", "1.5万") against a source carrying the full figure ("15.23%",
    # "15234") is quoting it, not inventing it. Two significant digits minimum, so "5亿" still has
    # to match exactly rather than swallowing anything from 450 million up.
    return digits >= 2 and any(
        _key(_sig_round(known, digits)) == _key(candidate)
        for candidate, pool in pairs for known in pool)


# Regions whose digits are never claims: fenced and inline code, URLs, footnote ids. Replaced by
# spaces rather than removed, so every offset still points at the same place in the body.
_MASK_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`|https?://\S+|\[\^[A-Za-z0-9_-]+\]")


def _mask(text: str) -> str:
    return _MASK_RE.sub(lambda m: " " * len(m.group(0)), text)


def _locate(text: str, offset: int) -> tuple[int, str]:
    line = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    end = len(text) if end < 0 else end
    if end - start > 90:
        start, end = max(start, offset - 40), min(end, offset + 50)
    return line, text[start:end].strip()


def _bound_citations(markers: list[tuple[int, int, int]], span: tuple[int, int],
                     token: tuple[int, int]) -> list[int]:
    """The citation cluster a number belongs to: the one right after it, else the one before it."""
    start, end = span
    inside = [m for m in markers if start <= m[1] and m[2] <= end + _SENTENCE_TAIL]
    after = [m for m in inside if m[1] >= token[1]]
    before = [m for m in inside if m[2] <= token[0]]
    cluster: list[tuple[int, int, int]] = []
    if after:
        for marker in after:
            if cluster and marker[1] - cluster[-1][2] > _CLUSTER_GAP:
                break
            cluster.append(marker)
    elif before:
        for marker in reversed(before):
            if cluster and cluster[0][1] - marker[2] > _CLUSTER_GAP:
                break
            cluster.insert(0, marker)
    return list(dict.fromkeys(marker[0] for marker in cluster))


def _number_problems(text: str, masked: str,
                     sources: Sequence[Source]) -> list[tuple[int, Problem]]:
    markers = _markers(masked, len(sources))
    cache: dict[int, tuple[set[float], set[float]]] = {}

    def values(index: int) -> tuple[set[float], set[float]]:
        if index not in cache:
            cache[index] = _evidence_values(sources[index - 1].evidence)
        return cache[index]

    out: list[tuple[int, Problem]] = []
    for segment in _SEGMENT_RE.finditer(masked):
        for match in _REPORT_NUMBER_RE.finditer(masked, segment.start(), segment.end()):
            parsed = _parse(match.group(0))
            if parsed is None:
                continue
            token = match.group(0).strip()
            cited = _bound_citations(markers, (segment.start(), segment.end()),
                                     (match.start(), match.end()))
            live = [n for n in cited if 1 <= n <= len(sources)]
            if cited and not live:
                continue  # every marker on it is dangling; that is already reported
            line, excerpt = _locate(text, match.start())
            if not live:
                if parsed[0] == "year":
                    continue  # an uncited year is the scope of a sentence, not a claim
                out.append((match.start(), Problem(
                    "number_uncited",
                    f"数字「{token}」没有引用任何来源。给它加上对应的 [N],或者改写成不含具体数字的表述。",
                    line, excerpt)))
                continue
            if any(_grounded(parsed, values(n)) for n in live):
                continue
            elsewhere = [n for n in range(1, len(sources) + 1)
                         if n not in live and _grounded(parsed, values(n))]
            if parsed[0] == "year" and not elsewhere:
                # A ledger quote is one sentence, and the sentence that carries the figure often
                # is not the one that carries the year -- so a missing year is far more often the
                # quote's silence than the writer's invention. A year is only worth reporting when
                # another cited source does carry it, which points at citation drift.
                continue
            marks = "".join(f"[{n}]" for n in live)
            if elsewhere:
                out.append((match.start(), Problem(
                    "number_wrong_source",
                    f"数字「{token}」不在 {marks} 的证据原文里,但出现在 "
                    f"{''.join(f'[{n}]' for n in elsewhere)} 的证据里 —— 引用编号写错了。",
                    line, excerpt)))
            else:
                out.append((match.start(), Problem(
                    "number_ungrounded",
                    f"数字「{token}」在它引用的 {marks} 的证据原文里找不到。"
                    "换成证据里确实有的数字,或者删掉这个数字。",
                    line, excerpt)))
    return out


def cited(body: str, sources: Sequence[Source]) -> set[int]:
    """The source numbers ``body`` actually resolves against, multi-citation groups included.

    A caller that wants to print the source list cannot re-derive this with a plain ``\\[(\\d+)\\]``
    scan: only this module's marker reader knows that a subscript in a code span is not a citation
    and that ``[1, 3]`` is two of them. And it must be read against the body it is delivering --
    which is numbered in the caller's own space or in the renumbered one, depending on whether the
    report passed (see ``audit_report``).
    """
    limit = len(sources)
    return {n for n, _s, _e in _markers(_mask(body), limit) if 1 <= n <= limit}


def audit_report(body: str, sources: Sequence[Source]) -> Audit:
    """Check a report against the ledger entries it was written from, and repair what is mechanical.

    Repairs: off-contract citation shapes are normalised to ``[N]`` and footnote markers that
    resolve to nothing are stripped -- always; and, *only when the report comes back clean*,
    citations are renumbered by first appearance and uncited sources are dropped from the returned
    list, so the numbers climb as the reader reads.

    That renumbering waits for a clean pass because it is a change of numbering space, and only one
    numbering space may ever exist at a time. The caller holds a source listing in its own
    numbering, and hands the model that listing together with these ``Problem`` messages and this
    ``body``; if the body and the messages were renumbered while the listing was not, a defect
    reported as ``[2]`` would name the caller's ``[3]``, the model would obediently move the claim
    onto the wrong source, and the second audit -- run against the caller's sources again -- would
    pass the mis-attribution clean. So a report with defects comes back in exactly the numbering it
    was written in, and only a report with nothing left to say gets renumbered for delivery.

    Findings (never repaired, because only the writer knows what was meant): dangling citations,
    URLs that disagree with the ledger, and numbers that are not in the evidence they cite.
    ``sources`` is 1-indexed against the body: ``sources[0]`` is ``[1]``.
    """
    sources = tuple(sources)
    text = _normalize_markers(body or "")
    masked = _mask(text)
    found: list[tuple[int, Problem]] = []
    seen: set[int] = set()
    for number, start, _end in _markers(masked, len(sources)):
        if 1 <= number <= len(sources) or number in seen:
            continue
        seen.add(number)
        line, excerpt = _locate(text, start)
        available = f"可用编号 1..{len(sources)}" if sources else "台账里没有任何可引用的条目"
        found.append((start, Problem(
            "citation_dangling",
            f"引用 [{number}] 没有对应的台账条目({available})。", line, excerpt)))
    resolves = any(1 <= n <= len(sources) for n, _s, _e in _markers(masked, len(sources)))
    if not found and sources and not resolves:
        found.append((0, Problem(
            "citation_missing",
            f"报告里没有任何 [N] 引用,但台账有 {len(sources)} 条可引用的证据。"
            "每一条有据可依的结论都要标上来源编号。", 1, _locate(text, 0)[1])))
    found += _url_problems(text, masked, sources)
    found += _number_problems(text, masked, sources)
    found.sort(key=lambda item: item[0])
    # One line that repeats a defect (a marker cited twice next to the same wrong link) is one
    # thing to fix, not several.
    unique: dict[tuple[str, int, str], Problem] = {}
    for _offset, problem in found:
        unique.setdefault((problem.kind, problem.line, problem.message), problem)
    if not unique and resolves:
        text, sources = _renumber(text, sources)
    return Audit(text, sources, tuple(unique.values()))
