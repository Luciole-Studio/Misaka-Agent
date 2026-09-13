"""Optional project Git history. Research and cards write directly into the project;
commits record selected artifacts, never deliver them through branches or merges.
"""
import fcntl
import functools
import os
import subprocess
import time

_IDENTITY = ["-c", "user.name=misaka", "-c", "user.email=misaka@local"]


def _git(cwd, *args):
    """Run git; an index.lock held by another process is retried briefly (node processes and the
    driver commit on the same project line), a stale one still fails and the caller stops."""
    for attempt in range(5):
        done = subprocess.run(["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=False)
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
        # ponytail: one repository lock; split by worktree only if Git throughput warrants it.
        # Git's index.lock protects one command, not add/commit sequences.
        with open(os.path.join(directory, "misaka-git.lock"), "a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return operation(workspace, *args, **kwargs)
    return locked


@_serialized
def commit(workspace, paths, message):
    """Commit ``paths`` (relative to ``workspace``) on the line checked out there. A path that is
    gone from disk but tracked is committed as a deletion; a path git has never seen is skipped.
    True when the paths are committed (a commit was made, or there was nothing left to commit)."""
    return _commit(workspace, paths, message)


def _commit(workspace, paths, message):
    """Commit with the repository mutation lock already held."""
    if not enabled(workspace):
        return False
    paths = [p for p in paths
             if os.path.lexists(os.path.join(workspace, p))
             or _git(workspace, "ls-files", "--", p).stdout.strip()]
    if not paths:
        return False
    if _git(workspace, "add", "-A", "--", *paths).returncode != 0:
        return False                                   # nothing staged: an empty diff below would lie
    staged = _git(workspace, "diff", "--cached", "--quiet", "--", *paths).returncode
    if staged == 0:
        return True                                    # already committed: nothing to do is not a failure
    if staged != 1:
        return False                                   # git itself failed (lock, corrupt index)
    return _git(workspace, "commit", "-q", "-m", message, "--", *paths).returncode == 0


def commit_card(workspace, task_id, submission, message):
    """Commit a card's submitted artifacts together with its contract and inputs."""
    return commit(workspace, [*(submission.get("artifacts") or []),
                              os.path.join("cards", f"{task_id}.md"),
                              os.path.join("cards", str(task_id))], message)
