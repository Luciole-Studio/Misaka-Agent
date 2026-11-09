"""Translated from pi's ``packages/coding-agent/test/edit-tool-legacy-input.test.ts`` and
``settings-diagnostics.test.ts`` (v1.1.0)."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from misaka.core.agent_session_services import AgentSessionRuntimeDiagnostic
from misaka.core.settings_diagnostics import (
    collectSettingsDiagnostics,
    deduplicateDiagnostics,
)
from misaka.core.settings_manager import SettingsManager, SettingsStorage
from misaka.core.tools.edit import createEditToolDefinition
from misaka.utils.values import read_field


def schemaProperties(definition: Any) -> dict[str, Any]:
    parameters = definition.parameters
    schema = parameters if isinstance(parameters, dict) else parameters.model_json_schema()
    return schema.get("properties", {})


def contentBlocks(result: Any) -> list[dict[str, Any]]:
    return [{"type": read_field(block, "type"), "text": read_field(block, "text")} for block in read_field(result, "content")]


# --- edit tool prepareArguments -----------------------------------------------------------------


def test_keeps_legacy_fields_out_of_the_public_schema():
    properties = schemaProperties(createEditToolDefinition(os.getcwd()))
    assert "oldText" not in properties
    assert "newText" not in properties


def test_folds_top_level_old_text_new_text_into_edits():
    prepared = createEditToolDefinition(os.getcwd()).prepareArguments(
        {"path": "file.txt", "oldText": "before", "newText": "after"}
    )
    assert prepared == {"path": "file.txt", "edits": [{"oldText": "before", "newText": "after"}]}


def test_appends_legacy_replacement_to_existing_edits():
    prepared = createEditToolDefinition(os.getcwd()).prepareArguments({
        "path": "file.txt",
        "edits": [{"oldText": "a", "newText": "b"}],
        "oldText": "c",
        "newText": "d",
    })
    assert prepared == {
        "path": "file.txt",
        "edits": [{"oldText": "a", "newText": "b"}, {"oldText": "c", "newText": "d"}],
    }


def test_passes_through_valid_input_unchanged():
    input = {"path": "file.txt", "edits": [{"oldText": "a", "newText": "b"}]}
    assert createEditToolDefinition(os.getcwd()).prepareArguments(input) is input


def test_passes_through_non_object_input_unchanged():
    prepareArguments = createEditToolDefinition(os.getcwd()).prepareArguments
    assert prepareArguments(None) is None
    assert prepareArguments("garbage") == "garbage"


async def test_prepared_args_execute_correctly(tmp_path: Path):
    filePath = tmp_path / "legacy.txt"
    filePath.write_text("before\n", encoding="utf-8", newline="")

    definition = createEditToolDefinition(str(tmp_path))
    prepared = definition.prepareArguments({"path": "legacy.txt", "oldText": "before", "newText": "after"})

    result = await definition.execute("tool-1", prepared, None, None, None)
    assert contentBlocks(result) == [{"type": "text", "text": "Successfully replaced 1 block(s) in legacy.txt."}]
    assert filePath.read_text(encoding="utf-8") == "after\n"


# --- edit tool stringified edits ----------------------------------------------------------------


def test_parses_edits_from_a_json_string():
    prepared = createEditToolDefinition(os.getcwd()).prepareArguments(
        {"path": "file.txt", "edits": json.dumps([{"oldText": "a", "newText": "b"}])}
    )
    assert prepared == {"path": "file.txt", "edits": [{"oldText": "a", "newText": "b"}]}


def test_leaves_edits_alone_when_the_string_is_not_valid_json():
    prepared = createEditToolDefinition(os.getcwd()).prepareArguments({"path": "file.txt", "edits": "not json"})
    assert prepared == {"path": "file.txt", "edits": "not json"}


# --- settings diagnostics -----------------------------------------------------------------------


def test_includes_the_settings_file_path_for_file_backed_storage(tmp_path: Path):
    agentDir = tmp_path / "agent"
    settingsPath = agentDir / "settings.json"
    agentDir.mkdir()
    settingsPath.write_text("{", encoding="utf-8")

    diagnostics = collectSettingsDiagnostics(SettingsManager.create(str(tmp_path), str(agentDir)))

    assert len(diagnostics) == 1
    assert read_field(diagnostics[0], "type") == "warning"
    assert f"Invalid settings file {settingsPath}:" in read_field(diagnostics[0], "message")


def test_falls_back_to_the_settings_scope_for_storage_without_file_paths():
    class Storage(SettingsStorage):
        def withLock(self, scope: str, fn: Any) -> None:
            if scope == "global":
                raise RuntimeError("backend failed")
            fn(None)

    diagnostics = collectSettingsDiagnostics(SettingsManager.fromStorage(Storage()))

    assert [{"type": read_field(item, "type"), "message": read_field(item, "message")} for item in diagnostics] == [
        {"type": "warning", "message": "Invalid global settings: backend failed"}
    ]


def test_deduplicates_diagnostics_by_type_and_message():
    warning = AgentSessionRuntimeDiagnostic(type="warning", message="Invalid settings file /tmp/settings.json")
    error = dataclasses.replace(warning, type="error")

    assert deduplicateDiagnostics([warning, warning, error]) == [warning, error]
