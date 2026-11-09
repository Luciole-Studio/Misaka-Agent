"""Durable code mode: a tool that runs model-written JavaScript in a QuickJS sandbox, whose script survives a restart.

Translated from pi's ``packages/durable/src/codemode/codemode.ts`` (v1.3.0).

Problem: a script is in-memory VM state. A restart while

    const results = await Promise.all(paths.map((path) => tools.read({ path })));

waits for the last of 200 reads would lose the script and every read it made.

Solution: deterministic replay (``CodemodeSandboxBase.start()`` in ``misaka.codemode``). The VM only runs when the host
hands it a result; given the same start data, clock, seed, and results in the same order, it makes the same calls with
the same arguments. The tool is replay-safe: after a restart, ``execute()`` runs again, starts the script again from the
stored start data, and hands it the results it got before, in the order it got them, from a journal written before
each delivery. Nested tool calls reattach to their tasks by key (``c<VM call id>``), so they never run twice unless
their own replay policy reruns them; their results come from ``NestedCallDoc``. Then the script continues live.

Documents, scoped to the code mode call's task and gone when it settles:

- ``pi.codemode.run``: the start data, written once before the VM starts, and the outcome, written once when the call
  decides it (with the store writes, in one transaction), so a rerun after that returns it without a VM.
- ``pi.codemode.journal``: one entry per delivered result, in delivery order: the VM call ID, the clock value, the calls
  the VM had issued after processing it (once acknowledged), and a global's outcome, which nothing else stores.

Each result's acknowledged call count is recorded with the next journal commit, or at once when the VM has processed
everything delivered: a script waiting on a long call is where a restart usually finds it. A sequential script therefore
costs one journal commit and one count commit per result, on top of its nested calls' own; that is the price of checking
the most common crash point.

A call is launched only once the count of calls issued in the drain that made it is committed. So every key an earlier
attempt admitted is at most its last recorded count; a replay checks every recorded count, so when it releases its calls
it has made every call an earlier attempt could have admitted, and later calls are new.

A rerun refuses to replay (no new nested calls; nested calls admitted earlier may still resume on their own and are
aborted by the call's cleanup) when this module's version or the VM's fingerprint changed, when the deadline passed, or
when the script diverges: it makes another call, or calls in another order, than the journal says. Each ends the call
with an error result.

The JavaScript ``void promise`` continuations are tasks the call holds until they finish; documents, records and the
VM's calls are ``dict`` objects with the upstream keys.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any, Literal, NotRequired, TypedDict

from misaka.chord.context import Context
from misaka.chord.json import copyJson
from misaka.codemode import (
    CODEMODE_SOURCE_GRAMMAR,
    parseCodemodeSource,
    toCodemodeIdentifier,
)
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    jsLength,
    jsonStringify,
    jsSlice,
    spawn,
)

from ..documents import defineDoc
from ..errors import errorMessage
from ..harness.define import defineExtension, defineTool, section
from ..harness.tool import NestedCallDoc
from ..search import rankDocuments
from .describe import (
    DEFAULT_CODEMODE_INLINE_BUDGET,
    CodemodeNamespace,
    codemodeDescription,
    codemodeSection,
    namespaceOf,
    sampleOf,
)
from .models import (
    CATALOG_GLOBALS,
    CODEMODE_INTERNAL_PREFIX,
    GENERATE_IMAGES_TOOL,
    MAX_CONCURRENT_MODEL_CALLS,
    MODEL_CALLS,
    catalogCall,
    createModelTools,
)
from .output import DEFAULT_MAX_OUTPUT_TOKENS, OutputFile, formatResult

CODEMODE_MODULE_VERSION = 2
"""Version of everything in this module that turns stored results into what a script sees: result mapping, globals,
start data. Bump it when that changes, so a call started before an upgrade is not replayed by different code."""

CODEMODE_TOOL_NAME = "codemode"

OUTPUT_DIR_PREFIX = "misaka-codemode-"
"""MISAKA fork: upstream's temp directory prefix is ``pi-codemode-``; misaka names its code mode files
``misaka-codemode`` (as the product's code mode does), and their paths reach the model."""


class DeclaredTool(TypedDict):
    """One tool as the script sees it: ``tools.<jsName>``. ``structured``: the tool declares ``structuredOutputSchema``,
    so an error result with a value resolves with it. ``hidden``: an internal tool of this extension
    (``pi.codemode.*``), which scripts reach only through a global such as ``models.classify()``; never in ``tools``,
    ``ALL_TOOLS``, or tool search."""

    name: str
    jsName: str
    description: str
    structured: NotRequired[Literal[True]]
    hidden: NotRequired[Literal[True]]


class Start(TypedDict):
    """What a call starts with; a rerun starts the VM from exactly this. ``fingerprint``: the VM's, recorded by the
    first run before any call is admitted. ``deadline``: ``startedAt + timeout_ms`` when the script set one.
    ``namespaces``: the namespaces of the tools, as ``describeNamespace()`` and ``searchTools()`` see them.
    ``maxOutputTokens``: ``max_output_tokens`` of the script's options line, or the default."""

    version: int
    startedAt: float
    seed: int
    fingerprint: NotRequired[str]
    deadline: NotRequired[float]
    tools: list[DeclaredTool]
    namespaces: list[dict[str, Any]]
    globals: list[dict[str, Any]]
    maxOutputTokens: int
    store: dict[str, Any]


CodemodeRunDoc = defineDoc({"kind": "pi.codemode.run", "version": 1, "scope": "task", "initial": dict})
"""``RunState``: ``start``; ``outputDir``, the directory of the result's files (the full output, images), created once:
a rerun writes the same files again; ``result``, the outcome, decided once."""

CodemodeJournalDoc = defineDoc(
    {"kind": "pi.codemode.journal", "version": 1, "scope": "task", "initial": lambda: {"entries": []}}
)
"""``JournalState``: ``entries``, and ``firstIssued``: the calls the VM had issued after the script's first run, before
any result; absent until acknowledged. An entry (``JournalEntry``) is ``id`` (the VM's call ID; its result is the
entry's position + 1, ``seq``), ``at`` (the VM clock while it processes this result), ``issuedAfter`` (calls the VM had
issued after processing this result; absent until acknowledged), and ``global`` (a global's call and outcome, ``{"name",
"args"?, "outcome"}``; a tool's result is its nested call's)."""

CodemodeStoreDoc = defineDoc(
    {
        "kind": "pi.codemode.store",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"values": {}},
    }
)
"""What scripts keep with ``store()`` and read with ``load()``, per conversation. Rewindable and copied as of the fork
point, so a fork sees the values as they were when its entry was committed."""


class CodemodeNestedCall(TypedDict):
    """One tool call a script made, as the code mode call's details show it. ``id``: the VM's number for the call; its
    nested call ID is ``<callId>/c<id>``. ``args``: compact JSON of the arguments, cut for display. ``error``: error
    text, cut for display."""

    id: int
    name: str
    args: str
    status: Literal["running", "ok", "error", "cancelled"]
    durationMs: NotRequired[float]
    error: NotRequired[str]


class CodemodeToolDetails(TypedDict):
    """The code mode call's details, in its result: its tool calls in the order it made them, with their status. Calls
    to globals are not listed. While the script runs, UIs show its nested calls from ``pi.live.nestedTools``, so the list
    is written once, with the result. ``fullOutputPath``: temp file with the complete text output, when the output was cut
    to its token budget."""

    calls: list[CodemodeNestedCall]
    fullOutputPath: NotRequired[str]


ARGS_PREVIEW_CHARS = 200
ERROR_PREVIEW_CHARS = 500


def cut(text: str, max: int) -> str:
    return f"{jsSlice(text, 0, max - 3)}..." if jsLength(text) > max else text


class CodemodeGlobal(TypedDict):
    """A function scripts call as a top-level global; its result is journaled. ``execute(args, api, context)`` returns
    an awaitable of a JSON value, or ``UNDEFINED``."""

    name: str
    spread: NotRequired[bool]
    execute: Callable[[Any, Any, Context], Awaitable[Any]]


codemodeSchema: dict[str, Any] = {
    "type": "object",
    "required": ["code"],
    "properties": {"code": {"type": "string", "description": "Raw JavaScript source."}},
}


class CodemodeToolInput(TypedDict):
    code: str


class CodemodeToolOptions(TypedDict, total=False):
    """``sandbox`` runs the scripts (``misaka.codemode.CodemodeSandbox``). ``namespaces``: groups of tools by name prefix,
    such as MCP servers, for the ``codemode`` section and tool search. ``inlineBudget``: estimated tokens the tool
    declarations in the ``codemode`` section may use; default 3000. ``models``: give scripts ``models.*`` (the model
    catalog, classifiers, and image generation); default true. ``globals`` (internal): host functions scripts call as
    globals."""

    sandbox: Any
    namespaces: Sequence[CodemodeNamespace]
    inlineBudget: int
    models: bool
    globals: Sequence[CodemodeGlobal]


OUTPUT_LIMITS = {"maxBytes": 4 * 1024 * 1024, "maxLines": 100_000}
"""Outputs bounded by the script's own token budget; the Harness's limits only stop a runaway."""

DISCOVERY_GLOBALS = ("searchTools", "describeTool", "describeNamespace")
"""Globals for finding tools, answered from the start data, so a replay sees what the first run saw."""
DEFAULT_SEARCH_LIMIT = 8


def createCodemodeTool(options: CodemodeToolOptions) -> dict[str, Any]:
    """The code mode tool. Scripts call the conversation's callable tools as ``tools.<name>(args)``."""

    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        return await CodemodeCall(options, api, context).execute(args["code"])

    return defineTool(
        {
            "name": CODEMODE_TOOL_NAME,
            "description": codemodeDescription(options.get("models") is not False),
            "parameters": codemodeSchema,
            "outputLimits": dict(OUTPUT_LIMITS),
            # Capable models write the script as raw text instead of a JSON-escaped string; pi-ai applies it only for
            # them.
            "constrainedSampling": {"type": "grammar", "variants": {"openai_lark": CODEMODE_SOURCE_GRAMMAR}},
            "callers": ["model"],
            "replay": "safe",
            "execute": execute,
        }
    )


def createCodemodeExtension(options: CodemodeToolOptions) -> dict[str, Any]:
    """The ``codemode`` extension: the code mode tool, and the ``codemode`` prompt section listing the tools scripts can
    call."""

    def render(input: dict[str, Any], *_: Any) -> str | None:
        return codemodeSection(
            # Deferred tools are found with `searchTools()`, as the coding agent leaves them out too.
            [
                tool
                for tool in input["agent"]["callable"]
                if not tool["name"].startswith(CODEMODE_INTERNAL_PREFIX) and tool.get("deferred") is not True
            ],
            options.get("namespaces") or [],
            options["inlineBudget"] if options.get("inlineBudget") is not None else DEFAULT_CODEMODE_INLINE_BUDGET,
        )

    return defineExtension(
        {
            "name": "codemode",
            "tools": [createCodemodeTool(options), *([] if options.get("models") is False else createModelTools())],
            "sections": [section("codemode", render)],
        }
    )


def outcomeOf(name: str, structured: bool, result: dict[str, Any]) -> dict[str, Any]:
    """What a script gets for a nested call, as in the coding agent. A tool with a schema resolves with its value, also
    on an error result that carries one (``bash``'s ``{ output, exitCode }`` for a nonzero exit); a schema-less tool
    resolves with its text output, images left out, as its declaration says. Any other error rejects, with its text: a
    schema-less tool's output, else its error diagnostics."""
    raw = result.get("structuredOutput", UNDEFINED)
    value = raw if structured else textOf(raw)
    if not result["isError"] or (structured and value is not UNDEFINED):
        return {"ok": True, "value": value}
    message = "" if structured else value
    diagnostics = "\n".join(
        diagnostic["message"] for diagnostic in result["diagnostics"] if diagnostic["severity"] == "error"
    )
    return {
        "ok": False,
        "message": message or diagnostics or f"Tool {name} failed",
        **({} if value is UNDEFINED else {"properties": {"structuredOutput": value}}),
    }


def textOf(value: Any) -> str:
    """The text of a schema-less tool's collapsed output: a string, one image, or an array of text and image blocks."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "\n".join(item["text"] for item in value if isText(item))


def isText(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "text"


def errorResult(message: str) -> dict[str, Any]:
    return {"output": [{"type": "text", "text": message}], "isError": True}


def _now() -> int:
    """``Date.now()``."""
    return int(time.time() * 1000)


class CodemodeCall:
    """One execution of the code mode tool: a first run, or a rerun after a restart that replays the journal."""

    def __init__(self, options: CodemodeToolOptions, api: Any, context: Context) -> None:
        self.options = options
        self.api = api
        self.context = context
        self.controller = AbortController()
        self.run: Any = None
        self.start: Start = None  # type: ignore[assignment]
        # The journal as this attempt found it; replay delivers exactly these.
        self.replayed: list[dict[str, Any]] = []
        # Entries in the journal, replayed and live; the next delivery is `length + 1`.
        self.journalLength = 0
        # Replayed entries delivered so far.
        self.nextReplayed = 0
        self.replaying = False
        # Whether an earlier attempt ran this call: its nested call keys may already exist.
        self.rerun = False
        # Whether live calls may launch; before, they are held (see `release()`).
        self.released = False
        # The count an earlier attempt recorded after the script's first run.
        self.firstIssued: int | None = None
        # The highest issued count known to be committed: calls up to it may launch.
        self.durableIssued = 0
        # Live calls waiting for their issued count to be committed.
        self.waiting: list[dict[str, Any]] = []
        # Journal index of each replayed call ID.
        self.replayIndex: dict[int, int] = {}
        # Calls the VM issued so far; its call IDs are 1, 2, ...
        self.issued = 0
        # Replay: results of journaled calls, ready to deliver in journal order.
        self.ready: dict[int, dict[str, Any]] = {}
        # Replay: calls the journal does not have, admitted once replay is done.
        self.held: list[dict[str, Any]] = []
        # Live: finished calls waiting for their journal commit.
        self.queue: list[dict[str, Any]] = []
        # Acknowledged counts to record, by result number (0: the script's first run).
        self.acks: dict[int, int] = {}
        self.committing = False
        # Set once the call decided an outcome other than the script's own (divergence, mismatch) or failed.
        self.ended: dict[str, Any] | None = None
        self.failure: BaseException | None = None
        # `models.classify()`/`generateImages()` calls in flight, and those waiting for a slot.
        self.modelCalls = 0
        self.modelWaiters: list[asyncio.Future[None]] = []
        # Images `models.generateImages()` returned, to notice a script that never shows them.
        self.generatedImages = 0
        # The script's tool calls for `details`, by VM call ID.
        self.rows: dict[int, CodemodeNestedCall] = {}
        # Resolves once the VM's fingerprint is checked or recorded; nothing is delivered or admitted before.
        self.gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # The `void promise` continuations this call started, held until they finish (asyncio holds tasks weakly).
        self._tasks: set[asyncio.Task[Any]] = set()

    def _void(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """``void promise``."""
        spawn(coroutine, self._tasks)

    def openGate(self) -> None:
        if not self.gate.done():
            self.gate.set_result(None)

    async def execute(self, source: str) -> dict[str, Any]:
        parsed = parseCodemodeSource(source)
        code = parsed["code"]
        sourceOptions = parsed["options"]
        api, context = self.api, self.context
        agent = await api.agent(context)
        tools: list[DeclaredTool] = [
            {
                "name": tool["name"],
                "jsName": toCodemodeIdentifier(tool["name"]),
                "description": sampleOf(tool),
                **({} if tool.get("structuredOutputSchema") is None else {"structured": True}),  # type: ignore[typeddict-item]
                **({"hidden": True} if tool["name"].startswith(CODEMODE_INTERNAL_PREFIX) else {}),  # type: ignore[typeddict-item]
            }
            for tool in agent["callable"]
        ]
        callable = [tool for tool in agent["callable"] if not tool["name"].startswith(CODEMODE_INTERNAL_PREFIX)]
        models = [] if self.options.get("models") is False else [*CATALOG_GLOBALS, *MODEL_CALLS]
        globals = [
            *({"name": name, "spread": True} for name in DISCOVERY_GLOBALS),
            *({"name": name, "spread": True} for name in models),
            *(
                {"name": global_["name"], "spread": global_.get("spread") is True}
                for global_ in self.options.get("globals") or []
            ),
        ]
        namespaces: list[dict[str, Any]] = []
        for tool in callable:
            namespace = namespaceOf(tool["name"], self.options.get("namespaces") or [])
            if namespace is None:
                continue
            entry = next((candidate for candidate in namespaces if candidate["name"] == namespace["name"]), None)
            if entry is None:
                entry = {
                    "name": namespace["name"],
                    **({} if namespace.get("description") is None else {"description": namespace["description"]}),
                    **({} if namespace.get("instructions") is None else {"instructions": namespace["instructions"]}),
                    "tools": [],
                }
                namespaces.append(entry)
            entry["tools"].append(tool["name"])
        now = _now()
        seed = secrets.randbits(32)

        async def find(tx: Any) -> dict[str, Any]:
            run = await tx.doc(CodemodeRunDoc, api.taskId)
            if run.get("result", UNDEFINED) is not UNDEFINED:
                return {"done": copyJson(run["result"])}
            rerun = run.get("start", UNDEFINED) is not UNDEFINED
            if not rerun:
                store = await tx.doc(CodemodeStoreDoc, api.conversationId)
                timeout = sourceOptions.get("timeoutMs")
                run["start"] = {
                    "version": CODEMODE_MODULE_VERSION,
                    "startedAt": now,
                    "seed": seed,
                    **({} if timeout is None else {"deadline": now + timeout}),
                    "tools": tools,
                    "namespaces": namespaces,
                    "globals": globals,
                    "maxOutputTokens": (
                        sourceOptions["maxOutputTokens"]
                        if sourceOptions.get("maxOutputTokens") is not None
                        else DEFAULT_MAX_OUTPUT_TOKENS
                    ),
                    "store": copyJson(store["values"]),
                }
            journal = await tx.doc(CodemodeJournalDoc, api.taskId)
            firstIssued = journal.get("firstIssued", UNDEFINED)
            return {
                "start": copyJson(run["start"]),
                "journal": copyJson(journal["entries"]),
                "firstIssued": None if firstIssued is UNDEFINED else firstIssued,
                "rerun": rerun,
            }

        found = await api.commit(find, context)
        if "done" in found:
            return found["done"]  # type: ignore[no-any-return]
        self.start = found["start"]
        self.replayed = found["journal"]
        self.replayIndex = {entry["id"]: index for index, entry in enumerate(found["journal"])}
        self.journalLength = len(found["journal"])
        self.replaying = len(found["journal"]) > 0
        self.rerun = found["rerun"]
        self.firstIssued = found["firstIssued"]
        # A loop, not spread arguments: a journal can hold more entries than a call takes arguments.
        durable = found["firstIssued"] if found["firstIssued"] is not None else 0
        for entry in found["journal"]:
            durable = max(durable, entry.get("issuedAfter", 0))
        self.durableIssued = durable

        if self.start["version"] != CODEMODE_MODULE_VERSION:
            return await self.decide(
                errorResult(
                    f"The script cannot resume: it started under code mode version {self.start['version']}, this is {CODEMODE_MODULE_VERSION}."
                )
            )
        deadline = self.start.get("deadline")
        remaining = None if deadline is None else deadline - _now()
        if remaining is not None and remaining <= 0:
            return await self.decide(errorResult("Script timed out: its deadline passed while it was interrupted."))

        signal = context.abortSignal

        def onAbort() -> None:
            self.controller.abort(signal.reason if signal is not None else UNDEFINED)

        if signal is not None:
            signal.addEventListener("abort", onAbort, once=True)
            if signal.aborted:
                onAbort()
        try:
            self.run = self.options["sandbox"].start(
                code,
                {
                    "startedAt": self.start["startedAt"],
                    "seed": self.start["seed"],
                    "tools": [tool for tool in self.start["tools"] if tool.get("hidden") is not True],
                    "globals": self.start["globals"],
                    "store": self.start["store"],
                    "signal": self.controller.signal,
                    **({} if remaining is None else {"timeoutMs": remaining}),
                    "onStarted": lambda fingerprint: self._void(self.checkFingerprint(fingerprint)),
                    "onCall": lambda call: self.onCall(call),
                    "onIdle": lambda seq, issued: self.onIdle(seq, issued),
                },
            )
            result = await self.run.result
            if self.failure is not None:
                raise self.failure
            if self.ended is not None:
                return self.ended
            if signal is not None:
                signal.throwIfAborted()
            # Aborted, but not by this call: the host closed the sandbox, for example while shutting down before closing
            # the Harness. That decides nothing about the script: store nothing and end with this invocation, so a later
            # attempt replays it.
            if not result["ok"] and result["error"]["kind"] == "aborted" and not self.controller.signal.aborted:
                if signal is not None and not signal.aborted:
                    closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

                    def resolveClosed() -> None:
                        if not closed.done():
                            closed.set_result(None)

                    signal.addEventListener("abort", resolveClosed, once=True)
                    try:
                        await closed
                    finally:
                        signal.removeEventListener("abort", resolveClosed)
                if signal is not None:
                    signal.throwIfAborted()
                raise RuntimeError("The code mode sandbox was closed while the script ran")
            # A replay that ends where the live run went on is a divergence (design section 4.3).
            during = result.get("during")
            # Within the replayed part, the first run included: a rerun without journal entries replays that much too.
            if self.rerun and during is not None and during["seq"] <= len(self.replayed):
                # The live run went on: it acknowledged that result, or it made a call that later results answer, which
                # the replay never made.
                acknowledged = (
                    len(self.replayed) > 0 or self.firstIssued is not None
                    if during["seq"] == 0
                    else self.replayed[during["seq"] - 1].get("issuedAfter") is not None
                )
                continued = acknowledged or any(entry["id"] > during["issued"] for entry in self.replayed[during["seq"] :])
                if continued:
                    return await self.decide(
                        errorResult(
                            f"The script took a different path when resumed after a restart: it ended after result {during['seq']}, where it went on before."
                        )
                    )
            return await self.complete(result)
        finally:
            if signal is not None:
                signal.removeEventListener("abort", onAbort)

    async def decide(self, decided: dict[str, Any]) -> dict[str, Any]:
        """Store an outcome the call decided, and end the VM if it runs; returns the outcome."""
        result = {**decided, "details": self.details()}

        async def store(tx: Any) -> None:
            run = await tx.doc(CodemodeRunDoc, self.api.taskId)
            if run.get("result", UNDEFINED) is UNDEFINED:
                run["result"] = copyJson(result, {"omitUndefinedProperties": True})

        await self.api.commit(store, self.context)
        return result

    def diverge(self, message: str) -> None:
        """End the call with ``message``: store it as the outcome, then stop the VM with an error the script cannot
        catch."""
        if self.ended is not None or self.failure is not None:
            return
        result = errorResult(f"The script took a different path when resumed after a restart: {message}")
        self.ended = result

        async def decideAndStop() -> None:
            try:
                await self.decide(result)
            except Exception as error:  # noqa: BLE001 - the call fails with it
                self.fail(error)
                return
            self.controller.abort(RuntimeError(message))

        self._void(decideAndStop())

    @property
    def stopped(self) -> bool:
        """Whether nothing more may happen: the run has its outcome, or the call decided or failed."""
        return self.ended is not None or self.failure is not None or (self.run is not None and self.run.finished is True)

    def fail(self, error: BaseException) -> None:
        if self.failure is not None:
            return
        self.failure = error
        self.controller.abort(error)

    async def complete(self, result: dict[str, Any]) -> dict[str, Any]:
        """The script's own outcome, stored with its store writes in one transaction."""
        details = self.details()
        formatted = await formatResult(
            result,
            {
                "generatedImages": self.generatedImages,
                "wallMs": _now() - self.start["startedAt"],
                "maxOutputTokens": self.start["maxOutputTokens"],
                "calls": details["calls"],
                "limits": OUTPUT_LIMITS,
                "write": self.outputFiles(),
            },
        )
        tool = {
            "output": formatted["content"],
            "isError": not result["ok"],
            "details": {
                **details,
                **({} if formatted.get("fullOutputPath") is None else {"fullOutputPath": formatted["fullOutputPath"]}),
            },
        }
        writes = result["storeWrites"] if result["ok"] else None

        async def store(tx: Any) -> dict[str, Any]:
            run = await tx.doc(CodemodeRunDoc, self.api.taskId)
            if run.get("result", UNDEFINED) is not UNDEFINED:
                return copyJson(run["result"])  # type: ignore[no-any-return]
            run["result"] = copyJson(tool, {"omitUndefinedProperties": True})
            if writes is not None and (len(writes["set"]) > 0 or len(writes["delete"]) > 0):
                values = (await tx.doc(CodemodeStoreDoc, self.api.conversationId))["values"]
                for key in writes["delete"]:
                    if key in values:
                        del values[key]
                for key, value in writes["set"].items():
                    values[key] = value
            return tool

        return await self.api.commit(store, self.context)  # type: ignore[no-any-return]

    def outputFiles(self) -> OutputFile:
        """Writes the result's files under fixed names into the run's output directory, so a rerun overwrites them."""
        env = self.api.env
        directory: asyncio.Future[str] | None = None

        async def write(name: str, data: str | bytes) -> dict[str, str]:
            nonlocal directory
            if env is None:
                return {"error": "no execution environment"}
            try:
                if directory is None:
                    directory = asyncio.ensure_future(self.outputDir(env))
                path = f"{await asyncio.shield(directory)}/{name}"
                written = await env.writeFile(path, data, self.context)
                return {"path": path} if written["ok"] else {"error": written["error"].message}
            except Exception as error:  # noqa: BLE001 - a file that cannot be written is named in the result
                return {"error": errorMessage(error)}

        return write

    async def outputDir(self, env: Any) -> str:
        """The run's output directory: the recorded one, else a new temp directory, recorded before any file is
        written."""

        async def read(tx: Any) -> Any:
            return (await tx.doc(CodemodeRunDoc, self.api.taskId)).get("outputDir", UNDEFINED)

        recorded = await self.api.commit(read, self.context)
        if recorded is not UNDEFINED:
            return recorded  # type: ignore[no-any-return]
        created = await env.createTempDir(OUTPUT_DIR_PREFIX, self.context)
        if not created["ok"]:
            raise RuntimeError(created["error"].message)

        async def record(tx: Any) -> str:
            run = await tx.doc(CodemodeRunDoc, self.api.taskId)
            if run.get("outputDir", UNDEFINED) is UNDEFINED:
                run["outputDir"] = created["value"]
            return run["outputDir"]  # type: ignore[no-any-return]

        return await self.api.commit(record, self.context)  # type: ignore[no-any-return]

    async def checkFingerprint(self, fingerprint: str) -> None:
        recorded = self.start.get("fingerprint")
        if recorded is not None:
            if recorded != fingerprint:
                self.ended = errorResult(
                    f"The script cannot resume: the sandbox changed since it started ({recorded} -> {fingerprint})."
                )
                try:
                    await self.decide(self.ended)
                except Exception as error:  # noqa: BLE001 - the call fails with it
                    self.fail(error)
                self.controller.abort(RuntimeError("Sandbox fingerprint changed"))
                return
        else:

            async def record(tx: Any) -> None:
                run = await tx.doc(CodemodeRunDoc, self.api.taskId)
                if run.get("start", UNDEFINED) is not UNDEFINED:
                    run["start"]["fingerprint"] = fingerprint

            try:
                await self.api.commit(record, self.context)
            except Exception as error:  # noqa: BLE001 - the call fails with it
                self.fail(error)
                return
        self.openGate()
        # Nothing was delivered before the gate: start now. A rerun without a journal releases its calls at the
        # script's first acknowledgment instead.
        if self.replaying:
            self.deliverReplayed()
        elif not self.rerun:
            self._void(self.release())

    def onCall(self, made: dict[str, Any]) -> None:
        if self.stopped:
            return
        call = asToolCall(made)
        if call["target"] == "tool":
            self.started(call)
        self.issued = max(self.issued, call["id"])
        index = self.replayIndex.get(call["id"], -1) if self.replaying else -1
        if index == -1:
            if self.released:
                self.waiting.append(call)
                self.launchCovered()
            else:
                self.held.append(call)
            return
        entry = self.replayed[index]
        recordedGlobal = entry.get("global")
        if recordedGlobal is not None:
            if (
                call["target"] != "global"
                or call["name"] != recordedGlobal["name"]
                or not sameJson(call.get("args", UNDEFINED), recordedGlobal.get("args", UNDEFINED))
            ):
                self.diverge(f"call {call['id']} is {describe(call)}, but was a call to {recordedGlobal['name']}")
                return
            self.ready[call["id"]] = recordedGlobal["outcome"]
            self.gate.add_done_callback(lambda _: self.deliverReplayed())
            return
        if call["target"] != "tool":
            self.diverge(f"call {call['id']} is {describe(call)}, but was a tool call")
            return

        async def reattach() -> None:
            await asyncio.shield(self.gate)
            try:
                outcome = await self.callTool(call)
            except Exception as error:  # noqa: BLE001 - a failed reattachment ends the call
                if self.stopped:
                    return
                signal = self.context.abortSignal
                if signal is not None and signal.aborted:
                    self.fail(error)
                else:
                    # The key names another tool or other arguments.
                    self.diverge(f"call {call['id']} is {describe(call)}: {errorMessage(error)}")
                return
            self.ready[call["id"]] = outcome
            self.deliverReplayed()

        self._void(reattach())

    def deliverReplayed(self) -> None:
        """Replay: deliver journaled results in journal order, each once the VM has issued its call."""
        while self.nextReplayed < len(self.replayed) and not self.stopped:
            entry = self.replayed[self.nextReplayed]
            outcome = self.ready.get(entry["id"])
            if entry["id"] > self.issued or outcome is None:
                return
            del self.ready[entry["id"]]
            self.nextReplayed += 1
            self.run.deliver(self.nextReplayed, entry["at"], entry["id"], outcome)

    def onIdle(self, seq: int, issued: int) -> None:
        if self.stopped:
            return
        # A count an earlier attempt recorded must match; a missing one is recorded now.
        if self.rerun and seq <= len(self.replayed):
            recorded = self.firstIssued if seq == 0 else self.replayed[seq - 1].get("issuedAfter")
            if recorded is not None and recorded != issued:
                self.diverge(
                    f"its first run made {issued} calls, where it had made {recorded}"
                    if seq == 0
                    else f"after result {seq} it had made {issued} calls, where it had made {recorded}"
                )
                return
        self.acks[seq] = issued
        if not self.replaying:
            # A rerun without a journal: every call an earlier attempt could have admitted is made by now.
            if seq == 0 and self.rerun:
                self._void(self.release())
            # Committed at once when the script has processed everything delivered: calls it made wait for it, and a
            # script waiting on a long call is where a restart usually finds it. Otherwise with the next commit; the VM
            # catches up without waiting on anything.
            if seq == self.journalLength:
                self._void(self.pump())
            return
        # Caught up: the VM processed everything delivered and waits.
        if seq != self.nextReplayed:
            return
        following = self.replayed[seq] if seq < len(self.replayed) else None
        if following is not None:
            if following["id"] > issued:
                self.diverge(f"it waits without making call {following['id']}, which it made before")
            return
        # The last replayed result is acknowledged: continue live, admitting the calls the journal did not have once
        # their count is committed.
        self.replaying = False
        self._void(self.pump())
        self._void(self.release())

    async def release(self) -> None:
        """Let the held calls launch, and later live calls, each once its issued count is committed. On a rerun, an
        earlier attempt may have admitted some of them under their keys, as other calls: when the replay releases (idle
        after the last journaled result, or after its first run without a journal) its counts matched every recorded
        one, so it has made every call an earlier attempt could have admitted. All held calls are checked first,
        whatever their target, and a divergence launches none. Calls made after this are new."""
        await asyncio.shield(self.gate)
        if self.stopped or self.released:
            return
        calls = self.held
        self.held = []
        if self.rerun:
            for call in calls:
                try:
                    other = await self.earlier(call)
                except Exception as error:  # noqa: BLE001 - the call fails with it
                    self.fail(error)
                    return
                if self.stopped:
                    return
                if other is not None:
                    self.diverge(f"call {call['id']} is {describe(call)}, but was {other}")
                    return
        self.released = True
        self.waiting.extend([*calls, *self.held])
        self.held = []
        self.launchCovered()

    def launchCovered(self) -> None:
        """Launch the waiting calls whose issued count is committed."""
        if not self.released or self.stopped:
            return
        covered = [call for call in self.waiting if call["id"] <= self.durableIssued]
        if len(covered) == 0:
            return
        self.waiting = [call for call in self.waiting if call["id"] > self.durableIssued]
        for call in covered:
            self.launch(call)

    async def earlier(self, call: dict[str, Any]) -> str | None:
        """The call an earlier attempt made under this call's key, when it is another one: another target, tool, or
        arguments."""
        api, context = self.api, self.context
        state = await api.snapshot(NestedCallDoc, api.taskId, f"c{call['id']}", context)
        taskId = None if state is None else state.get("taskId")
        if taskId is None:
            return None
        record = await api.getTask(taskId, context)
        input = None if record is None else record["input"]
        if input is None or input.get("kind") != "nested":
            return None
        made = input["call"]
        args = call.get("args", UNDEFINED)
        if call["target"] == "tool" and made["name"] == call["name"] and sameJson(made["arguments"], {} if args is UNDEFINED or args is None else args):
            return None
        return f"tools.{made['name']}({jsonStringify(made['arguments'])})"

    def launch(self, call: dict[str, Any]) -> None:
        """Run one live call and journal its result."""
        if self.stopped:
            return

        async def running() -> None:
            try:
                if call["target"] == "tool":
                    done: dict[str, Any] = {"outcome": await self.callTool(call)}
                else:
                    outcome = await self.callGlobal(call)
                    args = call.get("args", UNDEFINED)
                    done = {
                        "outcome": outcome,
                        "global": {
                            "name": call["name"],
                            **({} if args is UNDEFINED else {"args": args}),
                            "outcome": outcome,
                        },
                    }
            except Exception as error:  # noqa: BLE001 - a failed launch ends the call
                # Stopped meanwhile: the outcome is decided, or a waiting model call gave up.
                if self.stopped:
                    return
                signal = self.context.abortSignal
                if signal is not None and signal.aborted:
                    self.fail(error)
                else:
                    # A key an earlier attempt admitted with another tool or other arguments: this attempt took another
                    # path.
                    self.diverge(f"call {call['id']} is {describe(call)}: {errorMessage(error)}")
                return
            self.queue.append({"id": call["id"], "at": _now(), **done})
            self._void(self.pump())

        self._void(running())

    async def callTool(self, call: dict[str, Any]) -> dict[str, Any]:
        # Passed as the script gave them; validation reports arguments that are not an object.
        rawArgs = call.get("args", UNDEFINED)
        args = {} if rawArgs is UNDEFINED or rawArgs is None else rawArgs
        model = call["name"].startswith(CODEMODE_INTERNAL_PREFIX)
        if model:
            await self.modelSlot()
            # The call may have stopped while this one waited: a finished script admits no new paid call.
            if self.stopped:
                self.releaseModelSlot()
                raise RuntimeError("The script has ended")
        try:
            result = await self.api.executeTool(call["name"], args, self.context, {"key": f"c{call['id']}"})
        finally:
            if model:
                self.releaseModelSlot()
        if call["name"] == GENERATE_IMAGES_TOOL:
            self.generatedImages += countImages(result.get("structuredOutput", UNDEFINED))
        structured = any(tool["name"] == call["name"] and tool.get("structured") is True for tool in self.start["tools"])
        outcome = outcomeOf(call["name"], structured, result)
        self.finishRow(call["id"], outcome, result)
        return outcome

    def started(self, call: dict[str, Any]) -> None:
        """The script made a tool call: its row appears, running, also while it waits to launch."""
        if call["id"] in self.rows:
            return
        rawArgs = call.get("args", UNDEFINED)
        args = "" if rawArgs is UNDEFINED else jsonStringify(rawArgs)
        name = call["name"].removeprefix(CODEMODE_INTERNAL_PREFIX)
        self.rows[call["id"]] = {"id": call["id"], "name": name, "args": cut(args, ARGS_PREVIEW_CHARS), "status": "running"}

    def finishRow(self, id: int, outcome: dict[str, Any], result: dict[str, Any]) -> None:
        """The call ended: an error result is an error row, also when the script got its value (bash's nonzero exit);
        the row gets its status, duration, and error."""
        row = self.rows.get(id)
        if row is None:
            return
        row["status"] = "error" if result["isError"] else "ok"
        if result.get("durationMs") is not None:
            row["durationMs"] = result["durationMs"]
        errors = [diagnostic for diagnostic in result["diagnostics"] if diagnostic["severity"] == "error"]
        error = outcome["message"] if not outcome["ok"] else "\n".join(diagnostic["message"] for diagnostic in errors)
        if result["isError"] and error != "":
            row["error"] = cut(error, ERROR_PREVIEW_CHARS)

    def details(self) -> CodemodeToolDetails:
        """The rows in call order; calls still running when the script ended were cancelled."""
        calls: list[CodemodeNestedCall] = [
            {**row, "status": "cancelled"} if row["status"] == "running" else row  # type: ignore[typeddict-item]
            for row in sorted(self.rows.values(), key=lambda row: row["id"])
        ]
        return {"calls": calls}

    async def callGlobal(self, call: dict[str, Any]) -> dict[str, Any]:
        discovery = call["name"] in DISCOVERY_GLOBALS
        catalog = call["name"] in CATALOG_GLOBALS
        global_ = (
            None
            if discovery or catalog
            else next((candidate for candidate in self.options.get("globals") or [] if candidate["name"] == call["name"]), None)
        )
        if not discovery and not catalog and global_ is None:
            return {"ok": False, "message": f"Unknown global {call['name']}"}
        args = call.get("args", UNDEFINED)
        try:
            # `UNDEFINED` stays undefined: the journal omits the value, and a replay resolves with undefined again.
            if discovery:
                value = self.discover(call)
            elif catalog:
                value = await catalogCall(call["name"], args, self.api.models, self.context)
            else:
                assert global_ is not None
                value = await global_["execute"](args, self.api, self.context)
            return {"ok": True, "value": value}
        except Exception as error:
            signal = self.context.abortSignal
            if signal is not None and signal.aborted:
                raise
            return {"ok": False, "message": errorMessage(error)}

    async def modelSlot(self) -> None:
        if self.modelCalls >= MAX_CONCURRENT_MODEL_CALLS:
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.modelWaiters.append(waiter)
            await waiter
        self.modelCalls += 1

    def releaseModelSlot(self) -> None:
        self.modelCalls -= 1
        while len(self.modelWaiters) > 0:
            waiter = self.modelWaiters.pop(0)
            if not waiter.done():
                waiter.set_result(None)
                break

    def discover(self, call: dict[str, Any]) -> Any:
        """``searchTools()``, ``describeTool()``, ``describeNamespace()``: answered from the start data."""
        rawArgs = call.get("args", UNDEFINED)
        args = rawArgs if isinstance(rawArgs, list) else []
        namespaces = self.start["namespaces"]
        tools = [tool for tool in self.start["tools"] if tool.get("hidden") is not True]

        def namespaceOfTool(name: str) -> dict[str, Any] | None:
            return next((namespace for namespace in namespaces if name in namespace["tools"]), None)

        def matchesNamespace(namespace: dict[str, Any], query: str) -> bool:
            return namespace["name"] == query or toCodemodeIdentifier(namespace["name"]) == toCodemodeIdentifier(query)

        first = args[0] if len(args) > 0 else UNDEFINED
        if call["name"] == "describeTool":
            if not isinstance(first, str):
                raise RuntimeError("describeTool() expects a tool name")
            found = next((tool for tool in tools if tool["name"] == first or tool["jsName"] == first), None)
            return UNDEFINED if found is None else found["description"]
        if call["name"] == "describeNamespace":
            if not isinstance(first, str):
                raise RuntimeError("describeNamespace() expects a namespace name")
            namespace = next((candidate for candidate in namespaces if matchesNamespace(candidate, first)), None)
            if namespace is None:
                return UNDEFINED
            return {
                "name": namespace["name"],
                **({} if namespace.get("description") is None else {"description": namespace["description"]}),
                **({} if namespace.get("instructions") is None else {"instructions": namespace["instructions"]}),
                "tools": [toCodemodeIdentifier(tool) for tool in namespace["tools"]],
            }
        query = first
        options = args[1] if len(args) > 1 else UNDEFINED
        if not isinstance(query, str):
            raise RuntimeError("searchTools() expects a query string")  # noqa: TRY004 - upstream `new Error`
        limit = options.get("limit", UNDEFINED) if isinstance(options, dict) else UNDEFINED
        if limit is UNDEFINED or limit is None:
            limit = DEFAULT_SEARCH_LIMIT
        if not isinstance(limit, int | float) or isinstance(limit, bool) or limit != int(limit) or limit <= 0:
            raise RuntimeError("searchTools() limit must be a positive integer")
        namespaceQuery = options.get("namespace", UNDEFINED) if isinstance(options, dict) else UNDEFINED
        if namespaceQuery is not UNDEFINED and namespaceQuery is not None and not isinstance(namespaceQuery, str):
            raise RuntimeError("searchTools() namespace must be a string")
        documents = []
        for tool in tools:
            own = namespaceOfTool(tool["name"])
            if isinstance(namespaceQuery, str) and (own is None or not matchesNamespace(own, namespaceQuery)):
                continue
            text = [
                tool["name"],
                tool["name"].replace("_", " "),
                tool["description"],
                "" if own is None else own["name"],
                "" if own is None else own.get("description") or "",
            ]
            documents.append({"name": tool["name"], "text": " ".join(text)})
        ranked = rankDocuments(query, documents, int(limit))  # type: ignore[arg-type]
        return [
            {"name": tool["jsName"], "description": tool["description"]}
            for name in ranked
            for tool in [next(candidate for candidate in tools if candidate["name"] == name)]
        ]

    async def pump(self) -> None:
        """Journal finished calls and acknowledged counts in one commit, then deliver the calls and launch the calls the
        counts cover; one commit in flight at a time, so what arrives meanwhile goes into the next."""
        if self.committing or self.stopped:
            return
        if len(self.queue) == 0 and len(self.acks) == 0:
            return
        self.committing = True
        batch = self.queue
        self.queue = []
        acks = list(self.acks.items())
        self.acks.clear()

        async def write(tx: Any) -> None:
            journal = await tx.doc(CodemodeJournalDoc, self.api.taskId)
            for seq, issued in acks:
                if seq == 0:
                    if journal.get("firstIssued", UNDEFINED) is UNDEFINED:
                        journal["firstIssued"] = issued
                else:
                    entries = journal["entries"]
                    entry = entries[seq - 1] if seq - 1 < len(entries) else UNDEFINED
                    if entry is not UNDEFINED and entry.get("issuedAfter", UNDEFINED) is UNDEFINED:
                        entry["issuedAfter"] = issued
            for item in batch:
                entry = {"id": item["id"], "at": item["at"], **({} if item.get("global") is None else {"global": item["global"]})}
                journal["entries"].push(copyJson(entry, {"omitUndefinedProperties": True}))

        try:
            await self.api.commit(write, self.context)
        except Exception as error:  # noqa: BLE001 - the call fails with it
            self.fail(error)
            return
        for _seq, issued in acks:
            self.durableIssued = max(self.durableIssued, issued)
        for item in batch:
            self.journalLength += 1
            if not self.run.finished:
                self.run.deliver(self.journalLength, item["at"], item["id"], item["outcome"])
        self.launchCovered()
        self.committing = False
        self._void(self.pump())


def asToolCall(call: dict[str, Any]) -> dict[str, Any]:
    """A script's call of ``models.classify()`` or ``models.generateImages()`` as the call of its internal tool."""
    tool = MODEL_CALLS.get(call["name"]) if call["target"] == "global" else None
    if tool is None:
        return call
    args = call.get("args", UNDEFINED)
    values = args if isinstance(args, list) else []
    model = values[0] if len(values) > 0 else None
    context = values[1] if len(values) > 1 else None
    return {"id": call["id"], "target": "tool", "name": tool, "args": {"model": model, "context": context}}


def countImages(value: Any) -> int:
    """Image blocks in a ``models.generateImages()`` result."""
    output = value.get("output") if isinstance(value, dict) else None
    return len([item for item in output if isinstance(item, dict) and item.get("type") == "image"]) if isinstance(output, list) else 0


def describe(call: dict[str, Any]) -> str:
    args = call.get("args", UNDEFINED)
    return f"{'tools.' if call['target'] == 'tool' else ''}{call['name']}({'' if args is UNDEFINED else jsonStringify(args)})"


def sameJson(a: Any, b: Any) -> bool:
    """Structural JSON equality; key order does not matter."""
    if a is b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if not isinstance(a, list | dict) or not isinstance(b, list | dict):
        return not isinstance(a, list | dict) and not isinstance(b, list | dict) and a is not UNDEFINED and b is not UNDEFINED and a == b
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
            sameJson(item, b[index]) for index, item in enumerate(a)
        )
    return len(a) == len(b) and all(key in b and sameJson(value, b[key]) for key, value in a.items())


__all__ = [
    "CODEMODE_MODULE_VERSION",
    "CODEMODE_TOOL_NAME",
    "DISCOVERY_GLOBALS",
    "OUTPUT_LIMITS",
    "CodemodeCall",
    "CodemodeGlobal",
    "CodemodeJournalDoc",
    "CodemodeNestedCall",
    "CodemodeRunDoc",
    "CodemodeStoreDoc",
    "CodemodeToolDetails",
    "CodemodeToolInput",
    "CodemodeToolOptions",
    "asToolCall",
    "createCodemodeExtension",
    "createCodemodeTool",
    "outcomeOf",
    "sameJson",
    "textOf",
]
