"""Dictionary-backed keyword tries, keyword sets, and numbering tables."""

from __future__ import annotations

import json
import re
from pathlib import Path

import regex as regex_module  # Unicode \p{...} property classes.

from ..model import (
    _UNICODE_WHITESPACE_CLASS,
    _strip_diacritics,
)
from ..tokens import (
    TrieConfig,
    build_trie,
    set_case_fold,
)

# --------------------------------------------------------------------------- #
# Dictionary tries (case-folded) #
# --------------------------------------------------------------------------- #


_DICT_PATH = Path(__file__).parent.parent / "data" / "dictionaries.json"
_DICTS = json.loads(_DICT_PATH.read_text(encoding="utf-8"))

SECTION_KEYWORDS_TRIE = build_trie(_DICTS.get("section_keywords", []), set_case_fold(TrieConfig(), True))     # general sections
ABSTRACT_KEYWORDS_TRIE = build_trie(_DICTS.get("abstract_keywords", []), set_case_fold(TrieConfig(), True))    # abstract
REFERENCES_TRIE = build_trie(_DICTS.get("references", []), set_case_fold(TrieConfig(), True))           # references
APPENDIX_SECTION_TRIE = build_trie(_DICTS.get("appendices_dict", []), set_case_fold(TrieConfig(), True))      # appendix
INTRODUCTION_SECTION_TRIE = build_trie(_DICTS.get("introduction_dict", []), set_case_fold(TrieConfig(), True))    # introduction
BOX_KEYWORD_TRIE = build_trie(["box"], set_case_fold(TrieConfig(), True))
KEYWORDS_SECTION_TRIE = build_trie(_DICTS.get("keywords_dict", []), set_case_fold(TrieConfig(), True))        # keywords
CHAPTER_WORDS_TRIE = build_trie(_DICTS.get("chapter_words", []), set_case_fold(TrieConfig(), True))         # chapter
APPENDIX_KEYWORDS_TRIE = build_trie(_DICTS.get("appendix_keywords", []), set_case_fold(TrieConfig(), True))    # appendix (hi)

# Whole-text lookup sets use normalized lowercase strings. The normalization is
# NFD -> strip combining marks (U+0300-U+036F) -> NFC; it is diacritic stripping,
# not compatibility folding.
def _normalize_text_key(text: str) -> str:
    return _strip_diacritics(text)

# Whole-text lookup sets for abstract and references headings.
# Abstract headings are matched diacritic-insensitively; references are not.
ABSTRACT_KEYWORDS_SET = frozenset(_strip_diacritics(text_value.lower()) for text_value in _DICTS.get("abstract_keywords", []))
REFERENCES_SET = frozenset(text_value.lower() for text_value in _DICTS.get("references", []))


# Numbered heading prefix: leading ASCII/fullwidth 1-9, followed by Unicode
# numeric code points, punctuation, and whitespace or uppercase lookahead. The
# leading class deliberately excludes fullwidth zero (U+FF10).
NUMBERED_PREFIX_RE = regex_module.compile(r"^([1-9１-９]\p{Number}*)[ .-](?:[" + _UNICODE_WHITESPACE_CLASS + r"]|\p{Lu})")
# Equation separator fallback. This intentionally matches only the literal
# string pattern around ``p{Number}``, so the branch remains inert for ordinary
# numeric text.
DEAD_DIGIT_RE = re.compile(r"^.p\{Number\}+.$")

# Trie of equation-like keywords ("equation", "eqn", "eq", plus multilingual
# variants).
EQUATION_KEYWORDS_TRIE = build_trie(
    [
        "equation", "equation.", "eqn", "eqn.", "eq", "eq.",
        "ecuación", "equação", "gleichung", "equazione", "ekvation",
        "yhtälö", "ligning", "persamaan", "denklem", "ecuația",
        "equació", "rovnica", "rovnice", "równanie", "vergelijking",
        "jednadžba", "jöfnu", "võrrand", "vienādojums", "lygtis",
        "enačba", "egyenlet", "phương trình", "εξίσωση",
        "方程", "방정식", "уравнение", "рівняння", "раўнанне", "једначина",
    ],
    set_case_fold(TrieConfig(), True),
)


# Roman and English number words used by heading numbering detectors.
ENGLISH_WORD_TO_NUMBER = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
ROMAN_NUMERAL_MAP = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
    "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
    "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20,
}


# Special-character weights used by equation-content scoring.
FORMULA_CHAR_WEIGHTS = {
    "=": 10, "{": 5, "}": 5, "+": 5, "/": 3, "*": 3,
    "-": 1, "~": 1, "[": 1, "]": 1, "(": 1, ")": 1,
}
