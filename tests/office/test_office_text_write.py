"""The text formats, written through the same tool as the Office ones.

They are here for symmetry: ``office`` is then the one tool that produces a deliverable, so
a model writing a report does not have to know that .docx goes one way and .md another --
nor fall back to a shell heredoc, which is how a csv ends up with a broken quote in row 400
and nobody finds out until someone opens it.

Content is written literally. For .md and .html the literal content is the formatting.
"""
from __future__ import annotations

import json

from misaka.core.tools._office import text as writer


def test_content_is_written_exactly_as_given(tmp_path):
    path = tmp_path / "notes.md"
    outcome = writer.write(str(path), "create", {"content": "# Title\n\n**bold**\n"})
    assert outcome["ok"]
    assert path.read_text(encoding="utf-8") == "# Title\n\n**bold**\n"


def test_the_line_count_goes_in_the_receipt(tmp_path):
    outcome = writer.write(str(tmp_path / "x.txt"), "create", {"content": "a\nb\nc"})
    assert outcome["counts"] == {"line": 3}


def test_rows_are_quoted_by_the_csv_rules(tmp_path):
    """Hand-built csv is where a shell heredoc goes wrong: a comma inside a field needs
    quoting and a quote inside one needs doubling."""
    path = tmp_path / "data.csv"
    writer.write(str(path), "create", {"rows": [
        ["region", "note"],
        ["North", 'a, with a comma'],
        ["South", 'a "quoted" word'],
    ]})
    assert path.read_text(encoding="utf-8") == (
        'region,note\n'
        'North,"a, with a comma"\n'
        'South,"a ""quoted"" word"\n')


def test_a_tsv_uses_tabs(tmp_path):
    path = tmp_path / "data.tsv"
    writer.write(str(path), "create", {"rows": [["a", "b"], ["1", "2"]]})
    assert path.read_text(encoding="utf-8") == "a\tb\n1\t2\n"


def test_rows_in_a_json_file_become_one_array_not_concatenated_lines(tmp_path):
    """A .json file has to parse as JSON; lines written one after another do not."""
    path = tmp_path / "out.json"
    writer.write(str(path), "create", {"rows": [{"a": 1}, {"a": 2}]})
    assert json.loads(path.read_text(encoding="utf-8")) == [{"a": 1}, {"a": 2}]


def test_rows_in_a_jsonl_file_become_one_object_per_line(tmp_path):
    path = tmp_path / "out.jsonl"
    writer.write(str(path), "create", {"rows": [{"a": 1}, {"a": 2}]})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"a": 2}]


def test_data_wins_over_rows_and_content_wins_over_both(tmp_path):
    """A caller that passed a literal string meant that string, whatever else is set."""
    path = tmp_path / "a.json"
    writer.write(str(path), "create", {"content": "literal", "data": {"x": 1},
                                       "rows": [[1]]})
    assert path.read_text(encoding="utf-8") == "literal"

    other = tmp_path / "b.json"
    writer.write(str(other), "create", {"data": {"x": 1}, "rows": [[1]]})
    assert json.loads(other.read_text(encoding="utf-8")) == {"x": 1}


def test_a_non_string_content_is_serialised_rather_than_stringified(tmp_path):
    """``str({'a': 1})`` produces Python's repr with single quotes, which is not JSON and
    not what anyone can parse back."""
    path = tmp_path / "x.json"
    writer.write(str(path), "create", {"content": {"a": 1}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


def test_create_needs_something_to_write(tmp_path):
    outcome = writer.write(str(tmp_path / "x.md"), "create", {})
    assert "create needs one of" in outcome


def test_create_refuses_a_path_that_exists(tmp_path):
    path = tmp_path / "x.md"
    writer.write(str(path), "create", {"content": "first"})
    outcome = writer.write(str(path), "create", {"content": "second"})
    assert "already exists" in outcome
    assert path.read_text(encoding="utf-8") == "first"


def test_overwrite_rebuilds_it(tmp_path):
    path = tmp_path / "x.md"
    writer.write(str(path), "create", {"content": "first"})
    writer.write(str(path), "create", {"content": "second"}, overwrite=True)
    assert path.read_text(encoding="utf-8") == "second"


def test_append_adds_the_newline_the_file_was_missing(tmp_path):
    """Otherwise the last existing line and the first new one become one line."""
    path = tmp_path / "log.txt"
    path.write_text("first", encoding="utf-8")
    writer.write(str(path), "append", {"content": "second"})
    assert path.read_text(encoding="utf-8") == "first\nsecond"


def test_append_to_a_file_ending_in_a_newline_adds_none(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("first\n", encoding="utf-8")
    writer.write(str(path), "append", {"content": "second"})
    assert path.read_text(encoding="utf-8") == "first\nsecond"


def test_append_creates_the_file_when_it_is_not_there(tmp_path):
    path = tmp_path / "new" / "log.txt"
    writer.write(str(path), "append", {"content": "line"})
    assert path.read_text(encoding="utf-8") == "line"


def test_replace_text_reports_how_many_it_changed(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("a a a", encoding="utf-8")
    outcome = writer.write(str(path), "replace_text", {"find": "a", "replace": "b"})
    assert "3 replacement(s)" in outcome["summary"]
    assert path.read_text(encoding="utf-8") == "b b b"


def test_replace_text_honours_a_count(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("a a a", encoding="utf-8")
    writer.write(str(path), "replace_text", {"find": "a", "replace": "b", "count": 2})
    assert path.read_text(encoding="utf-8") == "b b a"


def test_a_count_of_zero_means_zero_not_all(tmp_path):
    """A truthiness test on the count would fall through and replace everything, which is
    the opposite of what was asked."""
    path = tmp_path / "x.md"
    path.write_text("a a a", encoding="utf-8")
    outcome = writer.write(str(path), "replace_text",
                           {"find": "a", "replace": "b", "count": 0})
    assert path.read_text(encoding="utf-8") == "a a a"
    assert "0 replacements requested" in outcome["summary"]
    assert outcome["warn"] is None            # it did exactly what it was told


def test_replace_text_with_no_match_warns(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("body", encoding="utf-8")
    outcome = writer.write(str(path), "replace_text", {"find": "absent", "replace": "x"})
    assert outcome["ok"]
    assert "no match" in outcome["warn"]


def test_replace_text_on_a_file_that_is_not_there_says_so(tmp_path):
    outcome = writer.write(str(tmp_path / "absent.md"), "replace_text",
                           {"find": "a", "replace": "b"})
    assert "not found" in outcome


def test_an_unknown_op_names_the_three_that_exist(tmp_path):
    outcome = writer.write(str(tmp_path / "x.md"), "add_slide", {})
    assert "unsupported op 'add_slide'" in outcome
    assert "create, append, replace_text" in outcome
