"""Late join: a client attaches while a run is already underway.

It gets the current state first, the conversation view or the snapshot event, and then only what changes after that.

Run from the repository root:
    python -m tests.durable_examples.example_21_late_join
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxProvider,
    fauxText,
    fauxToolCall,
)
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    defineTool,
    watchEvents,
)
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT


async def pause(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


# A tool that prints a line every 100 ms, then a slowly streamed answer.
async def count(_args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    for n in range(1, 11):
        api.output(f"{n}\n")
        await pause(100)
    return {}


async def main() -> None:
    registry = createRegistry()
    registry.install(
        defineExtension(
            {
                "name": "count",
                "tools": [
                    defineTool(
                        {
                            "name": "count",
                            "description": "Counts to ten",
                            "parameters": {"type": "object", "properties": {}},
                            "execute": count,
                        }
                    )
                ],
            }
        )
    )
    faux = fauxProvider({"tokensPerSecond": 40, "tokenSize": {"min": 1, "max": 1}})
    faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("count", {}, {"id": "call-1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("Counted to ten, and this answer streams slowly.")]),
        ]
    )
    models = createModels()
    models.setProvider(faux.provider)
    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry}, context)
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})

    submission = await root.submit({"type": "input", "content": "Count to ten, then tell me."}, context)
    # Join while the tool is halfway through.
    await pause(500)

    # Structural client: the view holds the committed transcript and documents, including the running tool's output.
    view = await root.viewState(context)
    live = view.value["docs"]["pi.live"]
    print("view entries:", [entry["kind"] for entry in view.value["entries"]])
    slot = (live.get("tools") or [{}])[0]
    print("view tool slot:", slot.get("status"), jsonStringify(slot.get("output")))

    def onView(value: Any, *_: Any) -> None:
        current = (value["docs"]["pi.live"].get("tools") or [{}])[0]
        if current.get("status") == "running":
            print("view output now:", jsonStringify(current["output"]))

    view.subscribe(onView)

    # Event client: the snapshot event carries the same state; later events apply on top of it.
    stream = await watchEvents(harness, root.id, context)
    print("snapshot tools:", [f"{tool['name']} {tool['status']}" for tool in stream.snapshot["tools"]])
    output = {"text": (stream.snapshot["tools"][0].get("output") or "") if stream.snapshot["tools"] else ""}

    async def onEvents(events: Any, *_: Any) -> None:
        for event in events:
            if event["type"] == "tool_execution_update" and event.get("output") is not None:
                change = event["output"]
                if "set" in change:
                    output["text"] = change["set"]
                else:
                    output["text"] = output["text"][change.get("trimStart", 0) :] + change.get("append", "")
                print("event output now:", jsonStringify(output["text"]))
            elif event["type"] == "message_update":
                deltas = [change["delta"] for change in event["changes"] if change["type"] == "text_delta"]
                print("event text delta:", jsonStringify("".join(deltas)))
            else:
                print("event:", event["type"])

    stream.start(onEvents)

    await submission.wait(context)
    await harness.waitForIdle(context)
    await pause(0)
    await stream.stop()
    view.dispose()
    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
