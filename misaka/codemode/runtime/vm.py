"""Drives one QuickJS VM through one script, independent of the transport that connects it to the host. The worker
thread runs the VM through this module, so the bridge between prelude and host exists once.

Contract with the caller:

- ``post`` receives every message for the host, synchronously from inside VM callbacks. The caller must not re-enter
  the VM from ``post`` (no ``deliver``, no ``dispose``); a worker posts to another thread.
- ``deliver`` hands the VM a tool result and runs the script until it waits again. It must only be called while the
  VM is idle, which holds for any caller that never re-enters from ``post``.
- ``shouldInterrupt`` is polled by QuickJS while the script runs, and here between batches of promise jobs. Returning
  true throws an uncatchable error in the script; the VM then reports ``crash`` or a failed ``done``. The job batches
  matter for a script such as ``while (true) await null``: every iteration is a separate job that executes only a few
  opcodes, so QuickJS's own polling, which counts opcodes, would let it run for a long time between polls.
- The VM never decides on its own when it is finished: after ``done`` or ``crash`` it stays alive until the caller
  disposes it (or terminates the worker that hosts it).

Failures after the VM exists are reported as a ``crash`` message; ``startVm`` only raises when the VM cannot be
created at all.

Deterministic mode (``data["deterministic"]``, used by ``CodemodeSandboxBase.start()``): a script must make the same
calls with the same arguments every time it runs with the same results, so a host can replay it after a restart. The
VM is otherwise deterministic, because it only runs when the host delivers something and has no timers. What remains
is fixed here:

- Time: every WASI clock reads virtual time, ``startedAt`` and then the ``now`` of the result being processed, never
  going backwards. While the context is created, the realtime clock reads a value derived from the seed instead,
  because QuickJS seeds ``Math.random()`` from the first clock read, and which read comes first is the engine's
  business.
- Randomness: ``random_get`` draws from a PRNG over the seed.
- Timezone: UTC, not the host's.
- Garbage collection: ``WeakRef`` and ``FinalizationRegistry`` are not available; collection timing is not.
- Delivery order: results carry a ``seq`` and are applied strictly in that order, buffering early ones, because a
  transport (RPC replies) may deliver them in another order than the host sent them.

The VM reports ``started`` with its fingerprint before the script runs, and ``idle`` with the number of calls issued
after each drain, so the host can check a replay step by step.

Translated from pi's ``packages/codemode/src/runtime/vm.ts``.

MISAKA fork: the in-thread launcher and the remote server of upstream are not ported (the worker thread is the only
host of a VM), and ``startVm`` is synchronous, as ``QuickJS.create`` is in misaka's quickjs-wasi port.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from misaka.utils.js import UNDEFINED, jsonStringify, jsRound, jsString, jsTrimEnd

from ..quickjs_wasi import (
    MAX_STACK_SIZE,
    Intrinsics,
    JSException,
    JSValueHandle,
    Memory,
    QuickJS,
)
from ..types import CodemodeOutputItem
from ..wasm import CodemodeWasmModule
from .prelude_source import PRELUDE_SOURCE
from .protocol import HostToWorkerMessage, VmStartData, WorkerToHostMessage


class VmSession(Protocol):
    def deliver(self, message: HostToWorkerMessage) -> None: ...

    def dispose(self) -> None:
        """Releases the VM's wasm instance. The session must not be used afterwards."""
        ...


@dataclass(slots=True, kw_only=True)
class StartVmOptions:
    data: VmStartData
    wasm: CodemodeWasmModule
    post: Callable[[WorkerToHostMessage], None]
    shouldInterrupt: Callable[[], bool]
    # QuickJS's guard on its own stack, which turns deep recursion into a catchable `RangeError`. The wasm frames also
    # use the host thread's native stack, so the guard must trip before that stack runs out: a host overflow fails the
    # whole execution instead. Default: `MAX_STACK_SIZE` (512 KiB), which fits the 4 MB stack of a worker thread.
    maxStackSize: int | None = None
    # Interrupt polls this VM may use, if the launcher counts them; part of the fingerprint.
    interruptBudget: int | None = None


# Bumped whenever this module or the prelude changes what a deterministic script observes, beyond what the
# fingerprint's other parts (engine versions, prelude hash, limits) already capture.
DETERMINISTIC_VERSION = 1


def fnv1a(text: str) -> str:
    """FNV-1a over UTF-16 code units, as 8 hex digits: identifies the prelude in the fingerprint."""
    hash = 0x811C9DC5
    encoded = text.encode("utf-16-le", "surrogatepass")
    for index in range(0, len(encoded), 2):
        hash ^= encoded[index] | (encoded[index + 1] << 8)
        # `Math.imul(hash, 0x01000193)`, kept as the unsigned value `hash >>> 0` prints.
        hash = (hash * 0x01000193) & 0xFFFFFFFF
    return f"{hash:08x}"


PRELUDE_HASH = fnv1a(PRELUDE_SOURCE)


def _imul(a: int, b: int) -> int:
    """``Math.imul(a, b) >>> 0``: the low 32 bits of the product."""
    return (a * b) & 0xFFFFFFFF


def prng(seed: int) -> Callable[[], int]:
    """mulberry32: a small, fast PRNG; one 32-bit draw per call."""
    state = seed & 0xFFFFFFFF

    def next_() -> int:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        value = _imul(state ^ (state >> 15), 1 | state)
        value = ((value + _imul(value ^ (value >> 7), 61 | value)) & 0xFFFFFFFF) ^ value
        return (value ^ (value >> 14)) & 0xFFFFFFFF

    return next_


@dataclass(slots=True)
class VirtualClock:
    """The deterministic VM's clock: seed-derived while the context is created, then virtual time."""

    creating: bool
    startedAt: float
    now: float
    seed: int


def deterministicWasi(clock: VirtualClock) -> Callable[[Memory], dict[str, Callable[..., int]]]:
    """WASI clock and random functions for deterministic mode, on top of ``discardOutput``."""
    random = prng(clock.seed ^ 0x9E3779B9)

    def wasi(memory: Memory) -> dict[str, Callable[..., int]]:
        def clock_time_get(clockId: int, _precision: int, resultPtr: int) -> int:
            # QuickJS seeds Math.random() from this reading in microseconds and turns 0 into 1, so seed s reads s + 1.
            if clock.creating:
                nanoseconds = (clock.seed + 1) * 1000 if clockId == 0 else 0
            elif clockId == 0:
                nanoseconds = jsRound(clock.now) * 1_000_000
            else:
                nanoseconds = jsRound(clock.now - clock.startedAt) * 1_000_000
            # `setBigUint64` stores the value modulo 2**64.
            memory.setBigUint64(resultPtr, nanoseconds & 0xFFFF_FFFF_FFFF_FFFF)
            return 0

        def random_get(bufPtr: int, bufLen: int) -> int:
            data = bytearray(bufLen)
            for index in range(0, bufLen, 4):
                value = random()
                for byte in range(4):
                    if index + byte >= bufLen:
                        break
                    data[index + byte] = (value >> (byte * 8)) & 0xFF
            memory.write(bytes(data), bufPtr)
            return 0

        return {**discardOutput(memory), "clock_time_get": clock_time_get, "random_get": random_get}

    return wasi


def discardOutput(memory: Memory) -> dict[str, Callable[..., int]]:
    """QuickJS writes engine diagnostics to fd 1 and 2, which the default shim forwards to the host's stdout and
    stderr. That output belongs to the host application (for example a TUI), so it is discarded. Reporting every byte
    as written keeps libc from retrying."""

    def fd_write(_fd: int, iovsPtr: int, iovsLen: int, nwrittenPtr: int) -> int:
        written = 0
        for i in range(iovsLen):
            written += memory.getUint32(iovsPtr + i * 8 + 4)
        memory.setUint32(nwrittenPtr, written)
        return 0

    return {"fd_write": fd_write}


def describeException(error: JSException) -> str:
    head = f"{error.name}: {error.message}" if error.message else error.name
    stack = jsTrimEnd(error.stack) if error.stack is not None else None
    return jsonStringify({"name": error.name, "message": error.message, "stack": f"{head}\n{stack}" if stack else head})


# Promise jobs run between two `shouldInterrupt` polls while draining the job queue.
JOBS_PER_POLL = 50


class _Error(Exception):
    """JavaScript's ``Error``, for the failures this module raises itself: crash messages name it ``Error``, as
    upstream's ``new Error(...)`` does."""

    name = "Error"


def crashMessage(error: BaseException) -> WorkerToHostMessage:
    """``${error.name}: ${error.message}``. MISAKA fork: a wasm trap's text is its cause; the backtrace above it is
    wasmtime's, so only its last line is kept."""
    name = error.name if isinstance(error, JSException | _Error) else type(error).__name__
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return {"type": "crash", "message": f"{name}: {lines[-1] if lines else ''}"}


class _Session:
    """``VmSession`` of ``startVm``: delivery in order (deterministic mode) and disposal."""

    def __init__(self, vm: QuickJS, options: StartVmOptions, clock: VirtualClock | None) -> None:
        self._vm = vm
        self._post = options.post
        self._shouldInterrupt = options.shouldInterrupt
        self._clock = clock
        self._disposed = False
        # Calls the script has issued, tools and globals; reported with every `idle`.
        self.issued = 0
        # Deterministic mode: the next result to apply, and results that arrived before it.
        self._nextSeq = 1
        # The result being processed, 0 during the script's first run; reported with `done`.
        self.current = 0
        self._early: dict[Any, HostToWorkerMessage] = {}
        # Set by `startVm` once the prelude has run.
        self.api: JSValueHandle | None = None
        self.settle: JSValueHandle | None = None
        self.stalled: JSValueHandle | None = None
        # `executePendingJobs()` runs the queue to empty without a chance to interrupt, so jobs run one at a time
        # through the (internal, but public) exports; quickjs-wasi is pinned to an exact version.
        self._exports = vm._getExports()

    def apply(self, message: HostToWorkerMessage) -> None:
        vm = self._vm
        properties = message.get("properties")
        vm.withScope(
            lambda _scope: vm.callFunction(
                self.settle,
                self.api,
                vm.newNumber(message["id"]),
                vm.true if message["ok"] else vm.false,
                vm.undefined if message["payload"] is None else vm.newString(message["payload"]),
                vm.undefined if properties is None else vm.newString(properties),
            )
        )
        self.drain()

    def drain(self) -> None:
        """Run queued jobs, then fail a script that waits on nothing that can ever resume it."""
        exports = self._exports
        jobs = 0
        while exports.qjs_is_job_pending():
            # A job only fails for uncatchable errors, such as an interrupt.
            if exports.qjs_execute_pending_job() < 0:
                raise _Error("Promise job failed")
            jobs += 1
            if jobs % JOBS_PER_POLL == 0 and self._shouldInterrupt():
                raise _Error("Execution interrupted")
        self._vm.callFunction(self.stalled, self.api).dispose()

    def deliver(self, message: HostToWorkerMessage) -> None:
        if self._disposed:
            return
        try:
            clock = self._clock
            if clock is None:
                self.apply(message)
                return
            seq = message.get("seq", UNDEFINED)
            if seq is UNDEFINED or not _isInteger(seq) or seq < self._nextSeq or seq in self._early:
                raise _Error(
                    f"Result {jsString(message['id'])} has sequence number {jsString(seq)}, "
                    f"expected {self._nextSeq} or later, once"
                )
            self._early[seq] = message
            next_ = self._early.get(self._nextSeq)
            while next_ is not None:
                del self._early[self._nextSeq]
                # Never backwards: a resumed host's clock may be behind the one that started the run.
                now = next_.get("now")
                if _isNumber(now) and math.isfinite(now):
                    clock.now = max(clock.now, now)
                self.current = self._nextSeq
                self.apply(next_)
                self._post({"type": "idle", "seq": self._nextSeq, "issued": self.issued})
                self._nextSeq += 1
                next_ = self._early.get(self._nextSeq)
        except Exception as error:  # noqa: BLE001 - upstream `catch (error) { post(crashMessage(error)) }`
            self._post(crashMessage(error))

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._vm.dispose()


def _isNumber(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _isInteger(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    return _isNumber(value) and math.isfinite(value) and float(value).is_integer()


def startVm(options: StartVmOptions) -> VmSession:
    data, post = options.data, options.post
    maxStackSize = options.maxStackSize if options.maxStackSize is not None else MAX_STACK_SIZE
    deterministic = data.get("deterministic")
    clock = (
        VirtualClock(
            creating=True,
            startedAt=deterministic["startedAt"],
            now=deterministic["startedAt"],
            seed=int(deterministic["seed"]) & 0xFFFFFFFF,
        )
        if deterministic
        else None
    )
    vm = QuickJS.create(
        wasm=options.wasm,
        memoryLimit=data["memoryLimitBytes"],
        # Without a guard, deep recursion overflows the wasm stack and traps instead of throwing a
        # catchable RangeError.
        maxStackSize=maxStackSize,
        interruptHandler=options.shouldInterrupt,
        wasi=deterministicWasi(clock) if clock else discardOutput,
        **({"timezoneOffset": 0, "intrinsics": Intrinsics.ALL & ~Intrinsics.WEAK_REF} if clock else {}),
    )
    if clock:
        clock.creating = False
    session = _Session(vm, options, clock)

    try:
        # Called from the prelude with primitives only. A missing argument is `None` (`undefined` upstream).
        # MISAKA fork: each argument is read out of the VM once; a read costs a few calls into wasm.
        def bridgeCallback(kind: JSValueHandle, a: Any = None, b: Any = None, c: Any = None) -> JSValueHandle:
            match kindText := kind.toString():
                case "call" | "global":
                    session.issued += 1
                    post({
                        "type": "call",
                        "id": int(a.toNumber()),
                        "target": "tool" if kindText == "call" else "global",
                        "name": b.toString(),
                        "args": None if c is None or c.isUndefined else c.toString(),
                    })
                case "output":
                    match a.toString():
                        case "image":
                            item: CodemodeOutputItem = {"type": "image", "data": b.toString(), "mimeType": c.toString()}
                        case "console":
                            item = {"type": "text", "text": b.toString(), "console": True}
                        case _:
                            item = {"type": "text", "text": b.toString()}
                    post({"type": "output", "item": item})
                case "done":
                    # The host stops the VM on `done`, so the `idle` of this drain never arrives: report its numbers
                    # here.
                    during: dict[str, Any] = (
                        {"during": {"seq": session.current, "issued": session.issued}} if clock else {}
                    )
                    if a.toBoolean():
                        post({
                            "type": "done",
                            "ok": True,
                            "value": None if b is None or b.isUndefined else b.toString(),
                            "writes": c.toString(),
                            **during,
                        })
                    else:
                        post({"type": "done", "ok": False, "error": b.toString(), **during})
            return vm.undefined

        bridge = vm.newFunction("bridge", bridgeCallback)

        # These handles live as long as the VM, which the caller disposes as a whole.
        session.api = vm.withScope(
            lambda scope: scope.escape(
                vm.callFunction(
                    vm.evalCode(PRELUDE_SOURCE, "codemode-prelude.js"),
                    vm.undefined,
                    bridge,
                    vm.newString(jsonStringify(data["tools"])),
                    vm.newString(jsonStringify(data["globals"])),
                    vm.newString(jsonStringify(data["store"])),
                )
            )
        )
        session.settle = session.api.getProp("settle")
        run = session.api.getProp("run")
        session.stalled = session.api.getProp("stalled")
        if clock:
            versions = vm.versions
            budget = "none" if options.interruptBudget is None else jsString(options.interruptBudget)
            heap = "none" if data["memoryLimitBytes"] is None else jsString(data["memoryLimitBytes"])
            post({
                "type": "started",
                "fingerprint": (
                    f"quickjs-wasi {versions['quickjs-wasi']}, quickjs {versions['quickjs']}, prelude {PRELUDE_HASH}, "
                    f"deterministic {DETERMINISTIC_VERSION}, stack {maxStackSize}, heap {heap}, budget {budget}"
                ),
            })

        # The prefix shares the first line with the script so reported line numbers
        # match the script as written.
        try:
            fn = vm.evalCode(f"(async (tools, console) => {{{data['code']}\n}})", "codemode.js")
        except JSException as error:
            post({"type": "done", "ok": False, "error": describeException(error)})
            return session
        vm.callFunction(run, session.api, fn).dispose()
        fn.dispose()
        session.drain()
        if clock:
            post({"type": "idle", "seq": 0, "issued": session.issued})
    except Exception as error:  # noqa: BLE001 - upstream `catch (error) { post(crashMessage(error)) }`
        post(crashMessage(error))
    return session


__all__ = [
    "DETERMINISTIC_VERSION",
    "JOBS_PER_POLL",
    "PRELUDE_HASH",
    "StartVmOptions",
    "VirtualClock",
    "VmSession",
    "crashMessage",
    "describeException",
    "deterministicWasi",
    "discardOutput",
    "fnv1a",
    "prng",
    "startVm",
]
