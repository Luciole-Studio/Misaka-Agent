"""Create and remove durable Sister profiles."""
import os
import re
import shutil
import sys

from misaka.config import home, profiles
from misaka.config.product import CFG, current_config
from misaka.core.moments import CoreCommand

ROOT = CFG["profiles_root"]
ACTIVE = ("running", "review", "ready", "todo", "blocked", "triage")   # anything a Sister still owes


def model_choices():
    """The pinning menu: the global default, then what the sessions' own registry can run on the
    product provider (builtin catalog plus models.json), then a free-form ID."""
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry
    registry = ModelRegistry.create(AuthStorage.create())
    provider = current_config()["provider"]
    mine = sorted({m.id for m in registry.getAvailable() if m.provider == provider})
    return ["default (use global setting)", *mine, "Custom…"]

SOUL_TEMPLATE = """# Misaka {sid}

You are Sister {sid} of the MISAKA Network. Describe her personality and voice here.
"""

DESCRIBE_TEMPLATE = """---
description: {specialty}
---
# Misaka {sid} · Capability profile

Last Order reads this file to decide which tasks fit this Sister. Put personality in `SOUL.md`.

## Areas of responsibility
- {specialty_line}

## Good and poor task fits
- Add concrete routing guidance here.
"""


def _valid(sid):
    return bool(re.fullmatch(r"[\w][\w.-]*", sid or "")) and sid not in {"last-order", "last_order"}


def roster_names(root=None):
    root = root or ROOT
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def card_counts(sid, db_path=None):
    """Count a Sister's task cards by status."""
    from misaka.config import CFG
    from misaka.core.platform import tasks as board_db
    path = os.path.expanduser(db_path or CFG["db"])
    if not os.path.exists(path):
        return {}
    con = board_db.connect(path)
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE assignee=? OR reviewer=? GROUP BY status",
            (sid, sid),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


def create_sister(sid, root=None, specialty=None, model=None):
    """Create a Sister profile and return ``(success, message)``."""
    root = root or ROOT
    if not _valid(sid):
        return False, f"Invalid Sister ID '{sid}'; use letters, numbers, '.', '_', or '-', but not last-order."
    prof = os.path.join(root, sid)
    if os.path.exists(prof):
        return False, f"Sister {sid} is already registered: {prof}"
    os.makedirs(os.path.join(prof, "skills"))
    specialty = (specialty or "").strip()
    with open(os.path.join(prof, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(SOUL_TEMPLATE.format(sid=sid))
    with open(os.path.join(prof, "DESCRIBE.md"), "w", encoding="utf-8") as f:
        f.write(DESCRIBE_TEMPLATE.format(
            sid=sid, specialty=specialty, specialty_line=specialty or "Not specified yet."))
    pinned = ""
    if model:
        profiles.persist_role_default_model(prof, model, strict=True)
        pinned = f"Pinned model: {model}. "
    return True, (
        f"Sister {sid} was added to the roster. {pinned}Profile: {prof} -- "
        f"DESCRIBE.md (what Last Order routes to her), SOUL.md (her voice), "
        f"settings.json (add it for a pinned model or \"mcpServers\"; /model Ctrl+S writes the pin), "
        f"skills/ (link skills here). Use /sister {sid} to switch to this Sister."
    )


def describe(sid, root=None):
    """Return the short description and body from DESCRIBE.md."""
    from misaka.utils.frontmatter import parse_frontmatter
    try:
        with open(os.path.join(root or ROOT, sid, "DESCRIBE.md"), encoding="utf-8") as f:
            parsed = parse_frontmatter(f.read())
    except OSError:
        return None, None
    except ValueError as error:
        # DESCRIBE.md is a file people are told to edit by hand, and parse_frontmatter raises
        # FrontmatterError (a ValueError) on malformed YAML. Every caller here walks the *whole*
        # roster -- the /sister menu, the misaka_sisters tool, the chat banner -- so letting one
        # member's typo out would take down the entire list. Name the file instead: whoever sees
        # the roster is the person who can fix it.
        return f"DESCRIBE.md could not be read ({' '.join(str(error).split())})", None
    desc = str(parsed.frontmatter.get("description") or "").strip()
    return desc or None, parsed.body.strip() or None


def describe_line(sid, root=None):
    """Return a concise roster description."""
    from misaka.core.skills.index import truncate_skill_description
    desc, _ = describe(sid, root)
    return truncate_skill_description(desc) if desc else None


def coordinator_profile(entry):
    """LO's routing summary, without mutating the internal capability catalog."""
    return {
        "id": entry["id"],
        "description": entry.get("description") or "",
        "profile_preview": (entry.get("profile") or "").strip()[:200],
    }


def routing_catalog(root=None):
    """Public routing summaries, without inspecting peers' tools, skills or settings."""
    root = os.path.expanduser(root or CFG["profiles_root"])
    out = []
    for sid in roster_names(root):
        if _valid(sid):
            description, body = describe(sid, root)
            out.append(coordinator_profile({"id": sid, "description": description, "profile": body}))
    return out


def capability_catalog(root=None, *, workspace=None, platform="cli"):
    """The coordinator's catalog, using the same layered Skill index as a Sister.

    These are configured capabilities, not a claim that every credential or
    script dependency is ready. No Sister runtime or extension is started to
    discover the catalog. Detailed profiles stay readable at profile_path.
    """
    from misaka.core.skills import index, layers, visibility

    root = os.path.expanduser(root or CFG["profiles_root"])
    out = []
    for sid in roster_names(root):
        if not _valid(sid):
            continue
        profile = os.path.join(root, sid)
        description, body = describe(sid, root)
        entries = index.runtime_build(layers.skill_roots(profile, cwd=workspace), platform=platform,
                                      detect=visibility.environment_detector(kind="card"))
        out.append({"id": sid, "description": description or "", "profile": body or "",
                    "profile_path": os.path.join(profile, "DESCRIBE.md"),
                    "skills": [{"name": e.get("runtime_name", e["name"]), "description": e["description"],
                                "path": e["path"], "layer": e["layer"]} for e in entries]})
    return out


def remove_sister(sid, root=None, db_path=None):
    """Remove a Sister profile unless it has active cards; return ``(success, message)``."""
    root = root or ROOT
    prof = os.path.join(root, sid)
    if not _valid(sid) or not os.path.isdir(prof):
        return False, f"Sister '{sid}' is not in the roster. Use /sister to list available Sisters."
    counts = card_counts(sid, db_path)
    live = {k: v for k, v in counts.items() if k in ACTIVE}
    if live:
        return False, f"Sister {sid} still has unfinished cards ({live}); finish, reassign or delete them before removal."
    shutil.rmtree(prof)
    rest = ','.join(f"{k}×{v}" for k, v in sorted(counts.items())) or 'none'
    return True, f"Sister {sid} was removed. Historical cards ({rest}), workspaces, and transcripts were preserved."


# ── TUI: /create and /remove (configuration wizard) ────────────────────────


def commands():
    """``/create`` and ``/remove`` as the part's commands."""
    async def create_cmd(args, ctx):
        sid = (args or "").strip()
        if not sid:
            sid = await ctx.ui.input("New Sister ID", "For example: 10033")
            sid = (sid or "").strip()
            if not sid:
                ctx.ui.notify("Creation cancelled: no Sister ID was entered.", "info")
                return
        if not _valid(sid) or os.path.exists(os.path.join(ROOT, sid)):
            ok, msg = create_sister(sid)
            ctx.ui.notify(msg, "error")
            return
        specialty = await ctx.ui.input(
            f"Describe Sister {sid}'s specialty for Last Order's task routing.",
            "For example: Finds and evaluates Soviet archival sources.")
        if specialty is None:
            ctx.ui.notify("Creation cancelled.", "info")
            return
        model_pick = await ctx.ui.select(f"Choose a model for Sister {sid}", model_choices())
        if model_pick is None:
            ctx.ui.notify("Creation cancelled.", "info")
            return
        model = None
        if model_pick == "Custom…":
            model = await ctx.ui.input("Model ID", "For example: claude-opus-4-5")
            if model is None:
                ctx.ui.notify("Creation cancelled.", "info")
                return
            model = model.strip() or None
        elif not model_pick.startswith("default"):
            model = model_pick
        summary = f"Specialty: {specialty.strip() or 'not specified'} | Model: {model or 'global default'}"
        if not await ctx.ui.confirm(f"Create Sister {sid}?", summary):
            ctx.ui.notify("Creation cancelled.", "info")
            return
        ok, msg = create_sister(sid, specialty=specialty, model=model)
        ctx.ui.notify(msg, "info" if ok else "error")

    async def remove_cmd(args, ctx):
        raw = (args or "").strip()
        force = raw.endswith("!")
        sid = raw.rstrip("!").strip()
        if not sid:
            names = roster_names()
            if not names:
                ctx.ui.notify('The roster is empty.', "info")
                return
            sid = await ctx.ui.select("Choose a Sister to remove", names)
            if not sid:
                return
        if sid == (os.environ.get("MISAKA_WHO") or "last-order"):
            ctx.ui.notify(
                f"Sister {sid} cannot remove its own active profile. Switch to Last Order first.",
                "error",
            )
            return
        if not force:
            counts = card_counts(sid)
            rest = ','.join(f"{k}×{v}" for k, v in sorted(counts.items())) or 'none'
            ok = await ctx.ui.confirm(
                f"Remove Sister {sid}?",
                f"Delete the profile and mounted skills. Historical cards ({rest}) and workspaces remain.")
            if not ok:
                return
        ok, msg = remove_sister(sid)
        ctx.ui.notify(msg, "info" if ok else "error")

    return [
        CoreCommand("create", f"Create a Sister profile with an ID, description, and model under {home.display(home.path('profiles_root'))}/.", create_cmd),
        CoreCommand("remove", "Remove a Sister profile; task history and workspaces remain, and active Sisters are protected.", remove_cmd),
    ]


class RosterPart:
    """Only Last Order grows or prunes the Sister roster: two commands, no tools."""

    def __init__(self):
        self.tools = []
        self.commands = commands()


# ── CLI: misaka create / misaka remove ──────────────────────────────


def cli_create(sid=None, desc=None, model=None, root=None):
    """Create a Sister interactively when optional fields are omitted."""
    interactive = sys.stdin.isatty()
    if not sid:
        if not interactive:
            print("Missing Sister ID: misaka create <id>")
            return 1
        sid = input("Sister ID (for example, 10033): ").strip()
        if not sid:
            print("Creation cancelled.")
            return 1
    if desc is None and interactive:
        desc = input("Specialty for Last Order's task routing (optional): ").strip()
    if model is None and interactive:
        model = input("Pinned model (blank uses the global default): ").strip()
    ok, msg = create_sister(sid, root=root, specialty=desc or None, model=model or None)
    print(msg)
    return 0 if ok else 1


def cli_remove(sid, yes=False, root=None, db_path=None):
    """Remove a Sister, asking for confirmation when interactive."""
    if not yes:
        counts = card_counts(sid, db_path)
        rest = ','.join(f"{k}×{v}" for k, v in sorted(counts.items())) or 'none'
        if not sys.stdin.isatty():
            print(f"Use --yes for non-interactive removal (Sister {sid} historical cards: {rest}).")
            return 1
        answer = input(
            f"Remove Sister {sid} and delete its profile? Historical cards ({rest}) remain. [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("Removal cancelled.")
            return 1
    ok, msg = remove_sister(sid, root=root, db_path=db_path)
    print(msg)
    return 0 if ok else 1
