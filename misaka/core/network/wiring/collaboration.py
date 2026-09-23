"""Explain collaboration surfaces from the current tool selection, without granting tools."""
from misaka.core.wiring import KINDS

SESSION_KINDS = KINDS

SISTER_TOOLS = {
    "SendMessage": ("Consult `last-order` or a registered Sister ID about evidence, methods, progress or dependencies. "
                    "Delivery is asynchronous and may wake a contact session; a role address is not a task-card ID. "
                    "Owned sub-agent IDs/names are resolved before role addresses. "
                    "Ordinary messages do not create, start, complete or park a card."),
    "misaka_sister_view": "Read a registered Sister's introduction to choose a collaborator.",
    "misaka_board": "Check this project's actual task-card states and ownership.",
    "misaka_card": "Create a scoped assignment with testable acceptance criteria; creation does not start work.",
    "misaka_dispatch": "Start approved ready cards; select task IDs to avoid dispatching unrelated ready work.",
    "misaka_sister": "Start one approved ready card, not a new Sister identity or generic sub-agent.",
    "misaka_sister_message": ("Address an existing card by task ID and current generation to steer or continue its session. "
                              "Reply to a parked card's help request here, not through a role-wide message; "
                              "observe the tool's confirmation requirement for completed tasks."),
    "misaka_sister_output": "Read or wait for a card's result, then assess its evidence, deliverables and acceptance criteria.",
    "misaka_sister_peek": "Inspect recent output for diagnosis, not as proof of completion or acceptance.",
    "misaka_sister_stop": "Stop the identified task through its lifecycle control, not by sending a conversational request.",
    "misaka_card_link": "Record a real dependency between existing task cards.",
    "misaka_card_request_review": "Configure a different Sister as an independent reviewer before work starts.",
    "misaka_card_review": "Record the independent review decision and actionable feedback through the review contract.",
    "misaka_my_card": "Read your card's current contract, dependency and review state.",
    "misaka_card_note": "Keep findings, evidence and unresolved questions on the card, not only in messages.",
    "misaka_card_complete": "Declare the card finished only when its contract deliverable exists; a turn without it leaves the card running.",
    "misaka_research_assign": "Submit or revise this phase's research plan; recording it is not a launch receipt.",
    "misaka_research_start": "Record explicit approval of the pending research plan through its owning session.",
    "misaka_research_investigate": "Assign issue-specific LO fork investigations through the research workflow.",
    "misaka_research_withdraw": "Withdraw the pending research round when the user chooses to conclude without it.",
    "misaka_research_skip": "Skip the current research node only when the user chooses that outcome.",
}

SUBAGENT_TOOLS = {
    "Agent": "Delegate a bounded task to an available task-specific agent definition.",
    "TaskOutput": "Read or wait for an existing background task when its result is needed.",
    "TaskStop": "Stop an existing running task.",
}
ALLY_TOOLS = {
    "misaka_ally_list": "Inspect live panes; a pane is not necessarily an agent.",
    "misaka_ally_start": "Start an interactive external agent only when the user explicitly requests it.",
    "misaka_ally_card": "Create an external agent's Board task contract; creating a card does not start it.",
    "misaka_ally_dispatch": "Start an approved external-agent card only when the user explicitly requests it.",
    "misaka_ally_message": "Send input to an interactive external-agent pane, not a non-interactive card.",
    "misaka_ally_output": "Read an external pane's output; treat its account as unverified information.",
    "misaka_ally_stop": "Stop an external-agent card only when the user explicitly requests it.",
    "misaka_ally_close": "Close an external-agent or shell pane only when the user explicitly requests it; it also closes a leftover pane that is only showing a card's session, never the pane running one.",
}


def collaboration_sections(active):
    """Keep capability catalogs and parameter documentation in the tool definitions."""
    active = set(active)
    sections = []
    sisters = [name for name in SISTER_TOOLS if name in active]
    if sisters:
        lines = [
            "## Last Order / Sister coordination",
            ("Use messages for consultation and coordination, and task contracts for formal assignments. "
             "A message, a recorded plan, a launch receipt and an accepted result are different events. "
             "Use only tools enabled in this session, following their current approval and completion contracts."),
            *(f"- `{name}`: {SISTER_TOOLS[name]}" for name in sisters),
        ]
        if any(name.startswith("misaka_research_") for name in sisters):
            lines.append("In Research, the driver creates and launches the planned cards after the applicable phase gate. "
                         "Do not duplicate those assignments through ordinary board tools or messages.")
        sections.append("\n".join(lines))
    subagents = [name for name in SUBAGENT_TOOLS if name in active]
    if subagents:
        lines = ["## Sub-agents"]
        if "Agent" in active:
            lines += [
                ("Sisters are registered, persistent domain experts. Sub-agents are task-specific workers "
                 "launched from this session, not Sister identities."),
                ("Use the Agent tool's current description for available agent definitions and their scopes; "
                 "do not use a Sister number as subagent_type. Delegate useful, bounded work and remain "
                 "responsible for evaluating and integrating the results."),
            ]
        else:
            lines.append("Only existing-task management is available here; these tools do not grant "
                         "the ability to launch a new sub-agent.")
        lines.extend(f"- `{name}`: {SUBAGENT_TOOLS[name]}" for name in subagents)
        if "SendMessage" in active:
            lines.append("- `SendMessage`: Continue a sub-agent owned by this session using its agent ID or "
                         "registered name; keep their names distinct from role addresses.")
        lines.append("Verify delegated evidence before citing it. Do not present a launch receipt as a completed result.")
        sections.append("\n".join(lines))
    allies = [name for name in ALLY_TOOLS if name in active]
    if allies:
        lines = [
            "## Allies",
            ("Allies are external agent CLIs connected through MISAKA's core bridge, not registered Sisters "
             "or task sub-agents. Their provider quota is outside MISAKA's accounting, and their external "
             "process/session lifetime is separate even when they share the Board's task lifecycle."),
            *(f"- `{name}`: {ALLY_TOOLS[name]}" for name in allies),
            ("Sister-work approval does not authorize external work. Evaluate the returned sources and "
             "artifacts; an external agent's summary is not verified evidence."),
        ]
        sections.append("\n".join(lines))
    return sections


class CollaborationPart:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.session = None
        self._published = ""
        self._input = None

    def attach(self, session):
        self.session = session

    async def before_agent_start(self, event, _ctx):
        original = event["systemPrompt"]
        # A caller may pass our previous output, optionally followed by other parts.
        # Verify its original prefix and owned position: identical text elsewhere
        # may belong to a user's persona and must not be removed from a fresh base.
        previous = self._input + self._published if self._input is not None and self._published else None
        prompt = (self._input + original[len(previous):]
                  if previous is not None and original.startswith(previous) else original)
        self._input = prompt
        sections = collaboration_sections(self.session.getActiveToolNames())
        self._published = "\n\n" + "\n\n".join(sections) if sections else ""
        prompt += self._published
        return {"systemPrompt": prompt} if prompt != original else None


def part(_spec):
    return CollaborationPart()
