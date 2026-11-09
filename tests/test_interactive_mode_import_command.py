"""Translated from pi's ``coding-agent/test/interactive-mode-import-command.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from misaka.core.agent_session_runtime import SessionImportFileNotFoundError
from misaka.modes.interactive.interactive_mode import InteractiveMode


def getPathCommandArgument(text: str, command: str) -> str | None:
    return InteractiveMode.getPathCommandArgument(None, text, command)  # type: ignore[arg-type]


def importContext(importFromJsonl: AsyncMock):
    async def unexpectedFatal(_prefix, _error):
        raise RuntimeError("unexpected fatal error")

    return SimpleNamespace(
        clearStatusIndicator=Mock(),
        runtimeHost=SimpleNamespace(importFromJsonl=importFromJsonl),
        showError=Mock(),
        showStatus=Mock(),
        showExtensionConfirm=AsyncMock(return_value=True),
        renderCurrentSessionState=Mock(),
        handleFatalRuntimeError=AsyncMock(side_effect=unexpectedFatal),
        promptForMissingSessionCwd=AsyncMock(return_value=None),
        getPathCommandArgument=getPathCommandArgument,
    )


def test_strips_quotes_from_import_path_arguments():
    assert getPathCommandArgument('/import "path/to/session.jsonl"', "/import") == "path/to/session.jsonl"
    assert (
        getPathCommandArgument('/import "path with spaces/session.jsonl"', "/import")
        == "path with spaces/session.jsonl"
    )


def test_preserves_apostrophes_in_unquoted_import_path_arguments():
    assert getPathCommandArgument("/import john's/session.jsonl", "/import") == "john's/session.jsonl"


def test_enforces_command_token_boundaries():
    assert getPathCommandArgument("/important /tmp/session.jsonl", "/import") is None
    assert getPathCommandArgument("/exporter out.html", "/export") is None
    assert getPathCommandArgument("/import /tmp/session.jsonl", "/import") == "/tmp/session.jsonl"


async def test_passes_unquoted_path_to_runtime_host_import_from_jsonl():
    importFromJsonl = AsyncMock(return_value={"cancelled": False})
    context = importContext(importFromJsonl)

    await InteractiveMode.handleImportCommand(context, '/import "path/to/session.jsonl"')

    context.showExtensionConfirm.assert_awaited_once_with(
        "Import session", "Replace current session with path/to/session.jsonl?"
    )
    importFromJsonl.assert_awaited_once_with("path/to/session.jsonl")
    context.showError.assert_not_called()
    context.showStatus.assert_called_once_with("Session imported from: path/to/session.jsonl")


async def test_passes_unquoted_apostrophe_path_to_runtime_host_import_from_jsonl_unchanged():
    importFromJsonl = AsyncMock(return_value={"cancelled": False})
    context = importContext(importFromJsonl)

    await InteractiveMode.handleImportCommand(context, "/import john's/session.jsonl")

    importFromJsonl.assert_awaited_once_with("john's/session.jsonl")
    context.showError.assert_not_called()
    context.showStatus.assert_called_once_with("Session imported from: john's/session.jsonl")


async def test_shows_a_non_fatal_error_when_import_path_does_not_exist():
    importFromJsonl = AsyncMock(side_effect=SessionImportFileNotFoundError("/tmp/missing-session.jsonl"))
    context = importContext(importFromJsonl)

    await InteractiveMode.handleImportCommand(context, "/import /tmp/missing-session.jsonl")

    context.showError.assert_called_once_with("Failed to import session: File not found: /tmp/missing-session.jsonl")
    context.showStatus.assert_not_called()
    context.handleFatalRuntimeError.assert_not_called()
