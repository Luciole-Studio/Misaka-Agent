"""Keyword-labeled section and caption-region detection. This module finds blocks that look like figure/table/chart labels or named
sections, then extends each label forward or backward to claim the associated
body blocks. The resulting regions are used by classification and outline
assembly to avoid treating captions or labeled content as ordinary headings.
"""

from typing import Optional

import regex as regex_module  # Unicode \p{...} property classes.

from ..classification import (
    CHART_KEYWORDS_TRIE,
    FIGURE_KEYWORDS_TRIE,
    TABLE_KEYWORDS_TRIE,
)
from ..model import (
    EMPTY_RECT,
    Block,
    Bounded,
    Line,
    Rect,
    _trim_unicode_ws,
    center_aligned,
    dominant_style_of,
    extend_bottom_to,
    extend_top_to,
    first_span_of,
    heading_score,
    info_weight,
    last_line_of,
    last_span,
    numbering_text,
    reading_order_key,
    rect_union,
)
from ..stats import column_index_of
from ..tokens import (
    BuiltTrie,
    Token,
    TokenView,
    TrieConfig,
    build_trie,
    enumerate_tokens,
    first_token,
    is_word_token,
    last_token,
    set_case_fold,
    strip_leading_if_in,
    tokenize_block,
    trie_prefix_match,
    wrap_tokens,
)
from .caption_regions import (
    CaptionContext,
    CaptionedRegion,
    CaptionEntry,
    build_caption_regions,
    dedupe_caption_entries,
    detect_captions,
    extend_caption_region,
    iter_page_blocks,
)
from .caption_text import (
    PERIOD_CHARS,
    REFERENCE_PHRASE_TRIE,
    STRUCTURAL_NUMBER_RE,
    advance_past_line,
    caption_outranks,
    extract_structural_number,
    format_caption_label,
    is_number_separator,
    is_uppercase_dominant,
    skip_bracketed_word,
    token_case_signal,
    trie_matches_all,
)

__all__ = [
    "PERIOD_CHARS",
    "REFERENCE_PHRASE_TRIE",
    "STRUCTURAL_NUMBER_RE",
    "CaptionContext",
    "CaptionEntry",
    "CaptionedRegion",
    "advance_past_line",
    "build_caption_regions",
    "caption_outranks",
    "dedupe_caption_entries",
    "detect_captions",
    "extend_caption_region",
    "extract_structural_number",
    "format_caption_label",
    "is_number_separator",
    "is_uppercase_dominant",
    "iter_page_blocks",
    "skip_bracketed_word",
    "token_case_signal",
    "trie_matches_all",
]
