"""Translated from pi's ``packages/coding-agent/test/system-prompt.test.ts`` (v1.1.0).

MISAKA fork: not translated -- the skills cases (the "skills" describe block and the hidden-tools case about loading
skill files; MISAKA's skills live in ``misaka/core/skills`` and are not listed in the system prompt) and the pi docs
case (MISAKA's default prompt has no docs section, see ``buildSystemPrompt``).
"""

from __future__ import annotations

import os
import re

import pytest

from misaka.core.system_prompt import buildSystemPrompt

# --- empty tools --------------------------------------------------------------------------------


def test_shows_none_for_empty_tools_list():
    prompt = buildSystemPrompt({"selectedTools": [], "contextFiles": [], "skills": [], "cwd": os.getcwd()})

    assert "<tools>\n(none)\n" in prompt


def test_shows_file_paths_guideline_even_with_no_tools():
    prompt = buildSystemPrompt({"selectedTools": [], "contextFiles": [], "skills": [], "cwd": os.getcwd()})

    assert "Show file paths clearly" in prompt


# --- prompt structure ---------------------------------------------------------------------------


def test_keeps_the_default_and_custom_prompt_prefixes_exact():
    defaultPrompt = buildSystemPrompt({"cwd": "/tmp", "selectedTools": [], "contextFiles": [], "skills": []})
    customPrompt = buildSystemPrompt({
        "customPrompt": "You are Exact.",
        "cwd": "/tmp",
        "selectedTools": [],
        "contextFiles": [],
        "skills": [],
    })

    # MISAKA fork: MISAKA's default prompt introduces MISAKA, where pi's opens "You are an expert coding assistant
    # operating inside pi".
    assert defaultPrompt.startswith("You are an agent of MISAKA")
    assert customPrompt.startswith("You are Exact.\n\n<cwd>")


def test_preserves_an_exact_forced_prompt_without_sections():
    assert buildSystemPrompt({"forceSystemPrompt": "exact", "cwd": "/tmp"}) == "exact"


def test_maps_appended_instructions_and_project_context_to_stable_sections():
    prompt = buildSystemPrompt({
        "customPrompt": "You are Exact.",
        "appendSystemPrompt": "Additional instructions.",
        "contextFiles": [{"path": "/tmp/AGENTS.md", "content": "Project instructions."}],
        "selectedTools": [],
        "skills": [],
        "cwd": "/tmp",
    })

    assert "<addendum>\nAdditional instructions.\n</addendum>" in prompt
    assert (
        '<project_context>\nProject-specific instructions and guidelines:\n\n<project_instructions path="/tmp/AGENTS.md">'
        in prompt
    )
    assert "<cwd>\n/tmp\n</cwd>" in prompt


# --- default tools ------------------------------------------------------------------------------


def test_includes_all_default_tools_when_snippets_are_provided():
    prompt = buildSystemPrompt({
        "toolSnippets": {
            "read": "Read file contents",
            "bash": "Execute bash commands",
            "edit": "Make surgical edits",
            "write": "Create or overwrite files",
        },
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })

    for name in ("read", "bash", "edit", "write"):
        assert f"- {name}:" in prompt


@pytest.mark.parametrize(
    ("selectedTools", "expected"),
    [
        (["powershell"], "Use PowerShell for file operations"),
        (["bash", "powershell"], "Use bash or PowerShell for file operations"),
    ],
)
def test_uses_shell_specific_guidance(selectedTools: list[str], expected: str):
    prompt = buildSystemPrompt({"selectedTools": selectedTools, "contextFiles": [], "skills": [], "cwd": os.getcwd()})

    assert expected in prompt


# --- custom tool snippets -----------------------------------------------------------------------


def test_includes_custom_tools_in_available_tools_section_when_prompt_snippet_is_provided():
    prompt = buildSystemPrompt({
        "selectedTools": ["read", "dynamic_tool"],
        "toolSnippets": {"dynamic_tool": "Run dynamic test behavior"},
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })

    assert "- dynamic_tool: Run dynamic test behavior" in prompt


def test_omits_custom_tools_from_available_tools_section_when_prompt_snippet_is_not_provided():
    prompt = buildSystemPrompt({
        "selectedTools": ["read", "dynamic_tool"],
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })

    assert "dynamic_tool" not in prompt


# --- prompt guidelines --------------------------------------------------------------------------


def test_appends_prompt_guidelines_to_default_guidelines():
    prompt = buildSystemPrompt({
        "selectedTools": ["read", "dynamic_tool"],
        "promptGuidelines": ["Use dynamic_tool for project summaries."],
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })

    assert "- Use dynamic_tool for project summaries." in prompt


def test_deduplicates_and_trims_prompt_guidelines():
    prompt = buildSystemPrompt({
        "selectedTools": ["read", "dynamic_tool"],
        "promptGuidelines": ["Use dynamic_tool for summaries.", "  Use dynamic_tool for summaries.  ", "   "],
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })

    assert len(re.findall(r"- Use dynamic_tool for summaries\.", prompt)) == 1


# --- hidden tools (#10343) ----------------------------------------------------------------------


def build(hiddenTools: list[str]) -> str:
    return buildSystemPrompt({
        "selectedTools": ["read", "bash", "run"],
        "hiddenTools": hiddenTools,
        "toolSnippets": {"read": "Read files", "bash": "Run commands", "run": "Run a task"},
        "toolGuidelines": {"read": ["Use read for files."], "run": ["Prefer run."]},
        "contextFiles": [],
        "skills": [],
        "cwd": os.getcwd(),
    })


def test_leaves_hidden_tools_out_of_the_tool_list_and_rules():
    prompt = build(["read", "bash"])

    assert "<tools>\n- run: Run a task\n" in prompt
    assert "- read: " not in prompt
    assert "Use read for files." not in prompt
    assert "Use bash for file operations" not in prompt
    assert "- Prefer run." in prompt
