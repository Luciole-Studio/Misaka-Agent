"""Block clustering. This module walks page lines in reading order, extends nearby compatible
blocks, starts a new block when no neighbor fits, and then splits simple
"heading + body" two-line blocks where the first line is a standalone section
heading. The clustering pass must return blocks, not raw lines. Reading-order assignment
then uses each block's first line to find the column index; doing that on raw
lines would read an unrelated first-span flag.
"""

import json
from pathlib import Path
from typing import Optional

from sortedcontainers import SortedKeyList

from ..model import (
    EMPTY_RECT,
    Block,
    Line,
    Rect,
    _max_nan_propagating,
    avg_char_width,
    case_signal,
    center_aligned,
    dominant_style_of,
    first_span_of,
    is_upper_dominant,
    last_line_of,
    last_span,
    left_aligned,
    left_edge_key,
    letter_count,
    magnitude_ratio,
    numbering_kind,
    reading_order_key,
    right_aligned,
    style_key,
    x_centers_close,
)
from ..stats import DocStats, PageStats
from ..tokens import TrieConfig, build_trie, set_case_fold, tokenize_block
from .build import (
    _set_add,
    cluster_lines_into_blocks,
    split_heading_body_blocks,
)
from .join_rules import (
    _DICT_PATH,
    _DICTS,
    SECTION_HEADING_TRIE,
    BlockClusterContext,
    should_join_line_to_block,
)

__all__ = ["SECTION_HEADING_TRIE", "BlockClusterContext", "cluster_lines_into_blocks", "should_join_line_to_block", "split_heading_body_blocks"]
