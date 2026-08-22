"""Create and remove durable Sister profiles."""
import json
import os
import re
import shutil
import sys

ROOT = os.path.expanduser("~/.misaka/profiles/sisters")
ACTIVE = ("running", "review", "verifying", "finalizing")
MODEL_CHOICES = [
    "default (use global setting)",
    "claude-opus-5",
    "claude-sonnet-5",
    "gemini-3.5-flash",
    "Custom…",
]

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
    from misaka.platform import tasks as board_db
    path = os.path.expanduser(db_path or CFG["db"])
    if not os.path.exists(path):
        return {}
    con = board_db.connect(path)
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE assignee=? GROUP BY status", (sid,)
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
        with open(os.path.join(prof, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"model": model}, f, ensure_ascii=False, indent=2)
        pinned = f"Pinned model: {model}. "
    return True, (
        f"Sister {sid} was added to the roster. {pinned}"
        f"Profile: {prof}/DESCRIBE.md; optionally configure mcp_servers in config.yaml "
        f"and link skills under skills/. Use /sister {sid} to switch to this Sister."
    )


def describe(sid, root=None):
    """Return the short description and body from DESCRIBE.md."""
    from misaka.utils.frontmatter import parse_frontmatter
    try:
        with open(os.path.join(root or ROOT, sid, "DESCRIBE.md"), encoding="utf-8") as f:
            parsed = parse_frontmatter(f.read())
    except OSError:
        return None, None
    desc = str(parsed.frontmatter.get("description") or "").strip()
    return desc or None, parsed.body.strip() or None


def describe_line(sid, root=None):
    """Return a concise roster description."""
    from misaka.core.skills import truncate_skill_description
    desc, _ = describe(sid, root)
    return truncate_skill_description(desc) if desc else None


def remove_sister(sid, root=None, db_path=None):
    """Remove a Sister profile unless it has active cards; return ``(success, message)``."""
    root = root or ROOT
    prof = os.path.join(root, sid)
    if not _valid(sid) or not os.path.isdir(prof):
        return False, f"Sister '{sid}' is not in the roster. Use /sister to list available Sisters."
    counts = card_counts(sid, db_path)
    live = {k: v for k, v in counts.items() if k in ACTIVE}
    if live:
        return False, f"Sister {sid} has active cards ({live}); stop or finish them before removal."
    shutil.rmtree(prof)
    rest = ','.join(f"{k}×{v}" for k, v in sorted(counts.items())) or 'none'
    return True, f"Sister {sid} was removed. Historical cards ({rest}), workspaces, and transcripts were preserved."


# ── TUI: /create and /remove (configuration wizard) ────────────────────────


def register(harn):
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
        model_pick = await ctx.ui.select(f"Choose a model for Sister {sid}", MODEL_CHOICES)
        if model_pick is None:
            ctx.ui.notify("Creation cancelled.", "info")
            return
        model = None
        if model_pick == "Custom…":
            model = await ctx.ui.input("Model ID", "For example: claude-opus-5")
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

    harn.registerCommand("create", {
        "description": "Create a Sister profile with an ID, description, and model under ~/.misaka/profiles/sisters/.",
        "handler": create_cmd,
    })
    harn.registerCommand("remove", {
        "description": "Remove a Sister profile; task history and workspaces remain, and active Sisters are protected.",
        "handler": remove_cmd,
    })


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
