"""System prompt sections and per-conversation instructions.

Run from the repository root:
    python -m tests.durable_examples.example_15_system_prompt
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    SystemEntry,
    createRegistry,
    defineExtension,
    section,
    wrapSection,
)
from misaka.durable.env.node import NodeExecutionEnv

context = BACKGROUND_CONTEXT

# Pico stores no prompt state. Before each model request, the sections of the
# conversation's selected extensions render the desired prompt, and only the
# difference to what the model already saw is appended to the transcript as a
# `pi.system` entry.
Coding = defineExtension(
    {
        "name": "coding",
        "sections": [
            # `tag: False` sends the text as is; by default it is wrapped in <key>...</key>.
            section("preamble", lambda *_: "You are a coding agent.", {"tag": False}),
            # Sections see the environment built for this request, here its working directory.
            section("cwd", lambda input, *_: None if input["env"] is None else input["env"].cwd),
        ],
    }
)
AgentsMd = defineExtension(
    {"name": "agents-md", "sections": [section("agents_md", lambda *_: "Run npm run check after changes.")]}
)


def terser(preamble: dict[str, Any]) -> dict[str, Any]:
    async def render(input: Any, renderContext: Any) -> str:
        text = preamble["render"](input, renderContext)
        if inspect.isawaitable(text):
            text = await text
        return f"{text} Be terse."

    return {**preamble, "render": render}


# Another extension decorates a section by key without replacing it.
Terse = defineExtension({"name": "terse", "wraps": [wrapSection("preamble", terser)]})


async def systemEntries(conversation: Any) -> list[dict[str, Any]]:
    page = await conversation.entries({}, 20, None, context)
    return [
        message for entry in reversed(page["items"]) if SystemEntry.is_(entry) for message in entry.get("model") or []
    ]


async def main() -> None:
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses([fauxAssistantMessage("Done."), fauxAssistantMessage("Done."), fauxAssistantMessage("Done.")])

    registry = createRegistry()
    registry.install(Coding)
    registry.install(AgentsMd)
    registry.install(Terse)

    # The environment follows each conversation's agent `cwd`.
    def env(target: dict[str, Any], callContext: Any) -> Any:
        return NodeExecutionEnv({"cwd": target.get("cwd", "/")})

    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry, "env": env}, context)
    model = {"provider": "faux", "modelId": "faux-1"}
    root = await harness.root(context, {"agent": {"model": model, "cwd": "/repo"}})

    # A subagent deselects AGENTS.md and gets its own instructions, rendered last
    # as the `instructions` section.
    subagent = await harness.createConversation(
        {
            "ownership": {"kind": "ownerless"},
            "agent": {
                "model": model,
                "cwd": "/repo",
                "extensions": {"remove": [AgentsMd]},
                "instructions": "Only read; never edit files.",
            },
        },
        context,
    )

    await (await root.submit({"type": "input", "content": "Fix the build."}, context)).wait(context)
    await (await subagent.submit({"type": "input", "content": "Read the logs."}, context)).wait(context)
    print("root system prompt:", await systemEntries(root))
    print("subagent system prompt:", await systemEntries(subagent))

    # When a section's output changes, the next request appends only the change.
    await root.configure({"cwd": "/repo/packages"}, context)
    await (await root.submit({"type": "input", "content": "Now the package."}, context)).wait(context)
    print("root system entries after cwd change:", await systemEntries(root))

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
