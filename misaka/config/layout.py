"""The one place that knows what ``~/.misaka`` looks like, and makes it so.

Most of the tree appears on its own: the boards open their SQLite files, the panel writes
its snapshot, the credential store creates ``agent/auth.json``, ``allies.json`` is seeded
the first time the daemon reads it. What never appeared is the half a *person* edits --
the profiles tree -- because every writer there creates only its own leaf:
``roster.create_sister`` builds a Sister when asked, ``profiles.shared_soul`` seeds
``MISAKA.md`` when something reads it, and nothing at all owned Last Order's own folder.

So a fresh install had no ``profiles/last_order/``: no ``config.yaml`` to declare her MCP
servers in, no ``skills/`` to drop a skill into, no shared ``profiles/skills/`` either.
The features were not missing, their doors were -- and a directory a user cannot see is a
feature they cannot find.

``ensure`` is that door, and it runs at every process entry (``cli.app.main``). It is
idempotent, it never overwrites, and it never raises: a read-only home is a reason to run
with less, not a reason not to start.

What is deliberately NOT created:

* ``SOUL.md`` for any role. It is an optional personality slot. Shared and role duties
  are assembled separately (``config.identity``); the README explains customisation.
* ``agent/models.json``. pi does not create it either, and an empty ``providers`` map is
  not more useful than its absence; the README says what it is for.
"""

import os

README = """# MISAKA profiles

One directory per role, the way pi keeps user data: personality, skills and MCP servers
live here, never in the source tree.

    MISAKA.md          identity every role loads before its own SOUL.md
    last_order/        Last Order, the coordinator
      SOUL.md          her voice -- OPTIONAL; shared duties and role charter are kept
      config.json      {"model": "..."} the model she starts on; /model Ctrl+S writes it
      config.yaml      her MCP servers (a commented skeleton is there to edit)
      skills/          skills only she sees
    sisters/<id>/      one per Sister, same layout (`misaka create <id>` builds it)
    skills/            skills every role sees

Skills resolve project -> role -> shared -> external, first match winning. The project
layer is a `skills/` folder in whatever directory MISAKA runs in.

Providers and credentials are engine-side, under `~/.misaka/agent/`: `/login` writes
`auth.json`, `/model` writes `settings.json`, and `models.json` is where you declare a
provider the built-in catalog does not carry (a local proxy, a private gateway).
"""

CONFIG_YAML = """# Last Order · MCP servers
#
# Every server listed here is started for Last Order, and its tools are registered as
# mcp__<server>__<tool>. To enable one, delete the leading '#' from the block below.
#
# mcp_servers:
#   camofox:                      # the <server> half of the tool name
#     command: npx
#     args: ["-y", "camofox-mcp"]
#     env:
#       CAMOFOX_HEADLESS: "1"
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


def ensure(roles_root=None):
    """Create the parts of ``~/.misaka`` a person edits. Returns the paths it made.

    Never overwrites, never raises. Callers get the list for a first-run message; nobody
    has to check it, because a tree that is already right produces an empty one.
    """
    from misaka.config import CFG

    root = os.path.expanduser(roles_root or CFG["roles_root"])
    made: list[str] = []
    try:
        _mkdir(root, made)
        _seed(os.path.join(root, "README.md"), README, made)
        # The shared identity every role loads before its own SOUL.md. Seeded from the
        # template rather than through ``profiles.shared_soul``, which resolves the root
        # from CFG: with a root passed in, that would seed somewhere else entirely.
        from misaka.config import profiles
        _seed(os.path.join(root, "MISAKA.md"), profiles.SHARED_SOUL_TEMPLATE, made)
        shared_skills = os.path.join(root, "skills")
        if _mkdir(shared_skills, made):
            _seed(os.path.join(shared_skills, "README.md"),
                  SKILLS_README.format(who="every role"), made)
        # Sisters are created on demand, but the folder they go in is part of the layout:
        # `misaka sister list` on a fresh install should read empty, not missing.
        _mkdir(os.path.join(root, "sisters"), made)
        last_order = os.path.join(root, "last_order")
        _mkdir(last_order, made)
        _seed(os.path.join(last_order, "config.yaml"), CONFIG_YAML, made)
        role_skills = os.path.join(last_order, "skills")
        if _mkdir(role_skills, made):
            _seed(os.path.join(role_skills, "README.md"),
                  SKILLS_README.format(who="Last Order only"), made)
    except OSError:
        # A read-only or full home: every reader of this tree already treats a missing
        # directory as "no entries", so the session still starts.
        pass
    return made


__all__ = ["ensure"]
