"""Translated from pi's ``coding-agent/test/interactive-mode-compaction.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container
from misaka.utils.ansi import stripAnsi
from misaka.utils.values import read_field


def usageOf(input, output, cacheRead, cacheWrite, totalTokens, cost):
    return {
        "input": input,
        "output": output,
        "cacheRead": cacheRead,
        "cacheWrite": cacheWrite,
        "totalTokens": totalTokens,
        "cost": cost,
    }


def closeTask(coroutine):
    coroutine.close()


def test_uses_the_cache_miss_notice_setting_for_compaction_and_branch_summary_costs():
    usage = usageOf(
        10, 20, 30, 40, 100, {"input": 0.01, "output": 0.02, "cacheRead": 0.03, "cacheWrite": 0.065, "total": 0.125}
    )

    initTheme("dark")
    enabled = SimpleNamespace(
        chatContainer=Container(), settingsManager=SimpleNamespace(getShowCacheMissNotices=lambda: True)
    )
    InteractiveMode.addCompactionCostNotice(enabled, {"type": "compaction_cost", "kind": "compaction", "usage": usage})
    InteractiveMode.addCompactionCostNotice(
        enabled, {"type": "compaction_cost", "kind": "branch_summary", "usage": usage}
    )
    output = stripAnsi("\n".join(enabled.chatContainer.render(120)))
    assert "Compaction: 100 tokens billed (~$0.13)" in output
    assert "Branch summary: 100 tokens billed (~$0.13)" in output

    disabled = SimpleNamespace(
        chatContainer=Container(), settingsManager=SimpleNamespace(getShowCacheMissNotices=lambda: False)
    )
    InteractiveMode.addCompactionCostNotice(disabled, {"type": "compaction_cost", "kind": "compaction", "usage": usage})
    assert len(disabled.chatContainer.children) == 0


def test_renders_each_compaction_cost_after_its_summary():
    currentUsage = usageOf(
        10, 20, 30, 40, 100, {"input": 0.01, "output": 0.02, "cacheRead": 0.03, "cacheWrite": 0.04, "total": 0.1}
    )
    previousUsage = usageOf(
        1, 2, 3, 4, 10, {"input": 0.001, "output": 0.002, "cacheRead": 0.003, "cacheWrite": 0.004, "total": 0.01}
    )
    entries = [
        {
            "type": "compaction",
            "id": "current",
            "parentId": "previous",
            "timestamp": "2025-01-02T00:00:00Z",
            "summary": "current summary",
            "firstKeptEntryId": "kept",
            "tokensBefore": 200,
            "usage": currentUsage,
        },
        {
            "type": "compaction",
            "id": "previous",
            "parentId": None,
            "timestamp": "2025-01-01T00:00:00Z",
            "summary": "previous summary",
            "firstKeptEntryId": "kept",
            "tokensBefore": 100,
            "usage": previousUsage,
        },
    ]
    fakeThis = SimpleNamespace(renderSessionItems=Mock())

    InteractiveMode.renderSessionEntries(fakeThis, entries)

    items, options = fakeThis.renderSessionItems.call_args.args
    assert [read_field(item, "role") or read_field(item, "type") for item in items] == [
        "compactionSummary",
        "compaction_cost",
        "compactionSummary",
        "compaction_cost",
    ]
    assert read_field(items[0], "summary") == "current summary"
    assert items[1] == {"type": "compaction_cost", "kind": "compaction", "usage": currentUsage}
    assert read_field(items[2], "summary") == "previous summary"
    assert items[3] == {"type": "compaction_cost", "kind": "compaction", "usage": previousUsage}
    assert not options


async def test_renders_retained_entries_and_appends_the_latest_summary_cost_at_the_bottom():
    usage = usageOf(
        10, 20, 30, 40, 100, {"input": 0.01, "output": 0.02, "cacheRead": 0.03, "cacheWrite": 0.065, "total": 0.125}
    )
    latestCompaction = {
        "type": "compaction",
        "id": "latest",
        "parentId": "previous",
        "timestamp": "2025-01-02T00:00:00Z",
        "summary": "summary",
        "firstKeptEntryId": "kept",
        "tokensBefore": 123,
        "usage": usage,
    }
    previousCompaction = {
        "type": "compaction",
        "id": "previous",
        "parentId": None,
        "timestamp": "2025-01-01T00:00:00Z",
        "summary": "previous summary",
        "firstKeptEntryId": "kept",
        "tokensBefore": 100,
        "usage": usage,
    }
    fakeThis = SimpleNamespace(
        isInitialized=True,
        footer=SimpleNamespace(invalidate=Mock()),
        programStatus=SimpleNamespace(handleEvent=Mock()),
        autoCompactionEscapeHandler=None,
        defaultEditor=SimpleNamespace(),
        statusContainer=SimpleNamespace(clear=Mock()),
        chatContainer=SimpleNamespace(clear=Mock()),
        sessionManager=SimpleNamespace(buildContextEntries=Mock(return_value=[latestCompaction, previousCompaction])),
        renderSessionEntries=Mock(),
        addMessageToChat=Mock(),
        addCompactionCostNotice=Mock(),
        showError=Mock(),
        showStatus=Mock(),
        clearStatusIndicator=Mock(),
        flushCompactionQueue=AsyncMock(return_value=None),
        settingsManager=SimpleNamespace(getShowTerminalProgress=lambda: False),
        ui=SimpleNamespace(requestRender=Mock(), terminal=SimpleNamespace(setProgress=Mock())),
        _schedule_task=closeTask,
    )

    await InteractiveMode.handleEvent(
        fakeThis,
        {
            "type": "compaction_end",
            "reason": "manual",
            "result": {"tokensBefore": 123, "summary": "summary", "usage": usage},
            "aborted": False,
            "willRetry": False,
        },
    )

    fakeThis.chatContainer.clear.assert_called_once_with()
    fakeThis.renderSessionEntries.assert_called_once_with([previousCompaction])
    fakeThis.addMessageToChat.assert_called_once()
    summary = fakeThis.addMessageToChat.call_args.args[0]
    assert read_field(summary, "role") == "compactionSummary"
    assert read_field(summary, "tokensBefore") == 123
    assert read_field(summary, "summary") == "summary"
    fakeThis.addCompactionCostNotice.assert_called_once_with(
        {"type": "compaction_cost", "kind": "compaction", "usage": usage}
    )
    fakeThis.flushCompactionQueue.assert_called_once_with({"willRetry": False})


async def test_updates_the_working_state_when_the_same_agent_run_resumes_after_compaction():
    fakeThis = SimpleNamespace(
        isInitialized=True,
        footer=SimpleNamespace(invalidate=Mock()),
        programStatus=SimpleNamespace(handleEvent=Mock()),
        activeStatusIndicator=None,
        workingVisible=True,
        showWorkingStatusIndicator=Mock(),
        clearStatusIndicator=Mock(),
        settingsManager=SimpleNamespace(getShowTerminalProgress=lambda: True),
        ui=SimpleNamespace(requestRender=Mock(), terminal=SimpleNamespace(setProgress=Mock())),
    )

    await InteractiveMode.handleEvent(fakeThis, {"type": "turn_start"})

    fakeThis.ui.terminal.setProgress.assert_called_with(True)
    assert fakeThis.showWorkingStatusIndicator.call_count == 1
    fakeThis.clearStatusIndicator.assert_not_called()
    assert fakeThis.ui.requestRender.call_count == 1

    fakeThis.workingVisible = False
    await InteractiveMode.handleEvent(fakeThis, {"type": "turn_start"})

    assert fakeThis.showWorkingStatusIndicator.call_count == 1
    assert fakeThis.clearStatusIndicator.call_count == 1
    assert fakeThis.ui.requestRender.call_count == 2


def test_routes_interactive_response_aborts_through_agent_session():
    # Regression test for #9340.
    abort = AsyncMock(return_value=None)
    ui = SimpleNamespace(
        clearAllQueues=lambda: {"steering": [], "followUp": []},
        updatePendingMessagesDisplay=Mock(),
        session=SimpleNamespace(abort=abort),
        _schedule_task=closeTask,
    )

    InteractiveMode.restoreQueuedMessagesToEditor(ui, {"abort": True})

    abort.assert_called_once_with()


async def test_preserves_steering_behavior_when_flushing_into_an_active_agent_run():
    fakeThis = SimpleNamespace(
        compactionQueuedMessages=[{"text": "change direction", "mode": "steer"}],
        session=SimpleNamespace(
            clearQueue=Mock(),
            prompt=AsyncMock(return_value=None),
            steer=AsyncMock(return_value=None),
            followUp=AsyncMock(return_value=None),
        ),
        isExtensionCommand=Mock(return_value=False),
        isCorePromptCommand=Mock(return_value=False),
        updatePendingMessagesDisplay=Mock(),
        showError=Mock(),
        _backgroundTasks=set(),
    )

    await InteractiveMode.flushCompactionQueue(fakeThis, {"willRetry": False})

    # MISAKA fork: the prompt also reports its preflight acceptance.
    fakeThis.session.prompt.assert_called_once_with(
        "change direction", {"streamingBehavior": "steer", "preflightResult": ANY}
    )
    assert fakeThis.compactionQueuedMessages == []
    fakeThis.showError.assert_not_called()
