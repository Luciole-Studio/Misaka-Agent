"""
Column detection via sweep-line gutter scoring and recursive page splitting.

The detector builds horizontal and vertical sweep events, scores candidate
gutters, assigns column indexes to lines, and returns column rectangles used by
the second line-clustering pass. Direction code 0 scans vertical positions to
find row breaks; direction code 1 scans horizontal positions to find column
breaks.
"""

import math

# Detect TOC dot leaders ("... 5", "....3"). Gutter scoring rejects a split
# candidate when too many dot-leader lines straddle the gap, because a TOC page
# should remain in one reading region.
# The regular expression is end-anchored only; use re.search rather than re.match.
import re as re_module
from typing import Optional

from ..model import (
    _UNICODE_WHITESPACE_CLASS,
    EMPTY_RECT,
    Line,
    Rect,
    _max_nan_propagating,
    _min_nan_propagating,
    info_weight,
    numbering_kind,
    numbering_value,
    rect_union,
    text_of_line,
)
from .gutters import (
    DOT_LEADER_RE,
    ColumnDetectionContext,
    SplitCandidate,
    SweepEvent,
    _score_gutter_gap,
    collect_gutter_candidates,
)
from .splitting import (
    assign_column_index,
    columns_to_x_bounds,
    detect_columns,
    recursive_split,
)

__all__ = [
    "ColumnDetectionContext",
    "SplitCandidate",
    "SweepEvent",
    "assign_column_index",
    "collect_gutter_candidates",
    "columns_to_x_bounds",
    "detect_columns",
    "recursive_split",
]
