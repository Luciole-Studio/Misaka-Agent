"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``7911-json-stream-usage.test.ts``,
``7925-toolcall-start-metadata.test.ts``, ``8328-zero-usage-auto-compaction.test.ts`` and
``8337-utf8-bom-parsing.test.ts`` (v1.1.0).

MISAKA fork: not translated -- ``7731-tui-method-wrapping.test.ts`` (``createInteractiveTuiReference`` belongs to the
fullscreen renderer swap MISAKA does not port, D1) and ``8261-subagent-project-trust.test.ts`` (it drives pi's
``examples/extensions/subagent``; MISAKA's subagent is its own core module).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import AssistantMessage
from misaka.config.home import DIR_NAME
from misaka.core.settings_manager import SettingsManager
from misaka.modes.json_event import _wireValue, toJsonEvent
from misaka.utils.frontmatter import parseFrontmatter
from misaka.utils.text import splitBom
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

# --- regression #7911: JSON message updates retain usage ----------------------------------------


async def test_includes_cumulative_usage_without_cumulative_message_snapshots():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("hello")])

        await harness.session.prompt("respond")

        # #7290's delta-only wire projection dropped this fixed-size metadata with the snapshots.
        update = next(
            (
                event
                for event in harness.eventsOfType("message_update")
                if read_field(read_field(event, "message"), "role") == "assistant"
                and read_field(read_field(read_field(event, "message"), "usage"), "totalTokens") > 0
            ),
            None,
        )
        assert update is not None, "Expected an assistant update with populated usage"

        wireUpdate = toJsonEvent(update)
        assert wireUpdate["usage"] == _wireValue(read_field(read_field(update, "message"), "usage"))
        assert "message" not in wireUpdate
        assert "partial" not in wireUpdate["assistantMessageEvent"]


# --- regression #7925: tool-call metadata is available when streaming starts --------------------


async def test_includes_the_tool_call_id_and_name_without_cumulative_snapshots():
    async with createHarness() as harness:
        harness.setResponses([
            fauxAssistantMessage(
                fauxToolCall("write", {"path": "output.txt", "content": "x" * 100}, {"id": "call_7925"}),
                {"stopReason": "toolUse"},
            ),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("write a file")

        update = next(
            (
                event
                for event in harness.eventsOfType("message_update")
                if read_field(read_field(event, "assistantMessageEvent"), "type") == "toolcall_start"
            ),
            None,
        )
        assert update is not None, "Expected toolcall_start assistant update"

        assert toJsonEvent(update) == {
            "type": "message_update",
            "usage": _wireValue(read_field(read_field(update, "message"), "usage")),
            "assistantMessageEvent": {
                "type": "toolcall_start",
                "contentIndex": 0,
                "id": "call_7925",
                "toolName": "write",
            },
        }


# --- issue #8328 zero-usage auto-compaction -----------------------------------------------------


def createZeroUsageAssistant(harness: Any) -> AssistantMessage:
    model = harness.getModel()
    return AssistantMessage.model_validate({
        "role": "assistant",
        "content": [{"type": "text", "text": "response"}],
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    })


def spyOnRunAutoCompaction(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def runAutoCompaction(*args: Any, **_kwargs: Any) -> bool:
        calls.append(args)
        return False

    monkeypatch.setattr(session, "_runAutoCompaction", runAutoCompaction)
    return calls


def _compactionHarness() -> Any:
    return createHarness(
        models=[{"id": "faux-1", "contextWindow": 100, "maxTokens": 20}],
        settings={"compaction": {"enabled": True, "reserveTokens": 10}},
    )


async def test_uses_the_message_estimate_when_no_assistant_has_reported_usage(monkeypatch: pytest.MonkeyPatch):
    async with _compactionHarness() as harness:
        assistant = createZeroUsageAssistant(harness)
        harness.session.agent.state.messages = [
            {"role": "user", "content": [{"type": "text", "text": "x" * 400}], "timestamp": int(time.time() * 1000) - 1},
            assistant,
        ]
        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)

        await harness.session._checkCompaction(assistant)

        assert calls == [("threshold", False)]


async def test_does_not_compact_when_the_zero_usage_message_estimate_is_below_the_threshold():
    async with _compactionHarness() as harness:
        assistant = createZeroUsageAssistant(harness)
        harness.session.agent.state.messages = [
            {"role": "user", "content": [{"type": "text", "text": "short"}], "timestamp": int(time.time() * 1000) - 1},
            assistant,
        ]

        # MISAKA fork: `_runAutoCompaction` is always called and applies the threshold itself (after asking an
        # installed context engine), where pi checks `shouldCompact` first and never calls it; so this checks that
        # no compaction starts instead of spying on the call.
        assert await harness.session._checkCompaction(assistant) is False
        assert harness.eventsOfType("compaction_start") == []


# --- issue #8337 UTF-8 BOM parsing --------------------------------------------------------------


async def test_loads_frontmatter_and_settings_with_a_leading_bom(tmp_path: Path):
    assert splitBom("﻿content") == {"bom": "﻿", "text": "content"}
    document = "---\nname: demo\ndescription: Test\n---\nBody"
    parsed = parseFrontmatter(f"﻿{document}")
    assert parsed.frontmatter == {"name": "demo", "description": "Test"}
    assert parsed.body == "Body"

    agentDir = tmp_path / "agent"
    projectDir = tmp_path / "project"
    (projectDir / DIR_NAME).mkdir(parents=True)
    agentDir.mkdir(parents=True)
    globalSettingsPath = agentDir / "settings.json"
    globalSettingsPath.write_text(f"﻿{json.dumps({'defaultModel': 'global-model'})}", encoding="utf-8")
    (projectDir / DIR_NAME / "settings.json").write_text(
        f"﻿{json.dumps({'defaultProvider': 'project-provider'})}", encoding="utf-8"
    )

    settings = SettingsManager.create(str(projectDir), str(agentDir))
    assert settings.getDefaultModel() == "global-model"
    assert settings.getDefaultProvider() == "project-provider"

    settings.setTheme("dark")
    await settings.flush()
    assert not globalSettingsPath.read_text(encoding="utf-8").startswith("﻿")
