"""A local stream socket named by a filesystem path, on every platform.

POSIX: an AF_UNIX socket bound at the path, which is what every caller used directly before.

Windows has no AF_UNIX in CPython, so this follows herdr's ``ipc.rs``: the server is a named
pipe whose name is the path under the pipe namespace (``\\\\.\\pipe\\<path>``), created with an
owner-only DACL (``D:P(A;;GA;;;SY)(A;;GA;;;OW)``), and a marker file (``<pid>:<nanoseconds>``)
is written at the path, so the path still says whether a server was started there and which
one: ``identity`` is the marker's content where POSIX compares the socket's device and inode.
A pipe handle cannot be selected on, so every connection is bridged to one end of a socketpair
by two threads. Callers keep a real socket (``select``, ``settimeout``, ``recv``/``sendall``)
and an asyncio server hands its callback ordinary streams.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import _overlapped
    import _winapi
    import ctypes
    from ctypes import wintypes

    _PIPE_PREFIX = "\\\\.\\pipe\\"
    _OWNER_ONLY = "D:P(A;;GA;;;SY)(A;;GA;;;OW)"
    _PIPE_REJECT_REMOTE_CLIENTS = 0x8
    _BUFFER = 65536
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_ACCESS_DENIED = 5
    _INFINITE = 0xFFFFFFFF

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]


def connect(path, timeout=None):
    """A connected stream socket. A missing or dead server raises what AF_UNIX ``connect``
    raises for one: FileNotFoundError, ConnectionRefusedError, TimeoutError."""
    if WINDOWS:
        return _pipe_connect(os.fspath(path), timeout)
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(timeout)
        sock.connect(os.fspath(path))
    except BaseException:
        sock.close()
        raise
    return sock


async def open_connection(path, *, limit):
    """``asyncio.open_unix_connection`` for a socket path, on every platform."""
    if not WINDOWS:
        return await asyncio.open_unix_connection(path, limit=limit)
    sock = await asyncio.to_thread(_pipe_connect, os.fspath(path), 10)
    sock.setblocking(False)
    return await asyncio.open_connection(sock=sock, limit=limit)


async def start_server(callback, path, *, limit):
    """``asyncio.start_unix_server`` at a socket path, on every platform. On POSIX the caller
    still owns the file's mode; on Windows the pipe is created owner-only."""
    if not WINDOWS:
        return await asyncio.start_unix_server(callback, path, limit=limit)
    return _PipeServer(callback, os.fspath(path), limit, asyncio.get_running_loop())


def identity(path):
    """What names this server's socket file, so a daemon can tell whether the path still
    leads to it: ``(st_dev, st_ino)`` on POSIX, the marker's bytes on Windows (ipc.rs
    ``socket_file_identity``). Raises OSError when the file is gone."""
    if WINDOWS:
        with open(path, "rb") as marker:
            return marker.read()
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino


def _pipe_connect(path, timeout):
    name = _PIPE_PREFIX + path
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            handle = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                                        _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
            break
        except OSError as error:
            if error.winerror == _ERROR_FILE_NOT_FOUND:
                if os.path.exists(path):
                    raise ConnectionRefusedError(f"nothing is listening at {path}") from None
                raise FileNotFoundError(_ERROR_FILE_NOT_FOUND, "no socket at this path", path) from None
            if error.winerror != _winapi.ERROR_PIPE_BUSY:
                raise
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"the server at {path} did not accept in time")
        wait_ms = 100 if remaining is None else max(1, min(100, int(remaining * 1000)))
        try:
            _winapi.WaitNamedPipe(name, wait_ms)
        except OSError:
            pass
    ours, theirs = socket.socketpair()
    _Bridge(handle, theirs)
    ours.settimeout(timeout)
    return ours


def _security_attributes():
    """SECURITY_ATTRIBUTES for an owner-only pipe; the returned pair must outlive the pipe."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(_OWNER_ONLY, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    return attributes, descriptor


class _Bridge:
    """Two threads copying between a pipe handle and one end of a socketpair. Either side
    ending ends both: a pipe has no half-close, and no caller here needs one."""

    def __init__(self, handle, sock):
        self.handle, self.sock = handle, sock
        self.stopped = _overlapped.CreateEvent(None, True, False, None)
        self._lock = threading.Lock()
        self._alive = 2
        threading.Thread(target=self._pipe_to_socket, name="pipe-read", daemon=True).start()
        threading.Thread(target=self._socket_to_pipe, name="pipe-write", daemon=True).start()

    def _wait(self, overlapped):
        """True when the operation finished, False when the bridge was stopped first."""
        if _winapi.WaitForMultipleObjects([overlapped.event, self.stopped], False, _INFINITE) != 0:
            overlapped.cancel()
            try:
                overlapped.GetOverlappedResult(True)
            except OSError:
                pass
            return False
        return True

    def _pipe_to_socket(self):
        try:
            while True:
                overlapped, _ = _winapi.ReadFile(self.handle, _BUFFER, overlapped=True)
                if not self._wait(overlapped):
                    return
                overlapped.GetOverlappedResult(True)
                self.sock.sendall(overlapped.getbuffer())
        except OSError:
            return
        finally:
            self._finish()

    def _socket_to_pipe(self):
        try:
            while data := self.sock.recv(_BUFFER):
                view = memoryview(data)
                while view:
                    overlapped, _ = _winapi.WriteFile(self.handle, view, overlapped=True)
                    if not self._wait(overlapped):
                        return
                    written, _ = overlapped.GetOverlappedResult(True)
                    view = view[written:]
        except OSError:
            return
        finally:
            self._finish()

    def _finish(self):
        _overlapped.SetEvent(self.stopped)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        with self._lock:
            self._alive -= 1
            last = self._alive == 0
        if last:
            self.sock.close()
            _winapi.CloseHandle(self.handle)
            _winapi.CloseHandle(self.stopped)


class _PipeServer:
    """The accept loop of a named-pipe server: one listening instance at a time on a thread,
    each connected instance bridged to a socketpair whose other end becomes asyncio streams
    for ``callback``. The first instance is created before this returns, so a name another
    server holds fails here (FILE_FLAG_FIRST_PIPE_INSTANCE), as a bind would."""

    def __init__(self, callback, path, limit, loop):
        self._callback, self._limit, self._loop = callback, limit, loop
        self._name = _PIPE_PREFIX + path
        self._attributes, self._descriptor = _security_attributes()
        self._closed = _overlapped.CreateEvent(None, True, False, None)
        self._done = asyncio.Event()
        self._tasks = set()
        try:
            first = self._instance(first=True)
        except OSError as error:
            if error.winerror == _ERROR_ACCESS_DENIED:
                raise OSError(f"another server is already listening at {path}") from None
            raise
        with open(path, "w", encoding="utf-8") as marker:
            marker.write(f"{os.getpid()}:{time.time_ns()}")
        threading.Thread(target=self._accept, args=(first,), name="pipe-accept", daemon=True).start()

    def _instance(self, *, first=False):
        flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        return _winapi.CreateNamedPipe(
            self._name, flags, _winapi.PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            _winapi.PIPE_UNLIMITED_INSTANCES, _BUFFER, _BUFFER, 0, ctypes.addressof(self._attributes))

    def _accept(self, handle):
        try:
            while True:
                overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
                if _winapi.WaitForMultipleObjects([overlapped.event, self._closed], False, _INFINITE) != 0:
                    overlapped.cancel()
                    _winapi.CloseHandle(handle)
                    return
                try:
                    overlapped.GetOverlappedResult(True)
                except OSError:
                    _winapi.CloseHandle(handle)
                    handle = self._instance()
                    continue
                connected, handle = handle, self._instance()
                ours, theirs = socket.socketpair()
                _Bridge(connected, theirs)
                self._loop.call_soon_threadsafe(self._serve, ours)
        except (OSError, RuntimeError):
            return
        finally:
            try:
                self._loop.call_soon_threadsafe(self._done.set)
            except RuntimeError:
                pass

    def _serve(self, sock):
        task = self._loop.create_task(self._client(sock))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _client(self, sock):
        sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=sock, limit=self._limit)
        result = self._callback(reader, writer)
        if asyncio.iscoroutine(result):
            await result

    def close(self):
        _overlapped.SetEvent(self._closed)

    async def wait_closed(self):
        await self._done.wait()
