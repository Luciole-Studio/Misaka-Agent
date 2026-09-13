"""CCB worktree.ts creation/preparation through MISAKA's process and storage host.

Reference pin 77a7934e15d69da13879112ed7db695c9ee7a52a. Never rewrite the
shared repository's core.hooksPath: other MISAKA sessions own that config too.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pathspec


async def canonical_root(cwd: str, run: Callable) -> str:
    code, result, error = await run(['git', '-C', cwd, 'worktree', 'list', '--porcelain'])
    if not code:
        for line in result.splitlines():
            if line.startswith('worktree '):
                return line.removeprefix('worktree ')
    raise ValueError(f'isolation="worktree" requires a git repository: {error.strip()}')


async def default_branch(repo: str, run: Callable) -> str:
    code, value, _ = await run(['git', '-C', repo, 'symbolic-ref', 'refs/remotes/origin/HEAD'])
    if not code and value.strip().startswith('refs/remotes/origin/'):
        return value.strip().removeprefix('refs/remotes/origin/')
    for name in ('main', 'master'):
        code, _, _ = await run(['git', '-C', repo, 'rev-parse', '--verify', f'refs/remotes/origin/{name}'])
        if not code:
            return name
    return 'main'


def _inside(root: Path, name: str) -> Path | None:
    path = root / name
    try:
        path.resolve().relative_to(root.resolve())
        return path
    except (OSError, ValueError):
        return None


async def copy_included_files(repo: Path, work: Path, run: Callable, warn: Callable) -> list[str]:
    """Same collapsed-directory/explicit-prefix algorithm as copyWorktreeIncludeFiles."""
    try:
        content = await asyncio.to_thread((repo/'.worktreeinclude').read_text, encoding='utf-8')
    except OSError:
        return []
    patterns = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith('#')]
    if not patterns:
        return []
    matcher = pathspec.GitIgnoreSpec.from_lines(content.splitlines())
    code, output, _ = await run(['git', '-C', str(repo), 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'])
    if code:
        return []
    entries = [name for name in output.split('\0') if name]
    files = [name for name in entries if not name.endswith('/') and matcher.match_file(name)]
    expand = []
    for directory in (name for name in entries if name.endswith('/')):
        if matcher.match_file(directory.removesuffix('/')) or matcher.match_file(directory):
            expand.append(directory)
            continue
        for pattern in patterns:
            normalized = pattern.removeprefix('/')
            glob = re.search(r'[*?\[]', normalized)
            if normalized.startswith(directory) or (glob is not None and glob.start() > 0 and directory.startswith(normalized[:glob.start()])):
                expand.append(directory)
                break
    if expand:
        code, output, _ = await run(['git', '-C', str(repo), 'ls-files', '--others', '--ignored', '--exclude-standard', '-z', '--', *expand])
        if not code:
            files.extend(name for name in output.split('\0') if name and matcher.match_file(name))
    copied = []
    for name in dict.fromkeys(files):
        source, destination = _inside(repo, name), _inside(work, name)
        if source is None or destination is None:
            warn(f'Skipped external worktree include: {name}')
            continue
        try:
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, source, destination)
            copied.append(name)
        except OSError as error:
            warn(f'Could not copy worktree include {name}: {error}')
    return copied


async def create(repo: str, path: Path, branch: str, settings: Mapping[str, Any], run: Callable, warn: Callable, *, prepare: bool = True) -> tuple[str, bool]:
    # Existing path must really be a linked worktree, not an arbitrary parent
    # repository reached by Git's upward traversal.
    if (path/'.git').is_file():
        code, head, _ = await run(['git', '-C', str(path), 'rev-parse', 'HEAD'])
        if not code:
            await asyncio.to_thread(os.utime, path, None)
            return head.strip(), True
    name = await default_branch(repo, run)
    base = f'origin/{name}'
    code, head, _ = await run(['git', '-C', repo, 'rev-parse', '--verify', base])
    if code:
        code, _, _ = await run(['git', '-C', repo, 'fetch', 'origin', name],
                               env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'true', 'SSH_ASKPASS': 'true'})
        if code:
            base = 'HEAD'
        code, head, error = await run(['git', '-C', repo, 'rev-parse', base])
        if code:
            raise RuntimeError(error.strip() or f'Could not resolve base branch {base}')
    sparse = settings.get('sparsePaths') or []
    if not isinstance(sparse, list) or any(not isinstance(item, str) for item in sparse):
        raise ValueError('worktree.sparsePaths must be a list of paths')
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    args = ['git', '-C', repo, 'worktree', 'add', *(['--no-checkout'] if sparse else []), '-B', branch, str(path), base]
    code, _, error = await run(args)
    if code:
        raise RuntimeError(error.strip() or 'Could not create agent worktree')
    try:
        if sparse:
            for command in (['sparse-checkout', 'set', '--cone', '--', *sparse], ['checkout', 'HEAD']):
                code, _, error = await run(['git', '-C', str(path), *command])
                if code:
                    raise RuntimeError(error.strip() or 'Could not configure sparse worktree')
        for name in settings.get('symlinkDirectories', ()) if prepare else ():
            if not isinstance(name, str) or Path(name).is_absolute() or any(part in {'.', '..'} for part in name.split('/')):
                continue
            source, destination = _inside(Path(repo), name), _inside(path, name)
            if source is not None and destination is not None and source.is_dir():
                try:
                    await asyncio.to_thread(destination.symlink_to, source, target_is_directory=True)
                except OSError:
                    pass  # CCB ignores missing/already present destinations.
        if prepare:
            await copy_included_files(Path(repo), path, run, warn)
    except BaseException:
        # A registered-but-empty sparse tree must not become a successful resume.
        await run(['git', '-C', repo, 'worktree', 'remove', '--force', str(path)])
        raise
    return head.strip(), False


async def run_vcs_hooks(hooks: Mapping[str, Any], event: str, payload: Mapping[str, Any], cwd: str) -> tuple[bool, str]:
    """CCB executeWorktree*Hook; keep failed cleanup visible to MISAKA's owner."""
    from misaka.core.subagent.hooks import execute_async_command_hook

    configured = [hook for matcher in hooks.get(event, []) for hook in matcher.get("hooks", [])
                  if not matcher.get("matcher")]
    if not configured:
        return False, ""
    results = []
    for hook in configured:
        if hook.get("type", "command") != "command":
            raise ValueError(f"{event} requires a command hook in the MISAKA process host")
        result, code = await execute_async_command_hook(
            hook, {**payload, "hook_event_name": event, "cwd": cwd}, cwd=cwd,
        )
        results.append((result, code))
    if event == "WorktreeCreate":
        for result, code in results:
            path = str(result.get("output") or "").strip()
            if code == 0 and path:
                target = Path(path).expanduser()
                if not target.is_absolute() or not target.is_dir():
                    raise ValueError("WorktreeCreate must return an existing absolute directory")
                if target.resolve() == Path(cwd).resolve():
                    raise ValueError("WorktreeCreate must return a separate working directory")
                return True, str(target.resolve())
        raise RuntimeError("WorktreeCreate hook failed: " + "; ".join(
            str(result.get("reason") or "no successful output") for result, _ in results))
    # Upstream reports 'ran' even on failure. Retain owner metadata on failures
    # here: deleting it would orphan the user's VCS working directory.
    if any(code != 0 for _, code in results):
        raise RuntimeError("WorktreeRemove hook failed: " + "; ".join(
            str(result.get("reason") or code) for result, code in results if code != 0))
    return True, ""


def activity_lock(path: Path | str):
    """Native multi-process host fence; OS releases it even after parent death."""
    from filelock import FileLock
    return FileLock(str(Path(path).resolve()) + '.active.lock', thread_local=False)


def release_activity(task) -> None:
    lock = getattr(task, '_worktree_lock', None)
    if lock is not None:
        lock.release()
        task._worktree_lock = None


async def cleanup_stale(repo: str, root: Path, cutoff: float, run: Callable,
                        warn: Callable, *, current: str | None = None) -> int:
    """Port cleanupStaleAgentWorktrees; only native ephemeral slugs/branches.

    MISAKA keeps worktrees outside the repo and has multiple process owners:
    exact namespace checks and activity locks replace CCB's global cwd owner.
    Unlike source -uno, preserve untracked files too; shared research outputs
    are user data. A clean remote-reachable tree is still reclaimed as upstream.
    """
    from filelock import Timeout

    code, output, _ = await run(['git', '-C', repo, 'worktree', 'list', '--porcelain', '-z'])
    if code:
        return 0
    root = root.expanduser().resolve()
    current_path = Path(current).resolve() if current else None
    removed = 0
    for record in output.split('\0\0'):
        fields = record.split('\0')
        data = dict(field.partition(' ')[::2] for field in fields if ' ' in field)
        raw = data.get('worktree')
        if not raw or any(field == 'locked' or field.startswith('locked ') for field in fields):
            continue
        path = Path(raw)
        try:
            relative = path.resolve().relative_to(root)
            if len(relative.parts) != 2 or not re.fullmatch(r'a[0-9a-f]{16}', relative.name):
                continue
            if path.is_symlink() or not path.is_dir() or (path/'.git').is_symlink() or not (path/'.git').is_file():
                continue
            if current_path is not None and current_path.is_relative_to(path.resolve()):
                continue
            branch = 'misaka-agent-' + relative.name
            if data.get('branch') != 'refs/heads/' + branch:
                continue
            with activity_lock(path).acquire(timeout=0):
                # Check AFTER acquiring the same fence used by create/resume.
                if path.stat().st_mtime >= cutoff:
                    continue
                status, unpushed = await asyncio.gather(
                    run(['git', '-C', str(path), '--no-optional-locks', 'status', '--porcelain']),
                    run(['git', '-C', str(path), 'rev-list', '--max-count=1', 'HEAD', '--not', '--remotes']),
                )
                if any(result[0] or result[1].strip() for result in (status, unpushed)):
                    continue
                code, _, error = await run(['git', '-C', repo, 'worktree', 'remove', '--force', str(path)])
                if code:
                    warn(f'Could not remove stale agent worktree {path}: {error.strip()}')
                    continue
                removed += 1
                code, _, error = await run(['git', '-C', repo, 'branch', '-D', branch])
                if code:
                    warn(f'Could not remove stale agent branch {branch}: {error.strip()}')
        except (OSError, ValueError, Timeout):
            continue  # Source fail-closed filesystem checks; another owner wins.
    if removed:
        await run(['git', '-C', repo, 'worktree', 'prune'])
    return removed
