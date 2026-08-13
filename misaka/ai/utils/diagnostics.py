"""Diagnostic helpers shared by AI providers and stream orchestration."""

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


def format_thrown_value(value: Any) -> str:
    if isinstance(value, Exception):
        name = getattr(value, "name", value.__class__.__name__)
        return str(value) or name
    if isinstance(value, str):
        return value
    return str(value)


def extract_diagnostic_error(error: Any) -> DiagnosticErrorInfo:
    if not isinstance(error, Exception):
        return DiagnosticErrorInfo(name="ThrownValue", message=format_thrown_value(error))

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


def create_assistant_message_diagnostic(
    type: str,
    error: Any,
    details: dict[str, Any] | None = None,
) -> AssistantMessageDiagnostic:
    return AssistantMessageDiagnostic(
        type=type,
        timestamp=int(time.time() * 1000),
        error=extract_diagnostic_error(error),
        details=details,
    )


def append_assistant_message_diagnostic(message: Any, diagnostic: AssistantMessageDiagnostic) -> None:
    if isinstance(message, dict):
        diagnostics = list(message.get("diagnostics") or [])
        diagnostics.append(diagnostic)
        message["diagnostics"] = diagnostics
        return

    diagnostics = list(getattr(message, "diagnostics", None) or [])
    diagnostics.append(diagnostic)
    setattr(message, "diagnostics", diagnostics)


formatThrownValue = format_thrown_value
extractDiagnosticError = extract_diagnostic_error
createAssistantMessageDiagnostic = create_assistant_message_diagnostic
appendAssistantMessageDiagnostic = append_assistant_message_diagnostic

__all__ = [
    "DiagnosticErrorInfo",
    "AssistantMessageDiagnostic",
    "formatThrownValue",
    "extractDiagnosticError",
    "createAssistantMessageDiagnostic",
    "appendAssistantMessageDiagnostic",
]
