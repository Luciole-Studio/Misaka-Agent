"""A message that opens a turn keeps its creation time live; its transcript entry carries the time
it was persisted. LCM's turn-start preflight compared the live list with the replay it cached from
the transcript, saw the timestamps differ, took it for a changed replay and summarised a leaf at
every turn start, whatever the threshold (2026-10-29, R64: research Last Orders on a 1M window
compacted at 28-31% instead of 85%; a foreground Last Order at 24-39% after each prompt, at the
threshold only mid-turn). The live session here runs the production order: the context engine
prepares the messages before every request (``sdk.transformContext``)."""
from types import SimpleNamespace

from misaka.core.session_manager import SessionManager


async def _turns(tmp_path, monkeypatch, threshold):
    from misaka.ai.providers.faux import fauxAssistantMessage
    from misaka.extensions import misaka_lcm
    from tests.suite_harness import createHarness

    monkeypatch.setenv("LCM_LEAF_CHUNK_TOKENS", "400")       # a backlog over the leaf chunk within a few turns
    monkeypatch.setenv("LCM_FRESH_TAIL_COUNT", "4")
    monkeypatch.setenv("LCM_CONTEXT_THRESHOLD", threshold)
    project = tmp_path / "project"
    project.mkdir()
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    spec = SimpleNamespace(kind="foreground", workspace=str(project), sender="last-order", role="last_order",
                           task_id=None)
    async with createHarness(extensionFactories=[misaka_lcm.activate(spec)], sessionManager=manager,
                             settings={"compaction": {"enabled": True}}) as harness:
        transform = harness.session.agent.transformContext

        async def production(messages, signal=None):
            return await transform(await harness.session.prepareContextMessages(messages, signal), signal)

        harness.session.agent.transformContext = production
        harness.setResponses([fauxAssistantMessage(f"answer {i} " + " ".join(f"w{j}" for j in range(120)))
                              for i in range(8)])
        await harness.session.prompt("start")
        for i in range(5):                                        # research-phase work orders
            await harness.session.sendCustomMessage(
                {"customType": "research-phase", "content": f"phase {i}", "display": True, "details": {"turn": True}},
                {"triggerTurn": True})
            await harness.session.agent.waitForIdle()
    return [entry for entry in manager.getEntries() if entry.get("type") == "compaction"]


async def test_turn_opening_messages_do_not_compact_below_the_threshold(tmp_path, monkeypatch):
    assert await _turns(tmp_path, monkeypatch, "0.95") == []


async def test_the_threshold_still_compacts(tmp_path, monkeypatch):
    assert await _turns(tmp_path, monkeypatch, "0.02")
