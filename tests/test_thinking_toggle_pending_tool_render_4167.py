"""Translated from pi's ``coding-agent/test/suite/regressions/4167-thinking-toggle-pending-tool-render.test.ts``
(v1.1.0)."""

from __future__ import annotations

import os
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container, Text
from misaka.utils.ansi import stripAnsi

TOOL_CALL_ID = "tool-4167"
TOOL_NAME = "slow_tool"

EMPTY_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def createFakeInteractiveModeThis():
    chatContainer = Container()
    fakeThis = SimpleNamespace(
        pendingTools={},
        chatContainer=chatContainer,
        footer=SimpleNamespace(invalidate=Mock()),
        programStatus=SimpleNamespace(handleEvent=Mock()),
        ui=SimpleNamespace(requestRender=Mock()),
        settingsManager=SimpleNamespace(
            getShowImages=lambda: False, getImageWidthCells=lambda: 60, getShowCacheMissNotices=lambda: False
        ),
        sessionManager=SimpleNamespace(getCwd=os.getcwd, getEntries=list),
        session=SimpleNamespace(retryAttempt=0),
        toolOutputExpanded=False,
        outputPad=1,
        isInitialized=True,
        updateEditorBorderColor=Mock(),
        getRegisteredToolDefinition=lambda _toolName: None,
        addMessageToChat=lambda message, _options=None: chatContainer.addChild(Text(message["role"], 0, 0)),
    )
    fakeThis.renderSessionItems = MethodType(InteractiveMode.renderSessionItems, fakeThis)
    return fakeThis


def createAssistantToolCallMessage():
    return {
        "role": "assistant",
        "content": [{"type": "toolCall", "id": TOOL_CALL_ID, "name": TOOL_NAME, "arguments": {"delayMs": 10_000}}],
        "api": "test-api",
        "provider": "test-provider",
        "model": "test-model",
        "usage": EMPTY_USAGE,
        "stopReason": "toolUse",
        "timestamp": 1,
    }


def createToolResultMessage(text: str):
    return {
        "role": "toolResult",
        "toolCallId": TOOL_CALL_ID,
        "toolName": TOOL_NAME,
        "content": [{"type": "text", "text": text}],
        "isError": False,
        "timestamp": 2,
    }


def createSessionEntries(messages):
    entries = []
    parentId = None
    for index, message in enumerate(messages):
        entry = {
            "type": "message",
            "id": f"entry-{index}",
            "parentId": parentId,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": message,
        }
        parentId = entry["id"]
        entries.append(entry)
    return entries


def renderChat(container: Container) -> str:
    return stripAnsi("\n".join(container.render(120)))


@pytest.fixture(autouse=True)
def darkTheme():
    initTheme("dark")


async def test_keeps_unresolved_rendered_tool_calls_registered_for_live_completion_events():
    fakeThis = createFakeInteractiveModeThis()

    InteractiveMode.renderSessionEntries(fakeThis, createSessionEntries([createAssistantToolCallMessage()]))

    assert TOOL_CALL_ID in fakeThis.pendingTools

    await InteractiveMode.handleEvent(
        fakeThis,
        {
            "type": "tool_execution_end",
            "toolCallId": TOOL_CALL_ID,
            "toolName": TOOL_NAME,
            "result": {"content": [{"type": "text", "text": "FINAL_RESULT"}], "details": None},
            "isError": False,
        },
    )

    assert TOOL_CALL_ID not in fakeThis.pendingTools
    assert "FINAL_RESULT" in renderChat(fakeThis.chatContainer)


def test_does_not_keep_completed_historical_tool_calls_registered_as_pending():
    fakeThis = createFakeInteractiveModeThis()

    InteractiveMode.renderSessionEntries(
        fakeThis,
        createSessionEntries([createAssistantToolCallMessage(), createToolResultMessage("HISTORICAL_RESULT")]),
    )

    assert len(fakeThis.pendingTools) == 0
    assert "HISTORICAL_RESULT" in renderChat(fakeThis.chatContainer)
