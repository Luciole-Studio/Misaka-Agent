# ruff: noqa: UP040 - `ModelsErrorCode` is spelled with `TypeAlias`, not the PEP 695 `type` statement.

"""Translated from pi's ``packages/ai/src/utils/models-error.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias, TypedDict

from misaka.ai.utils.diagnostics import formatThrownValue
from misaka.utils.js import jsTrim

ModelsErrorCode: TypeAlias = Literal["model_source", "model_validation", "provider", "stream", "auth", "oauth"]


class _ModelsErrorOptions(TypedDict, total=False):
    cause: Any


class ModelsError(Exception):
    def __init__(self, code: ModelsErrorCode, message: str, options: _ModelsErrorOptions | None = None) -> None:
        cause = None if options is None else options.get("cause")
        super().__init__(_withCauseDetail(message, cause))
        # MISAKA fork: `Error`'s `cause` option is `__cause__`, which takes only an exception.
        if isinstance(cause, BaseException):
            self.__cause__ = cause
        self.name = "ModelsError"
        self.code: ModelsErrorCode = code


def _withCauseDetail(message: str, cause: Any) -> str:
    """Callers surface `error.message` only, so keep the underlying reason in it."""
    if cause is None:
        return message
    detail = jsTrim(formatThrownValue(cause))
    if not detail or detail in message:
        return message
    return f"{message}: {detail}"


__all__ = ["ModelsError", "ModelsErrorCode"]
