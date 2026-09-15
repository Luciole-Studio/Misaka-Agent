"""Writing a workbook: twenty-seven ops, applied in order to one file.

Ported from FrontierAgent's ``plugins/tools/_writer_xlsx.py`` (audit D104-D130). Addressing
is by sheet name plus an A1 reference, which does not drift when an earlier op inserts a
row -- the same reason the docx writer anchors on text.

Two things this fixes in the port. FrontierAgent's ``add_chart`` computes which column holds
the categories and then throws the answer away (``_ = data_min_col``), so ``categories_col``
is accepted and silently ignored and the first column is always used; here it is honoured.
And a formula written by any op -- not just ``set_cell`` -- reports ``wrote_formula``, which
is what decides whether the workbook needs recalculating before anyone reads a number out
of it.

**The honest limit of writing formulas.** openpyxl stores the formula string and cannot
store its result: that is Excel's job. So a workbook this tool writes has formulas whose
cached values are empty until something opens it. ``cache_empty`` detects exactly that, and
the receipt says so rather than letting a model believe it wrote a number it only wrote the
recipe for. LibreOffice fills them in when it is installed (the plan's W22-I).
"""
from __future__ import annotations

import datetime as _dt
import os
import zipfile

from misaka.core.tools._office._receipt import result

SUFFIXES = frozenset({".xlsx"})

# The named number formats, so a model does not have to know Excel's format language. A raw
# format string is accepted too and passes through untouched.
NUMBER_FORMATS = {
    "general": "General", "integer": "0", "number2": "0.00",
    "percent": "0%", "percent2": "0.00%",
    "currency_usd": '"$"#,##0.00', "currency_eur": '"€"#,##0.00',
    "accounting": '_("$"* #,##0.00_)', "date_iso": "yyyy-mm-dd",
    "date_us": "m/d/yyyy", "datetime": "yyyy-mm-dd hh:mm", "time": "hh:mm:ss",
    "scientific": "0.00E+00", "text": "@",
}

PAPER_SIZES = {"a4": 9, "letter": 1, "legal": 5, "a3": 8, "tabloid": 3, "a5": 11}

# Column width bounds for the content-based autofit. Deterministic, no model involved.
MIN_WIDTH, MAX_WIDTH = 8, 60

SHEET_NAME_CHARS = 31

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y")

# The ops whose ``sheet`` does not name an existing worksheet: ``add_sheet`` names the one
# to create, and a named range is a workbook-level object. Resolving a sheet for these
# would refuse them for the very sheet they are about to make.
NO_EXISTING_SHEET = frozenset({"create", "add_sheet", "delete_named_range"})

OPS = ("create", "set_cell", "set_range", "add_sheet", "delete_sheet", "rename_sheet",
       "set_cell_format", "add_table", "add_chart", "clear_charts", "merge_cells",
       "unmerge_cells", "freeze_panes", "set_column_width", "set_row_height",
       "set_page_setup", "hide_sheet", "show_sheet", "set_sheet_visibility",
       "add_named_range", "delete_named_range", "add_data_validation",
       "add_conditional_formatting", "set_auto_filter", "set_number_format", "add_image")


def _argb(color):
    """A 6- or 8-digit hex colour as openpyxl's ARGB."""
    value = str(color).lstrip("#").upper()
    return value if len(value) == 8 else "FF" + value


def _coerce(value):
    """A numeric string as a number; a leading ``=`` stays a formula string."""
    if not isinstance(value, str):
        return value
    if value.startswith("="):
        return value
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _number_format(value):
    return NUMBER_FORMATS.get(str(value), value) if value else value


def _typed(value, kind):
    """A value under an explicit type.

    This is the cure for "numbers stored as text": a postal code or an account number that
    happens to be digits must stay a string, and a figure that arrived as a string must not.
    Neither is guessable, so the caller says which.
    """
    wanted = (kind or "auto").lower()
    if wanted == "text":
        return "" if value is None else str(value)
    if wanted == "number":
        if isinstance(value, float):
            return value  # int(float) would silently discard the fractional part.
        for cast in (int, float):
            try:
                return cast(value)
            except (ValueError, TypeError):
                continue
        return value
    if wanted == "formula":
        text = str(value)
        return text if text.startswith("=") else "=" + text
    if wanted == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "y")
    if wanted == "date":
        if isinstance(value, (_dt.date, _dt.datetime)):
            return value
        for fmt in _DATE_FORMATS:
            try:
                return _dt.datetime.strptime(str(value), fmt)  # noqa: DTZ007 - a cell has no zone
            except ValueError:
                continue
        return value
    return _coerce(value)


def _is_formula(value):
    return isinstance(value, str) and value.startswith("=")


def _assign(cell, value, kind):
    cell.value = _typed(value, kind)
    if (kind or "auto").lower() == "text":
        # openpyxl otherwise re-infers formulas and error codes from the string.
        cell.data_type = "s"
    return cell.data_type == "f"


def _autofit(sheet):
    """Column widths from the content: the longest cell plus two, clamped."""
    widths = {}
    for row in sheet.iter_rows():
        for cell in row:
            letter = getattr(cell, "column_letter", None)
            if cell.value is not None and letter:
                widths[letter] = max(widths.get(letter, 0), len(str(cell.value)))
    for letter, width in widths.items():
        sheet.column_dimensions[letter].width = min(max(width + 2, MIN_WIDTH), MAX_WIDTH)


def _sheet(workbook, name):
    if name is None:
        return workbook.active
    if name not in workbook.sheetnames:
        raise ValueError(f"sheet not found: {name!r} (this workbook has {workbook.sheetnames})")
    return workbook[name]


def _fill(sheet, headers, rows):
    """Populate a sheet, and report whether any formula went in.

    The report is load-bearing: a workbook built by ``create`` with a totals row of
    ``=SUM(...)`` has empty caches exactly like one built cell by cell, and a gate that only
    watched ``set_cell`` left those workbooks reading as blank.
    """
    wrote_formula = False
    if headers:
        sheet.append(list(headers))
        sheet.freeze_panes = "A2"
    for row in rows or []:
        values = [_coerce(value) for value in row]
        wrote_formula = wrote_formula or any(_is_formula(value) for value in values)
        sheet.append(values)
    _autofit(sheet)
    return wrote_formula


def cache_empty(path):
    """Whether any cell holds a formula with no cached result.

    A namespace-aware scan of sheet XML, not the openpyxl object model:
    a formula cell is ``<c><f>…</f><v>cache</v></c>`` and all three empty shapes have to be
    caught -- no ``<v>`` at all, the empty ``<v></v>`` openpyxl actually writes, and a
    self-closing ``<v/>``. A shared-formula dependent is a self-closing ``<f t="shared"/>``.

    One exclusion: an empty ``<v></v>`` under ``<c t="str">`` is a formula that computed the
    empty string, which is a real cached result. Without it every workbook with a
    ``=IF(...,"")`` would be reported as uncalculated forever.
    """
    import xml.etree.ElementTree as ET
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
                    continue
                root = ET.fromstring(archive.read(name))
                for cell in root.iter(namespace + "c"):
                    if cell.find(namespace + "f") is None or cell.get("t") == "str":
                        continue
                    value = cell.find(namespace + "v")
                    if value is None or not (value.text or "").strip():
                        return True
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        return False
    return False


def _cells(sheet, reference):
    """Every cell of an A1 range as a flat list, single cells included.

    openpyxl returns a bare cell for ``A1`` and nested tuples for ``A1:C3``; subscripting
    the bare cell raises and would abort the whole batch.
    """
    selected = sheet[reference]
    if hasattr(selected, "value"):
        return [selected]
    flat = []
    for row in selected:
        flat.extend(row if isinstance(row, tuple) else [row])
    return flat


def _font_edit(cell, changes):
    """Apply font changes one attribute at a time.

    ``Font(**changes)`` replaces the whole font and resets everything untouched back to the
    default, so setting bold would quietly drop a size and a colour set by an earlier op.
    """
    from copy import copy

    from openpyxl.styles import Color
    font = copy(cell.font)
    for name, value in changes.items():
        setattr(font, name, Color(rgb=str(value)) if name == "color" else value)
    cell.font = font


def _format_cells(sheet, args):
    from copy import copy

    from openpyxl.styles import PatternFill, Side
    cells = _cells(sheet, args["cell_range"])
    font = {}
    for key, name in (("bold", "bold"), ("italic", "italic")):
        if args.get(key) is not None:
            font[name] = bool(args[key])
    if args.get("font_color"):
        font["color"] = str(args["font_color"]).lstrip("#")
    if args.get("font_size"):
        font["size"] = float(args["font_size"])
    if args.get("font_name"):
        font["name"] = args["font_name"]
    alignment = {}
    horizontal = args.get("align_h") or args.get("align")
    if horizontal:
        alignment["horizontal"] = str(horizontal).lower()
    if args.get("align_v"):
        vertical = str(args["align_v"]).lower()
        alignment["vertical"] = {"middle": "center"}.get(vertical, vertical)
    if args.get("wrap") is not None:
        alignment["wrap_text"] = bool(args["wrap"])
    border = None
    if args.get("border"):
        spec = args["border"]
        style = str(spec.get("style", "thin")).lower()
        side = Side(style=(None if style == "none" else style),
                    color=_argb(spec["color"]) if spec.get("color") else None)
        wanted = spec.get("sides", "all")
        wanted = [wanted] if isinstance(wanted, str) else list(wanted)
        edges = set()
        for name in wanted:
            if str(name).lower() in ("all", "outline"):
                edges |= {"top", "bottom", "left", "right"}
            else:
                edges.add(str(name).lower())
        border = dict.fromkeys(edges, side)
    for cell in cells:
        if font:
            _font_edit(cell, font)
        if args.get("fill_color"):
            cell.fill = PatternFill("solid", fgColor=_argb(args["fill_color"]))
        if args.get("number_format"):
            cell.number_format = _number_format(args["number_format"])
        if alignment:
            updated = copy(cell.alignment)
            for name, value in alignment.items():
                setattr(updated, name, value)
            cell.alignment = updated
        if border is not None:
            updated = copy(cell.border)
            for name, value in border.items():
                setattr(updated, name, value)
            cell.border = updated
    return f"formatted {len(cells)} cell(s) in {args['cell_range']}"


def _add_chart(sheet, args):
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.utils.cell import column_index_from_string, range_boundaries
    kind = args.get("chart_type", "bar")
    chart = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(kind, BarChart)()
    if args.get("title"):
        chart.title = args["title"]
    left, top, right, bottom = range_boundaries(args["data_range"])
    header = args.get("include_header", True)
    # Which column holds the category labels. FrontierAgent computes this and then discards
    # it, so its ``categories_col`` never did anything; a chart of columns C:E labelled from
    # column A came out labelled 1, 2, 3.
    categories = args.get("categories_col")
    if categories is None:
        category_column = left
    elif isinstance(categories, str):
        category_column = column_index_from_string(categories.upper())
    else:
        category_column = int(categories)
    for column in range(left, right + 1):
        if column != category_column:
            chart.add_data(Reference(sheet, min_col=column, min_row=top,
                                     max_col=column, max_row=bottom), titles_from_data=header)
    if not chart.series:
        raise ValueError("add_chart needs at least one data column besides categories")
    chart.set_categories(Reference(sheet, min_col=category_column,
                                   min_row=(top + 1 if header else top), max_row=bottom))
    if args.get("width"):
        chart.width = float(args["width"])       # centimetres, openpyxl's unit
    if args.get("height"):
        chart.height = float(args["height"])
    sheet.add_chart(chart, args.get("anchor_cell", "E2"))
    return f"added {kind} chart from {args['data_range']}"


def _validation(sheet, args):
    from openpyxl.worksheet.datavalidation import DataValidation
    kind = args.get("kind", "list")
    formula = args.get("formula1", "")
    if kind == "list" and isinstance(formula, (list, tuple)):
        formula = ",".join(str(item) for item in formula)
    if (kind == "list" and not str(formula).startswith('"')
            and "," in str(formula) and "!" not in str(formula)):
        formula = f'"{formula}"'          # an inline list has to be a quoted literal
    validation = DataValidation(
        type=kind, formula1=formula, formula2=args.get("formula2"),
        operator=args.get("operator", "between"),
        allow_blank=bool(args.get("allow_blank", True)), showDropDown=False)
    if args.get("prompt"):
        validation.prompt = args["prompt"]
        validation.promptTitle = args.get("prompt_title", "")
    if args.get("error"):
        validation.error = args["error"]
        validation.errorTitle = args.get("error_title", "")
        validation.showErrorMessage = True
    sheet.add_data_validation(validation)
    validation.add(args["cell_range"])
    return f"added {kind} validation on {args['cell_range']}"


def _conditional(sheet, args):
    from openpyxl.formatting.rule import (
        CellIsRule,
        ColorScaleRule,
        DataBarRule,
        FormulaRule,
    )
    from openpyxl.styles import Font, PatternFill
    kind = args.get("rule_type", "cell_is")
    styling = {}
    if args.get("fill_color"):
        styling["fill"] = PatternFill("solid", fgColor=_argb(args["fill_color"]))
    if args.get("font_color"):
        styling["font"] = Font(color=_argb(args["font_color"]))
    if kind == "cell_is":
        rule = CellIsRule(operator=args.get("operator", "greaterThan"),
                          formula=[str(value) for value in
                                   (args.get("formula") or [args.get("value", 0)])],
                          **styling)
    elif kind == "color_scale":
        colors = [_argb(c) for c in
                  (args.get("colors") or ["FFFF0000", "FFFFFF00", "FF00FF00"])]
        rule = (ColorScaleRule(start_type="min", start_color=colors[0],
                               end_type="max", end_color=colors[1]) if len(colors) == 2
                else ColorScaleRule(start_type="min", start_color=colors[0],
                                    mid_type="percentile", mid_value=50, mid_color=colors[1],
                                    end_type="max", end_color=colors[2]))
    elif kind == "data_bar":
        rule = DataBarRule(start_type="min", end_type="max",
                           color=_argb(args.get("color", "FF638EC6")))
    elif kind == "formula":
        rule = FormulaRule(formula=args["formula"], **styling)
    else:
        return f"[error] unknown rule_type {kind!r}"
    sheet.conditional_formatting.add(args["cell_range"], rule)
    return f"added {kind} conditional formatting on {args['cell_range']}"


def _page_setup(sheet, args):
    setup = sheet.page_setup
    if args.get("orientation"):
        setup.orientation = ("landscape"
                             if str(args["orientation"]).lower().startswith("land")
                             else "portrait")
    width, height = args.get("fit_to_width"), args.get("fit_to_height")
    if width is not None or height is not None or args.get("fit_to_page"):
        from openpyxl.worksheet.properties import PageSetupProperties
        sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        if width is not None:
            setup.fitToWidth = int(width)
        if height is not None:
            setup.fitToHeight = int(height)
    if args.get("scale") is not None:
        setup.scale = int(args["scale"])
    if args.get("paper_size"):
        size = PAPER_SIZES.get(str(args["paper_size"]).lower())
        if size:
            setup.paperSize = size
    if args.get("margins"):
        from openpyxl.worksheet.page import PageMargins
        sheet.page_margins = PageMargins(
            **{name: float(value) for name, value in args["margins"].items()
               if name in ("left", "right", "top", "bottom", "header", "footer")})
    if args.get("center_h") is not None:
        sheet.print_options.horizontalCentered = bool(args["center_h"])
    if args.get("center_v") is not None:
        sheet.print_options.verticalCentered = bool(args["center_v"])
    for key, attribute in (("print_area", "print_area"),
                           ("print_title_rows", "print_title_rows"),
                           ("print_title_cols", "print_title_cols")):
        if args.get(key):
            setattr(sheet, attribute, args[key])
    return f"set page setup on {sheet.title}"


def _create(path, args, overwrite):
    import openpyxl
    if os.path.exists(path) and not (overwrite or args.get("overwrite")):
        return result(f"create refused: {path} already exists — use set_cell or add_sheet to "
                      "edit it, or pass overwrite:true to rebuild it", ok=False)
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    sheets = args.get("sheets") or [{"name": "Sheet1"}]
    wrote_formula = False
    for spec in sheets:
        sheet = workbook.create_sheet(title=(spec.get("name") or "Sheet")[:SHEET_NAME_CHARS])
        wrote_formula = _fill(sheet, spec.get("headers"), spec.get("rows")) or wrote_formula
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    workbook.save(path)
    return result(f"created xlsx: {path}", counts={"sheet": len(sheets)},
                  wrote_formula=wrote_formula)


def write(path, op, args, *, overwrite=False):
    """Apply one op to a workbook. Returns a result dict or an ``[error]`` string."""
    try:
        import openpyxl
    except ImportError:                                             # pragma: no cover
        return "[error] openpyxl is not installed."

    if op == "create":
        return _create(path, args, overwrite)
    if not os.path.exists(path):
        return f"[error] file not found (this op edits an existing file): {path}"
    workbook = openpyxl.load_workbook(path)
    sheet = None
    if op not in NO_EXISTING_SHEET:
        try:
            sheet = _sheet(workbook, args.get("sheet") or args.get("old"))
        except ValueError as error:
            return f"[error] {error}"

    def saved(summary, **extra):
        workbook.save(path)
        return result(summary, **extra) if extra else summary

    if op == "set_cell":
        cell = sheet[args["cell"]]
        wrote_formula = _assign(cell, args.get("value"), args.get("type"))
        if args.get("number_format"):
            cell.number_format = _number_format(args["number_format"])
        return saved(f"set {sheet.title}!{args['cell']} = {args.get('value')!r}",
                     wrote_formula=wrote_formula)

    if op == "set_range":
        from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter
        top, left = coordinate_to_tuple(args["start_cell"])
        types, written, wrote_formula = args.get("types"), 0, False
        for r, row in enumerate(args["rows"]):
            for c, value in enumerate(row):
                if isinstance(types, list):
                    kind = (types[r][c] if r < len(types) and isinstance(types[r], list)
                            and c < len(types[r]) else None)
                else:
                    kind = types
                cell = sheet[f"{get_column_letter(left + c)}{top + r}"]
                wrote_formula = _assign(cell, value, kind) or wrote_formula
                written += 1
        return saved(f"set {written} cell(s) from {args['start_cell']}",
                     wrote_formula=wrote_formula)

    if op == "add_sheet":
        name = str(args["sheet"])[:SHEET_NAME_CHARS]
        if name in workbook.sheetnames:
            return f"[error] sheet already exists: {name}"
        wrote_formula = _fill(workbook.create_sheet(title=name),
                              args.get("headers"), args.get("rows"))
        return saved(f"added sheet {name!r}", wrote_formula=wrote_formula)

    if op == "delete_sheet":
        if len(workbook.sheetnames) == 1:
            return "[error] cannot delete the only sheet."
        workbook.remove(sheet)
        return saved(f"deleted sheet {sheet.title!r}")

    if op == "rename_sheet":
        new = args.get("new") or args.get("new_name")
        if not new:
            return "[error] rename_sheet needs 'new'."
        old = sheet.title
        sheet.title = str(new)[:SHEET_NAME_CHARS]
        return saved(f"renamed sheet {old!r} -> {sheet.title!r}")

    if op in ("set_cell_format", "format_cells"):
        return saved(_format_cells(sheet, args))
    if op == "add_chart":
        return saved(_add_chart(sheet, args))
    if op == "clear_charts":
        count = len(sheet._charts)
        sheet._charts = []
        return saved(f"cleared {count} chart(s) on {sheet.title}")
    if op == "merge_cells":
        sheet.merge_cells(args["cell_range"])
        return saved(f"merged {args['cell_range']}")
    if op == "unmerge_cells":
        sheet.unmerge_cells(args["cell_range"])
        return saved(f"unmerged {args['cell_range']}")
    if op == "freeze_panes":
        sheet.freeze_panes = args.get("cell") or args.get("cell_range") or "A2"
        return saved(f"froze panes at {sheet.freeze_panes}")

    if op == "set_column_width":
        columns = args.get("columns") or {}
        for name, width in columns.items():
            sheet.column_dimensions[str(name).upper()].width = float(width)
        for name in args.get("hidden") or []:
            sheet.column_dimensions[str(name).upper()].hidden = True
        hidden = f", hid {len(args['hidden'])}" if args.get("hidden") else ""
        return saved(f"set width on {len(columns)} column(s){hidden}")

    if op == "set_row_height":
        rows = args.get("rows") or {}
        for number, height in rows.items():
            sheet.row_dimensions[int(number)].height = float(height)
        for number in args.get("hidden") or []:
            sheet.row_dimensions[int(number)].hidden = True
        hidden = f", hid {len(args['hidden'])}" if args.get("hidden") else ""
        return saved(f"set height on {len(rows)} row(s){hidden}")

    if op == "set_page_setup":
        return saved(_page_setup(sheet, args))

    if op in ("hide_sheet", "show_sheet", "set_sheet_visibility"):
        hidden = (True if op == "hide_sheet" else
                  False if op == "show_sheet" else bool(args.get("hidden", True)))
        sheet.sheet_state = "hidden" if hidden else "visible"
        if all(other.sheet_state != "visible" for other in workbook.worksheets):
            sheet.sheet_state = "visible"
            return "[error] cannot hide the only visible sheet."
        return saved(f"sheet {sheet.title!r} -> {sheet.sheet_state}")

    if op == "add_named_range":
        from openpyxl.utils import quote_sheetname
        from openpyxl.workbook.defined_name import DefinedName
        reference = f"{quote_sheetname(sheet.title)}!{args['cell_range']}"
        workbook.defined_names.add(DefinedName(args["name"], attr_text=reference))
        return saved(f"defined name {args['name']!r} -> {reference}")

    if op == "delete_named_range":
        name = args["name"]
        if name not in workbook.defined_names:
            return f"[error] named range not found: {name!r}"
        del workbook.defined_names[name]
        return saved(f"deleted name {name!r}")

    if op == "add_data_validation":
        return saved(_validation(sheet, args))
    if op == "add_conditional_formatting":
        outcome = _conditional(sheet, args)
        return outcome if outcome.startswith("[error]") else saved(outcome)

    if op == "set_auto_filter":
        sheet.auto_filter.ref = (args.get("cell_range") or args.get("ref")
                                 or sheet.dimensions)
        return saved(f"set auto_filter on {sheet.auto_filter.ref}")

    if op == "set_number_format":
        fmt = _number_format(args["number_format"])
        cells = _cells(sheet, args["cell_range"])
        for cell in cells:
            cell.number_format = fmt
        return saved(f"set number_format on {len(cells)} cell(s)")

    if op == "add_image":
        from openpyxl.drawing.image import Image
        source = args.get("image_path") or args.get("path")
        if not source or not os.path.exists(str(source)):
            return f"[error] add_image: no such image {source!r}"
        image = Image(str(source))
        if args.get("width"):
            image.width = int(args["width"])
        if args.get("height"):
            image.height = int(args["height"])
        sheet.add_image(image, args.get("anchor_cell", "A1"))
        return saved(f"added image at {args.get('anchor_cell', 'A1')}")

    if op == "add_table":
        from openpyxl.utils.cell import get_column_letter, range_boundaries
        from openpyxl.worksheet.table import Table, TableStyleInfo
        reference = args["cell_range"]
        name = args.get("name") or f"Table{len(sheet.tables) + 1}"
        if args.get("headers"):
            # A ListObject requires unique names on its first row; write them when the
            # caller supplied them rather than failing on an empty header row.
            left, top, _right, _bottom = range_boundaries(reference)
            for index, header in enumerate(args["headers"]):
                sheet[f"{get_column_letter(left + index)}{top}"] = header
        table = Table(displayName=name, ref=reference)
        table.tableStyleInfo = TableStyleInfo(
            name=args.get("style", "TableStyleMedium9"), showRowStripes=True,
            showColumnStripes=False, showFirstColumn=False, showLastColumn=False)
        sheet.add_table(table)
        return saved(f"added table {name!r} over {reference} (ListObject)")

    return f"[error] unknown xlsx op {op!r}: this writes {', '.join(OPS)}."


def formula_errors(path):
    """FrontierAgent _xlsx_recalc's post-save error scan (no silent scan failures)."""
    import openpyxl
    errors = []
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        for sheet in workbook.worksheets:
            for coordinate in sorted(sheet._cells):
                cell = sheet._cells[coordinate]
                if cell.data_type == "e":
                    errors.append(f"{sheet.title}!{cell.coordinate} {cell.value}")
    finally:
        workbook.close()
    return errors
