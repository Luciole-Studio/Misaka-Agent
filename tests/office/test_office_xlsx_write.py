"""Every xlsx op, verified by reading the workbook back with misaka's own reader.

The one thing a spreadsheet writer cannot do is compute. openpyxl stores a formula string
and not its result, so a workbook this tool writes carries formulas whose cached values are
empty until something opens it -- and a model that is not told reads the cell back as blank
and concludes its own formula was wrong. ``cache_empty`` is what makes the receipt honest,
so it gets its own tests below.
"""
from __future__ import annotations

import importlib
import zipfile

from misaka.core.documents.office import xlsx as reader
from misaka.core.tools._office import xlsx as writer

openpyxl = importlib.import_module("openpyxl")


def _book(tmp_path, name="book.xlsx", **sheet):
    path = tmp_path / name
    spec = {"name": "S", **sheet}
    outcome = writer.write(str(path), "create", {"sheets": [spec]})
    assert outcome["ok"], outcome
    return path


def _load(path, **kwargs):
    return openpyxl.load_workbook(str(path), **kwargs)


# ---- create and cells --------------------------------------------------------------------

def test_create_writes_the_sheets_it_was_given(tmp_path):
    path = tmp_path / "multi.xlsx"
    outcome = writer.write(str(path), "create", {"sheets": [
        {"name": "Revenue", "headers": ["region", "units"], "rows": [["North", 800]]},
        {"name": "Notes"},
    ]})
    assert outcome["counts"] == {"sheet": 2}
    out = reader.render(str(path))
    assert "## Sheet: Revenue" in out
    assert "region\tunits" in out
    assert _load(path).sheetnames == ["Revenue", "Notes"]


def test_create_refuses_a_path_that_exists(tmp_path):
    path = _book(tmp_path, headers=["keep"])
    outcome = writer.write(str(path), "create", {"sheets": [{"name": "other"}]})
    assert not outcome["ok"]
    assert "already exists" in outcome["summary"]
    assert _load(path).sheetnames == ["S"]


def test_an_explicit_text_type_keeps_leading_zeros(tmp_path):
    """The cure for "numbers stored as text" runs both ways: a postal code that happens to
    be digits has to stay a string, and neither direction is guessable."""
    path = _book(tmp_path)
    writer.write(str(path), "set_cell", {"sheet": "S", "cell": "A1",
                                         "value": "0123", "type": "text"})
    assert _load(path)["S"]["A1"].value == "0123"


def test_an_explicit_number_type_stores_a_number(tmp_path):
    path = _book(tmp_path)
    writer.write(str(path), "set_cell", {"sheet": "S", "cell": "A1",
                                         "value": "42", "type": "number"})
    assert _load(path)["S"]["A1"].value == 42


def test_a_date_type_parses_the_common_forms(tmp_path):
    import datetime

    path = _book(tmp_path)
    writer.write(str(path), "set_cell", {"sheet": "S", "cell": "A1",
                                         "value": "2024-03-01", "type": "date"})
    assert _load(path)["S"]["A1"].value == datetime.datetime(2024, 3, 1)  # noqa: DTZ001


def test_set_range_lays_out_a_grid_from_one_corner(tmp_path):
    path = _book(tmp_path)
    outcome = writer.write(str(path), "set_range", {
        "sheet": "S", "start_cell": "B2", "rows": [["a", "b"], ["c", "d"]]})
    assert "4 cell(s)" in outcome["summary"]
    sheet = _load(path)["S"]
    assert sheet["B2"].value == "a"
    assert sheet["C3"].value == "d"


def test_a_named_number_format_saves_the_model_knowing_excels_format_language(tmp_path):
    path = _book(tmp_path)
    writer.write(str(path), "set_cell", {"sheet": "S", "cell": "A1", "value": 0.153,
                                         "number_format": "percent2"})
    assert _load(path)["S"]["A1"].number_format == "0.00%"
    assert "15.30%" in reader.render(str(path))


def test_a_raw_format_string_passes_through(tmp_path):
    path = _book(tmp_path)
    writer.write(str(path), "set_number_format", {"sheet": "S", "cell_range": "A1",
                                                  "number_format": '"€"#,##0'})
    assert _load(path)["S"]["A1"].number_format == '"€"#,##0'


# ---- formulas and the honest limit ---------------------------------------------------------

def test_a_formula_written_by_any_op_reports_that_it_needs_recalculating(tmp_path):
    """The gate is what triggers recalculation. FrontierAgent once watched only
    ``set_cell``, so a workbook whose totals row came in through ``create`` was left with
    empty caches and read back as blank."""
    for op, args in (
        ("create", {"sheets": [{"name": "S", "rows": [["=1+1"]]}]}),
        ("set_cell", {"sheet": "S", "cell": "B1", "value": "=A1*2"}),
        ("set_range", {"sheet": "S", "start_cell": "C1", "rows": [["=A1+1"]]}),
        ("add_sheet", {"sheet": "T", "rows": [["=1+1"]]}),
    ):
        path = tmp_path / f"{op}.xlsx"
        if op != "create":
            writer.write(str(path), "create", {"sheets": [{"name": "S", "rows": [[1]]}]})
        outcome = writer.write(str(path), op, args)
        assert outcome["wrote_formula"], op


def test_a_workbook_without_formulas_reports_nothing_to_recalculate(tmp_path):
    path = tmp_path / "plain.xlsx"
    outcome = writer.write(str(path), "create",
                           {"sheets": [{"name": "S", "rows": [[1, 2]]}]})
    assert not outcome["wrote_formula"]
    assert not writer.cache_empty(str(path))


def test_cache_empty_sees_a_formula_with_no_stored_result(tmp_path):
    path = tmp_path / "sums.xlsx"
    writer.write(str(path), "create",
                 {"sheets": [{"name": "S", "rows": [[1], [2], ["=SUM(A1:A2)"]]}]})
    assert writer.cache_empty(str(path))
    assert "`uncached`" in reader.render(str(path))


def test_a_formula_whose_result_is_the_empty_string_is_not_uncalculated(tmp_path):
    """``=IF(A1>0,A1,"")`` legitimately caches an empty string. Counting that as
    uncalculated would run LibreOffice on every such workbook forever."""
    path = tmp_path / "str.xlsx"
    writer.write(str(path), "create", {"sheets": [{"name": "S", "rows": [["x"]]}]})
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
        members = {n: archive.read(n) for n in archive.namelist()}
    target = names[0]
    members[target] = members[target].replace(
        b"</sheetData>",
        b'<row r="9"><c r="A9" t="str"><f>IF(1&gt;0,"","")</f><v></v></c></row></sheetData>')
    rebuilt = tmp_path / "str2.xlsx"
    with zipfile.ZipFile(rebuilt, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    assert not writer.cache_empty(str(rebuilt))


# ---- structure ------------------------------------------------------------------------------

def test_sheets_can_be_added_renamed_and_deleted(tmp_path):
    path = _book(tmp_path)
    writer.write(str(path), "add_sheet", {"sheet": "Two", "headers": ["a"]})
    writer.write(str(path), "rename_sheet", {"sheet": "Two", "new": "Second"})
    assert _load(path).sheetnames == ["S", "Second"]
    writer.write(str(path), "delete_sheet", {"sheet": "Second"})
    assert _load(path).sheetnames == ["S"]


def test_the_last_sheet_cannot_be_deleted_or_hidden(tmp_path):
    """A workbook with no visible sheet is one Excel refuses to open."""
    path = _book(tmp_path)
    assert "cannot delete the only sheet" in writer.write(str(path), "delete_sheet",
                                                          {"sheet": "S"})
    assert "only visible sheet" in writer.write(str(path), "hide_sheet", {"sheet": "S"})
    assert _load(path)["S"].sheet_state == "visible"


def test_adding_a_sheet_that_exists_is_refused(tmp_path):
    path = _book(tmp_path)
    assert "already exists" in writer.write(str(path), "add_sheet", {"sheet": "S"})


def test_a_hidden_sheet_is_hidden_and_the_reader_says_so(tmp_path):
    path = _book(tmp_path, headers=["a"])
    writer.write(str(path), "add_sheet", {"sheet": "Workings", "headers": ["x"]})
    writer.write(str(path), "hide_sheet", {"sheet": "Workings"})
    assert _load(path)["Workings"].sheet_state == "hidden"
    assert "## Sheet: Workings (hidden)" in reader.render(str(path))


def test_an_op_naming_a_sheet_that_is_not_there_lists_the_ones_that_are(tmp_path):
    path = _book(tmp_path)
    outcome = writer.write(str(path), "set_cell", {"sheet": "Nope", "cell": "A1", "value": 1})
    assert "sheet not found: 'Nope'" in outcome
    assert "['S']" in outcome


# ---- formatting --------------------------------------------------------------------------

def test_formatting_a_range_shows_up_in_the_readout(tmp_path):
    path = _book(tmp_path, headers=["region", "units"], rows=[["North", 800]])
    writer.write(str(path), "set_cell_format", {
        "sheet": "S", "cell_range": "A1:B1", "bold": True, "fill_color": "#DDEEFF"})
    out = reader.render(str(path))
    assert "bold: A1:B1" in out
    assert "bg-color: #DDEEFF A1:B1" in out


def test_formatting_a_single_cell_works_like_a_range(tmp_path):
    """openpyxl returns a bare cell for ``A1`` and nested tuples for ``A1:C3``; subscripting
    the bare one raises and used to abort the whole batch."""
    path = _book(tmp_path, headers=["a"])
    outcome = writer.write(str(path), "set_cell_format",
                           {"sheet": "S", "cell_range": "A1", "bold": True})
    assert "1 cell(s)" in outcome


def test_a_second_format_op_keeps_what_the_first_one_set(tmp_path):
    """``Font(**changes)`` replaces the whole font and resets everything untouched, so
    setting bold would quietly drop a size an earlier op chose."""
    path = _book(tmp_path, headers=["a"])
    writer.write(str(path), "set_cell_format", {"sheet": "S", "cell_range": "A1",
                                                "font_size": 18})
    writer.write(str(path), "set_cell_format", {"sheet": "S", "cell_range": "A1",
                                                "bold": True})
    font = _load(path)["S"]["A1"].font
    assert font.bold and font.size == 18


def test_a_border_covers_the_sides_it_was_asked_for(tmp_path):
    path = _book(tmp_path, headers=["a"])
    writer.write(str(path), "set_cell_format", {
        "sheet": "S", "cell_range": "A1", "border": {"style": "thin", "sides": "all"}})
    border = _load(path)["S"]["A1"].border
    assert all(getattr(border, side).style == "thin"
               for side in ("top", "bottom", "left", "right"))


# ---- objects -----------------------------------------------------------------------------

def test_a_table_becomes_a_listobject_the_reader_reports(tmp_path):
    path = _book(tmp_path, headers=["region", "units"], rows=[["North", 800], ["South", 440]])
    writer.write(str(path), "add_table", {"sheet": "S", "cell_range": "A1:B3",
                                          "name": "Revenue"})
    assert '▸ table "Revenue"' in reader.render(str(path))


def test_a_chart_labels_its_categories_from_the_column_it_was_told(tmp_path):
    """FrontierAgent computes this and then discards the answer, so its ``categories_col``
    never did anything and a chart of C:E labelled from column A came out labelled 1, 2, 3."""
    path = _book(tmp_path, headers=["region", "skip", "units"],
                 rows=[["North", 0, 800], ["South", 0, 440]])
    writer.write(str(path), "add_chart", {"sheet": "S", "data_range": "A1:C3",
                                          "chart_type": "bar", "categories_col": "A",
                                          "title": "By region"})
    out = reader.render(str(path))
    assert "cats='S'!$A$2:$A$3" in out
    assert 'title="By region"' in out


def test_charts_can_be_cleared(tmp_path):
    path = _book(tmp_path, headers=["a", "b"], rows=[[1, 2]])
    writer.write(str(path), "add_chart", {"sheet": "S", "data_range": "A1:B2"})
    outcome = writer.write(str(path), "clear_charts", {"sheet": "S"})
    assert "cleared 1 chart(s)" in outcome
    assert "▸ charts" not in reader.render(str(path))


def test_merged_cells_are_reported_and_can_be_unmerged(tmp_path):
    path = _book(tmp_path, headers=["a", "b", "c"])
    writer.write(str(path), "merge_cells", {"sheet": "S", "cell_range": "A1:C1"})
    assert "A1:C1 (value at A1)" in reader.render(str(path))
    writer.write(str(path), "unmerge_cells", {"sheet": "S", "cell_range": "A1:C1"})
    assert "A1:C1 (value at A1)" not in reader.render(str(path))


def test_a_dropdown_and_a_conditional_rule_reach_the_readout(tmp_path):
    path = _book(tmp_path, headers=["status", "value"], rows=[["ok", 5]])
    writer.write(str(path), "add_data_validation", {
        "sheet": "S", "cell_range": "A2:A9", "kind": "list", "formula1": ["ok", "bad"]})
    writer.write(str(path), "add_conditional_formatting", {
        "sheet": "S", "cell_range": "B2:B9", "rule_type": "cell_is",
        "operator": "greaterThan", "value": 3, "fill_color": "#FFDDDD"})
    out = reader.render(str(path))
    assert 'dropdown A2:A9 "ok,bad"' in out
    assert "B2:B9 cellIs greaterThan 3" in out


def test_freeze_panes_and_auto_filter_apply(tmp_path):
    path = _book(tmp_path, headers=["a", "b"], rows=[[1, 2]])
    writer.write(str(path), "freeze_panes", {"sheet": "S", "cell": "B2"})
    writer.write(str(path), "set_auto_filter", {"sheet": "S", "cell_range": "A1:B2"})
    sheet = _load(path)["S"]
    assert sheet.freeze_panes == "B2"
    assert sheet.auto_filter.ref == "A1:B2"


def test_hidden_columns_and_rows_stay_hidden(tmp_path):
    path = _book(tmp_path, headers=["a", "b"], rows=[[1, 2]])
    writer.write(str(path), "set_column_width", {"sheet": "S", "columns": {"A": 20},
                                                 "hidden": ["B"]})
    writer.write(str(path), "set_row_height", {"sheet": "S", "rows": {"1": 30},
                                               "hidden": [2]})
    sheet = _load(path)["S"]
    assert sheet.column_dimensions["A"].width == 20
    assert sheet.column_dimensions["B"].hidden
    assert sheet.row_dimensions[2].hidden


def test_a_named_range_can_be_defined_and_removed(tmp_path):
    path = _book(tmp_path, headers=["a"])
    writer.write(str(path), "add_named_range", {"sheet": "S", "name": "Head",
                                                "cell_range": "A1"})
    assert "Head" in _load(path).defined_names
    writer.write(str(path), "delete_named_range", {"name": "Head"})
    assert "Head" not in _load(path).defined_names
    assert "not found" in writer.write(str(path), "delete_named_range", {"name": "Head"})


def test_page_setup_writes_the_print_properties(tmp_path):
    path = _book(tmp_path, headers=["a"])
    writer.write(str(path), "set_page_setup", {
        "sheet": "S", "orientation": "landscape", "fit_to_width": 1, "paper_size": "a4",
        "print_title_rows": "1:1", "margins": {"top": 0.5}})
    sheet = _load(path)["S"]
    assert sheet.page_setup.orientation == "landscape"
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.page_setup.paperSize == 9
    assert sheet.sheet_properties.pageSetUpPr.fitToPage
    assert sheet.print_title_rows == "$1:$1"      # openpyxl makes the reference absolute


def test_an_unknown_op_names_the_ones_that_exist(tmp_path):
    path = _book(tmp_path)
    outcome = writer.write(str(path), "teleport", {})
    assert "unknown xlsx op 'teleport'" in outcome
    assert "set_cell" in outcome
