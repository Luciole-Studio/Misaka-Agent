"""Translated from pi's ``coding-agent/test/session-selector-path-delete.test.ts`` (v1.1.0).

The two symlink tests skip where the process may not create symlinks (Windows without Developer Mode).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from typing import Any

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo
from misaka.modes.interactive.components.session_selector import (
    SessionSelectorComponent,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from tests.fake_timers import flush


async def flushPromises() -> None:
    await flush()


_ANSI = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def stripAnsi(text: str) -> str:
    return _ANSI.sub("", text)


def makeSession(id: str, **overrides: Any) -> SessionInfo:
    return SessionInfo(
        path=overrides.get("path", f"/tmp/{id}.jsonl"),
        id=id,
        cwd=overrides.get("cwd", ""),
        name=overrides.get("name"),
        parentSessionPath=overrides.get("parentSessionPath"),
        created=overrides.get("created", datetime.fromtimestamp(0, UTC)),
        modified=overrides.get("modified", datetime.fromtimestamp(0, UTC)),
        messageCount=overrides.get("messageCount", 1),
        firstMessage=overrides.get("firstMessage", "hello"),
        allMessagesText=overrides.get("allMessagesText", "hello"),
    )


def date(text: str) -> datetime:
    return datetime.fromisoformat(text)


def createSymlinkedSessionPaths() -> dict[str, str]:
    baseDir = tempfile.mkdtemp(prefix="pi-session-selector-")
    realDir = os.path.join(baseDir, "real")
    aliasADir = os.path.join(baseDir, "alias-a")
    aliasBDir = os.path.join(baseDir, "alias-b")
    os.makedirs(realDir, exist_ok=True)
    os.makedirs(aliasADir, exist_ok=True)
    os.makedirs(aliasBDir, exist_ok=True)

    sharedDir = os.path.join(realDir, "sessions")
    os.makedirs(sharedDir, exist_ok=True)
    aliasASessions = os.path.join(aliasADir, "sessions")
    aliasBSessions = os.path.join(aliasBDir, "sessions")
    try:
        os.symlink(sharedDir, aliasASessions, target_is_directory=True)
        os.symlink(sharedDir, aliasBSessions, target_is_directory=True)
    except OSError as error:
        shutil.rmtree(baseDir, ignore_errors=True)
        pytest.skip(f"symlinks are unavailable: {error}")

    parentRealPath = os.path.join(sharedDir, "parent.jsonl")
    childRealPath = os.path.join(sharedDir, "child.jsonl")
    with open(parentRealPath, "w", encoding="utf-8") as file:
        file.write("parent\n")
    with open(childRealPath, "w", encoding="utf-8") as file:
        file.write("child\n")

    return {
        "baseDir": baseDir,
        "parentAliasA": os.path.join(aliasASessions, "parent.jsonl"),
        "parentAliasB": os.path.join(aliasBSessions, "parent.jsonl"),
        "childAliasB": os.path.join(aliasBSessions, "child.jsonl"),
    }


CTRL_D = "\x04"
CTRL_BACKSPACE = "\x1b[127;5u"

keybindings = KeybindingsManager()


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    # session selector uses the global theme instance
    initTheme("dark")


@pytest.fixture(autouse=True)
def isolatedKeybindings():
    # Ensure test isolation: keybindings are a global singleton
    setKeybindings(KeybindingsManager())


@pytest.fixture
def tempDirs():
    dirs: list[str] = []
    yield dirs
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)


def noop(*_args: Any) -> None:
    return None


def loader(sessions: list[SessionInfo]) -> Any:
    async def load(*_args: Any) -> list[SessionInfo]:
        return sessions

    return load


async def test_does_not_treat_ctrl_backspace_as_delete_when_search_query_is_non_empty():
    sessions = [makeSession("a"), makeSession("b")]

    selector = SessionSelectorComponent(loader(sessions), loader([]), noop, noop, noop, noop, {"keybindings": keybindings})
    await flushPromises()

    sessionList = selector.getSessionList()
    confirmationChanges: list[str | None] = []
    sessionList.onDeleteConfirmationChange = confirmationChanges.append

    sessionList.handleInput("a")
    sessionList.handleInput(CTRL_BACKSPACE)

    assert confirmationChanges == []


async def test_enters_confirmation_mode_on_ctrl_d_even_with_a_non_empty_search_query():
    sessions = [makeSession("a"), makeSession("b")]

    selector = SessionSelectorComponent(loader(sessions), loader([]), noop, noop, noop, noop, {"keybindings": keybindings})
    await flushPromises()

    sessionList = selector.getSessionList()
    confirmationChanges: list[str | None] = []
    sessionList.onDeleteConfirmationChange = confirmationChanges.append

    sessionList.handleInput("a")
    sessionList.handleInput(CTRL_D)

    assert confirmationChanges == [sessions[0].path]


async def test_enters_confirmation_mode_on_ctrl_backspace_when_search_query_is_empty():
    sessions = [makeSession("a"), makeSession("b")]

    selector = SessionSelectorComponent(loader(sessions), loader([]), noop, noop, noop, noop, {"keybindings": keybindings})
    await flushPromises()

    sessionList = selector.getSessionList()
    confirmationChanges: list[str | None] = []
    sessionList.onDeleteConfirmationChange = confirmationChanges.append

    deletedPath: str | None = None

    async def onDeleteSession(sessionPath: str) -> None:
        nonlocal deletedPath
        deletedPath = sessionPath

    sessionList.onDeleteSession = onDeleteSession

    sessionList.handleInput(CTRL_BACKSPACE)
    assert confirmationChanges == [sessions[0].path]

    sessionList.handleInput("\r")
    assert confirmationChanges == [sessions[0].path, None]
    assert deletedPath == sessions[0].path


async def test_does_not_switch_scope_back_to_all_when_all_load_resolves_after_toggling_back_to_current():
    currentSessions = [makeSession("current")]
    allDeferred: asyncio.Future[list[SessionInfo]] = asyncio.get_running_loop().create_future()
    allLoadCalls = 0

    async def loadAll(*_args: Any) -> list[SessionInfo]:
        nonlocal allLoadCalls
        allLoadCalls += 1
        return await allDeferred

    selector = SessionSelectorComponent(
        loader(currentSessions), loadAll, noop, noop, noop, noop, {"keybindings": keybindings}
    )
    await flushPromises()

    sessionList = selector.getSessionList()
    sessionList.handleInput("\t")  # current -> all (starts async load)
    sessionList.handleInput("\t")  # all -> current

    allDeferred.set_result([makeSession("all")])
    await flushPromises()

    assert allLoadCalls == 1
    output = "\n".join(selector.render(120))
    assert "Resume Session (Current Folder)" in output
    assert "Resume Session (All)" not in output


async def test_does_not_start_redundant_all_loads_when_toggling_scopes_while_all_is_already_loading():
    currentSessions = [makeSession("current")]
    allSessions = [makeSession("all")]
    allDeferred: asyncio.Future[list[SessionInfo]] = asyncio.get_running_loop().create_future()
    allLoadCalls = 0

    async def loadAll(onProgress: Any = None, *_args: Any) -> list[SessionInfo]:
        nonlocal allLoadCalls
        allLoadCalls += 1
        if onProgress is not None:
            onProgress(1, 2, allSessions)
        return await allDeferred

    selector = SessionSelectorComponent(
        loader(currentSessions), loadAll, noop, noop, noop, noop, {"keybindings": keybindings}
    )
    await flushPromises()

    sessionList = selector.getSessionList()
    sessionList.handleInput("\t")  # current -> all (starts async load)
    sessionList.handleInput("\t")  # all -> current
    sessionList.handleInput("\t")  # current -> all again while load pending

    assert allLoadCalls == 1
    assert selector.getSessionList().getSelectedSessionPath() == allSessions[0].path
    assert "Loading" in "\n".join(selector.render(120))

    allDeferred.set_result(allSessions)
    await flushPromises()


async def test_threads_sessions_when_parent_and_child_paths_use_different_symlink_aliases(tempDirs):
    paths = createSymlinkedSessionPaths()
    tempDirs.append(paths["baseDir"])

    sessions = [
        makeSession("parent", path=paths["parentAliasB"], name="Parent", modified=date("2026-01-01T00:00:00.000Z")),
        makeSession(
            "child",
            path=paths["childAliasB"],
            parentSessionPath=paths["parentAliasA"],
            name="Child",
            modified=date("2025-12-31T00:00:00.000Z"),
        ),
    ]

    selector = SessionSelectorComponent(loader(sessions), loader([]), noop, noop, noop, noop, {"keybindings": keybindings})
    await flushPromises()

    output = stripAnsi("\n".join(selector.render(120)))
    assert "Parent" in output
    assert "└─ Child" in output


async def test_sorts_threaded_sessions_by_latest_activity_in_their_subtree():
    parentOne = makeSession("parent-one", name="Parent one", modified=date("2026-01-02T00:00:00.000Z"))
    parentTwo = makeSession("parent-two", name="Parent two", modified=date("2026-01-01T00:00:00.000Z"))
    childTwo = makeSession(
        "child-two", name="Child two", parentSessionPath=parentTwo.path, modified=date("2026-01-03T00:00:00.000Z")
    )

    selector = SessionSelectorComponent(
        loader([parentOne, parentTwo, childTwo]), loader([]), noop, noop, noop, noop, {"keybindings": keybindings}
    )
    await flushPromises()

    output = stripAnsi("\n".join(selector.render(120)))
    parentTwoIndex = output.find("Parent two")
    childTwoIndex = output.find("└─ Child two")
    parentOneIndex = output.find("Parent one")

    assert parentTwoIndex >= 0
    assert childTwoIndex > parentTwoIndex
    assert parentOneIndex > childTwoIndex


async def test_treats_the_current_session_as_active_across_symlink_aliases(tempDirs):
    paths = createSymlinkedSessionPaths()
    tempDirs.append(paths["baseDir"])

    sessions = [makeSession("parent", path=paths["parentAliasB"], name="Parent")]
    selector = SessionSelectorComponent(
        loader(sessions), loader([]), noop, noop, noop, noop, {"keybindings": keybindings}, paths["parentAliasA"]
    )
    await flushPromises()

    sessionList = selector.getSessionList()
    confirmationChanges: list[str | None] = []
    errorMessage: str | None = None
    sessionList.onDeleteConfirmationChange = confirmationChanges.append

    def onError(message: str) -> None:
        nonlocal errorMessage
        errorMessage = message

    sessionList.onError = onError

    sessionList.handleInput(CTRL_D)

    assert confirmationChanges == []
    assert errorMessage == "Cannot delete the currently active session"
