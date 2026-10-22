"""Messages between the host (the event loop) and the worker. Tool arguments, results, and values cross as JSON
strings: the worker passes them into and out of the QuickJS VM as strings and never builds structured values itself.

Translated from pi's ``packages/codemode/src/runtime/protocol.ts``. MISAKA fork: the interrupt flag is a
``threading.Event`` where upstream shares an ``Int32Array`` over a ``SharedArrayBuffer``.
"""

from __future__ import annotations

import threading
from typing import Any, Literal, NotRequired, TypedDict

from ..types import CodemodeDuring, CodemodeOutputItem
from ..wasm import CodemodeWasmModule


class WorkerTool(TypedDict):
    name: str
    # The identifier the script uses.
    jsName: str
    # Listed in `ALL_TOOLS`.
    description: str


class WorkerGlobal(TypedDict):
    name: str
    spread: bool


class VmDeterministic(TypedDict):
    startedAt: float
    seed: int


class VmStartData(TypedDict):
    """Everything a VM needs to run one script, independent of where it runs. Plain data, so it can cross a
    ``postMessage`` or a Workers RPC call unchanged."""

    code: str
    tools: list[WorkerTool]
    globals: list[WorkerGlobal]
    memoryLimitBytes: int | None
    # Snapshot for `load()`: key to JSON text.
    store: dict[str, str]
    # Run deterministically (`CodemodeSandboxBase.start()`): UTC, a virtual clock starting at `startedAt` (ms since
    # the epoch) that moves only with delivered results, randomness from `seed`, no `WeakRef`, results applied in
    # `seq` order, and `started`/`idle` messages.
    deterministic: NotRequired[VmDeterministic]


class WorkerData(VmStartData):
    # Compiled `quickjs.wasm`. The worker instantiates it; the compiled code is shared.
    wasm: CodemodeWasmModule
    # Set by the host before terminating the worker. The VM's interrupt handler polls it, because a thread spinning
    # in wasm cannot be stopped from outside.
    interrupt: threading.Event


# JSON-encoded `{ name?, message, stack? }` of an error thrown by the script.
type ScriptErrorJson = str


class CallMessage(TypedDict):
    type: Literal["call"]
    id: int
    target: Literal["tool", "global"]
    name: str
    args: str | None


class OutputMessage(TypedDict):
    type: Literal["output"]
    item: CodemodeOutputItem


class DoneOkMessage(TypedDict):
    type: Literal["done"]
    ok: Literal[True]
    value: str | None
    # A JSON array of `[key, json]` for `store()` and `[key]` for deletions.
    writes: str
    # Deterministic mode: the result the VM was processing when the script finished (0: its first run) and the
    # calls issued by then.
    during: NotRequired[CodemodeDuring]


class DoneErrorMessage(TypedDict):
    type: Literal["done"]
    ok: Literal[False]
    error: ScriptErrorJson
    # As `DoneOkMessage.during`.
    during: NotRequired[CodemodeDuring]


class CrashMessage(TypedDict):
    """The VM failed outside the script's control, for example a wasm trap."""

    type: Literal["crash"]
    message: str


class StartedMessage(TypedDict):
    """Deterministic mode, before the script runs: what the VM is, so a replay can refuse a different one."""

    type: Literal["started"]
    fingerprint: str


class IdleMessage(TypedDict):
    """Deterministic mode, after the script waits again: having processed result ``seq`` (0: the script's first
    run), it has issued ``issued`` calls in total."""

    type: Literal["idle"]
    seq: int
    issued: int


type WorkerToHostMessage = (
    CallMessage | OutputMessage | DoneOkMessage | DoneErrorMessage | CrashMessage | StartedMessage | IdleMessage
)


class HostToWorkerMessage(TypedDict):
    type: Literal["result"]
    id: int
    ok: bool
    # The JSON result when `ok`, otherwise the error message.
    payload: str | None
    # Deterministic mode: `seq` numbers the results from 1 in the order the VM applies them, and the VM's clock
    # reads `now` while it processes this one.
    seq: NotRequired[int]
    now: NotRequired[float]
    # Deterministic mode, `ok: False`: a JSON object whose properties the script's `Error` gets.
    properties: NotRequired[str]


def isWorkerToHostMessage(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    type = value.get("type")
    return type in ("call", "output", "done", "crash", "started", "idle")


def isHostToWorkerMessage(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "result"


__all__ = [
    "CallMessage",
    "CrashMessage",
    "DoneErrorMessage",
    "DoneOkMessage",
    "HostToWorkerMessage",
    "IdleMessage",
    "OutputMessage",
    "ScriptErrorJson",
    "StartedMessage",
    "VmDeterministic",
    "VmStartData",
    "WorkerData",
    "WorkerGlobal",
    "WorkerToHostMessage",
    "WorkerTool",
    "isHostToWorkerMessage",
    "isWorkerToHostMessage",
]
