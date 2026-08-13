"""System-prompt helpers for model-visible skill lists."""

from __future__ import annotations

from misaka.agent.harness.types import Skill


def format_skills_for_system_prompt(skills: list[Skill]) -> str:
    visible_skills = [skill for skill in skills if not skill.disableModelInvocation]
    if not visible_skills:
        return ""

    lines = [
        "The following skills provide specialized instructions for specific tasks.",
        "Read the full skill file when the task matches its description.",
        (
            "When a skill file references a relative path, resolve it against the skill directory "
            "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands."
        ),
        "",
        "<available_skills>",
    ]
    for skill in visible_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
        lines.append(f"    <description>{_escape_xml(skill.description)}</description>")
        lines.append(f"    <location>{_escape_xml(skill.filePath)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


formatSkillsForSystemPrompt = format_skills_for_system_prompt

__all__ = [
    "formatSkillsForSystemPrompt",
    "format_skills_for_system_prompt",
]
