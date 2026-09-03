"""Document-title detection. The scoring formula is the heart of title detection: score is a product of layout, recurrence, label, script, width, numbering, punctuation, alignment, and page-position factors. Each factor is in roughly ``[0.1, 3.0]-- the product can grow to a few
thousand for a strong title candidate. The factors are documented in the
scoring body. The multilingual title-keyword and institution-word sets are stored in
``data/dictionaries.json`` as ``title`` and ``institution_words``.
"""

import json
import math
import unicodedata
from pathlib import Path
from typing import Optional

from ..model import (
    Block,
    Line,
    Rect,
    _trim_unicode_ws,
    alignment_code,
    block_text,
    center_aligned,
    deaccented_text,
    dominant_style_of,
    first_span_of,
    heading_score,
    info_weight,
    is_upper_dominant,
    last_line_of,
    last_span,
    left_aligned,
    letter_count,
    right_aligned,
)
from ..stats import (
    DocStats,
    ScriptHistogram,
    column_index_of,
    dominant_script_family,
    tally_scripts,
)
from ..tokens import (
    BuiltTrie,
    TrieConfig,
    _de_norm,
    build_trie,
    clamp_value,
    enumerate_tokens,
    is_superscript_adjacent,
    is_word_token,
    jenkins_hash,
    set_case_fold,
    tokenize_block,
    trie_prefix_match,
)
from .detect import (
    TitleSearchState,
    detect_title,
)
from .dicts import (
    _DICT_PATH,
    INSTITUTION_WORDS,
    TITLE_LABEL_TRIE,
    _load_dicts,
    _normalize_text_key,
)
from .scoring import (
    TitleCandidate,
    is_cover_like_page,
    is_title_candidate_block,
    score_title_candidate,
)

__all__ = ["INSTITUTION_WORDS", "TITLE_LABEL_TRIE", "TitleCandidate", "TitleSearchState", "detect_title", "is_cover_like_page", "is_title_candidate_block", "score_title_candidate"]
