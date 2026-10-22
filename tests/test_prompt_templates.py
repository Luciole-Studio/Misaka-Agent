"""Translated from pi's ``packages/coding-agent/test/prompt-templates.test.ts`` (v1.3.0).

Tests for prompt template argument parsing and substitution.

Tests verify:
- Argument parsing with quotes and special characters
- Placeholder substitution ($1, $2, $@, $ARGUMENTS)
- No recursive substitution of patterns in argument values
- Edge cases and integration between parsing and substitution

MISAKA fork: ``loadPromptTemplates`` has no ``includeDefaults`` option (it was always false), so the load calls omit it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from misaka.config.engine import get_agent_dir
from misaka.core.prompt_templates import (
    PromptTemplate,
    expandPromptTemplate,
    loadPromptTemplates,
    parseCommandArgs,
    substituteArgs,
)
from misaka.core.source_info import SourceInfo

# ============================================================================
# substituteArgs
# ============================================================================


def test_should_replace_arguments_with_all_args_joined():
    assert substituteArgs("Test: $ARGUMENTS", ["a", "b", "c"]) == "Test: a b c"


def test_should_replace_at_with_all_args_joined():
    assert substituteArgs("Test: $@", ["a", "b", "c"]) == "Test: a b c"


def test_should_replace_at_and_arguments_identically():
    args = ["foo", "bar", "baz"]
    assert substituteArgs("Test: $@", args) == substituteArgs("Test: $ARGUMENTS", args)


# CRITICAL: argument values containing patterns should remain literal
def test_should_not_recursively_substitute_patterns_in_argument_values():
    assert substituteArgs("$ARGUMENTS", ["$1", "$ARGUMENTS"]) == "$1 $ARGUMENTS"
    assert substituteArgs("$@", ["$100", "$1"]) == "$100 $1"
    assert substituteArgs("$ARGUMENTS", ["$100", "$1"]) == "$100 $1"


def test_should_support_mixed_1_2_and_arguments():
    assert substituteArgs("$1: $ARGUMENTS", ["prefix", "a", "b"]) == "prefix: prefix a b"


def test_should_support_mixed_1_2_and_at():
    assert substituteArgs("$1: $@", ["prefix", "a", "b"]) == "prefix: prefix a b"


def test_should_handle_empty_arguments_array_with_arguments():
    assert substituteArgs("Test: $ARGUMENTS", []) == "Test: "


def test_should_handle_empty_arguments_array_with_at():
    assert substituteArgs("Test: $@", []) == "Test: "


def test_should_handle_empty_arguments_array_with_1():
    assert substituteArgs("Test: $1", []) == "Test: "


def test_should_handle_multiple_occurrences_of_arguments():
    assert substituteArgs("$ARGUMENTS and $ARGUMENTS", ["a", "b"]) == "a b and a b"


def test_should_handle_multiple_occurrences_of_at():
    assert substituteArgs("$@ and $@", ["a", "b"]) == "a b and a b"


def test_should_handle_mixed_occurrences_of_at_and_arguments():
    assert substituteArgs("$@ and $ARGUMENTS", ["a", "b"]) == "a b and a b"


def test_should_handle_special_characters_in_arguments():
    # Note: $100 in argument doesn't get partially matched - full strings are substituted
    assert substituteArgs("$1 $2: $ARGUMENTS", ["arg100", "@user"]) == "arg100 @user: arg100 @user"


def test_should_handle_out_of_range_numbered_placeholders():
    # Note: Out-of-range placeholders become empty strings (preserving spaces from template)
    assert substituteArgs("$1 $2 $3 $4 $5", ["a", "b"]) == "a b   "


def test_should_handle_unicode_characters():
    assert substituteArgs("$ARGUMENTS", ["日本語", "🎉", "café"]) == "日本語 🎉 café"


def test_should_preserve_newlines_and_tabs_in_argument_values():
    assert substituteArgs("$1 $2", ["line1\nline2", "tab\tthere"]) == "line1\nline2 tab\tthere"


def test_should_handle_consecutive_dollar_patterns():
    assert substituteArgs("$1$2", ["a", "b"]) == "ab"


def test_should_handle_quoted_arguments_with_spaces():
    assert substituteArgs("$ARGUMENTS", ["first arg", "second arg"]) == "first arg second arg"


def test_should_handle_single_argument_with_arguments():
    assert substituteArgs("Test: $ARGUMENTS", ["only"]) == "Test: only"


def test_should_handle_single_argument_with_at():
    assert substituteArgs("Test: $@", ["only"]) == "Test: only"


def test_should_handle_0_zero_index():
    assert substituteArgs("$0", ["a", "b"]) == ""


def test_should_handle_decimal_number_in_pattern_only_integer_part_matches():
    assert substituteArgs("$1.5", ["a"]) == "a.5"


def test_should_handle_arguments_as_part_of_word():
    assert substituteArgs("pre$ARGUMENTS", ["a", "b"]) == "prea b"


def test_should_handle_at_as_part_of_word():
    assert substituteArgs("pre$@", ["a", "b"]) == "prea b"


def test_should_handle_empty_arguments_in_middle_of_list():
    assert substituteArgs("$ARGUMENTS", ["a", "", "c"]) == "a  c"


def test_should_handle_trailing_and_leading_spaces_in_arguments():
    assert substituteArgs("$ARGUMENTS", ["  leading  ", "trailing  "]) == "  leading   trailing  "


def test_should_handle_argument_containing_pattern_partially():
    assert substituteArgs("Prefix $ARGUMENTS suffix", ["ARGUMENTS"]) == "Prefix ARGUMENTS suffix"


def test_should_handle_non_matching_patterns():
    assert substituteArgs("$A $$ $ $ARGS", ["a"]) == "$A $$ $ $ARGS"


def test_should_handle_case_variations_case_sensitive():
    assert substituteArgs("$arguments $Arguments $ARGUMENTS", ["a", "b"]) == "$arguments $Arguments a b"


def test_should_handle_both_syntaxes_in_same_command_with_same_result():
    args = ["x", "y", "z"]
    result1 = substituteArgs("$@ and $ARGUMENTS", args)
    result2 = substituteArgs("$ARGUMENTS and $@", args)
    assert result1 == result2
    assert result1 == "x y z and x y z"


def test_should_handle_very_long_argument_lists():
    args = [f"arg{i}" for i in range(100)]
    result = substituteArgs("$ARGUMENTS", args)
    assert result == " ".join(args)


def test_should_handle_numbered_placeholders_with_single_digit():
    assert substituteArgs("$1 $2 $3", ["a", "b", "c"]) == "a b c"


def test_should_handle_numbered_placeholders_with_multiple_digits():
    args = [f"val{i}" for i in range(15)]
    assert substituteArgs("$10 $12 $15", args) == "val9 val11 val14"


def test_should_handle_escaped_dollar_signs_literal_backslash_preserved():
    # Note: No escape mechanism exists - backslash is treated literally
    assert substituteArgs("Price: \\$100", []) == "Price: \\"


def test_should_handle_mixed_numbered_and_wildcard_placeholders():
    assert (
        substituteArgs("$1: $@ ($ARGUMENTS)", ["first", "second", "third"])
        == "first: first second third (first second third)"
    )


def test_should_handle_command_with_no_placeholders():
    assert substituteArgs("Just plain text", ["a", "b"]) == "Just plain text"


def test_should_handle_command_with_only_placeholders():
    assert substituteArgs("$1 $2 $@", ["a", "b", "c"]) == "a b a b c"


# ============================================================================
# substituteArgs - Positional Defaults
# ============================================================================


def test_should_use_default_when_positional_arg_is_missing():
    assert substituteArgs("List exactly ${1:-7} next steps", []) == "List exactly 7 next steps"


def test_should_support_defaults_for_all_arguments():
    template = "${@:-default}\n${ARGUMENTS:-default}"

    assert substituteArgs(template, []) == "default\ndefault"
    assert (
        substituteArgs(template, ["This", "would", "be", "the", "arguments"])
        == "This would be the arguments\nThis would be the arguments"
    )


def test_should_use_positional_arg_when_present():
    assert substituteArgs("List exactly ${1:-7} next steps", ["3"]) == "List exactly 3 next steps"


def test_should_use_default_when_positional_arg_is_empty():
    assert substituteArgs("Mode: ${1:-brief}", [""]) == "Mode: brief"


def test_should_support_multiple_positional_defaults():
    assert substituteArgs("${1:-7} ${2:-brief}", []) == "7 brief"
    assert substituteArgs("${1:-7} ${2:-brief}", ["3"]) == "3 brief"
    assert substituteArgs("${1:-7} ${2:-brief}", ["3", "verbose"]) == "3 verbose"


def test_should_not_recursively_substitute_patterns_in_arg_values():
    assert substituteArgs("${1:-7}", ["$ARGUMENTS"]) == "$ARGUMENTS"
    assert substituteArgs("${1:-7}", ["$1"]) == "$1"


def test_should_not_recursively_substitute_patterns_in_default_values():
    assert substituteArgs("${1:-$ARGUMENTS}", ["a", "b"]) == "a"
    assert substituteArgs("${3:-$ARGUMENTS}", ["a", "b"]) == "$ARGUMENTS"


def test_should_support_defaults_with_spaces():
    assert substituteArgs("${1:-seven steps}", []) == "seven steps"


def test_should_support_out_of_range_positional_defaults():
    assert substituteArgs("${3:-fallback}", ["a", "b"]) == "fallback"


def test_should_mix_positional_defaults_with_existing_placeholders():
    assert substituteArgs("$1 ${2:-x} $ARGUMENTS", ["a"]) == "a x a"


# ============================================================================
# substituteArgs - Array Slicing (Bash-Style)
# ============================================================================


def test_should_slice_from_index():
    assert substituteArgs("${@:2}", ["a", "b", "c", "d"]) == "b c d"
    assert substituteArgs("${@:1}", ["a", "b", "c"]) == "a b c"
    assert substituteArgs("${@:3}", ["a", "b", "c", "d"]) == "c d"


def test_should_slice_with_length():
    assert substituteArgs("${@:2:2}", ["a", "b", "c", "d"]) == "b c"
    assert substituteArgs("${@:1:1}", ["a", "b", "c"]) == "a"
    assert substituteArgs("${@:3:1}", ["a", "b", "c", "d"]) == "c"
    assert substituteArgs("${@:2:3}", ["a", "b", "c", "d", "e"]) == "b c d"


def test_should_handle_out_of_range_slices():
    assert substituteArgs("${@:99}", ["a", "b"]) == ""
    assert substituteArgs("${@:5}", ["a", "b"]) == ""
    assert substituteArgs("${@:10:5}", ["a", "b"]) == ""


def test_should_handle_zero_length_slices():
    assert substituteArgs("${@:2:0}", ["a", "b", "c"]) == ""
    assert substituteArgs("${@:1:0}", ["a", "b"]) == ""


def test_should_handle_length_exceeding_array():
    assert substituteArgs("${@:2:99}", ["a", "b", "c"]) == "b c"
    assert substituteArgs("${@:1:10}", ["a", "b"]) == "a b"


def test_should_process_slice_before_simple_at():
    assert substituteArgs("${@:2} vs $@", ["a", "b", "c"]) == "b c vs a b c"
    assert substituteArgs("First: ${@:1:1}, All: $@", ["x", "y", "z"]) == "First: x, All: x y z"


def test_should_not_recursively_substitute_slice_patterns_in_args():
    assert substituteArgs("${@:1}", ["${@:2}", "test"]) == "${@:2} test"
    assert substituteArgs("${@:2}", ["a", "${@:3}", "c"]) == "${@:3} c"


def test_should_handle_mixed_usage_with_positional_args():
    assert substituteArgs("$1: ${@:2}", ["cmd", "arg1", "arg2"]) == "cmd: arg1 arg2"
    assert substituteArgs("$1 $2 ${@:3}", ["a", "b", "c", "d"]) == "a b c d"


def test_should_treat_at_0_as_all_args():
    assert substituteArgs("${@:0}", ["a", "b", "c"]) == "a b c"


def test_should_handle_empty_args_array():
    assert substituteArgs("${@:2}", []) == ""
    assert substituteArgs("${@:1}", []) == ""


def test_should_handle_single_arg_array():
    assert substituteArgs("${@:1}", ["only"]) == "only"
    assert substituteArgs("${@:2}", ["only"]) == ""


def test_should_handle_slice_in_middle_of_text():
    assert substituteArgs("Process ${@:2} with $1", ["tool", "file1", "file2"]) == "Process file1 file2 with tool"


def test_should_handle_multiple_slices_in_one_template():
    assert substituteArgs("${@:1:1} and ${@:2}", ["a", "b", "c"]) == "a and b c"
    assert substituteArgs("${@:1:2} vs ${@:3:2}", ["a", "b", "c", "d", "e"]) == "a b vs c d"


def test_should_handle_quoted_arguments_in_slices():
    assert substituteArgs("${@:2}", ["cmd", "first arg", "second arg"]) == "first arg second arg"


def test_should_handle_special_characters_in_sliced_args():
    assert substituteArgs("${@:2}", ["cmd", "$100", "@user", "#tag"]) == "$100 @user #tag"


def test_should_handle_unicode_in_sliced_args():
    assert substituteArgs("${@:1}", ["日本語", "🎉", "café"]) == "日本語 🎉 café"


def test_should_combine_positional_slice_and_wildcard_placeholders():
    template = "Run $1 on ${@:2:2}, then process $@"
    args = ["eslint", "file1.ts", "file2.ts", "file3.ts"]
    assert (
        substituteArgs(template, args) == "Run eslint on file1.ts file2.ts, then process eslint file1.ts file2.ts file3.ts"
    )


def test_should_handle_slice_with_no_spacing():
    assert substituteArgs("prefix${@:2}suffix", ["a", "b", "c"]) == "prefixb csuffix"


def test_should_handle_large_slice_lengths_gracefully():
    args = [f"arg{i + 1}" for i in range(10)]
    assert substituteArgs("${@:5:100}", args) == "arg5 arg6 arg7 arg8 arg9 arg10"


# ============================================================================
# parseCommandArgs
# ============================================================================


def test_should_parse_simple_space_separated_arguments():
    assert parseCommandArgs("a b c") == ["a", "b", "c"]


def test_should_parse_quoted_arguments_with_spaces():
    assert parseCommandArgs('"first arg" second') == ["first arg", "second"]


def test_should_parse_single_quoted_arguments():
    assert parseCommandArgs("'first arg' second") == ["first arg", "second"]


def test_should_parse_mixed_quote_styles():
    assert parseCommandArgs('"double" \'single\' "double again"') == ["double", "single", "double again"]


def test_should_handle_empty_string():
    assert parseCommandArgs("") == []


def test_should_handle_extra_spaces():
    assert parseCommandArgs("a  b   c") == ["a", "b", "c"]


def test_should_handle_tabs_as_separators():
    assert parseCommandArgs("a\tb\tc") == ["a", "b", "c"]


def test_should_handle_quoted_empty_string():
    # Empty quotes keep their position as an empty argument (#10867)
    assert parseCommandArgs('"" " "') == ["", " "]
    assert parseCommandArgs("a '' c") == ["a", "", "c"]
    assert parseCommandArgs('a ""') == ["a", ""]


def test_should_handle_arguments_with_special_characters():
    assert parseCommandArgs("$100 @user #tag") == ["$100", "@user", "#tag"]


def test_parse_command_args_should_handle_unicode_characters():
    assert parseCommandArgs("日本語 🎉 café") == ["日本語", "🎉", "café"]


def test_should_handle_newlines_in_quoted_arguments():
    assert parseCommandArgs('"line1\nline2" second') == ["line1\nline2", "second"]


def test_should_treat_unquoted_newlines_as_separators():
    assert parseCommandArgs("label-2\n\nHere is some description #2.") == [
        "label-2",
        "Here",
        "is",
        "some",
        "description",
        "#2.",
    ]


def test_should_collapse_mixed_unquoted_whitespace():
    assert parseCommandArgs("a\n\n\tb  c") == ["a", "b", "c"]


def test_should_handle_escaped_quotes_inside_quoted_strings():
    # Note: This implementation doesn't handle escaped quotes - backslash is literal
    assert parseCommandArgs('"quoted \\"text\\""') == ["quoted \\text\\"]


def test_should_handle_trailing_spaces():
    assert parseCommandArgs("a b c   ") == ["a", "b", "c"]


def test_should_handle_leading_spaces():
    assert parseCommandArgs("   a b c") == ["a", "b", "c"]


# ============================================================================
# Integration
# ============================================================================


def argTestTemplate(content: str) -> PromptTemplate:
    return PromptTemplate(
        name="arg-test",
        description="test",
        content=content,
        sourceInfo=SourceInfo(path="/tmp/arg-test.md", source="local", scope="temporary", origin="top-level"),
        filePath="/tmp/arg-test.md",
    )


# #10867
def test_should_use_positional_default_for_empty_quoted_argument_without_shifting_later_args():
    template = PromptTemplate(
        name="review",
        description="test",
        content="Review $2. Focus: ${1:-correctness}.",
        sourceInfo=SourceInfo(path="/tmp/review.md", source="local", scope="temporary", origin="top-level"),
        filePath="/tmp/review.md",
    )

    assert expandPromptTemplate('/review "" src/index.ts', [template]) == "Review src/index.ts. Focus: correctness."
    assert expandPromptTemplate("/review '' src/index.ts", [template]) == "Review src/index.ts. Focus: correctness."


def test_should_split_template_arguments_on_unquoted_newlines():
    result = expandPromptTemplate(
        "/arg-test label-2\n\nHere is some description #2.", [argTestTemplate("- arg1: $1\n- rest: ${@:2}")]
    )

    assert result == "- arg1: label-2\n- rest: Here is some description #2."


def test_should_support_template_command_separated_from_args_by_newline():
    result = expandPromptTemplate("/arg-test\nlabel-2", [argTestTemplate("arg1: $1")])

    assert result == "arg1: label-2"


# ============================================================================
# Integration
# ============================================================================


def test_should_parse_and_substitute_together_correctly():
    input = 'Button "onClick handler" "disabled support"'
    args = parseCommandArgs(input)
    template = "Create component $1 with features: $ARGUMENTS"
    result = substituteArgs(template, args)
    assert result == "Create component Button with features: Button onClick handler disabled support"


def test_should_handle_the_example_from_readme():
    input = 'Button "onClick handler" "disabled support"'
    args = parseCommandArgs(input)
    template = "Create a React component named $1 with features: $ARGUMENTS"
    result = substituteArgs(template, args)
    assert result == "Create a React component named Button with features: Button onClick handler disabled support"


def test_should_produce_same_result_with_at_and_arguments():
    args = parseCommandArgs("feature1 feature2 feature3")
    template1 = "Implement: $@"
    template2 = "Implement: $ARGUMENTS"
    assert substituteArgs(template1, args) == substituteArgs(template2, args)


# ============================================================================
# loadPromptTemplates - argument-hint frontmatter
# ============================================================================


@pytest.fixture
def testDir(tmp_path: Path) -> Path:
    return tmp_path / "pi-test-prompts"


def writeTemplate(testDir: Path, name: str, content: str) -> None:
    testDir.mkdir(parents=True, exist_ok=True)
    (testDir / f"{name}.md").write_text(content, encoding="utf-8")


def loadFrom(testDir: Path) -> list[PromptTemplate]:
    return loadPromptTemplates({"cwd": os.getcwd(), "agentDir": get_agent_dir(), "promptPaths": [str(testDir)], "includeDefaults": False}).templates


def test_should_parse_required_argument_hint_from_frontmatter(testDir: Path):
    writeTemplate(
        testDir,
        "pr",
        """---
description: Review PRs from URLs with structured issue and code analysis
argument-hint: "<PR-URL>"
---
You are given one or more GitHub PR URLs: $@""",
    )

    pr = next((t for t in loadFrom(testDir) if t.name == "pr"), None)
    assert pr is not None
    assert pr.argumentHint == "<PR-URL>"
    assert pr.description == "Review PRs from URLs with structured issue and code analysis"


def test_should_parse_optional_argument_hint_from_frontmatter(testDir: Path):
    writeTemplate(
        testDir,
        "wr",
        """---
description: Finish the current task end-to-end with changelog, commit, and push
argument-hint: "[instructions]"
---
Wrap it. Additional instructions: $ARGUMENTS""",
    )

    wr = next((t for t in loadFrom(testDir) if t.name == "wr"), None)
    assert wr is not None
    assert wr.argumentHint == "[instructions]"
    assert wr.description == "Finish the current task end-to-end with changelog, commit, and push"


def test_should_leave_argument_hint_undefined_when_not_specified(testDir: Path):
    writeTemplate(
        testDir,
        "cl",
        """---
description: Audit changelog entries before release
---
Audit changelog entries for all commits since the last release.""",
    )

    cl = next((t for t in loadFrom(testDir) if t.name == "cl"), None)
    assert cl is not None
    assert cl.argumentHint is None


def test_should_ignore_empty_argument_hint(testDir: Path):
    writeTemplate(
        testDir,
        "empty-hint",
        """---
description: A command with empty hint
argument-hint: ""
---
Do something""",
    )

    tmpl = next((t for t in loadFrom(testDir) if t.name == "empty-hint"), None)
    assert tmpl is not None
    assert tmpl.argumentHint is None


def test_should_preserve_argument_hint_with_special_characters(testDir: Path):
    writeTemplate(
        testDir,
        "is",
        """---
description: Analyze GitHub issues (bugs or feature requests)
argument-hint: "<issue>"
---
Analyze GitHub issue(s): $ARGUMENTS""",
    )

    is_ = next((t for t in loadFrom(testDir) if t.name == "is"), None)
    assert is_ is not None
    assert is_.argumentHint == "<issue>"


# ============================================================================
# loadPromptTemplates - diagnostics
# ============================================================================


# Regression test for #9354.
def test_reports_invalid_yaml_frontmatter_and_keeps_valid_siblings(tmp_path: Path):
    testDir = tmp_path / "pi-test-prompts-invalid"
    testDir.mkdir()
    invalidPromptPath = testDir / "invalid.md"
    invalidPromptPath.write_text("---\ndescription: Broken: unquoted colon\n---\nDo something.\n", encoding="utf-8")
    (testDir / "valid.md").write_text("Valid prompt content.", encoding="utf-8")

    result = loadPromptTemplates({"cwd": os.getcwd(), "agentDir": get_agent_dir(), "promptPaths": [str(testDir)], "includeDefaults": False})

    assert [template.name for template in result.templates] == ["valid"]
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.type == "warning"
    assert diagnostic.path == str(invalidPromptPath)
    # MISAKA fork: the YAML parser is ruamel's, whose messages are not the `yaml` package's ("line 1, column 14").
    assert diagnostic.message
