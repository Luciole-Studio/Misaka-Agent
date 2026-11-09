"""Translated from pi durable's ``test/provider-session-cache-e2e.test.ts`` (v1.1.0).

Live regression coverage for #10424; skipped without Codex credentials, and unless ``MISAKA_LIVE_TESTS=1`` is set
(``tests/live_credentials.py``). Upstream retries the case twice; pytest runs it once."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.ai.api.openai_codex_responses import (
    streamSimple as streamSimpleOpenAICodexResponses,
)
from misaka.ai.providers.all import builtinModels
from misaka.ai.utils.transcript import normalizeContext
from misaka.ai.utils.uuid import uuidv7
from misaka.durable import (
    AssistantEntry,
    Harness,
    MemoryStorage,
    ProviderDoc,
    createRegistry,
)
from misaka.utils.values import read_field
from tests.durable_support import context
from tests.live_credentials import LIVE_TESTS, resolveApiKey


def cacheProbe(nonce: str) -> str:
    lines = [
        "This is a real automated prompt-cache test. The repeated context below is intentional.",
        "Read it silently, then reply with exactly: CACHE PROBE READY",
    ]
    for index in range(180):
        lines.append(
            f"{nonce} immutable cache record {str(index).rjust(3, '0')}: alpha beta gamma delta epsilon zeta eta theta "
            "iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega."
        )
    return "\n".join(lines)


async def assistant(harness: Any, conversationId: str, answer: str) -> Any:
    record = await harness.commit(lambda tx: tx.entry(AssistantEntry, answer), context)
    if read_field(record, "conversationId") != conversationId:
        raise RuntimeError("Answer belongs to another conversation")
    model = read_field(record, "model")
    return model[0] if model else None


def cacheHitRate(usage: Any) -> float:
    promptTokens = read_field(usage, "input") + read_field(usage, "cacheRead") + read_field(usage, "cacheWrite")
    return 0 if promptTokens == 0 else read_field(usage, "cacheRead") / promptTokens


class _ForwardingModels:
    """MISAKA fork: upstream's ``Proxy`` over ``builtinModels()``, replacing only ``streamSimple``."""

    def __init__(self, base: Any, streamSimple: Any) -> None:
        self._base = base
        self.streamSimple = streamSimple

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


# --- Durable provider session cache e2e -------------------------------------------------------------


@pytest.mark.skipif(not LIVE_TESTS, reason="live test: set MISAKA_LIVE_TESTS=1")
async def test_reuses_the_codex_prompt_cache_across_durable_turns():
    codexToken = await resolveApiKey("openai-codex")
    if not codexToken:
        pytest.skip("no openai-codex credentials")
    forwarded: list[str] = []

    def streamSimple(model: Any, request: Any, options: Any = None) -> Any:
        forwarded.append(read_field(options, "sessionId") or "")
        return streamSimpleOpenAICodexResponses(
            model,
            normalizeContext(request),
            options.model_copy(update={"apiKey": codexToken, "transport": "sse"}),
        )

    models = _ForwardingModels(builtinModels(), streamSimple)
    harness = await Harness.open(
        MemoryStorage(),
        {
            "models": models,
            "registry": createRegistry(),
            "settings": {"stream": {"transport": "sse"}, "compaction": {"enabled": False}},
        },
        context,
    )
    try:
        async with asyncio.timeout(120):
            root = await harness.root(context, {"agent": {"model": {"provider": "openai-codex", "modelId": "gpt-5.5"}}})
            harness.resume()
            usages: list[Any] = []
            firstHit: int | None = None
            for turn in range(8):
                content = cacheProbe(uuidv7()) if turn == 0 else f"Reply exactly: CACHE PROBE FOLLOW-UP {turn}"
                settled = await (await root.submit({"type": "input", "content": content}, context)).wait(context)
                if settled["status"] != "done" or settled["type"] != "input":
                    raise RuntimeError(f"Unexpected submission status: {settled['status']}")
                message = await assistant(harness, root.id, settled["answer"])
                assert read_field(message, "stopReason") != "error", read_field(message, "errorMessage")
                usages.append(read_field(message, "usage"))
                if firstHit is None and read_field(usages[-1], "cacheRead") > 0:
                    firstHit = len(usages) - 1
                if firstHit is not None and len(usages) - firstHit >= 3:
                    break
            sessionId = (await harness.snapshot(ProviderDoc, root.id, context))["sessionId"]
            assert forwarded == [sessionId for _ in usages]
            first = usages[0]
            firstPrompt = read_field(first, "input") + read_field(first, "cacheRead") + read_field(first, "cacheWrite")
            assert firstPrompt >= 4_000
            assert read_field(first, "cacheRead") == 0
            assert firstHit == 1
            cached = usages[firstHit:]
            assert len(cached) == 3
            for usage in cached:
                assert cacheHitRate(usage) >= 0.9
    finally:
        await harness.close(context)
