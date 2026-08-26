"""One way to atomically replace a state file.

A temp file beside the target is fsynced and replaced, then its parent directory is fsynced when
the platform supports it. A failure before ``os.replace`` leaves the previous file untouched;
after replacement the write is committed, so a directory-fsync error is best-effort rather than
misreported as a failed write. Every store that rewrites a file in place uses this.
"""
import os
import secrets
import stat


def write_bytes(path, data, *, mode=None):
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    resolved_mode = mode
    if resolved_mode is None:
        try:
            target_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            pass
        else:
            if stat.S_ISREG(target_stat.st_mode):
                resolved_mode = stat.S_IMODE(target_stat.st_mode)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            if resolved_mode is not None:
                os.chmod(tmp, resolved_mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(parent or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.close(directory_fd)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text, *, encoding="utf-8", mode=None):
    write_bytes(path, text.encode(encoding), mode=mode)
