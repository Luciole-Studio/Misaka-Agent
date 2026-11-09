"""Translated from pi's ``packages/coding-agent/test/frontmatter.test.ts`` and ``path-utils.test.ts`` (v1.1.0)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from misaka.core.tools.path_utils import expandPath, resolveReadPath, resolveToCwd
from misaka.utils.frontmatter import (
    FrontmatterError,
    parseFrontmatter,
    stripFrontmatter,
)

# --- parseFrontmatter ---------------------------------------------------------------------------


def test_parses_keys_strips_quotes_and_returns_body():
    input = "---\nname: \"skill-name\"\ndescription: 'A desc'\nfoo-bar: value\n---\n\nBody text"
    parsed = parseFrontmatter(input)
    assert parsed.frontmatter["name"] == "skill-name"
    assert parsed.frontmatter["description"] == "A desc"
    assert parsed.frontmatter["foo-bar"] == "value"
    assert parsed.body == "Body text"


def test_normalizes_newlines_and_handles_crlf():
    input = "---\r\nname: test\r\n---\r\nLine one\r\nLine two"
    assert parseFrontmatter(input).body == "Line one\nLine two"


def test_throws_on_invalid_yaml_frontmatter():
    input = "---\nfoo: [bar\n---\nBody"
    # MISAKA fork: the YAML parser is ruamel's, whose messages are not the `yaml` package's ("at line 1, column 10").
    with pytest.raises(FrontmatterError):
        parseFrontmatter(input)


def test_parses_multiline_yaml_syntax():
    input = "---\ndescription: |\n  Line one\n  Line two\n---\n\nBody"
    parsed = parseFrontmatter(input)
    assert parsed.frontmatter["description"] == "Line one\nLine two\n"
    assert parsed.body == "Body"


def test_returns_original_content_when_frontmatter_is_missing_or_unterminated():
    noFrontmatter = "Just text\nsecond line"
    missingEnd = "---\nname: test\nBody without terminator"
    assert parseFrontmatter(noFrontmatter).body == "Just text\nsecond line"
    assert parseFrontmatter(missingEnd).body == "---\nname: test\nBody without terminator"


def test_returns_empty_object_for_empty_or_comment_only_frontmatter():
    input = "---\n# just a comment\n---\nBody"
    assert parseFrontmatter(input).frontmatter == {}


# --- stripFrontmatter ---------------------------------------------------------------------------


def test_removes_frontmatter_and_trims_body():
    assert stripFrontmatter("---\nkey: value\n---\n\nBody\n") == "Body"


def test_returns_body_when_no_frontmatter_present():
    assert stripFrontmatter("\n  No frontmatter body  \n") == "\n  No frontmatter body  \n"


# --- path-utils: expandPath ---------------------------------------------------------------------


def test_should_expand_tilde_to_home_directory():
    assert "~" not in expandPath("~")


def test_should_expand_tilde_slash_path_to_home_directory():
    assert "~/" not in expandPath("~/Documents/file.txt")


def test_should_keep_tilde_prefixed_filenames_literal():
    assert expandPath("~draft.md") == "~draft.md"
    assert expandPath("@~draft.md") == "~draft.md"


def test_should_normalize_unicode_spaces():
    # Non-breaking space (U+00A0) should become regular space
    assert expandPath("file name.txt") == "file name.txt"


# --- path-utils: resolveToCwd -------------------------------------------------------------------


def test_should_resolve_absolute_paths_as_is():
    absolutePath = os.path.abspath(os.path.join(tempfile.gettempdir(), "absolute", "path", "file.txt"))
    result = resolveToCwd(absolutePath, os.path.abspath(os.path.join(tempfile.gettempdir(), "some", "cwd")))
    assert result == absolutePath


def test_should_resolve_relative_paths_against_cwd():
    assert resolveToCwd("relative/file.txt", "/some/cwd") == os.path.abspath(os.path.join("/some/cwd", "relative/file.txt"))


def test_should_resolve_tilde_prefixed_filenames_against_cwd():
    cwd = os.path.join(tempfile.gettempdir(), "pi-path-utils-cwd")
    assert resolveToCwd("~draft.md", cwd) == os.path.abspath(os.path.join(cwd, "~draft.md"))
    assert resolveToCwd("@~draft.md", cwd) == os.path.abspath(os.path.join(cwd, "~draft.md"))


# --- path-utils: resolveReadPath ----------------------------------------------------------------


def test_should_resolve_existing_file_path(tmp_path: Path):
    fileName = "test-file.txt"
    (tmp_path / fileName).write_text("content", encoding="utf-8")

    assert resolveReadPath(fileName, str(tmp_path)) == os.path.join(str(tmp_path), fileName)


def test_should_handle_nfc_vs_nfd_unicode_normalization(tmp_path: Path):
    # macOS stores filenames in NFD (decomposed) form: e + combining acute accent (U+0301).
    # Users typically type in NFC (composed) form: a single character (U+00E9).
    nfdFileName = "fileé.txt"
    nfcFileName = "fileé.txt"

    # Verify they have different byte sequences
    assert nfdFileName != nfcFileName
    assert nfdFileName.encode() != nfcFileName.encode()

    # Create file with NFD name
    (tmp_path / nfdFileName).write_text("content", encoding="utf-8")

    # User provides NFC path - should find the file (via filesystem normalization or our fallback)
    result = resolveReadPath(nfcFileName, str(tmp_path))
    # Result should contain the accented character (either NFC or NFD form)
    assert str(tmp_path) in result
    assert result.startswith(os.path.join(str(tmp_path), "file")) and result.endswith(".txt")


def test_should_handle_curly_quotes_vs_straight_quotes(tmp_path: Path):
    # macOS uses curly apostrophe (U+2019) in screenshot filenames.
    curlyQuoteName = "Capture d’cran.txt"  # U+2019 right single quotation mark
    straightQuoteName = "Capture d'cran.txt"  # U+0027 apostrophe

    assert curlyQuoteName != straightQuoteName

    (tmp_path / curlyQuoteName).write_text("content", encoding="utf-8")

    assert resolveReadPath(straightQuoteName, str(tmp_path)) == os.path.join(str(tmp_path), curlyQuoteName)


def test_should_handle_combined_nfc_and_curly_quote(tmp_path: Path):
    # Full macOS screenshot filename: "Capture d'écran" with a curly quote (APFS stores NFC).
    nfcCurlyName = "Capture d’écran.txt"
    nfcStraightName = "Capture d'écran.txt"

    assert nfcCurlyName != nfcStraightName

    (tmp_path / nfcCurlyName).write_text("content", encoding="utf-8")

    assert resolveReadPath(nfcStraightName, str(tmp_path)) == os.path.join(str(tmp_path), nfcCurlyName)


def test_should_handle_macos_screenshot_am_pm_variant_with_narrow_no_break_space(tmp_path: Path):
    # macOS uses narrow no-break space (U+202F) before AM/PM in screenshot names
    macosName = "Screenshot 2024-01-01 at 10.00.00 AM.png"
    userName = "Screenshot 2024-01-01 at 10.00.00 AM.png"

    (tmp_path / macosName).write_text("content", encoding="utf-8")

    # This works because tryMacOSScreenshotPath() handles this case
    assert resolveReadPath(userName, str(tmp_path)) == os.path.join(str(tmp_path), macosName)


def test_should_handle_macos_screenshot_lowercase_am_pm_variant(tmp_path: Path):
    # Some locales like en_AU use lowercase am/pm in screenshot names
    macosName = "Screenshot 2024-01-01 at 10.00.00 am.png"
    userName = "Screenshot 2024-01-01 at 10.00.00 am.png"

    (tmp_path / macosName).write_text("content", encoding="utf-8")

    # This works because tryMacOSScreenshotPath() uses case-insensitive matching
    assert resolveReadPath(userName, str(tmp_path)) == os.path.join(str(tmp_path), macosName)
