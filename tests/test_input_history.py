"""The persisted input history holds what was typed, once, whichever window typed it.

It used to be refilled from the session transcript on every open, resume and reload (pi's
in-memory behaviour), so a role's history file filled up with the same few prompts over and
over -- Up cycled through them for ever -- and took in user-role messages nobody typed. And
windows sharing one role's file overwrote each other's entries with their own stale copy.
"""
import json
from types import SimpleNamespace

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import getEditorTheme, getMarkdownTheme
from misaka.ui.tui import Container
from misaka.ui.tui.components.editor import Editor


def _editor(monkeypatch, path):
    monkeypatch.setenv("MISAKA_INPUT_HISTORY", str(path))
    return Editor(SimpleNamespace(requestRender=lambda *a: None), getEditorTheme())


def _load_session(editor, prompts):
    host = SimpleNamespace(editor=editor, chatContainer=Container(), outputPad=1, toolOutputExpanded=False,
                           getMarkdownThemeWithSettings=getMarkdownTheme, getMarkdownTransformers=list)
    host.getUserMessageText = lambda message: InteractiveMode.getUserMessageText(host, message)
    for text in prompts:
        InteractiveMode.addMessageToChat(host, {"role": "user", "content": text, "timestamp": 1},
                                         {"populateHistory": True})


def test_reopening_a_session_does_not_refill_a_persisted_history(tmp_path, monkeypatch):
    path = tmp_path / "last-order.json"
    editor = _editor(monkeypatch, path)
    for text in ["开始", "继续"]:
        editor.addToHistory(text)
    for _ in range(3):                       # three opens / resumes / reloads of the same session
        _load_session(_editor(monkeypatch, path), ["开始", "继续", "<<<UNTRUSTED-DATA replay>>>"])
    assert json.loads(path.read_text(encoding="utf-8")) == ["继续", "开始"]


def test_without_a_history_file_the_session_still_fills_the_in_memory_history(monkeypatch):
    monkeypatch.delenv("MISAKA_INPUT_HISTORY", raising=False)
    editor = Editor(SimpleNamespace(requestRender=lambda *a: None), getEditorTheme())
    _load_session(editor, ["first", "second"])
    assert editor.history == ["second", "first"]


def test_windows_sharing_a_history_file_keep_each_others_entries(tmp_path, monkeypatch):
    path = tmp_path / "last-order.json"
    root = _editor(monkeypatch, path)
    node = _editor(monkeypatch, path)        # opened before the root typed anything
    root.addToHistory("from the root window")
    node.addToHistory("from a fork node window")
    root.addToHistory("the root again")
    assert json.loads(path.read_text(encoding="utf-8")) == ["the root again", "from a fork node window", "from the root window"]


def test_up_walks_the_history_once_and_stops_at_the_oldest(tmp_path, monkeypatch):
    path = tmp_path / "last-order.json"
    editor = _editor(monkeypatch, path)
    for text in ["one", "two", "three"]:
        editor.addToHistory(text)
    seen = []
    for _ in range(5):
        editor.navigateHistory(-1)
        seen.append(editor.getText())
    assert seen == ["three", "two", "one", "one", "one"]
