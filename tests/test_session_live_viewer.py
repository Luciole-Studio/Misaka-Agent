"""The real Sessions follow loop renders owner snapshots without driving an agent."""

import asyncio
from types import SimpleNamespace

import pytest

from misaka.cli.chat import follow_session
from misaka.core import session_catalog, session_control
from misaka.core.settings_manager import SettingsManager
from misaka.ui.tui import Container
from misaka.ui.tui.interactive.components.assistant_message import (
    AssistantMessageComponent,
)
from misaka.ui.tui.interactive.conversation import Conversation
from misaka.utils.ansi import strip_ansi


class ViewerUI(Container):
    """No terminal, render thread, input source, socket, or session owner."""

    def __init__(self):
        super().__init__()
        self.terminal = SimpleNamespace(rows=40, columns=120)
        self.listener = None
        self.started = self.stopped = False

    def requestRender(self, *_args):
        pass

    def setFocus(self, component):
        component.focused = True

    def addInputListener(self, listener):
        self.listener = listener

        def unsubscribe():
            self.listener = None

        return unsubscribe

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def viewer(monkeypatch, tmp_path):
    ui = ViewerUI()
    screen = Conversation(ui, SettingsManager.inMemory({"hideThinkingBlock": False}), str(tmp_path))
    owner = {"role": "LO", "state": "running", "control": str(tmp_path / "owner.sock")}
    monkeypatch.setattr(session_catalog, "owner_record", lambda _path: owner)

    async def next_poll(awaitable, *, timeout):
        # Replace only polling delay; follow_session still owns and checks its real close event.
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", next_poll)
    return screen, tmp_path / "session.jsonl"


def message(text, thinking=""):
    content = ([{"type": "thinking", "thinking": thinking}] if thinking else [])
    content.append({"type": "text", "text": text})
    return {"role": "assistant", "content": content, "timestamp": 0}


def entry(identifier, value):
    return {"type": "message", "id": identifier, "parentId": None, "message": value}


def snapshot(cursor, stream_cursor, entries=None, streaming=None, workflow=None):
    return {
        "cursor": cursor, "entries": entries,
        "stream_cursor": stream_cursor, "streaming": streaming,
        "workflow": workflow, "state": "running", "paused": False,
        "cwd": "/fixture", "steering": [], "follow_up": [], "error": "",
    }


def assistants(screen):
    return [child for child in screen.chatContainer.children if isinstance(child, AssistantMessageComponent)]


def rendered(screen):
    return strip_ansi("\n".join(screen.chatContainer.render(120)))


async def test_follow_live_thinking_text_commit_and_sanitize(viewer, monkeypatch):
    screen, path = viewer
    history = [entry("u", {"role": "user", "content": "question", "timestamp": 0})]
    final = message("FINAL_ANSWER", "VISIBLE_THOUGHT expanded")
    workflow = {
        "run_id": "run", "node": "root", "depth": 0,
        "run_phase": "final_report", "run_status": "running", "node_phase": "completed",
    }
    calls, live = [], []

    async def request(_owner, operation, **arguments):
        calls.append((operation, arguments))
        assert operation == "snapshot"  # Watching must not send input, resume, or create an owner.
        poll = len(calls)
        if poll == 1:
            assert arguments.get("stream_cursor") is None
            return snapshot("history-1", "stream-1", history,
                            message("FIRST_TEXT", "\x1b[31mVISIBLE_THOUGHT\x1b[0m\x00"), workflow)
        if poll == 2:
            assert arguments == {"cursor": "history-1", "stream_cursor": "stream-1"}
            assert screen.entries == history
            live.extend(assistants(screen))
            assert len(live) == 1 and live[0].isStreaming
            assert live[0].lastMessage["content"][0]["thinking"] == "VISIBLE_THOUGHT"
            assert "VISIBLE_THOUGHT" in rendered(screen) and "FIRST_TEXT" in rendered(screen)
            assert "final_report" in screen.headerContainer.children[0].text
            assert "completed" in screen.headerContainer.children[0].text
            return snapshot("history-1", "stream-2", streaming=final, workflow=workflow)
        if poll == 3:
            assert assistants(screen) == live
            assert "FINAL_ANSWER" in rendered(screen) and "expanded" in rendered(screen)
            assert screen.entries == history
            return snapshot("history-1", "stream-2", workflow=workflow)
        if poll == 4:
            # An unchanged stream cursor with a null payload means "no update", not "clear".
            assert assistants(screen) == live
            return snapshot("history-2", "stream-3", [*history, entry("a", final)], workflow=workflow)
        assert poll == 5
        committed = assistants(screen)
        assert len(committed) == 1 and committed[0] is not live[0]
        assert not committed[0].isStreaming
        assert rendered(screen).count("FINAL_ANSWER") == 1
        assert screen._liveComponent is None
        screen.ui.listener("\x03")
        return snapshot("history-2", "stream-3", workflow=workflow)

    monkeypatch.setattr(session_control, "request", request)
    assert await follow_session(screen, path, attach=True) == 0
    assert len(calls) == 5 and screen.ui.started and screen.ui.stopped
    assert screen.ui.listener is None and screen._liveComponent is None


async def test_follow_disconnect_clears_live_and_reconnects_without_replaying_input(viewer, monkeypatch):
    screen, path = viewer
    calls = []

    async def request(_owner, operation, **arguments):
        calls.append((operation, arguments))
        assert operation == "snapshot"
        poll = len(calls)
        if poll == 1:
            return snapshot("same-history", "same-stream", [], message("IN_PROGRESS"))
        if poll == 2:
            assert "IN_PROGRESS" in rendered(screen)
            # Unsent editor text remains local across disconnects.
            screen.editor.setText("LOCAL_DRAFT")
            raise OSError("fixture disconnect")
        if poll == 3:
            assert arguments["stream_cursor"] is None
            assert screen._liveComponent is None and not assistants(screen)
            assert screen.editor.getText() == "LOCAL_DRAFT"
            return snapshot("same-history", "same-stream", streaming=message("RECONNECTED"))
        assert poll == 4
        assert arguments["stream_cursor"] == "same-stream"
        assert "RECONNECTED" in rendered(screen)
        assert screen.editor.getText() == "LOCAL_DRAFT"
        screen.ui.listener("\x04")
        return snapshot("same-history", "same-stream")

    monkeypatch.setattr(session_control, "request", request)
    assert await follow_session(screen, path, attach=True) == 0
    assert len(calls) == 4
    assert screen._liveComponent is None and not assistants(screen)
    assert screen.ui.stopped and screen.ui.listener is None
