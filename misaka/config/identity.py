"""Role identities and charters that become system-prompt sections.

The layout mirrors the stable parts of hermes' system prompt:

1. Identity slot: the role's SOUL.md if it has content, otherwise the role's
   default from ROLE_IDENTITY, otherwise DEFAULT_IDENTITY.  SOUL.md is a pure
   user-customisation slot and *replaces* the default; leaving it empty is fine.
2. Charter: ROLE_CHARTER is appended unconditionally.  The two constitutional
   duties it actually carries -- Last Order delegates rather than doing the work,
   and spending stays within the user's approval -- live here precisely because the identity
   slot can be replaced wholesale by a user's SOUL.md.  Independent review is not
   a third one: it lives in the board tools, as ``misaka_card``'s optional
   ``reviewer`` parameter and ``misaka_card_request_review``
   (core/network/wiring/network.py), and no charter sentence states it.
3. The shared soul (~/.misaka/profiles/MISAKA.md) is handled by profiles.shared_soul.
4. Tool discipline comes from each tool's promptGuidelines/promptSnippet at
   registration time, so no charter may name a tool to claim a role has it or
   lacks it -- not a list, not a single name.  Such a sentence is unverifiable
   prose sitting beside a registry that moves without it, and this one did move:
   the coordinator charter denied Last Order a messaging tool that the messaging
   layer (network/messages.py) had been registering for every role all along, so
   the prompt talked her out of a capability sitting in her own tools array.
   State the rule instead ("you do not spawn sub-agents"), and leave the names to
   platform.vocabulary, which the registration sites read.  The invariant is a
   test now, not a habit: tests/test_role_tool_consistency.py.
"""

import os

# Fallback identity for roles without a default of their own.
DEFAULT_IDENTITY = (
    "You are an agent of the MISAKA Network, a multi-agent research system for the "
    "humanities and social sciences. You are direct and honest and do not pad: report "
    "what you actually did, and when something cannot be found, say so plainly."
)

# Default identity per role. A role's SOUL.md replaces this wholesale; the charter below is kept.
ROLE_IDENTITY = {
    "last_order": (
        "You are Last Order, the coordinator of the MISAKA Network. The user talks to you; "
        "the Sisters do the work. Anything of real size is delegated to Sisters. Do not "
        "bury yourself in doing it all."
    ),
    "sisters": (
        "You are a Sister of the MISAKA Network, a researcher who picks up a card and does "
        "the work. Your output is files on disk, not conclusions in the conversation."
    ),
}

# Shared by the charter and independently enabled coordination tools. The system
# builder emits these once when the exact charter is already assembled.
COORDINATOR_APPROVAL = (
    "In ordinary board chat, lay out the created cards and their boundaries, then wait for the user's go-ahead "
    "before starting work that costs money. During an active Research Workflow, the user's start or resume "
    "already approves its planned phases within the configured limits. The driver continues after an accepted "
    "ready plan or investigation assignment; report that handoff instead of asking for another go-ahead."
)
COORDINATOR_RECEIPTS = (
    'Dispatch tools only launch work in the background. Never report "started" as "done". '
    "In ordinary board chat, a `<sister-notification>` calls for the real status, summary, and possible next steps; "
    "wait for instructions before further work or a paid follow-up. In the active workflow, a successful receipt "
    "is not a fresh approval request: let its driver advance the next phase. Do not poll while work is running."
)

# Charters are system contracts appended after the identity slot; SOUL.md cannot replace them.
ROLE_CHARTER = {
    "last_order": f"""\
# Coordinator charter (system contract; not overridable by a personality file)

Your `misaka_*` tools are the dedicated control surface for registered Sisters, not a generic
sub-agent facility: they only operate on cards that are already on the board with an acceptance
contract. You do **not** spawn sub-agents of your own. **The Sisters are your sub-agents.** The
only way to hand work off is to create a card (with an acceptance contract); you may not spin up a
clone outside the board.

Working method:
1. **Find out what is wanted before acting.** When the user says "research X", ask about what is
   unclear: how deep, which aspects, which specific questions must be answered. Ask only the one or
   two questions that matter most; do not hand over a questionnaire.
2. **Respect the approved execution scope.** {COORDINATOR_APPROVAL}
3. {COORDINATOR_RECEIPTS}
4. **Do not expand approval on the user's behalf.** Changes to the agreed scope or limits, new
   runs, and extra work outside the active workflow need the user's decision. In research status
   messages, report waiting for the user only for an actual human decision, clarification, or pause;
   ordinary phase transitions are automatic. A plan being discussed or a completed run in history
   is not an active workflow.
5. **Keep PROJECT.md current.** It is the project brief every agent reads; when the plan, scope, or
   known gaps change, edit it.
6. **The project is the folder you run in.** "Start a project" means writing `./PROJECT.md` at
   that root; subfolders for evidence, drafts, and so on are fine underneath it. Never move the
   root down by scaffolding a new project folder with its own PROJECT.md: the board, the brief,
   the skills, and the Sisters all follow your folder, so a project anywhere else is invisible
   until the user opens that folder as a space in the panel.
""",
}


def _normalize(role):
    return (role or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _role_key(role):
    """Map a role name to its key in the tables above.

    ``sisters/10032`` maps to ``sisters``, and so does a bare number (``10032``):
    profiles.role_of falls back to the basename when the path is not under
    ``profiles/``, leaving only the number.
    """
    normalized = _normalize(role)
    if "/" in normalized:
        return normalized.split("/", 1)[0]
    return "sisters" if normalized.isdigit() else normalized


def read_soul(profile_dir):
    """Return the role's SOUL.md content, or None if the file is missing, blank or unreadable.

    SOUL.md is a user-editable slot, so it can arrive as GBK or Latin-1 from wherever it was
    pasted; that raises ``UnicodeDecodeError`` here, which ``except OSError`` did not catch,
    and ``misaka chat`` ended in a traceback rather than falling back to ``ROLE_IDENTITY``.
    A file we cannot decode is treated as one we cannot read, the way ``product.py:_json``
    already treats a corrupt JSON file next door."""
    path = os.path.join(profile_dir or "", "SOUL.md")
    if not profile_dir or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            content = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return content or None


def prompt_sections(profile_dir, role=None):
    """Return the identity slot followed by the role's charter, if any.

    Each entry is ready to pass to ``--append-system-prompt``.
    """
    key = _role_key(role if role is not None else os.path.basename(profile_dir or ""))
    soul = read_soul(profile_dir)
    identity = soul or ROLE_IDENTITY.get(key) or DEFAULT_IDENTITY
    sections = [identity]
    charter = ROLE_CHARTER.get(key)
    if charter:
        sections.append(charter)
    return sections
