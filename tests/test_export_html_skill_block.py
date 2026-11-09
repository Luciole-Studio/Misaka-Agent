"""Ported from pi's ``packages/coding-agent/test/export-html-skill-block.test.ts`` (v1.1.0)."""

from __future__ import annotations

import re
from pathlib import Path

from misaka.core import export_html

templateJs = (Path(export_html.__file__).parent / "template.js").read_text(encoding="utf-8")


def test_strips_skill_wrapper_xml_from_user_message_rendering():
    # Skill commands store a structural wrapper in the raw user message:
    #   <skill name="..." location="...">\n...\n</skill>\n\nactual prompt
    # The export renderer must detect that wrapper and render only the user-visible prompt,
    # not the Pi-generated <skill>...</skill> XML tags.
    assert re.search(r"parseSkillBlock", templateJs)
    assert re.search(r"skillBlock\.userMessage", templateJs)


def test_renders_skill_invocation_and_user_message_as_separate_sibling_blocks():
    # The skill block and user message should render as separate entry-level elements,
    # matching the TUI layout where SkillInvocationMessageComponent and
    # UserMessageComponent are siblings, not nested.
    assert re.search(r"skill-invocation", templateJs)

    # When a skill block has a userMessage, the user-message div must be emitted
    # as a separate block after the skill-invocation div, containing the user-authored text.
    # Verify the code checks hasUserContent so the user-message div is only omitted
    # when the skill block has no user prompt and no images.
    assert re.search(r"hasUserContent", templateJs)


def test_renders_skill_content_as_markdown_not_raw_text():
    # The skill block body is markdown (from the SKILL.md file).
    # It should be rendered through safeMarkedParse, not escaped as raw text.
    assert re.search(r"safeMarkedParse\(skillBlock\.content\)", templateJs)


def test_shows_skill_name_and_user_message_in_the_sidebar_tree():
    # The sidebar tree should display both the skill name and the user prompt,
    # not just one or the other.
    assert re.search(r"tree-role-skill", templateJs)
