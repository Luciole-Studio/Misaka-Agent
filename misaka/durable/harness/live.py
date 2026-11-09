"""The built-in live conversation state: run control and presentation of the current generation and tool round.

Translated from pi's ``packages/durable/src/harness/live.ts`` (v1.3.0). The state is a plain ``dict`` (``run``,
``generation``, ``tools``, ``nestedTools``, ``compactions``, ``compactionFailure``); inside a commit it is a draft, read
by item and changed by item assignment and ``del``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from misaka.utils.js import UNDEFINED

from ..documents import defineDoc
from ..types import TaskId, Tx


class SlotProgress(TypedDict, total=False):
    """What a running tool call publishes, shared by model-issued and nested calls: ``output``, ``droppedBytes`` and
    ``droppedLines`` are the retained running output and what the bounds dropped, ``details`` the last ``details()``
    value, and ``diagnostics`` those recorded through ``api.diagnostic()``."""

    callId: str
    name: str
    status: str  # "pending" | "running" | "done"
    output: str
    droppedBytes: int
    droppedLines: int
    details: Any
    diagnostics: list[dict[str, Any]]


class ToolSlot(SlotProgress, total=False):
    """Presentation of one tool call of the current round. ``taskId`` is absent for a call not started yet (sequential
    round) and for a call its request did not offer, which starts ``done`` with the ``entry`` generation wrote;
    ``entry`` is the result entry once done (absent when the tool task faulted or was orphaned)."""

    taskId: TaskId
    entry: int


class NestedToolSummary(TypedDict, total=False):
    """How a nested call ended: an error flag, its execution time, its spend, and up to 500 characters of error text."""

    isError: bool
    durationMs: float
    usage: dict[str, Any]
    error: str


class NestedToolSlot(SlotProgress, total=False):
    """Presentation of one nested call: a call a running tool made through ``executeTool()``. ``callId`` is
    ``<parentCallId>/<key>``, the call's ID in tool events and unique because keys contain no ``/``. The Harness relates
    slots by task ID: ``taskId`` is the call's tool task, ``parentTaskId`` the calling one, model-issued or nested.
    ``arguments`` is what the call was made with, replaced by the arguments it runs with, after repair, ``beforeTool``,
    and coercion, once it starts executing (a model-issued call's arguments are in its assistant entry instead);
    ``summary``, once done, how the call ended, for a status line (the result itself is in the caller's
    ``NestedCallDoc``)."""

    parentCallId: str
    parentTaskId: TaskId
    taskId: TaskId
    arguments: dict[str, Any]
    summary: NestedToolSummary


class CompactionStatus(TypedDict, total=False):
    """Presentation of one live compaction task (spec section 8.7): ``taskId``, ``reason``, ``blocking`` (a generation
    waits for it), ``attempt``, and ``retry`` (``{"at", "error"}``, the durable backoff before the next attempt)."""

    taskId: TaskId
    reason: str
    blocking: bool
    attempt: int
    retry: dict[str, Any]


class CompactionFailure(TypedDict):
    """The newest compaction that failed since a compaction last produced a summary (spec section 8.7): ``error`` is the
    failed or faulted outcome's error message, or why the scheduler orphaned the task."""

    taskId: TaskId
    reason: str
    error: str


LiveDoc = defineDoc(
    {
        "kind": "pi.live",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": dict,
        # REMINDER: a complete base whenever nothing runs (spec section 8.2): no generation and no running tool slot.
        # That holds while idle, in the commit handing a generation over to its tool round, and between tools, so the
        # delta chain spans at most one generation or the overlapping execution of one round's tools. A slot holds
        # output only while running, so every base is small. Do not add a delta-count bound; the tool output
        # benchmark checks this rule; a nested call's result lives in its caller's task documents, never here. MISAKA
        # fork: that benchmark (upstream's storage-benchmark.ts) is not ported.
        "checkpointWhen": lambda value, *_: "generation" not in value
        and not any(slot["status"] == "running" for slot in value.get("tools") or [])
        and not any(slot["status"] == "running" for slot in value.get("nestedTools") or []),
    }
)

RUN_TASK_KINDS = frozenset({"pi.generation"})
"""Built-in task kinds that can own ``pi.live.run``."""
TOOL_TASK_KIND = "pi.tool"
COMPACTION_TASK_KIND = "pi.compaction"


def endRun(tx: Tx, live: Any, taskId: TaskId, settlement: dict[str, Any]) -> None:
    """End the run owned by ``taskId``: settle each of its inputs and remove ``run``. Always removes ``generation``,
    ``tools``, and ``nestedTools``, whose presentation belongs to the ending run."""
    run = live.get("run", UNDEFINED)
    if run is not UNDEFINED and run["taskId"] == taskId:
        for id in run["inputs"]:
            tx.settleSubmission(id, settlement)
        del live["run"]
    if "generation" in live:
        del live["generation"]
    if "tools" in live:
        del live["tools"]
    if "nestedTools" in live:
        del live["nestedTools"]


def addCompactionStatus(live: Any, status: dict[str, Any]) -> None:
    """Add the status of a compaction task created in this commit; statuses stay in task ID order."""
    if live.get("compactions", UNDEFINED) is UNDEFINED:
        live["compactions"] = []
    live["compactions"].push(status)


def compactionStatus(live: Any, taskId: TaskId) -> Any:
    """The status of compaction task ``taskId``, if listed."""
    statuses = live.get("compactions", UNDEFINED)
    if statuses is UNDEFINED:
        return None
    for status in statuses:
        if status["taskId"] == taskId:
            return status
    return None


def removeCompactionStatus(live: Any, taskId: TaskId) -> None:
    """Remove the status of compaction task ``taskId``, and the list once empty."""
    statuses = live.get("compactions", UNDEFINED)
    if statuses is UNDEFINED:
        return
    index = next((at for at, status in enumerate(statuses) if status["taskId"] == taskId), -1)
    if index >= 0:
        statuses.splice(index, 1)
    if len(statuses) == 0:
        del live["compactions"]


def failCompactionStatus(live: Any, taskId: TaskId, error: str) -> None:
    """Remove the status of compaction task ``taskId`` and record that it failed with ``error``."""
    status = compactionStatus(live, taskId)
    if status is not None:
        live["compactionFailure"] = {"taskId": status["taskId"], "reason": status["reason"], "error": error}
    removeCompactionStatus(live, taskId)


def toolSlot(live: Any, taskId: TaskId) -> Any:
    """The slot of tool task ``taskId``, model-issued or nested, if ``pi.live`` still lists it. Nested slots are found by
    binary search: a tool may make thousands of nested calls, each of which looks up its slot in several commits, and
    every element a search reads costs a node in the draft's change tracker. Their task IDs ascend: each slot is
    appended in the commit that creates its task, task IDs ascend in commit order, and removal keeps the order."""
    slots = live.get("tools", UNDEFINED)
    if slots is not UNDEFINED:
        for slot in slots:
            if slot.get("taskId", UNDEFINED) == taskId:
                return slot
    nested = live.get("nestedTools", UNDEFINED)
    if nested is UNDEFINED:
        return None
    low = 0
    high = len(nested) - 1
    while low <= high:
        middle = (low + high) >> 1
        candidate = nested[middle]
        if candidate["taskId"] == taskId:
            return candidate
        if candidate["taskId"] < taskId:
            low = middle + 1
        else:
            high = middle - 1
    return None


def finishSlot(slot: Any) -> None:
    """Mark a slot done and clear its running output; the caller sets a model-issued call's result ``entry``, or a
    nested call's ``summary``. Neither is set when the task faulted or was orphaned."""
    slot["status"] = "done"
    clearProgress(slot)


def removeNestedSlots(live: Any, taskId: TaskId) -> None:
    """Remove the nested slots below tool task ``taskId``, transitively; the list holds parents before their children."""
    nested = live.get("nestedTools", UNDEFINED)
    if nested is UNDEFINED:
        return
    removed = {taskId}
    for slot in nested:
        if slot["parentTaskId"] in removed:
            removed.add(slot["taskId"])
    for index in range(len(nested) - 1, -1, -1):
        if nested[index]["parentTaskId"] in removed:
            nested.splice(index, 1)
    if len(nested) == 0:
        del live["nestedTools"]


def clearProgress(slot: Any) -> None:
    """Remove what a tool published while running; its result entry or a rerun replaces it."""
    for key in ("output", "droppedBytes", "droppedLines", "details", "diagnostics"):
        if key in slot:
            del slot[key]


async def settleSchedulerOutcome(tx: Any, record: dict[str, Any], outcome: dict[str, Any]) -> None:
    """Harness cleanup for a terminal outcome the scheduler writes itself (``faulted`` or ``orphaned``). A run task ends
    its run; a tool task's slot is marked done without an entry, and context derivation synthesizes the missing result;
    a compaction task's status is removed and its failure recorded. Ignores other kinds so it never creates ``pi.live`` elsewhere. The scheduler
    calls this without knowing task kinds; the Harness passes it in (spec section 5.4).

    REMINDER: a committed generation partial becomes an aborted assistant entry here, exactly as in the generation
    abort handler, so the transcript keeps what the model produced and ``pi.usage`` counts its spend. The scheduler's
    commit has no task scope, so that entry has no ``byTaskId``. Faults come from task bugs or malformed provider data
    (a non-JSON value in a response), or a commit the Storage rejected without effect; an uncertain storage failure
    poisons the Session instead and writes no outcome."""
    # Imported here: the generation module imports this one.
    from .generation import convertPartial

    if record["kind"] == TOOL_TASK_KIND:
        live = await tx.doc(LiveDoc, record["conversationId"])
        slot = toolSlot(live, record["id"])
        if slot is not None:
            finishSlot(slot)
        removeNestedSlots(live, record["id"])
        return
    if record["kind"] == COMPACTION_TASK_KIND:
        error = outcome["error"]["message"] if outcome["status"] == "faulted" else outcome["reason"]
        failCompactionStatus(await tx.doc(LiveDoc, record["conversationId"]), record["id"], error)
        return
    if record["kind"] not in RUN_TASK_KINDS:
        return
    live = await tx.doc(LiveDoc, record["conversationId"])
    run = live.get("run", UNDEFINED)
    if run is UNDEFINED or run["taskId"] != record["id"]:
        return
    await convertPartial(tx, live, record["conversationId"])
    endRun(
        tx,
        live,
        record["id"],
        {"status": "unanswered", "reason": "faulted", "detail": outcome["error"]["message"]}
        if outcome["status"] == "faulted"
        else {"status": "unanswered", "reason": outcome["reason"]},
    )


__all__ = [
    "CompactionFailure",
    "CompactionStatus",
    "LiveDoc",
    "NestedToolSlot",
    "NestedToolSummary",
    "SlotProgress",
    "ToolSlot",
    "addCompactionStatus",
    "clearProgress",
    "compactionStatus",
    "endRun",
    "failCompactionStatus",
    "finishSlot",
    "removeCompactionStatus",
    "removeNestedSlots",
    "settleSchedulerOutcome",
    "toolSlot",
]
