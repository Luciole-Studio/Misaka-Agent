"""pi durable's ``test/harness-prompt.test.ts``."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.utils.transcript import getCurrentTools
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    MemoryStorage,
    SystemEntry,
    createRegistry,
    defineExtension,
    wrapSection,
)
from misaka.durable.harness.agent import resolveAgent, resolveSettings
from misaka.durable.harness.prompt import (
    planSystemEntries,
    renderSections,
    replaySections,
    toToolDeclaration,
)
from misaka.utils.js import AbortController
from tests.durable_support import context
from tests.harness_support import addSection, openHarness, user


async def apply(conversation: Any, desired: dict[str, str]) -> list[dict[str, Any]]:
    """Plan against the current context, append the plan, and check that replay then yields ``desired`` in order."""
    drafts = planSystemEntries(await conversation.context(context), dict(desired), [], 7)

    async def append(tx: Any) -> None:
        for draft in drafts:
            await tx.appendEntry(SystemEntry, conversation.id, draft)

    await conversation.commit(append, context)
    replayed = replaySections((await conversation.context(context))["messages"])
    assert list(replayed.items()) == list(desired.items())
    planned = []
    for draft in drafts:
        message = draft["model"][0]
        assert message["role"] == "system"
        assert message["content"] == ""
        assert message["timestamp"] == 7
        omit = None
        if draft.get("edits") is not None:
            omit = []
            for edit in draft["edits"]:
                assert edit["action"] == "omit"
                omit.append(edit["target"])
        planned.append({"sections": message["sections"]} if omit is None else {"sections": message["sections"], "omit": omit})
    return planned


async def root() -> Any:
    opened = await openHarness(MemoryStorage())
    return await opened.harness.root(context)


async def lastSystemId(conversation: Any) -> int:
    page = await conversation.entries({}, 100, None, context)
    return next(entry for entry in page["items"] if entry["kind"] == "pi.system")["id"]


async def marker(conversation: Any, head: Any) -> int:
    async def append(tx: Any) -> int:
        return (await tx.appendEntry(conversation.id, {"kind": "summary", "head": head, "model": [user("summary")]}))["id"]

    return await conversation.commit(append, context)  # type: ignore[no-any-return]


def section(key: str, render: Any, tag: bool | None = None) -> dict[str, Any]:
    return {"key": key, "render": render} if tag is None else {"key": key, "render": render, "tag": tag}


async def noSnapshot(*args: Any) -> None:
    return None


promptInput = {
    "conversationId": 1,
    "agent": {"thinkingLevel": "off", "extensions": [], "tools": [], "callable": [], "deferred": [], "sections": []},
    "env": None,
    "shown": {},
    "read": SimpleNamespace(snapshot=noSnapshot, snapshotAsOf=noSnapshot),
}


async def test_renders_sections_in_order_with_tags_omissions_wrappers_and_failures():
    registry = createRegistry()
    addSection(registry, "preamble", lambda *_: "You are helpful.", {"tag": False})

    async def cwd(*_: Any) -> str:
        return "/repo"

    addSection(registry, "cwd", cwd)
    addSection(registry, "skipped", lambda *_: None)

    def failing(*_: Any) -> str:
        raise RuntimeError("render failed")

    def newFailing(*_: Any) -> str:
        raise RuntimeError("also failed")

    addSection(registry, "failing", failing)
    addSection(registry, "new-failing", newFailing)

    def gitWrap(inner: dict[str, Any]) -> dict[str, Any]:
        async def render(value: Any, ctx: Any) -> str:
            text = inner["render"](value, ctx)
            if inspect.isawaitable(text):
                text = await text
            return f"{text} (git)"

        return {**inner, "render": render}

    registry.install(defineExtension({"name": "git", "wraps": [wrapSection("cwd", gitWrap)]}))
    reports: list[Any] = []
    shown = {"failing": "<failing>\nold\n</failing>", "cwd": "stale"}

    def rethrow(error: BaseException) -> None:
        raise error

    agent = resolveAgent(None, registry.snapshot(), resolveSettings(None), rethrow)
    desired = await renderSections(agent["sections"], promptInput, shown, reports.append, context)
    assert list(desired.items()) == [
        ("preamble", "You are helpful."),
        ("cwd", "<cwd>\n/repo (git)\n</cwd>"),
        ("failing", "<failing>\nold\n</failing>"),
    ]
    assert [str(error) for error in reports] == ["render failed", "also failed"]


async def test_propagates_section_errors_after_cancellation():
    controller = AbortController()
    controller.abort(RuntimeError("cancelled"))
    cancelled = withAbortSignal(controller.signal, context)

    def failing(*_: Any) -> str:
        raise RuntimeError("cancelled")

    with pytest.raises(Exception, match="cancelled"):
        await renderSections([section("a", failing)], promptInput, {}, lambda error: None, cancelled)


def test_replays_sections_in_place_deletes_on_null_and_appends_re_additions():
    def system(sections: dict[str, str | None]) -> dict[str, Any]:
        return {"role": "system", "content": "", "sections": sections, "timestamp": 1}

    shown = replaySections(
        [system({"a": "1", "b": "2", "c": "3"}), user("x"), system({"b": "20", "a": None}), system({"a": "10"})]
    )
    assert list(shown.items()) == [("b", "20"), ("c", "3"), ("a", "10")]


async def test_emits_minimal_value_patches_removals_and_additions():
    conversation = await root()
    assert await apply(conversation, {"a": "1", "b": "2", "c": "3"}) == [{"sections": {"a": "1", "b": "2", "c": "3"}}]
    assert await apply(conversation, {"a": "1", "b": "20", "c": "3", "d": "4"}) == [{"sections": {"b": "20", "d": "4"}}]
    assert await apply(conversation, {"a": "1", "c": "3", "d": "4"}) == [{"sections": {"b": None}}]
    assert await apply(conversation, {"a": "1", "c": "3", "d": "4"}) == []
    assert await apply(conversation, {}) == [{"sections": {"a": None, "c": None, "d": None}}]


async def test_rewrites_order_only_changes_and_re_additions_as_two_entries():
    conversation = await root()
    await apply(conversation, {"a": "1", "b": "2"})
    assert await apply(conversation, {"b": "2", "a": "1"}) == [
        {"sections": {"a": None, "b": None}},
        {"sections": {"b": "2", "a": "1"}},
    ]
    readded = await root()
    await apply(readded, {"a": "1", "b": "2", "c": "3"})
    assert await apply(readded, {"a": "1", "c": "3"}) == [{"sections": {"b": None}}]
    # Patching would append ``b`` after ``c``.
    assert await apply(readded, {"a": "1", "b": "2", "c": "3"}) == [
        {"sections": {"a": None, "c": None}},
        {"sections": {"a": "1", "b": "2", "c": "3"}},
    ]


async def test_rebaselines_after_a_head_marker_omitting_retained_deltas_on_both_sides_of_it():
    conversation = await root()
    await apply(conversation, {"a": "1", "b": "2"})
    await conversation.commit(lambda tx: tx.appendEntry(conversation.id, {"kind": "pi.user", "model": [user("hi")]}), context)
    await apply(conversation, {"a": "1", "b": "20"})
    delta = await lastSystemId(conversation)
    # The head keeps the delta but cuts its baseline: replay alone would show only ``b``.
    await marker(conversation, delta)
    assert await apply(conversation, {"a": "1", "b": "20"}) == [{"sections": {"a": "1", "b": "20"}, "omit": [delta]}]
    baseline = await lastSystemId(conversation)
    # A system entry follows the marker now, so later changes are ordinary patches.
    assert await apply(conversation, {"a": "1", "b": "21"}) == [{"sections": {"b": "21"}}]
    after = await lastSystemId(conversation)
    # A second marker keeps deltas from both sides of the first one.
    await marker(conversation, delta)
    assert await apply(conversation, {"a": "1", "b": "21"}) == [
        {"sections": {"a": "1", "b": "21"}, "omit": [delta, baseline, after]}
    ]


async def test_writes_a_complete_post_head_baseline_even_when_replay_already_matches():
    conversation = await root()
    await apply(conversation, {"a": "1"})
    baseline = await lastSystemId(conversation)
    await marker(conversation, baseline)
    assert await apply(conversation, {"a": "1"}) == [{"sections": {"a": "1"}, "omit": [baseline]}]
    await marker(conversation, "self")
    assert await apply(conversation, {}) == [{"sections": {}}]
    assert await apply(conversation, {}) == []


def declaration(name: str, description: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description if description is not None else name,
        "parameters": {"type": "object", "properties": {}},
    }


async def applyTools(conversation: Any, tools: list[dict[str, Any]], sections: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Plan tools only, append the plan, check that replay offers ``tools`` in order, and return each message's changes."""
    drafts = planSystemEntries(await conversation.context(context), dict(sections or {}), tools, 7)

    async def append(tx: Any) -> None:
        for draft in drafts:
            await tx.appendEntry(SystemEntry, conversation.id, draft)

    await conversation.commit(append, context)
    offered = getCurrentTools((await conversation.context(context))["messages"])
    assert offered == [toToolDeclaration(tool) for tool in tools]
    changes = []
    for draft in drafts:
        message = draft["model"][0]
        changes.append(
            {
                **({} if message.get("toolsRemoved") is None else {"removed": [tool["name"] for tool in message["toolsRemoved"]]}),
                **({} if message.get("toolsAdded") is None else {"added": [tool["name"] for tool in message["toolsAdded"]]}),
                **({} if message.get("sections") is None else {"sections": message["sections"]}),
            }
        )
    return changes


async def test_adds_removes_replaces_changed_declarations_and_rewrites_the_order_when_needed():
    conversation = await root()
    a, b, c = declaration("a"), declaration("b"), declaration("c")
    assert await applyTools(conversation, [a, b]) == [{"added": ["a", "b"]}]
    assert await applyTools(conversation, [a, b]) == []
    assert await applyTools(conversation, [a, b, c]) == [{"added": ["c"]}]
    assert await applyTools(conversation, [a, c]) == [{"removed": ["b"]}]
    # A changed declaration at the end is removed and re-added in place.
    c2 = declaration("c", "changed")
    assert await applyTools(conversation, [a, c2]) == [{"removed": ["c"], "added": ["c"]}]
    # A changed declaration in the middle would move to the end, so the whole order is rewritten.
    a2 = declaration("a", "changed")
    assert await applyTools(conversation, [a2, c2]) == [{"removed": ["a", "c"], "added": ["a", "c"]}]
    # Order-only change.
    assert await applyTools(conversation, [c2, a2]) == [{"removed": ["a", "c"], "added": ["c", "a"]}]
    assert await applyTools(conversation, []) == [{"removed": ["c", "a"]}]


async def test_puts_tool_changes_on_the_last_section_entry_and_re_declares_every_tool_after_a_head_cut():
    conversation = await root()
    a, b = declaration("a"), declaration("b")
    assert await applyTools(conversation, [a], {"x": "1", "y": "2"}) == [{"added": ["a"], "sections": {"x": "1", "y": "2"}}]
    # Section order changes need two entries; the tool change rides on the second.
    assert await applyTools(conversation, [a, b], {"y": "2", "x": "1"}) == [
        {"sections": {"x": None, "y": None}},
        {"added": ["b"], "sections": {"y": "2", "x": "1"}},
    ]
    await marker(conversation, "self")
    assert await applyTools(conversation, [a, b], {"y": "2", "x": "1"}) == [
        {"added": ["a", "b"], "sections": {"y": "2", "x": "1"}}
    ]
