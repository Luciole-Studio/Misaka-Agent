"""System prompt construction for coding-agent sessions (pi ``core/system-prompt.ts``).

The prompt is a set of ordered, independently replaceable sections. The leading system
message of a transcript carries them; a later system message replaces sections by name, so
a prompt change mid-conversation is a small patch rather than a rewrite of the head.
"""

from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict

from misaka.ai.types import SystemMessage
from misaka.ai.utils.text import get_system_message_text

CURRENT_TOOLS_GUIDELINE = "Use only the tools offered in the current request; tool names in conversation history do not grant capabilities."

SYSTEM_PROMPT_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class BuildSystemPromptOptions(TypedDict):
    cwd: str
    customPrompt: NotRequired[str]
    # A rendering of the current prompt sent verbatim as the provider's leading system prompt
    # without being recorded (a `before_agent_start` handler's `systemPrompt`).
    forceSystemPrompt: NotRequired[str]
    selectedTools: NotRequired[list[str]]
    toolSnippets: NotRequired[dict[str, str]]
    # Per-tool guidelines, keyed by tool name; only the selected tools' apply.
    toolGuidelines: NotRequired[dict[str, list[str]]]
    promptGuidelines: NotRequired[list[str]]
    appendSystemPrompt: NotRequired[str]
    # Custom sections rendered after the built-in ones; names are section names.
    sections: NotRequired[dict[str, str]]
    contextFiles: NotRequired[list[dict[str, str]]]


def normalize_build_system_prompt_options(input: BuildSystemPromptOptions | dict[str, Any]) -> dict[str, Any]:
    """Normalize prompt input into the mutable, collection-complete shape exposed to extensions."""
    return {
        "customPrompt": input.get("customPrompt"),
        "forceSystemPrompt": input.get("forceSystemPrompt"),
        # MISAKA fork: `office` is a built-in here, so it is in the default loadout.
        # `??`, not `||`: an explicit empty list means no tools.
        "selectedTools": [
            *(["read", "bash", "edit", "write", "office"] if input.get("selectedTools") is None else input["selectedTools"])
        ],
        "toolSnippets": {**(input.get("toolSnippets") or {})},
        "toolGuidelines": {name: [*guidelines] for name, guidelines in (input.get("toolGuidelines") or {}).items()},
        "promptGuidelines": [*(input.get("promptGuidelines") or [])],
        "appendSystemPrompt": input.get("appendSystemPrompt") or "",
        "sections": {**(input.get("sections") or {})},
        "cwd": input["cwd"],
        "contextFiles": [{**file} for file in (input.get("contextFiles") or [])],
    }


def _render_project_context(context_files: list[dict[str, str]]) -> str:
    return "\n\n".join(
        [
            "Project-specific instructions and guidelines:",
            *(
                f'<project_instructions path="{file["path"]}">\n{file["content"]}\n</project_instructions>'
                for file in context_files
            ),
        ]
    )


def _build_rules(selected_tools: list[str], tool_guidelines: dict[str, list[str]], prompt_guidelines: list[str]) -> str:
    rules: list[str] = []
    seen: set[str] = set()

    def add_rule(rule: str) -> None:
        normalized = rule.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        rules.append(normalized)

    has_bash = "bash" in selected_tools
    has_powershell = "powershell" in selected_tools
    has_grep = "grep" in selected_tools
    has_find = "find" in selected_tools
    has_ls = "ls" in selected_tools
    if (has_bash or has_powershell) and not has_grep and not has_find and not has_ls:
        if has_bash and has_powershell:
            add_rule("Use bash or PowerShell for file operations like listing, searching, and finding files")
        elif has_powershell:
            add_rule("Use PowerShell for file operations like listing, searching, and finding files")
        else:
            add_rule("Use bash for file operations like ls, rg, find")
    for name in selected_tools:
        for rule in tool_guidelines.get(name) or []:
            add_rule(rule)
    for rule in prompt_guidelines:
        add_rule(rule)
    # MISAKA fork: one more standing rule than pi's two.
    add_rule("Batch independent tool calls into one turn; serialize only when a call depends on an earlier result")
    add_rule("Be concise in your responses")
    add_rule("Show file paths clearly when working with files")
    return "\n".join(f"- {rule}" for rule in rules)


def build_system_prompt_sections(input: BuildSystemPromptOptions | dict[str, Any]) -> dict[str, str]:
    """Build the ordered, independently replaceable sections of the structured system prompt."""
    options = normalize_build_system_prompt_options(input)
    custom_prompt = options["customPrompt"]
    selected_tools = options["selectedTools"]
    tool_snippets = options["toolSnippets"]
    tool_guidelines = options["toolGuidelines"]
    prompt_guidelines = options["promptGuidelines"]
    append_system_prompt = options["appendSystemPrompt"]
    custom_sections = options["sections"]
    cwd = options["cwd"]
    context_files = options["contextFiles"]
    for name in custom_sections:
        if not SYSTEM_PROMPT_SECTION_NAME.match(name) or name == "preamble":
            raise ValueError(f"Invalid system prompt section name: {name}")

    prompt_sections: dict[str, str] = {}
    if custom_prompt:
        prompt_sections["preamble"] = custom_prompt
        if append_system_prompt:
            prompt_sections["addendum"] = append_system_prompt
    else:
        # MISAKA fork: pi's preamble calls itself a coding assistant and is followed by the
        # tools, rules and a pi docs section. Here the identity comes first and the role
        # stack (shared soul, identity, charter -- the append slot) comes *before* the
        # tools, so the model reads who it is before what it can do; there is no docs section.
        prompt_sections["preamble"] = (
            "You are an agent of MISAKA, a collaborative multi-agent research system for the humanities and social sciences. "
            "Last Order coordinates research and user requirements; Sisters are domain specialists who plan and execute their assignments."
        )
        if append_system_prompt:
            prompt_sections["addendum"] = append_system_prompt
        visible_tools = [name for name in selected_tools if tool_snippets.get(name)]
        tools = (
            "\n".join(f"- {name}: {tool_snippets[name]}" for name in visible_tools) if visible_tools else "(none)"
        )
        prompt_sections["tools"] = (
            f"{tools}\n\nIn addition to the tools above, you may have access to other custom tools depending on the project."
            f"\n\n{CURRENT_TOOLS_GUIDELINE}"
        )
        prompt_sections["rules"] = _build_rules(selected_tools, tool_guidelines, prompt_guidelines)
    if context_files:
        prompt_sections["project_context"] = _render_project_context(context_files)
    prompt_sections["cwd"] = cwd.replace("\\", "/")
    for name, content in custom_sections.items():
        if content:
            prompt_sections[name] = content
    sections: dict[str, str] = {"preamble": prompt_sections["preamble"]}
    for name, content in prompt_sections.items():
        if name != "preamble":
            sections[name] = f"<{name}>\n{content}\n</{name}>"
    return sections


def build_system_prompt_state(input: BuildSystemPromptOptions | dict[str, Any]) -> dict[str, Any]:
    """The complete prompt state for `input`. A forced prompt is opaque and lives in `content`
    with no sections; otherwise `content` is empty and the structured sections carry the prompt."""
    if input.get("forceSystemPrompt") is not None:
        return {"content": input["forceSystemPrompt"]}
    return {"content": "", "sections": build_system_prompt_sections(input)}


def build_system_prompt(input: BuildSystemPromptOptions | dict[str, Any]) -> str:
    """Build the system prompt text, rendered exactly as the transcript's system message replays it."""
    return get_system_message_text(SystemMessage(**build_system_prompt_state(input), timestamp=0))


def diff_system_prompt_sections(previous: dict[str, str], current: dict[str, str]) -> dict[str, str | None] | None:
    """Diff the sections the model currently has (replayed from the transcript, so never null)
    against the desired ones. Returns a `SystemMessage.sections` patch, or None when
    nothing changed."""
    patch: dict[str, str | None] = {}
    for name, text in current.items():
        if previous.get(name) != text:
            patch[name] = text
    for name in previous:
        if name not in current:
            patch[name] = None
    return patch if patch else None


__all__ = [
    "BuildSystemPromptOptions",
    "build_system_prompt",
    "build_system_prompt_sections",
    "build_system_prompt_state",
    "diff_system_prompt_sections",
    "normalize_build_system_prompt_options",
]
