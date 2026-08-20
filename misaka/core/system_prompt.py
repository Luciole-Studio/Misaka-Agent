"""System prompt construction for coding-agent sessions."""

from __future__ import annotations

import datetime as _datetime
from typing import NotRequired, TypedDict

from misaka.core.skills import Skill, format_skills_for_prompt


class BuildSystemPromptOptions(TypedDict):
    cwd: str
    customPrompt: NotRequired[str]
    selectedTools: NotRequired[list[str]]
    toolSnippets: NotRequired[dict[str, str]]
    promptGuidelines: NotRequired[list[str]]
    appendSystemPrompt: NotRequired[str]
    contextFiles: NotRequired[list[dict[str, str]]]
    skills: NotRequired[list[Skill]]


def build_system_prompt(options: BuildSystemPromptOptions) -> str:
    custom_prompt = options.get("customPrompt")
    selected_tools = options.get("selectedTools")
    tool_snippets = options.get("toolSnippets")
    prompt_guidelines = options.get("promptGuidelines")
    append_system_prompt = options.get("appendSystemPrompt")
    cwd = options["cwd"]
    context_files = options.get("contextFiles") or []
    skills = options.get("skills") or []

    prompt_cwd = cwd.replace("\\", "/")
    date = _datetime.date.today().isoformat()
    append_section = f"\n\n{append_system_prompt}" if append_system_prompt else ""

    if custom_prompt:
        prompt = custom_prompt
        if append_section:
            prompt += append_section
        if context_files:
            prompt += _format_project_context(context_files)
        if (selected_tools is None or "read" in selected_tools) and skills:
            prompt += format_skills_for_prompt(skills)
        prompt += f"\nCurrent date: {date}"
        # 尾换行：后续 append 的 prompt 内容必须另起一行（上游 #7887/3dd4623ee）
        prompt += f"\nCurrent working directory: {prompt_cwd}\n"
        return prompt

    tools = selected_tools or ["read", "bash", "edit", "write"]
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
    has_grep = "grep" in tools
    has_find = "find" in tools
    has_ls = "ls" in tools
    has_read = "read" in tools

    if has_bash and not has_grep and not has_find and not has_ls:
        add_guideline("Use bash for file operations like ls, rg, find")
    elif has_bash and (has_grep or has_find or has_ls):
        add_guideline("Prefer grep/find/ls tools over bash for file exploration (faster, respects .gitignore)")

    for guideline in prompt_guidelines or []:
        normalized = guideline.strip()
        if normalized:
            add_guideline(normalized)

    add_guideline("Be concise in your responses")
    add_guideline("Show file paths clearly when working with files")

    guidelines_text = "\n".join(f"- {guideline}" for guideline in guidelines)
    # MISAKA: fork 定制——原 harn 版自称 coding assistant 并附 harn 文档区
    # （其 examples/ 路径在本仓不存在）。角色的真实人格由 appended 的 SOUL 定义。
    prompt = f"""You are an agent of MISAKA (御坂网络), a multi-agent research system for the humanities and social sciences. Your specific role and working discipline are defined in the role instructions appended below — follow them over any generic assumptions.

Available tools:
{tools_list}

In addition to the tools above, you may have access to other custom tools depending on the project.

Guidelines:
{guidelines_text}"""

    if append_section:
        prompt += append_section
    if context_files:
        prompt += _format_project_context(context_files)
    if has_read and skills:
        prompt += format_skills_for_prompt(skills)
    prompt += f"\nCurrent date: {date}"
    prompt += f"\nCurrent working directory: {prompt_cwd}"
    return prompt


def _format_project_context(context_files: list[dict[str, str]]) -> str:
    prompt = "\n\n<project_context>\n\nProject-specific instructions and guidelines:\n\n"
    for item in context_files:
        prompt += f'<project_instructions path="{item["path"]}">\n{item["content"]}\n</project_instructions>\n\n'
    prompt += "</project_context>\n"
    return prompt


buildSystemPrompt = build_system_prompt

__all__ = [
    "BuildSystemPromptOptions",
    "buildSystemPrompt",
]
