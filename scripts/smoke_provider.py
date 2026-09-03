"""Real-endpoint smoke for one provider: a completion, a tool round-trip, the thinking levels.

Not part of ``make check`` -- every test there is network-free, which is exactly why this
exists. Run it once the credential is in place (``misaka auth check --provider <p>`` says
ready) and before a release::

    .venv/bin/python scripts/smoke_provider.py --provider amazon-bedrock --model <model id>
    .venv/bin/python scripts/smoke_provider.py --provider mistral --model mistral-large-latest

The tool round-trip is the scenario the 2026-09-02 audit found unusable on Bedrock, and the
``max`` thinking level the one that raised ``KeyError`` on budgeted Claude; both fixes were
verified against recorded responses only. Exit status is non-zero if any step fails, and the
provider's own error text is printed for it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.stream import complete
from misaka.ai.types import (
    Context,
    ProviderStreamOptions,
    TextContent,
    Tool,
    ToolResultMessage,
    UserMessage,
)
from misaka.ai.utils.headers import provider_headers_to_record
from misaka.config import get_agent_dir
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_registry import ModelRegistry

WEATHER = Tool(
    name="get_weather",
    description="Current weather for a city.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def _now() -> int:
    return int(time.time() * 1000)


def _text(message) -> str:
    return "".join(part.text for part in message.content if part.type == "text")


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=_now())


async def _resolve(provider: str, model_id: str):
    registry = ModelRegistry(
        AuthStorage.create(), os.path.join(get_agent_dir(), "models.json"), InMemoryModelsStore()
    )
    if registry.getError():
        sys.exit(registry.getError())
    model = registry.find(provider, model_id)
    if model is None:
        known = sorted(m.id for m in registry.getAll() if m.provider == provider)
        sys.exit(f"{provider} has no model {model_id!r}. Known: {', '.join(known) or '(none)'}")
    auth = await registry.getAuth(model)
    if auth is None:
        sys.exit(f"No credential for {provider}; `misaka auth check --provider {provider}` "
                 "says what is missing.")
    options = {
        "apiKey": auth.auth.apiKey,
        "env": auth.env or None,
        "headers": provider_headers_to_record(auth.auth.headers),
    }
    return model, options, auth.source


async def main(args) -> int:
    model, options, source = await _resolve(args.provider, args.model)
    print(f"{model.provider}/{model.id} via {source or 'resolved credential'}")
    failures = 0

    def fail(step: str, why: str) -> None:
        nonlocal failures
        failures += 1
        print(f"FAIL {step}: {why}")

    async def step(name: str, context: Context, **extra):
        try:
            reply = await complete(model, context, ProviderStreamOptions(**options, **extra))
        except Exception as err:  # noqa: BLE001 - the provider's own exception text is the finding
            fail(name, f"{type(err).__name__}: {err}")
            return None
        if reply.stopReason == "error" or reply.errorMessage:
            fail(name, f"{reply.stopReason}: {reply.errorMessage}")
            return None
        print(f"ok   {name}: stop={reply.stopReason} in={reply.usage.input} out={reply.usage.output}")
        return reply

    reply = await step("completion", Context(
        systemPrompt="Answer with one word.", messages=[_user("What is the capital of France?")]))
    if reply is not None and "paris" not in _text(reply).lower():
        fail("completion", f"unexpected answer {_text(reply)!r}")

    messages = [_user("What is the weather in Tokyo right now?")]
    first = await step("tool call", Context(
        systemPrompt="Use the tool; never guess.", messages=messages, tools=[WEATHER]))
    if first is not None:
        calls = [part for part in first.content if part.type == "toolCall"]
        if not calls or calls[0].name != WEATHER.name:
            fail("tool call", f"no {WEATHER.name} call in {[part.type for part in first.content]}")
        else:
            call = calls[0]
            messages += [first, ToolResultMessage(
                toolCallId=call.id, toolName=call.name, isError=False, timestamp=_now(),
                content=[TextContent(text="Sunny, 25 C")])]
            second = await step("tool result", Context(
                systemPrompt="Use the tool; never guess.", messages=messages, tools=[WEATHER]))
            if second is not None:
                text = _text(second).lower()
                if "25" not in text and "sunny" not in text:
                    fail("tool result", f"reply ignores the result: {text!r}")

    if model.reasoning:
        for level in args.thinking:
            reply = await step(f"thinking={level}", Context(
                messages=[_user("What is 17 * 23? Answer with the number only.")]), reasoning=level)
            if reply is not None and "391" not in _text(reply):
                fail(f"thinking={level}", f"wrong answer {_text(reply)!r}")
    else:
        print("skip thinking: not a reasoning model")

    print("PASS" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", required=True, help="provider id as in models.json, e.g. amazon-bedrock, mistral")
    parser.add_argument("--model", required=True, help="model id under that provider")
    parser.add_argument("--thinking", nargs="*", default=["high", "max"],
                        help="thinking levels to try on a reasoning model (default: high max)")
    sys.exit(asyncio.run(main(parser.parse_args())))
