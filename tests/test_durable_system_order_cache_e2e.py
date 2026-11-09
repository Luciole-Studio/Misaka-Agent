"""Translated from pi durable's ``test/system-order-cache-e2e.test.ts`` (v1.1.0).

Live regression coverage for #10542; skipped without Anthropic credentials, and unless ``MISAKA_LIVE_TESTS=1`` is set
(``tests/live_credentials.py``). Upstream retries the case once; pytest runs it once."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import streamSimple as streamSimpleAnthropic
from misaka.ai.providers.all import builtinModels
from misaka.ai.utils.transcript import normalizeContext
from misaka.ai.utils.uuid import uuidv7
from misaka.durable import (
    AssistantEntry,
    GenerationTask,
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    defineTool,
    hook,
)
from misaka.utils.values import read_field
from tests.durable_support import context
from tests.live_credentials import LIVE_TESTS, resolveApiKey
from tests.test_durable_provider_session_cache_e2e import _ForwardingModels

# Sets both mid-conversation compat flags, so tool changes use Anthropic's native additions when allowed.
MODEL = {"provider": "anthropic", "modelId": "claude-sonnet-5-5"}


def instructions(nonce: str) -> str:
    lines = [
        "This is an automated prompt-cache test. The repeated context below is intentional.",
        "Never call tools. Reply with exactly the text the user asks for.",
    ]
    for index in range(250):
        lines.append(
            f"{nonce} cache record {str(index).rjust(3, '0')}: alpha beta gamma delta epsilon zeta eta theta iota kappa "
            "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega."
        )
    return "\n".join(lines)


Parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


async def _noContent(*_args: Any) -> dict[str, Any]:
    return {"output": []}


first = defineTool({"name": "probe_first", "description": "Unused probe tool.", "parameters": Parameters, "execute": _noContent})
second = defineTool({
    "name": "probe_second",
    "description": "Unused probe tool added on the second turn.",
    "parameters": Parameters,
    "execute": _noContent,
})


def committedOrder(messages: list[Any]) -> list[Any]:
    """Puts the leading system message back after the user messages before it: the request order before the fix."""
    if not messages or read_field(messages[0], "role") != "system":
        return list(messages)
    system, *rest = messages
    users = next((index for index, message in enumerate(rest) if read_field(message, "role") != "user"), -1)
    at = len(rest) if users == -1 else users
    return [*rest[:at], system, *rest[at:]]


async def cacheReadAfterToolChange(token: str, legacyOrder: bool) -> float:
    """Share of the prompt the second turn read from cache after adding a tool."""

    def streamSimple(model: Any, request: Any, options: Any = None) -> Any:
        return streamSimpleAnthropic(model, normalizeContext(request), options.model_copy(update={"apiKey": token}))

    models = _ForwardingModels(builtinModels(), streamSimple)
    registry = createRegistry()
    probe = defineExtension({"name": "probe", "tools": [first, second]})
    legacy = defineExtension({
        "name": "legacy-order",
        "hooks": [hook(GenerationTask, {"beforeRequest": lambda arg, *_: {"messages": committedOrder(arg["messages"])}})],
    })
    registry.install(probe)
    registry.install(legacy)
    harness = await Harness.open(
        MemoryStorage(), {"models": models, "registry": registry, "settings": {"compaction": {"enabled": False}}}, context
    )
    try:
        root = await harness.root(context, {
            "agent": {
                "model": MODEL,
                "thinkingLevel": "low",
                "instructions": instructions(uuidv7()),
                "extensions": [probe, legacy] if legacyOrder else [probe],
                "tools": [first],
            },
        })
        harness.resume()

        async def ask(text: str) -> Any:
            settled = await (await root.submit({"type": "input", "content": f"Reply exactly: {text}"}, context)).wait(context)
            if settled["status"] != "done" or settled["type"] != "input":
                raise RuntimeError(f"Unexpected {settled['status']}")
            record = await harness.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
            model = read_field(record, "model")
            message = model[0] if model else None
            assert read_field(message, "stopReason") == "stop", read_field(message, "errorMessage")
            return message

        before = read_field(await ask("READY"), "usage")
        assert read_field(before, "input") + read_field(before, "cacheRead") + read_field(before, "cacheWrite") >= 4_000
        await root.configure({"tools": [first, second]}, context)
        after = read_field(await ask("READY AGAIN"), "usage")
        return read_field(after, "cacheRead") / (
            read_field(after, "input") + read_field(after, "cacheRead") + read_field(after, "cacheWrite")
        )
    finally:
        await harness.close(context)


# --- Durable system message order e2e ---------------------------------------------------------------


@pytest.mark.skipif(not LIVE_TESTS, reason="live test: set MISAKA_LIVE_TESTS=1")
async def test_keeps_the_prompt_cache_across_a_tool_change_only_when_the_system_message_leads():
    anthropicToken = await resolveApiKey("anthropic")
    if not anthropicToken:
        pytest.skip("no anthropic credentials")
    async with asyncio.timeout(180):
        # Before the fix: the input precedes the system message, so Anthropic sends the tool list at the top of the
        # request, and adding a tool changes the cached prefix from its first token.
        assert await cacheReadAfterToolChange(anthropicToken, True) < 0.2
        # The fix: the system message leads, the initial tools stay fixed, and the new tool arrives in the conversation.
        assert await cacheReadAfterToolChange(anthropicToken, False) > 0.8
