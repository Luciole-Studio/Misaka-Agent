"""Translated from the ``loadProjectContextFiles - nested worktree dedup`` cases of pi's
``packages/coding-agent/test/resource-loader.test.ts`` (v1.3.0)."""

from __future__ import annotations

import os

from misaka.core.resource_loader import loadProjectContextFiles


def _link_worktree(main_dir: str, worktree_dir: str, name: str) -> None:
    """A linked-worktree skeleton (no git binary needed): the main repo's ``.git/worktrees/<name>/`` holds ``HEAD``
    plus a ``commondir`` pointing back at the main ``.git``, and the worktree's working tree carries a ``.git``
    *file* whose ``gitdir:`` resolves to it."""
    git_dir = os.path.join(main_dir, ".git", "worktrees", name)
    os.makedirs(git_dir)
    # The main repo's own `.git` is a real git dir with a HEAD, as git writes it.
    with open(os.path.join(main_dir, ".git", "HEAD"), "w") as file:
        file.write("ref: refs/heads/main\n")
    with open(os.path.join(git_dir, "HEAD"), "w") as file:
        file.write("ref: refs/heads/feat\n")
    # commondir is relative to the worktree gitdir and points at the main .git.
    with open(os.path.join(git_dir, "commondir"), "w") as file:
        file.write("../..")
    with open(os.path.join(worktree_dir, ".git"), "w") as file:
        file.write(f"gitdir: {git_dir}\n")


def _setup_nested_worktree(temp_dir: str) -> dict[str, str]:
    """Main repo at <temp_dir>/outer/main with a linked worktree at main/worktrees/feat."""
    outer = os.path.join(temp_dir, "outer")
    main = os.path.join(outer, "main")
    worktree = os.path.join(main, "worktrees", "feat")
    worktree_src = os.path.join(worktree, "src")
    os.makedirs(worktree_src)
    _link_worktree(main, worktree, "feat")
    return {"outer": outer, "main": main, "worktree": worktree, "worktreeSrc": worktree_src}


def _write(path: str, text: str) -> None:
    with open(path, "w") as file:
        file.write(text)


def _load(tmp_path, cwd: str) -> list[dict[str, str]]:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(exist_ok=True)
    return loadProjectContextFiles({"cwd": cwd, "agentDir": str(agent_dir)})


def test_skips_the_main_repos_duplicate_when_the_worktree_root_has_its_own_context(tmp_path):
    paths = _setup_nested_worktree(str(tmp_path))
    _write(os.path.join(paths["main"], "AGENTS.md"), "main repo instructions")
    _write(os.path.join(paths["worktree"], "AGENTS.md"), "worktree instructions")

    files = _load(tmp_path, paths["worktreeSrc"])

    assert [file["content"] for file in files] == ["worktree instructions"]


# https://github.com/earendil-works/pi/issues/10681
def test_loads_the_worktrees_context_once_when_it_symlinks_to_the_main_repos_file(tmp_path):
    paths = _setup_nested_worktree(str(tmp_path))
    _write(os.path.join(paths["main"], "AGENTS.md"), "main repo instructions")
    os.symlink(os.path.join(paths["main"], "AGENTS.md"), os.path.join(paths["worktree"], "AGENTS.md"))

    files = _load(tmp_path, paths["worktreeSrc"])

    assert [file["path"] for file in files] == [os.path.join(paths["worktree"], "AGENTS.md")]


def test_skips_the_main_repos_duplicate_when_the_main_repos_file_is_a_symlink(tmp_path):
    paths = _setup_nested_worktree(str(tmp_path))
    shared = os.path.join(paths["outer"], "shared-agents.md")
    _write(shared, "shared instructions")
    os.symlink(shared, os.path.join(paths["main"], "AGENTS.md"))
    os.symlink(shared, os.path.join(paths["worktree"], "AGENTS.md"))

    files = _load(tmp_path, paths["worktreeSrc"])

    assert [file["path"] for file in files] == [os.path.join(paths["worktree"], "AGENTS.md")]


def test_still_inherits_the_main_repos_context_when_the_worktree_root_has_none(tmp_path):
    paths = _setup_nested_worktree(str(tmp_path))
    _write(os.path.join(paths["main"], "AGENTS.md"), "main repo instructions")

    files = _load(tmp_path, paths["worktreeSrc"])

    assert [file["content"] for file in files] == ["main repo instructions"]


def test_only_skips_the_same_filename_not_a_differently_named_context_file(tmp_path):
    # The repo tracks CLAUDE.md; the worktree adds an AGENTS.md, which loadContextFileFromDir prefers. The main
    # repo's CLAUDE.md is nobody's duplicate, so dropping it would lose its content entirely.
    paths = _setup_nested_worktree(str(tmp_path))
    _write(os.path.join(paths["main"], "CLAUDE.md"), "main repo instructions")
    _write(os.path.join(paths["worktree"], "AGENTS.md"), "worktree instructions")

    files = _load(tmp_path, paths["worktreeSrc"])

    assert [file["content"] for file in files] == ["main repo instructions", "worktree instructions"]
