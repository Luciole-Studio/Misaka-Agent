"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``3616-settings-inmemory-reload.test.ts``,
``3686-session-name-event.test.ts``, ``3688-tree-cancel-compacting.test.ts`` and
``3982-message-end-cost-override.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.suite_harness import createHarness
from tests.utilities import assistantMsg, userMsg

# --- regression #3616: in-memory settings survive reload ----------------------------------------


async def test_preserves_initial_settings_after_direct_reload():
    settingsManager = SettingsManager.inMemory(
        {"defaultThinkingLevel": "high", "images": {"autoResize": False}, "compaction": {"enabled": False}}
    )

    await settingsManager.reload()

    assert settingsManager.getDefaultThinkingLevel() == "high"
    assert settingsManager.getImageAutoResize() is False
    assert settingsManager.getCompactionEnabled() is False
    assert settingsManager.getGlobalSettings() == {
        "defaultThinkingLevel": "high",
        "images": {"autoResize": False},
        "compaction": {"enabled": False},
    }


async def test_preserves_initial_settings_when_default_resource_loader_reloads(tmp_path):
    settingsManager = SettingsManager.inMemory(
        {"defaultThinkingLevel": "high", "images": {"autoResize": False}, "compaction": {"enabled": False}}
    )
    (tmp_path / "agent").mkdir()
    resourceLoader = DefaultResourceLoader({
        "cwd": str(tmp_path),
        "agentDir": str(tmp_path / "agent"),
        "settingsManager": settingsManager,
        "noExtensions": True,
        "noSkills": True,
        "noPromptTemplates": True,
        "noThemes": True,
        "noContextFiles": True,
    })

    await resourceLoader.reload()

    assert settingsManager.getDefaultThinkingLevel() == "high"
    assert settingsManager.getImageAutoResize() is False
    assert settingsManager.getCompactionEnabled() is False


async def test_preserves_initial_settings_after_an_unrelated_setter_flush_and_reload():
    settingsManager = SettingsManager.inMemory({"images": {"autoResize": False}, "compaction": {"enabled": False}})

    settingsManager.setTheme("dark")
    await settingsManager.flush()
    await settingsManager.reload()

    assert settingsManager.getTheme() == "dark"
    assert settingsManager.getImageAutoResize() is False
    assert settingsManager.getCompactionEnabled() is False
    assert settingsManager.getGlobalSettings() == {
        "images": {"autoResize": False},
        "compaction": {"enabled": False},
        "theme": "dark",
    }


# --- regression #3686: session name changes emit an event ---------------------------------------


async def test_emits_session_info_changed_when_agent_session_set_session_name_is_called():
    async with createHarness() as harness:
        harness.session.setSessionName("hello world")

        assert harness.sessionManager.getSessionName() == "hello world"
        assert [read_field(event, "name") for event in harness.eventsOfType("session_info_changed")] == ["hello world"]


async def test_emits_session_info_changed_when_an_extension_calls_pi_set_session_name():
    api: dict[str, Any] = {}
    async with createHarness(extensionFactories=[lambda pi: api.setdefault("pi", pi)]) as harness:
        api["pi"].setSessionName("from extension")

        assert harness.sessionManager.getSessionName() == "from extension"
        assert [read_field(event, "name") for event in harness.eventsOfType("session_info_changed")] == [
            "from extension"
        ]


async def test_emits_session_info_changed_to_extensions():
    api: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    def extension(pi: Any) -> None:
        api["pi"] = pi
        pi.on("session_info_changed", lambda event, _ctx: events.append({"name": read_field(event, "name")}))

    async with createHarness(extensionFactories=[extension]) as harness:
        api["pi"].setSessionName("first")
        harness.session.setSessionName("second")

        assert events == [{"name": "first"}, {"name": "second"}]


# --- issue #3688 tree cancellation compaction state ---------------------------------------------


async def test_clears_branch_summary_state_when_session_before_tree_cancels_navigation():
    def extension(pi: Any) -> None:
        pi.on("session_before_tree", lambda _event, _ctx: {"cancel": True})

    async with createHarness(extensionFactories=[extension]) as harness:
        targetId = harness.sessionManager.appendMessage(userMsg("first"))
        harness.sessionManager.appendMessage(assistantMsg("reply"))
        currentLeafId = harness.sessionManager.appendMessage(userMsg("second"))

        assert harness.sessionManager.getLeafId() == currentLeafId

        result = await harness.session.navigateTree(targetId, {"summarize": False})

        assert result == {"cancelled": True}
        assert harness.session.isCompacting is False
        assert harness.sessionManager.getLeafId() == currentLeafId


# --- regression #3982: message_end cost override ------------------------------------------------


async def test_allows_extensions_to_replace_finalized_assistant_usage_cost():
    def extension(pi: Any) -> None:
        def onMessageEnd(event: Any, _ctx: Any) -> Any:
            message = read_field(event, "message")
            if read_field(message, "role") != "assistant":
                return None
            data = message.model_dump() if hasattr(message, "model_dump") else dict(message)
            usage = dict(data["usage"])
            usage["cost"] = {**dict(usage["cost"]), "total": 0.123}
            return {"message": {**data, "usage": usage}}

        pi.on("message_end", onMessageEnd)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("hello")])

        await harness.session.prompt("hi")

        assistantMessage = next(
            (message for message in harness.session.messages if read_field(message, "role") == "assistant"), None
        )
        assert assistantMessage is not None, "missing assistant message"
        assert read_field(read_field(read_field(assistantMessage, "usage"), "cost"), "total") == 0.123

        messageEnd = next(
            (
                event
                for event in harness.eventsOfType("message_end")
                if read_field(read_field(event, "message"), "role") == "assistant"
            ),
            None,
        )
        assert messageEnd is not None, "missing assistant message_end event"
        assert read_field(read_field(read_field(read_field(messageEnd, "message"), "usage"), "cost"), "total") == 0.123
