"""What every built-in tool needs to read its arguments and honour an abort.

The seven tool modules were ported one file at a time, so each arrived with its own copy
of the same handful of ideas. The copies had drifted: the abort predicate went by two
names, and one of the three abort-task spellings passed a Future to
``asyncio.create_task``, which raises. Written once here, they cannot drift again.

The helpers keep the leading underscore they were ported with. The module is already
package-private, and the alternative was renaming a hundred and thirty call sites in a
change whose whole point is that nothing about them should differ.

``write_file_text`` joined them later for the same reason: write and edit had the same
one-line body, and it was the same line in both that broke symlinks.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

from misaka.utils import atomic
from misaka.utils.async_lifecycle import settle
from misaka.utils.values import signal_aborted

try:  # POSIX only; on Windows there is no O_NONBLOCK to clear either.
    import fcntl
except ImportError:  # pragma: no cover - exercised only off POSIX
    fcntl = None  # type: ignore[assignment]


def abort_wait_task(signal: Any | None) -> asyncio.Task[None] | None:
    """A task that finishes when the caller aborts, or None if this signal cannot say.

    ``misaka.agent.agent.AbortSignal`` is the only signal in the project and it wraps an
    ``asyncio.Event``, so awaiting it is free. A signal that cannot be awaited gets None
    and the caller runs unraced, rather than polling a flag at 100Hz for the length of
    the tool call -- which is what the ported fallback did.
    """
    if signal is None:
        return None
    wait = getattr(signal, "wait", None)
    if not callable(wait):
        return None
    pending = wait()
    if not isinstance(pending, Awaitable):
        return None
    return asyncio.ensure_future(pending)


@asynccontextmanager
async def abort_race(signal: Any | None) -> AsyncIterator[asyncio.Task[None] | None]:
    """Hold an abort task for the body, and always retire it on the way out.

    Every tool wrote the same teardown by hand in a ``finally``; the one that forgot
    would leak a task waiting on an Event for the life of the process.
    """
    task = abort_wait_task(signal)
    try:
        yield task
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run_with_abort[T](work: Any, signal: Any | None) -> tuple[T | None, bool]:
    """Run *work* to completion, cancelling it if the caller aborts.

    Returns ``(result, False)``, or ``(None, True)`` when the abort won. The agent loop
    awaits a tool call directly rather than running it as a task it can cancel
    (``misaka/agent/agent_loop.py``), so a tool that only checks its signal between steps
    cannot be interrupted *during* one: a single vendor call with a sixty-second ceiling
    is a minute of a session that will not answer Ctrl-C, and five pages through such a
    backend is five. A tool whose work is one long await races it instead of polling.

    Nothing is left running: the losing side is cancelled and reaped before this returns.
    """
    task = asyncio.ensure_future(work)
    aborted = None
    try:
        aborted = abort_wait_task(signal)
        if signal_aborted(signal):
            return None, True
        if aborted is None:
            return await asyncio.shield(task), False
        done, _ = await asyncio.wait({task, aborted}, return_when=asyncio.FIRST_COMPLETED)
        if aborted in done:
            return None, True
        return task.result(), False
    finally:
        owned = [task] if aborted is None else [task, aborted]
        for pending in owned:
            if not pending.done():
                pending.cancel()
        _, cancelled = await settle(asyncio.gather(*owned, return_exceptions=True))
        if cancelled is not None:
            raise cancelled


def _string_arg(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return None


def _ignore_background_task_result(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:  # noqa: BLE001 - the background task's outcome is intentionally discarded
            return

    task.add_done_callback(_consume)


async def _drain_worker(task: asyncio.Task[Any]) -> bool:
    """Wait through repeated caller cancellation; return whether another cancel arrived."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - draining must survive every worker outcome
            break
    if task.done():
        try:
            task.result()
        except BaseException:  # noqa: BLE001, S110 - observing the drained outcome is sufficient
            pass
    return cancelled


def _open_for_write(real_path: str, *, may_block: bool) -> Any:
    """Open a file for truncating writes; when the open itself could block, refuse to.

    ``open(fifo, "wb")`` blocks until somebody opens the read end -- forever, if nobody ever
    does. That wait happens inside a thread, so it cannot be cancelled: the tool call never
    returns, the abort signal is ignored because the abort path deliberately drains the worker
    to completion, and the per-path mutation lock it holds takes every later write and edit of
    the same path down with it. One unread pipe would end the session.

    So ask for ``O_NONBLOCK`` on the open and drop it again the moment the descriptor exists.
    A FIFO with no reader answers ENXIO instead of hanging, which is a far better answer than
    silence; a FIFO with a reader opens exactly as before. Clearing the flag straight away
    restores blocking semantics for the *write*, so a full pipe still waits for the reader to
    catch up rather than failing with EAGAIN and losing the rest of the content.

    This is a deliberate step away from ``fsWriteFile``, whose open blocks in libuv's pool for
    the same reason ours blocked in a thread. Everything observable is unchanged -- a pipe with
    a reader takes the bytes, a pipe without one was never going to -- except that the failure
    now arrives as an errno rather than as a session that stops answering.

    ``O_NONBLOCK`` (and ``fcntl``, which takes it back off) is POSIX; where it is missing this
    is the plain truncating open it has always been. Regular files pass ``may_block=True`` and
    get exactly that, since opening one never waits on anybody.
    """
    nonblock = 0 if may_block or fcntl is None else getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(real_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nonblock, 0o666)
    try:
        if nonblock:
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~nonblock)
        return os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise


def write_file_text(path: str, content: str) -> None:
    """Write to the file a path *names*, the way ``fsWriteFile`` does, not to the name itself.

    ``atomic.write_text`` replaces the name: ``os.replace`` drops a brand-new file where the
    old one was. The state stores want precisely that -- a fresh inode is how they keep a
    concurrent writer from being half-overwritten -- but for the file tools it is silent data
    loss. A symlink at ``path`` becomes a plain file holding the new text while the file it
    pointed at keeps the old, and a hardlinked name is unlinked from its twin, both while the
    tool reports success and a later read of the same path shows the new text back.

    So resolve the link first, and only take the atomic path when what it would replace is an
    ordinary file with no second name -- the one shape where a fresh inode is indistinguishable
    from a rewrite. Everything else is written through in place with a plain truncating
    ``open``, which is what ``fsWriteFile`` does for every write:

    * a name with a link count above one cannot be replaced atomically and stay linked, since a
      new inode has a link count of one by construction, so it truncates and gives up atomicity;
    * a FIFO, a device node or any other non-regular file is written *into*, not deleted and
      replaced by a regular file. A symlink to ``/dev/null`` or to a named log pipe survives the
      write as what it was. Something like a socket, which cannot be opened this way, fails with
      the OSError the open raises -- a loud failure being much better than a silent substitution.
      That open is non-blocking (see ``_open_for_write``) so a pipe nobody is reading
      says ENXIO instead of hanging the session.

    Which branch a name takes decides whether its own permission bits are enforced, and the two
    answers differ. A read-only regular file with one name goes down the atomic path, where
    ``os.replace`` consults the *directory*, not the file, so the write succeeds and the new
    contents keep the old 0o444; give that same file a second name and the in-place open raises
    EACCES. This is the shape the two rules produce, not a decision about read-only files, and
    it is pinned by tests so that changing it has to be deliberate.

    A dangling symlink resolves to the name it points at and stats ENOENT, so the write creates
    that file and leaves the link pointing at it, again as ``fsWriteFile`` would. Any other stat
    error -- ELOOP from a symlink cycle, EACCES from an unreadable directory -- is raised rather
    than guessed at, so the tool reports the failure instead of replacing a link on the cycle
    with a regular file.

    The atomic path stages its temp file in the *resolved* file's own directory, so a writable
    file inside a read-only directory now fails with PermissionError where the pre-symlink code
    would have staged next to the link instead. That is the trade for never handing back a write
    that landed somewhere other than where the path pointed.
    """
    real_path = os.path.realpath(path)
    try:
        target = os.stat(real_path)
    except FileNotFoundError:
        atomic.write_text(real_path, content)
        return
    regular = stat.S_ISREG(target.st_mode)
    if regular and target.st_nlink == 1:
        atomic.write_text(real_path, content)
        return
    data = content.encode("utf-8")
    with _open_for_write(real_path, may_block=regular) as handle:
        handle.write(data)
        handle.flush()
        if regular:
            # fsync is meaningless on a pipe and an error on some platforms; the durability
            # this buys is only owed to files that have durable contents in the first place.
            os.fsync(handle.fileno())
