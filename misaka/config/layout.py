"""Seeds the half of the home a person edits. (Where everything lives is ``config.home``.)

Most of the tree appears on its own: the boards open their SQLite files, the panel writes
its snapshot, the credential store creates ``auth.json``. What never appeared is the half a *person* edits --
the profiles tree -- because every writer there creates only its own leaf:
``roster.create_sister`` builds a Sister when asked, ``profiles.shared_soul`` seeds
``MISAKA.md`` when something reads it, and nothing at all owned Last Order's own folder.

So a fresh install had no ``profiles/last_order/``: no ``skills/`` to drop a skill into,
no shared ``skills/`` either.
The features were not missing, their doors were -- and a directory a user cannot see is a
feature they cannot find.

``ensure`` is that door, and it runs at every process entry (``cli.app.main``). It is
idempotent, it never overwrites, and it never raises: a read-only home is a reason to run
with less, not a reason not to start.

What is deliberately NOT created:

* ``SOUL.md`` for any role. It is an optional personality slot. Shared and role duties
  are assembled separately (``config.identity``); the README explains customisation.
* ``models.json``. pi does not create it either, and an empty ``providers`` map is
  not more useful than its absence; the README says what it is for.
"""

import os

README = """# MISAKA profiles

One directory per role: personality, skills, sub-agent types and MCP servers live here, never
in the source tree. A role directory overlays the home above it -- what a role does not
define, it shares with every other role.

    last_order/        Last Order, the coordinator
      SOUL.md          her voice -- OPTIONAL; shared duties and role charter are kept
      settings.json    what she holds of her own: "defaultProvider"/"defaultModel" (the model
                       she starts on; /model Ctrl+S writes it), "mcpServers", "web"
      skills/          skills only she sees
      subagents/       sub-agent types only she sees
    sisters/<id>/      one per Sister, same layout (`misaka create <id>` builds it)

In the home, shared by every role:

    ../MISAKA.md       identity every role loads before its own SOUL.md
    ../settings.json   every other setting, and the fallback model
    ../skills/         skills every role sees
    ../subagents/      sub-agent types every role sees

An MCP server entry has the shape Hermes uses:

    "mcpServers": {"camofox": {"command": "npx", "args": ["-y", "camofox-mcp"]}}

Skills resolve project -> role -> shared -> external, first match winning. The project
layer is a `skills/` folder in whatever directory MISAKA runs in.

Providers and credentials are shared by every role too: `/login` writes
`../credentials/auth.json`, `/model` writes `../settings.json`, and `../models.json` is where you
declare a provider the built-in catalog does not carry (a local proxy, a private gateway).
"""

SHARED_README = """# shared

The one place inside the MISAKA home where Last Order and the Sisters may create things of
their own: material several cards need (a downloaded corpus, a skill library being assembled,
an audit trail). Everything else in the home belongs to the program; a card's own work belongs
in its project folder.

Nothing here is read by MISAKA itself. It is yours to tidy or delete.
"""

SKILLS_README = """Drop a skill here as `<name>/SKILL.md` with YAML front matter:

    ---
    name: release-notes
    description: What this skill is for. The model reads this line to decide to open it.
    ---

    The instructions themselves.

Skills in this folder are seen by {who}.
"""


def _mkdir(path, made):
    if os.path.isdir(path):
        return False
    os.makedirs(path, exist_ok=True)
    made.append(path)
    return True


def _seed(path, text, made):
    if os.path.exists(path):
        return False
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)
    made.append(path)
    return True


def ensure():
    """Create the parts of the home a person edits. Returns the paths it made.

    Never overwrites, never raises. Callers get the list for a first-run message; nobody
    has to check it, because a tree that is already right produces an empty one.
    """
    from misaka.config import home, profiles

    made: list[str] = []
    try:
        # The shared layer, at the root of the home: the identity every role loads before
        # its own SOUL.md, and the skills every role sees.
        _mkdir(str(home.home()), made)
        _seed(str(home.path("shared_soul")), profiles.SHARED_SOUL_TEMPLATE, made)
        if _mkdir(str(home.path("shared_skills")), made):
            _seed(str(home.path("shared_skills") / "README.md"), SKILLS_README.format(who="every role"), made)
        if _mkdir(str(home.path("shared")), made):
            _seed(str(home.path("shared") / "README.md"), SHARED_README, made)
        # The roles. Sisters are created on demand, but the folder they go in is part of the
        # layout: `misaka sister list` on a fresh install should read empty, not missing.
        roles = home.path("roles_root")
        _mkdir(str(roles), made)
        _seed(str(roles / "README.md"), README, made)
        _mkdir(str(home.path("profiles_root")), made)
        last_order = roles / "last_order"
        _mkdir(str(last_order), made)
        if _mkdir(str(last_order / "skills"), made):
            _seed(str(last_order / "skills" / "README.md"), SKILLS_README.format(who="Last Order only"), made)
    except OSError:
        # A read-only or full home: every reader of this tree already treats a missing
        # directory as "no entries", so the session still starts.
        pass
    return made


__all__ = ["ensure"]
