"""Error codes callers see, and the helpers that make and collect them.

Translated from pi's ``packages/chord/src/errors.ts``. ``AggregateError`` is an ``ExceptionGroup``; an error's
``cause`` is its ``__cause__``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal

from misaka.chord._promise import asException, isRejection
from misaka.utils.js import UNDEFINED, errorMessage

# Error codes callers see (guide table). A refused or aborted reload keeps the code of its cause.
CHORD_ERROR_CODES: Final = (
    "aborted",
    "contract_mismatch",
    "forbidden",
    "gone",
    "invalid",
    "not_in_variant",
    "not_ready",
    "released",
    "superseded",
    "unavailable",
    "upgrade_conflict",
    "upgrade_required",
)
type ChordErrorCode = Literal[
    "aborted",
    "contract_mismatch",
    "forbidden",
    "gone",
    "invalid",
    "not_in_variant",
    "not_ready",
    "released",
    "superseded",
    "unavailable",
    "upgrade_conflict",
    "upgrade_required",
]

# Finer cause inside a code, for logs and tests: e.g. `invalid` + `args`, `result`, `state`, `input`, `implementation`,
# `duplicate`, `cycle`, `read_only`, `not_host_lifetime`, `registry_conflict`; `unavailable` + `unknown_type`.
type ChordErrorReason = str

RETRYABLE: Final[frozenset[str]] = frozenset({"superseded", "upgrade_conflict"})


class ChordError(Exception):
    """``message`` is ``code[/reason]: message``, as upstream's ``Error.message``; ``str(error)`` is the same."""

    def __init__(
        self,
        code: ChordErrorCode,
        message: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        options = options or {}
        reason = options.get("reason")
        super().__init__(f"{code}{f'/{reason}' if reason else ''}: {message}")
        self.message = self.args[0]
        self.name = "ChordError"
        self.code: ChordErrorCode = code
        self.reason: ChordErrorReason | None = reason
        self.retryable = code in RETRYABLE
        self.details: Any = options.get("details")
        cause = options.get("cause", UNDEFINED)
        # MISAKA fork: `{ cause }` is the exception's `__cause__` when it is one; any cause is kept as `cause`.
        self.cause: Any = None if cause is UNDEFINED else cause
        if isinstance(cause, BaseException):
            self.__cause__ = cause

    def __reduce__(self) -> tuple[Any, ...]:
        """``copy`` and ``pickle``: the constructor takes the parts of the message, not the message (``args``)."""
        return (_rebuild, (type(self), self.args), self.__dict__.copy())


def _rebuild(cls: type[ChordError], args: tuple[Any, ...]) -> ChordError:
    error = Exception.__new__(cls)
    error.args = args
    return error


def isChordError(error: Any, code: ChordErrorCode | None = None) -> bool:
    return isinstance(error, ChordError) and (code is None or error.code == code)


def forbidden(message: str = "forbidden") -> ChordError:
    """For factories and methods refusing a principal: ``raise forbidden()``."""
    return ChordError("forbidden", message)


def invalid(reason: ChordErrorReason, message: str, details: Any = None) -> ChordError:
    return ChordError("invalid", message, {"reason": reason, "details": details})


def asChordError(error: Any, context: str) -> ChordError:
    """Keeps a ChordError's code; anything else becomes ``unavailable`` with the original as cause."""
    if isinstance(error, ChordError):
        return error
    return ChordError("unavailable", f"{context}: {errorMessage(error)}", {"cause": error})


async def runAll(steps: Sequence[Callable[[], Any]], message: str) -> None:
    """Runs every step, then throws one error, or an AggregateError when several failed. A step that is cancelled
    (the running task's own cancellation) or interrupted does not stop the others either: it propagates after them."""
    errors: list[Exception] = []
    interrupted: BaseException | None = None
    for step in steps:
        try:
            result = step()
            if inspect.isawaitable(result):
                await result
        except GeneratorExit:
            raise
        except BaseException as error:  # noqa: BLE001 - every step runs; the failures are thrown together
            if isRejection(error):
                # A rejection-like CancelledError as an Exception: the aggregate holds it, and a task failing with it is
                # reported rather than counted as cancelled.
                errors.append(asException(error))
            elif interrupted is None:
                interrupted = error
    if interrupted is not None:
        raise interrupted
    if len(errors) == 1:
        raise errors[0]
    if len(errors) > 1:
        raise ExceptionGroup(message, errors)


__all__ = [
    "CHORD_ERROR_CODES",
    "ChordError",
    "ChordErrorCode",
    "ChordErrorReason",
    "asChordError",
    "forbidden",
    "invalid",
    "isChordError",
    "runAll",
]
