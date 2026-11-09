"""Translated from pi-tui's ``test/autocomplete-skill-slash.test.ts`` (v1.1.0)."""

from __future__ import annotations

import os

from misaka.ui.tui.autocomplete import (
    AutocompleteItem,
    CombinedAutocompleteProvider,
    SlashCommand,
)
from misaka.utils.js import AbortController, jsTrimStart

commands = [
    SlashCommand(name="skill:deep-research", description="Multi-agent deep research"),
    SlashCommand(name="skill:research-idea", description="Refine a raw idea into a falsifiable seed"),
    SlashCommand(name="skill:to-sidecar", description="Route work to a sidecar"),
    SlashCommand(name="skill:brainstorm", description="Generate ideas"),
    SlashCommand(name="model", description="Select the active model"),
]


async def suggestionsFor(prefix: str) -> list[str]:
    provider = CombinedAutocompleteProvider(commands, os.getcwd())
    line = f"/{prefix}"
    result = await provider.getSuggestions([line], 0, len(line), {"signal": AbortController().signal})
    assert result, f'expected suggestions for "/{prefix}"'
    return [item.value for item in result.items]


async def test_ranks_skill_research_idea_first_for_query_idea():
    items = await suggestionsFor("idea")
    assert items[0] == "skill:research-idea"
    assert items.index("skill:deep-research") > items.index("skill:research-idea")


async def test_keeps_ordinary_slash_commands_matching():
    items = await suggestionsFor("mod")
    assert "model" in items


async def test_completes_commands_after_leading_whitespace_and_preserves_it():
    provider = CombinedAutocompleteProvider([SlashCommand(name="model")], os.getcwd())
    for line, expected in [
        (" /", " /model "),
        ("  /mod", "  /model "),
        ("\t/mod", "\t/model "),
    ]:
        result = await provider.getSuggestions([line], 0, len(line), {"signal": AbortController().signal})
        assert result
        assert result.prefix == jsTrimStart(line)
        assert [item.value for item in result.items] == ["model"]
        applied = provider.applyCompletion([line], 0, len(line), result.items[0], result.prefix)
        assert applied["lines"][0] == expected
        assert applied["cursorCol"] == len(expected)


async def test_completes_command_arguments_after_leading_whitespace():
    def getArgumentCompletions(prefix: str) -> list[AutocompleteItem]:
        assert prefix == "son"
        return [AutocompleteItem(value="sonnet", label="sonnet")]

    provider = CombinedAutocompleteProvider(
        [SlashCommand(name="model", getArgumentCompletions=getArgumentCompletions)],
        os.getcwd(),
    )
    line = "  /model son"
    result = await provider.getSuggestions([line], 0, len(line), {"signal": AbortController().signal})
    assert result
    assert result.prefix == "son"
    applied = provider.applyCompletion([line], 0, len(line), result.items[0], result.prefix)
    assert applied["lines"][0] == "  /model sonnet"


async def test_keeps_explicit_skill_queries_working():
    items = await suggestionsFor("skill:side")
    assert "skill:to-sidecar" in items


# Regression test for #9944.
async def test_lists_skills_while_typing_the_skill_prefix():
    items = await suggestionsFor("skill")
    assert [item for item in items if item.startswith("skill:")] == [
        command.name for command in commands if command.name.startswith("skill:")
    ]


async def test_keeps_fuzzy_skill_prefix_shorthand_working():
    items = await suggestionsFor("skbra")
    assert "skill:brainstorm" in items
