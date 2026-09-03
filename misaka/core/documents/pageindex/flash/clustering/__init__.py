"""Line clustering pipeline.

The initial pass walks spans in document order and groups them into lines using
an in-line continuation test, while also collapsing overstrike duplicates
(artificial-bold rendering where the same glyph is painted twice). The merge
pass inserts lines into a sorted structure keyed by top-desc reading order,
looks up predecessor/successor neighbors, and either merges the new line into a
neighbor or keeps it separate. Neighbor lookup is inclusive of an exact
reading-order key match, so the successor uses ``bisect_left`` and the
predecessor uses ``bisect_right - 1``.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from sortedcontainers import SortedKeyList

from ..model import (
    _UNICODE_WHITESPACE_CLASS,
    Line,
    Span,
    append_span,
    avg_char_width,
    avg_char_width2,
    is_upper_dominant,
    last_span,
    left_edge_key,
    letter_count,
    magnitude_ratio,
    numbering_kind,
    raw_text_of_line,
    reading_order_key,
    same_x_extent,
    same_y_extent,
    text_of_line,
)
from .build import (
    LinesContainer,
    _is_label_stack,
    _set_add,
    _skip_mark_only,
    build_initial_lines,
    cluster_lines,
)
from .merge_rules import (
    TRAILING_DOT_LEADER_RE,
    pick_closer_neighbor,
    should_merge_lines,
    span_continues_line,
    vertical_distance_in_line_heights,
)

# --------------------------------------------------------------------------- #
# Combined helper #
# --------------------------------------------------------------------------- #


__all__ = [
    "TRAILING_DOT_LEADER_RE",
    "build_initial_lines",
    "cluster_lines",
    "pick_closer_neighbor",
    "should_merge_lines",
    "span_continues_line",
    "vertical_distance_in_line_heights",
]
