"""Foreground chat: ``misaka chat`` talks to Last Order or to one Sister.

Runs the engine's interactive mode in-process; extensions are registered as
factories rather than mounted from ``-e`` files.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from misaka.config import current_config, profiles, sessions, sisters


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
        # Her own pin first: one Sister, one model, set from her own session's
        # /model selector (config.profiles.persist_role_default_model). The
        # product-wide default is only the fallback for a Sister who has none.
        return prof, profiles.pinned_model(prof) or cfg["default_model"]
    prof = os.path.join(cfg["roles_root"], "last_order")
    return prof, cfg["lo_model"]


def resolve_session(session, who):
    """The engine's own resolution (path, id prefix in this folder's bucket, then any bucket), done
    once, here, before the folder changes -- so a prefix is not mistaken for a file and a relative
    path is not resolved twice. Returns the absolute session file."""
    from misaka.cli.engine import resolve_session_path
    explicit_path = "/" in session or "\\" in session or session.endswith(".jsonl")
    bucket = None
    if not explicit_path:
        bucket = sessions.chat_dir(who, os.getcwd())
    resolved = asyncio.run(resolve_session_path(session, os.getcwd(), bucket))
    if not resolved.path:
        sys.exit(f"No session found matching '{session}'.")
    return resolved.path


def launch(who, model=None, cont=False, pick=False, session=None, read_only=False, catalog=None, attach=False, skills=None):
    """Open chat; only the writable form assembles an agent. ``who=None`` means Last Order."""
    from misaka.core.session_manager import read_session_header
    # `chat` is a full-screen TUI in every form, not just under --pick. Without a terminal the
    # picker died on a bare OSError(EINVAL) from registering stdin with the event loop, while
    # every other form exited 0 having printed nothing at all -- so a piped `misaka chat` looked
    # like it had worked. Both are the same missing precondition, so both are refused here.
    #
    # Naming --session or -c as the way out would be wrong: they are equally interactive, and
    # recommending them is what the earlier --pick-only guard did.
    if not sys.stdin.isatty():
        sys.exit("misaka chat needs a terminal; stdin is not a TTY.\n"
                 "For a scripted, non-interactive turn use `misaka dm <who> \"<message>\"`.")
    if read_only or attach:
        access = "--attach" if attach else "--read-only"
        if not (session or catalog) or (session and catalog):
            sys.exit(f"chat {access} needs exactly one of --session or --catalog.")
        if who is not None or model is not None or cont or pick or skills:
            sys.exit(f"chat {access} opens the original session; omit --as, --model, --continue and --pick.")
        # An explicit file needs no engine, roster, auth, session writer or task hooks.
        path = session or catalog
        if session and not ("/" in session or "\\" in session or session.endswith(".jsonl")):
            path = resolve_session(session, None)
        from misaka.core.settings_manager import SettingsManager
        from misaka.ui.tui import TUI, ProcessTerminal
        from misaka.ui.tui.interactive.conversation import Conversation

        settings = SettingsManager.create(os.getcwd())
        ui = TUI(ProcessTerminal(), settings.getShowHardwareCursor())
        ui.setClearOnShrink(settings.getClearOnShrink())
        screen = Conversation(ui, settings, os.getcwd())
        try:
            return asyncio.run(follow_session(screen, Path(path).expanduser().absolute(), catalog=bool(catalog),
                                             **({"attach": True} if attach else {})))
        except KeyboardInterrupt:
            return 0
        except (OSError, ValueError) as error:
            sys.exit(str(error))
    if catalog:
        sys.exit("--catalog requires --attach or --read-only.")
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
    sess = sessions.chat_dir(who, os.getcwd())
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
    # decides what this session sees (misaka.core.skills.index).
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
        from misaka.core.network.roster import describe_line
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

    from misaka.core.skills.vendor.startup import _normalize_skills
    from misaka.core.wiring import SessionSpec, assemble
    session_assembly = assemble(SessionSpec(
        profile_dir=prof,
        role=profile_role,
        workspace=workspace,
        kind="foreground",
        sender=session_role,
        mcp_role=session_role,
        receive_messages=True,
        startup_skills=tuple(_normalize_skills(skills)),
    ))

    from misaka.cli.engine import main as engine_main
    sys.exit(asyncio.run(engine_main(flags, session_assembly.engine_options())))


def _display_value(value):
    """Saved strings are content, not terminal control instructions."""
    from misaka.utils.ansi import strip_ansi

    if isinstance(value, str):
        return "".join(ch for ch in strip_ansi(value) if ch in "\n\t" or ch.isprintable())
    if isinstance(value, list):
        return [_display_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _display_value(item) for key, item in value.items()}
    return value


def read_session_snapshot(path):
    from misaka.core.session_manager import _parse_jsonl_entries, build_context_entries

    # The writer may be mid-UTF-8 or mid-entry. Read only committed lines; never
    # open a writable SessionManager to repair, migrate or flush someone else's file.
    data = path.read_bytes()
    entries = _parse_jsonl_entries(data[:data.rfind(b"\n") + 1].decode("utf-8"), strict=True)
    if not entries or entries[0].get("type") != "session" or not isinstance(entries[0].get("id"), str):
        raise ValueError("Waiting for a complete session header")
    header, *entries = _display_value(entries)
    return header, build_context_entries(entries)


async def follow_session(screen, path, *, catalog=False, attach=False):
    """One conversation screen; attach sends input to its owner, history only reads."""
    from misaka.core.keybindings import KeybindingsManager
    from misaka.core.platform import processes
    from misaka.core.settings_manager import SettingsManager
    from misaka.ui.tui import Text, matchesKey, setCapabilityOverrides, setKeybindings
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint
    from misaka.ui.tui.interactive.theme.theme import init_theme, theme

    closed = asyncio.Event()
    keybindings = KeybindingsManager.create()
    setKeybindings(keybindings)
    init_theme(screen.settingsManager.getTheme())
    setCapabilityOverrides(screen.settingsManager.getTerminalCapabilityOverrides())
    header, notice = Text("", 1, 1), Text("", 1, 1)
    screen.headerContainer.addChild(header)
    screen.statusContainer.addChild(notice)
    footer_text = "Read-only · saved messages · " + " · ".join((
        key_hint("app.tools.expand", "tools"), key_hint("app.thinking.toggle", "thinking"),
        "Ctrl+C/D closes window",
    ))
    screen.footer = Text("", 1, 1)
    owner, cursor, sending = None, None, set()
    receipt = ""
    if attach:
        from misaka.core.session_catalog import _object, owner_record
        from misaka.core.session_control import request
        from misaka.ui.tui.interactive.components.custom_editor import CustomEditor
        from misaka.ui.tui.interactive.theme.theme import get_editor_theme

        owner = _display_value(_object(path) if catalog else owner_record(path))
        if not owner.get("control") or owner.get("state") == "saved":
            raise ValueError("This session has no live input endpoint; open its saved history instead.")
        screen.editor = CustomEditor(screen.ui, get_editor_theme(), keybindings)
        screen.editorContainer.addChild(screen.editor)
        screen.ui.setFocus(screen.editor)
        footer_text = "Original session · Enter sends/steers · /pause /resume · Ctrl+C/D detaches"

        async def submit(text):
            nonlocal receipt
            try:
                operation = text.strip()[1:] if text.strip() in {"/pause", "/resume"} else "input"
                receipt = await request(owner, operation, **({"text": text} if operation == "input" else {}))
            except (OSError, ValueError, KeyError, TimeoutError) as error:
                receipt = f"Delivery unconfirmed: {error or 'owner timed out'}. No automatic retry.\nInput: {text}"
                if not screen.editor.getText():
                    screen.editor.setText(text)
            notice.setText(_display_value(receipt))
            screen.ui.requestRender()

        def send(text):
            task = asyncio.create_task(submit(text))
            sending.add(task)
            task.add_done_callback(sending.discard)

        screen.editor.onSubmit = send
    screen.mount()

    def on_input(data):
        if matchesKey(data, "ctrl+c") or matchesKey(data, "ctrl+d"):
            closed.set()
        elif keybindings.matches(data, "app.tools.expand"):
            screen.toggleToolOutputExpansion()
        elif keybindings.matches(data, "app.thinking.toggle"):
            screen.toggleThinkingBlockVisibility()
        else:
            return None  # The focused editor (if attached) receives ordinary input.
        return {"consume": True}

    unsubscribe = screen.ui.addInputListener(on_input)
    stamp = session_id = None
    started = False
    try:
        while not closed.is_set():
            try:
                if owner is not None:
                    snapshot = _display_value(await request(owner, "snapshot", cursor=cursor))
                    cursor = snapshot["cursor"]
                    if snapshot["entries"] is not None:
                        screen.updateEntries(snapshot["entries"])
                    workflow = snapshot["workflow"]
                    phase = (f" · depth {workflow['depth']} · node {workflow['phase']}" if workflow else "")
                    state = "pause requested" if snapshot["paused"] else snapshot["state"]
                    header.setText(f"{owner.get('role', 'Session')} · session {state}{phase}\n{snapshot['cwd']}")
                    queued = snapshot["steering"] + snapshot["follow_up"]
                    notice.setText(snapshot["error"] or ("Queued for original session:\n" + "\n".join(queued) if queued else receipt))
                    screen.footer.setText(theme.fg("dim", footer_text))
                    screen.ui.requestRender()
                    if not started:
                        screen.ui.start()
                        started = True
                    try:
                        await asyncio.wait_for(closed.wait(), timeout=.5)
                    except TimeoutError:
                        pass
                    continue
                stat = path.stat()
                current = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                changed = current != stamp or catalog
                if changed:
                    if catalog:
                        record = _display_value(json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8")))
                        if not isinstance(record, dict):
                            raise ValueError("Invalid session catalog record")
                        state = (record.get("state") if processes.identity_is_alive(record.get("pid"), record.get("identity"))
                                 else "ended")
                        header.setText(f"{record.get('role')} · {record.get('kind')} · {state}")
                        notice.setText("Ephemeral session: message history is not saved.")
                    else:
                        info, entries = await asyncio.to_thread(read_session_snapshot, path)
                        if info["id"] != session_id:
                            screen.cwd = info.get("cwd") or str(path.parent)
                            screen.settingsManager = SettingsManager.create(screen.cwd)
                            init_theme(screen.settingsManager.getTheme())
                            setCapabilityOverrides(screen.settingsManager.getTerminalCapabilityOverrides())
                            screen.hideThinkingBlock = screen.settingsManager.getHideThinkingBlock()
                            screen.outputPad = screen.settingsManager.getOutputPad()
                            screen.updateEntries([])
                            session_id = info["id"]
                            header.setText(theme.fg("accent", f"Session {session_id}") + f"\n{screen.cwd}")
                        screen.updateEntries(entries)
                        notice.setText("" if screen.chatContainer.children else "Waiting for saved messages…")
                    stamp = current
            except (OSError, ValueError, KeyError, TimeoutError) as error:
                stamp = None
                changed = True
                if owner is not None and not os.path.exists(str(owner.get("control") or "")):
                    # The owning process exited (a research node that finished its routine, a
                    # closed window): its input socket is gone, the conversation itself is not.
                    notice.setText("The original session has ended; this is its saved conversation, read-only. "
                                   "Nothing typed here can reach it any more.")
                else:
                    notice.setText(_display_value(f"Session unavailable: {error}"))
                if owner is not None:
                    screen.footer.setText(theme.fg("dim", "Original owner disconnected · reopen from Sessions · Ctrl+C/D detaches"))
                    # Read any final committed messages, but never start a replacement
                    # owner or silently route queued text to a new runtime instance.
                    saved = owner.get("path")
                    if saved and os.path.isfile(saved):
                        try:
                            _info, entries = await asyncio.to_thread(read_session_snapshot, Path(saved))
                            screen.updateEntries(entries)
                        except (OSError, ValueError):
                            pass
            if changed:
                if owner is None:
                    screen.footer.setText(theme.fg("dim", footer_text))
                screen.ui.requestRender()
            if not started:
                screen.ui.start()
                started = True
            try:
                await asyncio.wait_for(closed.wait(), timeout=0.5)
            except TimeoutError:
                pass
    finally:
        unsubscribe()
        for task in sending:
            task.cancel()
        await asyncio.gather(*sending, return_exceptions=True)
        screen.ui.stop()
    return 0
