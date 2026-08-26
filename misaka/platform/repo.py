"""Git for projects (git unification): one repository per project.

Cards commit on whatever line their workspace is checked out on: the project folder
(default branch) or a research node's worktree. Research nodes are the only branches:
``research/<node>`` forks from its parent node's branch and merges back when the node
closes. A folder that is not a repository with a commit degrades every function to a
no-op, so nothing below requires git.
"""
import os
import subprocess

_IDENTITY = ["-c", "user.name=misaka", "-c", "user.email=misaka@local"]


def _git(cwd, *args):
    return subprocess.run(["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=False)


def enabled(workspace):
    """True when the folder is inside a git repository (or worktree)."""
    try:
        return _git(workspace, "rev-parse", "--git-dir").returncode == 0
    except OSError:
        return False


def commit(workspace, paths, message):
    """Commit ``paths`` (relative to ``workspace``) on the line checked out there. A path that is
    gone from disk but tracked is committed as a deletion; a path git has never seen is skipped.
    True when the paths are committed (a commit was made, or there was nothing left to commit)."""
    if not enabled(workspace):
        return False
    paths = [p for p in paths
             if os.path.lexists(os.path.join(workspace, p))
             or _git(workspace, "ls-files", "--", p).stdout.strip()]
    if not paths:
        return False
    _git(workspace, "add", "-A", "--", *paths)
    if _git(workspace, "diff", "--cached", "--quiet", "--", *paths).returncode == 0:
        return True                                    # already committed: nothing to do is not a failure
    return _git(workspace, "commit", "-q", "-m", message, "--", *paths).returncode == 0


def branch_merged(workspace, name, into=None):
    """True when branch ``name`` is already contained in the line checked out at ``into``."""
    return enabled(workspace) and _git(into or workspace, "merge-base", "--is-ancestor", name, "HEAD").returncode == 0


def commit_card(workspace, task_id, report, message):
    """A card's submission: the artifacts its report lists plus its contract and inputs."""
    return commit(workspace, [*(report.get("artifacts") or []),
                              os.path.join("cards", f"{task_id}.md"),
                              os.path.join("cards", str(task_id))], message)


def branch_start(workspace, name, worktree, base=None):
    """Check branch ``name`` out into ``worktree``, creating it from ``base`` (default: the
    workspace's HEAD) when new. Reuses a live worktree. Returns the path, or None when degraded."""
    if not enabled(workspace):
        return None
    if os.path.exists(os.path.join(worktree, ".git")):
        return worktree
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    _git(workspace, "worktree", "prune")                # a deleted directory may still be registered
    if _git(workspace, "rev-parse", "--verify", "-q", name).returncode == 0:
        made = _git(workspace, "worktree", "add", "-q", worktree, name)
    else:
        made = _git(workspace, "worktree", "add", "-q", "-b", name, worktree, *([base] if base else []))
    return worktree if made.returncode == 0 else None


def branch_finish(workspace, name, worktree, *, into=None, merge=True, message="", remove=True):
    """Close a branch: leftover work is committed, then -- when ``merge`` -- the branch is merged
    --no-ff into the line checked out at ``into`` (default: the workspace), and only then is the
    worktree removed. A conflict leaves both the branch and its worktree in place for a human.
    The branch itself always stays as the record. Returns "merged" / "conflict" / "closed" / None."""
    if not enabled(workspace):
        return None
    live = os.path.exists(os.path.join(worktree, ".git"))
    if live:
        commit(worktree, ["."], f"{name}: leftover changes")
    if merge:
        merged = _git(into or workspace, "merge", "--no-ff", "-q", "-m", message or f"{name}: merged", name)
        if merged.returncode != 0:
            _git(into or workspace, "merge", "--abort")
            return "conflict"                          # the branch and its worktree stay for a human
    if live and remove:
        _git(workspace, "worktree", "remove", "--force", worktree)
    _git(workspace, "worktree", "prune")
    return "merged" if merge else "closed"

