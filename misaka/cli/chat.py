"""Foreground chat: ``misaka chat`` talks to Last Order or to one Sister.

Runs the engine's interactive mode in-process; extensions are registered as
factories rather than mounted from ``-e`` files.
"""
import asyncio
import os
import sys

from misaka.config import current_config, profiles, sisters


def _session_by_id(session_dir, session_id):
    from misaka.core.session_manager import read_session_header

    try:
        names = os.listdir(session_dir)
    except OSError:
        return None
    return next(
        (
            os.path.join(session_dir, name)
            for name in names
            if name.endswith(".jsonl")
            and read_session_header(os.path.join(session_dir, name)).get("id") == session_id
        ),
        None,
    )


def assembly(who, cfg=None):
    """Shared role setup for foreground chat and the DM loop.

    Returns ``(profile_dir, default_model)``; exits if ``who`` is not a known Sister.
    Persona text is not resolved here (``config.identity``), and neither are skills:
    the skills extension discovers them from the session spec."""
    cfg = cfg or current_config()
    if who:
        prof = os.path.join(cfg["profiles_root"], who)
        if not os.path.isdir(prof):
            sys.exit(f"Unknown Sister {who!r}. Roster: {', '.join(sorted(sisters()))}")
        return prof, cfg["default_model"]
    prof = os.path.join(cfg["roles_root"], "last_order")
    return prof, cfg["lo_model"]


def resolve_session(session, who):
    """The engine's own resolution (path, id prefix in this folder's bucket, then any bucket), done
    once, here, before the folder changes -- so a prefix is not mistaken for a file and a relative
    path is not resolved twice. Returns the absolute session file."""
    from misaka.cli.engine import resolve_session_path
    from misaka.core.session_manager import get_session_dir_for_cwd
    explicit_path = "/" in session or "\\" in session or session.endswith(".jsonl")
    bucket = None
    if not explicit_path:
        root = os.path.expanduser(f"~/.misaka/sessions/{who or 'last-order'}")
        bucket = get_session_dir_for_cwd(os.getcwd(), root)
    resolved = asyncio.run(resolve_session_path(session, os.getcwd(), bucket))
    if not resolved.path:
        sys.exit(f"No session found matching '{session}'.")
    return resolved.path


def launch(who, model=None, cont=False, pick=False, session=None):
    """Assemble the session and run interactive mode until it exits. ``who=None`` means Last Order."""
    from misaka.core.session_manager import get_session_dir_for_cwd, read_session_header
    # The session picker is a full-screen TUI: it registers stdin with the event loop, which
    # fails with a bare OSError(EINVAL) when stdin is a pipe or /dev/null. Refuse early and
    # name the two ways to resume without a terminal.
    if pick and not sys.stdin.isatty():
        sys.exit("--pick needs a terminal; stdin is not a TTY.\n"
                 "Resume a known session with --session <id>, the last one with -c, "
                 "or run misaka chat --pick in a terminal.")
    resumed_session_id = None
    if session:
        session = resolve_session(session, who)
        # A resumed conversation goes back to the folder it worked in, whatever folder the
        # shell or the panel sits in: the bucket, the workspace, the skills, and every tool
        # follow the session. A folder that is gone is an error, not a silent move.
        header = read_session_header(session)
        folder = header.get("cwd")
        resumed_session_id = header.get("id")
        if not folder or not os.path.isdir(folder):
            sys.exit(f"Cannot resume {session}: its folder {folder or '(unknown)'} no longer exists.")
        os.chdir(folder)
    cfg = current_config()
    prof, model_default = assembly(who, cfg)
    # Sessions are bucketed per role and per folder, like pi's per-cwd sessions:
    # `-c` resumes this role's conversation about *this* project.
    sess = get_session_dir_for_cwd(
        os.getcwd(),
        os.path.expanduser(f"~/.misaka/sessions/{who or 'last-order'}"),
    )
    if session and not os.path.isfile(session):
        session = _session_by_id(sess, resumed_session_id)
        if not session:
            sys.exit(f"Cannot resume migrated session {resumed_session_id or '(unknown)'}.")
    if who:
        title = f"MISAKA · {who}"
    else:
        title = "MISAKA · Last Order"
    from misaka.config import identity
    # The engine has no skill loading of its own; the skills extension is the one place that
    # decides what this session sees (misaka.skills.index).
    flags = ["--provider", cfg["provider"], "--model", model or model_default,
             "--append-system-prompt", profiles.shared_soul()]
    for section in identity.prompt_sections(prof, profiles.role_of(prof)):
        flags += ["--append-system-prompt", section]
    flags += ["--session-dir", sess]
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
        blurb = (describe_line(who, root=cfg["profiles_root"])
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
