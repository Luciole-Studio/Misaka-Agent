"""Heading predicates and section-keyword helpers. The full outline tree is assembled in ``outline_assembly``. This module keeps
the lower-level heading checks that decide whether a block is a plausible
outline heading based on numbering, style, geometry, and section-keyword tries.
"""

import re
from collections import defaultdict
from typing import Optional

from ..labels import extract_structural_number
from ..model import Block, block_text, is_caps_heavy, numbering_kind, numbering_text
from ..stats import column_index_of
from ..tokens import (
    TrieConfig,
    build_trie,
    set_case_fold,
    tokenize_block,
    trie_full_match,
)
from .filtering import (
    _AUTHOR_PATTERNS,
    _BULLET_LIST_RE,
    _CAPTION_LABEL_RE,
    _DOT_LEADER_RE,
    _EQUATION_LABEL_RE,
    _NON_HEADING_TYPES,
    _PAREN_FRAGMENT_RE,
    _PSEUDO_CODE_PATTERNS,
    SECTION_KEYWORD_TRIE,
    _heading_signature,
    _looks_like_pseudo_code,
    _matches_section_keywords,
    _numbering_depth,
    _style_key,
    collect_headings,
    filter_by_clique,
    heading_order_key,
    is_heading_candidate,
)
from .tree import (
    _heading_page_num,
    _heading_title,
    assign_levels,
    build_tree,
    extract_top_level_headings,
    validate,
)

__all__ = [
    "SECTION_KEYWORD_TRIE",
    "assign_levels",
    "build_tree",
    "collect_headings",
    "extract_top_level_headings",
    "filter_by_clique",
    "heading_order_key",
    "is_heading_candidate",
    "validate",
]
