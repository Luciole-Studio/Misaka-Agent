"""Explain collaboration surfaces from the current tool selection, without granting tools."""
from misaka.core.platform import owners
from misaka.core.wiring import KINDS

SESSION_KINDS = KINDS

SISTER_TOOLS = {
    "SendMessage": ("Consult `last-order` or a registered Sister ID about evidence, methods, progress or dependencies. "
                    "A name reaches the one session of that role in your space; with several there, address one "
                    "by its card or session id. To go back to the Sister who did a piece of work, address "
                    "her card or session id, never her name: a name may reach her contact session, which "
                    "holds none of that work. A finished card answers your message in her own conversation "
                    "and stays finished; a card that has not started gets it when it does. "
                    "Delivery is asynchronous and may wake a contact session. "
                    "Owned sub-agent IDs/names are resolved before those names. "
                    "Ordinary messages do not create, start, complete or park a card."),
    "misaka_sister_view": "Read a registered Sister's introduction to choose a collaborator.",
    "misaka_board": "Check this project's actual task-card states and ownership.",
    "misaka_card": "Create a scoped assignment with testable acceptance criteria; creation does not start work.",
    "misaka_dispatch": "Start approved ready cards; select task IDs to avoid dispatching unrelated ready work.",
    "misaka_sister": "Start one approved ready card, not a new Sister identity or generic sub-agent.",
    "misaka_sister_message": ("Address an existing card by task ID and current generation to steer or continue its session. "
                              "Reply to a parked card's help request here, not through SendMessage; "
                              "observe the tool's confirmation requirement for completed tasks."),
    "misaka_sister_output": "Read or wait for a card's result, then assess its evidence, deliverables and acceptance criteria.",
    "misaka_sister_peek": "Inspect recent output for diagnosis, not as proof of completion or acceptance.",
    "misaka_sister_stop": "Stop the identified task through its lifecycle control, not by sending a conversational request.",
    "misaka_pane_list": "See the panes in your tab of the panel and which of them may be closed.",
    "misaka_pane_close": ("Close what finished cards left in your tab; a pane running a card is stopped with "
                          "misaka_sister_stop, and a pane the user opened is closed only at their request."),
    "misaka_card_link": "Record a real dependency between existing task cards.",
    "misaka_card_request_review": "Configure a different Sister as an independent reviewer before work starts.",
    "misaka_card_review": "Record the independent review decision and actionable feedback through the review contract.",
    "misaka_my_card": "Read your card's current contract, dependency and review state.",
    "misaka_card_note": "Keep findings, evidence and unresolved questions on the card, not only in messages.",
    "misaka_card_complete": "Declare the card finished only when its contract deliverable exists; a turn without it leaves the card running.",
}

SUBAGENT_TOOLS = {
    "Agent": "Delegate a bounded task to an available task-specific agent definition.",
    "TaskOutput": "Read or wait for an existing background task when its result is needed.",
    "TaskStop": "Stop an existing running task.",
}


def collaboration_sections(active):
    """Keep capability catalogs and parameter documentation in the tool definitions."""
    active = set(active)
    sections = []
    # The board's own coordination tools, then those of the workflows that own cards.
    described = dict(SISTER_TOOLS)
    for owner in owners.registered():
        described.update(owner.tool_descriptions())
    sisters = [name for name in described if name in active]
    if sisters:
        lines = [
            "## Last Order / Sister coordination",
            ("Use messages for consultation and coordination, and task contracts for formal assignments. "
             "A message, a recorded plan, a launch receipt and an accepted result are different events. "
             "Use only tools enabled in this session, following their current approval and completion contracts."),
            *(f"- `{name}`: {described[name]}" for name in sisters),
        ]
        lines += [note for owner in owners.registered() if (note := owner.coordination_note(sisters))]
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
                         "registered name; keep their names distinct from role names.")
        lines.append("Verify delegated evidence before citing it. Do not present a launch receipt as a completed result.")
        sections.append("\n".join(lines))
    return sections


class CollaborationPart:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.session = None

    def attach(self, session):
        self.session = session

    async def before_agent_start(self, event, _ctx):
        # A section of the run's prompt, as pi's extensions add theirs (see SisterCapabilitiesPart).
        sections = event["systemPromptOptions"]["sections"]
        text = "\n\n".join(collaboration_sections(self.session.getActiveToolNames()))
        if text:
            sections["collaboration"] = text
        else:
            sections.pop("collaboration", None)


def part(_spec):
    return CollaborationPart()
