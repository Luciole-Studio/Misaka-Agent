"""Translated from pi's ``packages/ai/src/utils/diagnostics.ts`` (v1.1.0).

MISAKA fork: the two interfaces are pydantic models; an error's name falls back to its class name and it has a stack
only when it carries a ``stack`` string; ``appendAssistantMessageDiagnostic`` also takes a dict-shaped message (durable
and LCM records).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict


class _DiagnosticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticErrorInfo(_DiagnosticsModel):
    name: str | None = None
    message: str
    stack: str | None = None
    code: str | int | None = None


class AssistantMessageDiagnostic(_DiagnosticsModel):
    type: str
    timestamp: int
    error: DiagnosticErrorInfo | None = None
    details: dict[str, Any] | None = None


def formatThrownValue(value: Any) -> str:
    if isinstance(value, Exception):
        name = getattr(value, "name", value.__class__.__name__)
        return str(value) or name
    if isinstance(value, str):
        return value
    return str(value)


def extractDiagnosticError(error: Any) -> DiagnosticErrorInfo:
    if not isinstance(error, Exception):
        return DiagnosticErrorInfo(name="ThrownValue", message=formatThrownValue(error))

    code = getattr(error, "code", None)
    name = getattr(error, "name", None) or error.__class__.__name__ or None
    stack = getattr(error, "stack", None)
    if not isinstance(stack, str):
        stack = None
    return DiagnosticErrorInfo(
        name=name,
        message=str(error) or name,
        stack=stack,
        code=code if isinstance(code, (str, int)) else None,
    )


def createAssistantMessageDiagnostic(
    type: str,
    error: Any,
    details: dict[str, Any] | None = None,
) -> AssistantMessageDiagnostic:
    return AssistantMessageDiagnostic(
        type=type,
        timestamp=int(time.time() * 1000),
        error=extractDiagnosticError(error),
        details=details,
    )


def appendAssistantMessageDiagnostic(message: Any, diagnostic: AssistantMessageDiagnostic) -> None:
    if isinstance(message, dict):
        diagnostics = list(message.get("diagnostics") or [])
        diagnostics.append(diagnostic)
        message["diagnostics"] = diagnostics
        return

    diagnostics = list(getattr(message, "diagnostics", None) or [])
    diagnostics.append(diagnostic)
    message.diagnostics = diagnostics


__all__ = [
    "AssistantMessageDiagnostic",
    "DiagnosticErrorInfo",
    "appendAssistantMessageDiagnostic",
    "createAssistantMessageDiagnostic",
    "extractDiagnosticError",
    "formatThrownValue",
]
