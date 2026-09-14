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
    compact_categories: "frozenset[str] | None", available_tools: "set[str] | None", *, can_manage: bool = True,
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
        "Before acting, check the skills relevant to the work you are actually performing. Load applicable "
        "instructions with skill_view(name) and follow their relevant workflow and quality requirements; "
        "do not skip them merely because the task seems familiar. Relevance is to your current responsibility, "
        "not every subject mentioned in an assignment you delegate. Load further references when needed.\n"
        + ("Report defects in loaded skills and use skill_manage for justified corrections through its approval "
           "and validation gates. After a reusable discovery, offer to save it as a skill rather than forcing a "
           "skill change for every completed task.\n" if can_manage else
           "Skills are read-only in this session. Report useful corrections to the coordinator instead of editing the skill tree.\n")
        +
        "\n"
        "<available_skills>\n"
        + "\n".join(index_lines) + "\n"
        "</available_skills>"
        + hidden_note
    )
