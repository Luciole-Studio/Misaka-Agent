"""MISAKA: the gitignore matcher of the Python daemon's ``listFiles`` (``misaka/env/daemon/list.py``), which stands in
for the ``ignore`` crate upstream's ``list.rs`` walks with. The cases follow the crate's parsing of rules (its
``gitignore.rs`` and ``globset``; where it departs from git's ``gitignore(5)``, as with POSIX classes, the crate's reading
is pinned) and its order of precedence; ``test_env_daemon_gitignore_fx6.py`` holds the cases where the two differ."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from misaka.env.daemon.list import Matcher, Options, list_files, parse_rule


def matches(pattern: str, path: str, is_dir: bool = False) -> bool | None:
    rule = parse_rule(pattern)
    assert rule is not None
    return Matcher([rule]).matched(path, is_dir)


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        # A pattern without a slash matches at any depth, by name.
        ("*.log", "a.log", False, True),
        ("*.log", "deep/dir/a.log", False, True),
        ("*.log", "a.log.txt", False, None),
        # A slash at the start or in the middle anchors it to the file's directory.
        ("/out", "out", True, True),
        ("/out", "sub/out", True, None),
        ("doc/frotz", "doc/frotz", False, True),
        ("doc/frotz", "a/doc/frotz", False, None),
        # A trailing slash matches directories only.
        ("build/", "build", True, True),
        ("build/", "build", False, None),
        # `*` and `?` stay within one component; `**` spans components where it is one.
        ("a/*/c", "a/b/c", False, True),
        ("a/*/c", "a/b/x/c", False, None),
        ("a/**/c", "a/c", False, True),
        ("a/**/c", "a/b/x/c", False, True),
        ("**/foo", "x/y/foo", False, True),
        ("abc/**", "abc/x/y", False, True),
        ("a?c", "abc", False, True),
        ("a?c", "a/c", False, None),
        ("a**c", "abxc", False, True),
        # Bracket expressions and negated ones; globset has no POSIX classes: `[[:digit:]]x` is the class `[:digt`
        # followed by `]x`.
        ("[ab].txt", "b.txt", False, True),
        ("[!ab].txt", "c.txt", False, True),
        ("[!ab].txt", "a.txt", False, None),
        ("[a-c]x", "bx", False, True),
        ("[[:digit:]]x", "7x", False, None),
        ("[[:digit:]]x", "d]x", False, True),
        # Escapes, negation, and comments.
        ("\\#hash", "#hash", False, True),
        ("\\!bang", "!bang", False, True),
        ("!keep", "keep", False, False),
        ("trailing\\ ", "trailing ", False, True),
        ("trailing   ", "trailing", False, True),
    ],
)
def test_patterns_follow_gitignore_5(pattern: str, path: str, is_dir: bool, expected: bool | None) -> None:
    assert matches(pattern, path, is_dir) is expected


def test_blank_lines_and_comments_are_no_rules() -> None:
    assert parse_rule("") is None
    assert parse_rule("   ") is None
    assert parse_rule("# comment") is None


def test_the_last_matching_rule_of_a_file_decides() -> None:
    rules = [rule for rule in (parse_rule(line) for line in ["*.txt", "!keep.txt"]) if rule is not None]
    matcher = Matcher(rules)
    assert matcher.matched("drop.txt", False) is True
    assert matcher.matched("keep.txt", False) is False


def write(root: Path, path: str, content: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def listing(root: Path, **options: object) -> list[str]:
    listed = list_files(str(root), Options(**options), lambda: False)  # type: ignore[arg-type]
    return [f"{chr(record[0])} {record[1:].decode()}" for record in bytes(listed.payload).split(b"\0") if record]


def test_a_deeper_gitignore_overrides_info_exclude_and_an_ignored_directory_is_not_entered(tmp_path: Path) -> None:
    write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    write(tmp_path, ".git/info/exclude", "*.tmp\n")
    write(tmp_path, ".gitignore", "ignored/\n")
    write(tmp_path, "ignored/inner/.gitignore", "!*\n")
    write(tmp_path, "ignored/inner/file.txt")
    write(tmp_path, "keep/a.tmp")
    write(tmp_path, "keep/.gitignore", "!a.tmp\n")
    write(tmp_path, "drop.tmp")
    assert listing(tmp_path) == ["f .gitignore", "d keep", "f keep/.gitignore", "f keep/a.tmp"]


def test_rules_apply_only_inside_a_work_tree_and_a_git_file_marks_one(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", "*.log\n")
    write(tmp_path, "a.log")
    assert listing(tmp_path) == ["f .gitignore", "f a.log"]
    # A worktree's `.git` file points at its git directory, whose `commondir` holds the `info/exclude` that applies.
    write(tmp_path, "gitdir/info/exclude", "*.txt\n")
    write(tmp_path, "gitdir/worktrees/w/commondir", "../..\n")
    write(tmp_path, ".git", f"gitdir: {tmp_path / 'gitdir' / 'worktrees' / 'w'}\n")
    write(tmp_path, "b.txt")
    assert listing(tmp_path) == [
        "f .gitignore",
        "d gitdir",
        "d gitdir/info",
        "f gitdir/info/exclude",
        "d gitdir/worktrees",
        "d gitdir/worktrees/w",
        "f gitdir/worktrees/w/commondir",
    ]


def test_the_global_excludes_file_has_the_lowest_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    write(config, "git/ignore", "*.bak\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-config"))
    tree = tmp_path / "tree"
    write(tree, ".git/HEAD", "ref: refs/heads/main\n")
    write(tree, "a.bak")
    write(tree, "b.bak")
    write(tree, ".gitignore", "!b.bak\n")
    assert listing(tree) == ["f .gitignore", "f b.bak"]


@pytest.mark.skipif(os.name == "nt", reason="file names that are not UTF-8 are a POSIX case")
def test_names_that_are_not_utf8_get_replacement_characters(tmp_path: Path) -> None:
    try:
        os.close(os.open(os.path.join(os.fsencode(tmp_path), b"bad\xff"), os.O_CREAT | os.O_WRONLY))
    except OSError:
        pytest.skip("the file system takes only UTF-8 names (APFS)")
    assert listing(tmp_path) == ["f bad�"]
