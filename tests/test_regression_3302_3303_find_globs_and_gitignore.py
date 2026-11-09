"""Translated from pi's ``packages/coding-agent/test/suite/regressions/3302-find-path-glob.test.ts`` and
``3303-find-nested-gitignore.test.ts`` (v1.1.0).

MISAKA fork: the find tool runs fd, which must be on the PATH (the managed copy lives under the real home, which the
tests redirect); without it these cases are skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from misaka.core.tools.find import createFindToolDefinition
from misaka.utils.tools_manager import find_tool
from misaka.utils.values import read_field

pytestmark = pytest.mark.skipif(find_tool("fd") is None, reason="fd is not on the PATH")


async def runFind(root: Path, pattern: str, *, sort: bool = False) -> list[str]:
    definition = createFindToolDefinition(str(root))
    # The find tool implementation does not touch ctx; pass a minimal stub.
    result = await definition.execute("call-1", {"pattern": pattern}, None, None, None)
    text = read_field(read_field(result, "content")[0], "text") or ""
    if text == "No files found matching pattern":
        return []
    files = [line.strip() for line in text.split("\n") if line.strip() and not line.strip().startswith("[")]
    return sorted(files) if sort else files


def _touch(root: Path, *paths: str) -> None:
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")


# --- issue #3302 find returns no results for path-based glob patterns ---------------------------


@pytest.fixture
def specTree(tmp_path: Path) -> Path:
    _touch(tmp_path, "some/parent/child/file.ext", "some/parent/child/test.spec.ts", "src/foo/bar/example.spec.ts")
    return tmp_path


async def test_basename_pattern_still_matches(specTree: Path):
    files = await runFind(specTree, "*.spec.ts")
    assert sorted(files) == ["some/parent/child/test.spec.ts", "src/foo/bar/example.spec.ts"]


async def test_directory_prefixed_pattern_with_double_star_tail_matches_subtree(specTree: Path):
    files = await runFind(specTree, "some/parent/child/**")
    # Matches files (and possibly directories) under the subtree. Assert the two files are present.
    assert "some/parent/child/file.ext" in files
    assert "some/parent/child/test.spec.ts" in files


async def test_leading_double_star_wildcard_with_path_segments_matches(specTree: Path):
    files = await runFind(specTree, "**/parent/child/*")
    assert "some/parent/child/file.ext" in files
    assert "some/parent/child/test.spec.ts" in files


async def test_src_double_star_spec_ts_matches_nested_spec_file(specTree: Path):
    assert await runFind(specTree, "src/**/*.spec.ts") == ["src/foo/bar/example.spec.ts"]


# --- issue #3303 nested .gitignore rules leak into sibling directories --------------------------


async def test_applies_a_gitignore_only_inside_a_and_leaves_b_untouched(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _touch(tmp_path, "a/ignored.txt", "a/kept.txt", "b/ignored.txt", "b/kept.txt", "root.txt")

    assert await runFind(tmp_path, "**/*.txt", sort=True) == ["a/kept.txt", "b/ignored.txt", "b/kept.txt", "root.txt"]


async def test_scopes_each_gitignore_to_its_own_subtree(tmp_path: Path):
    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / "a" / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "a" / "deep" / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    _touch(
        tmp_path,
        "a/ignored.txt",
        "a/kept.txt",
        "a/deep/ignored.txt",
        "a/deep/secret.txt",
        "a/deep/kept.txt",
        "b/ignored.txt",
        "b/kept.txt",
        "root.txt",
    )

    files = await runFind(tmp_path, "**/*.txt", sort=True)
    # a/.gitignore ignores 'ignored.txt' within a/ and a/deep/.
    # a/deep/.gitignore additionally ignores 'secret.txt' within a/deep/.
    # b/ is untouched by either.
    assert files == ["a/deep/kept.txt", "a/kept.txt", "b/ignored.txt", "b/kept.txt", "root.txt"]

