"""Small Research-only additions to the existing system-prompt assembly."""
from misaka.config.identity import (
    COORDINATOR_APPROVAL,
    COORDINATOR_RECEIPTS,
)
from misaka.core.system_prompt import CURRENT_TOOLS_GUIDELINE

RESEARCH_LO_ORCHESTRATION = """[Research orchestration]
In Research mode, your primary responsibility is to orchestrate and advance research, not to answer the research
question prematurely. Until the research and its required review are complete, do not deliver an answer to the research
question to the user or substitute your own immediate judgement for unfinished research.

Actively identify, expand and enumerate prerequisite questions, hidden premises, potential subquestions, and
follow-up questions that emerge during research. Make their relationships, dependencies and relevance to the original
question explicit. Give every major or minor question included in the plan a targeted Sister assignment matched to
her expertise, with clear evidence needs, deliverables and acceptance criteria, rather than a generic request to
collect material.

The same Sister may take multiple distinct tasks, each in its own independent session. Independent tasks may run
concurrently within the existing concurrency and budget limits; dependent tasks run after their prerequisites.
Continually revise the research orchestration in light of returned evidence and unresolved questions, while respecting
the agreed research scope and execution limits.

Still produce internal node conclusions, syntheses and report drafts when the current phase requires them for further
research and independent review. Clearly identify these as provisional working products, not answers delivered to
the user before the research is complete.
"""


def system_context(session, names, system_prompt, *, sister_id=None):
    """Only the Research delta; the ordinary loader owns identity and duties.

    SYSTEM.md can replace the default tool guidance, so preserve required current-tool
    and navigation rules without rebuilding the role base or rewriting custom text.
    """
    if sister_id is None:
        sections = [RESEARCH_LO_ORCHESTRATION, COORDINATOR_APPROVAL, COORDINATOR_RECEIPTS]
    else:
        from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE
        sections = [RESEARCH_SISTER_DISCIPLINE]
    sections.append(CURRENT_TOOLS_GUIDELINE)
    for name in dict.fromkeys(names):
        if name not in {"misaka_research_view", "coverage_scan"}:
            continue
        definition = session.getToolDefinition(name)
        sections.extend(getattr(definition, "promptGuidelines", ()) or ())
    # Exact owned strings only: no fuzzy deletion or rewriting of custom text.
    return "\n".join(section for section in dict.fromkeys(sections) if section not in system_prompt)
