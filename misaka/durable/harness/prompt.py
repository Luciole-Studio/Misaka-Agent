"""System prompt sections and tool declarations as ``pi.system`` entries.

Translated from pi's ``packages/durable/src/harness/prompt.ts``. Messages, entries, tools and sections are plain ``dict``
objects with the upstream keys; the replay helpers are those of ``misaka.ai.utils.transcript``, which read dicts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from misaka.ai.utils.transcript import declarationsEqual, getCurrentTools
from misaka.ai.utils.transcript import toToolDeclaration as _toToolDeclaration
from misaka.chord.context import Context
from misaka.utils.js import awaitValue

from ..entries import SystemEntry


def toToolDeclaration(tool: Any) -> dict[str, Any]:
    """The declaration of a tool as strict JSON: its name, description, parameters and sampling constraint."""
    return _toToolDeclaration(tool).model_dump(mode="json", exclude_none=True)


def replaySections(messages: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Sections in effect after replaying system messages in order: set in place, ``None`` deletes, re-adding
    appends."""
    shown: dict[str, str] = {}
    for message in messages:
        if message["role"] != "system" or message.get("sections") is None:
            continue
        for key, value in message["sections"].items():
            if value is None:
                shown.pop(key, None)
            else:
                shown[key] = value
    return shown


async def renderSections(
    sections: Sequence[dict[str, Any]],
    input: dict[str, Any],
    shown: Mapping[str, str],
    report: Callable[[BaseException], None],
    context: Context,
) -> dict[str, str]:
    """Render the agent's sections in order. ``None`` omits a section; tagged text is wrapped as ``<key>\\n...\\n</key>``.
    A section that throws keeps its shown text, if any, and is reported; errors after ``context`` is aborted
    propagate."""
    desired: dict[str, str] = {}
    for section in sections:
        try:
            text = await awaitValue(section["render"](input, context))
        except Exception as error:
            signal = context.abortSignal
            if signal is not None and signal.aborted:
                raise
            report(error)
            kept = shown.get(section["key"])
            if kept is not None:
                desired[section["key"]] = kept
            continue
        if text is None:
            continue
        desired[section["key"]] = (
            text if section.get("tag") is False else f"<{section['key']}>\n{text}\n</{section['key']}>"
        )
    return desired


def planSystemEntries(
    view: dict[str, Any], desired: Mapping[str, str], tools: Sequence[dict[str, Any]], timestamp: int
) -> list[dict[str, Any]]:
    """Plan the ``pi.system`` entries that make the replayed sections and tools of ``view`` equal ``desired`` and
    ``tools`` in values and order.

    - A head marker with no later ``pi.system`` entry in context: one complete baseline that omits every retained
      earlier ``pi.system`` entry, written even when it restates the replayed values.
    - Otherwise, when a minimal section patch would leave a different order: remove every shown section, then re-add
      every desired section in order.
    - Otherwise the minimal patch of changed values and ``None`` removals, or nothing.

    Tool changes ride on the last planned entry, or on one entry of their own."""
    head = view["head"]
    if head is not None and not any(
        SystemEntry.is_(entry) and entry["id"] > head["id"] for entry in view["entries"]
    ):
        edits = [{"target": entry["id"], "action": "omit"} for entry in view["entries"] if SystemEntry.is_(entry)]
        baseline = systemEntry(
            dict(desired),
            {"toolsRemoved": [], "toolsAdded": [toToolDeclaration(tool) for tool in tools]},
            timestamp,
        )
        return [baseline if len(edits) == 0 else {**baseline, "edits": edits}]
    sections = planSections(replaySections(view["messages"]), desired)
    changes = planTools(getCurrentTools(view["messages"]), tools)
    if len(changes["toolsRemoved"]) == 0 and len(changes["toolsAdded"]) == 0:
        return [systemEntry(patch, None, timestamp) for patch in sections]
    if len(sections) == 0:
        return [systemEntry(None, changes, timestamp)]
    return [
        systemEntry(patch, changes if index == len(sections) - 1 else None, timestamp)
        for index, patch in enumerate(sections)
    ]


def planTools(offered: Sequence[Any], desired: Sequence[Any]) -> dict[str, Any]:
    """Tool changes from ``offered`` to ``desired``. A changed declaration is removed and re-added. Replay keeps
    retained tools in place and appends additions; when that would not yield the desired order, every offered tool is
    removed and every desired tool re-added in order."""
    wanted = {tool["name"]: tool for tool in desired}
    kept = [
        tool
        for tool in offered
        if wanted.get(tool["name"]) is not None and declarationsEqual(tool, wanted[tool["name"]])
    ]
    keptNames = {tool["name"] for tool in kept}
    added = [tool for tool in desired if tool["name"] not in keptNames]
    replayed = [*kept, *added]
    if any(tool["name"] != desired[index]["name"] for index, tool in enumerate(replayed)):
        return {
            "toolsRemoved": [{"name": tool["name"]} for tool in offered],
            "toolsAdded": [toToolDeclaration(tool) for tool in desired],
        }
    return {
        "toolsRemoved": [{"name": tool["name"]} for tool in offered if tool["name"] not in keptNames],
        "toolsAdded": [toToolDeclaration(tool) for tool in added],
    }


def planSections(shown: Mapping[str, str], desired: Mapping[str, str]) -> list[dict[str, str | None]]:
    """Section patches: none, the minimal patch, or a remove-all/re-add-all pair when the order would differ."""
    patchedOrder = [
        *[key for key in shown if key in desired],
        *[key for key in desired if key not in shown],
    ]
    desiredOrder = list(desired)
    if any(index >= len(desiredOrder) or key != desiredOrder[index] for index, key in enumerate(patchedOrder)):
        return [{key: None for key in shown}, dict(desired)]

    patch: dict[str, str | None] = {}
    for key, value in shown.items():
        following = desired.get(key)
        if following != value:
            patch[key] = following
    for key, value in desired.items():
        if key not in shown:
            patch[key] = value
    return [] if len(patch) == 0 else [patch]


def systemEntry(
    sections: dict[str, str | None] | None, tools: dict[str, Any] | None, timestamp: int
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "system",
        "content": "",
        **({} if sections is None else {"sections": sections}),
        **({} if tools is None or len(tools["toolsRemoved"]) == 0 else {"toolsRemoved": tools["toolsRemoved"]}),
        **({} if tools is None or len(tools["toolsAdded"]) == 0 else {"toolsAdded": tools["toolsAdded"]}),
        "timestamp": timestamp,
    }
    return {"model": [message]}


__all__ = ["planSystemEntries", "renderSections", "replaySections", "toToolDeclaration"]
