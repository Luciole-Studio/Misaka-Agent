"""pi durable's ``test/codemode-describe.test.ts``."""

from __future__ import annotations

from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText
from misaka.codemode import CodemodeSandbox
from misaka.codemode.source import CODEMODE_SOURCE_GRAMMAR
from misaka.durable import MemoryStorage, defineExtension, defineTool
from misaka.durable.codemode.codemode import createCodemodeExtension
from misaka.durable.codemode.describe import codemodeDescription, codemodeSection
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context


def tool(name: str, description: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    async def execute(*_: Any) -> dict[str, Any]:
        return {"output": []}

    return defineTool(
        {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string", "description": "File path"}},
            },
            "execute": execute,
            **(extra or {}),
        }
    )


GITHUB = {"name": "github", "description": "GitHub MCP server", "prefix": "mcp__github__"}


class TestCodeModeDescriptionAndSection:
    def test_keeps_the_tool_description_the_same_for_every_conversation(self) -> None:
        text = codemodeDescription(False)
        assert "`await tools.<name>({ ...args })`" in text
        assert "searchTools(query" in text
        assert "The `codemode` section of the system prompt lists the tools scripts can call" in text
        assert "`models`" not in text
        assert "`models`" in codemodeDescription(True)

    def test_lists_callable_tools_as_declarations_grouped_by_namespace_with_namespace_descriptions(self) -> None:
        section = codemodeSection([tool("read", "Read a file."), tool("mcp__github__list_issues", "List issues.")], [GITHUB], 3000)
        assert section is not None
        assert "### `read`\nRead a file." in section
        assert "read(args: {" in section
        assert "## github\nGitHub MCP server" in section
        # Tools before namespaces; a namespace's tools after its heading.
        assert section.index("### `read`") < section.index("## github")
        assert section.index("## github") < section.index("### `mcp__github__list_issues`")
        assert "Promise<string>" in section

    def test_lists_what_fits_the_budget_every_namespace_before_any_is_complete_and_says_what_is_left_out(self) -> None:
        github = [tool(f"mcp__github__tool{index}", "x" * 200) for index in range(30)]
        section = codemodeSection([tool("read", "Read a file."), *github], [GITHUB], 400)
        assert section is not None
        assert "### `read`" in section
        assert "## github (some tools not listed)" in section
        assert "tool29" not in section
        small = codemodeSection([*github], [GITHUB], 10)
        assert small is not None and "## github (tools not listed)" in small
        assert codemodeSection([], [GITHUB], 3000) is None

    def test_uses_the_longest_matching_namespace_prefix(self) -> None:
        section = codemodeSection([tool("mcp__github__x", "x")], [{"name": "git", "prefix": "mcp__git"}, GITHUB], 3000)
        assert section is not None
        assert "## github" in section
        assert "## git\n" not in section

    async def test_renders_the_section_from_the_conversations_callable_tools_into_the_system_prompt(self) -> None:
        setup = chatSetup()
        setup.registry.install(createCodemodeExtension({"sandbox": CodemodeSandbox(), "namespaces": [GITHUB]}))
        setup.registry.install(
            defineExtension(
                {
                    "name": "tools",
                    "tools": [
                        tool("read", "Read a file."),
                        tool("mcp__github__list_issues", "List issues.", {"callers": ["tools"]}),
                        tool("mcp__github__get_issue", "Get an issue.", {"deferred": True}),
                    ],
                }
            )
        )
        setup.faux.setResponses([fauxAssistantMessage([fauxText("hi")])])
        opened = await openChat(MemoryStorage(), setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        system = next(entry for entry in await allEntries(opened.root) if entry["kind"] == "pi.system")
        sections = system["model"][0]["sections"]
        assert "### `read`" in sections["codemode"]
        assert "### `mcp__github__list_issues`" in sections["codemode"]
        # Deferred tools are found with searchTools(), not listed.
        assert "mcp__github__get_issue" not in sections["codemode"]
        # The code mode tool itself is not callable from scripts.
        assert "### `codemode`" not in sections["codemode"]
        # The tool declaration carries the script grammar; pi-ai applies it only for models that support it.
        tools = system["model"][0]["toolsAdded"]
        codemode = next((candidate for candidate in tools if candidate["name"] == "codemode"), None)
        assert codemode is not None
        assert codemode.get("constrainedSampling") == {
            "type": "grammar",
            "variants": {"openai_lark": CODEMODE_SOURCE_GRAMMAR},
        }
        await opened.harness.close(context)
