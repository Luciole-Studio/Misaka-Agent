"""Ported from pi's ``packages/coding-agent/test/suite/regressions/7150-rpc-prompt-during-compaction.test.ts`` (v1.1.0):
an RPC prompt submitted during a manual compaction is rejected before it reaches the transcript."""

from __future__ import annotations

import asyncio
import time

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.core.agent_session import PromptOptions
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getMessageText, getUserTexts


async def test_rejects_an_rpc_prompt_while_manual_compaction_is_in_progress():
    compactionStarted = asyncio.Event()
    compactionReleased = asyncio.Event()

    def factory(pi) -> None:
        async def beforeCompact(event, _ctx):
            compactionStarted.set()
            await compactionReleased.wait()
            preparation = event["preparation"]
            return {
                "compaction": {
                    "summary": "manual compacted",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", beforeCompact)

    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[factory]) as harness:
        timestamp = int(time.time() * 1000)
        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "old user message"}],
            "timestamp": timestamp - 1000,
        })
        harness.sessionManager.appendMessage(
            fauxAssistantMessage("old assistant response", {"timestamp": timestamp - 500})
        )
        harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages
        harness.setResponses([fauxAssistantMessage("probe response")])

        compactPromise = asyncio.ensure_future(harness.session.compact())
        await compactionStarted.wait()

        preflightResults: list = []
        promptError: BaseException | None = None
        try:
            await harness.session.prompt(
                "PROBE-7150", PromptOptions(source="rpc", preflightResult=preflightResults.append)
            )
        except Exception as error:  # noqa: BLE001 - the case inspects whatever the prompt rejects with
            promptError = error
        finally:
            compactionReleased.set()
            await compactPromise

        persistedUserTexts = [
            getMessageText(entry["message"])
            for entry in harness.sessionManager.getEntries()
            if entry["type"] == "message" and read_field(entry["message"], "role") == "user"
        ]

        assert preflightResults == []
        assert promptError is not None and "compaction is in progress" in str(promptError)
        assert "PROBE-7150" not in getUserTexts(harness)
        assert "PROBE-7150" not in persistedUserTexts
        assert len(harness.eventsOfType("agent_start")) == 0
        assert len(harness.eventsOfType("agent_settled")) == 0
