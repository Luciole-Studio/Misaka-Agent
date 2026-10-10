"""Create and remove durable Sister profiles."""
import json
import os
import re
import shutil
import sys

from misaka.config import home, profiles
from misaka.config.product import CFG, current_config
from misaka.core.moments import CoreCommand

ROOT = CFG["profiles_root"]
ACTIVE = ("running", "review", "ready", "todo", "blocked", "triage")   # anything a Sister still owes
DEFAULT_CHOICE = "default (use global setting)"
CUSTOM_CHOICE = "Custom…"
PIN_EXAMPLE = "anthropic/claude-opus-5"


def _registry():
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry
    return ModelRegistry.create(AuthStorage.create())


def model_choices():
    """The pinning menu: the global default, then what the sessions' own registry can run on the
    configured providers (builtin catalog plus models.json) as ``provider/id`` references -- the
    shape a pin is stored in -- then a free-form reference."""
    mine = sorted({f"{m.provider}/{m.id}" for m in _registry().getAvailable()})
    return [DEFAULT_CHOICE, *mine, CUSTOM_CHOICE]


def resolve_pin(model):
    """The canonical ``provider/id`` for a pin the user typed or picked.

    A bare ID resolves on the product provider. A raw ID that itself contains slashes
    (``anthropic/claude-opus-4`` on a gateway) resolves through the catalog rather than being
    split at its first slash: the segment before the slash is usually a vendor name that is
    also a real provider, so splitting would silently pin the model to that provider's
    account. Raises ``ValueError`` naming the candidates when the reference is ambiguous,
    or when it is unknown.
    """
    return profiles.resolve_model_reference(model, _registry(), fallback_provider=current_config()["provider"])

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


def create_sister(sid, root=None, specialty=None, model=None, thinking=None):
    """Create a Sister profile and return ``(success, message)``."""
    root = root or ROOT
    if not _valid(sid):
        return False, f"Invalid Sister ID '{sid}'; use letters, numbers, '.', '_', or '-', but not last-order."
    prof = os.path.join(root, sid)
    if os.path.exists(prof):
        return False, f"Sister {sid} is already registered: {prof}"
    pin = None
    if model:
        # Resolve before anything is written: a bad reference must not leave a half-made
        # profile behind that then reports the ID as already registered.
        try:
            pin = resolve_pin(model)
        except ValueError as error:
            return False, f"Sister {sid} was not created: {error}"
    if thinking:
        from misaka.cli.args import VALID_THINKING_LEVELS
        if thinking not in VALID_THINKING_LEVELS:
            return False, (f"Sister {sid} was not created: unknown thinking level {thinking!r} "
                           f"(choose {', '.join(VALID_THINKING_LEVELS)})")
    os.makedirs(os.path.join(prof, "skills"))
    specialty = (specialty or "").strip()
    with open(os.path.join(prof, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(SOUL_TEMPLATE.format(sid=sid))
    with open(os.path.join(prof, "DESCRIBE.md"), "w", encoding="utf-8") as f:
        # Quoted as a JSON string, which YAML reads as a double-quoted scalar: a plain one ends
        # at the first ": ", so "Economic history: late imperial China" wrote a profile nobody
        # could read, and the Sister dropped out of every plan while create reported success.
        f.write(DESCRIBE_TEMPLATE.format(
            sid=sid, specialty=json.dumps(specialty, ensure_ascii=False),
            specialty_line=specialty or "Not specified yet."))
    written, _body = describe(sid, root=os.path.dirname(prof))
    if (written or "") != specialty:
        shutil.rmtree(prof, ignore_errors=True)
        return False, (f"Sister {sid} was not created: her DESCRIBE.md did not read back "
                       f"({written or 'no description'}).")
    pinned = ""
    if pin:
        profiles.persist_role_default_model(prof, pin, strict=True)
        pinned = f"Pinned model: {pin}. "
    if thinking:
        profiles.persist_role_thinking_level(prof, thinking)
        pinned += f"Default thinking level: {thinking}. "
    return True, (
        f"Sister {sid} was added to the roster. {pinned}Profile: {prof} -- "
        f"DESCRIBE.md (what Last Order routes to her), SOUL.md (her voice), "
        f"settings.json (her pinned model, her default thinking level, \"mcpServers\"; /model and "
        f"/thinking with Ctrl+S in her window write them), "
        f"skills/ (link skills here). Use /sister {sid} to switch to this Sister."
    )


def _read_by_hand(path):
    """``(description, body)`` of a DESCRIBE.md whose frontmatter YAML could not parse, read as
    plain text: the ``description:`` line, and everything after the closing ``---``. 0.18.7 kept
    the description and dropped the body, so Last Order routed the Sister without her profile."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().split("\n")
    except OSError:
        return None, None
    if not lines or lines[0].strip() != "---":
        return None, None
    description = None
    for number, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return description, "\n".join(lines[number + 1:]).strip() or None
        if description is None and line.startswith("description:"):
            value = line[len("description:"):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            description = value or None
    return None, None


def describe(sid, root=None):
    """Return the short description and body from DESCRIBE.md."""
    from misaka.utils.frontmatter import parse_frontmatter
    path = os.path.join(root or ROOT, sid, "DESCRIBE.md")
    try:
        with open(path, encoding="utf-8-sig") as f:
            parsed = parse_frontmatter(f.read())
    except OSError:
        return None, None
    except ValueError as error:
        # DESCRIBE.md is a file people are told to edit by hand, and parse_frontmatter raises
        # FrontmatterError (a ValueError) on malformed YAML. Every caller here walks the *whole*
        # roster -- the /sister menu, misaka_board's roster line, the chat banner -- so letting
        # one member's typo out would take down the entire list. The description line alone is
        # read as written first: `misaka create --desc` wrote it unquoted before 0.18.7, so any
        # description with a colon in it is in exactly this state. Only when that line is not
        # there either is the file named: whoever sees the roster is the person who can fix it.
        description, body = _read_by_hand(path)
        return description or f"DESCRIBE.md could not be read ({' '.join(str(error).split())})", body
    desc = str(parsed.frontmatter.get("description") or "").strip()
    return desc or None, parsed.body.strip() or None


def describe_line(sid, root=None):
    """Return a concise roster description."""
    from misaka.core.skills.index import truncate_skill_description
    desc, _ = describe(sid, root)
    return truncate_skill_description(desc) if desc else None


def allies():
    """The allies this home enables that can start on this machine, by name (``ally.presets``)."""
    from misaka.core.network.ally import presets
    return {name: ally for name, ally in presets.configured().items() if presets.unavailable(ally) is None}


def executors(root=None):
    """Who a card may be assigned to: the Sisters, and the usable allies."""
    return set(roster_names(root)) | set(allies())


def _ally_entry(ally):
    return {"id": ally.name, "profile": "",
            "description": f"Ally -- an external agent misaka drives over ACP, not a Sister: {ally.description}"}


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
    return out + [coordinator_profile(_ally_entry(ally)) for ally in allies().values()]


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
    # An ally brings its own skills and tools; misaka has no profile or skill stack to show.
    return out + [{**_ally_entry(ally), "profile_path": None, "skills": []} for ally in allies().values()]


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
        if model_pick == CUSTOM_CHOICE:
            model = await ctx.ui.input("Model (provider/model)", f"For example: {PIN_EXAMPLE}")
            if model is None:
                ctx.ui.notify("Creation cancelled.", "info")
                return
            model = model.strip() or None
        elif model_pick != DEFAULT_CHOICE:
            model = model_pick
        if model:
            # Resolve now so the confirmation shows the provider the pin actually lands on.
            try:
                model = resolve_pin(model)
            except ValueError as error:
                ctx.ui.notify(f"Sister {sid} was not created: {error}", "error")
                return
        from misaka.cli.args import VALID_THINKING_LEVELS
        thinking_pick = await ctx.ui.select(f"Choose Sister {sid}'s default thinking level",
                                            [DEFAULT_CHOICE, *VALID_THINKING_LEVELS])
        if thinking_pick is None:
            ctx.ui.notify("Creation cancelled.", "info")
            return
        thinking = None if thinking_pick == DEFAULT_CHOICE else thinking_pick
        summary = (f"Specialty: {specialty.strip() or 'not specified'} | Model: {model or 'global default'}"
                   f" | Thinking: {thinking or 'global default'}")
        if not await ctx.ui.confirm(f"Create Sister {sid}?", summary):
            ctx.ui.notify("Creation cancelled.", "info")
            return
        ok, msg = create_sister(sid, specialty=specialty, model=model, thinking=thinking)
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


def cli_create(sid=None, desc=None, model=None, root=None, thinking=None):
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
        model = input(f"Pinned model as provider/model, such as {PIN_EXAMPLE} (blank uses the global default): ").strip()
    if thinking is None and interactive:
        thinking = input("Default thinking level: off, minimal, low, medium, high, xhigh or max "
                         "(blank uses the global default): ").strip()
    ok, msg = create_sister(sid, root=root, specialty=desc or None, model=model or None, thinking=thinking or None)
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
