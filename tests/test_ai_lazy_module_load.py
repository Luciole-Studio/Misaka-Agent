"""Translated from pi's ``packages/ai/test/lazy-module-load.test.ts`` (v1.1.0).

MISAKA fork: a module-resolution hook becomes a look at ``sys.modules`` in a fresh interpreter; the SDKs are the
Python ones (``anthropic``, ``openai``, ``google.genai``, ``boto3``). The root barrel is ``misaka.ai``, the compat
entrypoint ``misaka.ai.compat``. The two streaming cases end at the request (no network): the probe answers on a
closed port.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

SDK_MODULES = ["anthropic", "openai", "google.genai", "boto3"]


def runProbe(action: str) -> list[str]:
    script = textwrap.dedent(
        """
        import asyncio, json, sys
        import misaka.ai as mod
        {action}
        print(json.dumps([name for name in {targets} if name in sys.modules]))
        """
    ).format(action=textwrap.indent(textwrap.dedent(action), ""), targets=json.dumps(SDK_MODULES))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Probe failed (exit {result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"Probe produced no output\nSTDERR:\n{result.stderr}")
    return json.loads(lines[-1])


_MODEL = """
from misaka.ai.types import Model, ModelCost
model = Model(id="claude-sonnet-4-6", name="Claude Sonnet 4", api="anthropic-messages", provider="anthropic",
              baseUrl="http://127.0.0.1:9", reasoning=True, input=["text"],
              cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0), contextWindow=200000, maxTokens=8192)
"""


def test_does_not_load_provider_sdks_when_importing_the_root_barrel():
    assert runProbe("") == []


def test_does_not_load_provider_sdks_when_building_all_builtin_providers():
    assert runProbe(
        """
        from misaka.ai.providers import all
        models = all.builtinModels()
        models.getModels()
        """
    ) == []


def test_does_not_load_provider_sdks_when_importing_the_compat_entrypoint():
    assert runProbe(
        """
        import misaka.ai.compat
        """
    ) == []


def test_loads_only_the_anthropic_sdk_when_streaming_through_the_lazy_api_wrapper():
    assert runProbe(
        _MODEL
        + """
from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.types import Context, SimpleStreamOptions, UserMessage
from misaka.ai.utils.transcript import normalizeContext
context = normalizeContext(Context(messages=[UserMessage(content="hi", timestamp=0)]))
async def main():
    await anthropicMessagesApi().streamSimple(model, context, SimpleStreamOptions(apiKey="k")).result()
asyncio.run(main())
"""
    ) == ["anthropic"]


def test_loads_only_the_anthropic_sdk_when_dispatching_through_stream_simple():
    assert runProbe(
        _MODEL
        + """
from misaka.ai.compat import streamSimple
from misaka.ai.types import Context, SimpleStreamOptions, UserMessage
context = Context(messages=[UserMessage(content="hi", timestamp=0)])
async def main():
    await streamSimple(model, context, SimpleStreamOptions(apiKey="k")).result()
asyncio.run(main())
"""
    ) == ["anthropic"]
