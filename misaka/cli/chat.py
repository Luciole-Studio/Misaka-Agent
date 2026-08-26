"""Foreground chat: ``misaka chat`` talks to Last Order or to one Sister.

Runs the engine's interactive mode in-process; extensions are registered as
factories rather than mounted from ``-e`` files.
"""
import asyncio
import os
import sys

from misaka.config import profiles
from misaka.config import CFG, sisters


def _migrate_lo_soul(prof):
    """One-time rename: SOUL-chat.md becomes SOUL.md, Last Order's only persona file.

    The previous SOUL.md (the planning contract) is kept as SOUL-plan-retired.md in
    case it holds user edits. Idempotent: no SOUL-chat.md means already migrated."""
    chat_soul = os.path.join(prof, "SOUL-chat.md")
    if not os.path.isfile(chat_soul):
        return
    try:
        old = os.path.join(prof, "SOUL.md")
        if os.path.isfile(old):
            os.rename(old, os.path.join(prof, "SOUL-plan-retired.md"))
        os.rename(chat_soul, old)
    except OSError:
        pass


def assembly(who):
    """Shared role setup for foreground chat and the DM loop.

    Returns ``(profile_dir, default_model)``; exits if ``who`` is not a known Sister.
    Persona text is not resolved here (``config.identity``), and neither are skills:
    the skills extension discovers them from the session spec."""
    if who:
        prof = os.path.join(CFG["profiles_root"], who)
        if not os.path.isdir(prof):
            sys.exit(f"Unknown Sister {who!r}. Roster: {', '.join(sorted(sisters()))}")
        return prof, CFG["default_model"]
    prof = os.path.join(CFG["roles_root"], "last_order")
    _migrate_lo_soul(prof)
    return prof, CFG["lo_model"]


def launch(who, model=None, cont=False, pick=False, session=None):
    """Assemble the session and run interactive mode until it exits. ``who=None`` means Last Order."""
    from misaka.core.session_manager import encode_cwd, read_session_header
    if session:
        # A resumed conversation goes back to the folder it worked in, whatever folder the
        # shell or the panel sits in: the bucket, the workspace, the skills, and every tool
        # follow the session. A folder that is gone is an error, not a silent move.
        folder = read_session_header(session).get("cwd")
        if not folder or not os.path.isdir(folder):
            sys.exit(f"Cannot resume {session}: its folder {folder or '(unknown)'} no longer exists.")
        os.chdir(folder)
    prof, model_default = assembly(who)
    # Sessions are bucketed per role and per folder, like pi's per-cwd sessions:
    # `-c` resumes this role's conversation about *this* project.
    sess = f"~/.misaka/sessions/{who or 'last-order'}/{encode_cwd(os.getcwd())}"
    if who:
        title = f"MISAKA · {who}"
    else:
        title = "MISAKA · Last Order"
    from misaka.config import identity
    # --no-skills: the engine's own skill loading stays off; the skills extension is the one
    # place that decides what this session sees (misaka.skills.index).
    flags = ["--provider", CFG["provider"], "--model", model or model_default, "--no-skills",
             "--append-system-prompt", profiles.shared_soul()]
    for section in identity.prompt_sections(prof, profiles.role_of(prof)):
        flags += ["--append-system-prompt", section]
    flags += ["--session-dir", os.path.expanduser(sess)]
    if session:
        flags += ["--session", session]
    elif pick:
        flags.append("-r")
    elif cont:
        flags.append("-c")

    profile_role = profiles.role_of(prof)
    session_role = who or "last-order"
    workspace = os.getcwd()
    if who:
        from misaka.network.roster import describe_line
        blurb = (describe_line(who, root=CFG["profiles_root"])
                 or "Ask her to read papers, look things up, or get work done.")
    tagline = ("Last Order, Misaka Network coordinator, standing by. She asks questions, splits work into cards, and calls Sisters once you approve. /sister shows the roster; /sister 10032 opens a direct chat."
               if not who else
               f"Sister {who} online: {blurb} (/sister shows the roster; /sister <id> opens a direct chat)")
    os.environ.update({
        "MISAKA_APP_TITLE": title, "MISAKA_TAGLINE": tagline,
        "MISAKA_WHO": session_role,
        "MISAKA_MCP_ROLE": session_role,
        "MISAKA_PROFILE_DIR": prof,
        "MISAKA_WORKSPACE": workspace,
        "MISAKA_INPUT_HISTORY": os.path.expanduser(f"~/.misaka/input-history/{who or 'last-order'}.json"),
        "MISAKA_CODING_AGENT": "true"})

    from misaka.app.composition import SessionSpec, build_extensions
    factories = build_extensions(SessionSpec(
        profile_dir=prof,
        role=profile_role,
        workspace=workspace,
        kind="foreground",
        sender=session_role,
        mcp_role=session_role,
        receive_messages=True,
    ))

    from misaka.cli.engine import main as engine_main
    sys.exit(asyncio.run(engine_main(flags, {"extensionFactories": factories})))
