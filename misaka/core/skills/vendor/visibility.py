# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/prompt_builder.py; see PROVENANCE.json and LICENSE.

def _skill_should_show(
    conditions: dict, available_tools: "set[str] | None", available_toolsets: "set[str] | None",
    session_platform: "str | None" = None,
) -> bool:
    """False if the skill's conditional activation rules exclude it."""
    # Gateway-channel gate runs regardless of tool info; fails open when the platform is unknown.
    wanted_platforms = [str(p).strip().lower() for p in (conditions.get("session_platforms") or []) if str(p).strip()]
    if wanted_platforms and session_platform and session_platform.strip().lower() not in wanted_platforms:
        return False
    if available_tools is None and available_toolsets is None:
        return True  # no filtering info — show everything
    at, ats = available_tools or set(), available_toolsets or set()
    # fallback_for: hide when the primary IS available; requires: hide when a requirement is NOT.
    return not (
        any(ts in ats for ts in conditions.get("fallback_for_toolsets", []))
        or any(t in at for t in conditions.get("fallback_for_tools", []))
        or any(ts not in ats for ts in conditions.get("requires_toolsets", []))
        or any(t not in at for t in conditions.get("requires_tools", []))
    )


def _render_skills_index(
    skills_by_category: dict[str, list[tuple[str, str]]], category_descriptions: dict[str, str],
    compact_categories: "frozenset[str] | None", available_tools: "set[str] | None",
) -> str:
    """Render the ## Skills block; "" when there is nothing to list."""
    if not skills_by_category:
        return ""
    # Demoted categories collapse to one names-only line. NEVER drop entries — agent-created skills are the
    # model's project memory and it won't rediscover them via skills_list. Nested categories follow their parent.
    demoted = frozenset(cat for cat in skills_by_category if cat.split("/", 1)[0] in (compact_categories or frozenset()))
    hidden_note = (
        "\n(Categories marked [names only] are outside the current coding "
        "context, so their descriptions are omitted — the skills work "
        "normally and load with skill_view(name) as usual.)"
    ) if demoted else ""
    # Don't name web_search when the session has no web tools (dangling reference).
    _basic_tools = "terminal" if available_tools is not None and "web_search" not in available_tools else "web_search or terminal"
    index_lines = []
    for category in sorted(skills_by_category):
        entries = skills_by_category[category]
        if category in demoted:
            index_lines.append(f"  {category} [names only]: {', '.join(sorted({n for n, _ in entries}))}")
            continue
        cat_desc = category_descriptions.get(category, "")
        index_lines.append(f"  {category}: {cat_desc}" if cat_desc else f"  {category}:")
        seen = set()
        for name, desc in sorted(entries, key=lambda x: x[0]):  # stable: first entry per name wins
            if name not in seen:
                seen.add(name)
                index_lines.append(f"    - {name}: {desc}" if desc else f"    - {name}")
    return (
        "## Skills\n"
        "Before replying, scan the skills below. If a skill matches or is even partially relevant to your "
        "task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of "
        "loading — it is always better to have context you don't need than to miss critical steps, pitfalls, "
        "or established workflows. Skills contain specialized knowledge — API endpoints, tool-specific "
        "commands, and proven workflows that outperform general-purpose approaches. Load the skill "
        f"even if you think you could handle the task with basic tools like {_basic_tools}. "
        "Skills also encode the user's preferred approach, conventions, and quality standards for tasks like "
        "code review, planning, and testing — load them even for tasks you already know how to do, because "
        "the skill defines how it should be done here.\n"
        "If a skill has issues, fix it with skill_manage(action='patch').\n"
        "After difficult/iterative tasks, offer to save as a skill. If a skill you loaded was missing steps, "
        "had wrong commands, or needed pitfalls you discovered, update it before finishing.\n"
        "\n"
        "<available_skills>\n"
        + "\n".join(index_lines) + "\n"
        "</available_skills>\n\n"
        "Only proceed without loading a skill if genuinely none are relevant to the task."
        + hidden_note
    )

