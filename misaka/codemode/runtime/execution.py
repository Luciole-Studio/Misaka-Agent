"""Host side of an execution, shared by every place a VM can run.

The host owns everything the script must not influence: the tool table, call records, output, the deadline, the
caller's abort signal, and the decoding of what the VM reports. The VM itself runs somewhere a ``VmLauncher``
decides: in misaka, a worker thread (``CodemodeSandbox``). The two sides only exchange the messages in
``protocol.py``, so launchers differ in transport and termination, not in behavior.

Lifecycle of one execution:

1. The constructor arms the deadline and abort listener, then asks the launcher for a channel.
2. Messages from the VM arrive through ``VmEvents.message``; tool calls run here and their results go back through
   ``VmChannel.post``. Replies produced before the launcher has returned its channel are queued and flushed when it
   arrives.
3. The first terminal event wins: ``done``, ``crash``, a launcher failure, the deadline, or an abort. ``finish()``
   aborts pending tool calls, asks the channel to stop, and resolves the result once that request completes. A worker
   may exit later. Later events are ignored.

The launcher, not this class, is responsible for actually stopping a running VM: a worker is interrupted and
terminated.

Translated from pi's ``packages/codemode/src/runtime/execution.ts``.

MISAKA fork: results are plain dicts, as the TypeScript objects are. Upstream's in-thread (``InlineCodemodeSandbox``)
and remote (``RemoteCodemodeSandbox``) launchers are not ported: an in-thread VM would run QuickJS on the event loop's
thread and freeze every session of the process while a script computes, and misaka has no isolate to reach over RPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    Timeout,
    addAbortListener,
    jsIsInteger,
    jsonParse,
    jsonStringify,
    jsString,
    nullishCoalesce,
    setTimeout,
    spawn,
)
from misaka.utils.values import read_field

from ..identifier import toCodemodeIdentifier
from ..types import (
    CodemodeCall,
    CodemodeCallStatus,
    CodemodeDeclaredGlobal,
    CodemodeDeclaredTool,
    CodemodeDuring,
    CodemodeError,
    CodemodeExecuteOptions,
    CodemodeHostCall,
    CodemodeOutcome,
    CodemodeOutputItem,
    CodemodeResult,
    CodemodeRunOptions,
    CodemodeSandboxBaseOptions,
    CodemodeStoreWrites,
    CodemodeTool,
)
from .protocol import HostToWorkerMessage, VmStartData, isWorkerToHostMessage

DEFAULT_TIMEOUT_MS = 300_000
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*\Z")
RESERVED_GLOBALS: frozenset[str] = frozenset({
    "tools",
    "ALL_TOOLS",
    "console",
    "text",
    "image",
    "exit",
    "globalThis",
    "store",
    "load",
})


@dataclass(slots=True)
class VmEvents:
    """Callbacks a launcher uses to report what its VM does."""

    # A message from the VM. Validated here, so launchers can pass through anything they receive.
    message: Callable[[Any], None]
    # The VM failed or stopped outside the bridge, for example a worker exit.
    failure: Callable[[CodemodeError], None]


@dataclass(slots=True)
class VmChannel:
    """The host's handle on a running VM."""

    post: Callable[[HostToWorkerMessage], None]
    # Requests VM shutdown. Called once; may resolve before a worker actually exits.
    stop: Callable[[], Awaitable[None]]


# Starts a VM for one execution. Failures (raised, or of the returned awaitable) become `sandbox` errors with their
# message. The VM may start reporting messages before the returned awaitable settles.
type VmLauncher = Callable[[VmStartData, VmEvents], VmChannel | Awaitable[VmChannel]]


def _now() -> float:
    """``performance.now()``, in milliseconds."""
    return time.perf_counter() * 1000


def errorMessage(error: Any) -> str:
    if isinstance(error, BaseException):
        return read_field(error, "message", None) or str(error)
    return jsString(error)


def _stringify(value: Any) -> str | None:
    """``JSON.stringify(value)``: ``None`` where it gives ``undefined`` (``undefined`` itself and functions);
    a value that cannot be serialized (circular) throws."""
    if value is UNDEFINED or callable(value):
        return None
    return jsonStringify(value)


def serializeStore(store: Mapping[str, Any] | None) -> dict[str, str]:
    serialized: dict[str, str] = {}
    for key, value in (store or {}).items():
        json = _stringify(value)
        if json is not None:
            serialized[key] = json
    return serialized


class _BridgeError(Exception):
    """The prelude that serializes values for the host runs in the same VM as the script, so a script
    that patches built-ins (for example ``Array.prototype.toJSON``) can make it send malformed data.
    Payloads that do not decode raise this error, and the execution fails as a sandbox error."""


def _parseBridgeJson(json: str, what: str) -> Any:
    try:
        return jsonParse(json)
    except RecursionError:
        # MISAKA fork: Python's decoder gives up on values nested a few thousand levels deep, which V8's
        # decodes; that is the host's limit, not a broken bridge.
        raise
    except Exception:  # noqa: BLE001 - upstream `catch {}`
        raise _BridgeError(f"{what} is not valid JSON") from None


def parseStoreWrites(json: str) -> CodemodeStoreWrites:
    entries = _parseBridgeJson(json, "store writes")
    if not isinstance(entries, list):
        raise _BridgeError("store writes are not an array")
    writes: CodemodeStoreWrites = {"set": {}, "delete": []}
    for entry in entries:
        if (
            not isinstance(entry, list)
            or len(entry) == 0
            or not isinstance(entry[0], str)
            or (not isinstance(entry[1], str) if len(entry) == 2 else len(entry) != 1)
        ):
            raise _BridgeError("store writes contain a malformed entry")
        key = entry[0]
        if len(entry) == 1:
            writes["delete"].append(key)
        else:
            writes["set"][key] = _parseBridgeJson(entry[1], f"store value for {jsonStringify(key)}")
    return writes


def _parseScriptError(json: str) -> CodemodeError:
    parsed = _parseBridgeJson(json, "script error")
    # `typeof parsed !== "object"`: an array is an object too, and has none of the fields.
    if not isinstance(parsed, (dict, list)):
        raise _BridgeError("script error is not an object")
    fields = parsed if isinstance(parsed, dict) else {}
    name, message, stack = (fields.get(key, UNDEFINED) for key in ("name", "message", "stack"))
    if (
        not isinstance(message, str)
        or (name is not UNDEFINED and not isinstance(name, str))
        or (stack is not UNDEFINED and not isinstance(stack, str))
    ):
        raise _BridgeError("script error is malformed")
    error: CodemodeError = {"kind": "script", "message": message}
    if isinstance(name, str):
        error["name"] = name
    if isinstance(stack, str):
        error["stack"] = stack
    return error


@dataclass(slots=True)
class _PendingCall:
    record: CodemodeCall | None
    startedAt: float
    controller: AbortController


def checkGlobals(globals: Sequence[CodemodeDeclaredGlobal | CodemodeTool]) -> None:
    """Checks global names: identifiers or ``namespace.member``, not shadowing a built-in global or each other."""
    names: set[str] = set()
    namespaces: dict[str, None] = {}
    for global_ in globals:
        name = read_field(global_, "name")
        parts = name.split(".")
        if len(parts) > 2 or not all(IDENTIFIER.match(part) for part in parts) or parts[0] in RESERVED_GLOBALS:
            raise ValueError(f'Invalid global name "{name}"')
        if name in names:
            raise ValueError(f'Global "{name}" is already registered')
        if len(parts) == 2:
            namespaces[parts[0]] = None
        names.add(name)
    for namespace in namespaces:
        if namespace in names:
            raise ValueError(f'Global "{namespace}" conflicts with the namespace "{namespace}"')


@dataclass(slots=True)
class _Control:
    """Callbacks of a host-controlled run: its calls go to the caller, which answers them with ``deliver()``."""

    onCall: Callable[[CodemodeHostCall], None]
    onStarted: Callable[[str], None] | None = None
    onIdle: Callable[[int, int], None] | None = None


@dataclass(slots=True)
class _Executors:
    tools: Mapping[str, CodemodeTool]
    globals: Mapping[str, CodemodeTool]


@dataclass(slots=True, kw_only=True)
class _ExecutionOptions:
    code: str
    tools: Sequence[CodemodeDeclaredTool | CodemodeTool]
    globals: Sequence[CodemodeDeclaredGlobal | CodemodeTool]
    # Run the script's calls; unused when `control` is set.
    executors: _Executors
    timeoutMs: float
    signal: Any
    memoryLimitBytes: int | None
    store: dict[str, str]
    deterministic: dict[str, Any] | None = None
    control: _Control | None = None
    launch: VmLauncher


class Execution:
    """One script run in its own VM."""

    def __init__(self, options: _ExecutionOptions) -> None:
        loop = asyncio.get_running_loop()
        self.promise: asyncio.Future[CodemodeResult] = loop.create_future()
        self._channel: VmChannel | None = None
        # Replies produced before the launcher returned the channel.
        self._unsent: list[HostToWorkerMessage] = []
        self._executors = options.executors
        self._control = options.control
        # Result numbers `deliver()` used.
        self._delivered: set[float] = set()
        self._signal = options.signal
        self._timer: Timeout | None = None
        self._removeAbortListener: Callable[[], None] = lambda: None
        self._output: list[CodemodeOutputItem] = []
        self._calls: list[CodemodeCall] = []
        self._pending: dict[int, _PendingCall] = {}
        # Whether the execution has its outcome.
        self.finished = False
        self._tasks: set[asyncio.Task[Any]] = set()
        # MISAKA fork: cancelling the task that awaits the result cancels this future, which has no
        # counterpart for a promise. The run is then abandoned: stop it, and end its VM.
        self.promise.add_done_callback(self._onSettled)

        if math.isfinite(options.timeoutMs):
            self._timer = setTimeout(
                lambda: self._finish({
                    "kind": "timeout",
                    "message": f"Execution timed out after {jsString(options.timeoutMs)} ms",
                }),
                options.timeoutMs,
            )

        if options.signal is not None:
            if read_field(options.signal, "aborted"):
                self._onAbort()
            else:
                self._removeAbortListener = addAbortListener(options.signal, self._onAbort)
        if self.finished:
            return

        data: VmStartData = {
            "code": options.code,
            "tools": [
                {
                    "name": read_field(tool, "name"),
                    "jsName": nullishCoalesce(read_field(tool, "jsName"), toCodemodeIdentifier(read_field(tool, "name"))),
                    "description": nullishCoalesce(read_field(tool, "description"), ""),
                }
                for tool in options.tools
            ],
            "globals": [
                {"name": read_field(global_, "name"), "spread": read_field(global_, "spread") is True}
                for global_ in options.globals
            ],
            "memoryLimitBytes": options.memoryLimitBytes,
            "store": options.store,
        }
        if options.deterministic:
            data["deterministic"] = options.deterministic  # type: ignore[typeddict-item]
        events = VmEvents(message=self._receive, failure=lambda error: self._finish(error))
        # `new Promise((resolve) => resolve(options.launch(data, events))).then(...)`: the launcher starts at once.
        spawn(self._launch(options.launch, data, events), self._tasks)

    async def _launch(self, launch: VmLauncher, data: VmStartData, events: VmEvents) -> None:
        try:
            launched = launch(data, events)
            channel = await launched if inspect.isawaitable(launched) else launched
        except Exception as error:  # noqa: BLE001 - upstream's rejection handler
            self._finish({"kind": "sandbox", "message": errorMessage(error)})
            return
        if self.finished:
            with contextlib.suppress(Exception):  # upstream `void channel.stop().catch(() => undefined)`
                await channel.stop()
            return
        self._channel = channel
        unsent, self._unsent = self._unsent, []
        for message in unsent:
            channel.post(message)

    def deliver(self, seq: float, now: float, id: int, outcome: CodemodeOutcome) -> None:
        """``CodemodeRun.deliver()``."""
        if self.finished:
            return
        if not (isinstance(now, int | float) and not isinstance(now, bool) and math.isfinite(now)):
            raise ValueError(f"The clock value of result {jsString(seq)} is not a finite number")
        if not jsIsInteger(seq) or seq < 1 or seq in self._delivered:
            raise ValueError(f"Result number {jsString(seq)} is not a positive integer used once")
        pending = self._pending.get(id)
        if pending is None:
            raise ValueError(f"Call {jsString(id)} is not waiting for a result")
        properties_ = outcome.get("properties", UNDEFINED) if not outcome["ok"] else UNDEFINED
        if properties_ is not UNDEFINED and not isinstance(properties_, dict):
            raise ValueError(f"The properties of result {jsString(seq)} are not an object")
        # Serialized before anything is recorded, so a value JSON cannot hold leaves the call waiting for another answer.
        if outcome["ok"]:
            value = outcome.get("value", UNDEFINED)
            payload = None if value is UNDEFINED else jsonStringify(value)
        else:
            payload = outcome["message"]
        properties = None if properties_ is UNDEFINED else jsonStringify(properties_)
        self._delivered.add(seq)
        del self._pending[id]
        if pending.record is not None:
            pending.record["status"] = "ok" if outcome["ok"] else "error"
            pending.record["durationMs"] = _now() - pending.startedAt
        message: HostToWorkerMessage = {"type": "result", "id": id, "ok": outcome["ok"], "payload": payload, "seq": seq, "now": now}
        if properties is not None:
            message["properties"] = properties
        self._post(message)

    def abort(self, message: str) -> asyncio.Future[CodemodeResult]:
        self._finish({"kind": "aborted", "message": message})
        return self.promise

    def _onSettled(self, future: asyncio.Future[CodemodeResult]) -> None:
        if future.cancelled():
            self._finish({"kind": "aborted", "message": "Execution cancelled"})

    def _onAbort(self) -> None:
        reason = read_field(self._signal, "reason") if self._signal is not None else None
        self._finish({
            "kind": "aborted",
            "message": errorMessage(reason) if isinstance(reason, BaseException) else "Execution aborted",
        })

    def _post(self, message: HostToWorkerMessage) -> None:
        if self._channel is not None:
            self._channel.post(message)
        else:
            self._unsent.append(message)

    def _receive(self, message: Any) -> None:
        # An exception here would escape into the launcher's transport and leave the execution unsettled.
        try:
            self._handleMessage(message)
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: the run's failure
            self._finish({
                "kind": "sandbox",
                "message": (
                    f"Sandbox bridge broken: {error}. The script may have modified built-ins such as a prototype's toJSON."
                    if isinstance(error, _BridgeError)
                    else f"Sandbox host failed: {errorMessage(error)}"
                ),
            })

    def _handleMessage(self, message: Any) -> None:
        if self.finished:
            return
        if not isWorkerToHostMessage(message):
            raise _BridgeError("unknown message from the worker")
        match message["type"]:
            case "output":
                self._output.append(message["item"])
            case "call":
                if message["id"] in self._pending:
                    raise _BridgeError(f"duplicate call id {message['id']}")
                if self._control is not None:
                    self._forwardCall(message, self._control)
                else:
                    # Started eagerly, so the call is pending before the next message of the batch.
                    spawn(self._handleCall(message), self._tasks)
            case "started":
                if self._control is not None and self._control.onStarted is not None:
                    self._control.onStarted(message["fingerprint"])
            case "idle":
                if self._control is not None and self._control.onIdle is not None:
                    self._control.onIdle(message["seq"], message["issued"])
            case "done":
                self._handleDone(message)
            case "crash":
                self._finish({"kind": "sandbox", "message": message["message"]})

    def _handleDone(self, message: Any) -> None:
        # Decode everything before finish(), which must not throw once it starts.
        if not message["ok"]:
            self._finish(_parseScriptError(message["error"]), during=message.get("during"))
            return
        value = UNDEFINED if message["value"] is None else _parseBridgeJson(message["value"], "return value")
        self._finish(None, value, parseStoreWrites(message["writes"]), message.get("during"))

    def _forwardCall(self, message: Any, control: _Control) -> None:
        """A host-controlled run: record the call and hand it to the caller, which answers with ``deliver()``."""
        id, name, target = message["id"], message["name"], message["target"]
        args = UNDEFINED if message["args"] is None else _parseBridgeJson(message["args"], "call arguments")
        record: CodemodeCall | None = {"name": name, "status": "cancelled", "durationMs": 0} if target == "tool" else None
        if record is not None:
            self._calls.append(record)
        self._pending[id] = _PendingCall(record=record, startedAt=_now(), controller=AbortController())
        control.onCall({"id": id, "target": target, "name": name, "args": args})

    async def _handleCall(self, message: Any) -> None:
        id, name = message["id"], message["name"]
        isTool = message["target"] == "tool"
        record: CodemodeCall | None = {"name": name, "status": "cancelled", "durationMs": 0} if isTool else None
        if record is not None:
            self._calls.append(record)
        pending = _PendingCall(record=record, startedAt=_now(), controller=AbortController())
        self._pending[id] = pending

        status: CodemodeCallStatus
        reply: HostToWorkerMessage
        try:
            tool = (self._executors.tools if isTool else self._executors.globals).get(name)
            if tool is None:
                raise RuntimeError(f'Unknown {"tool" if isTool else "global"} "{name}"')
            # `undefined` is `UNDEFINED` and `null` is `None`, as everywhere in the port.
            args = UNDEFINED if message["args"] is None else jsonParse(message["args"])
            value = tool.execute(args, {"signal": pending.controller.signal})
            # `await tool.execute(...)`: anything awaitable is awaited.
            if inspect.isawaitable(value):
                value = await value
            reply = {
                "type": "result",
                "id": id,
                "ok": True,
                "payload": None if value is UNDEFINED else jsonStringify(value),
            }
            status = "ok"
        except Exception as error:  # noqa: BLE001 - the script sees the error as a rejection
            reply = {"type": "result", "id": id, "ok": False, "payload": errorMessage(error)}
            status = "error"
        except asyncio.CancelledError:
            # MISAKA fork: a tool that ends in a cancellation nobody asked this call for (it awaited a
            # future another component cancelled) rejects like any other failure; unanswered, the
            # script's promise would never settle. A cancellation of this task passes through.
            task = asyncio.current_task()
            if self.finished or (task is not None and task.cancelling() > 0):
                raise
            reply = {"type": "result", "id": id, "ok": False, "payload": "Tool call was cancelled"}
            status = "error"

        # Already cancelled by finish(): the record keeps "cancelled" and the VM is gone or going.
        if self._pending.pop(id, None) is None:
            return
        if record is not None:
            record["status"] = status
            record["durationMs"] = _now() - pending.startedAt
        self._post(reply)

    def _finish(
        self,
        error: CodemodeError | None,
        value: Any = UNDEFINED,
        writes: CodemodeStoreWrites | None = None,
        during: CodemodeDuring | None = None,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if self._timer is not None:
            self._timer.cancel()
        self._removeAbortListener()

        now = _now()
        for pending in self._pending.values():
            if pending.record is not None:
                pending.record["durationMs"] = now - pending.startedAt
            pending.controller.abort()
        self._pending.clear()
        self._unsent = []

        result: CodemodeResult
        if error is not None:
            result = {"ok": False, "error": error, "output": self._output, "calls": self._calls}
        else:
            result = {
                "ok": True,
                "value": value,
                "output": self._output,
                "calls": self._calls,
                "storeWrites": {"set": {}, "delete": []} if writes is None else writes,
            }
        if during is not None:
            result["during"] = {"seq": during["seq"], "issued": during["issued"]}
        channel = self._channel
        if channel is None:
            self._resolveResult(result)
            return

        async def stop() -> None:
            with contextlib.suppress(Exception):  # upstream `.catch(() => undefined)`
                await channel.stop()
            self._resolveResult(result)

        spawn(stop(), self._tasks)

    def _resolveResult(self, result: CodemodeResult) -> None:
        if not self.promise.done():
            self.promise.set_result(result)


class _Run:
    """``CodemodeRun`` of ``CodemodeSandboxBase.start()``."""

    def __init__(self, execution: Execution) -> None:
        self._execution = execution

    @property
    def result(self) -> asyncio.Future[CodemodeResult]:
        return self._execution.promise

    @property
    def finished(self) -> bool:
        return self._execution.finished

    def deliver(self, seq: float, now: float, id: int, outcome: CodemodeOutcome) -> None:
        self._execution.deliver(seq, now, id, outcome)


class CodemodeSandboxBase:
    """Tool table, defaults, and in-flight executions; the behavior every sandbox shares. Subclasses only choose
    where the VM runs by passing a ``VmLauncher``.

    The script sees ``tools.<name>(args)`` for every registered tool, ``ALL_TOOLS``, the output helpers ``text``,
    ``image``, ``exit``, and ``console.*``, ``store``/``load``, and the configured globals; nothing else (no timers,
    ``fetch``, ``process``, ``require``, modules). Each ``execute()`` gets its own VM; the sandbox only holds the tool
    table and defaults. ``close()`` aborts in-flight executions.
    """

    def __init__(self, options: CodemodeSandboxBaseOptions | None, launch: VmLauncher) -> None:
        options = options or {}
        self._toolsByName: dict[str, CodemodeTool] = {}
        self._globalsByName: dict[str, CodemodeTool] = {}
        self._timeoutMs: float = nullishCoalesce(options.get("timeoutMs"), DEFAULT_TIMEOUT_MS)
        self._memoryLimitBytes = options.get("memoryLimitBytes")
        self._launch = launch
        self._running: set[Execution] = set()
        self._closed = False
        for tool in options.get("tools") or []:
            self.registerTool(tool)
        checkGlobals(options.get("globals") or [])
        for global_ in options.get("globals") or []:
            self._globalsByName[global_.name] = global_

    def registerTool(self, tool: CodemodeTool) -> None:
        """Raises if a tool with the same name is already registered."""
        if tool.name in self._toolsByName:
            raise ValueError(f'Tool "{tool.name}" is already registered')
        self._toolsByName[tool.name] = tool

    def unregisterTool(self, name: str) -> bool:
        return self._toolsByName.pop(name, None) is not None

    @property
    def tools(self) -> list[CodemodeTool]:
        return list(self._toolsByName.values())

    @property
    def globals(self) -> list[CodemodeTool]:
        return list(self._globalsByName.values())

    def _track(self, execution: Execution) -> None:
        self._running.add(execution)
        execution.promise.add_done_callback(lambda _future: self._running.discard(execution))

    def execute(self, code: str, options: CodemodeExecuteOptions | None = None) -> asyncio.Future[CodemodeResult]:
        """``code`` is an async function body: ``return`` and top-level ``await`` work. The execution
        starts at once and the returned future never fails for script failures; those come back as
        ``{"ok": False}``. The script can use ``store(key, value)`` and ``load(key)`` on
        ``options["store"]``. A script fails once its output passes ``MAX_OUTPUT_CHARS`` characters of
        text and base64 image data or ``MAX_OUTPUT_ITEMS`` items, even if it catches the error. The
        future fails once the sandbox is closed."""
        if self._closed:
            rejected: asyncio.Future[CodemodeResult] = asyncio.get_running_loop().create_future()
            rejected.set_exception(RuntimeError("Sandbox is closed"))
            return rejected
        options = options or {}
        tools = dict(self._toolsByName)
        execution = Execution(_ExecutionOptions(
            code=code,
            tools=list(tools.values()),
            globals=list(self._globalsByName.values()),
            executors=_Executors(tools=tools, globals=self._globalsByName),
            timeoutMs=nullishCoalesce(options.get("timeoutMs"), self._timeoutMs),
            signal=options.get("signal"),
            memoryLimitBytes=self._memoryLimitBytes,
            store=serializeStore(options.get("store")),
            launch=self._launch,
        ))
        self._track(execution)
        return execution.promise

    def start(self, code: str, options: CodemodeRunOptions) -> _Run:
        """Run ``code`` deterministically, with the caller answering its calls: the same script, start time, seed,
        tools, globals, and store, answered with the same results in the same ``seq`` order, makes the same calls
        with the same arguments and produces the same output, on any VM with the same fingerprint. A host can
        therefore replay it after a restart. The VM runs in UTC, its clock reads ``startedAt`` and then the ``now`` of
        the result being processed, its randomness comes from ``seed``, and ``WeakRef`` and ``FinalizationRegistry``
        do not exist.

        The sandbox's own tools and globals are not used: the run declares its own, and every call goes to
        ``options["onCall"]``. Never fails for script failures; abort through ``options["signal"]`` to end the run
        with an error the script cannot catch."""
        if self._closed:
            raise RuntimeError("Sandbox is closed")
        startedAt = options["startedAt"]
        if (
            not isinstance(startedAt, int | float)
            or isinstance(startedAt, bool)
            or not math.isfinite(startedAt)
            or startedAt < 0
        ):
            raise ValueError("startedAt must be a non-negative number of milliseconds since the epoch")
        seed = options["seed"]
        if not jsIsInteger(seed) or seed < 0 or seed > 0xFFFFFFFF:
            raise ValueError("seed must be a 32-bit unsigned integer")
        tools = list(options.get("tools") or [])
        if len({tool["name"] for tool in tools}) != len(tools):
            raise ValueError("Tool names must be unique")
        scriptNames = [nullishCoalesce(tool.get("jsName"), toCodemodeIdentifier(tool["name"])) for tool in tools]
        if len(set(scriptNames)) != len(tools):
            raise ValueError("Tool script names must be unique")
        globals = list(options.get("globals") or [])
        checkGlobals(globals)
        execution = Execution(_ExecutionOptions(
            code=code,
            tools=tools,
            globals=globals,
            executors=_Executors(tools={}, globals={}),
            timeoutMs=nullishCoalesce(options.get("timeoutMs"), math.inf),
            signal=options.get("signal"),
            memoryLimitBytes=self._memoryLimitBytes,
            store=serializeStore(options.get("store")),
            deterministic={"startedAt": startedAt, "seed": int(seed)},
            control=_Control(onCall=options["onCall"], onStarted=options.get("onStarted"), onIdle=options.get("onIdle")),
            launch=self._launch,
        ))
        self._track(execution)
        return _Run(execution)

    async def close(self) -> None:
        """Aborts in-flight executions (they resolve with ``kind: "aborted"``) and rejects new ones."""
        self._closed = True
        await asyncio.gather(
            *(execution.abort("Sandbox closed") for execution in list(self._running)), return_exceptions=True
        )


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "CodemodeSandboxBase",
    "Execution",
    "VmChannel",
    "VmEvents",
    "VmLauncher",
    "checkGlobals",
    "errorMessage",
    "parseStoreWrites",
    "serializeStore",
]
