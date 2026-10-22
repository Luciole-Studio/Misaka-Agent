"""Translated from pi's ``packages/durable/src/errors.ts``."""

from __future__ import annotations

from typing import Any

from misaka.utils.js import errorMessage as jsErrorMessage
from misaka.utils.js import jsString


def errorMessage(error: Any) -> str:
    """The message of a thrown value, without throwing itself: ``String()`` throws for values such as
    ``Object.create(null)``, and code that formats an error must not fail for the value it formats."""
    try:
        if isinstance(error, BaseException):
            return jsErrorMessage(error)
        return jsString(error)
    except Exception:  # noqa: BLE001 - formatting must not fail for the value it formats
        # `Object.prototype.toString.call(error)`. MISAKA fork: upstream reads an `Error`'s `message` property, which
        # cannot throw, so it takes this path only for a non-error; a Python exception's message is its `str()`, which
        # its class can make raise, so an exception may reach it too. The type name stands in for the `[object Tag]`
        # tag, which a Python value does not have.
        return f"[object {type(error).__name__}]"


class ReadAfterWrite(Exception):
    """A transaction read a table after its first table write. Read every required row before writing."""

    def __init__(self, method: str) -> None:
        super().__init__(f"Tx.{method}() cannot read tables after the first table write")
        self.name = "ReadAfterWrite"


class StorageRequestError(Exception):
    """A Storage read rejected an invalid request, such as an unknown conversation or a cursor from another scan, with no
    durable effect. Unlike any other error a Storage throws, it fails only that read, not the Session; from ``commit()``
    it is fatal like any other."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "StorageRequestError"


class SessionFailed(Exception):
    """The Session failed, so nothing runs or commits any more. It fails when a Storage method throws, a commit cannot be
    adopted, or the Harness's own bookkeeping throws (a scheduler commit, an internal commit listener, a registry whose
    ``snapshot()`` throws). ``cause`` is that first error. Reopen the Session; reopening recovers from what was
    committed."""

    def __init__(self, cause: Any) -> None:
        super().__init__("Session failed after a storage error; close and reopen it")
        self.name = "SessionFailed"
        # `{ cause }`: Error keeps it as `cause`, which Python also chains when it is an exception.
        self.cause = cause
        if isinstance(cause, BaseException):
            self.__cause__ = cause


class ConversationBusy(Exception):
    """A submission reached a busy conversation and was not admitted."""

    def __init__(self, conversationId: int) -> None:
        super().__init__(f"Conversation {conversationId} is busy")
        self.name = "ConversationBusy"
        self.conversationId = conversationId


__all__ = ["ConversationBusy", "ReadAfterWrite", "SessionFailed", "StorageRequestError", "errorMessage"]
