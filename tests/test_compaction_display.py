"""An engine's compaction summary is shown as a summary, never remembered as typed input.

2026-09-18 (B30): the LCM engine writes a complete context onto the compaction entry, with
its summaries as user turns because that is where the model expects them. The transcript
rendered those turns as the user's own and, with ``populateHistory`` on every reopen,
replayed them into the editor's input history: ten summary blocks of 12-33k characters,
tripled. pi shows a compaction as a compaction summary; so does misaka now, while the
model's view is untouched."""
from types import SimpleNamespace

from misaka.core import session_manager
from misaka.core.platform.prompt_guard import untrusted
from misaka.ui.tui.interactive.interactive_mode import InteractiveMode

SUMMARY = "[Session Arc Summary (d1, node 42)]\nT0 checked 130 titles.\n[Expand for details: node 42]"


def _entry(context_messages, scaffolds, **extra):
    return {"type": "compaction", "id": "c1", "summary": "LCM compressed", "tokensBefore": 120000,
            "firstKeptEntryId": "m9", "timestamp": 1700000000000,
            "contextMessages": context_messages, "details": {"lcm": {"scaffolds": scaffolds}}, **extra}


KEPT = {"role": "assistant", "content": [{"type": "text", "text": "kept turn"}], "stopReason": "stop",
        "timestamp": 1700000000001, "api": "anthropic-messages", "provider": "test", "model": "m",
        "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
                  "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0}}}


def test_the_engines_summary_turn_is_shown_as_a_compaction_summary():
    entry = _entry([{"role": "user", "content": SUMMARY, "timestamp": 1}, KEPT], scaffolds=[0])
    shown = session_manager.session_entry_to_display_messages(entry)
    assert [session_manager._message_role(m) for m in shown] == ["compactionSummary", "assistant"]
    assert shown[0].summary == SUMMARY and shown[0].tokensBefore == 120000


def test_a_fenced_summary_is_shown_without_its_fence():
    fenced = untrusted("lcm:compaction:s1", SUMMARY)
    entry = _entry([{"role": "user", "content": fenced, "timestamp": 1}], scaffolds=[0])
    shown = session_manager.session_entry_to_display_messages(entry)
    assert shown[0].summary == SUMMARY


def test_the_models_view_is_unchanged():
    entry = _entry([{"role": "user", "content": SUMMARY, "timestamp": 1}, KEPT], scaffolds=[0])
    for_model = session_manager.session_entry_to_context_messages(entry)
    assert session_manager._message_role(for_model[0]) == "user"
    assert session_manager._content_text(for_model[0]) == SUMMARY


def test_a_user_turn_the_engine_kept_is_still_a_user_turn():
    """Only the engine's own scaffolds are re-labelled; a real question it kept stays one."""
    question = {"role": "user", "content": "which edition?", "timestamp": 2}
    entry = _entry([{"role": "user", "content": SUMMARY, "timestamp": 1}, question], scaffolds=[0])
    shown = session_manager.session_entry_to_display_messages(entry)
    assert session_manager._message_role(shown[1]) == "user"


def test_a_compaction_without_an_engine_view_shows_pis_own_summary():
    entry = {"type": "compaction", "id": "c1", "summary": "pi summary", "tokensBefore": 10,
             "firstKeptEntryId": "m9", "timestamp": 1700000000000}
    shown = session_manager.session_entry_to_display_messages(entry)
    assert [session_manager._message_role(m) for m in shown] == ["compactionSummary"]
    assert shown[0].summary == "pi summary"


def test_an_ordinary_entry_is_shown_as_is():
    entry = {"type": "message", "id": "m1", "message": {"role": "user", "content": "hello", "timestamp": 3}}
    assert session_manager.session_entry_to_display_messages(entry) == \
        session_manager.session_entry_to_context_messages(entry)


def test_a_compaction_summary_never_enters_the_editor_history():
    remembered = []
    tui = SimpleNamespace(editor=SimpleNamespace(addToHistory=remembered.append))
    entry = _entry([{"role": "user", "content": SUMMARY, "timestamp": 1}, KEPT], scaffolds=[0])
    for message in session_manager.session_entry_to_display_messages(entry):
        InteractiveMode._rememberUserMessage(tui, message, {"populateHistory": True})
    assert remembered == []
    typed = {"role": "user", "content": "a real question", "timestamp": 4}
    InteractiveMode._rememberUserMessage(tui, typed, {"populateHistory": True})
    assert remembered == ["a real question"]
