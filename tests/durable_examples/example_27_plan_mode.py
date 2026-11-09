"""Plan mode: the user switches a conversation into a read-only planning mode, the agent writes a plan into the
extension's own document, and switching back restores the full tool set.

Run from the repository root:
    python -m tests.durable_examples.example_27_plan_mode
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineExtension,
    defineTool,
    section,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import CodingTools, createReadTool

context = BACKGROUND_CONTEXT

# ─── Product code: the plan extension ───────────────────────────────────────

# The current plan of a conversation, ``{"steps": [str]}``. A fork keeps the plan it had at the fork entry.
PlanDoc = defineDoc(
    {
        "kind": "app.plan",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"steps": []},
    }
)


async def executeSubmitPlan(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    async def write(tx: Any) -> None:
        (await tx.doc(PlanDoc, api.conversationId))["steps"] = args["steps"]

    await api.commit(write, callContext)
    return {"output": [{"type": "text", "text": "Plan submitted."}], "control": {"terminate": True}}


submitPlan = defineTool(
    {
        "name": "submit_plan",
        "description": "Submit the plan as a list of steps.",
        "parameters": {
            "type": "object",
            "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
            "required": ["steps"],
        },
        "execute": executeSubmitPlan,
    }
)

Plan = defineExtension(
    {
        "name": "plan",
        "tools": [submitPlan],
        "sections": [
            section("plan_mode", lambda *_: "You are in plan mode. Read the code, then call submit_plan. Change nothing.")
        ],
    }
)

# Plan mode is a change to the conversation's agent: select the plan extension and offer only reading and
# submitting. Clearing both returns to the host's default selection and every tool.
read = createReadTool()
enterPlanMode = {"extensions": {"add": [Plan]}, "tools": [read, submitPlan]}
leavePlanMode = {"extensions": None, "tools": None}


async def main() -> None:
    # ─── Host setup ─────────────────────────────────────────────────────────

    directory = tempfile.mkdtemp(prefix="pi-durable-plan-")
    Path(directory, "server.ts").write_bytes(b"app.listen(3000);\n")

    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("read", {"path": "server.ts"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage(
                [fauxToolCall("submit_plan", {"steps": ["Read PORT from the environment", "Default to 3000"]})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("Implementing step 1."),
        ]
    )

    registry = createRegistry()
    registry.install(CodingTools)
    registry.install(Plan)
    harness = await Harness.open(
        MemoryStorage(),
        {
            "models": models,
            "registry": registry,
            # Installed, but only CodingTools is selected by default: conversations opt into plan mode.
            "settings": {"extensions": [CodingTools]},
            "env": lambda *_: NodeExecutionEnv({"cwd": directory}),
        },
        context,
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})

    async def tools() -> list[str]:
        return [tool["name"] for tool in (await root.agent(context))["tools"]]

    print("tools:", await tools())

    await root.configure(enterPlanMode, context)
    print("plan mode tools:", await tools())
    await (await root.submit({"type": "input", "content": "Make the port configurable."}, context)).wait(context)
    print("plan:", ((await harness.snapshot(PlanDoc, root.id, context)) or {}).get("steps"))

    await root.configure(leavePlanMode, context)
    print("tools again:", await tools())
    await (await root.submit({"type": "input", "content": "Go ahead."}, context)).wait(context)

    # The model saw each switch as a system prompt change in its transcript.
    messages = (await root.context(context))["messages"]
    for message in messages:
        if message["role"] != "system":
            continue
        print(
            "system:",
            {
                # A null section value removes it.
                "sections": message.get("sections"),
                "added": [tool["name"] for tool in message["toolsAdded"]] if "toolsAdded" in message else None,
                "removed": [tool["name"] for tool in message["toolsRemoved"]] if "toolsRemoved" in message else None,
            },
        )

    await harness.close(context)
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
