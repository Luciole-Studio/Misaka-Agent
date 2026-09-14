"""System prompt construction for coding-agent sessions."""

from __future__ import annotations

from typing import NotRequired, TypedDict

CURRENT_TOOLS_GUIDELINE = "Use only the tools offered in the current request; tool names in conversation history do not grant capabilities."


class BuildSystemPromptOptions(TypedDict):
    cwd: str
    customPrompt: NotRequired[str]
    selectedTools: NotRequired[list[str]]
    toolSnippets: NotRequired[dict[str, str]]
    promptGuidelines: NotRequired[list[str]]
    appendSystemPrompt: NotRequired[str]
    contextFiles: NotRequired[list[dict[str, str]]]


def build_system_prompt(options: BuildSystemPromptOptions) -> str:
    custom_prompt = options.get("customPrompt")
    selected_tools = options.get("selectedTools")
    tool_snippets = options.get("toolSnippets")
    prompt_guidelines = options.get("promptGuidelines")
    append_system_prompt = options.get("appendSystemPrompt")
    cwd = options["cwd"]
    context_files = options.get("contextFiles") or []

    prompt_cwd = cwd.replace("\\", "/")
    append_section = f"\n\n{append_system_prompt}" if append_system_prompt else ""

    if custom_prompt:
        prompt = custom_prompt
        if append_section:
            prompt += append_section
        if context_files:
            prompt += _format_project_context(context_files)
        # Trailing newline: anything appended to the prompt later must start on its own line (upstream #7887/3dd4623ee)
        prompt += f"\nCurrent working directory: {prompt_cwd}\n"
        return prompt

    tools = (
        selected_tools
        if selected_tools is not None
        else ["read", "bash", "edit", "write", "office"]
    )
    visible_tools = [name for name in tools if tool_snippets and tool_snippets.get(name)]
    tools_list = "\n".join(f"- {name}: {tool_snippets[name]}" for name in visible_tools) if visible_tools else "(none)"

    guidelines: list[str] = []
    seen_guidelines: set[str] = set()

    def add_guideline(guideline: str) -> None:
        if guideline in seen_guidelines:
            return
        seen_guidelines.add(guideline)
        guidelines.append(guideline)

    has_bash = "bash" in tools
    has_powershell = "powershell" in tools
    has_grep = "grep" in tools
    has_find = "find" in tools
    has_ls = "ls" in tools

    if (has_bash or has_powershell) and not has_grep and not has_find and not has_ls:
        if has_bash and has_powershell:
            add_guideline(
                "Use bash or PowerShell for file operations like listing, searching, and finding files"
            )
        elif has_powershell:
            add_guideline(
                "Use PowerShell for file operations like listing, searching, and finding files"
            )
        else:
            add_guideline("Use bash for file operations like ls, rg, find")

    for guideline in prompt_guidelines or []:
        normalized = guideline.strip()
        if normalized:
            add_guideline(normalized)

    add_guideline("Batch independent tool calls into one turn; serialize only when a call depends on an earlier result")
    add_guideline("Be concise in your responses")
    add_guideline("Show file paths clearly when working with files")

    guidelines_text = "\n".join(f"- {guideline}" for guideline in guidelines)
    # MISAKA fork: the harn original called itself a coding assistant and appended a harn
    # docs section. Here the role stack (shared soul, identity, charter) comes first, via
    # the append slot, so the model reads who it is before what it can do.
    prompt = (
        "You are an agent of MISAKA, a collaborative multi-agent research system for the humanities and social sciences. "
        "Last Order coordinates research and user requirements; Sisters are domain specialists who plan and execute their assignments."
    )
    if append_section:
        prompt += append_section
    prompt += f"""

Available tools:
{tools_list}

{CURRENT_TOOLS_GUIDELINE}

Guidelines:
{guidelines_text}"""
    if context_files:
        prompt += _format_project_context(context_files)
    prompt += f"\nCurrent working directory: {prompt_cwd}"
    return prompt


def _format_project_context(context_files: list[dict[str, str]]) -> str:
    prompt = "\n\n<project_context>\n\nProject-specific instructions and guidelines:\n\n"
    for item in context_files:
        prompt += f'<project_instructions path="{item["path"]}">\n{item["content"]}\n</project_instructions>\n\n'
    prompt += "</project_context>\n"
    return prompt


__all__ = [
    "BuildSystemPromptOptions",
]
