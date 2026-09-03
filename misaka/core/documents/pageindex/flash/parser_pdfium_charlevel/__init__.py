"""PDFium-backed text-item reconstruction via textpage chars and bbox-mapped font handles.

The parser reconstructs content-stream text items from rendered characters while
preserving the geometry needed by downstream line clustering and heading
detection. The merge thresholds operate on glyph advance, font size, text
matrix scale, and spacing introduced by char spacing, text-position operators,
and ``TJ`` adjustments.

Per page, the reconstruction uses rendered character origins, glyph widths,
font bbox containment, effective font size, text-item merging, baseline-anchored
character boxes, and the minimum font size derived in each emitted chunk. Those
calibrations keep small caps, math glyphs, ligatures, Type 3 fonts, rotated
text, and vertical writing stable enough for layout statistics.
"""

import bisect
import ctypes
import difflib
import json
import math
import re
import unicodedata
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Union

# Raw PDF object access (ToUnicode CMaps, content streams, font dicts, /WMode)
# that PDFium does not expose, read via PyPDF2 -- already a project dependency and
# permissively licensed. A thin adapter exposes the small raw-object API the
# helpers below need, so their calibrated logic stays unchanged.
import PyPDF2 as _pypdf2  # declared dependency (also imported by pageindex.utils/client)
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PyPDF2.generic import (
    ArrayObject as PdfArray,
)
from PyPDF2.generic import (
    BooleanObject as PdfBoolean,
)
from PyPDF2.generic import (
    DictionaryObject as PdfDictionary,
)
from PyPDF2.generic import (
    FloatObject as PdfFloat,
)
from PyPDF2.generic import (
    IndirectObject as PdfIndirectRef,
)
from PyPDF2.generic import (
    NameObject as PdfName,
)
from PyPDF2.generic import (
    NumberObject as PdfNumber,
)

from ..model import Rect, Span
from .char_extract import (
    _accumulate_type3_extents,
    _apply_type3_sizes,
    _extract_raw_chars,
    _finalize_chars,
    _inherited_box,
    _off_page,
    _page_view_rect,
    _type3_size_by_font,
)
from .cmap_parse import (
    _NUM_BINARY_RE,
    _NUM_DECIMAL_RE,
    _NUM_HEX_RE,
    _NUM_INFINITY_RE,
    _NUM_OCTAL_RE,
    _WHITESPACE_STRIP,
    _cmap_str_to_int,
    _compute_skew,
    _ieee_div,
    _parse_int,
    _parse_tounicode_cmap,
    _to_number,
    _utf16be_units_to_str,
)
from .code_walk import (
    _char_category,
    _page_show_codes,
    _resource_dict_xrefs,
    _walk_codes,
)
from .content_stream import (
    _FLUSH_OPS,
    _OP_LEX_PREFIX,
    _OP_OPERAND_COUNTS,
    _SHOW_OPS,
    _assign_show_tz,
    _assign_vertical_tags,
    _page_vertical_resource_names,
    _tokenize_show_operators,
)
from .font_unicode import (
    _TYPE1_SPECIAL_BYTES,
    _TYPE1_WHITESPACE_BYTES,
    _font_unicode_map,
    _simple_font_to_unicode,
    _type1_builtin_encoding,
)
from .geometry import (
    _IDENT_MTX,
    _build_obj_index,
    _char_render_fs,
    _collect_text_objs,
    _compose_mtx,
    _find_obj_for_char,
    _obj_rotation,
    _xf_point,
)
from .glyph_tables import (
    _GLYPHLIST_PATH,
    _cached_encodings,
    _cached_glyphs,
    _from_char_code,
    _get_unicode_for_glyph,
    _load_glyph_tables,
)
from .merge import _merge_text_items
from .pdf_objects import (
    _PDF_DELIMITER_BYTES,
    _PDF_STRING_ESCAPE_BYTES,
    _PDF_WHITESPACE_BYTES,
    _decode_pdf_name,
    _pdf_obj_str,
    _pdf_tok,
    _pdf_typed,
    _PdfDoc,
    _PdfPage,
)
from .pipeline import (
    _page_pass1,
    _page_pass2,
    _page_spans,
    parse_charlevel,
    parse_charlevel_meta,
)
from .remerge import (
    _close_oblique,
    _close_vert_span,
    _grow_rot_span,
    _grow_vert_span,
    _merge_oblique_one,
    _merge_rotated_one,
    _merge_vertical_one,
    _new_oblique_span,
    _oblique_space,
    _remerge_oblique,
    _remerge_rotated,
    _remerge_vertical,
    _start_rot_span,
    _start_vert_span,
)
from .text_normalize import (
    _BIDI_ARABIC_TYPES,
    _BIDI_BASE_TYPES,
    _DROP_CHARS,
    _NORMALIZED_UNICODES,
    _WHITESPACE_CODEPOINTS,
    NEGATIVE_SPACE_FACTOR,
    NON_SPACE_GAP_FACTOR,
    SPACE_IN_FLOW_MAX_FACTOR,
    SPACE_IN_FLOW_MIN_FACTOR,
    TRACKING_SPACE_FACTOR,
    _apply_bidi_reordering,
    _is_invisible_format_mark,
    _is_whitespace,
    _is_zero_width_diacritic,
    _normalize_unicodes,
    _read_end,
    _read_gap,
    _reverse_if_rtl,
    _rtl_sign,
)
from .unicode_apply import (
    _apply_font_unicode,
    _synthesize_dropped_glyphs,
)

__all__ = ["parse_charlevel", "parse_charlevel_meta"]
