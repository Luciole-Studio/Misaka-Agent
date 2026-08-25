"""Thin client: connect to the daemon, starting it if it is not running (herdr-style auto-detect)."""
import json
import os
import socket
import subprocess
import sys
import time

from misaka.config import CFG


def _sock_path():
    return os.path.expanduser(CFG["net_sock"])


def request(method, params=None, *, timeout=10):
    """Send one request and return its result. Raises ConnectionError if the daemon is not running."""
    con = socket.socket(socket.AF_UNIX)
    con.settimeout(timeout)
    con.connect(_sock_path())
    try:
        con.sendall((json.dumps(
            {"id": "1", "method": method, "params": params or {}},
            ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = con.recv(65536)
            if not chunk:
                # A daemon mid-shutdown accepts and then hangs up: that is "not
                # running", never a reply to parse.
                raise ConnectionError("the daemon closed the connection")
            buf += chunk
    finally:
        con.close()
    out = json.loads(buf)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]


def _wait_for_dying_daemon(timeout):
    """A daemon that hung up mid-shutdown still LISTENS for a moment; spawning
    immediately makes the new daemon exit with "already running". Wait until its
    listener is gone -- refused / missing / timed out (herdr's stale classification);
    the file itself stays behind by design and the new daemon reclaims it."""
    deadline = time.monotonic() + timeout
    path = _sock_path()
    while time.monotonic() < deadline and os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(0.5)
        try:
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
            return                       # herdr's stale classification: the old daemon is gone
        except OSError:
            pass
        finally:
            probe.close()
        time.sleep(0.05)


def _spawn_and_wait(timeout):
    with open(os.devnull, "rb") as devnull_in, open(os.devnull, "ab") as devnull_out:
        subprocess.Popen(
            [sys.executable, "-m", "misaka", "net-daemon"],
            stdin=devnull_in, stdout=devnull_out, stderr=devnull_out,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return request("ping", timeout=2)
        except (ConnectionError, FileNotFoundError, OSError):
            time.sleep(0.05)
    raise RuntimeError("The daemon did not become ready in time (run `misaka net-daemon` by hand to see the error).")


def ensure(timeout=8.0):
    """Connect to the daemon, starting it if needed.

    On a protocol mismatch (an old daemon survived an upgrade): replace it in
    place when no cards are running; refuse with an explanation when cards are
    running. Never serve from a stale daemon.
    """
    from misaka.net import daemon as _d

    try:
        info = request("ping", timeout=2)
    except (ConnectionError, FileNotFoundError, OSError):
        _wait_for_dying_daemon(timeout=min(3.0, timeout))
        return _spawn_and_wait(timeout)
    if info.get("proto") == _d.PROTOCOL:
        return info
    try:
        panes = request("panes.list")["panes"]
    except (RuntimeError, ConnectionError, OSError):
        panes = []
    if any(p.get("card") and p.get("alive") for p in panes):
        raise RuntimeError(
            "The daemon is an older version and still has a running task card in a pane. Wait for it to finish, "
            "or run `misaka net stop` once you are sure it can be discarded, then reopen the panel."
        )
    try:
        request("server.stop")
    except (RuntimeError, ConnectionError, OSError):
        pass
    _wait_for_dying_daemon(timeout=min(3.0, timeout))
    info = _spawn_and_wait(timeout)
    if info.get("proto") != _d.PROTOCOL:
        raise RuntimeError("The restarted daemon still has a protocol mismatch (an older MISAKA may be on PATH).")
    return info
