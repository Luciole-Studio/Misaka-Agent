"""A role's contact session is her Bot Chat, kept as Hermes keeps a bot's: found by its exact title
(``session_bot._target``), started afresh by deleting it, never brought back by the delivery that was
using it, and never renamed (``hermes_state_titles.py``).

2026-10-29: the contact session was whichever file in the role's dm folder had been written last. A
rename from the session picker, ``/name`` in a window opened on an older file, or a branch made there
quietly made another file the contact session; deleting it from the picker was not refused while a
delivery ran, and the running turn wrote the file back without its header."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from misaka.config import CFG, sessions
from misaka.core import session_catalog
from misaka.core.network import messages
from misaka.core.platform import processes
from misaka.core.session_manager import SessionManager, readSessionHeader

ROLE = "10032"
HOME = os.path.expanduser("~")
REFUSED = "it has a queued or running turn. Wait for it to finish (or stop it), then retry."


def chat(folder=None, *, name=sessions.CONTACT_TITLE):
    """A session file as a contact turn leaves one, in ``ROLE``'s dm folder unless ``folder`` says."""
    folder = folder or sessions.dm_dir(ROLE)
    os.makedirs(folder, exist_ok=True)
    manager = SessionManager.create(HOME, folder)
    if name:
        manager.appendSessionInfo(name)
    manager.appendMessage({"role": "user", "content": "hello", "timestamp": 0})
    return manager.getSessionFile()


def branch(path):
    """``/fork`` of ``path`` in a window whose session folder is the file's own (``/resume``)."""
    manager = SessionManager.open(path)
    manager.createBranchedSession(manager.getLeafId())
    manager.rewrite_file()
    return manager.getSessionFile()


def session_id(path):
    return readSessionHeader(path)["id"]


def owned(path, *, alive=True):
    """A catalog record of a process holding ``path``, as CatalogPart writes one."""
    if alive:
        pid, identity = os.getpid(), processes.identity(os.getpid())
    else:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        pid, identity = gone.pid, "gone"
    index = session_catalog._index_dir()
    index.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.realpath(path).encode()).hexdigest()
    record = {"id": session_id(path), "path": path, "pid": pid, "identity": identity, "state": "working"}
    (index / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")


def refusal_for(path):
    with session_catalog.deletable(path) as refusal:
        return refusal


@pytest.fixture
def turns(monkeypatch):
    """``misaka dm`` with its model turns scripted. ``turns.results`` lists what each turn returns as
    its error (None answers); ``turns.during`` runs inside a turn, before it returns."""
    from misaka.core.platform import session as platform_session

    os.makedirs(os.path.join(CFG["profiles_root"], ROLE), exist_ok=True)
    state = type("Turns", (), {"calls": [], "results": [], "during": None})()

    async def turn(flags, text, cwd, **kw):
        state.calls.append(flags)
        if "--session" in flags:
            path = flags[flags.index("--session") + 1]
        else:
            manager = SessionManager.create(cwd, flags[flags.index("--session-dir") + 1])
            manager.appendSessionInfo(flags[flags.index("--name") + 1])
            manager.appendMessage({"role": "user", "content": text, "timestamp": 0})
            path = manager.getSessionFile()
        if state.during:
            state.during(path)
        error = state.results[len(state.calls) - 1] if len(state.calls) <= len(state.results) else None
        return {"text": None if error else "standing by", "error": error, "timed_out": False, "session_file": path}

    monkeypatch.setattr(platform_session, "run_session", turn)
    return state


def where(flags):
    """What a contact turn was told to open: ``("--session", path)`` or ``("--name", title)``."""
    flag = "--session" if "--session" in flags else "--name"
    return flag, flags[flags.index(flag) + 1]


def test_the_bot_chat_is_the_session_holding_its_title_in_the_dm_folder():
    from misaka.cli import dm

    legacy = chat(name=None)                       # written before contact sessions were named
    assert dm.contact_session(ROLE) is None        # not adopted: a session without the title is not it
    bot_chat = chat()
    forked = branch(bot_chat)
    imported = os.path.join(sessions.dm_dir(ROLE), "import-0123abcd.jsonl")
    os.replace(chat(), imported)                   # a copy brought in with /import holds the title too
    for later in (legacy, forked, imported):       # each written after it: the old rule took the newest
        os.utime(later, None)
    assert SessionManager.open(forked).getSessionName() == sessions.CONTACT_TITLE
    assert dm.contact_session(ROLE) == bot_chat


def test_a_delivery_names_a_new_bot_chat_and_comes_back_to_it(turns):
    from misaka.cli import dm

    assert dm.deliver(ROLE, "hello there") == 0
    assert where(turns.calls[0]) == ("--name", sessions.CONTACT_TITLE)
    bot_chat = dm.contact_session(ROLE)
    assert bot_chat is not None
    assert dm.deliver(ROLE, "and again") == 0
    assert where(turns.calls[1]) == ("--session", bot_chat)


def test_deleting_the_bot_chat_starts_a_new_one(turns):
    from misaka.cli import dm

    old = chat()
    with session_catalog.deletable(old) as refusal:
        assert refusal is None
        os.remove(old)
    assert dm.deliver(ROLE, "hello there") == 0
    assert where(turns.calls[0]) == ("--name", sessions.CONTACT_TITLE)
    new = dm.contact_session(ROLE)
    assert new != old and session_id(new) != os.path.basename(old) and not os.path.exists(old)


def test_a_bot_chat_with_a_queued_or_running_turn_is_not_deleted():
    from misaka.cli import dm

    bot_chat = chat()
    with dm._serial(ROLE):                          # a delivery is claiming or running
        assert refusal_for(bot_chat) == f"Cannot delete session '{session_id(bot_chat)}': {REFUSED}"
    owned(bot_chat)                                 # its contact turn is running
    assert refusal_for(bot_chat).endswith(REFUSED)
    owned(bot_chat, alive=False)                    # ... and has ended
    assert refusal_for(bot_chat) is None
    con = messages.connect()
    try:
        mid = messages.send(con, ROLE, "for you", summary="x", sender="last-order", to_session=session_id(bot_chat))
        assert refusal_for(bot_chat).endswith(REFUSED)  # mail addressed to it waits for its next turn
        con.execute("UPDATE messages SET delivered_at=1 WHERE id=?", (mid,))
    finally:
        con.close()
    assert refusal_for(bot_chat) is None


def test_any_session_with_a_live_owner_is_not_deleted():
    """Hermes refuses every busy session, not only a Bot Chat; a window still writing the file would
    put it back without its header."""
    path = chat(sessions.chat_dir(None, HOME), name=None)
    owned(path)
    assert refusal_for(path).endswith(REFUSED)
    owned(path, alive=False)
    assert refusal_for(path) is None


@pytest.mark.parametrize("meanwhile", ["deleted", "replaced"])
def test_the_retry_never_brings_back_a_bot_chat_gone_since_the_first_attempt(turns, meanwhile):
    """Hermes' retry may not create a Bot Chat: with the first attempt's gone or replaced, it is refused
    and the first failure stands."""
    from misaka.cli import dm

    first = chat()
    turns.results = ["Error code: 529 overloaded_error", None]

    def gone(path):
        if len(turns.calls) == 1:
            os.remove(path)
            if meanwhile == "replaced":
                chat()

    turns.during = gone
    assert dm.deliver(ROLE, "hello there") == 1
    assert [where(flags) for flags in turns.calls] == [("--session", first)]
    assert not os.path.exists(first)


def test_the_bot_chat_refuses_a_rename_and_ignores_an_automatic_one():
    bot_chat = chat()
    manager = SessionManager.open(bot_chat)
    entries = len(manager.getEntries())
    with pytest.raises(ValueError) as refusal:
        manager.appendSessionInfo("my notes")
    assert str(refusal.value) == (
        f"This is {ROLE}'s canonical Bot Chat — its name is its identity, and renaming it would orphan the "
        f"conversation. To start fresh, delete it: the next message to {ROLE} starts a new one.")
    assert manager.appendSessionInfo("my notes", is_user=False) == ""       # an extension's title
    assert manager.appendSessionInfo(sessions.CONTACT_TITLE) == ""          # the name it has
    assert len(manager.getEntries()) == entries
    assert SessionManager.open(bot_chat).getSessionName() == sessions.CONTACT_TITLE


def test_only_the_bot_chat_keeps_its_name():
    """Hermes: "a visible session merely named 'Bot Chat' stays renameable"."""
    for path in (chat(sessions.chat_dir(ROLE, HOME)),    # her own conversation, named so
                 branch(chat()),                          # a branch of the Bot Chat
                 chat(name=None)):                        # a file in her dm folder without the title
        manager = SessionManager.open(path)
        manager.appendSessionInfo("my notes")
        assert manager.getSessionName() == "my notes"


async def test_an_extension_naming_a_bot_chat_changes_nothing():
    """An extension's ``pi.setSessionName`` is an automatic title (Hermes' auto-titler: a silent no-op);
    ``/name`` is a person's, and is refused."""
    from tests.suite_harness import createHarness

    api = {}
    manager = SessionManager.open(chat())
    async with createHarness(extensionFactories=[lambda pi: api.setdefault("pi", pi)],
                             sessionManager=manager) as harness:
        api["pi"].setSessionName("my notes")
        assert harness.sessionManager.getSessionName() == sessions.CONTACT_TITLE
        with pytest.raises(ValueError):
            harness.session.setSessionName("my notes")


# ── the session picker ──────────────────────────────────────────────────────────────────────

CTRL_R = "\x12"


def picker(path, name, **options):
    from datetime import UTC, datetime

    from misaka.core.keybindings import KeybindingsManager
    from misaka.core.session_manager import SessionInfo
    from misaka.modes.interactive.components.session_selector import (
        SessionSelectorComponent,
    )
    from misaka.modes.interactive.theme.theme import initTheme
    from misaka.ui.tui import setKeybindings

    initTheme("dark")
    keybindings = KeybindingsManager()
    setKeybindings(keybindings)
    info = SessionInfo(path=path, id=session_id(path), cwd=HOME, name=name, parentSessionPath=None,
                       created=datetime.fromtimestamp(0, UTC), modified=datetime.fromtimestamp(0, UTC),
                       messageCount=1, firstMessage="hello", allMessagesText="hello")

    async def load(*_args):
        return [info]

    def noop(*_args):
        return None

    return SessionSelectorComponent(load, load, noop, noop, noop, noop, {"keybindings": keybindings, **options})


async def test_the_picker_offers_no_rename_on_a_bot_chat():
    from tests.fake_timers import flush

    renamed = []

    async def rename(path, name):
        renamed.append(path)

    selector = picker(chat(), sessions.CONTACT_TITLE, renameSession=rename)
    await flush()
    selector.getSessionList().handleInput(CTRL_R)
    assert selector.mode == "list" and "canonical Bot Chat" in selector.header.statusMessage["message"]

    other = picker(chat(sessions.chat_dir(ROLE, HOME)), sessions.CONTACT_TITLE, renameSession=rename)
    await flush()
    other.getSessionList().handleInput(CTRL_R)
    assert other.mode == "rename"


async def test_the_picker_does_not_delete_a_bot_chat_while_a_delivery_runs(monkeypatch):
    from misaka.cli import dm
    from misaka.modes.interactive.components import session_selector
    from tests.fake_timers import flush

    async def unlink(path):        # the real one moves the file to the user's Trash
        os.remove(path)
        return {"ok": True, "method": "unlink"}

    monkeypatch.setattr(session_selector, "deleteSessionFile", unlink)
    bot_chat = chat()
    selector = picker(bot_chat, sessions.CONTACT_TITLE)
    await flush()
    with dm._serial(ROLE):
        await selector.getSessionList().onDeleteSession(bot_chat)
    assert os.path.exists(bot_chat) and selector.header.statusMessage["message"].endswith(REFUSED)
    await selector.getSessionList().onDeleteSession(bot_chat)
    assert not os.path.exists(bot_chat)
