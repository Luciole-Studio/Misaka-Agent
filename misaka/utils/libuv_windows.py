"""MISAKA fork: two things libuv does for every Node process on Windows, which Python's ``subprocess`` and ``open``
do not: children go into one job that ends them when this process exits, and an append opens the file with
append-only access shared for reading, writing and deleting. Windows only."""

from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import threading
from ctypes import wintypes
from typing import Any

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_FILE_APPEND_DATA = 0x0004
_SYNCHRONIZE = 0x00100000
_READ_CONTROL = 0x00020000
_FILE_WRITE_ATTRIBUTES = 0x0100
_FILE_WRITE_EA = 0x0010
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_lock = threading.Lock()
_job: list[Any] = []


def job() -> Any:
    """libuv's global job: its processes end when this process exits, however it exits."""
    with _lock:
        if not _job:
            handle = _kernel32.CreateJobObjectW(None, None)
            if handle:
                limits = _ExtendedLimits()
                # BREAKAWAY_OK | SILENT_BREAKAWAY_OK | DIE_ON_UNHANDLED_EXCEPTION | KILL_ON_JOB_CLOSE
                limits.BasicLimitInformation.LimitFlags = 0x800 | 0x1000 | 0x400 | 0x2000
                _kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            _job.append(handle or None)
        return _job[0]


def assignToJob(process: subprocess.Popen[bytes]) -> None:
    """Put a started child into the job; a failure is ignored, as libuv ignores it."""
    handle = job()
    if handle is not None:
        _kernel32.AssignProcessToJobObject(handle, int(process._handle))  # type: ignore[attr-defined]


def openAppend(path: str) -> int:
    """A file descriptor opened as libuv opens ``"a"``: append-only, shared with every other opener."""
    access = _FILE_APPEND_DATA | _SYNCHRONIZE | _READ_CONTROL | _FILE_WRITE_ATTRIBUTES | _FILE_WRITE_EA
    handle = _kernel32.CreateFileW(path, access, _FILE_SHARE_ALL, None, _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(handle, os.O_WRONLY)


def appendFile(path: str, data: bytes) -> None:
    """``appendFileSync``: one append through a handle opened as libuv opens ``"a"``."""
    fd = openAppend(path)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


__all__ = ["appendFile", "assignToJob", "job", "openAppend"]
