"""Admission, waits and withdrawal of the durable submissions of one Harness.

Translated from pi's ``packages/durable/src/harness/submissions.ts`` (v1.3.0). A draft, a record and a boundary are plain ``dict``
objects with the upstream keys; public async methods are ``@eager`` (they run when called, as JavaScript async functions
do) and return a future.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from misaka.chord.context import Context
from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED, eager, jsString, resolvedPromise

from ..errors import ConversationBusy
from ..types import ConversationId, Storage, SubmissionId, SubmissionRecord, Tx
from .generation import startRun
from .inbox import (
    InboxDoc,
    appendInput,
    applyBoundary,
    hasQueuedReset,
    isDisplayOnly,
    isStale,
    pickInputEntry,
    prepareBoundary,
    removeInboxItem,
)
from .live import LiveDoc
from .util import LineWait, Waiters, closedError


class Submissions:
    """Admission, waits, and withdrawal of the durable submissions of one Harness."""

    def __init__(
        self,
        session: Any,
        storage: Storage,
        now: Callable[[], int],
        queueModes: Callable[[], dict[str, Any]],
        resume: Callable[[], None],
    ) -> None:
        self._session = session
        self._storage = storage
        self._now = now
        self._queueModes = queueModes  # Read at each admission, on the Session line.
        self._resume = resume  # Enable task scheduling; submitting or waiting asks for progress.
        self._waiters = Waiters()
        self._closed = False

        def onClose() -> None:
            self._closed = True
            self._waiters.rejectAll(closedError(self._session))

        session.observeCommits(lambda publication, context: self._observe(publication))
        session.subscribeClose(onClose)

    @eager
    async def submit(self, conversationId: ConversationId, draft: dict[str, Any], context: Context) -> Any:
        """Admit a submission in one commit; see ``admitSubmission()``."""
        self._resume()
        id = await self._session.commitWith(
            lambda tx: admitSubmission(tx, conversationId, draft, self._now(), self._queueModes()), context
        )
        return SubmissionHandle(id, self)

    @eager
    async def get(self, id: SubmissionId, context: Context) -> Any:
        """Handle for an existing submission, or ``None``."""
        record = await self._session.readOnLine(lambda: self._storage.submission(id, context))
        return None if record is None else SubmissionHandle(record["id"], self)

    @eager
    async def status(self, id: SubmissionId, context: Context) -> SubmissionRecord:
        record = await self._session.readOnLine(lambda: self._storage.submission(id, context))
        if record is None:
            raise RuntimeError(f"Submission {id} does not exist")
        return record  # type: ignore[no-any-return]

    @eager
    async def wait(self, id: SubmissionId, context: Context) -> dict[str, Any]:
        self._resume()
        line = LineWait()

        # Check and register on the line so no settling publication falls between them.
        async def job() -> dict[str, Any]:
            record = await self._storage.submission(id, context)
            if record is None:
                raise RuntimeError(f"Submission {id} does not exist")
            if isSettled(record):
                return {"promise": resolvedPromise(record)}
            # Close rejects registered waiters synchronously and may begin during the read.
            if self._closed:
                raise closedError(self._session)
            return {"promise": line.register(self._waiters.add(id, context))}

        try:
            found = await self._session.readOnLine(job)
        except BaseException:
            line.abandon()
            raise
        return await found["promise"]  # type: ignore[no-any-return]

    def abort(self, id: SubmissionId, context: Context, conversationId: ConversationId | None = None) -> asyncio.Future[str]:
        """Withdraw a queued submission and remove its inbox item; placed inputs and settled submissions are
        reported."""

        async def withdraw(tx: Tx) -> str:
            record = await tx.submission(id)
            if record is None or (conversationId is not None and record["conversationId"] != conversationId):
                return "not_found"
            if record["status"] == "queued":
                tx.settleSubmission(id, {"status": "unanswered", "reason": "aborted"})
                await removeInboxItem(tx, record["conversationId"], id)
                return "aborted"
            return "already_placed" if record["status"] == "placed" else "settled"

        return self._session.commitWith(withdraw, context)  # type: ignore[no-any-return]

    def _observe(self, publication: dict[str, Any]) -> None:
        for change in publication["changes"]:
            if change["type"] != "submission" or not isSettled(change["value"]):
                continue
            self._waiters.resolve(change["value"]["id"], change["value"])


class SubmissionHandle:
    """Awaitable host object for one durably admitted submission."""

    def __init__(self, id: SubmissionId, submissions: Submissions) -> None:
        self.id = id
        self._submissions = submissions

    def status(self, context: Context) -> asyncio.Future[SubmissionRecord]:
        return self._submissions.status(self.id, context)

    def wait(self, context: Context) -> asyncio.Future[dict[str, Any]]:
        return self._submissions.wait(self.id, context)

    @eager
    async def abort(self, context: Context) -> str:
        result = await self._submissions.abort(self.id, context)
        if result == "not_found":
            raise RuntimeError(f"Submission {self.id} does not exist")
        return result


def isSettled(record: SubmissionRecord) -> bool:
    return record["status"] == "done" or record["status"] == "unanswered"


def checkInputEntry(entry: Any) -> None:
    """Reject an input entry an untyped caller got wrong: its kind must be a non-empty string outside the ``pi.``
    prefix."""
    if entry is None:
        # Destructuring `null`, as upstream's `const { kind } = entry` does.
        raise TypeError("Cannot destructure property 'kind' of 'entry' as it is null.")
    kind = entry.get("kind", UNDEFINED) if isinstance(entry, dict) else UNDEFINED
    if not isinstance(kind, str) or len(kind) == 0 or kind.startswith("pi."):
        raise TypeError(
            f'An input entry\'s kind must be a non-empty string not starting with "pi.", got {jsString(kind)}'
        )


async def admitSubmission(
    tx: Tx, conversationId: ConversationId, draft: dict[str, Any], now: int, queueModes: dict[str, Any]
) -> SubmissionId:
    """Admit a submission inside a commit (spec section 6); ``Conversation.submit()`` and conversation-owned compactions
    share it. A known request ID returns its existing submission without writing. A display-only write (only ``kind`` and
    ``data``) is appended and settled ``done`` at once, busy or not, as it changes nothing the model sees; only behind a
    queued reset it queues, so it lands after the reset instead of before it in the context the reset ends. A busy
    conversation queues anything else in ``pi.inbox``, or rejects ``whenBusy: "reject"`` input with
    ``ConversationBusy``. An idle conversation with queued items queues it behind them and runs a final boundary.
    Otherwise idle input places its entry (``pi.user``, or the input's own) and starts a run, and an idle write appends
    its entry and settles ``done``, or ``stale`` when its head reaches before the active range."""
    # An absent entry is `undefined`; an explicit `None` is `null`, which the check rejects.
    if draft["type"] == "input" and draft.get("entry", UNDEFINED) is not UNDEFINED:
        checkInputEntry(draft["entry"])
    if draft.get("requestId") is not None:
        existing = await tx.submissionByRequest(conversationId, draft["requestId"])
        if existing is not None:
            if existing["type"] != draft["type"]:
                raise RuntimeError(
                    f"Request {draft['requestId']} already identifies a submission of type {existing['type']}"
                )
            return existing["id"]  # type: ignore[no-any-return]
    requestId = {} if draft.get("requestId") is None else {"requestId": draft["requestId"]}
    if (
        draft["type"] == "write"
        and isDisplayOnly(draft["entry"])
        and not hasQueuedReset((await tx.doc(InboxDoc, conversationId))["items"])
    ):
        entry = await tx.appendEntry(conversationId, draft["entry"])
        write = {"conversationId": conversationId, **requestId, "type": "write", "status": "done", "entry": entry["id"]}
        return (await tx.createSubmission(write))["id"]  # type: ignore[no-any-return]
    live = await tx.doc(LiveDoc, conversationId)
    busy = "run" in live
    if busy and draft["type"] == "input" and draft.get("whenBusy") == "reject":
        raise ConversationBusy(conversationId)
    # A boundary reads the table, so it is prepared before the first table write; a busy one needs none.
    boundary = None if busy else await prepareBoundary(tx, conversationId, queueModes)
    if boundary is None or len(boundary["inbox"]["items"]) > 0:
        created = await tx.createSubmission(
            {"conversationId": conversationId, **requestId, "type": draft["type"], "status": "queued"}
        )
        id = created["id"]
        # Hosts may leave optional fields ``undefined``; drafts take strict JSON.
        value = copyJson(
            draft["entry"] if draft["type"] == "write" else draft["content"], {"omitUndefinedProperties": True}
        )
        items = (boundary["inbox"] if boundary is not None else await tx.doc(InboxDoc, conversationId))["items"]
        if draft["type"] == "write":
            items.push({"id": id, "mode": "write", "entry": value})
        else:
            mode = "steer" if draft.get("whenBusy") == "steer" else "followUp"
            entryField = {} if draft.get("entry") is None else {"entry": pickInputEntry(draft["entry"])}
            items.push({"id": id, "mode": mode, "content": value, **entryField})
        if boundary is None:
            return id  # type: ignore[no-any-return]
        result = await applyBoundary(tx, boundary, "final", now)
        if len(result["users"]) > 0:
            await startRun(tx, conversationId, live, result["users"])
        return id  # type: ignore[no-any-return]
    if draft["type"] == "write":
        if isStale(boundary, draft["entry"]):
            stale = {"status": "unanswered", "reason": "stale"}
            created = await tx.createSubmission({"conversationId": conversationId, **requestId, "type": "write", **stale})
            return created["id"]  # type: ignore[no-any-return]
        entry = await tx.appendEntry(conversationId, draft["entry"])
        write = {
            "conversationId": conversationId,
            **requestId,
            "type": "write",
            "status": "done",
            "entry": entry["id"],
        }
        return (await tx.createSubmission(write))["id"]  # type: ignore[no-any-return]
    entry = await appendInput(tx, conversationId, draft["content"], draft.get("entry"), now)
    placed = {
        "conversationId": conversationId,
        **requestId,
        "type": "input",
        "status": "placed",
        "entry": entry["id"],
    }
    created = await tx.createSubmission(placed)
    id = created["id"]
    await startRun(tx, conversationId, live, [id])
    return id  # type: ignore[no-any-return]


__all__ = ["SubmissionHandle", "Submissions", "admitSubmission", "checkInputEntry"]
