"""Runs one codemode script in the sandbox. Split from ``tool.py`` and loaded through
``execute_lazy.py`` so the sandbox runtime only loads when a script runs.

Translated from pi's ``coding-agent/src/extensions/codemode/execute.ts``.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, TypeGuard

from misaka.agent.types import AgentToolResult
from misaka.ai.auth.types import AuthOperationOptions
from misaka.ai.types import (
    ClassifierContext,
    ImageContent,
    ImagesContext,
    TextContent,
    Usage,
)
from misaka.codemode import (
    CodemodeSandbox,
    CodemodeTool,
    parseCodemodeSource,
    renderToolSample,
    toCodemodeIdentifier,
)
from misaka.codemode.types import CodemodeFailure, CodemodeOutputItem, CodemodeResult
from misaka.core.extensions.types import ToolNamespace
from misaka.core.tools.truncate import formatSize
from misaka.core.usage_totals import combineUsage
from misaka.utils.js import (
    UNDEFINED,
    bufferFromBase64,
    errorMessage,
    jsLength,
    jsNumber,
    jsonStringify,
    jsRound,
    jsSlice,
    jsString,
    nullishCoalesce,
    objectEntries,
    toFixed,
)
from misaka.utils.output_files import writeOutputFile
from misaka.utils.values import read_field

from ..tool_search.tool import (
    DEFAULT_TOOL_SEARCH_LIMIT,
    Bm25Ranker,
    createToolSearchDocument,
)
from .tool import (
    CODEMODE_DOCS_PATH,
    CODEMODE_STORE_ENTRY_TYPE,
    CodemodeModelRuntime,
    CodemodeNestedCall,
    CodemodeToolDetails,
    CodemodeToolOptions,
    getCodemodeCallableTools,
    toCodemodeDeclaration,
)

ARGS_PREVIEW_CHARS = 200
ERROR_PREVIEW_CHARS = 500
# `models.classify()` and `models.generateImages()` calls one script may have in flight; `Promise.all`
# over many items queues the rest.
MAX_CONCURRENT_MODEL_CALLS = 4
# Heap limit for the QuickJS VM. The worker shares misaka's process, so without a limit a runaway
# script can grow to wasm32's 4 GiB and take the session down. Overruns throw
# `InternalError: out of memory` inside the script.
CODEMODE_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
# Shared across calls and sessions. Allow one replacement while a cancelled worker is still exiting.
# MISAKA fork: "sessions" are those of one OS process; every Sister card, headless node, and subagent runs in a
# process of its own, so one Sister's stuck workers never block another's. A worker thread cannot be killed, so a
# cancelled one counts until its built-in returns.
DEFAULT_MAX_CODEMODE_WORKERS = 2


def maxCodemodeWorkers() -> int:
    """``MISAKA_CODEMODE_MAX_WORKERS`` if it is a positive integer. Read per call so SDK hosts can set it at runtime.

    MISAKA fork: the variable carries MISAKA's prefix where pi reads ``PI_CODEMODE_MAX_WORKERS``."""
    value = jsNumber(os.environ.get("MISAKA_CODEMODE_MAX_WORKERS", UNDEFINED))
    return int(value) if isinstance(value, int | float) and math.isfinite(value) and value == int(value) and value > 0 \
        else DEFAULT_MAX_CODEMODE_WORKERS


# How long a script waits for a free worker before failing. Bounds the wait when every worker is
# held by a script that waits on another script, for example an in-process subagent that uses codemode.
CODEMODE_WORKER_WAIT_MS = 60_000
activeCodemodeWorkers = 0
# Scripts waiting for a worker, in arrival order.
codemodeWorkerWaiters: list[Callable[[], bool]] = []
# MISAKA fork: workers are reserved on whichever event loop thread runs the session and released on the worker's own
# thread as it ends (see `CodemodeSandboxOptions.acquireWorker`), so the counter and the queue are guarded, and a
# waiter's one outcome -- reserved, aborted or timed out -- is decided under this lock.
_activeCodemodeWorkersLock = threading.Lock()


async def reserveCodemodeWorker(signal: Any, timeoutMs: float) -> Literal["reserved", "aborted", "timeout"]:
    """Reserve one worker slot, waiting in arrival order while all are taken. Resolves with why it could
    not reserve one: the signal aborted, or ``timeoutMs`` passed."""
    if signal is not None and signal.aborted:
        return "aborted"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Literal["reserved", "aborted", "timeout"]] = loop.create_future()
    decided: list[Literal["reserved", "aborted", "timeout"]] = []

    def settle(outcome: Literal["reserved", "aborted", "timeout"]) -> bool:
        # With the lock held. The first outcome is the only one; it is delivered on the waiter's own loop.
        if decided:
            return False
        if grant in codemodeWorkerWaiters:
            codemodeWorkerWaiters.remove(grant)
        try:
            loop.call_soon_threadsafe(finish)
        except RuntimeError:
            return False            # the waiter's loop is gone: nobody takes the slot
        decided.append(outcome)
        return True

    def finish() -> None:
        timer.cancel()
        if signal is not None:
            signal.removeEventListener("abort", onAbort)
        if not future.done():
            future.set_result(decided[0])

    def grant() -> bool:
        return settle("reserved")

    def onAbort() -> None:
        with _activeCodemodeWorkersLock:
            settle("aborted")

    def onTimeout() -> None:
        with _activeCodemodeWorkersLock:
            settle("timeout")

    timer = loop.call_later(timeoutMs / 1000, onTimeout)
    with _activeCodemodeWorkersLock:
        codemodeWorkerWaiters.append(grant)
    if signal is not None:
        signal.addEventListener("abort", onAbort, once=True)
    admitCodemodeWorkers()
    try:
        return await future
    except asyncio.CancelledError:
        # Python only (a JS promise cannot be cancelled): a slot granted to a cancelled waiter is given back.
        with _activeCodemodeWorkersLock:
            granted = decided[:1] == ["reserved"]
            settle("aborted")
        if granted:
            releaseCodemodeWorker()
        raise


def _admitCodemodeWorkersLocked() -> None:
    global activeCodemodeWorkers
    while len(codemodeWorkerWaiters) > 0 and activeCodemodeWorkers < maxCodemodeWorkers():
        activeCodemodeWorkers += 1
        if not codemodeWorkerWaiters.pop(0)():
            activeCodemodeWorkers -= 1


def admitCodemodeWorkers() -> None:
    """Give free slots to waiting scripts in arrival order."""
    with _activeCodemodeWorkersLock:
        _admitCodemodeWorkersLocked()


def releaseCodemodeWorker() -> None:
    global activeCodemodeWorkers
    with _activeCodemodeWorkersLock:
        activeCodemodeWorkers -= 1
        _admitCodemodeWorkersLocked()
MODEL_TYPES: frozenset[str] = frozenset({"chat", "image", "classifier"})


def _now() -> float:
    return time.perf_counter() * 1000


def truncateText(text: str, maxChars: int) -> str:
    return f"{jsSlice(text, 0, maxChars - 3)}..." if jsLength(text) > maxChars else text


def previewArgs(args: Any) -> str:
    if args is UNDEFINED:
        return ""
    try:
        return truncateText(jsonStringify(args), ARGS_PREVIEW_CHARS)
    except (TypeError, ValueError):
        return ""


def textOf(result: Any) -> str:
    content = read_field(result, "content") or []
    return "\n".join(read_field(block, "text") for block in content if read_field(block, "type") == "text")


def toModelType(value: Any) -> str:
    if isinstance(value, str) and value in MODEL_TYPES:
        return value
    shown = "undefined" if value is UNDEFINED else jsonStringify(value)
    raise RuntimeError(f'Unknown model type {shown}. Use "chat", "image", or "classifier".')


def toProvider(value: Any) -> str | None:
    if value is None or value is UNDEFINED:
        return None
    if not isinstance(value, str):
        raise RuntimeError("provider must be a string")  # noqa: TRY004 - upstream `new Error`
    return value


def toModelInfo(model: Any) -> dict[str, Any]:
    """Catalog entry for scripts. ``headers`` is dropped because models.json headers can carry
    credentials."""
    info = dict(model.model_dump(exclude_none=True) if hasattr(model, "model_dump") else model)
    info.pop("headers", None)
    return info


def withArticle(word: str) -> str:
    """``an image``, ``a classifier``."""
    return f"{'an' if re.match('^[aeiou]', word) else 'a'} {word}"


def isRecord(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def describeValue(value: Any) -> str:
    """A script value in an error message: ``undefined``, ``a string``, ``an array``, or its keys
    (``{ prompt }``)."""
    if value is None or value is UNDEFINED:
        # Spread arguments cross the sandbox as a JSON array, so a script's `undefined` arrives as null.
        return "null" if value is None else "undefined"
    if isinstance(value, list):
        return "an empty array" if len(value) == 0 else "an array"
    if isinstance(value, dict):
        keys = list(value)
        if len(keys) == 0:
            return "{}"
        return f"{{ {', '.join(keys[:6])}{', ...' if len(keys) > 6 else ''} }}"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    return f"a {type(value).__name__}"


CLASSIFIER_CONTEXT_SHAPE = (
    '{ state: { ... }, images?: [{ type: "image", data: <base64>, mimeType }], '
    'questions: { <id>: { type: "choice", instructions, criteria: { <label>: <meaning> } } | '
    '{ type: "score", instructions, criteria: [<lowest level>, ..., <highest level>] } | '
    '{ type: "bool", instructions, criteria: { true: <meaning>, false: <meaning> } } } }'
)


def checkClassifierContext(context: Any) -> ClassifierContext:
    """Check a script's classifier context, so mistakes fail with the expected shape instead of a
    provider error."""

    def fail(problem: str) -> RuntimeError:
        return RuntimeError(
            f"models.classify() {problem}. Expected context: {CLASSIFIER_CONTEXT_SHAPE}. "
            f'See "Classify" in {CODEMODE_DOCS_PATH}.'
        )

    if not isRecord(context):
        raise fail(f"expects a context object as its second argument, got {describeValue(context)}")
    if not isRecord(context.get("state")):
        raise fail(f"context.state must be an object, got {describeValue(context.get('state', UNDEFINED))}")
    images = context.get("images", UNDEFINED)
    if images is not UNDEFINED:
        if not isinstance(images, list):
            raise fail(f"context.images must be an array, got {describeValue(images)}")
        for index, image in enumerate(images):
            if (
                not isRecord(image)
                or image.get("type") != "image"
                or not isinstance(image.get("data"), str)
                or not isinstance(image.get("mimeType"), str)
            ):
                raise fail(f"context.images[{index}] must be an image block, got {describeValue(image)}")
    questions = context.get("questions", UNDEFINED)
    if not isRecord(questions) or len(questions) == 0:
        raise fail(f"context.questions must map question IDs to questions, got {describeValue(questions)}")

    def isStrings(values: Sequence[Any]) -> bool:
        return len(values) > 0 and all(isinstance(value, str) for value in values)

    for id, question in questions.items():
        at = f"context.questions.{id}"
        if not isRecord(question):
            raise fail(f"{at} must be a question object, got {describeValue(question)}")
        if not isinstance(question.get("instructions"), str):
            raise fail(f"{at}.instructions must be a string")
        criteria = question.get("criteria")
        type = question.get("type", UNDEFINED)
        if type == "choice":
            if not isRecord(criteria) or not isStrings(list(criteria.values())):
                raise fail(f'{at} is a "choice" question, so criteria must map each label to its meaning')
        elif type == "score":
            if not isinstance(criteria, list) or not isStrings(criteria):
                raise fail(f'{at} is a "score" question, so criteria must list the levels as strings, lowest first')
        elif type == "bool":
            if not isRecord(criteria) or not isinstance(criteria.get("true"), str) or not isinstance(criteria.get("false"), str):
                raise fail(f'{at} is a "bool" question, so criteria must be {{ true: string, false: string }}')
        else:
            raise fail(f'{at}.type must be "choice", "score", or "bool", got {"undefined" if type is UNDEFINED else jsonStringify(type)}')
    # MISAKA fork: upstream casts the checked object, so keys it does not know reach the service, which
    # ignores them; the models forbid extra keys, so only the checked ones are kept.
    return ClassifierContext.model_validate({
        "state": context["state"],
        **(
            {"images": [{"type": "image", "data": image["data"], "mimeType": image["mimeType"]} for image in images]}
            if images is not UNDEFINED
            else {}
        ),
        "questions": {
            id: {
                "type": question["type"],
                "instructions": question["instructions"],
                "criteria": (
                    {"true": question["criteria"]["true"], "false": question["criteria"]["false"]}
                    if question["type"] == "bool"
                    else question["criteria"]
                ),
            }
            for id, question in questions.items()
        },
    })


def checkImagesContext(context: Any) -> ImagesContext:
    """Check a script's image context, so mistakes such as ``{ prompt }`` fail with the expected
    shape."""

    def fail(problem: str) -> RuntimeError:
        return RuntimeError(
            f"models.generateImages() {problem}. Expected context: {{ input: [{{ type: \"text\", text: <prompt> }}, "
            f'...optional {{ type: "image", data: <base64>, mimeType }} references] }}. See "Generate images" in {CODEMODE_DOCS_PATH}.'
        )

    if not isRecord(context):
        raise fail(f"expects a context object as its second argument, got {describeValue(context)}")
    input = context.get("input", UNDEFINED)
    if not isinstance(input, list) or len(input) == 0:
        raise fail(f"context.input must be a non-empty array of blocks, got {describeValue(input)}")
    for index, block in enumerate(input):
        if isRecord(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        if (
            isRecord(block)
            and block.get("type") == "image"
            and isinstance(block.get("data"), str)
            and isinstance(block.get("mimeType"), str)
        ):
            continue
        raise fail(f"context.input[{index}] must be a text or image block, got {describeValue(block)}")
    # MISAKA fork: only the checked keys are kept, as for the classifier context.
    return ImagesContext.model_validate({
        "input": [
            {"type": "text", "text": block["text"]}
            if block["type"] == "text"
            else {"type": "image", "data": block["data"], "mimeType": block["mimeType"]}
            for block in input
        ],
    })


class ModelCallResult(Protocol):
    stopReason: Literal["stop", "error", "aborted"]
    errorMessage: str | None
    usage: Usage | None


def createLimiter(limit: int) -> Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]:
    """Runs at most ``limit`` calls at once, in call order."""
    active = 0
    waiting: list[asyncio.Future[None]] = []

    def wakeNext() -> None:
        # `waiting.shift()?.()`. Python only: a cancelled waiter has nothing to resume, so the wake-up goes on.
        while waiting:
            future = waiting.pop(0)
            if not future.done():
                future.set_result(None)
                return

    async def limited(run: Callable[[], Awaitable[Any]]) -> Any:
        nonlocal active
        if active >= limit:
            future = asyncio.get_running_loop().create_future()
            waiting.append(future)
            try:
                await future
            except asyncio.CancelledError:
                # Python only (a JS promise cannot be cancelled): a waiter cancelled in the queue leaves it, and one
                # cancelled after its wake-up passes the wake-up on.
                if future in waiting:
                    waiting.remove(future)
                elif not future.cancelled():
                    wakeNext()
                raise
        active += 1
        try:
            return await run()
        finally:
            active -= 1
            wakeNext()

    return limited


def isStoreEntryData(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    set_, deleted = data.get("set"), data.get("delete")
    return isinstance(set_, dict | list) and isinstance(deleted, list) and all(isinstance(key, str) for key in deleted)


def readCodemodeStore(branch: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Values of ``load()``: the ``codemode-store`` entries on the branch, applied from the root."""
    store: dict[str, Any] = {}
    for entry in branch:
        if entry.get("type") != "custom" or entry.get("customType") != CODEMODE_STORE_ENTRY_TYPE or not isStoreEntryData(entry.get("data")):
            continue
        for key in entry["data"]["delete"]:
            store.pop(key, None)
        # `Object.entries(set)`: an array's entries are its indices as strings.
        set_ = entry["data"]["set"]
        for key, value in (objectEntries(set_) if isinstance(set_, dict) else [(str(i), v) for i, v in enumerate(set_)]):
            store[key] = value
    return store


# Default token budget for script output.
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
# Characters per token when estimating.
CHARS_PER_TOKEN = 4


def valueText(value: Any) -> str:
    """Like the script's ``text()``: strings as is, other values as compact JSON."""
    if isinstance(value, str):
        return value
    try:
        return jsonStringify(value)
    except (TypeError, ValueError):
        return jsString(value)


def formatOutput(output: Sequence[CodemodeOutputItem]) -> list[TextContent | ImageContent]:
    """Lay out the script's output so the model can tell items apart: providers join adjacent text
    blocks with a newline or with nothing. With more than one text item (``text()`` or the returned
    value), each starts with a ``==> text N/M <==`` line. ``console.*`` lines follow all other output in
    one ``<console_output>`` block."""
    total = len([item for item in output if item["type"] == "text" and not item.get("console")])
    items: list[TextContent | ImageContent] = []
    consoleLines: list[str] = []
    index = 0
    for item in output:
        if item["type"] == "image":
            items.append(ImageContent(data=item["data"], mimeType=item["mimeType"]))
        elif item.get("console"):
            consoleLines.append(item["text"])
        else:
            index += 1
            items.append(TextContent(text=f"==> text {index}/{total} <==\n{item['text']}" if total > 1 else item["text"]))
    if len(consoleLines) > 0:
        items.append(TextContent(text=f"<console_output>\n{'\n'.join(consoleLines)}\n</console_output>"))
    return items


def joinAdjacentText(items: Sequence[TextContent | ImageContent]) -> list[TextContent | ImageContent]:
    """Join adjacent text items into one, each part starting on its own line. The separator check looks
    at the last non-empty part, not the joined text: rebuilding a growing concatenation on every item
    is quadratic (#10678)."""
    joined: list[TextContent | ImageContent] = []
    parts: list[str] = []
    lastPart = ""

    def flush() -> None:
        nonlocal parts, lastPart
        if len(parts) > 0:
            joined.append(TextContent(text="".join(parts)))
        parts = []
        lastPart = ""

    for item in items:
        if not isinstance(item, TextContent):
            flush()
            joined.append(item)
            continue
        if lastPart != "" and not lastPart.endswith("\n"):
            parts.append("\n")
            lastPart = "\n"
        parts.append(item.text)
        if item.text != "":
            lastPart = item.text
    flush()
    return joined


def formatCallSummary(calls: Sequence[CodemodeNestedCall]) -> str:
    if len(calls) == 0:
        return "No tool calls were made."
    listed = ", ".join(f"{call['name']} ({call['status']})" for call in calls)
    return f"Tool calls made before the failure (they are not undone): {listed}"


def formatError(result: CodemodeFailure, calls: Sequence[CodemodeNestedCall]) -> str:
    error = result["error"]
    kind = error["kind"]
    if kind == "script":
        head = nullishCoalesce(error.get("stack"), f"{nullishCoalesce(error.get('name'), 'Error')}: {error['message']}")
    elif kind == "timeout":
        head = f"Script timed out: {error['message']}"
    elif kind == "aborted":
        head = f"Script aborted: {error['message']}"
    else:
        head = f"Script sandbox failed: {error['message']}"
    return f"{head}\n\n{formatCallSummary(calls)}"


# MISAKA fork: `misaka-codemode` rather than `pi-codemode`, as misaka names its files.
_OUTPUT_FILE_PREFIX = "misaka-codemode"


async def spillOutput(text: str) -> dict[str, str]:
    """Write the full text output to a temp file, like bash does for truncated output."""
    try:
        return {"path": await writeOutputFile(_OUTPUT_FILE_PREFIX, ".txt", text)}
    except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: reported in the result
        return {"error": str(error)}


# File extensions of the image types `image()` accepts. Must list every type the sandbox's `image()` detects.
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


async def saveImages(items: Sequence[TextContent | ImageContent]) -> list[TextContent | ImageContent]:
    """Save each image to a temp file and put a text item with its path before it. The model sees the
    image but has no other way to reach its bytes: scripts cannot write files, and ``write`` only takes
    text. Images shown more than once are saved once."""
    labels: dict[str, asyncio.Future[str]] = {}

    async def label(image: ImageContent) -> str:
        data = bufferFromBase64(image.data)
        kind = f"{image.mimeType}, {formatSize(len(data))}"
        extension = IMAGE_EXTENSIONS.get(image.mimeType)
        if not extension:
            raise RuntimeError(f"No file extension for image type {image.mimeType}")
        # A failed write (disk full, unwritable temp dir) must not discard the result of a script whose
        # tool calls already ran, so it becomes part of the label.
        try:
            path = await writeOutputFile(_OUTPUT_FILE_PREFIX, extension, data)
            return f"[Image saved to {path} ({kind})]"
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: reported in the label
            return f"[Image ({kind}) could not be saved: {errorMessage(error)}]"

    async def expand(item: TextContent | ImageContent) -> list[TextContent | ImageContent]:
        if not isinstance(item, ImageContent):
            return [item]
        pending = labels.get(item.data)
        if pending is None:
            pending = asyncio.ensure_future(label(item))
            labels[item.data] = pending
        return [TextContent(text=await pending), item]

    result = await asyncio.gather(*(expand(item) for item in items))
    return [part for parts in result for part in parts]


async def truncateOutput(items: list[TextContent | ImageContent], maxTokens: int) -> tuple[list[TextContent | ImageContent], str | None]:
    """Apply the token budget: when the combined text exceeds it, the text items become one item that
    keeps the start and end of the text, and images follow it. The full text is written to a temp
    file."""
    texts = [item.text for item in items if isinstance(item, TextContent)]
    combined = "\n".join(texts)
    budget = maxTokens * CHARS_PER_TOKEN
    # Lengths and slices count UTF-16 code units, as `String.prototype.length` and `slice` do.
    length = jsLength(combined)
    if len(texts) == 0 or length <= budget:
        return items, None
    headChars = budget // 2
    tailChars = budget - headChars
    removed = length - headChars - tailChars
    head = jsSlice(combined, 0, headChars)
    tail = jsSlice(combined, -tailChars) if tailChars > 0 else ""
    text = (
        f"Warning: truncated output (original token count: {-(-length // CHARS_PER_TOKEN)})\n"
        f"Total output lines: {len(combined.split(chr(10)))}\n\n{head}…{-(-removed // CHARS_PER_TOKEN)} tokens truncated…{tail}"
    )
    spilled = await spillOutput(combined)
    text += (
        f"\n\n[Full output: {spilled['path']} (read with offset/limit)]"
        if "path" in spilled
        else f"\n\n[Could not save the full output: {spilled['error']}]"
    )
    images = [item for item in items if isinstance(item, ImageContent)]
    return [TextContent(text=text), *images], spilled.get("path")


def toScriptValue(tool: Any, outcome: Any) -> Any:
    """The value a script receives for a nested call: a tool that declares ``outputSchema`` resolves
    to its ``structuredContent``, also for error results that carry one (such as MCP results with
    ``isError``); any other tool resolves to its text content. Other failures reject with the tool's
    error text."""
    result = outcome.result
    structured = read_field(result, "structuredContent")
    if read_field(tool, "outputSchema") is not None and structured is not None:
        return structured
    text = textOf(result)
    if outcome.isError:
        raise RuntimeError(text or f'Tool "{read_field(tool, "name")}" failed')
    return text


async def executeCodemode(
    toolCallId: str,
    input: Mapping[str, Any],
    signal: Any,
    onUpdate: Callable[[AgentToolResult], None] | None,
    ctx: Any,
    options: CodemodeToolOptions | None = None,
) -> AgentToolResult:
    """Run one script. Without a session context (a plain Agent or a direct call) scripts cannot
    call tools, ``store()`` starts empty, and writes are dropped."""
    options = options or CodemodeToolOptions()
    startedAt = _now()
    parsed = parseCodemodeSource(input["code"])
    code, sourceOptions = parsed["code"], parsed["options"]
    calls: list[CodemodeNestedCall] = []
    # Usage of the script's `models.*` calls. Nested tool calls report theirs through the session.
    modelUsage: Usage | None = None
    # Images returned by `models.generateImages()`, to notice a script that never shows them.
    generatedImages = 0

    def addGeneratedImages(count: int) -> None:
        nonlocal generatedImages
        generatedImages += count

    def addModelUsage(usage: Usage) -> None:
        nonlocal modelUsage
        modelUsage = combineUsage(modelUsage, usage) if modelUsage is not None else usage

    def snapshot() -> CodemodeToolDetails:
        return {"calls": [dict(call) for call in calls]}  # type: ignore[misc]

    def publish() -> None:
        if onUpdate is not None:
            onUpdate(AgentToolResult(content=[], details=snapshot()))

    # Show the script as running before its first nested call, so the row's timer starts.
    publish()

    callable = getCodemodeCallableTools(ctx.tools) if ctx is not None else []
    # ALL_TOOLS entries carry the declaration.
    guidelines = options.getToolGuidelines() if options.getToolGuidelines is not None else None
    samples = {
        tool.name: renderToolSample(toCodemodeDeclaration(tool, guidelines.get(tool.name, ()) if guidelines else ()))
        for tool in callable
    }

    def sandboxTool(tool: Any) -> CodemodeTool:
        async def execute(args: Any, context: Any) -> Any:
            record: CodemodeNestedCall = {
                "id": f"{toolCallId}/?",
                "name": tool.name,
                "args": previewArgs(args),
                "status": "running",
            }
            calls.append(record)
            publish()
            callStartedAt = _now()
            # Only tools from ctx.tools are callable, so ctx is set here.
            if ctx is None:
                raise RuntimeError("Tool calls need a session")
            callSignal = context["signal"]
            outcome = await ctx.executeTool(tool.name, None if args is UNDEFINED else args, {"signal": callSignal})
            record["id"] = outcome.toolCall.id
            record["durationMs"] = _now() - callStartedAt
            if outcome.isError:
                record["status"] = "cancelled" if callSignal.aborted else "error"
                record["error"] = truncateText(textOf(outcome.result) or f'Tool "{tool.name}" failed', ERROR_PREVIEW_CHARS)
            else:
                record["status"] = "ok"
            publish()
            return toScriptValue(tool, outcome)

        return CodemodeTool(name=tool.name, description=samples.get(tool.name), execute=execute)

    sandboxTools = [sandboxTool(tool) for tool in callable]

    # The wait for a worker counts toward the script's deadline.
    deadline = sourceOptions.get("timeoutMs", math.inf)
    waitStartedAt = _now()
    admission = await reserveCodemodeWorker(signal, min(deadline, CODEMODE_WORKER_WAIT_MS))
    waitedMs = _now() - waitStartedAt
    if admission != "reserved":
        waited = toFixed((_now() - startedAt) / 1000, 1)
        reason = signal.reason if signal is not None else None
        if admission == "aborted":
            error = f"Script aborted: {errorMessage(reason) if isinstance(reason, BaseException) else 'Execution aborted'}"
        elif deadline <= CODEMODE_WORKER_WAIT_MS:
            error = (f"Script timed out: Execution timed out after {jsString(deadline)} ms while waiting for a free "
                     "codemode worker")
        else:
            error = (f"No codemode worker became free within {jsString(CODEMODE_WORKER_WAIT_MS / 1000)} seconds. All "
                     f"{maxCodemodeWorkers()} workers, shared by every session in this process, are held by other "
                     "scripts. Retry after they finish.")
        return AgentToolResult(
            content=[
                TextContent(text=f"Script failed\nWall time {waited} seconds\nOutput:\n"),
                TextContent(text=f"Script error:\n{error}\n\nThe script did not start."),
            ],
            details=snapshot(),
            isError=True,
        )
    # The reservation covers the one worker this script starts. A launch that resumes after the result
    # settled finds it released and starts no worker.
    reserved = True

    def acquireWorker() -> Callable[[], None]:
        nonlocal reserved
        if not reserved:
            raise RuntimeError("The script already settled")
        # From here the sandbox releases the slot when the worker actually exits.
        reserved = False
        return releaseCodemodeWorker

    sandbox: CodemodeSandbox | None = None
    result: CodemodeResult
    try:
        sandbox = CodemodeSandbox({
            "tools": sandboxTools,
            "globals": [
                *createDiscoveryGlobals(callable, samples, options),
                *(
                    createModelGlobals(ctx.modelRegistry, toolCallId, calls, publish, addModelUsage, addGeneratedImages)
                    if options.models and ctx is not None
                    else []
                ),
            ],
            # `Math.max(1, Math.round(deadline - waitedMs))`; Python cannot round an unset (infinite) deadline.
            "timeoutMs": deadline if math.isinf(deadline) else max(1, jsRound(deadline - waitedMs)),
            "memoryLimitBytes": CODEMODE_MEMORY_LIMIT_BYTES,
            "acquireWorker": acquireWorker,
        })
        store = readCodemodeStore(ctx.sessionManager.getBranch()) if ctx is not None else {}
        result = await sandbox.execute(code, {"signal": signal, "store": store})
    finally:
        if sandbox is not None:
            await sandbox.close()
        if reserved:
            reserved = False
            releaseCodemodeWorker()
    # Calls still marked running were cut off by the script ending, a timeout, or an abort.
    for call in calls:
        if call["status"] == "running":
            call["status"] = "cancelled"

    scriptOutput: list[CodemodeOutputItem] = [*result["output"]]
    if result["ok"]:
        set_, deleted = result["storeWrites"]["set"], result["storeWrites"]["delete"]
        if (len(set_) > 0 or len(deleted) > 0) and options.appendEntry is not None:
            options.appendEntry(CODEMODE_STORE_ENTRY_TYPE, {"set": set_, "delete": deleted})
        # pi extension: a returned value is appended like text().
        if result["value"] is not UNDEFINED:
            scriptOutput.append({"type": "text", "text": valueText(result["value"])})
    items = formatOutput(scriptOutput)
    if not result["ok"]:
        items.append(TextContent(text=f"Script error:\n{formatError(result, calls)}"))
    if generatedImages > 0 and not any(isinstance(item, ImageContent) for item in items):
        items.append(TextContent(text=(
            f"Note: models.generateImages() returned {generatedImages} image{'' if generatedImages == 1 else 's'} "
            "that the script did not show. Show each image block of result.output with image(block)."
        )))

    truncated, fullOutputPath = await truncateOutput(
        joinAdjacentText(items), sourceOptions.get("maxOutputTokens", DEFAULT_MAX_OUTPUT_TOKENS)
    )
    # After truncation, which joins the text items and moves images after them, so each path stays
    # next to its image and is never cut.
    output = joinAdjacentText(await saveImages(truncated))
    wallTime = toFixed((_now() - startedAt) / 1000, 1)
    header = f"{'Script completed' if result['ok'] else 'Script failed'}\nWall time {wallTime} seconds\nOutput:\n"
    details = snapshot()
    if fullOutputPath is not None:
        details["fullOutputPath"] = fullOutputPath
    return AgentToolResult(
        content=[TextContent(text=header), *output],
        details=details,
        usage=modelUsage,
        isError=None if result["ok"] else True,
    )


def isNamespaceName(namespace: ToolNamespace, query: str) -> bool:
    """Whether ``query`` names the namespace: its name or one of its aliases, as written or as the same
    script identifier (``dev-radius`` and ``dev_radius`` are both ``dev_radius``)."""
    queryId = toCodemodeIdentifier(query)
    return any(
        name == query or toCodemodeIdentifier(name) == queryId
        for name in [namespace["name"], *(namespace.get("aliases") or [])]
    )


def createDiscoveryGlobals(tools: Sequence[Any], samples: Mapping[str, str], options: CodemodeToolOptions) -> list[CodemodeTool]:
    """``searchTools()``, ``describeTool()``, and ``describeNamespace()``: ranked search and lookup
    over the script's nested tools and their namespaces."""
    ranker = Bm25Ranker()

    def entry(name: str) -> dict[str, str]:
        return {"name": toCodemodeIdentifier(name), "description": samples.get(name, "")}

    def getNamespace(name: str) -> Any:
        return options.getToolNamespace(name) if options.getToolNamespace is not None else None

    def searchTools(args: Any, _context: Any) -> Any:
        query = args[0] if len(args) > 0 else None
        searchOptions = args[1] if len(args) > 1 else None
        if not isinstance(query, str):
            raise RuntimeError("searchTools() expects a query string")  # noqa: TRY004 - upstream `new Error`
        limit = searchOptions.get("limit") if isinstance(searchOptions, dict) else None
        if limit is None:
            limit = DEFAULT_TOOL_SEARCH_LIMIT
        if isinstance(limit, bool) or not isinstance(limit, int | float) or limit != int(limit) or limit <= 0:
            raise RuntimeError("searchTools() limit must be a positive integer")
        namespace = searchOptions.get("namespace") if isinstance(searchOptions, dict) else None
        if namespace is not None and not isinstance(namespace, str):
            raise RuntimeError("searchTools() namespace must be a string")
        documents = []
        for tool in tools:
            toolNamespace = getNamespace(tool.name)
            if namespace and (toolNamespace is None or not isNamespaceName(toolNamespace, namespace)):
                continue
            documents.append(createToolSearchDocument(tool, toolNamespace))
        return [entry(match.name) for match in ranker.rank(query, documents, int(limit))]

    def describeTool(args: Any, _context: Any) -> Any:
        name = args[0] if len(args) > 0 else None
        if not isinstance(name, str):
            raise RuntimeError("describeTool() expects a tool name")  # noqa: TRY004 - upstream `new Error`
        tool = next((candidate for candidate in tools if candidate.name == name or toCodemodeIdentifier(candidate.name) == name), None)
        return samples.get(tool.name) if tool is not None else UNDEFINED

    def describeNamespace(args: Any, _context: Any) -> Any:
        name = args[0] if len(args) > 0 else None
        if not isinstance(name, str):
            raise RuntimeError("describeNamespace() expects a namespace name")  # noqa: TRY004 - upstream `new Error`
        namespace: Any = None
        names: list[str] = []
        for tool in tools:
            toolNamespace = getNamespace(tool.name)
            if toolNamespace is None or not isNamespaceName(toolNamespace, name):
                continue
            if namespace is None:
                namespace = toolNamespace
            names.append(toCodemodeIdentifier(tool.name))
        if namespace is None:
            return UNDEFINED
        return {
            "name": namespace["name"],
            **({"description": namespace["description"]} if namespace.get("description") else {}),
            **({"instructions": namespace["instructions"]} if namespace.get("instructions") else {}),
            "tools": names,
        }

    return [
        CodemodeTool(name="searchTools", spread=True, execute=searchTools),
        CodemodeTool(name="describeTool", spread=True, execute=describeTool),
        CodemodeTool(name="describeNamespace", spread=True, execute=describeNamespace),
    ]


def _dump(result: Any) -> Any:
    """A classifier or image result as the JSON the script receives: fields the service left unset
    are absent, as `undefined` properties are."""
    return result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result


def createModelGlobals(
    models: CodemodeModelRuntime,
    toolCallId: str,
    calls: list[CodemodeNestedCall],
    publish: Callable[[], None],
    addUsage: Callable[[Usage], None],
    addGeneratedImages: Callable[[int], None],
) -> list[CodemodeTool]:
    """``models.*`` for scripts: the model registry methods documented in codemode.md. Classifier and
    image calls appear as nested call rows so the renderer shows them, and their usage goes to
    ``addUsage``. Rows show only the model, never prompts or image data."""
    limit = createLimiter(MAX_CONCURRENT_MODEL_CALLS)
    callCount = 0

    async def runModelCall(
        name: str,
        type: str,
        args: Sequence[Any],
        checkContext: Callable[[Any], Any],
        run: Callable[[Any, Any], Awaitable[Any]],
    ) -> Any:
        """Resolve the script's model by provider and id only, check the context, then run the call as
        a nested call row. A script-supplied baseUrl or headers must never receive the credentials."""
        nonlocal callCount
        model = args[0] if len(args) > 0 else UNDEFINED
        context = args[1] if len(args) > 1 else UNDEFINED
        listHint = f'List the {type} models you can use with models.getAvailableOfType("{type}").'
        if not isRecord(model) or not isinstance(model.get("provider"), str) or not isinstance(model.get("id"), str):
            # undefined arrives as null: spread arguments cross the sandbox as a JSON array.
            undefinedHint = (
                " models.getModelOfType() returns undefined for an unknown provider or id."
                if model is None or model is UNDEFINED
                else ""
            )
            raise RuntimeError(
                f"{name}() expects {withArticle(type)} model as its first argument, got {describeValue(model)}.{undefinedHint} {listHint}"
            )
        provider, id = model["provider"], model["id"]
        ref = f"{provider}/{id}"
        resolved = models.getModelOfType(type, provider, id)
        if resolved is None:
            actualType = next(
                (other for other in sorted(MODEL_TYPES) if other != type and models.getModelOfType(other, provider, id) is not None),
                None,
            )
            raise RuntimeError(
                f'"{ref}" is {withArticle(actualType)} model, not {withArticle(type)} model. {listHint}'
                if actualType is not None
                else f'Unknown {type} model "{ref}". {listHint}'
            )
        checked = checkContext(context)

        callCount += 1
        record: CodemodeNestedCall = {
            "id": f"{toolCallId}/{name}/{callCount}",
            "name": name,
            "args": f"{resolved.provider}/{resolved.id}",
            "status": "running",
        }
        calls.append(record)
        publish()
        startedAt = _now()
        result = await limit(lambda: run(resolved, checked))
        record["durationMs"] = _now() - startedAt
        record["status"] = "ok" if result.stopReason == "stop" else "cancelled" if result.stopReason == "aborted" else "error"
        if result.errorMessage:
            record["error"] = truncateText(result.errorMessage, ERROR_PREVIEW_CHARS)
        if result.usage is not None:
            record["cost"] = result.usage.cost.total
            addUsage(result.usage)
        publish()
        return _dump(result)

    def getModelsOfType(args: Any, _context: Any) -> Any:
        type = args[0] if len(args) > 0 else UNDEFINED
        provider = args[1] if len(args) > 1 else UNDEFINED
        return [toModelInfo(model) for model in models.getModelsOfType(toModelType(type), toProvider(provider))]

    async def getAvailableOfType(args: Any, context: Any) -> Any:
        type = args[0] if len(args) > 0 else UNDEFINED
        provider = args[1] if len(args) > 1 else UNDEFINED
        available = await models.getAvailableOfType(toModelType(type), toProvider(provider), AuthOperationOptions(signal=context["signal"]))
        return [toModelInfo(model) for model in available]

    def getModelOfType(args: Any, _context: Any) -> Any:
        type = args[0] if len(args) > 0 else UNDEFINED
        provider = args[1] if len(args) > 1 else UNDEFINED
        id = args[2] if len(args) > 2 else UNDEFINED
        if not isinstance(provider, str) or not isinstance(id, str):
            raise RuntimeError(  # noqa: TRY004 - upstream `new Error`
                f"models.getModelOfType(type, provider, id) expects three strings, got ({', '.join(describeValue(arg) for arg in args)}). "
                'The provider and the id are separate arguments, for example models.getModelOfType("classifier", "typesafe", "jev-latest").'
            )
        model = models.getModelOfType(toModelType(type), provider, id)
        return UNDEFINED if model is None else toModelInfo(model)

    def classify(args: Any, context: Any) -> Any:
        return runModelCall(
            "models.classify",
            "classifier",
            args,
            checkClassifierContext,
            lambda resolved, checked: models.classify(resolved, checked, {"signal": context["signal"]}),
        )

    def generateImages(args: Any, context: Any) -> Any:
        async def run(resolved: Any, checked: Any) -> Any:
            result = await models.generateImages(resolved, checked, {"signal": context["signal"]})
            addGeneratedImages(sum(1 for block in result.output if read_field(block, "type") == "image"))
            return result

        return runModelCall("models.generateImages", "image", args, checkImagesContext, run)

    implementations: dict[str, Callable[[Any, Any], Any]] = {
        "models.getModelsOfType": getModelsOfType,
        "models.getAvailableOfType": getAvailableOfType,
        "models.getModelOfType": getModelOfType,
        "models.classify": classify,
        "models.generateImages": generateImages,
    }
    return [CodemodeTool(name=name, spread=True, execute=execute) for name, execute in implementations.items()]


__all__ = ["createDiscoveryGlobals", "createModelGlobals", "executeCodemode", "readCodemodeStore"]
