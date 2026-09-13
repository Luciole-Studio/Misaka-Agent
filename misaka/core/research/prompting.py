"""Small Research-only additions to the existing system-prompt assembly."""
from misaka.config.identity import (
    COORDINATOR_APPROVAL,
    COORDINATOR_RECEIPTS,
    ROLE_CHARTER,
)
from misaka.core.system_prompt import CURRENT_TOOLS_GUIDELINE


def system_context(session, names, system_prompt):
    """Bare/custom prompts get missing common rules, not a second whole prompt.

    The normal builder already includes tool guidelines. SYSTEM.md intentionally
    replaces that builder output, so Research supplies the required navigation
    and coverage rules from their actual tool definitions in the coordinator's
    system-prompt publisher.
    """
    sections = [CURRENT_TOOLS_GUIDELINE, COORDINATOR_APPROVAL, COORDINATOR_RECEIPTS]
    if ROLE_CHARTER["last_order"] not in system_prompt:
        sections.append("Coordinator role: Last Order.")
    for name in dict.fromkeys(names):
        if name not in {"misaka_research_view", "coverage_scan"}:
            continue
        definition = session.getToolDefinition(name)
        sections.extend(getattr(definition, "promptGuidelines", ()) or ())
    # Exact owned strings only: no fuzzy deletion or rewriting of custom text.
    return "\n".join(section for section in dict.fromkeys(sections) if section not in system_prompt)
