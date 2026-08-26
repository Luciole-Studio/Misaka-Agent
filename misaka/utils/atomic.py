"""One way to write a state file: the whole content or none of it.

A temp file beside the target, fsync, then ``os.replace`` -- a crash, a full disk or a failing
write leaves the previous file untouched. Every store that rewrites a file in place (auth,
settings, sessions, cards, document metadata, skill files) uses this and nothing else.
"""
import os
import secrets


def write_bytes(path, data, *, mode=None):
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text, *, encoding="utf-8", mode=None):
    write_bytes(path, text.encode(encoding), mode=mode)
