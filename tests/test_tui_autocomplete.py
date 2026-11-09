"""Translated from pi-tui's ``test/autocomplete.test.ts`` (v1.1.0).

MISAKA fork: offsets count code points (Python strings), so a ``cursorCol`` built from ``before.length`` is
``len(before)`` here. The fd suite runs only where ``fd`` is on PATH, as upstream's does.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from misaka.ui.tui.autocomplete import CombinedAutocompleteProvider
from misaka.utils.js import AbortController


def resolveFdPath() -> str | None:
    return shutil.which("fd")


def setupFolder(baseDir: str, structure: dict[str, Any] | None = None) -> None:
    structure = structure or {}
    dirs = structure.get("dirs", [])
    files = structure.get("files", {})

    for dir in dirs:
        os.makedirs(os.path.join(baseDir, dir), exist_ok=True)
    for filePath, contents in files.items():
        fullPath = os.path.join(baseDir, filePath)
        os.makedirs(os.path.dirname(fullPath), exist_ok=True)
        Path(fullPath).write_text(contents, encoding="utf-8")


fdPath = resolveFdPath()
isFdInstalled = bool(fdPath)


def requireFdPath() -> str:
    if not fdPath:
        raise RuntimeError("fd is not available")
    return fdPath


async def getSuggestions(
    provider: CombinedAutocompleteProvider,
    lines: list[str],
    cursorLine: int,
    cursorCol: int,
    force: bool = False,
) -> Any:
    return await provider.getSuggestions(lines, cursorLine, cursorCol, {"signal": AbortController().signal, "force": force})


# --- extractPathPrefix ----------------------------------------------------------------------------


async def test_extracts_slash_from_hey_slash_when_forced():
    provider = CombinedAutocompleteProvider([], "/tmp")
    lines = ["hey /"]
    cursorLine = 0
    cursorCol = 5  # After the "/"

    result = await getSuggestions(provider, lines, cursorLine, cursorCol, True)

    assert result is not None, "Should return suggestions for root directory"
    if result:
        assert result.prefix == "/", "Prefix should be '/'"


async def test_extracts_slash_a_from_slash_a_when_forced():
    provider = CombinedAutocompleteProvider([], "/tmp")
    lines = ["/A"]
    cursorLine = 0
    cursorCol = 2  # After the "A"

    result = await getSuggestions(provider, lines, cursorLine, cursorCol, True)

    # This might return null if /A doesn't match anything, which is fine
    # We're mainly testing that the prefix extraction works
    if result:
        assert result.prefix == "/A", "Prefix should be '/A'"


async def test_does_not_trigger_for_slash_commands():
    provider = CombinedAutocompleteProvider([], "/tmp")
    lines = ["/model"]
    cursorLine = 0
    cursorCol = 6  # After "model"

    result = await getSuggestions(provider, lines, cursorLine, cursorCol, True)

    assert result is None, "Should not trigger for slash commands"


async def test_triggers_for_absolute_paths_after_slash_command_argument():
    provider = CombinedAutocompleteProvider([], "/tmp")
    lines = ["/command /"]
    cursorLine = 0
    cursorCol = 10  # After the second "/"

    result = await getSuggestions(provider, lines, cursorLine, cursorCol, True)

    assert result is not None, "Should trigger for absolute paths in command arguments"
    if result:
        assert result.prefix == "/", "Prefix should be '/'"


# --- fd @ file suggestions ------------------------------------------------------------------------


@pytest.fixture
def fdDirs() -> Iterator[tuple[str, str, str]]:
    rootDir = tempfile.mkdtemp(prefix="pi-autocomplete-root-")
    baseDir = os.path.join(rootDir, "cwd")
    outsideDir = os.path.join(rootDir, "outside")
    os.makedirs(baseDir, exist_ok=True)
    os.makedirs(outsideDir, exist_ok=True)
    yield rootDir, baseDir, outsideDir
    shutil.rmtree(rootDir, ignore_errors=True)


fd = pytest.mark.skipif(not isFdInstalled, reason="fd is not installed")


@fd
async def test_returns_all_files_and_folders_for_empty_at_query(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"dirs": ["src"], "files": {"README.md": "readme"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = sorted(item.value for item in result.items) if result else None
    assert values == sorted(["@README.md", "@src/"])


@fd
async def test_recognizes_at_after_cjk_punctuation_without_consuming_the_preceding_text(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"README.md": "readme"}})
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for before in ["查看，", "　", *"，．：；！？（）［］｛｝“”‘’…—。、「」『』《》【】"]:
        for force in [False, True]:
            line = f"{before}@REA"
            result = await getSuggestions(provider, [line], 0, len(line), force)
            assert result, line
            assert result.prefix == "@REA"
            assert [item.value for item in result.items] == ["@README.md"]
            applied = provider.applyCompletion([line], 0, len(line), result.items[0], result.prefix)
            assert applied["lines"][0] == f"{before}@README.md "
            assert applied["cursorCol"] == len(applied["lines"][0])


@fd
async def test_recognizes_at_after_opening_wrappers_like_paren_and_backticks(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"README.md": "readme"}})
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for before in ["(", "see (", "[", "`", "<", "{"]:
        line = f"{before}@REA"
        result = await getSuggestions(provider, [line], 0, len(line))
        assert result, line
        assert result.prefix == "@REA"
        applied = provider.applyCompletion([line], 0, len(line), result.items[0], result.prefix)
        assert applied["lines"][0] == f"{before}@README.md "
    embedded = "foo(@REA"
    assert await getSuggestions(provider, [embedded], 0, len(embedded)) is None


@fd
async def test_preserves_cjk_characters_and_embedded_at_in_attachment_paths(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"文档/说明.md": "text", "文档@备份/说明.md": "backup"}})
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for before in ["", "查看，"]:
        for directory in ["文档", "文档@备份"]:
            prefix = f"@{directory}/说"
            line = before + prefix
            result = await getSuggestions(provider, [line], 0, len(line))
            assert result, line
            assert result.prefix == prefix
            assert [item.value for item in result.items] == [f"@{directory}/说明.md"]


@fd
async def test_completes_quoted_cjk_attachments_after_prose_without_losing_path_segments_or_quotes(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for separator in [" ", "　", "，", "。"]:
        directory = f"我的{separator}文档"
        setupFolder(baseDir, {"files": {f"{directory}/说明.md": "text", "文档/说明.md": "not the quoted path"}})
        line = f'查看：@"{directory}/说"后文'
        cursorCol = line.find('"后文')
        result = await getSuggestions(provider, [line], 0, cursorCol)
        assert result
        assert result.prefix == f'@"{directory}/说'
        assert [item.value for item in result.items] == [f'@"{directory}/说明.md"']
        applied = provider.applyCompletion([line], 0, cursorCol, result.items[0], result.prefix)
        assert applied["lines"][0] == f'查看：@"{directory}/说明.md" 后文'
        assert applied["cursorCol"] == len(f'查看：@"{directory}/说明.md" ')


@fd
async def test_does_not_interpret_email_addresses_or_at_after_ascii_or_cjk_letters_as_attachment_prefixes(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"README.md": "readme", "example.com": "text"}})
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for before in ["user", "查看", "あ", "カ", "한", "ㄅ", "𠮷", "が", "禰\U000e0100", "々", "Ａ"]:
        for name in ["REA", "example.com"]:
            line = f"{before}@{name}"
            assert await getSuggestions(provider, [line], 0, len(line)) is None, line


@fd
async def test_matches_file_with_extension_in_query(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"file.txt": "content"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@file.txt"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@file.txt" in values


@fd
async def test_filters_are_case_insensitive(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"dirs": ["src"], "files": {"README.md": "readme"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@re"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = sorted(item.value for item in result.items) if result else None
    assert values == ["@README.md"]


@fd
async def test_ranks_directories_before_files(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"dirs": ["src"], "files": {"src.txt": "text"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@src"
    result = await getSuggestions(provider, [line], 0, len(line))

    firstValue = result.items[0].value if result and result.items else None
    hasSrcFile = any(item.value == "@src.txt" for item in result.items) if result else False
    assert firstValue == "@src/"
    assert hasSrcFile


@fd
async def test_returns_nested_file_paths(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"src/index.ts": "export {};\n"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@index"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@src/index.ts" in values


@fd
async def test_matches_deeply_nested_paths(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(
        baseDir,
        {"files": {"packages/tui/src/autocomplete.ts": "export {};", "packages/ai/src/autocomplete.ts": "export {};"}},
    )

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@tui/src/auto"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@packages/tui/src/autocomplete.ts" in values
    assert "@packages/ai/src/autocomplete.ts" not in values


@fd
async def test_matches_directory_in_middle_of_path_with_full_path(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(
        baseDir, {"files": {"src/components/Button.tsx": "export {};", "src/utils/helpers.ts": "export {};"}}
    )

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@components/"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@src/components/Button.tsx" in values
    assert "@src/utils/helpers.ts" not in values


@fd
async def test_scopes_fuzzy_search_to_relative_directories_and_searches_recursively(fdDirs):
    _rootDir, baseDir, outsideDir = fdDirs
    setupFolder(
        outsideDir,
        {
            "files": {
                "nested/alpha.ts": "export {};",
                "nested/deeper/also-alpha.ts": "export {};",
                "nested/deeper/zzz.ts": "export {};",
            }
        },
    )

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@../outside/a"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@../outside/nested/alpha.ts" in values
    assert "@../outside/nested/deeper/also-alpha.ts" in values
    assert "@../outside/nested/deeper/zzz.ts" not in values


@fd
async def test_ranks_shallower_same_score_at_matches_before_deeper_matches(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"dirs": ["scope/aaa/venv/lib/python3.12/site-packages/pkg/core/profile", "scope/projects"]})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@scope/pro"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert values[0] == "@scope/projects/"
    assert "@scope/aaa/venv/lib/python3.12/site-packages/pkg/core/profile/" in values


@fd
async def test_includes_scoped_direct_children_when_recursive_at_matches_are_flooded(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    floodedDirs = [
        f"scope/a{str(index + 1).zfill(3)}/venv/lib/python3.12/site-packages/pkg/core/profile" for index in range(250)
    ]
    setupFolder(baseDir, {"dirs": ["scope/projects", *floodedDirs]})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@scope/pro"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert values[0] == "@scope/projects/"
    assert any("/profile/" in value for value in values), "Should keep deep fuzzy matches after direct children"


@fd
async def test_quotes_paths_containing_whitespace_or_cjk_punctuation_for_at_suggestions(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    for separator in [" ", "　", "，", "。"]:
        directory = f"my{separator}folder"
        setupFolder(baseDir, {"files": {f"{directory}/test.txt": "content"}})
        line = "@my"
        result = await getSuggestions(provider, [line], 0, len(line))
        assert result
        item = next((entry for entry in result.items if entry.value == f'@"{directory}/"'), None)
        assert item, directory
        applied = provider.applyCompletion([line], 0, len(line), item, result.prefix)
        continued = await getSuggestions(provider, applied["lines"], 0, applied["cursorCol"])
        assert (continued.prefix if continued else None) == f'@"{directory}/'
        assert continued is not None and any(entry.value == f'@"{directory}/test.txt"' for entry in continued.items)


@fd
async def test_includes_hidden_paths_but_excludes_git(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(
        baseDir,
        {
            "dirs": [".pi", ".github", ".git"],
            "files": {".pi/config.json": "{}", ".github/workflows/ci.yml": "name: ci", ".git/config": "[core]"},
        },
    )

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@.pi/" in values
    assert "@.github/" in values
    assert not any(value == "@.git" or value.startswith("@.git/") for value in values)


@fd
async def test_follows_symlinked_directories_for_fuzzy_at_search(fdDirs):
    _rootDir, baseDir, outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"dir/some_file.txt": "real"}})
    setupFolder(outsideDir, {"files": {"some_file.txt": "symlinked"}})
    os.symlink("../outside", os.path.join(baseDir, "symlinked_dir"), target_is_directory=True)

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@some"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@dir/some_file.txt" in values
    assert "@symlinked_dir/some_file.txt" in values


@fd
async def test_returns_symlinked_directories_when_matching_their_name(fdDirs):
    _rootDir, baseDir, outsideDir = fdDirs
    setupFolder(outsideDir, {"files": {"nested/file.txt": "symlinked"}})
    os.symlink("../outside", os.path.join(baseDir, "symlinked_dir"), target_is_directory=True)

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@symlinked"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@symlinked_dir/" in values


@fd
async def test_returns_symlinked_files_without_requiring_type_l(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"original.txt": "content"}})
    linkPath = os.path.join(baseDir, "link.txt")
    os.symlink("original.txt", linkPath)

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = "@link"
    result = await getSuggestions(provider, [line], 0, len(line))

    values = [item.value for item in result.items] if result else []
    assert "@link.txt" in values


@fd
async def test_returns_the_same_at_suggestions_when_the_cwd_path_contains_the_query(fdDirs):
    rootDir, _baseDir, _outsideDir = fdDirs
    normalBaseDir = os.path.join(rootDir, "cwd-normal")
    queryInPathBaseDir = os.path.join(rootDir, "cwd-plan-repro")
    os.makedirs(normalBaseDir, exist_ok=True)
    os.makedirs(queryInPathBaseDir, exist_ok=True)

    structure = {
        "dirs": ["packages/coding-agent/examples/extensions/plan-mode"],
        "files": {
            "packages/coding-agent/examples/extensions/plan-mode/README.md": "readme",
            "packages/tui/docs/plan.md": "plan",
        },
    }
    setupFolder(normalBaseDir, structure)
    setupFolder(queryInPathBaseDir, structure)

    query = "@plan"
    normalProvider = CombinedAutocompleteProvider([], normalBaseDir, requireFdPath())
    queryInPathProvider = CombinedAutocompleteProvider([], queryInPathBaseDir, requireFdPath())

    normalResult = await getSuggestions(normalProvider, [query], 0, len(query))
    queryInPathResult = await getSuggestions(queryInPathProvider, [query], 0, len(query))

    def normalize(result: Any) -> list[str]:
        return sorted(f"{item.label} :: {item.description or ''}" for item in (result.items if result else []))

    assert normalize(queryInPathResult) == normalize(normalResult)
    assert "plan-mode/ :: packages/coding-agent/examples/extensions/plan-mode" in normalize(normalResult)
    assert "plan.md :: packages/tui/docs/plan.md" in normalize(normalResult)


@fd
async def test_continues_autocomplete_inside_quoted_at_paths(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"my folder/test.txt": "content", "my folder/other.txt": "content"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = '@"my folder/"'
    result = await getSuggestions(provider, [line], 0, len(line) - 1)

    assert result is not None, "Should return suggestions for quoted folder path"
    values = [item.value for item in result.items]
    assert '@"my folder/test.txt"' in values
    assert '@"my folder/other.txt"' in values


@fd
async def test_applies_quoted_at_completion_without_duplicating_closing_quote(fdDirs):
    _rootDir, baseDir, _outsideDir = fdDirs
    setupFolder(baseDir, {"files": {"my folder/test.txt": "content"}})

    provider = CombinedAutocompleteProvider([], baseDir, requireFdPath())
    line = '@"my folder/te"'
    cursorCol = len(line) - 1
    result = await getSuggestions(provider, [line], 0, cursorCol)

    assert result is not None, "Should return suggestions for quoted @ path"
    item = next((entry for entry in result.items if entry.value == '@"my folder/test.txt"'), None)
    assert item, "Should find test.txt suggestion"

    applied = provider.applyCompletion([line], 0, cursorCol, item, result.prefix)
    assert applied["lines"][0] == '@"my folder/test.txt" '


# --- dot-slash path completion --------------------------------------------------------------------


@pytest.fixture
def baseDir() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-autocomplete-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


async def test_completes_chinese_path_prefixes_after_whitespace_or_cjk_punctuation_on_tab(baseDir):
    setupFolder(baseDir, {"files": {"说明.md": "file", "文档/说明.md": "nested file"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    completions = [
        {"prefix": "说", "value": "说明.md"},
        {"prefix": "文", "value": "文档/"},
        {"prefix": "文档/说", "value": "文档/说明.md"},
        {"prefix": "./文档/说", "value": "./文档/说明.md"},
    ]
    if sys.platform != "win32":
        completions.append({"prefix": f"{baseDir}/文档/说", "value": f"{baseDir}/文档/说明.md"})
    for separator in " \t　 ，：；。！？（「《":
        for completion in completions:
            prefix, value = completion["prefix"], completion["value"]
            before = f"查看𠮷{separator}"
            line = f"{before}{prefix} 后文"
            cursorCol = len(before) + len(prefix)
            result = await getSuggestions(provider, [line], 0, cursorCol, True)
            assert result, line
            assert result.prefix == prefix
            assert [item.value for item in result.items] == [value]
            applied = provider.applyCompletion([line], 0, cursorCol, result.items[0], result.prefix)
            assert applied["lines"][0] == f"{before}{value} 后文"
            assert applied["cursorCol"] == len(before) + len(value)


async def test_treats_unquoted_separators_as_boundaries_even_when_a_matching_literal_path_exists(baseDir):
    setupFolder(baseDir, {"files": {"归档/说明.md": "other"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    for separator in [" ", "　", "，", "。"]:
        directory = f"资料{separator}归档"
        setupFolder(baseDir, {"files": {f"{directory}/说明.md": "archive"}})
        for marker in ["", "@"]:
            line = f"{marker}{directory}/说"
            result = await getSuggestions(provider, [line], 0, len(line), True)
            assert result, line
            assert result.prefix == "归档/说"
            assert [item.value for item in result.items] == ["归档/说明.md"]
        quoted = f'查看，"{directory}/说"后文'
        cursorCol = quoted.find('"后文')
        result = await getSuggestions(provider, [quoted], 0, cursorCol, True)
        assert result
        assert result.prefix == f'"{directory}/说'
        assert [item.value for item in result.items] == [f'"{directory}/说明.md"']
        applied = provider.applyCompletion([quoted], 0, cursorCol, result.items[0], result.prefix)
        assert applied["lines"][0] == f'查看，"{directory}/说明.md"后文'
        missing = f'查看，"不存在{separator}归档/说'
        assert await getSuggestions(provider, [missing], 0, len(missing), True) is None


async def test_handles_an_empty_prefix_after_whitespace_or_cjk_punctuation_consistently(baseDir):
    setupFolder(baseDir, {"files": {"说明.md": "text"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    for separator in [" ", "\t", "　", "，", "。"]:
        for force in [False, True]:
            line = f"查看{separator}"
            result = await getSuggestions(provider, [line], 0, len(line), force)
            assert result, line
            assert result.prefix == ""
            assert [item.value for item in result.items] == ["说明.md"]
    assert await getSuggestions(provider, [""], 0, 0) is None


async def test_completes_paths_after_opening_wrappers_like_paren_bracket_brace_angle_and_backticks(baseDir):
    setupFolder(baseDir, {"files": {"src/main.ts": "x"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    for wrapper in ["(", "[", "{", "<", "`", "((", "(`"]:
        for prefix in ["src/ma", "./src/ma"]:
            before = f"see {wrapper}"
            line = f"{before}{prefix}"
            result = await getSuggestions(provider, [line], 0, len(line), True)
            assert result, line
            assert result.prefix == prefix
            value = prefix.replace("src/ma", "src/main.ts", 1)
            assert [item.value for item in result.items] == [value]
            applied = provider.applyCompletion([line], 0, len(line), result.items[0], result.prefix)
            assert applied["lines"][0] == f"{before}{value}"


async def test_completes_quoted_paths_after_opening_wrappers(baseDir):
    setupFolder(baseDir, {"files": {"my dir/main.ts": "x"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    line = 'see ("my dir/ma'
    result = await getSuggestions(provider, [line], 0, len(line), True)
    assert result
    assert result.prefix == '"my dir/ma'
    assert [item.value for item in result.items] == ['"my dir/main.ts"']


async def test_keeps_wrappers_that_are_closed_inside_the_path(baseDir):
    setupFolder(baseDir, {"files": {"[slug]/page.tsx": "x", "(group)/layout.tsx": "x"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    for prefix, value in [
        ("[slug]/pa", "[slug]/page.tsx"),
        ("(group)/la", "(group)/layout.tsx"),
        ("./[slug]/pa", "./[slug]/page.tsx"),
    ]:
        line = f"see {prefix}"
        result = await getSuggestions(provider, [line], 0, len(line), True)
        assert result, line
        assert result.prefix == prefix
        assert [item.value for item in result.items] == [value]


async def test_preserves_cjk_characters_in_unprefixed_tab_completions(baseDir):
    setupFolder(baseDir, {"files": {"文档/说明.md": "text"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    line = "文档/说"
    result = await getSuggestions(provider, [line], 0, len(line), True)
    assert result
    assert result.prefix == line
    assert [item.value for item in result.items] == ["文档/说明.md"]


async def test_preserves_dot_slash_prefix_when_completing_paths(baseDir):
    setupFolder(baseDir, {"files": {"update.sh": "#!/bin/bash", "utils.ts": "export {};"}})

    provider = CombinedAutocompleteProvider([], baseDir)
    line = "./up"
    result = await getSuggestions(provider, [line], 0, len(line), True)

    assert result is not None, "Should return suggestions for ./ path"
    values = [item.value for item in result.items]
    assert "./update.sh" in values, f"Expected ./update.sh in {values}"


async def test_preserves_dot_slash_prefix_for_directory_completions(baseDir):
    setupFolder(baseDir, {"dirs": ["src"], "files": {"src/index.ts": "export {};"}})

    provider = CombinedAutocompleteProvider([], baseDir)
    line = "./sr"
    result = await getSuggestions(provider, [line], 0, len(line), True)

    assert result is not None, "Should return suggestions for ./ directory path"
    values = [item.value for item in result.items]
    assert "./src/" in values, f"Expected ./src/ in {values}"


# --- quoted path completion -----------------------------------------------------------------------


async def test_quotes_paths_containing_whitespace_or_cjk_punctuation_for_direct_completion(baseDir):
    provider = CombinedAutocompleteProvider([], baseDir)
    for separator in [" ", "　", "，", "。"]:
        directory = f"my{separator}folder"
        setupFolder(baseDir, {"files": {f"{directory}/test.txt": "content"}})
        line = "my"
        result = await getSuggestions(provider, [line], 0, len(line), True)
        assert result
        item = next((entry for entry in result.items if entry.value == f'"{directory}/"'), None)
        assert item, directory
        applied = provider.applyCompletion([line], 0, len(line), item, result.prefix)
        continued = await getSuggestions(provider, applied["lines"], 0, applied["cursorCol"], True)
        assert (continued.prefix if continued else None) == f'"{directory}/'
        assert [entry.value for entry in continued.items] == [f'"{directory}/test.txt"']


async def test_keeps_quoted_directories_before_files(baseDir):
    setupFolder(baseDir, {"dirs": ["z folder", "z，folder"], "files": {"a.txt": "text"}})
    provider = CombinedAutocompleteProvider([], baseDir)
    result = await getSuggestions(provider, [""], 0, 0, True)
    assert result
    assert [item.label.endswith("/") for item in result.items] == [True, True, False]


async def test_continues_completion_inside_quoted_paths(baseDir):
    setupFolder(baseDir, {"files": {"my folder/test.txt": "content", "my folder/other.txt": "content"}})

    provider = CombinedAutocompleteProvider([], baseDir)
    line = '"my folder/"'
    result = await getSuggestions(provider, [line], 0, len(line) - 1, True)

    assert result is not None, "Should return suggestions for quoted folder path"
    values = [item.value for item in result.items]
    assert '"my folder/test.txt"' in values
    assert '"my folder/other.txt"' in values


async def test_applies_quoted_completion_without_duplicating_closing_quote(baseDir):
    setupFolder(baseDir, {"files": {"my folder/test.txt": "content"}})

    provider = CombinedAutocompleteProvider([], baseDir)
    line = '"my folder/te"'
    cursorCol = len(line) - 1
    result = await getSuggestions(provider, [line], 0, cursorCol, True)

    assert result is not None, "Should return suggestions for quoted path"
    item = next((entry for entry in result.items if entry.value == '"my folder/test.txt"'), None)
    assert item, "Should find test.txt suggestion"

    applied = provider.applyCompletion([line], 0, cursorCol, item, result.prefix)
    assert applied["lines"][0] == '"my folder/test.txt"'


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive paths")
def test_a_drive_path_completes_from_its_own_root(tmp_path):
    (tmp_path / "notes.md").write_text("", encoding="utf-8")
    provider = CombinedAutocompleteProvider([], str(Path(tempfile.gettempdir())))
    prefix = str(tmp_path).replace("\\", "/") + "/no"
    values = [item["value"] if isinstance(item, dict) else item.value for item in provider.getFileSuggestions(prefix)]
    assert any(value.endswith("notes.md") for value in values)
