"""The project's Git history, written only when the user asks.

Workflows and cards write directly into the project and never commit: the history is the
user's. ``misaka init`` creates the repository; ``project_commit`` and ``/commit`` commit what the
user confirms, under the user's own git identity. Commits record selected files, never deliver
them through branches or merges.
"""
import functools
import os
import subprocess
import time

from misaka.utils import file_lock


def _git(cwd, *args):
    """Run git; an index.lock held by another process is retried briefly, a stale one still
    fails and the caller stops."""
    for attempt in range(5):
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
        if done.returncode == 0 or "index.lock" not in done.stderr or attempt == 4:
            return done
        time.sleep(0.2 * (attempt + 1))
    return done


def enabled(workspace):
    """True when the folder is inside a git repository (or worktree)."""
    try:
        return _git(workspace, "rev-parse", "--git-dir").returncode == 0
    except OSError:
        return False


def _serialized(operation):
    @functools.wraps(operation)
    def locked(workspace, *args, **kwargs):
        if not enabled(workspace):
            return operation(workspace, *args, **kwargs)
        common = _git(workspace, "rev-parse", "--git-common-dir")
        if common.returncode:
            raise OSError(common.stderr.strip())
        directory = os.path.realpath(os.path.join(workspace, common.stdout.strip()))
        # Git's index.lock protects one command, not an add/commit sequence.
        with open(os.path.join(directory, "misaka-git.lock"), "a", encoding="utf-8") as lock:
            file_lock.lock_fd(lock.fileno())
            return operation(workspace, *args, **kwargs)
    return locked


def changes(workspace, paths=()):
    """What a commit of ``paths`` (all of the project when empty) would record, as ``git status
    --porcelain`` lines: modified, added, deleted and untracked files, the ignored ones left out."""
    done = _git(workspace, "status", "--porcelain", "--untracked-files=all", "--", *(paths or ["."]))
    if done.returncode:
        raise OSError(done.stderr.strip() or "git status failed")
    return [line for line in done.stdout.splitlines() if line.strip()]


@_serialized
def commit(workspace, message, paths=()):
    """Stage and commit ``paths`` (all of the project when empty) under the repository's own
    configured identity. Returns the new commit's short hash; raises OSError with git's own words
    when git refuses (no identity configured, a hook, a lock)."""
    spec = list(paths) or ["."]
    added = _git(workspace, "add", "-A", "--", *spec)
    if added.returncode:
        raise OSError(added.stderr.strip() or "git add failed")
    staged = _git(workspace, "diff", "--cached", "--quiet", "--", *spec).returncode
    if staged == 0:
        raise OSError("nothing to commit")
    if staged != 1:
        raise OSError("git could not compare the staged files")
    done = _git(workspace, "commit", "-q", "-m", message, "--", *spec)
    if done.returncode:
        raise OSError(done.stderr.strip() or done.stdout.strip() or "git commit failed")
    return _git(workspace, "rev-parse", "--short", "HEAD").stdout.strip()
