"""Tokenizer subsystem. The tokenizer is character-driven: it walks each character of each line,
uses category-transition tolerances to decide when the current token can
extend, and closes tokens when script, punctuation, or spacing transitions
require a boundary. Tokens keep back-references to the contributing line and span offsets so the
visible text can be reconstructed and cross-line tokens, such as a word broken
by a hyphen across two lines, can be stitched.
"""

import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any, Optional

from ..model import (
    EMPTY_RECT,
    Block,
    Line,
    _strip_diacritics,
    avg_char_width,
    avg_char_width2,
    char_category,
    info_weight,
    intervals_overlap,
    is_punct_category,
    is_word_category,
    letter_count,
    punct_count,
    rect_union,
    to_number,
)
from .hashing import (
    _FH_MASK,
    _int32_left_shift,
    _int32_xor,
    _jenkins_mix,
    _little_endian_signed_word,
    _to_int32,
    _to_uint32,
    _uint32_right_shift,
    _utf8_bytes_from_utf16_units,
    jenkins_hash,
)
from .token_types import (
    GAP_TOLERANCE_GRID,
    SCRIPT_FAMILY_MAP,
    Token,
    TokenAnchor,
    TokenView,
    _build_gap_tolerance_grid,
    can_extend_token,
    enumerate_tokens,
    first_anchor_span,
    first_token,
    is_char_token,
    is_trimmable_token,
    is_word_token,
    last_token,
    last_token_anchor,
    token_numeric_value,
    wrap_tokens,
)
from .tokenizer import (
    LineTokenizer,
    clamp_value,
    is_superscript_adjacent,
    tokenize_block,
)
from .tries import (
    COMMA_CHARS,
    BuiltTrie,
    TrieBuilder,
    TrieConfig,
    TrieNode,
    _de_norm,
    _trie_finalize,
    _trie_full_match,
    _trie_insert_entry,
    aho_corasick_match,
    aho_corasick_tokens,
    build_trie,
    is_comma_token,
    set_case_fold,
    set_reverse,
    strip_leading_if_in,
    strip_trailing_comma,
    strip_trie_match,
    trie_bulk_insert,
    trie_full_match,
    trie_insert_step,
    trie_prefix_match,
    trie_walk_step,
    trim_trailing_punct,
)

__all__ = [
    # state machine
    "GAP_TOLERANCE_GRID", "can_extend_token", "TokenAnchor", "last_token_anchor", "first_anchor_span",
    "is_char_token", "is_word_token", "is_trimmable_token", "token_numeric_value", "Token",
    "TokenView", "wrap_tokens", "enumerate_tokens", "first_token", "last_token",
    "LineTokenizer", "tokenize_block",
    "clamp_value", "is_superscript_adjacent", "jenkins_hash",
    # trie
    "TrieConfig", "BuiltTrie", "set_reverse", "set_case_fold", "TrieNode", "trie_insert_step", "trie_walk_step", "build_trie", "trie_prefix_match", "trie_full_match",
    "strip_trie_match", "strip_leading_if_in", "strip_trailing_comma", "trim_trailing_punct", "COMMA_CHARS",
    "SCRIPT_FAMILY_MAP",
]
