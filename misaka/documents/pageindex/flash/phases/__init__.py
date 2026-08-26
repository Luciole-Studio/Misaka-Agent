"""Per-page pipeline orchestration. For each page, the extractor builds initial lines, computes page statistics,
detects columns, reclusters lines with column awareness, removes line-number
artifacts, recomputes statistics, and assigns reading order.
"""

import math
from typing import Optional

from sortedcontainers import SortedKeyList

from ..clustering import LinesContainer, build_initial_lines, cluster_lines
from ..columns import ColumnDetectionContext, columns_to_x_bounds, detect_columns
from ..model import (
    Line,
    Rect,
    Span,
    append_span,
    avg_char_width,
    center_aligned,
    info_weight,
    left_aligned,
    right_aligned,
    to_number,
    x_centers_close,
)
from ..stats import PageStats, column_index_of, compute_page_stats
from .line_numbers import (
    LineNumberCluster,
    init_line_number_cluster,
    nearest_cluster,
    strip_line_numbers,
    validate_line_number_cluster,
)
from .page_view import (
    PageView,
    assign_reading_order,
    process_page,
)

__all__ = [
    "LineNumberCluster",
    "PageView",
    "assign_reading_order",
    "init_line_number_cluster",
    "nearest_cluster",
    "process_page",
    "strip_line_numbers",
    "validate_line_number_cluster",
]
