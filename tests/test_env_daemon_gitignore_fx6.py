"""MISAKA: regressions of the Python daemon's gitignore matcher (``misaka/env/daemon/list.py``) against what upstream's
``ignore`` crate (0.4.33, ``globset`` 0.4.20) does with ``list.rs``'s ``WalkBuilder`` settings, found by the V6
verification (V6-ENV-B01, B02, B05, B06). Every expectation was checked against the v1.3.0 Rust daemon; where git
decides otherwise the crate wins, as upstream lists what the crate lists."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from misaka.env.daemon.list import Matcher, Options, list_files, parse_rule


@pytest.fixture(autouse=True)
def no_global_excludes(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home without git configuration, so the runner's own global excludes file stays out of the listings."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-system-config"))
    return home


def write(root: Path, path: str, content: str | bytes = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def repository(root: Path, path: str = "") -> None:
    write(root, f"{path}/.git/HEAD" if path else ".git/HEAD", "ref: refs/heads/main\n")


def listing(root: Path, **options: object) -> list[str]:
    listed = list_files(str(root), Options(**options), lambda: False)  # type: ignore[arg-type]
    return [f"{chr(record[0])} {record[1:].decode()}" for record in bytes(listed.payload).split(b"\0") if record]


def git(*args: str, cwd: Path, home: Path) -> None:
    environment = {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, env=environment, check=True, capture_output=True)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


# V6-ENV-B01: the git boundary is decided per directory, as the crate's `Ignore` chain does (`has_git`, `saw_git`).


def test_b01_a_directory_outside_any_repository_applies_the_rules_of_the_repositories_it_holds(tmp_path: Path) -> None:
    write(tmp_path, "top.tmp")
    write(tmp_path, ".gitignore", "*.tmp\n")
    repository(tmp_path, "r2")
    write(tmp_path, "r2/.gitignore", "*.tmp\nnode_modules/\n")
    write(tmp_path, "r2/x.tmp")
    write(tmp_path, "r2/k.txt")
    write(tmp_path, "r2/node_modules/m.js")
    # Outside a repository nothing is ignored, not even by the listed directory's own `.gitignore`.
    assert listing(tmp_path) == ["f .gitignore", "d r2", "f r2/.gitignore", "f r2/k.txt", "f top.tmp"]


def test_b01_a_nested_repository_stops_its_parents_rules_and_reads_its_own_info_exclude(tmp_path: Path) -> None:
    repository(tmp_path)
    write(tmp_path, ".gitignore", "*.log\n*.out\n")
    repository(tmp_path, "nested")
    write(tmp_path, "nested/.gitignore", "keep.txt\n")
    write(tmp_path, "nested/.git/info/exclude", "nex.txt\n")
    for name in ("a.log", "n.out", "keep.txt", "nex.txt"):
        write(tmp_path, f"nested/{name}")
    write(tmp_path, "outer.log")
    assert listing(tmp_path) == ["f .gitignore", "d nested", "f nested/.gitignore", "f nested/a.log", "f nested/n.out"]
    # Listed from inside, the outer repository's rules stop at the nested one too.
    assert listing(tmp_path / "nested") == ["f .gitignore", "f a.log", "f n.out"]


def test_b01_a_jj_directory_marks_a_repository_as_dot_git_does(tmp_path: Path) -> None:
    write(tmp_path, ".jj/repo/store/x")
    write(tmp_path, ".gitignore", "*.o\n")
    write(tmp_path, "a.o")
    write(tmp_path, "b.c")
    assert listing(tmp_path, hidden=False) == ["f b.c"]


def test_b01_a_rule_that_whitelists_a_hidden_name_lists_it_without_hidden(tmp_path: Path) -> None:
    # `matched_dir_entry`: hidden names are dropped only when no rule decided.
    repository(tmp_path)
    write(tmp_path, ".gitignore", "!.env\n")
    write(tmp_path, ".env")
    write(tmp_path, ".other")
    assert listing(tmp_path, hidden=False) == ["f .env"]


# V6-ENV-B02: a linked worktree's `.git` file leads to the main repository's `info/exclude` through `commondir`.


@needs_git
def test_b02_a_linked_worktree_reads_the_main_repositorys_info_exclude(tmp_path: Path, no_global_excludes: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main, home=no_global_excludes)
    write(main, "f.txt")
    git("add", ".", cwd=main, home=no_global_excludes)
    git("commit", "-qm", "x", cwd=main, home=no_global_excludes)
    write(main, ".git/info/exclude", "excluded_by_info.txt\n")
    git("worktree", "add", "-q", str(tmp_path / "wt"), cwd=main, home=no_global_excludes)
    write(tmp_path, "wt/excluded_by_info.txt")
    write(tmp_path, "wt/k.txt")
    assert listing(tmp_path / "wt") == ["f f.txt", "f k.txt"]


def test_b02_a_git_file_without_commondir_has_no_info_exclude(tmp_path: Path) -> None:
    # A submodule's `.git` file: the crate reads no `info/exclude` for it, where git reads the module's own.
    write(tmp_path, "modules/sub/info/exclude", "*.txt\n")
    write(tmp_path, "work/.git", f"gitdir: {tmp_path / 'modules' / 'sub'}\n")
    write(tmp_path, "work/b.txt")
    write(tmp_path, "work/.gitignore", "*.log\n")
    write(tmp_path, "work/c.log")
    assert listing(tmp_path / "work") == ["f .gitignore", "f b.txt"]


def test_b02_a_relative_commondir_is_taken_from_the_git_directory(tmp_path: Path) -> None:
    write(tmp_path, "gitdir/worktrees/w/commondir", "../..\n")
    write(tmp_path, "gitdir/info/exclude", "e1\n")
    write(tmp_path, "work/.git", f"gitdir: {tmp_path / 'gitdir' / 'worktrees' / 'w'}\r\n")
    write(tmp_path, "work/e1")
    write(tmp_path, "work/e2")
    assert listing(tmp_path / "work") == ["f e2"]


# V6-ENV-B05: rule and glob syntax as `gitignore.rs` and globset parse it.


def matches(pattern: str, path: str, is_dir: bool = False) -> bool | None:
    rule = parse_rule(pattern)
    return None if rule is None else Matcher([rule]).matched(path, is_dir)


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        # Alternation.
        ("{a,b}", "b", False, True),
        ("{a,b}", "{a,b}", False, None),
        ("a{b,c}", "ac", False, True),
        ("{a,{b,c}}.x", "c.x", False, True),
        ("a{,b}", "a", False, None),
        ("a{,b}", "ab", False, True),
        # No POSIX classes: `[[:alpha:]]` is the class `[:alph` then `]`.
        ("[[:alpha:]]", "b", False, None),
        ("[[:alpha:]]", "h]", False, True),
        # No escapes inside a class: `[\]]` is the class `\` then `]`; `[\[]` is `\` or `[`.
        ("[\\]]", "]", False, None),
        ("[\\]]", "\\]", False, True),
        ("[\\[]", "\\", False, True),
        # A negated class matches `/`.
        ("a[!x]b", "a/b", False, True),
        # Trailing white space is Unicode's, tabs included; U+001C is no white space.
        ("tab\t", "tab", False, True),
        ("ideo　", "ideo", False, True),
        ("fs\x1c", "fs\x1c", False, True),
        # A dangling escape, an unopened `}`, an unclosed `{` or a reversed range is no rule.
        ("a\\  ", "a ", False, None),
        ("\\", "\\", False, None),
        ("tab\\\t", "tab\t", False, None),
        ("a}", "a}", False, None),
        ("{a", "{a", False, None),
        ("[z-a]", "b", False, None),
        # Doubled slashes never match; an escaped trailing slash is a directory rule.
        ("a//", "a/b", False, None),
        ("//a", "a", False, None),
        ("a\\/", "a", True, True),
        ("a\\/", "a", False, None),
        # `!` alone whitelists everything; `/` alone matches nothing.
        ("!", "x/y", False, False),
        ("/", "x", True, None),
        # An unclosed class is a literal `[`.
        ("[", "[", False, True),
    ],
)
def test_b05_rules_parse_as_the_crate_parses_them(pattern: str, path: str, is_dir: bool, expected: bool | None) -> None:
    assert matches(pattern, path, is_dir) is expected


def test_b05_a_gitignore_file_is_read_as_the_crate_reads_it(tmp_path: Path) -> None:
    repository(tmp_path)
    for name in ("b1", "b2", "c1", "c2", "u1"):
        write(tmp_path, name)
    # Byte order marks on the first line, CRLF, and a line that is not UTF-8, which ends the file.
    write(tmp_path, ".gitignore", b"\xef\xbb\xbfb1\r\nc1\r\nb2\n\xffbad\nu1\n")
    assert listing(tmp_path) == ["f .gitignore", "f c2", "f u1"]


@pytest.mark.skipif(os.name == "nt", reason="a newline in a file name is a POSIX case")
def test_b05_wildcards_cross_a_newline_in_a_name(tmp_path: Path) -> None:
    repository(tmp_path)
    write(tmp_path, "x\ny/foo")
    write(tmp_path, ".gitignore", "foo\n")
    assert listing(tmp_path) == ["f .gitignore", "d x\ny"]


# V6-ENV-B06: the global excludes file, found and matched as the crate does.


def test_b06_the_global_excludes_file_is_matched_relative_to_the_listed_directory(
    tmp_path: Path, no_global_excludes: Path
) -> None:
    write(no_global_excludes, ".gitconfig", "[core]\n\texcludesFile = ~/global_ignore\n")
    write(no_global_excludes, "global_ignore", "/anchored.txt\nsub/gsub.txt\n")
    repository(tmp_path)
    for path in ("anchored.txt", "sub/anchored.txt", "sub/gsub.txt", "sub/sub/gsub.txt"):
        write(tmp_path, path)
    assert listing(tmp_path) == ["d sub", "f sub/anchored.txt", "d sub/sub", "f sub/sub/gsub.txt"]
    assert listing(tmp_path / "sub") == ["f gsub.txt", "d sub"]


def test_b06_the_global_excludes_file_is_found_as_the_crate_finds_it(
    tmp_path: Path, no_global_excludes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = no_global_excludes
    repository(tmp_path)
    for name in ("g1", "g2", "g3"):
        write(tmp_path, name)
    write(home, "ig_home", "g1\n")
    write(home, "ig_global", "g2\n")
    # Any section: the first `excludesfile = …` line counts.
    write(home, ".gitconfig", "[user]\n\tname = x\n[other]\nexcludesfile = ~/ig_home\n")
    assert listing(tmp_path) == ["f g2", "f g3"]
    # `GIT_CONFIG_GLOBAL` comes first; one without the key falls through to `~/.gitconfig`.
    write(home, "global.cfg", "[core]\nexcludesFile=~/ig_global\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "global.cfg"))
    assert listing(tmp_path) == ["f g1", "f g3"]
    write(home, "empty.cfg", "[core]\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "empty.cfg"))
    assert listing(tmp_path) == ["f g2", "f g3"]


def test_b06_a_relative_excludes_file_is_the_daemons_working_directorys(
    tmp_path: Path, no_global_excludes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(no_global_excludes, ".gitconfig", "[core]\n  excludesfile = rel_ignore\n")
    repo = tmp_path / "repo"
    repository(repo)
    write(repo, "a.rel")
    write(repo, "b.txt")
    write(repo, "rel_ignore", "*.txt\n")
    write(tmp_path, "cwd/rel_ignore", "*.rel\n")
    monkeypatch.chdir(tmp_path / "cwd")
    assert listing(repo) == ["f b.txt", "f rel_ignore"]


def test_b06_an_empty_home_is_no_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `std::env::home_dir` skips an empty `$HOME` (for the user database's), so `./.gitconfig` is not read.
    monkeypatch.setenv("HOME", "")
    repo = tmp_path / "repo"
    repository(repo)
    write(repo, "h1")
    write(repo, "ign", "h1\n")
    write(tmp_path, "cwd/.gitconfig", "[core]\nexcludesfile = ign\n")
    write(tmp_path, "cwd/ign", "h1\n")
    monkeypatch.chdir(tmp_path / "cwd")
    assert listing(repo) == ["f h1", "f ign"]
