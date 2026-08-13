"""瘦客户端：连守护进程，不在就拉起（herdr 的自动探测同款）。"""
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
    """发一条请求收一条响应。守护进程不在会抛 ConnectionError。"""
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
                break
            buf += chunk
    finally:
        con.close()
    out = json.loads(buf)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]


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
    raise RuntimeError("守护进程没能在时限内就绪（misaka net-daemon 手动跑一次看报错）")


def ensure(timeout=8.0):
    """守护进程在就直连；不在就拉起。版本不合（升级后旧进程还活着）：
    没有活卡就原地换新，有活卡在跑就拒绝并说清楚——绝不带病服务。"""
    from misaka.net import daemon as _d

    try:
        info = request("ping", timeout=2)
    except (ConnectionError, FileNotFoundError, OSError):
        return _spawn_and_wait(timeout)
    if info.get("proto") == _d.PROTOCOL:
        return info
    try:
        panes = request("panes.list")["panes"]
    except (RuntimeError, ConnectionError, OSError):
        panes = []
    if any(p.get("card") and p.get("alive") for p in panes):
        raise RuntimeError(
            "守护进程是旧版本，且有卡正在格子里跑——等它们跑完，"
            "或确认可弃后 `misaka net stop` 再进面板")
    try:
        request("server.stop")
    except (RuntimeError, ConnectionError, OSError):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and os.path.exists(_sock_path()):
        time.sleep(0.05)
    info = _spawn_and_wait(timeout)
    if info.get("proto") != _d.PROTOCOL:
        raise RuntimeError("重启后的守护进程版本仍不匹配（PATH 里可能有旧 misaka）")
    return info
