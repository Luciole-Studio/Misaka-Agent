"""Windows, following libuv (``src/win/error.c``, ``src/win/process.c``, ``src/win/fs.c``) and Node's ``child_process``."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from ..errors import Failure
from . import APPEND, READ, READ_WRITE, WRITE, File, ShellConfig, path_exists, synthetic

_kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_HANDLE = wintypes.HANDLE
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    _HANDLE,
]
_kernel32.CreateFileW.restype = _HANDLE
_kernel32.ReadFile.argtypes = [_HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [_HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenThread.restype = _HANDLE
_kernel32.CancelSynchronousIo.argtypes = [_HANDLE]
_kernel32.CancelSynchronousIo.restype = wintypes.BOOL
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = _HANDLE
_kernel32.SetInformationJobObject.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [_HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.SetFileInformationByHandle.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
_kernel32.FlushFileBuffers.argtypes = [_HANDLE]
_kernel32.FlushFileBuffers.restype = wintypes.BOOL
_kernel32.GetEnvironmentStringsW.argtypes = []
_kernel32.GetEnvironmentStringsW.restype = ctypes.c_void_p
_kernel32.FreeEnvironmentStringsW.argtypes = [ctypes.c_void_p]
_kernel32.FreeEnvironmentStringsW.restype = wintypes.BOOL

_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_FILE_GENERIC_READ = 0x120089
_FILE_GENERIC_WRITE = 0x120116
_FILE_WRITE_DATA = 0x2
_FILE_APPEND_DATA = 0x4
_CREATE_ALWAYS = 2
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_TAG_NAME_SURROGATE = 0x20000000
_ERROR_HANDLE_EOF = 38
_ERROR_BROKEN_PIPE = 109
_ERROR_FILE_EXISTS = 80
_CREATE_NO_WINDOW = 0x08000000

# libuv's `uv_translate_sys_error` for the errors file and process operations produce.
_ERROR_NAMES = {
    1: "EISDIR",  # ERROR_INVALID_FUNCTION
    2: "ENOENT",  # ERROR_FILE_NOT_FOUND
    3: "ENOENT",  # ERROR_PATH_NOT_FOUND
    15: "ENOENT",  # ERROR_INVALID_DRIVE
    123: "ENOENT",  # ERROR_INVALID_NAME
    161: "ENOENT",  # ERROR_BAD_PATHNAME
    4392: "ENOENT",  # ERROR_INVALID_REPARSE_DATA
    4: "EMFILE",  # ERROR_TOO_MANY_OPEN_FILES
    5: "EPERM",  # ERROR_ACCESS_DENIED
    1314: "EPERM",  # ERROR_PRIVILEGE_NOT_HELD
    6: "EBADF",  # ERROR_INVALID_HANDLE
    8: "ENOMEM",  # ERROR_NOT_ENOUGH_MEMORY
    14: "ENOMEM",  # ERROR_OUTOFMEMORY
    17: "EXDEV",  # ERROR_NOT_SAME_DEVICE
    19: "EROFS",  # ERROR_WRITE_PROTECT
    32: "EBUSY",  # ERROR_SHARING_VIOLATION
    33: "EBUSY",  # ERROR_LOCK_VIOLATION
    39: "ENOSPC",  # ERROR_HANDLE_DISK_FULL
    82: "ENOSPC",  # ERROR_CANNOT_MAKE
    112: "ENOSPC",  # ERROR_DISK_FULL
    50: "ENOTSUP",  # ERROR_NOT_SUPPORTED
    80: "EEXIST",  # ERROR_FILE_EXISTS
    183: "EEXIST",  # ERROR_ALREADY_EXISTS
    13: "EINVAL",  # ERROR_INVALID_DATA
    87: "EINVAL",  # ERROR_INVALID_PARAMETER
    122: "EINVAL",  # ERROR_INSUFFICIENT_BUFFER
    131: "EINVAL",  # ERROR_NEGATIVE_SEEK
    1464: "EINVAL",  # ERROR_SYMLINK_NOT_SUPPORTED
    126: "ENOENT",  # ERROR_MOD_NOT_FOUND
    145: "ENOTEMPTY",  # ERROR_DIR_NOT_EMPTY
    206: "ENAMETOOLONG",  # ERROR_FILENAME_EXCED_RANGE
    267: "ENOENT",  # ERROR_DIRECTORY (libuv maps it to ENOENT)
    998: "EFAULT",  # ERROR_NOACCESS
    740: "EACCES",  # ERROR_ELEVATION_REQUIRED
    1920: "EACCES",  # ERROR_CANT_ACCESS_FILE
    1921: "ELOOP",  # ERROR_CANT_RESOLVE_FILENAME
}

# MISAKA fork: Rust calls Win32 directly; Python's `os.read`, `os.write`, `os.ftruncate` and `os.fsync` on a descriptor go
# through the C runtime, whose errors carry only an errno (already translated from the Windows error).
_ERRNO_NAMES: dict[int, str] = {}
for _name in (
    "EPERM",
    "ENOENT",
    "EIO",
    "EBADF",
    "EAGAIN",
    "ENOMEM",
    "EACCES",
    "EBUSY",
    "EEXIST",
    "EXDEV",
    "ENOTDIR",
    "EISDIR",
    "EINVAL",
    "ENFILE",
    "EMFILE",
    "EFBIG",
    "ENOSPC",
    "EROFS",
    "EMLINK",
    "ENAMETOOLONG",
    "ENOTEMPTY",
    "ENOSYS",
):
    if hasattr(errno, _name):
        _ERRNO_NAMES.setdefault(getattr(errno, _name), _name)


def os_error_name(error: BaseException) -> str:
    """libuv's ``uv_translate_sys_error`` for the errors file and process operations produce."""
    if not isinstance(error, OSError):
        return "UNKNOWN"
    code = getattr(error, "winerror", None)
    if code is None:
        return _ERRNO_NAMES.get(error.errno, "UNKNOWN") if error.errno is not None else "UNKNOWN"
    return _ERROR_NAMES.get(code, "UNKNOWN")


PATH_SEPARATOR = "\\"


def os_path(path: str) -> str | bytes:
    """A path as the system takes it."""
    return path


def to_string(value: bytes | str) -> str:
    """What the system returned, with lone surrogates replaced (``to_string_lossy``)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", _HANDLE),
    ]


def read_at(file: File, length: int, offset: int) -> bytes:
    """``seek_read``: a ``ReadFile`` at ``offset``, which also moves the file position."""
    import msvcrt

    handle = msvcrt.get_osfhandle(file.fd)  # type: ignore[attr-defined]
    buffer = ctypes.create_string_buffer(length)
    overlapped = _OVERLAPPED()
    overlapped.Offset = offset & 0xFFFFFFFF
    overlapped.OffsetHigh = offset >> 32
    read = wintypes.DWORD(0)
    if not _kernel32.ReadFile(handle, buffer, length, ctypes.byref(read), ctypes.byref(overlapped)):
        code = ctypes.get_last_error()  # type: ignore[attr-defined]
        if code == _ERROR_HANDLE_EOF:
            return b""
        raise ctypes.WinError(code)  # type: ignore[attr-defined]
    return buffer.raw[: read.value]


# MISAKA fork: Rust's `File` calls Win32 directly; Python's `os.read`, `os.write`, `os.ftruncate` and `os.fsync` go
# through the C runtime, which turns the Windows error into an errno (ERROR_INVALID_FUNCTION, a read of a directory, into
# EINVAL instead of libuv's EISDIR). These call Win32 as Rust does.


def _handle(file: File) -> Any:
    import msvcrt

    return msvcrt.get_osfhandle(file.fd)  # type: ignore[attr-defined]


def read(file: File, length: int) -> bytes:
    """``Read::read``: one ``ReadFile`` at the current position; a broken pipe is the end."""
    buffer = ctypes.create_string_buffer(length)
    count = wintypes.DWORD(0)
    if not _kernel32.ReadFile(_handle(file), buffer, length, ctypes.byref(count), None):
        code = ctypes.get_last_error()  # type: ignore[attr-defined]
        if code == _ERROR_BROKEN_PIPE:
            return b""
        raise ctypes.WinError(code)  # type: ignore[attr-defined]
    return buffer.raw[: count.value]


def write_all(file: File, data: bytes) -> None:
    """``Write::write_all``: ``WriteFile`` until everything is written."""
    handle = _handle(file)
    view = memoryview(data)
    while view:
        chunk = bytes(view[: 1 << 30])
        count = wintypes.DWORD(0)
        if not _kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(count), None):
            raise _last_error()
        if count.value == 0:
            raise OSError(None, "failed to write whole buffer")
        view = view[count.value :]


def set_len(file: File, size: int) -> None:
    """``File::set_len``: ``SetFileInformationByHandle`` with ``FileEndOfFileInfo``."""
    end = ctypes.c_int64(size)
    if not _kernel32.SetFileInformationByHandle(_handle(file), 6, ctypes.byref(end), ctypes.sizeof(end)):
        raise _last_error()


def sync_all(file: File) -> None:
    """``File::sync_all``: ``FlushFileBuffers``."""
    if not _kernel32.FlushFileBuffers(_handle(file)):
        raise _last_error()


def mtime(metadata: os.stat_result) -> tuple[int, int]:
    """Modification time as seconds and nanoseconds since the epoch; before 1970 the seconds are negative."""
    seconds, nanoseconds = divmod(metadata.st_mtime_ns, 1_000_000_000)
    return seconds, nanoseconds


def identity(metadata: os.stat_result) -> tuple[int, int]:
    """File index numbers need a handle per file; the creation time tells a replaced file or directory apart instead."""
    created = getattr(metadata, "st_birthtime_ns", None)
    if created is None:
        created = metadata.st_ctime_ns
    # FILETIME: 100-nanosecond intervals since 1601.
    return 0, created // 100 + 116444736000000000


def _is_symlink(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and bool(
        getattr(metadata, "st_reparse_tag", 0) & _REPARSE_TAG_NAME_SURROGATE
    )


def file_type(metadata: os.stat_result) -> str:
    """``file``, ``directory`` or ``symlink``, as Rust's ``FileType`` tells them apart: a name-surrogate reparse point
    (a symbolic link or a junction) is a symbolic link."""
    if _is_symlink(metadata):
        return "symlink"
    if getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_DIRECTORY:
        return "directory"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "file"


def _variable(key: str) -> str | None:
    value = os.environ.get(key)
    return value if value else None


def tmpdir() -> str:
    """Node's ``os.tmpdir()`` on Windows."""
    path = _variable("TEMP") or _variable("TMP") or f"{_variable('SystemRoot') or _variable('windir') or ''}\\temp"
    if len(path) > 1 and path.endswith("\\") and not path.endswith(":\\"):
        path = path[:-1]
    return path


def home() -> str:
    """libuv's ``uv_os_homedir``: ``USERPROFILE`` first."""
    return os.environ.get("USERPROFILE", "")


_TEMPLATE_CHARACTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def mkdtemp(prefix: str) -> str:
    """libuv's ``fs__mkdtemp``: ``prefix`` plus six characters from ``[a-zA-Z0-9]``, retried while the name exists."""
    for _ in range(1000):
        path = prefix + "".join(_TEMPLATE_CHARACTERS[byte % 62] for byte in os.urandom(6))
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path
    raise ctypes.WinError(_ERROR_FILE_EXISTS)  # type: ignore[attr-defined]


def _from_handle(handle: Any, flags: int) -> File:
    import msvcrt

    try:
        return File(msvcrt.open_osfhandle(handle, flags | os.O_BINARY))  # type: ignore[attr-defined]
    except OSError:
        _kernel32.CloseHandle(handle)
        raise


def open_reader(path: str, no_follow: bool) -> File:
    """Windows cannot open a directory as a file and has no ``O_NOFOLLOW``: check the path first, as
    ``NodeExecutionEnv`` does there (not race-free)."""
    try:
        if no_follow and _is_symlink(os.lstat(path)):
            raise Failure("SYMLINK", "Refusing to follow a symbolic link").path(path)
    except (OSError, ValueError):
        pass
    try:
        is_directory = stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        is_directory = False
    if is_directory:
        raise Failure("EISDIR", "EISDIR: illegal operation on a directory, read").path(path)
    try:
        if "\0" in path:
            raise ValueError("path contains a NUL byte")
        handle = _kernel32.CreateFileW(path, _FILE_GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None)
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise _last_error()
        return _from_handle(handle, os.O_RDONLY)
    except (OSError, ValueError) as error:
        raise Failure.io(error, "open", path) from None


def open(path: str, mode: str) -> File:
    """libuv's ``fs__open`` for Node's modes: ``CreateFileW`` with its access, sharing and disposition, always with
    ``FILE_FLAG_BACKUP_SEMANTICS``, so a directory opens and later calls on it fail as in Node; creating a file where a
    directory exists reports ``EISDIR``."""
    if mode == READ:
        access, disposition, flags = _FILE_GENERIC_READ, _OPEN_EXISTING, os.O_RDONLY
    elif mode == WRITE:
        access, disposition, flags = _FILE_GENERIC_WRITE, _CREATE_ALWAYS, os.O_WRONLY
    elif mode == APPEND:
        access = (_FILE_GENERIC_WRITE & ~_FILE_WRITE_DATA) | _FILE_APPEND_DATA
        disposition, flags = _OPEN_ALWAYS, os.O_WRONLY
    elif mode == READ_WRITE:
        access, disposition, flags = _FILE_GENERIC_READ | _FILE_GENERIC_WRITE, _OPEN_EXISTING, os.O_RDWR
    else:
        raise ValueError(f"unknown open mode {mode}")
    if "\0" in path:
        raise ValueError("path contains a NUL byte")
    handle = _kernel32.CreateFileW(
        path,
        access,
        _FILE_SHARE_ALL,
        None,
        disposition,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        error = _last_error()
        # ERROR_FILE_EXISTS while creating means the path is a directory.
        if getattr(error, "winerror", None) == _ERROR_FILE_EXISTS and mode in (WRITE, APPEND):
            raise synthetic("EISDIR")
        raise error
    return _from_handle(handle, flags)


def realpath(path: str) -> str:
    """libuv's ``uv_fs_realpath``: the final path without the ``\\\\?\\`` prefix."""
    import nt  # type: ignore[import-not-found]

    resolved = to_string(nt._getfinalpathname(path))  # type: ignore[attr-defined]
    if resolved.startswith("\\\\?\\UNC\\"):
        return "\\\\" + resolved[len("\\\\?\\UNC\\") :]
    if resolved.startswith("\\\\?\\"):
        return resolved[len("\\\\?\\") :]
    return resolved


def rename(source: str, destination: str) -> None:
    """libuv's ``fs__rename``: ``MoveFileExW`` replacing an existing destination."""
    os.replace(source, destination)


def remove_file(path: str) -> None:
    """Node's ``rm`` on Windows: a symbolic link or junction to a directory is removed as a directory entry (libuv's
    ``fs__unlink``), and a read-only file is retried after making it writable."""
    try:
        metadata = os.lstat(path)
    except (OSError, ValueError):
        metadata = None
    if (
        metadata is not None
        and _is_symlink(metadata)
        and getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_DIRECTORY
    ):
        os.rmdir(path)
        return
    try:
        os.remove(path)
    except PermissionError:
        os.stat(path)
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.remove(path)


def read_pipe(pipe: Any, stop: threading.Event, on_data: Callable[[bytes], bool]) -> None:
    """Read ``pipe`` with blocking calls until it ends, ``on_data`` returns false, or ``stop`` is set; the pipe closes
    when this returns. ``interrupt_reader`` cancels a blocked read."""
    try:
        fd = pipe.fileno()
        while not stop.is_set():
            try:
                data = os.read(fd, 64 * 1024)
            except OSError:
                return
            if not data:
                return
            if not on_data(data):
                return
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def interrupt_reader(reader: threading.Thread) -> None:
    """Cancel a reader blocked in ``ReadFile``, so it sees ``stop`` and closes its pipe."""
    native_id = reader.native_id
    if native_id is None:
        return
    handle = _kernel32.OpenThread(0x0001, False, native_id)  # THREAD_TERMINATE
    if not handle:
        return
    _kernel32.CancelSynchronousIo(handle)
    _kernel32.CloseHandle(handle)


def _split_paths(variable: str) -> list[str]:
    """``std::env::split_paths``: entries separated by ``;``, double quotes removed and quoting ``;``."""
    paths: list[str] = []
    current: list[str] = []
    quoted = False
    for character in variable:
        if character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            paths.append("".join(current))
            current = []
        else:
            current.append(character)
    paths.append("".join(current))
    return paths


def resolve_program(program: str, cwd: str, env: dict[str, Any]) -> str:
    """libuv's ``search_path`` for an argv command: a name with a directory is tried as given, otherwise the working
    directory and then each ``PATH`` entry; a name without an extension also tries ``.com`` and ``.exe``. Node refuses
    batch files without a shell."""

    def not_found() -> Failure:
        return Failure("spawn_error", f"spawn {program} ENOENT")

    has_extension = os.path.splitext(program)[1] != ""

    def candidates(base: str) -> list[str]:
        names = [base] if has_extension else []
        for extension in ("com", "exe"):
            names.append(f"{base}.{extension}")
        return names

    found = None
    if any(character in program for character in "\\/:"):
        found = next((path for path in candidates(os.path.join(cwd, program)) if os.path.isfile(path)), None)
    else:
        path_variable = next(
            (value for key, value in env.items() if key.upper() == "PATH" and isinstance(value, str)),
            None,
        )
        if path_variable is None:
            path_variable = os.environ.get("PATH", "")
        for directory in [cwd, *_split_paths(path_variable)]:
            found = next((path for path in candidates(os.path.join(directory, program)) if os.path.isfile(path)), None)
            if found is not None:
                break
    if found is None:
        raise not_found()
    lower = found.lower()
    if lower.endswith((".bat", ".cmd")):
        raise Failure("spawn_error", f"spawn {program} EINVAL")
    return found


def is_legacy_wsl_bash(path: str) -> bool:
    """Node's ``isLegacyWslBashPath``: ``^[a-z]:\\\\windows\\\\(system32|sysnative)\\\\bash\\.exe$``, case-insensitive."""
    normalized = path.replace("/", "\\").lower()
    return (
        len(normalized) > 0
        and "a" <= normalized[0] <= "z"
        and normalized[1:] in (":\\windows\\system32\\bash.exe", ":\\windows\\sysnative\\bash.exe")
    )


def bash_config(program: str) -> ShellConfig:
    if is_legacy_wsl_bash(program):
        return ShellConfig(program, ["-s"], True)
    return ShellConfig(program, ["-c"], False)


def where_bash() -> str | None:
    try:
        output = subprocess.run(
            ["where", "bash.exe"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except OSError:
        return None
    if output.returncode != 0:
        return None
    lines = output.stdout.decode("utf-8", "replace").strip().split("\n")
    first = lines[0].strip() if lines else ""
    if not first:
        return None
    return first if path_exists(first) else None


def shell_config(shell_path: str | None) -> ShellConfig:
    """Node's shell resolution on Windows: a configured shell, Git Bash, ``where bash.exe``, or ``shell_unavailable``."""
    if shell_path is not None:
        if path_exists(shell_path):
            return bash_config(shell_path)
        raise Failure("shell_unavailable", f"Custom shell path not found: {shell_path}")
    candidates = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        directory = os.environ.get(variable)
        if directory is not None:
            candidates.append(f"{directory}\\Git\\bin\\bash.exe")
    for candidate in candidates:
        if path_exists(candidate):
            return bash_config(candidate)
    found = where_bash()
    if found is not None:
        return bash_config(found)
    searched = "".join(f"  {candidate}\n" for candidate in candidates)
    raise Failure(
        "shell_unavailable",
        "No bash shell found. Options:\n  1. Install Git for Windows: https://git-scm.com/download/win\n"
        "  2. Add your bash to PATH (Cygwin, MSYS2, etc.)\n  3. Configure an explicit shellPath\n\n"
        f"Searched Git Bash in:\n{searched.rstrip()}",
    )


def quote_argument(argument: str) -> str:
    """libuv's ``quote_cmd_arg``: how one argument is written into the command line the child splits again."""
    if argument == "":
        return '""'
    if not any(character in argument for character in ' \t"'):
        return argument
    if '"' not in argument and "\\" not in argument:
        return f'"{argument}"'
    # Walk backwards: backslashes before a quote (or the closing quote) are doubled, and quotes are escaped.
    reversed_characters: list[str] = []
    quote_hit = True
    for character in reversed(argument):
        reversed_characters.append(character)
        if quote_hit and character == "\\":
            reversed_characters.append("\\")
        elif character == '"':
            quote_hit = True
            reversed_characters.append("\\")
        else:
            quote_hit = False
    return '"' + "".join(reversed(reversed_characters)) + '"'


# Variables libuv copies from the parent into every child environment that lacks them.
REQUIRED_VARIABLES = (
    "HOMEDRIVE",
    "HOMEPATH",
    "LOGONSERVER",
    "PATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)


def vars_os() -> list[tuple[str, str]]:
    """``std::env::vars_os``: the process environment block as it is, including the ``=C:`` variables that Python's
    ``os.environ`` leaves out, with lone surrogates replaced."""
    block = _kernel32.GetEnvironmentStringsW()
    if not block:
        return []
    entries: list[tuple[str, str]] = []
    try:
        offset = 0
        while True:
            entry = ctypes.wstring_at(block + offset)
            if not entry:
                break
            offset += len(entry.encode("utf-16-le", "surrogatepass")) + 2
            position = entry.find("=", 1)
            if position == -1:
                continue
            entries.append((to_string(entry[:position]), to_string(entry[position + 1 :])))
    finally:
        _kernel32.FreeEnvironmentStringsW(block)
    return entries


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass")


def child_environment(env: dict[str, Any], inherit_env: bool) -> list[tuple[str, str]]:
    """The child environment as Node builds it on Windows: the variables in object order, sorted by UTF-16 code units,
    keeping the first of names that differ only in case; then libuv adds required variables the result lacks."""
    entries: list[list[str]] = []

    def set_variable(key: str, value: str) -> None:
        for entry in entries:
            if entry[0] == key:
                entry[1] = value
                return
        entries.append([key, value])

    if inherit_env:
        for key, value in vars_os():
            set_variable(key, value)
    for key, value in env.items():
        if isinstance(value, str):
            set_variable(key, value)
    entries.sort(key=lambda entry: _utf16(entry[0]))
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for name, value in entries:
        upper = name.upper()
        if upper in seen:
            continue
        seen.add(upper)
        result.append((name, value))
    for required in REQUIRED_VARIABLES:
        if required in seen:
            continue
        inherited = os.environ.get(required)
        if inherited is not None:
            result.append((required, inherited))
    return result


def prepare_children() -> None:
    """POSIX only: signal state for children."""


def configure(program: str, args: list[str], env: dict[str, Any], inherit_env: bool) -> dict[str, Any]:
    """Arguments quoted as libuv quotes them, Node's environment, and a hidden console: the ``subprocess.Popen``
    arguments. As Rust's ``Command``, the program name is always quoted, and the environment block is ordered by name
    without regard to case."""
    command_line = " ".join([f'"{program}"', *(quote_argument(argument) for argument in args)])
    environment = child_environment(env, inherit_env)
    environment.sort(key=lambda entry: _utf16(entry[0].upper()))
    return {
        "args": command_line,
        "executable": program,
        "env": dict(environment),
        "creationflags": _CREATE_NO_WINDOW,
    }


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_job_lock = threading.Lock()
_job_handle: list[Any] = []


def job() -> Any:
    """libuv puts every child it starts into one job that ends its processes when the daemon exits."""
    with _job_lock:
        if not _job_handle:
            handle = _kernel32.CreateJobObjectW(None, None)
            if handle:
                information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                # BREAKAWAY_OK | SILENT_BREAKAWAY_OK | DIE_ON_UNHANDLED_EXCEPTION | KILL_ON_JOB_CLOSE
                information.BasicLimitInformation.LimitFlags = 0x800 | 0x1000 | 0x400 | 0x2000
                _kernel32.SetInformationJobObject(
                    handle, 9, ctypes.byref(information), ctypes.sizeof(information)
                )  # JobObjectExtendedLimitInformation
            _job_handle.append(handle or None)
        return _job_handle[0]


def after_spawn(child: subprocess.Popen[bytes]) -> None:
    handle = job()
    if handle is not None:
        _kernel32.AssignProcessToJobObject(handle, int(child._handle))  # type: ignore[attr-defined]


def kill_tree(pid: int) -> None:
    """Node's ``killProcessTree`` on Windows: ``taskkill /F /T``, not waited for."""
    system_root = os.environ.get("SystemRoot", "C:\\Windows")
    try:
        process = subprocess.Popen(
            [f"{system_root}\\System32\\taskkill.exe", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        return
    # Reaped in the background, so the handle is not left behind.
    threading.Thread(target=process.wait, daemon=True).start()


def exit_code(returncode: int) -> int:
    """Exit codes are unsigned 32-bit values on Windows (an access violation is 3221225477)."""
    return returncode & 0xFFFFFFFF


def raw_name(name: bytes | str) -> list[int] | None:
    """Windows listings keep the file system's order; nothing sorts by raw names."""
    return None


def drive_cwds() -> dict[str, str]:
    """The per-drive working directories of the ``=C:`` variables, which Node's ``path.resolve`` uses for drive-relative
    paths."""
    result: dict[str, str] = {}
    for key, value in vars_os():
        if len(key) == 3 and key[0] == "=" and key[1].isascii() and key[1].isalpha() and key[2] == ":":
            result[key[1:].upper()] = value
    return result


def read_dir(path: str) -> Any:
    """libuv's ``scandir`` and ``opendir`` report a file as ``ENOTDIR`` (elsewhere ``ERROR_DIRECTORY`` is ``ENOENT``)."""
    try:
        return os.scandir(path)
    except OSError as error:
        if getattr(error, "winerror", None) == 267:
            raise synthetic("ENOTDIR") from None
        raise


def path_identity(path: str, metadata: os.stat_result, follow: bool) -> tuple[int, int]:
    """Volume serial number and file index of ``path``, as libuv's ``stat`` reports ``dev`` and ``ino``: the creation
    time is not enough, because NTFS gives a file created under a recently removed name the old one's creation time
    ("tunneling"). Python's ``stat`` and ``lstat`` open the file for them, as libuv does; when it could not, they are
    zero, and the metadata's creation time stands in."""
    if metadata.st_ino == 0:
        return identity(metadata)
    return metadata.st_dev, metadata.st_ino
