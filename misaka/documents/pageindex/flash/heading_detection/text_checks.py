"""Block-text predicates: keyword matches, continuation, content, and number parsing."""

from __future__ import annotations

import math

from ..labels import (
    extract_structural_number,
    trie_matches_all,
)
from ..model import (
    Block,
    Line,
    _trim_unicode_ws,
    block_text,
    center_aligned,
    is_upper_dominant,
    left_aligned,
    right_aligned,
    to_number,
    x_aligned,
    y_overlaps,
)
from ..tokens import (
    Token,
    TokenView,
    first_anchor_span,
    first_token,
    is_word_token,
    last_token,
    strip_trie_match,
    token_numeric_value,
    tokenize_block,
    trie_prefix_match,
)
from .keyword_tables import (
    ABSTRACT_KEYWORDS_SET,
    ABSTRACT_KEYWORDS_TRIE,
    DEAD_DIGIT_RE,
    ENGLISH_WORD_TO_NUMBER,
    EQUATION_KEYWORDS_TRIE,
    FORMULA_CHAR_WEIGHTS,
    NUMBERED_PREFIX_RE,
    REFERENCES_SET,
    REFERENCES_TRIE,
    ROMAN_NUMERAL_MAP,
    _normalize_text_key,
)


def token_text_of_block(block: Block) -> str:
    """Tokenize ``block``, join tokens using their stored spacing flags, trim the result, and memoize it on the block."""
    if block.token_text_cache is not None:
        return block.token_text_cache
    block.token_text_cache = _trim_unicode_ws(tokenize_block(block).to_string())

    return block.token_text_cache


# --------------------------------------------------------------------------- #
# Simple heading and equation predicates.
# --------------------------------------------------------------------------- #


def similar_style(block: Block, other_block: Block) -> bool:
    """Return whether two blocks have very similar bold ratio and font size."""
    return abs(block.bold_frac() - other_block.bold_frac()) < 0.5 and abs(block.avg_font_size() - other_block.avg_font_size()) < 1


def is_heading_continuation(block: Block, other_block: Block, candidate_number: int) -> bool:
    """Return whether ``block`` is the next numbered heading continuation of ``other_block``."""
    if block.type != 0 or block.char_count() >= 500 or not similar_style(other_block, block):
        return False
    text = token_text_of_block(block)
    if block.left_edge() >= other_block.left_edge() and text.startswith("•"):
        return True
    if is_upper_dominant(other_block.char_stats) and is_upper_dominant(block.char_stats) and not left_aligned(other_block, block, 1) and not right_aligned(other_block, block, 1) and center_aligned(other_block, block, 1):
        return False
    heading = NUMBERED_PREFIX_RE.match(text)
    if heading and len(heading.groups()) >= 1:
        matched_number = to_number(heading.group(1))
        return abs(candidate_number - matched_number) == 1
    return False


def matches_abstract(tokens: TokenView) -> bool:
    """Token sequence matches abstract keywords or their normalized text set."""
    if trie_matches_all(ABSTRACT_KEYWORDS_TRIE, tokens):
        return True
    if tokens.length > 10:
        return False
    normalized = ""
    for candidate_item in tokens:
        if is_word_token(candidate_item):
            continue
        if candidate_item.type != 2 or len(normalized) + len(candidate_item.str) > 20:
            return False
        normalized += _normalize_text_key(candidate_item.str.lower())
    return normalized in ABSTRACT_KEYWORDS_SET


def matches_references(tokens: TokenView) -> bool:
    """Token sequence matches references keywords or their whole-text set."""
    secondary_item = trie_prefix_match(REFERENCES_TRIE, tokens)
    if secondary_item is None:
        if tokens.length <= 15:
            normalized = ""
            for candidate_item in tokens:
                if is_word_token(candidate_item):
                    continue
                if candidate_item.type != 2 or len(normalized) + len(candidate_item.str) > 20:
                    return False
                normalized += candidate_item.str.lower()
            return normalized in REFERENCES_SET
        return False
    if secondary_item.length == tokens.length:
        return True
    rest = tokens.slice(secondary_item.length)
    if rest.length == 1:
        first = rest.token_at(0)
        if first is not None and is_word_token(first):
            return True
    return trie_matches_all(REFERENCES_TRIE, rest)


def vertically_close(block: Block | None, other_block: Block) -> bool:
    """a is vertically very close to b."""
    if block is None:
        return False
    candidate_item = block.bottom_edge() - other_block.top_edge() if block.top_edge() > other_block.top_edge() else other_block.bottom_edge() - block.top_edge()
    return candidate_item < 2 * other_block.avg_font_size() or (x_aligned(block, other_block, 1) and candidate_item < 5 * other_block.avg_font_size())


def is_equation_adjacent_line(line: Line | None, block: Block) -> bool:
    """Return whether a line is adjacent to an equation block: it overlaps and follows the block, matches the equation-separator pattern, or consists entirely of equation-keyword tokens after trimming wrapper punctuation."""
    if line is None or line.line_count() != 1:
        return False
    if line.left_edge() < block.right_edge() or not y_overlaps(block, line):
        return False
    if DEAD_DIGIT_RE.match(block_text(line)):
        return True                                  # Equation separator match is enough to accept.
    tokens = tokenize_block(line)
    # Drop single non-digit chars at both edges when token-count is >= 3.
    if (tokens.length >= 3
            and (first := first_token(tokens)) is not None and len(first.str) <= 1
            and first.type != 1
            and (last := last_token(tokens)) is not None and len(last.str) <= 1
            and last.type != 1):
        tokens = tokens.slice(1, tokens.length - 1)
    tokens = strip_trie_match(tokens, EQUATION_KEYWORDS_TRIE)
    yi_match = extract_structural_number(tokens)
    return yi_match is not None and yi_match.length == tokens.length


def has_substantive_content(block: Block, other_block: Block | None, candidate_block: Block | None) -> bool:
    """heuristic "this block has substantive content?" score >= 5."""
    entry_item = 0
    for token in tokenize_block(block):
        anchor = first_anchor_span(token)
        line = token.line()
        size = line.previous_slot
        flag = anchor.top_edge() < line.bottom_edge() + 0.8 * size or anchor.bottom_edge() > line.top_edge() - 0.8 * size
        if token.type == 1:
            entry_item += 2 if flag else 1
            continue
        weight = FORMULA_CHAR_WEIGHTS.get(token.str)
        if weight is not None:
            entry_item += (3 if flag else 1) * weight
            continue
        if token.type == 6:
            entry_item += (3 if flag else 1) * 5
            continue
        if len(token.str) <= 3 and token.primary_slot != 4:
            if flag:
                entry_item += 5 if is_word_token(token) else 1
            continue
        if flag:
            continue
        len_value = (2 if anchor.primary_slot else 1) * len(token.str)
        if token.primary_slot == 4:
            entry_item -= 2 * len_value
        elif token.primary_slot == 2:
            entry_item -= len_value
        elif token.primary_slot == 3:
            entry_item -= 0.5 * len_value
    if entry_item < 0:
        return False
    if entry_item >= 5:
        return True
    return is_equation_adjacent_line(other_block, block) or is_equation_adjacent_line(candidate_block, block)


def is_cover_page(doc, page) -> bool:
    """Return whether ``page`` behaves like a cover page: it is title-marked, appears early, and has light content or no body text."""
    return (
        page.auxiliary_slot
        and page.page_index < max(2, len(doc.primary_slot) / 2)
        and (
            page.primary_slot.secondary_slot < clamp(0.5 * doc.secondary_slot.secondary_slot, 200, 1000)
            or not page.state_slot
        )
    )


def clamp(value: float, lower_bound: float, upper_bound: float) -> float:
    """``max(lo, min(hi, v))``. NaN propagates."""
    measure_item = min(value, upper_bound)
    return max(measure_item, lower_bound)


def token_to_number(tok: Token | None) -> int | float | None:
    """extract numeric value from a token (digit, Roman, or English)."""
    if tok is None:
        return None
    if tok.type == 1:
        token = token_numeric_value(tok)
        if not math.isnan(token) and token > 0:
            return int(token) if token.is_integer() else token
        return None
    return ROMAN_NUMERAL_MAP.get(tok.str) or ENGLISH_WORD_TO_NUMBER.get(tok.str.lower())


def letter_to_ordinal(tok_str: str) -> int | None:
    """'a'/'A' -> 1, 'b' -> 2, ..., 'h' -> 8. None otherwise."""
    if len(tok_str) != 1:
        return None
    # Only the FIRST UTF-16 code unit of the lowercased character counts: a
    # case mapping that expands to several units (U+0130) contributes just its
    # first, and an astral lowercase contributes its high surrogate.
    low = tok_str[0].lower()
    code_unit = ord(low[0])
    if code_unit > 0xFFFF:
        code_unit = 0xD800 + ((code_unit - 0x10000) >> 10)
    value = code_unit - 96
    return value if 1 <= value <= 8 else None
