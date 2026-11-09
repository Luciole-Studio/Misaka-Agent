"""pi durable's ``test/tool-search.test.ts``."""

from __future__ import annotations

from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import AgentDoc, MemoryStorage, defineExtension, defineTool
from misaka.durable.tool_search import createToolSearchExtension
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context
from tests.harness_support import addTool


def tool(name: str, description: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    async def execute(*_: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": f"{name} ran"}]}

    return defineTool(
        {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"number": {"type": "number", "description": "Issue number"}}},
            "execute": execute,
            **(extra or {}),
        }
    )


DONE = fauxAssistantMessage([fauxText("done")])


def call(name: str, args: dict[str, Any] | None = None, id: str = "c1") -> Any:
    return fauxAssistantMessage([fauxToolCall(name, args or {}, {"id": id})], {"stopReason": "toolUse"})


def setup() -> Any:
    """``alpha``, the deferred ``github_get_issue`` and ``github_list_repos``, then ``omega``, and ``tool_search``
    last."""
    chat = chatSetup()
    addTool(chat.registry, tool("alpha", "Alpha tool"))
    addTool(chat.registry, tool("github_get_issue", "Get a GitHub issue by number", {"deferred": True}))
    addTool(chat.registry, tool("github_list_repos", "List the repositories of a user", {"deferred": True}))
    addTool(chat.registry, tool("omega", "Omega tool"))
    chat.registry.install(createToolSearchExtension())
    return chat


async def offered(root: Any) -> list[str]:
    return [each["name"] for each in (await root.agent(context))["tools"]]


def systemMessages(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The model messages of the ``pi.system`` entries, oldest first."""
    return [entry["model"][0] for entry in entries if entry["kind"] == "pi.system"]


def toolResultTexts(entries: list[dict[str, Any]]) -> list[str]:
    return [
        "".join(item.get("text") or "" for item in entry["model"][0]["content"])
        for entry in entries
        if entry["kind"] == "pi.tool-result"
    ]


def names(tools: list[dict[str, Any]] | None) -> list[str] | None:
    return None if tools is None else [each["name"] for each in tools]


class TestDeferredToolsAndToolSearch:
    async def test_offers_a_deferred_tool_only_once_tool_search_loads_it_by_appending_its_declaration(self) -> None:
        chat = setup()
        chat.faux.setResponses(
            [
                call("tool_search", {"query": "github issue", "limit": 1}),
                call("github_get_issue", {"number": 42}, "c2"),
                DONE,
            ]
        )
        opened = await openChat(MemoryStorage(), chat)
        root = opened.root
        assert await offered(root) == ["alpha", "omega", "tool_search"]
        assert names((await root.agent(context))["deferred"]) == ["github_get_issue", "github_list_repos"]
        # Deferred tools stay callable by other tools, such as code mode scripts.
        assert "github_get_issue" in names((await root.agent(context))["callable"])  # type: ignore[operator]
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        entries = await allEntries(root)
        search, issue = toolResultTexts(entries)
        assert search == "Loaded 1 tool. They are available from your next call:\n- github_get_issue: Get a GitHub issue by number"
        assert issue == "github_get_issue ran"
        assert (await opened.harness.snapshot(AgentDoc, root.id, context))["modelTools"] == {"add": ["github_get_issue"]}
        assert await offered(root) == ["alpha", "omega", "tool_search", "github_get_issue"]
        # A later search sees only the tools not loaded yet.
        assert names((await root.agent(context))["deferred"]) == ["github_list_repos"]
        # The load appended the declaration: no tool was removed and declared again, so the cached prefix holds.
        baseline, load = systemMessages(entries)
        assert names(baseline["toolsAdded"]) == ["alpha", "omega", "tool_search"]
        assert names(load.get("toolsAdded")) == ["github_get_issue"]
        assert load.get("toolsRemoved") is None
        await opened.harness.close(context)

    async def test_appends_each_loaded_tool_in_load_order_not_registry_order(self) -> None:
        chat = setup()
        chat.faux.setResponses(
            [
                call("tool_search", {"query": "repositories", "limit": 1}),
                call("tool_search", {"query": "issue", "limit": 1}, "c2"),
                DONE,
            ]
        )
        opened = await openChat(MemoryStorage(), chat)
        root = opened.root
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await offered(root) == ["alpha", "omega", "tool_search", "github_list_repos", "github_get_issue"]
        loads = systemMessages(await allEntries(root))[1:]
        assert [names(message.get("toolsAdded")) for message in loads] == [["github_list_repos"], ["github_get_issue"]]
        assert all(message.get("toolsRemoved") is None for message in loads)
        await opened.harness.close(context)

    async def test_records_loads_by_name_unknown_and_non_deferred_names_change_nothing_remove_wins_and_a_load_lifts_it(
        self,
    ) -> None:
        chat = setup()

        async def execute(*_: Any) -> dict[str, Any]:
            return {"output": [], "control": {"addTools": ["github_get_issue", "alpha", "ghost"]}}

        grow = defineTool({"name": "grow", "description": "grow", "parameters": {"type": "object", "properties": {}}, "execute": execute})
        addTool(chat.registry, grow)
        chat.faux.setResponses([call("grow"), DONE, call("grow"), DONE])
        opened = await openChat(MemoryStorage(), chat)
        harness, root = opened.harness, opened.root
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert (await harness.snapshot(AgentDoc, root.id, context))["modelTools"] == {
            "add": ["github_get_issue", "alpha", "ghost"]
        }
        assert await offered(root) == ["alpha", "omega", "tool_search", "grow", "github_get_issue"]
        # A name in both remove and add is not offered.
        getIssue = next(each for each in (await root.agent(context))["callable"] if each["name"] == "github_get_issue")
        await root.configure({"modelTools": {"remove": [getIssue], "add": [getIssue]}}, context)
        assert "github_get_issue" not in await offered(root)
        # Loading it again drops it from remove.
        await (await root.submit({"type": "input", "content": "again"}, context)).wait(context)
        assert (await harness.snapshot(AgentDoc, root.id, context))["modelTools"] == {
            "add": ["github_get_issue", "alpha", "ghost"]
        }
        assert "github_get_issue" in await offered(root)
        await harness.close(context)

    async def test_keeps_a_loaded_tool_when_its_extension_is_gone_for_a_while_and_appends_to_an_explicit_array(
        self,
    ) -> None:
        chat = setup()
        issue = tool("github_get_issue", "Get a GitHub issue by number", {"deferred": True})
        chat.faux.setResponses([call("tool_search", {"query": "github issue", "limit": 1}), DONE])
        opened = await openChat(MemoryStorage(), chat)
        harness, root = opened.harness, opened.root
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        # The server goes away: its extension is installed again without the tool.
        chat.registry.install(defineExtension({"name": "tool:github_get_issue", "tools": []}))
        assert "github_get_issue" not in await offered(root)
        chat.registry.install(defineExtension({"name": "tool:github_get_issue", "tools": [issue]}))
        assert "github_get_issue" in await offered(root)
        # With an explicit array, a load appends to it as before.
        alpha = next(each for each in (await root.agent(context))["tools"] if each["name"] == "alpha")
        search = next(each for each in (await root.agent(context))["tools"] if each["name"] == "tool_search")
        await root.configure({"modelTools": [alpha, search]}, context)
        chat.faux.setResponses([call("tool_search", {"query": "repositories", "limit": 1}), DONE])
        await (await root.submit({"type": "input", "content": "more"}, context)).wait(context)
        assert (await harness.snapshot(AgentDoc, root.id, context))["modelTools"] == [
            "alpha",
            "tool_search",
            "github_list_repos",
        ]
        await harness.close(context)

    async def test_keeps_loaded_tools_per_branch_and_in_the_baseline_after_a_reset(self) -> None:
        chat = setup()
        chat.faux.setResponses([call("tool_search", {"query": "github issue", "limit": 1}), DONE])
        opened = await openChat(MemoryStorage(), chat)
        harness, root = opened.harness, opened.root
        await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
        entries = await allEntries(root)
        before = await root.fork(entries[0]["id"], {"ownership": {"kind": "ownerless"}}, context)
        after = await root.fork(entries[-1]["id"], {"ownership": {"kind": "ownerless"}}, context)
        assert "github_get_issue" not in await offered(before)
        assert "github_get_issue" in await offered(after)
        await root.reset(None, context)
        chat.faux.setResponses([DONE])
        await (await root.submit({"type": "input", "content": "fresh"}, context)).wait(context)
        baseline = systemMessages(await allEntries(root))[-1]
        assert names(baseline["toolsAdded"]) == ["alpha", "omega", "tool_search", "github_get_issue"]
        await harness.close(context)

    async def test_finds_nothing_for_an_unrelated_query_and_loads_nothing(self) -> None:
        chat = setup()
        chat.faux.setResponses([call("tool_search", {"query": "weather forecast"}), DONE])
        opened = await openChat(MemoryStorage(), chat)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert toolResultTexts(await allEntries(opened.root)) == ["No matching tools found."]
        assert "modelTools" not in (await opened.harness.snapshot(AgentDoc, opened.root.id, context))
        await opened.harness.close(context)
