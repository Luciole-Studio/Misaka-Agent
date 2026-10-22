"""Translated from pi's ``packages/codemode/src/types.ts``.

MISAKA fork: the worker is a thread, not a file, so the ``workerUrl`` sandbox option has no counterpart.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Protocol, Required, TypedDict


class CodemodeToolContext(TypedDict):
    # Aborted when the script finishes (including unawaited calls), the execution times out, the
    # caller aborts, or the sandbox is closed.
    signal: Any


# A JSON Schema document. Only used to render declarations; values are not validated against it.
type CodemodeJsonSchema = dict[str, Any] | bool


# MISAKA fork: keyword-only, so the members keep upstream's order with the required `execute` last.
@dataclass(slots=True, kw_only=True)
class CodemodeTool:
    # The script calls tools as `tools.<id>(args)`, where `<id>` is the name with characters that
    # are not valid in identifiers replaced by `_` (see `toCodemodeIdentifier`), and also as
    # `tools["<name>"](args)`. Globals are called as `<name>(args)` and must be identifiers, or
    # `<namespace>.<member>`, which groups them into a frozen namespace object.
    name: str
    # Shown as a doc comment in `renderDeclarations`, and listed in `ALL_TOOLS` for tools.
    description: str | None = None
    # Schema of the single argument. Rendered as the parameter type; `unknown` when omitted.
    inputSchema: CodemodeJsonSchema | None = None
    # Schema of the resolved value. Rendered as the promise type; `unknown` when omitted.
    outputSchema: CodemodeJsonSchema | None = None
    # Globals only: `execute` receives all call arguments as a list instead of the first one.
    spread: bool | None = None
    # Globals only: TypeScript parameter list and return type for `renderDeclarations`, for
    # example `(type: string, id?: string): Promise<Model[]>`. Replaces the rendering from the
    # schemas.
    signature: str | None = None
    # `args` is whatever the script passed, after a JSON round trip. The return value must be
    # JSON-serializable; a raised error surfaces in the script as an `Error` with the same
    # message. May return an awaitable.
    execute: Callable[[Any, CodemodeToolContext], Any]


class CodemodeTextOutputItem(TypedDict):
    type: Literal["text"]
    text: str
    console: NotRequired[Literal[True]]


class CodemodeImageOutputItem(TypedDict):
    type: Literal["image"]
    # base64
    data: str
    mimeType: str


# One item of the script's output, in the order the script produced it: `text()` and `console.*`
# produce text items, with `console: True` for `console.*`, and `image()` image items. `data` is base64.
type CodemodeOutputItem = CodemodeTextOutputItem | CodemodeImageOutputItem

type CodemodeCallStatus = Literal["ok", "error", "cancelled"]


class CodemodeCall(TypedDict):
    name: str
    status: CodemodeCallStatus
    durationMs: float


# "script": the script threw or failed to parse; `name` and `stack` come from the script's error.
# "timeout": the overall deadline expired, or a VM on the calling thread used up its interrupt budget.
#   The execution ended and the VM was asked to stop; a worker or remote VM may still be computing.
#   (MISAKA fork: misaka has no VM on the calling thread, and a worker thread cannot be killed, so its
#   VM may always still be computing.)
# "aborted": the caller's signal fired or the sandbox was closed. The execution ended and the VM was
#   asked to stop.
# "sandbox": the VM or its transport failed outside the script's control (for example a wasm trap, a
#   worker that cannot start, or a remote sandbox that failed).
type CodemodeErrorKind = Literal["script", "timeout", "aborted", "sandbox"]


class CodemodeError(TypedDict):
    kind: CodemodeErrorKind
    name: NotRequired[str]
    message: str
    stack: NotRequired[str]


class CodemodeStoreWrites(TypedDict):
    """Keys the script changed with ``store()``. Only successful executions report writes."""

    set: dict[str, Any]
    # Keys stored as `undefined`.
    delete: list[str]


class CodemodeDuring(TypedDict):
    """In a ``CodemodeSandboxBase.start`` run: the result the VM was processing when the script finished, 0 for its
    first run, and the calls issued by then."""

    seq: int
    issued: int


class CodemodeSuccess(TypedDict):
    ok: Literal[True]
    # `exit()` completes with `value: UNDEFINED`.
    value: Any
    output: list[CodemodeOutputItem]
    calls: list[CodemodeCall]
    storeWrites: CodemodeStoreWrites
    # In a `CodemodeSandboxBase.start` run that the script itself ended (it returned, threw, or exited),
    # the result being processed when it did.
    during: NotRequired[CodemodeDuring]


class CodemodeFailure(TypedDict):
    ok: Literal[False]
    error: CodemodeError
    # Kept for failed executions too, up to the failure.
    output: list[CodemodeOutputItem]
    calls: list[CodemodeCall]
    # As `CodemodeSuccess.during`.
    during: NotRequired[CodemodeDuring]


type CodemodeResult = CodemodeSuccess | CodemodeFailure


class CodemodeSandboxBaseOptions(TypedDict, total=False):
    """Options every sandbox accepts, wherever its VM runs."""

    tools: Sequence[CodemodeTool]
    # Functions exposed as top-level identifiers instead of on `tools`, for host helpers such as
    # attaching an image to the result. They behave like tools (JSON round trip, promise result)
    # but are not recorded in `result.calls`. Names must be identifiers and may not shadow the
    # built-in globals (`tools`, `ALL_TOOLS`, `console`, `text`, `image`, `exit`, `store`, `load`).
    globals: Sequence[CodemodeTool]
    # Overall deadline per execution, including time spent in tools. `math.inf` disables the
    # deadline; the execution then only ends when the script settles or is aborted.
    # Default: 300000.
    timeoutMs: float
    # Maximum memory the QuickJS VM may allocate. Allocations beyond it fail inside the script as
    # `InternalError: out of memory`. Default: no limit beyond wasm32's 4 GiB address space.
    memoryLimitBytes: int


class CodemodeSandboxOptions(CodemodeSandboxBaseOptions, total=False):
    """Options of ``CodemodeSandbox``, which runs each VM in a worker thread."""

    # Compiled `quickjs.wasm`, usually from `loadQuickJSWasm()`, or a future of it. Default: `loadQuickJSWasm()`,
    # the file shipped with misaka.
    wasm: Any
    # Synchronously reserves capacity immediately before creating each worker, after loading wasm. Raise to deny
    # admission without starting a worker. Return a release callback, called once if worker construction fails or
    # when the worker actually exits, not when the result settles. The release callback must not raise.
    # Reservations can be shared across sandbox instances.
    # MISAKA fork: the release runs on the worker thread as it ends (upstream: on the worker's "exit" event), so
    # it must be thread-safe; it runs even when the event loop that started the worker has closed.
    acquireWorker: Callable[[], Callable[[], None]]
    # Called once for each worker started by `execute()` or `start()`. `exited` resolves when that worker actually
    # exits, which may be after the result and `close()` settle: a non-interruptible built-in can delay its end.
    # Use it to keep the worker counted against host concurrency limits until it exits. A raised error fails the
    # execution and requests worker termination.
    # MISAKA fork: `exited` is a `concurrent.futures.Future` set from the worker thread as it ends (await it with
    # `asyncio.wrap_future`), so it resolves even after the event loop that started the worker has closed.
    onWorkerStarted: Callable[[concurrent.futures.Future[None]], None]


class CodemodeHostCall(TypedDict):
    """A call the script made in a ``CodemodeSandboxBase.start`` run, answered with ``CodemodeRun.deliver()``."""

    # The VM's own number for the call, `1`, `2`, ... in the order the script made its calls (tools and globals).
    id: int
    target: Literal["tool", "global"]
    name: str
    # What the script passed, after a JSON round trip; for spread globals the argument list.
    args: Any


class CodemodeDeclaredTool(TypedDict):
    """A tool as a ``CodemodeSandboxBase.start`` run's script sees it: ``tools.<name>`` and its ``ALL_TOOLS``
    entry."""

    name: str
    # The identifier the script uses, `tools.<jsName>`. Default: `toCodemodeIdentifier(name)`. A host that replays
    # a run passes the one the run started with, so a changed mapping cannot rename a tool under a resumed script.
    jsName: NotRequired[str]
    description: NotRequired[str]


class CodemodeDeclaredGlobal(TypedDict):
    """A global as a ``CodemodeSandboxBase.start`` run's script sees it; the same names as ``globals`` allow."""

    name: str
    spread: NotRequired[bool]


class CodemodeOutcomeOk(TypedDict):
    ok: Literal[True]
    value: Any


class CodemodeOutcomeError(TypedDict):
    ok: Literal[False]
    message: str
    properties: NotRequired[dict[str, Any]]


# A call's answer. A rejection becomes an `Error` with `message` in the script, carrying the own properties of
# `properties` (JSON values; `name`, `message`, and `stack` are ignored), for example `{ structuredOutput }`.
type CodemodeOutcome = CodemodeOutcomeOk | CodemodeOutcomeError


class CodemodeRunOptions(TypedDict, total=False):
    # The VM clock's start, in ms since the epoch: `Date.now()` until the first result arrives.
    startedAt: Required[float]
    # Seeds `Math.random()` and the VM's other randomness. A 32-bit unsigned integer.
    seed: Required[int]
    tools: Sequence[CodemodeDeclaredTool]
    globals: Sequence[CodemodeDeclaredGlobal]
    store: Mapping[str, Any]
    signal: Any
    # Deadline of this run in ms. Default: none (not the sandbox's default).
    timeoutMs: float
    # The VM's fingerprint, before the script runs and before any of its calls.
    onStarted: Callable[[str], None]
    # The script made a call. Answer it with `CodemodeRun.deliver()`, now or later.
    onCall: Required[Callable[[CodemodeHostCall], None]]
    # The VM processed result `seq` (`0`: the script's first run, before any result) and waits again, having
    # issued `issued` calls in total.
    onIdle: Callable[[int, int], None]


class CodemodeRun(Protocol):
    """A running ``CodemodeSandboxBase.start`` execution."""

    @property
    def result(self) -> asyncio.Future[CodemodeResult]: ...

    @property
    def finished(self) -> bool:
        """Whether the run has its outcome: the script ended, timed out, or failed, or the run was aborted. Set when
        that is decided, before ``result`` settles; calls can no longer be answered. It does not mean the VM has
        exited."""
        ...

    def deliver(self, seq: int, now: float, id: int, outcome: CodemodeOutcome) -> None:
        """Answer call ``id`` as result number ``seq``. Results are numbered 1, 2, ... without gaps, and the VM
        applies them strictly in that order, whatever order they arrive in: after a gap it waits for the missing
        number. While it processes this one, its clock reads ``now`` (ms since the epoch), or the previous value if
        ``now`` is earlier. Raises for a call that is not waiting for an answer, a ``seq`` used before, or a ``now``
        that is not finite."""
        ...


class CodemodeExecuteOptions(TypedDict, total=False):
    signal: Any
    # Overrides the sandbox default for this execution.
    timeoutMs: float
    # Values the script reads with `load(key)`. Must be JSON-serializable. The script's own
    # `store()` calls come back as `result["storeWrites"]`; persisting them is up to the caller.
    store: Mapping[str, Any]


__all__ = [
    "CodemodeCall",
    "CodemodeCallStatus",
    "CodemodeDeclaredGlobal",
    "CodemodeDeclaredTool",
    "CodemodeDuring",
    "CodemodeError",
    "CodemodeErrorKind",
    "CodemodeExecuteOptions",
    "CodemodeFailure",
    "CodemodeHostCall",
    "CodemodeImageOutputItem",
    "CodemodeJsonSchema",
    "CodemodeOutcome",
    "CodemodeOutcomeError",
    "CodemodeOutcomeOk",
    "CodemodeOutputItem",
    "CodemodeResult",
    "CodemodeRun",
    "CodemodeRunOptions",
    "CodemodeSandboxBaseOptions",
    "CodemodeSandboxOptions",
    "CodemodeStoreWrites",
    "CodemodeSuccess",
    "CodemodeTextOutputItem",
    "CodemodeTool",
    "CodemodeToolContext",
]
