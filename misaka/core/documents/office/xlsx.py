"""A workbook read as a workbook: coordinates, formulas, styles, tables, pivots, charts.

Ported from FrontierAgent's ``plugins/tools/_reader_xlsx.py`` (audit D29-D45), which is the
only reader either project has that treats a spreadsheet as something other than a wall of
values. What it does that a naive dump does not: real row and column coordinates, so a
citation can name a cell; data islands, so two unrelated tables on one sheet are two
regions; homogeneous formula fills grouped as one R1C1 entry instead of five hundred lines;
merged regions, number formats, styles, conditional formats, hyperlinks, comments and
dropdowns aggregated once per sheet rather than annotated per cell; Excel Tables and csv
through one relational representation; charts parsed out of the package, because openpyxl
discards them on load.

Conventions, borrowed whole: a backtick span is parser-added and not file content, a
``▸`` line opens a category inside the sheet's ```meta`` fence, and only deviations from
the default are recorded -- a sheet where nothing is bold has no ``bold:`` line.

**The one deliberate difference from FrontierAgent.** Its ``_x_grid`` emits a markdown pipe
table. This emits tab-separated rows, because misaka pins citations to pages and
``index.normalize_for_quote_match`` folds every whitespace character away while keeping
``|``: under a pipe table a quotation spanning two cells ("year amount") folds to
"yearamount" while the page folds to "|year|amount|", so it can never be found; under tabs
the page folds to "yearamount" and it verifies. The citation ledger is what this corpus is
for, so the ledger wins over the prettier table.
"""
from __future__ import annotations

import csv as _csv
import datetime as _dt
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from misaka.core.documents.office import soffice

# Data-island split: this many consecutive blank rows or columns end a block. Borrowed from
# FrontierAgent(_reader_xlsx.py:22 _GAP), with its own recorded risk -- a title separated
# from its table by more than two blank rows is split off as a region of its own.
GAP = 2

# An Excel Table of at most this many data rows is emitted whole; past it the reader emits
# a column schema plus PREVIEW rows and points at cell_range for the rest.
TABLE_FULL = 20
PREVIEW = 20

# A dense region larger than this is named rather than rendered. FrontierAgent has no such
# bound because it runs in a sandbox with its own memory cap; here the rendering goes into
# a model's context, and a million-cell island would be the whole of it.
MAX_GRID_CELLS = 200_000

_NS_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# A cell reference in a formula: optional $ anchors, 1-3 column letters, up to 7 digits, not
# followed by a word character or "(" -- so ``LOG10(`` is a function, not column LOG row 10.
_REF_RE = re.compile(r"(?<![A-Za-z0-9_$])(\$?)([A-Za-z]{1,3})(\$?)([0-9]{1,7})(?![\w(])")

_CHART_PART = re.compile(r"xl/charts/chart\d+\.xml")

_HEAD = (
    "<!-- xlsx readout. Sheet metadata is gathered once per sheet in a ```meta fence "
    "(parser-added, not file content); each category opens with a ▸ line. A backtick span "
    "marks a per-cell note. The grid's first row is column letters and its first column is "
    "row numbers, both real coordinates; cells are separated by tabs. Homogeneous formula "
    "fills are grouped as R1C1. Dates are ISO. `uncached` means the file carries a formula "
    "whose cached result is empty. Re-read part of a sheet with cell_range=\"<sheet>!A1:..\". -->"
)


def _col(n):
    from openpyxl.utils import get_column_letter
    return get_column_letter(n)


def _esc(value):
    """One cell's text, safe to put in a tab-separated row.

    A tab or a newline inside a cell would invent a column or a row, and a backtick would
    collide with the parser-added marker convention. Nothing else is escaped: every added
    character is one a quotation would have to reproduce to verify.
    """
    return str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ").replace("`", "\\`")


def _ref(r1, c1, r2, c2):
    a, b = f"{_col(c1)}{r1}", f"{_col(c2)}{r2}"
    return a if a == b else f"{a}:{b}"


def _render(cell):
    """A cell's display text -- what the sheet shows, not what it stores.

    Dates are normalised to ISO; percent, scientific, thousands, currency and
    negative-in-parentheses are rendered from ``number_format``. Anything this cannot cover
    degrades to the stored value, whose format is still visible on the sheet's
    ``number-format`` line. Borrowed from FrontierAgent(_reader_xlsx.py:48 _x_render).
    """
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    fmt = cell.number_format or "General"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or fmt in ("General", "@"):
        return _esc(value)
    sections = fmt.split(";")
    active = sections[1] if (value < 0 and len(sections) > 1) else sections[0]
    parenthesised = value < 0 and "(" in active
    magnitude = abs(value) if parenthesised else value
    try:
        if "%" in active:
            match = re.search(r"0\.(0+)%", active)
            shown = f"{magnitude * 100:.{len(match.group(1)) if match else 0}f}%"
            return f"({shown})" if parenthesised else shown
        if re.search(r"[0#]\.?0*E\+?0+", active, re.IGNORECASE):
            match = re.search(r"\.(0+)E", active, re.IGNORECASE)
            return f"{value:.{len(match.group(1)) if match else 2}E}"
        if re.search(r"[0#],(?![0#])", active):
            # A trailing comma is thousands *scaling* (#,##0,,"M"): rendering it would state
            # a number the sheet does not show. Give the stored value instead.
            return _esc(value)
        if "#,##" in active:
            match = re.search(r"0\.(0+)", active)
            shown = f"{magnitude:,.{len(match.group(1)) if match else 0}f}"
            if "$" in active:
                shown = "$" + shown
            return f"({shown})" if parenthesised else shown
    except (ValueError, TypeError, ArithmeticError):
        pass
    return _esc(value)


def _compress(coords):
    """A coordinate set as ``A1:B3,C5`` -- greedy rectangles, extend right then down."""
    remaining = set(coords)
    out = []
    while remaining:
        r, c = min(remaining)
        width = 1
        while (r, c + width) in remaining:
            width += 1
        height = 1
        while all((r + height, cc) in remaining for cc in range(c, c + width)):
            height += 1
        for rr in range(r, r + height):
            for cc in range(c, c + width):
                remaining.discard((rr, cc))
        out.append(_ref(r, c, r + height - 1, c + width - 1))
    return ",".join(out)


def _islands(coords):
    """Non-empty coordinates as a list of bounding boxes, split where the sheet is blank.

    Three passes -- rows, columns, rows -- because a gap that separates two tables
    side by side is a column gap and one that separates them vertically is a row gap, and a
    single pass finds only the first kind.
    """
    if not coords:
        return []

    def split(group, axis):
        values = sorted({t[axis] for t in group})
        runs, current = [], [values[0]]
        for value in values[1:]:
            if value - current[-1] > GAP:
                runs.append(set(current))
                current = [value]
            else:
                current.append(value)
        runs.append(set(current))
        return [{t for t in group if t[axis] in run} for run in runs]

    blocks = [set(coords)]
    for axis in (0, 1, 0):
        blocks = [part for block in blocks for part in split(block, axis)]
    boxes = []
    for block in blocks:
        rows = [t[0] for t in block]
        cols = [t[1] for t in block]
        boxes.append((min(rows), min(cols), max(rows), max(cols)))
    return sorted(boxes)


def _stored_cells(sheet):
    """Only serialized cells, in row order; iter_rows densifies sparse worksheets."""
    return [sheet._cells[key] for key in sorted(sheet._cells)]


def _has_formula(cell):
    return cell.data_type == "f" or cell.value.__class__.__name__ == "ArrayFormula"


def _missing_cache(formula_cell, value_cell):
    # t="str" with an empty <v> is a calculated empty string, not a missing result.
    return (_has_formula(formula_cell) and value_cell.value is None
            and value_cell.data_type not in ("s", "str", "inlineStr"))


def _grid(values_sheet, formula_sheet, bbox, mark_uncached):
    """One region as tab-separated rows: column letters across the top, row numbers down
    the side. A row with nothing in it is dropped, but the numbering stays real -- the
    coordinates are what a citation can name."""
    r1, c1, r2, c2 = bbox
    if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_GRID_CELLS:
        return [(f"`{_ref(r1, c1, r2, c2)} is too large to render here "
                 f"({(r2 - r1 + 1) * (c2 - c1 + 1)} cells); read part of it with "
                 f'cell_range="{formula_sheet.title}!{_ref(r1, c1, r2, c2)}"`')]
    lines = ["\t" + "\t".join(_col(c) for c in range(c1, c2 + 1))]
    for r in range(r1, r2 + 1):
        cells, seen = [], False
        for c in range(c1, c2 + 1):
            cell = values_sheet.cell(row=r, column=c)
            if mark_uncached and _missing_cache(formula_sheet.cell(row=r, column=c), cell):
                cells.append("`uncached`")
                seen = True
                continue
            shown = _render(cell)
            seen = seen or bool(shown)
            cells.append(shown)
        if seen:
            lines.append(f"{r}\t" + "\t".join(cells))
    # Every cell rendered to nothing. Not hypothetical: after a recalculation a report
    # template full of ``=IF(A1>0,A1,"")`` has a cached value of ``""`` in every unused row
    # -- non-empty to the coordinate scan, empty to the renderer. A label and column letters
    # with no rows under them assert a region is there and say nothing about it.
    if len(lines) == 1:
        return []
    return lines


def _r1c1(formula, anchor_row, anchor_col):
    """An A1 formula relative to its anchor. A fill converts to one identical string, which
    is what lets five hundred cells collapse to a single range entry."""
    from openpyxl.utils import column_index_from_string

    def convert(match):
        col_abs, letters, row_abs, digits = match.groups()
        col, row = column_index_from_string(letters.upper()), int(digits)
        row_part = f"R{row}" if row_abs else ("R" if row == anchor_row else f"R[{row - anchor_row}]")
        col_part = f"C{col}" if col_abs else ("C" if col == anchor_col else f"C[{col - anchor_col}]")
        return row_part + col_part

    from openpyxl.formula import Tokenizer
    from openpyxl.formula.tokenizer import TokenizerError
    try:
        tokens = Tokenizer(formula)
    except TokenizerError:
        return formula  # Unsupported syntax remains verbatim, never guessed.
    for token in tokens.items:
        if token.type != "OPERAND" or token.subtype != "RANGE":
            continue
        prefix, separator, reference = token.value.rpartition("!")
        endpoints = reference.split(":")
        if len(endpoints) > 2 or not all(_REF_RE.fullmatch(part) for part in endpoints):
            continue  # Names, structured references and whole-column ranges stay literal.
        matches = [_REF_RE.fullmatch(part) for part in endpoints]
        if any(not 1 <= int(match[4]) <= 1048576
               or column_index_from_string(match[2].upper()) > 16384 for match in matches):
            continue  # e.g. ZZZ1 can be a defined name, not an Excel cell reference.
        token.value = prefix + separator + _REF_RE.sub(convert, reference)
    return tokens.render()


def _formula_lines(formula_sheet):
    cells = {}
    for cell in _stored_cells(formula_sheet):
        value = cell.value
        if cell.data_type == "f" and isinstance(value, str):
            cells[(cell.row, cell.column)] = value
        elif value.__class__.__name__ == "ArrayFormula":
            text = getattr(value, "text", "") or ""
            cells[(cell.row, cell.column)] = text if text.startswith("=") else "=" + text
    if not cells:
        return ("formulas", [])
    done, items = set(), []
    # Column-major, so a vertical fill -- much the commoner shape -- is found first.
    for (r, c) in sorted(cells, key=lambda t: (t[1], t[0])):
        if (r, c) in done:
            continue
        base = _r1c1(cells[(r, c)], r, c)
        run = 1
        while (r + run, c) in cells and (r + run, c) not in done \
                and _r1c1(cells[(r + run, c)], r + run, c) == base:
            run += 1
        if run > 1:
            done.update((r + i, c) for i in range(run))
            items.append(f"{_col(c)}{r}:{_col(c)}{r + run - 1} {base}")
            continue
        run = 1
        while (r, c + run) in cells and (r, c + run) not in done \
                and _r1c1(cells[(r, c + run)], r, c + run) == base:
            run += 1
        done.update((r, c + i) for i in range(max(run, 1)))
        if run > 1:
            items.append(f"{_col(c)}{r}:{_col(c + run - 1)}{r} {base}")
        else:
            items.append(f"{_col(c)}{r} {cells[(r, c)]}")   # isolated: keep its A1 text
    return ("formulas", items)


def _numfmt_lines(formula_sheet, coords):
    formats = {}
    for (r, c) in coords:
        fmt = formula_sheet.cell(row=r, column=c).number_format or "General"
        if fmt not in ("General", "@"):
            formats.setdefault(fmt, set()).add((r, c))
    return ("number-format", [f"{fmt} {_compress(cells)}"
                              for fmt, cells in sorted(formats.items())])


def _merged_lines(formula_sheet):
    """An xlsx merge stores the value in the top-left cell, so the region alone does not
    say where its text is."""
    try:
        ranges = sorted(str(r) for r in formula_sheet.merged_cells.ranges)
    except (AttributeError, TypeError):
        return ("merged", [])
    return ("merged", [f"{r} (value at {r.split(':')[0]})" for r in ranges])


def _color(colour):
    try:
        if colour is not None and getattr(colour, "type", None) == "rgb" and colour.rgb:
            text = str(colour.rgb)
            if len(text) == 8:
                text = text[2:]
            if text != "000000":
                return "#" + text
    except (AttributeError, TypeError, ValueError):
        pass
    return ""


def _style_lines(formula_sheet, coords):
    """One line per category, ranges compressed. Black text and white fill are the default
    and say nothing, so they are not recorded."""
    background, font_colour = {}, {}
    bold, italic, underline = set(), set(), set()
    for (r, c) in coords:
        cell = formula_sheet.cell(row=r, column=c)
        try:
            fill = cell.fill
            if fill is not None and fill.patternType == "solid":
                colour = _color(fill.fgColor)
                if colour and colour != "#FFFFFF":
                    background.setdefault(colour, set()).add((r, c))
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            colour = _color(cell.font.color)
            if colour:
                font_colour.setdefault(colour, set()).add((r, c))
            if cell.font.bold:
                bold.add((r, c))
            if cell.font.italic:
                italic.add((r, c))
            if cell.font.underline and cell.font.underline != "none":
                underline.add((r, c))
        except (AttributeError, TypeError, ValueError):
            pass
    items = []
    for label, table in (("bg-color", background), ("font-color", font_colour)):
        if table:
            items.append(f"{label}: " + " | ".join(
                f"{colour} {_compress(cells)}" for colour, cells in sorted(table.items())))
    for label, cells in (("bold", bold), ("italic", italic), ("underline", underline)):
        if cells:
            items.append(f"{label}: {_compress(cells)}")
    return ("styles", items)


def _cond_lines(formula_sheet):
    items = []
    try:
        formats = list(formula_sheet.conditional_formatting)
    except (AttributeError, TypeError):
        return ("cond", items)
    for entry in formats:
        where = str(entry.sqref).replace(" ", ",")
        for rule in entry.rules:
            kind = rule.type
            if kind == "colorScale":
                try:
                    # ``val`` is meaningless for min/max, so those show the type name alone.
                    stops = [v.type if v.type in ("min", "max") else f"{v.type}:{v.val}"
                             for v in rule.colorScale.cfvo]
                    items.append(f"{where} color-scale({'→'.join(stops)})")
                except (AttributeError, TypeError):
                    items.append(f"{where} color-scale")
            elif kind == "dataBar":
                items.append(f"{where} data-bar")
            elif kind == "cellIs":
                items.append(f"{where} cellIs {rule.operator or '?'} "
                             f"{rule.formula[0] if rule.formula else ''}".rstrip())
            else:
                items.append(f"{where} {kind}")
    return ("cond", items)


def _extra_lines(formula_sheet):
    """The handles whose meaning is not in the value layer: where a cell links to, what a
    reviewer wrote about it, what a column is allowed to contain."""
    items = []
    for cell in _stored_cells(formula_sheet):
        try:
            if cell.hyperlink is not None and getattr(cell.hyperlink, "target", None):
                items.append(f"link {cell.coordinate}→{cell.hyperlink.target}")
            if cell.comment is not None and cell.comment.text:
                text = cell.comment.text.strip().replace("\n", " ")[:60]
                items.append(f'comment {cell.coordinate}:"{text}"')
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        for validation in formula_sheet.data_validations.dataValidation:
            if validation.type == "list" and validation.formula1:
                items.append(f"dropdown {validation.sqref} {validation.formula1}")
    except (AttributeError, TypeError):
        pass
    return ("extras", items)


def _to_num(value):
    """``float`` or ``None`` for the column schema's numeric verdict. ``bool`` is excluded
    -- True is not 1 in a data column -- and a numeric string counts, which is how a csv's
    columns are typed at all."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _table_md(header, rows, name=None, ref=None, preview=PREVIEW, delimiter="\t"):
    """A relational table: a ```meta`` fence, then the rows.

    Shared by Excel Tables and csv. ``preview=None`` emits every row -- a csv has no
    ``cell_range`` to fetch more with, so its size is the pager's problem rather than this
    function's.

    ``delimiter`` is a tab for a workbook, whose source has no textual form, and the file's
    own separator for a csv, whose source is text a reader will quote as it stands. A csv
    rendered with tabs cannot verify the quotation ``North,800`` that its own first line
    shows, which is the thing this corpus exists to be able to do. csv rows therefore go
    back out through ``csv.writer`` on the source dialect, so a row round-trips exactly --
    quoting and all.
    """
    columns = len(header)
    total = len(rows)
    large = total > TABLE_FULL
    truncated = preview is not None and large
    if name and ref:
        title = f'▸ table "{name}": {ref} ({total} data rows)'
    elif name:
        title = f'▸ table "{name}" ({total} data rows)'
    else:
        title = f"▸ table ({total} data rows)"
    lines = ["```meta", title]
    if large:
        lines.append("▸ columns")
        for index in range(columns):
            raw = [rows[r][index][0] for r in range(total)
                   if index < len(rows[r]) and rows[r][index][0] not in (None, "")]
            numbers = [n for n in (_to_num(v) for v in raw) if n is not None]
            if raw and len(numbers) >= len(raw) * 0.9:
                lines.append(f"    {header[index]}: num, "
                             f"min={min(numbers):g}, max={max(numbers):g}")
            else:
                unique = sorted({str(v) for v in raw})
                if len(unique) > 20:
                    lines.append(f"    {header[index]}: str, {len(unique)} uniq, "
                                 f"first 20: {unique[:20]}")
                else:
                    lines.append(f"    {header[index]}: str, {len(unique)} uniq: {unique}")
        if truncated:
            lines.append(f"▸ preview: first {preview} rows")
    lines.append("```")
    end = min(preview, total) if truncated else total
    body = [[_esc(h) for h in header]]
    body += [[rows[r][i][1] if i < len(rows[r]) else "" for i in range(columns)]
             for r in range(end)]
    if delimiter == "\t":
        lines += ["\t".join(row) for row in body]
    else:
        buffer = io.StringIO(newline="")
        _csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(body)
        lines += buffer.getvalue().splitlines()
    return lines


def _table_cell(values_sheet, formula_sheet, r, c, mark_uncached):
    """``(raw value, display text)`` for one table cell, uncached formulas marked."""
    cell = values_sheet.cell(row=r, column=c)
    if mark_uncached and _missing_cache(formula_sheet.cell(row=r, column=c), cell):
        return (None, "`uncached`")
    return (cell.value, _render(cell))


def _region_mask(sheet, r1, c1, r2, c2):
    if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_GRID_CELLS:
        return {(r, c) for r, c in sheet._cells if r1 <= r <= r2 and c1 <= c <= c2}
    return {(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)}


def _table_regions(values_sheet, formula_sheet, mark_uncached=False):
    """Excel Tables as ``[(anchor, lines)]`` plus the coordinates they own, so the island
    pass does not render the same cells a second time as a bare grid.

    ``mark_uncached`` has to reach here as well as the grid. FrontierAgent's equivalent does
    not take it, so in that reader a formula cell with an empty cache renders as ``uncached``
    inside a data region and as *nothing at all* inside an Excel Table -- while the document
    head promises "empty formula caches marked uncached". A blank cell reads as "this
    formula is empty", which is the one thing it must not read as."""
    from openpyxl.utils import range_boundaries
    regions, mask = [], set()
    for name, table in (getattr(formula_sheet, "tables", {}) or {}).items():
        ref = table.ref if hasattr(table, "ref") else str(table)
        try:
            c1, r1, c2, r2 = range_boundaries(ref)
        except (ValueError, TypeError):
            continue
        mask.update(_region_mask(formula_sheet, r1, c1, r2, c2))
        if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_GRID_CELLS:
            regions.append(((r1, c1), [f'`table "{name}": {ref} is too large; read a smaller cell_range`']))
            continue
        header = [_render(values_sheet.cell(row=r1, column=c)) for c in range(c1, c2 + 1)]
        rows = [[_table_cell(values_sheet, formula_sheet, r, c, mark_uncached)
                 for c in range(c1, c2 + 1)]
                for r in range(r1 + 1, r2 + 1)]
        regions.append(((r1, c1), _table_md(header, rows, name=name, ref=ref)))
    return regions, mask


def _pivot_regions(formula_sheet):
    """A pivot contributes its definition, never its values: the values are a view of a
    source range that is already in the workbook, and expanding them would state the same
    numbers twice. ``cell_range`` fetches the rendered result when it is wanted."""
    from openpyxl.utils import range_boundaries
    regions, mask = [], set()
    for pivot in getattr(formula_sheet, "_pivots", []) or []:
        where, anchor = "?", (1, 1)
        try:
            where = pivot.location.ref
            c1, r1, c2, r2 = range_boundaries(where)
            anchor = (r1, c1)
            mask.update(_region_mask(formula_sheet, r1, c1, r2, c2))
        except (AttributeError, ValueError, TypeError):
            pass
        source = "?"
        try:
            sheet_source = pivot.cache.cacheSource.worksheetSource
            source = f"{sheet_source.sheet}!{sheet_source.ref}"
        except (AttributeError, TypeError):
            pass
        rows, cols, values = [], [], []
        try:
            names = [f.name for f in pivot.cache.cacheFields]
            rows = [names[f.x] for f in (pivot.rowFields or []) if 0 <= f.x < len(names)]
            cols = [names[f.x] for f in (pivot.colFields or []) if 0 <= f.x < len(names)]
            values = [d.name for d in (pivot.dataFields or [])]
        except (AttributeError, IndexError, TypeError):
            pass
        regions.append((anchor, (f"at {where} source={source} rows={rows} cols={cols} "
                                 f"values={values} — values not expanded; "
                                 f're-read with cell_range="{formula_sheet.title}!{where}"')))
    return regions, mask


def _chart_lines(path):
    """Charts, per sheet, parsed straight out of the package.

    openpyxl discards charts when it loads a workbook, so a chart that is never read here
    cannot be read at all -- and a chart is often the only place a sheet says what its
    numbers mean.
    """
    found = {}
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return found
    with archive:
        for name in archive.namelist():
            if not _CHART_PART.fullmatch(name):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except (ET.ParseError, OSError, KeyError):
                continue
            plot = root.find(f".//{_NS_C}plotArea")
            if plot is None:
                continue
            # Deduplicated in order: a combo chart with two bar plots is "bar", not
            # "bar+bar".
            kinds = list(dict.fromkeys(
                e.tag[len(_NS_C):].replace("Chart", "") for e in plot
                if e.tag.startswith(_NS_C) and e.tag.endswith("Chart")))
            title_element = root.find(f".//{_NS_C}title")
            # Joined with a space and requoted: a title split across formatting runs
            # otherwise runs its words together, and one carrying a quote closes the
            # attribute early.
            title = (" ".join(t.text or "" for t in title_element.iter(f"{_NS_A}t"))
                     if title_element is not None else "")
            title = " ".join(title.split()).replace('"', "'")
            series, categories, trends, category_key = [], "", [], "cats"
            for ser in plot.iter(f"{_NS_C}ser"):
                values = ser.find(f"{_NS_C}val")
                if values is None:
                    values = ser.find(f"{_NS_C}yVal")     # scatter / bubble
                if values is not None:
                    formula = values.find(f".//{_NS_C}f")
                    if formula is not None and formula.text:
                        series.append(formula.text)
                axis = ser.find(f"{_NS_C}cat")
                if axis is None:
                    axis = ser.find(f"{_NS_C}xVal")       # scatter: x values, not categories
                    if axis is not None:
                        category_key = "x"
                if axis is not None and not categories:
                    formula = axis.find(f".//{_NS_C}f")
                    if formula is not None and formula.text:
                        categories = formula.text
                for trend in ser.iter(f"{_NS_C}trendline"):
                    kind = trend.find(f"{_NS_C}trendlineType")
                    trends.append(kind.get("val") if kind is not None else "linear")
            sheet = ""
            for reference in series + ([categories] if categories else []):
                match = re.match(r"'?([^'!]+)'?!", reference)
                if match:
                    sheet = match.group(1)
                    break
            bit = "+".join(kinds) or "?"
            if title:
                bit += f' title="{title}"'
            if series:
                bit += f" series={','.join(series)}"
            if categories:
                bit += f" {category_key}={categories}"
            if trends:
                bit += f" trendline={','.join(trends)}"
            found.setdefault(sheet, []).append(bit)
    return found


def _uncached_anywhere(formula_book, value_book, scope=None):
    """Whether any visible sheet holds a formula with no cached result.

    A workbook this product's own writer produced is exactly this case: openpyxl cannot
    write a cached value, so every formula it writes reads back empty until something
    calculates it.

    ``scope`` is ``(sheet name, bbox)`` and is what keeps a ``cell_range`` point query
    cheap. Without it, asking for two cells walks every cell of every sheet twice --
    FrontierAgent does exactly that, and risks a 90-second LibreOffice subprocess for it.
    """
    sheets = ([formula_book[scope[0]]] if scope is not None else formula_book.worksheets)
    for sheet in sheets:
        values = value_book[sheet.title]
        for cell in _stored_cells(sheet):
            if scope is not None:
                r1, c1, r2, c2 = scope[1]
                if not (r1 <= cell.row <= r2 and c1 <= cell.column <= c2):
                    continue
            if _missing_cache(cell, values.cell(row=cell.row, column=cell.column)):
                return True
    return False


def _load(path):
    """``(formula workbook, value workbook)``.

    Two loads, because openpyxl gives either the formula or its cached result and the
    reader needs both. Keep pivot cache definitions but skip large cache record lists.
    The proxy is local to each reader, not a global openpyxl monkeypatch; parser failures
    propagate rather than silently reporting missing pivot data as a successful read.
    """
    from collections import defaultdict

    from openpyxl.reader.excel import ExcelReader

    class MetadataParser:
        # The upstream optimization used to replace WorkbookParser.pivot_caches
        # globally. That leaked into later writes and lost actual pivot caches.
        # Delegate everything except cache loading, on this reader instance only.
        def __init__(self, parser):
            self.parser = parser
            from openpyxl.packaging.relationship import get_rel
            from openpyxl.pivot.cache import CacheDefinition
            self.pivot_caches = defaultdict(lambda: None)
            # Keep cache *definitions* (source and field names); only large cache
            # records are unnecessary. Follow WorkbookParser's get_rel lookup but
            # never fetch RecordList, and never change another reader or writer.
            for cache in parser.caches:
                self.pivot_caches[cache.cacheId] = get_rel(
                    parser.archive, parser.rels, id=cache.id, cls=CacheDefinition)

        def __getattr__(self, name):
            return getattr(self.parser, name)

    class MetadataReader(ExcelReader):
        def read_workbook(self):
            super().read_workbook()
            self.parser = MetadataParser(self.parser)

    def load(data_only):
        reader = MetadataReader(path, data_only=data_only)
        try:
            reader.read()
            return reader.wb
        finally:
            reader.archive.close()

    return load(False), load(True)


def _range(cell_range, value_book):
    """``(sheet name, boundaries)`` for a ``Sheet1!A3:D15`` argument.

    Both refusals name what is actually there: a model that mistyped a sheet name cannot
    fix it from "sheet not found".

    A whole-column or whole-row range ("A:C", "3:5") is a legitimate thing to ask for and
    openpyxl half-accepts it -- ``range_boundaries`` returns ``None`` for the bounds the
    argument did not give, which then reaches the grid and dies on ``None + 1``. That was a
    bare ``TypeError`` out of a tool call rather than anything a model could act on
    (FrontierAgent has the same crash; found by reading its source, reproduced here before
    this was written). The missing bounds are filled from the sheet's own extent instead,
    which is what the argument meant.
    """
    from openpyxl.utils import range_boundaries
    if "!" in cell_range:
        name, rest = cell_range.rsplit("!", 1)
        if name.startswith("'") and name.endswith("'"):
            name = name[1:-1].replace("''", "'")
    else:
        name, rest = value_book.sheetnames[0], cell_range
    if name not in value_book.sheetnames:
        raise ValueError(f"No sheet named {name!r} in this workbook. It has: "
                         + ", ".join(value_book.sheetnames))
    try:
        c1, r1, c2, r2 = range_boundaries(rest)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{cell_range!r} is not an A1-style range "
                         f"like \"Sheet1!A3:D15\" ({error})") from error
    sheet = value_book[name]
    if not hasattr(sheet, "cell"):
        # A chartsheet is in ``sheetnames`` but is not a grid: ``.cell`` does not exist on
        # it, and reaching the renderer with one raised a bare AttributeError out of a tool
        # call. This is the second of the two cell_range crash paths.
        raise ValueError(f"{name!r} is a chart sheet, not a grid, so it has no cell range. "
                         "Its chart is described under ▸ charts in the full reading.")
    r1 = sheet.min_row if r1 is None else r1
    r2 = sheet.max_row if r2 is None else r2
    c1 = sheet.min_column if c1 is None else c1
    c2 = sheet.max_column if c2 is None else c2
    if not (1 <= r1 <= r2 <= 1048576 and 1 <= c1 <= c2 <= 16384):
        raise ValueError(f"{cell_range!r} is outside Excel bounds or has reversed endpoints")
    return name, (r1, c1, r2, c2)


def _sheet_markdown(values_sheet, formula_sheet, coords, charts, mark_uncached):
    """One sheet's body: data regions in reading order, then its ```meta`` fence."""
    out = []
    tables, table_mask = _table_regions(values_sheet, formula_sheet, mark_uncached)
    pivots, pivot_mask = _pivot_regions(formula_sheet)
    islands = _islands(coords - table_mask - pivot_mask)
    regions = [((b[0], b[1]), "region", b) for b in islands]
    regions += [(anchor, "table", lines) for anchor, lines in tables]
    regions += [(anchor, "pivot", text) for anchor, text in pivots]
    regions.sort(key=lambda entry: entry[0])
    numbered, pivot_items = 0, []
    for _anchor, kind, payload in regions:
        if kind == "region":
            numbered += 1
            label = f"data region {numbered}" if len(islands) > 1 else "data region"
            out += ["", f"`{label}: {_ref(*payload)}`"]
            out += _grid(values_sheet, formula_sheet, payload, mark_uncached)
        elif kind == "table":
            out += ["", *payload]
        else:
            pivot_items.append(payload)
    sections = [_formula_lines(formula_sheet), _numfmt_lines(formula_sheet, coords),
                _merged_lines(formula_sheet), _style_lines(formula_sheet, coords),
                _cond_lines(formula_sheet), _extra_lines(formula_sheet),
                ("pivots", pivot_items), ("charts", charts)]
    meta = []
    for label, items in sections:
        if items:
            meta.append(f"▸ {label}")
            meta += [f"    {item}" for item in items]
    if meta:
        out += ["", "```meta", *meta, "```"]
    return out


def _recalculated(path, into, meta):
    """A recalculated copy of the workbook, or the path itself.

    A workbook misaka wrote carries formulas with no cached results (openpyxl stores the
    formula string and not its value), and so does one saved by a program that deferred
    calculation. Without this the reader shows every such cell as ``uncached`` even on a
    machine that could work them out.

    A **copy**: the corpus identifies a document by the sha256 of the file on disk, so
    recalculating in place would change a document's identity as a side effect of reading
    it.
    """
    if soffice.binary() is None:
        return path
    fresh = soffice.recalc(path, into=into, meta=meta)
    return fresh or path


def render(path, *, cell_range=None, meta=None):
    """The workbook as markdown. Raises ``ValueError`` naming the file when it holds no data.

    When the file carries formulas whose results are not stored and LibreOffice is
    installed, the rendering is of a recalculated copy; the file itself is never modified.
    ``meta`` collects why a recalculation did not happen, so a refusal can quote it.
    """
    import tempfile

    from misaka.core.tools._office.xlsx import cache_empty
    if cache_empty(path):
        with tempfile.TemporaryDirectory(prefix="misaka-office-recalc-") as staging:
            fresh = _recalculated(path, staging, meta)
            if fresh != path:
                return _render_book(fresh, path, cell_range=cell_range)
    return _render_book(path, path, cell_range=cell_range)


def _render_book(path, chart_source, *, cell_range=None):
    formula_book, value_book = _load(path)
    head = [_HEAD]
    # Only ever shown when the recalculation did not happen -- ``render`` renders a
    # recalculated copy when it could make one, and then no cell is uncached.
    note = ("`uncached: this workbook carries formulas whose cached results are empty, so "
            f"their values are not in the file; {soffice.INSTALL_HINT} to have them "
            "recalculated on read.`")

    if cell_range:
        name, bbox = _range(cell_range, value_book)
        mark_uncached = _uncached_anywhere(formula_book, value_book, scope=(name, bbox))
        return "\n".join([*head, *([note] if mark_uncached else []),
                          "", f"## Sheet: {name}", "", f"`cell_range: {cell_range}`",
                          *_grid(value_book[name], formula_book[name], bbox, mark_uncached)])

    mark_uncached = _uncached_anywhere(formula_book, value_book)
    if mark_uncached:
        head.append(note)

    charts = _chart_lines(chart_source)
    # A chart is filed under the sheet its *data* is on, which for a dashboard is not the
    # sheet it is drawn on. Anything no worksheet claims -- a series pointing at another
    # workbook, a deleted sheet, a chartsheet, a name whose spacing does not match a title
    # -- would otherwise be dropped without a word.
    claimed = {sheet.title for sheet in formula_book.worksheets}
    out, any_content = list(head), False
    for formula_sheet in formula_book.worksheets:
        value_sheet = value_book[formula_sheet.title]
        coords = {(cell.row, cell.column) for cell in _stored_cells(formula_sheet)
                  if cell.value is not None}
        sheet_charts = charts.get(formula_sheet.title, [])
        if not coords and not sheet_charts:
            continue                      # an empty sheet is not a page
        any_content = True
        # Owner decision D6: a hidden sheet is read rather than skipped. An archival
        # workbook hides the sheet holding the working numbers at least as often as it
        # hides junk, and for a corpus a missing number is the worse failure. The marker
        # is what tells a reader where the text came from.
        state = "" if formula_sheet.sheet_state == "visible" else (
            " (very hidden)" if formula_sheet.sheet_state == "veryHidden" else " (hidden)")
        out += ["", f"## Sheet: {formula_sheet.title}{state}"]
        out += _sheet_markdown(value_sheet, formula_sheet, coords, sheet_charts, mark_uncached)
    orphans = [bit for key, bits in sorted(charts.items()) if key not in claimed
               for bit in bits]
    if orphans:
        any_content = True
        out += ["", "## Sheet: (charts whose data sheet is not in this workbook)", "",
                "```meta", "▸ charts", *[f"    {c}" for c in orphans], "```"]
    if not any_content:
        raise ValueError(f"No text found in {os.path.basename(path)}: "
                         "the workbook has no cell content.")
    return "\n".join(out)


def render_rows(name, rows):
    """A plain table of values as one ``## Sheet:`` block.

    The output of a reader that has values and nothing else -- ``xlrd`` on a pre-2007
    workbook. It goes through the same relational rendering an Excel Table takes, so a row
    quoted out of a .xls verifies exactly the way one quoted out of a .xlsx does.
    """
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return ""
    header = [str(cell) for cell in rows[0]]
    data = [[((cell if cell != "" else None), _esc(cell))
             for cell in row] for row in rows[1:]]
    return "\n".join([f"## Sheet: {name}", "",
                       *_table_md(header, data, name=name, preview=None)])


def render_csv(path):
    """A csv or tsv as the same relational table an Excel Table renders to.

    Decoded through the corpus' own strict decoder rather than utf-8-with-replacements: a
    Shift-JIS or GB18030 data file otherwise enters as replacement characters, and a ledger
    that verifies a quotation against mojibake is worse than one that cannot read the file.
    The import is local because ``index`` imports this package.
    """
    from misaka.core.documents.index import _decode_bytes

    delimiter = "\t" if str(path).lower().endswith(".tsv") else ","
    with open(path, "rb") as handle:
        text = _decode_bytes(handle.read(), os.path.basename(path))[0]
    rows = list(_csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ValueError(f"No text found in {os.path.basename(path)}: the file has no rows.")
    header = [str(cell) for cell in rows[0]]
    data = [[((cell if cell != "" else None), _esc(cell)) for cell in row] for row in rows[1:]]
    summary = _table_md(header, data, name=os.path.basename(path), preview=0, delimiter=delimiter)
    summary = summary[:summary.index("```") + 1]
    # The preview marker is for truncated XLSX tables, not the full CSV below.
    summary = [line for line in summary if not line.startswith("▸ preview:")]
    return "\n".join([
        ("<!-- csv readout: the file's own rows, with a ```meta column summary added above "
         "them (parser-added, not file content). The rows are the file's, separator and "
         "quoting included, so a line quoted as it appears here verifies against it. -->"),
        "",
        f"## Sheet: {os.path.basename(path)}",
        "",
        *summary, text.rstrip("\r\n"),
    ])
