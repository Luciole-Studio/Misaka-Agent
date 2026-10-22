"""quickjs-wasi: QuickJS-NG compiled to WebAssembly, with the API pi's codemode worker drives it through.

Translated from quickjs-wasi 3.6.2 (https://github.com/vercel-labs/quickjs-wasi, tag ``quickjs-wasi@3.6.2``),
``src/index.ts`` and ``src/wasi-shim.ts``. ``quickjs.wasm`` beside this file is the npm package's binary, byte for
byte (sha256 ``d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9``); it is QuickJS-NG 0.17.0 built
with wasi-sdk 32, whose licences are collected in ``LICENSES/quickjs-wasi.txt``.

MISAKA fork:

- Only what pi's ``packages/codemode/src/runtime/vm.ts`` uses is here: ``QuickJS.create`` with a memory limit, a
  stack limit, an interrupt handler, WASI overrides, ``intrinsics`` (``Intrinsics``) and ``timezoneOffset``;
  ``versions``, ``_getExports()``, ``newFunction``, ``newString``, ``newNumber``, ``newError``, the
  ``undefined``/``true``/``false`` singletons, ``evalCode``, ``callFunction``, ``getException``,
  ``executePendingJobs`` and ``withScope``; and on handles ``getProp``, ``setProp``, ``toString``, ``toNumber``,
  ``toBoolean``, ``isUndefined``, ``consume``, ``dup`` and ``dispose``. No snapshots, extensions, module loaders or
  promise helpers; the imports that serve them answer as upstream does when no handler is set.
- The host's WebAssembly is wasmtime (``engine()``), and the VM is synchronous: ``create`` is not awaited. A VM
  belongs to the thread that created it.
- Host callbacks receive the arguments only; upstream also binds the ``this`` handle, which nothing here reads.
- ``newError`` given a Python exception takes its class name as the error's name and leaves out its traceback.
- Calls into wasm go through the C API's unchecked call (``_UncheckedCall``) where wasmtime-py has it, and
  ``toString()`` has the length written to one slot per VM instead of four bytes allocated per call: a call through
  wasmtime-py's ``Func.__call__`` costs about 8 µs, and every ``text()`` a script makes costs several.

MIT License

Copyright (c) 2026 Vercel, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import math
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import wasmtime

VERSION = "3.6.2"
# The file in the installed package (pi resolves `quickjs-wasi/quickjs.wasm`).
WASM_PATH = Path(__file__).with_name("quickjs.wasm")

MAX_STACK_SIZE = 512 * 1024


class Intrinsics:
    """Intrinsic flags for ``QuickJS.create(intrinsics=...)`` controlling which built-in JavaScript features are
    available in the VM.

    By default all intrinsics are enabled. Pass a bitmask of these flags to create a minimal context. For example,
    omit ``Intrinsics.EVAL`` to prevent ``eval()`` usage, or omit ``Intrinsics.PROXY`` to disallow ``Proxy``.

    ``BaseObjects`` (Object, Array, Number, String, Boolean, Error, etc.) is always included and cannot be disabled.
    """

    # `Date` constructor and prototype methods.
    DATE = 1 << 0
    # `eval()` and `Function()` constructor.
    EVAL = 1 << 1
    # `RegExp` constructor, prototype methods, and regex literals.
    REGEXP = 1 << 2
    # `JSON.parse()` and `JSON.stringify()`.
    JSON = 1 << 3
    # `Proxy` and `Reflect`.
    PROXY = 1 << 4
    # `Map`, `Set`, `WeakMap`, `WeakSet`.
    MAP_SET = 1 << 5
    # `ArrayBuffer`, `TypedArray` variants, `DataView`.
    TYPED_ARRAYS = 1 << 6
    # `Promise`, `async`/`await`.
    PROMISE = 1 << 7
    # `BigInt`. Note: BigInt is part of BaseObjects in quickjs-ng and cannot be fully removed.
    BIG_INT = 1 << 8
    # `WeakRef` and `FinalizationRegistry`.
    WEAK_REF = 1 << 9
    # `performance.now()`.
    PERFORMANCE = 1 << 10
    # `DOMException` class.
    DOM_EXCEPTION = 1 << 11
    # `atob()` and `btoa()` global functions. Also pulls in `DOMException` as a dependency (errors thrown by these
    # functions are `DOMException`s).
    ATOB_BTOA = 1 << 12
    # All intrinsics enabled (default).
    ALL = 0xFFFFFFFF

# MISAKA fork: native stack the wasm frames may use. wasmtime's default (512 KiB) runs out before QuickJS's own guard
# (MAX_STACK_SIZE, measured on the wasm shadow stack) in recursion that passes through native functions -- getters,
# Proxy traps, Array callbacks -- which then traps instead of throwing a catchable RangeError; Node's worker threads
# give wasm about 4 MiB. 2 MiB is as far as wasmtime-py goes: a limit above its async stack size (2 MiB, which Python
# cannot change) makes `Engine()` panic and abort the process. A thread that runs a VM needs a larger stack than this.
MAX_WASM_STACK = 2 * 1024 * 1024

_engine: wasmtime.Engine | None = None
_engineLock = threading.Lock()
# MISAKA fork: wasmtime-py keeps every host function of every store in one process-wide table (`wasmtime._func.FUNCTIONS`)
# whose allocate and free are not atomic. Creating a VM's host functions and closing a VM's store (which frees them) run
# on the worker threads of different executions at once -- a run's result no longer waits for its worker to end -- so
# both hold this lock; unguarded, an allocation can pick a slot another thread is freeing and fail the VM's creation.
_hostFunctionsLock = threading.Lock()


def _wasmtime() -> Any:
    import wasmtime

    return wasmtime


def engine() -> wasmtime.Engine:
    """MISAKA fork: the process's wasmtime engine, the host's ``WebAssembly``. Modules and VMs must share it."""
    global _engine
    with _engineLock:
        if _engine is None:
            config = _wasmtime().Config()
            config.max_wasm_stack = MAX_WASM_STACK
            _engine = _wasmtime().Engine(config)
            atexit.register(_closeEngine)
        return _engine


def _closeEngine() -> None:
    """Release the engine while wasmtime-py's bindings still exist; left to interpreter teardown, its finalizer finds
    them gone and reports a TypeError. Stores and modules hold the engine themselves."""
    global _engine
    with _engineLock:
        if _engine is not None:
            _engine.close()
            _engine = None


def compileModule(source: bytes) -> wasmtime.Module:
    """MISAKA fork: ``WebAssembly.compile(bytes)`` on ``engine()``."""
    return _wasmtime().Module(engine(), source)


# ---- Memory ----


def _u32(value: int) -> int:
    """A wasm ``i32`` as the unsigned address it stands for (wasmtime hands them over signed)."""
    return value & 0xFFFFFFFF


def _i32(value: int) -> int:
    """An unsigned 32-bit value as the signed ``i32`` wasmtime takes."""
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value >= 0x8000_0000 else value


class Memory:
    """MISAKA fork: the instance's linear memory with the ``DataView`` reads and writes upstream does on
    ``memory.buffer`` (little-endian). Addresses may be given as wasmtime passes them. It is made before the instance,
    so the WASI functions can close over it, and attached to the instance's memory once there is one (upstream's
    memory proxy). Reads and writes copy straight from and to the memory's current base address, which moves when the
    memory grows, so it is asked for every time."""

    def __init__(self, store: wasmtime.Store) -> None:
        self._store = store
        self._memory: wasmtime.Memory | None = None

    def attach(self, memory: wasmtime.Memory) -> None:
        self._memory = memory

    @property
    def size(self) -> int:
        """The memory's size in bytes."""
        assert self._memory is not None
        return self._memory.data_len(self._store)

    def _address(self, ptr: int, length: int) -> int:
        assert self._memory is not None
        start = _u32(ptr)
        if length < 0 or start + length > self._memory.data_len(self._store):
            raise IndexError(f"wasm memory access out of bounds: {start}+{length}")
        return ctypes.addressof(self._memory.data_ptr(self._store).contents) + start

    def read(self, ptr: int, length: int) -> bytes:
        return ctypes.string_at(self._address(ptr, length), length) if length else b""

    def write(self, data: bytes, ptr: int) -> None:
        if data:
            ctypes.memmove(self._address(ptr, len(data)), data, len(data))

    def getUint32(self, ptr: int) -> int:
        return struct.unpack("<I", self.read(ptr, 4))[0]

    def setUint8(self, ptr: int, value: int) -> None:
        self.write(struct.pack("<B", value), ptr)

    def setUint16(self, ptr: int, value: int) -> None:
        self.write(struct.pack("<H", value), ptr)

    def setUint32(self, ptr: int, value: int) -> None:
        self.write(struct.pack("<I", value), ptr)

    def setBigUint64(self, ptr: int, value: int) -> None:
        self.write(struct.pack("<Q", value), ptr)


# ---- WASI ----

# WASI error codes
ERRNO_SUCCESS = 0
ERRNO_BADF = 8
ERRNO_NOSYS = 52
# Clock IDs
CLOCK_REALTIME = 0
CLOCK_MONOTONIC = 1

type WasiFunction = Callable[..., int]


def createWasiShim(memory: Memory) -> dict[str, WasiFunction]:
    """The subset of WASI snapshot_preview1 that QuickJS needs: clocks for ``Date.now()`` and seeding, ``fd_write``
    for the runtime's own logging, stubs for the other descriptors, and ``random_get``."""

    def clock_time_get(clockId: int, _precision: int, resultPtr: int) -> int:
        if clockId in (CLOCK_REALTIME, CLOCK_MONOTONIC):
            # `BigInt(Date.now()) * 1000000n`: millisecond precision, as upstream.
            memory.setBigUint64(resultPtr, int(time.time() * 1000) * 1_000_000)
            return ERRNO_SUCCESS
        return ERRNO_NOSYS

    def fd_write(fd: int, iovsPtr: int, iovsLen: int, nwrittenPtr: int) -> int:
        if fd not in (1, 2):
            return ERRNO_BADF
        totalWritten = 0
        for i in range(iovsLen):
            bufPtr = memory.getUint32(iovsPtr + i * 8)
            bufLen = memory.getUint32(iovsPtr + i * 8 + 4)
            text = memory.read(bufPtr, bufLen).decode("utf-8", "replace")
            (sys.stdout if fd == 1 else sys.stderr).write(text)
            totalWritten += bufLen
        memory.setUint32(nwrittenPtr, totalWritten)
        return ERRNO_SUCCESS

    def fd_close(_fd: int) -> int:
        return ERRNO_NOSYS

    def fd_fdstat_get(fd: int, statPtr: int) -> int:
        if fd in (1, 2):
            memory.setUint8(statPtr, 2)  # fs_filetype = CHARACTER_DEVICE
            memory.setUint16(statPtr + 2, 0)  # fs_flags
            memory.setBigUint64(statPtr + 8, 0)  # fs_rights_base
            memory.setBigUint64(statPtr + 16, 0)  # fs_rights_inheriting
            return ERRNO_SUCCESS
        return ERRNO_BADF

    def fd_seek(_fd: int, _offset: int, _whence: int, _resultPtr: int) -> int:
        return ERRNO_NOSYS

    def random_get(bufPtr: int, bufLen: int) -> int:
        memory.write(os.urandom(bufLen), bufPtr)
        return ERRNO_SUCCESS

    return {
        "clock_time_get": clock_time_get,
        "fd_write": fd_write,
        "fd_close": fd_close,
        "fd_fdstat_get": fd_fdstat_get,
        "fd_seek": fd_seek,
        "random_get": random_get,
    }


# ---- Strings ----

_LONE_SURROGATE = re.compile(r"(?:[\ud800-\udbff](?![\udc00-\udfff]))|(?:(?<![\ud800-\udbff])[\udc00-\udfff])")


def stringKeyNeedsValuePath(key: str) -> bool:
    return "\0" in key or _LONE_SURROGATE.search(key) is not None


def encodeWtf8(text: str) -> bytes:
    """A string as WTF-8: a surrogate pair is one 4-byte sequence and a lone surrogate the 3-byte sequence QuickJS's
    tolerant decoder accepts, so a string with lone surrogates reaches the guest intact."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        # A Python string may hold a pair as two code points; UTF-16 joins them, and keeps the lone ones.
        joined = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
        return joined.encode("utf-8", "surrogatepass")


def decodeWtf8(data: bytes) -> str:
    """WTF-8 bytes as a string: the 3-byte sequences quickjs writes for lone surrogates come back as those code units."""
    try:
        return data.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


# ---- JSException ----


class JSException(Exception):
    """An exception thrown from QuickJS code. ``handle`` is a live handle to the exception value and must be disposed
    when done; ``name``, ``message`` and ``stack`` are read eagerly, so they survive it."""

    def __init__(self, handle: JSValueHandle) -> None:
        msgHandle = handle.getProp("message")
        try:
            name = handle.getProp("name").consume(lambda h: "Error" if h.isUndefined else h.toString())
            message = handle.toString() if msgHandle.isUndefined else msgHandle.toString()
            stack = handle.getProp("stack").consume(lambda h: None if h.isUndefined else h.toString())
        finally:
            msgHandle.dispose()
        super().__init__(message)
        self.handle = handle
        self.name: str = name
        self.message: str = message
        self.stack: str | None = stack

    def dispose(self) -> None:
        self.handle.dispose()


# ---- QuickJS ----


class Scope(Protocol):
    def escape(self, handle: JSValueHandle) -> JSValueHandle: ...


type HostCallback = Callable[..., JSValueHandle]
type WasiOverrides = Callable[[Memory], Mapping[str, WasiFunction]]
type TimezoneOffset = str | float | Callable[[int], float]


def _hostTimezoneOffset(timeSecs: int) -> float:
    """``-new Date(timeSecs * 1000).getTimezoneOffset() * 60``: the host's UTC offset in seconds at that instant.
    MISAKA fork: Python reads ``TZ`` when ``time.tzset()`` runs, not on every call as Node does; an instant the
    platform's ``localtime`` cannot represent gets offset 0, where JavaScript's Date would give NaN (also 0)."""
    try:
        return time.localtime(timeSecs).tm_gmtoff
    except (OverflowError, OSError, ValueError):
        return 0


class _Scope:
    def __init__(self, tracked: dict[JSValueHandle, None], enclosing: dict[JSValueHandle, None] | None) -> None:
        self._tracked = tracked
        self._enclosing = enclosing

    def escape(self, handle: JSValueHandle) -> JSValueHandle:
        self._tracked.pop(handle, None)
        if self._enclosing is not None:
            self._enclosing[handle] = None
        return handle


def _uncheckedCalls() -> Any:
    """MISAKA fork: wasmtime-py's ctypes bindings of the C API's unchecked call, or None when this wasmtime-py does
    not have them where 49.x keeps them. ``Func.__call__`` re-reads the function's type and converts every value on
    each call, about 8 µs, nine times the unchecked call; a ``text()`` from a script costs a dozen calls."""
    try:
        import wasmtime
        from wasmtime import _ffi, _func

        parts = (_ffi.wasmtime_func_call_unchecked, _ffi.wasmtime_val_raw_t, _ffi.wasm_trap_t, _func.maybe_raise_last_exn,
                 wasmtime.Trap._from_ptr, wasmtime.WasmtimeError._from_ptr, wasmtime.Store._context)
    except (ImportError, AttributeError):
        return None
    return parts


def _kind(valType: Any) -> str:
    wasmtime_ = _wasmtime()
    for kind in ("i32", "i64", "f32", "f64"):
        if valType == getattr(wasmtime_.ValType, kind)():
            return kind
    raise TypeError(f"quickjs.wasm export takes or returns {valType}, which the unchecked call does not pass")


class _UncheckedCall:
    """One export called through ``wasmtime_func_call_unchecked``: the arguments and the result share one buffer,
    reused across calls (a nested call of the same export has read its arguments before the outer one writes them,
    and writes its result before the outer one returns)."""

    def __init__(self, function: wasmtime.Func, store: wasmtime.Store, unchecked: Any) -> None:
        (self._call, valRaw, self._trapType, self._raiseLast, self._trap, self._error, _context) = unchecked
        funcType = function.type(store)
        self._kinds = [_kind(param) for param in funcType.params]
        results = funcType.results
        self._result = _kind(results[0]) if results else None
        self._length = max(len(self._kinds), len(results), 1)
        self._buffer = (valRaw * self._length)()
        self._context = store._context()
        self._function = function
        self._func = ctypes.byref(function._func)

    def __call__(self, *args: Any) -> Any:
        buffer = self._buffer
        for index, kind in enumerate(self._kinds):
            setattr(buffer[index], kind, args[index])
        trap = ctypes.POINTER(self._trapType)()
        error = self._call(self._context, self._func, buffer, self._length, ctypes.byref(trap))
        if error:
            raise self._error(error)
        if trap:
            raised = self._trap(trap)
            self._raiseLast()  # a Python exception a host function raised, as `Func.__call__` re-raises it
            raise raised
        return None if self._result is None else getattr(buffer[0], self._result)


class _Exports:
    """The instance's exports, each bound to the store (upstream calls ``instance.exports.<name>(...)``)."""

    def __init__(self, instance: wasmtime.Instance, store: wasmtime.Store) -> None:
        self._exports = instance.exports(store)
        self._store = store
        self._unchecked = _uncheckedCalls()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        function = self._exports[name]
        store = self._store

        def checked(*args: Any) -> Any:
            return function(store, *args)

        call: Callable[..., Any] = checked
        if self._unchecked is not None:
            with contextlib.suppress(AttributeError, TypeError):  # a wasmtime-py that keeps them elsewhere
                call = _UncheckedCall(function, store, self._unchecked)
        setattr(self, name, call)
        return call


class QuickJS:
    """One QuickJS runtime and context in a wasm instance of its own."""

    def __init__(self, module: wasmtime.Module) -> None:
        self.module = module
        self.hostCallbacks: dict[str, HostCallback] = {}
        self.interruptHandler: Callable[[], bool] | None = None
        self.timezoneOffsetHandler: Callable[[int], float] | None = None
        self.disposed = False
        # The innermost active `withScope()` batch, if any; new non-singleton handles register here.
        self._activeScope: dict[JSValueHandle, None] | None = None
        self._versions: dict[str, str] | None = None
        self._undefined: JSValueHandle | None = None
        self._true: JSValueHandle | None = None
        self._false: JSValueHandle | None = None
        self._e: _Exports
        self._memory: Memory
        # MISAKA fork: where `toString()` has the string's length written; upstream allocates four bytes each time.
        self._lengthSlot = 0

    # ---- Cached property accessors ----

    @property
    def versions(self) -> dict[str, str]:
        """Version information for the runtime and loaded native libraries. Always includes ``"quickjs-wasi"`` (the
        npm package version) and ``"quickjs"`` (the QuickJS engine version)."""
        self.assertNotDisposed()
        if self._versions is None:
            self._versions = {
                "quickjs-wasi": VERSION,
                "quickjs": self.readCString(self._e.qjs_get_quickjs_version()),
            }
        return self._versions

    @property
    def undefined(self) -> JSValueHandle:
        """The undefined value. Cached; do not dispose."""
        if self._undefined is None:
            self._undefined = JSValueHandle(self, self._e.qjs_get_undefined(), True)
        return self._undefined

    @property
    def true(self) -> JSValueHandle:
        """The true value. Cached; do not dispose."""
        if self._true is None:
            self._true = JSValueHandle(self, self._e.qjs_get_true(), True)
        return self._true

    @property
    def false(self) -> JSValueHandle:
        """The false value. Cached; do not dispose."""
        if self._false is None:
            self._false = JSValueHandle(self, self._e.qjs_get_false(), True)
        return self._false

    @staticmethod
    def create(
        *,
        wasm: wasmtime.Module,
        memoryLimit: int | None = None,
        maxStackSize: int | None = None,
        interruptHandler: Callable[[], bool] | None = None,
        wasi: WasiOverrides | None = None,
        intrinsics: int | None = None,
        timezoneOffset: TimezoneOffset | None = None,
    ) -> QuickJS:
        """A fresh VM from the compiled ``quickjs.wasm``. ``memoryLimit`` and ``maxStackSize`` are in bytes; the
        interrupt handler is polled while code runs and ends it with an uncatchable error when it returns true;
        ``wasi`` returns WASI functions that replace the built-in ones; ``intrinsics`` is a bitmask of
        ``Intrinsics`` flags (default: all).

        ``timezoneOffset`` controls the timezone ``Date`` uses: ``"host"`` (default) mirrors the host's timezone;
        a number is a fixed UTC offset in minutes, with ``getTimezoneOffset()``'s sign (west of UTC is positive);
        a callback is called with seconds since the epoch and returns the offset in minutes for that instant."""
        vm = QuickJS(wasm)
        vm._instantiate(wasi)
        try:
            # Initialize the WASI reactor
            vm._e._initialize()
            # Initialize QuickJS runtime and context
            result = vm._e.qjs_init2(_i32(intrinsics)) if intrinsics is not None else vm._e.qjs_init()
            if result != 0:
                raise RuntimeError("Failed to initialize QuickJS runtime")
            vm._lengthSlot = vm._e.wasm_malloc(4)
            if vm._lengthSlot == 0:
                raise MemoryError("wasm_malloc failed")
            if memoryLimit is not None:
                vm._e.qjs_set_memory_limit(memoryLimit)
            if maxStackSize is not None:
                vm._e.qjs_set_max_stack_size(maxStackSize)
            if interruptHandler is not None:
                vm.interruptHandler = interruptHandler
                vm._e.qjs_set_interrupt_handler(1)
            # Configure timezone handler.
            # The internal handler always returns the UTC offset in *seconds* (positive east of UTC), which is what
            # libc's __secs_to_zone expects.
            tz = "host" if timezoneOffset is None else timezoneOffset
            if callable(tz):
                # User callback returns minutes (getTimezoneOffset convention: positive west of UTC). Convert to
                # seconds with sign flip.
                vm.timezoneOffsetHandler = lambda timeSecs: -tz(timeSecs) * 60
            elif isinstance(tz, int | float):
                # Fixed offset in minutes, convert to seconds with sign flip.
                offsetSecs = -tz * 60
                vm.timezoneOffsetHandler = lambda _timeSecs: offsetSecs
            else:
                # 'host' (default): use the host's timezone
                vm.timezoneOffsetHandler = _hostTimezoneOffset
        except BaseException:
            # MISAKA fork: a VM that failed to start closes its store now. The bound host functions keep it alive
            # in wasmtime's function table, so the garbage collector would never free its linear memory.
            vm.dispose()
            raise
        return vm

    def _instantiate(self, wasi: WasiOverrides | None) -> None:
        wasmtime_ = _wasmtime()
        store = wasmtime_.Store(engine())
        linker = wasmtime_.Linker(engine())
        memory = Memory(store)
        # Builtins plus the caller's overrides
        wasiShim = {**createWasiShim(memory), **(wasi(memory) if wasi is not None else {})}
        env: dict[str, Callable[..., Any]] = {
            "host_call": self._handleHostCall,
            "host_interrupt": self._hostInterrupt,
            "host_promise_rejection": self._hostPromiseRejection,
            "host_module_normalize": self._hostModuleNormalize,
            "host_module_load": self._hostModuleLoad,
            "host_get_timezone_offset": self._hostGetTimezoneOffset,
        }
        imports = {"env": env, "wasi_snapshot_preview1": wasiShim}
        try:
            with _hostFunctionsLock:
                for item in self.module.imports:
                    function = imports.get(item.module, {}).get(item.name or "")
                    if function is None:
                        raise RuntimeError(
                            f"quickjs.wasm imports {item.module}.{item.name}, which this host does not provide"
                        )
                    linker.define(store, item.module, item.name, wasmtime_.Func(store, item.type, function))
            instance = linker.instantiate(store, self.module)
            self._e = _Exports(instance, store)
            memory.attach(instance.exports(store)["memory"])
        except BaseException:
            # MISAKA fork: as in `create`, a store whose instance never came up is closed now.
            with _hostFunctionsLock:
                store.close()
            raise
        self._memory = memory

    # ---- Imports ----

    def _hostInterrupt(self) -> int:
        return 1 if self.interruptHandler is not None and self.interruptHandler() else 0

    def _hostPromiseRejection(self, promisePtr: int, reasonPtr: int, _isHandled: int) -> None:
        # No handler registered; free the heap-allocated values
        self._e.qjs_free_value(promisePtr)
        self._e.qjs_free_value(reasonPtr)

    def _hostModuleNormalize(self, _baseNamePtr: int, namePtr: int) -> int:
        # No normalize handler; return a copy of the specifier as-is
        return self._writeString(self.readCString(namePtr))[0]

    def _hostModuleLoad(self, _namePtr: int, _outLenPtr: int) -> int:
        return 0

    def _hostGetTimezoneOffset(self, hi: int, lo: int) -> int:
        """Receives the time as split i32 (hi, lo) and returns the UTC offset in seconds."""
        timeSecs = (hi << 32) | _u32(lo)
        if self.timezoneOffsetHandler is None:
            return 0
        offset = self.timezoneOffsetHandler(timeSecs)
        # A JS number crosses into the i32 result as ToInt32 makes it: NaN and infinities become 0.
        return _i32(int(offset)) if math.isfinite(offset) else 0

    def _handleHostCall(self, namePtr: int, nameLen: int, _thisPtr: int, argc: int, argvPtr: int) -> int:
        """Called from WASM when a host function is invoked from QuickJS code."""
        name = self._memory.read(namePtr, nameLen).decode("utf-8", "replace")
        callback = self.hostCallbacks.get(name)
        if callback is None:
            # Throw inside the guest: a function whose callback is gone fails loudly.
            errHandle = self.newError(f'Host callback "{name}" is not registered: it was unregistered, '
                                      "its ephemeral function handle was disposed, or it was never "
                                      "re-registered after a snapshot restore.")
            self._e.qjs_throw(errHandle.ptr)
            errHandle.dispose()
            return 0
        # The argv entries are owned by the C trampoline, which frees them after this call returns: borrowed
        # handles, which neither dispose() nor a scope frees. A callback that keeps one must dup() it.
        args: list[JSValueHandle] = []
        if argc > 0 and argvPtr != 0:
            for argPtr in struct.unpack(f"<{argc}I", self._memory.read(argvPtr, argc * 4)):
                args.append(JSValueHandle(self, _i32(argPtr), False, True))
        try:
            result = callback(*args)
            return self._e.qjs_dup_value(result.ptr)
        except Exception as err:  # noqa: BLE001 - thrown into the guest, as upstream
            # Throw an exception inside QuickJS and return NULL to signal to the C trampoline that an exception
            # was thrown.
            errHandle = self.newError(err)
            self._e.qjs_throw(errHandle.ptr)
            errHandle.dispose()
            return 0

    # ---- Strings ----

    def _writeString(self, text: str) -> tuple[int, int]:
        """Write a string into WASM memory as WTF-8, returning ``(pointer, length)``. Caller must free."""
        encoded = encodeWtf8(text)
        ptr = self._e.wasm_malloc(len(encoded) + 1)
        if ptr == 0:
            raise MemoryError("wasm_malloc failed")
        self._memory.write(encoded + b"\0", ptr)
        return ptr, len(encoded)

    def readCString(self, ptr: int) -> str:
        """Read a null-terminated C string from WASM memory."""
        start = _u32(ptr)
        data = self._memory.read(start, self._memory.size - start)
        end = data.find(b"\0")
        return (data if end < 0 else data[:end]).decode("utf-8", "replace")

    # ---- Public API ----

    def throwIfException(self, result: JSValueHandle) -> JSValueHandle:
        """Raise a :class:`JSException` when ``result`` is the exception marker; otherwise return it."""
        if self._e.qjs_is_exception(result.ptr) != 0:
            exc = self.getException()
            result.dispose()
            raise JSException(exc)
        return result

    def evalCode(self, code: str, filename: str = "<eval>", flags: int = 0) -> JSValueHandle:
        """Evaluate JavaScript code and return the result as a handle. If the code throws, a :class:`JSException`
        is raised on the host side."""
        codePtr, codeLen = self._writeString(code)
        fnPtr, _ = self._writeString(filename)
        resultPtr = self._e.qjs_eval(codePtr, codeLen, fnPtr, flags)
        self._e.wasm_free(codePtr)
        self._e.wasm_free(fnPtr)
        return self.throwIfException(JSValueHandle(self, resultPtr))

    def executePendingJobs(self) -> int:
        """Execute all pending microtask jobs (promise reactions, etc.). Returns the number of jobs executed."""
        count = 0
        while self._e.qjs_is_job_pending():
            if self._e.qjs_execute_pending_job() < 0:
                exc = self.getException()
                raise RuntimeError(f"Job execution error: {exc.toString()}")
            count += 1
        return count

    def newString(self, text: str) -> JSValueHandle:
        """Create a new QuickJS string value."""
        ptr, length = self._writeString(text)
        resultPtr = self._e.qjs_new_string(ptr, length)
        self._e.wasm_free(ptr)
        return JSValueHandle(self, resultPtr)

    def newNumber(self, num: float) -> JSValueHandle:
        """Create a new QuickJS number value."""
        return JSValueHandle(self, self._e.qjs_new_number(float(num)))

    def newFunction(self, name: str, fn: HostCallback) -> JSValueHandle:
        """Create a new QuickJS function backed by a host callback, which is called with the arguments as
        handles and returns a handle."""
        if name in self.hostCallbacks:
            raise ValueError(f'Host callback with name "{name}" is already registered')
        self.hostCallbacks[name] = fn
        namePtr, nameLen = self._writeString(name)
        resultPtr = self._e.qjs_new_host_function(namePtr, nameLen, 0)
        self._e.wasm_free(namePtr)
        return JSValueHandle(self, resultPtr)

    def withScope[T](self, fn: Callable[[Scope], T]) -> T:
        """Run ``fn`` with a handle scope: every handle created during the call is disposed when it returns, except
        those passed to ``scope.escape()``, which go to the enclosing scope when there is one. Handles a host
        callback is passed are not tracked; the ones it creates are."""
        enclosing = self._activeScope
        tracked: dict[JSValueHandle, None] = {}
        self._activeScope = tracked
        try:
            return fn(_Scope(tracked, enclosing))
        finally:
            self._activeScope = enclosing
            for handle in tracked:
                handle.dispose()

    def callFunction(self, func: JSValueHandle, thisVal: JSValueHandle, *args: JSValueHandle) -> JSValueHandle:
        """Call a QuickJS function. If the function throws, a :class:`JSException` is raised on the host side."""
        return self.throwIfException(self.callFunctionRaw(func, thisVal, *args))

    def callFunctionRaw(self, func: JSValueHandle, thisVal: JSValueHandle, *args: JSValueHandle) -> JSValueHandle:
        """Call a QuickJS function without raising on an exception."""
        argc = len(args)
        argvPtr = 0
        if argc > 0:
            argvPtr = self._e.wasm_malloc(argc * 4)
            self._memory.write(b"".join(struct.pack("<I", _u32(arg.ptr)) for arg in args), argvPtr)
        resultPtr = self._e.qjs_call(func.ptr, thisVal.ptr, argc, argvPtr)
        if argvPtr:
            self._e.wasm_free(argvPtr)
        return JSValueHandle(self, resultPtr)

    def setProp(self, obj: JSValueHandle, key: JSValueHandle, value: JSValueHandle) -> None:
        """Set a property on an object, by a key handle."""
        self._e.qjs_set_prop_value(obj.ptr, key.ptr, value.ptr)

    def getProp(self, obj: JSValueHandle, key: JSValueHandle) -> JSValueHandle:
        """Get a property from an object, by a key handle."""
        return JSValueHandle(self, self._e.qjs_get_prop_value(obj.ptr, key.ptr))

    def getException(self) -> JSValueHandle:
        """Get the current exception, if any."""
        return JSValueHandle(self, self._e.qjs_get_exception())

    def newError(self, messageOrError: str | BaseException) -> JSValueHandle:
        """Create a new QuickJS Error object from a message or a host exception."""
        errHandle = JSValueHandle(self, self._e.qjs_new_error())
        message = messageOrError if isinstance(messageOrError, str) else str(messageOrError)
        msgHandle = self.newString(message)
        errHandle.setProp("message", msgHandle)
        msgHandle.dispose()
        if isinstance(messageOrError, BaseException):
            nameHandle = self.newString(type(messageOrError).__name__)
            errHandle.setProp("name", nameHandle)
            nameHandle.dispose()
        return errHandle

    def assertNotDisposed(self) -> None:
        if self.disposed:
            raise RuntimeError("QuickJS instance has been disposed")

    # ---- Internal accessors for JSValueHandle ----

    def _getExports(self) -> _Exports:
        """@internal The instance's exports."""
        return self._e

    def dispose(self) -> None:
        """Release the VM. Handles disposed afterwards free nothing. MISAKA fork: the store is closed at once, which
        frees the wasm instance and its linear memory; upstream drops its references and leaves the memory to the
        garbage collector, which here would wait for a collection of the VM's reference cycles."""
        if self.disposed:
            return
        self.disposed = True
        self._undefined = self._true = self._false = None
        self.hostCallbacks.clear()
        self.interruptHandler = None
        self.timezoneOffsetHandler = None
        self._activeScope = None
        store = self._e._store
        del self._e, self._memory
        with _hostFunctionsLock:
            store.close()


# ---- JSValue Handle ----


class JSValueHandle:
    """A handle to a JSValue inside the QuickJS WASM instance.

    A ``singleton`` (``undefined``, ``true``, ``false``) is shared, so disposing it does nothing. A ``borrowed``
    handle wraps a value owned by the C caller (a host callback's arguments), which frees it itself: disposing it
    does nothing either, and no scope tracks it."""

    __slots__ = ("borrowed", "disposed", "ptr", "singleton", "vm")

    def __init__(self, vm: QuickJS, ptr: int, singleton: bool = False, borrowed: bool = False) -> None:
        self.vm = vm
        self.ptr = ptr
        self.singleton = singleton
        self.borrowed = borrowed
        self.disposed = False
        if not singleton and not borrowed and vm._activeScope is not None:
            vm._activeScope[self] = None

    @property
    def isUndefined(self) -> bool:
        return self.vm._e.qjs_is_undefined(self.ptr) != 0

    def getProp(self, name: str) -> JSValueHandle:
        """Get a property by name."""
        if stringKeyNeedsValuePath(name):
            # A NUL or lone surrogate in the key cannot cross the C-string API; go through a guest string key.
            keyHandle = self.vm.newString(name)
            try:
                return self.vm.getProp(self, keyHandle)
            finally:
                keyHandle.dispose()
        namePtr, _ = self.vm._writeString(name)
        resultPtr = self.vm._e.qjs_get_prop_string(self.ptr, namePtr)
        self.vm._e.wasm_free(namePtr)
        return JSValueHandle(self.vm, resultPtr)

    def setProp(self, name: str, value: JSValueHandle) -> None:
        """Set a property by name."""
        if stringKeyNeedsValuePath(name):
            keyHandle = self.vm.newString(name)
            try:
                self.vm.setProp(self, keyHandle, value)
            finally:
                keyHandle.dispose()
            return
        namePtr, _ = self.vm._writeString(name)
        self.vm._e.qjs_set_prop_string(self.ptr, namePtr, value.ptr)
        self.vm._e.wasm_free(namePtr)

    def toNumber(self) -> float:
        """Extract the value as a number."""
        return self.vm._e.qjs_get_float64(self.ptr)

    def toBoolean(self) -> bool:
        """Extract the value as a boolean, applying JavaScript truthiness."""
        return self.vm._e.qjs_get_bool(self.ptr) != 0

    def toString(self) -> str:
        """The value as a string, read length-aware: embedded U+0000 code units and lone surrogates survive."""
        e = self.vm._e
        lenPtr = self.vm._lengthSlot
        cstrPtr = e.qjs_get_string_len(self.ptr, lenPtr)
        if cstrPtr == 0:
            return "<null>"
        text = decodeWtf8(self.vm._memory.read(cstrPtr, self.vm._memory.getUint32(lenPtr)))
        e.qjs_free_cstring(cstrPtr)
        return text

    def consume[T](self, fn: Callable[[JSValueHandle], T]) -> T:
        """Use this handle, then dispose it. Returns the callback's return value."""
        try:
            return fn(self)
        finally:
            self.dispose()

    def dup(self) -> JSValueHandle:
        """Duplicate this handle (increment refcount)."""
        return JSValueHandle(self.vm, self.vm._e.qjs_dup_value(self.ptr))

    def dispose(self) -> None:
        """Dispose this handle, freeing the heap-allocated JSValue."""
        if self.singleton or self.borrowed:
            return
        if not self.disposed:
            self.disposed = True
            # If the VM is already disposed, the WASM instance is gone; there is nothing to free.
            if not self.vm.disposed:
                self.vm._e.qjs_free_value(self.ptr)


__all__ = [
    "MAX_STACK_SIZE",
    "MAX_WASM_STACK",
    "VERSION",
    "WASM_PATH",
    "Intrinsics",
    "JSException",
    "JSValueHandle",
    "Memory",
    "QuickJS",
    "Scope",
    "compileModule",
    "createWasiShim",
    "decodeWtf8",
    "encodeWtf8",
    "engine",
]
