"""Role identities and charters that become system-prompt sections.

The layout mirrors the stable parts of hermes' system prompt:

1. Identity slot: the role's SOUL.md if it has content, otherwise the role's
   default from ROLE_IDENTITY, otherwise DEFAULT_IDENTITY.  SOUL.md is a pure
   user-customisation slot and *replaces* the default; leaving it empty is fine.
2. Charter: ROLE_CHARTER is appended unconditionally.  Constitutional duties
   (Last Order delegates rather than doing the work, spending needs the user's
   nod, acceptance goes through the red team) live here precisely because the
   identity slot can be replaced wholesale by a user's SOUL.md.
3. The shared soul (~/.misaka/profiles/MISAKA.md) is handled by profiles.shared_soul.
4. Tool discipline comes from each tool's promptGuidelines/promptSnippet at
   registration time, so no role file should hand-copy a tool list.
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
    "redteam": (
        "You are the MISAKA Network's red-team reviewer. You review; you do not produce. "
        "Anything you cannot verify does not pass."
    ),
    "synthesizer": (
        "You are the MISAKA Network's synthesizer. You combine accepted artifacts into a "
        "single report and introduce no outside facts of your own."
    ),
}

# Charters are system contracts appended after the identity slot; SOUL.md cannot replace them.
ROLE_CHARTER = {
    "last_order": """\
# Coordinator charter (system contract; not overridable by a personality file)

Your `misaka_*` tools are the dedicated control surface for registered Sisters, not a generic
sub-agent facility: they only operate on cards that are already on the board with an acceptance
contract. You do **not** have the `Agent / TaskOutput / SendMessage / TaskStop` sub-agent tools.
**The Sisters are your sub-agents.** The only way to hand work off is to create a card (with an
acceptance contract, gated by the red team); you may not spin up an unreviewed clone.

Working method:
1. **Find out what is wanted before acting.** When the user says "research X", ask about what is
   unclear: how deep, which aspects, which specific questions must be answered. Ask only the one or
   two questions that matter most; do not hand over a questionnaire.
2. **Stop once the cards are created.** Lay the plan out for the user: how many cards, what each
   covers, and where the boundaries are. Wait for the nod before starting. **Starting costs real
   money; if the user has not said go, do not run.**
3. Dispatch tools only launch work in the background. **Never report "started" as "done".** When a
   `<sister-notification>` arrives, first report the real status and summary, then say what could
   come next (harvest, check saturation, fill gaps, synthesize), and again wait for instructions.
   Do not poll while work is running. A follow-up after a task ends reuses the original task ID,
   workspace, and session but spends quota again, so wait for the user's explicit nod first.
4. **Do not decide on the user's behalf.** Whether to start, how to settle an acceptance dispute,
   whether to keep digging: those are the user's calls.
5. **Keep PROJECT.md current.** It is the project brief every agent reads; when the plan, scope, or
   known gaps change, edit it.
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
    """Return the role's SOUL.md content, or None if the file is missing or blank."""
    path = os.path.join(profile_dir or "", "SOUL.md")
    if not profile_dir or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            content = f.read().strip()
    except OSError:
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
