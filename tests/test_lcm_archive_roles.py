"""In the archive a row says who spoke; the model is sent what it was always sent.

``convertToLlm`` makes every custom message a user turn. Upstream reads a user row as the user
asking, and of the custom messages in the real sessions on this machine about 2,700 were notices
and mail against 1,041 work orders: a to-do reminder became a summary's focus.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.moments import SENDERS, TURN
from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host import ingest, repair, storage, tools
from misaka.extensions.misaka_lcm.vendor import engine as upstream


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def custom(kind, text, **details):
    return {"role": "custom", "customType": kind, "content": text, "display": True, "details": details,
            "timestamp": 1789460410000}


def role_of(message):
    return ingest.upstream_messages([message])[0]["role"]


def test_each_kind_of_message_says_who_spoke():
    assert role_of({"role": "user", "content": "hello", "timestamp": 1789460410000}) == "user"
    assert role_of(custom("research-phase", "plan the node", **{TURN: True})) == "user"
    assert role_of(custom("todo-reminder", "[To-do reminder] use a list")) == "notice"
    assert role_of(custom("agent-messages", "<agent-messages/>", **{SENDERS: ["10032"]})) == "agent:10032"
    assert role_of(custom("agent-messages", "<agent-messages/>", **{SENDERS: ["10032", "last-order"]})) \
        == "agent:10032,last-order"


def test_mail_from_before_senders_were_recorded_is_a_notice():
    assert role_of(custom("agent-messages", "<agent-messages/>", count=1)) == "notice"


@pytest.fixture
def session(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    ctx = storage.context(SimpleNamespace(cwd=str(project), sessionManager=manager, model=None), str(project), "test")
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count = 2
    built._config.leaf_chunk_tokens = 40
    seen = []

    def summarize(**kwargs):
        seen.append(kwargs)
        return "Summary. Expand for details about: the ledger", 1

    monkeypatch.setattr(upstream, "summarize_with_escalation", summarize)
    for i in range(8):
        manager.appendMessage({"role": "user", "content": f"read the ledger {i} " + "page " * 40,
                               "timestamp": 1789460410000 + i})
        manager.appendCustomMessageEntry("todo-reminder", "[To-do reminder] use a list " + "x " * 30, True, {})
        manager.appendCustomMessageEntry("agent-messages", f"<agent-messages>seal {i}</agent-messages>", True,
                                         {SENDERS: ["10032"]})
    return ctx, seen


def compact(ctx):
    messages = ctx.sessionManager.buildSessionContext().messages
    return ce.compact(ce.prepare({"reason": "manual", "messages": messages}, ctx))


def test_a_summary_s_focus_is_what_the_user_asked_and_its_text_says_who_spoke(session):
    ctx, seen = session
    assert compact(ctx) is not None
    assert "read the ledger" in seen[0]["focus_topic"]
    assert "To-do reminder" not in seen[0]["focus_topic"]
    assert "[NOTICE]: " in seen[0]["text"]
    assert "[AGENT:10032]: " in seen[0]["text"]


def test_the_model_is_sent_the_original_messages(session):
    ctx, _ = session
    result = compact(ctx)
    wire = json.dumps([getattr(m, "role", None) or m.get("role") for m in result.contextMessages])
    assert "notice" not in wire and "agent" not in wire
    assert any((getattr(m, "role", None) or m.get("role")) == "custom" for m in result.contextMessages)


def test_a_row_with_an_archive_role_and_no_source_cannot_reach_the_model():
    replay = ingest.Replay([{"role": "user", "content": "hello", "timestamp": 1789460410000}])
    with pytest.raises(ValueError, match="Unsupported LCM replay role"):
        replay.restore([{"role": "notice", "content": "nobody's row"}])


def test_a_changed_notice_goes_back_as_the_message_it_was():
    notice = custom("todo-reminder", "[To-do reminder] use a list")
    replay = ingest.Replay([notice])
    row = {**replay.messages[0], "content": "[To-do reminder] redacted"}
    restored = replay.restore([row])[0]
    assert restored["role"] == "custom" and restored["customType"] == "todo-reminder"


def test_grep_finds_one_agent_s_mail(session):
    ctx, _ = session
    ce.sync(ctx)
    snapshot = ce.snapshot(ctx)

    def hits(**args):
        answer = tools._answer("lcm_grep", {"query": "seal", "session_scope": "all", **args}, snapshot, ctx)
        return json.loads(answer[answer.index("{"):answer.rindex("}") + 1])["results"]

    assert hits(role="agent:10032")
    assert not hits(role="agent:10033")
    assert not hits(role="user")


def test_a_research_node_s_last_order_has_an_address_of_her_own():
    from misaka.core.wiring import SessionSpec
    from misaka.extensions.misaka_lcm import identity

    spec = SessionSpec(profile_dir="/p", role="last_order", workspace="/w", kind="foreground", sender="last-order",
                       node="b_1")
    assert identity(spec) == "last-order@b_1"


def checkpoint(frame, text, **fields):
    return {"type": "compaction", "contextMessages": [{"role": "user", "content": text},
                                                      {"role": "user", "content": "a real question"}],
            "details": {"lcm": {"nodes": [{"frame": frame}], "scaffolds": []}}, **fields}


def test_repair_names_the_message_that_holds_the_recorded_frame():
    frame = "[Recent Summary (d0, node 7)]\nSummary."
    assert repair.named(checkpoint(frame, "<<<fence>>>\n" + frame + "\n<<<end>>>")) == [0]
    escaped = "[Recent Summary (d0, node 7)]\n<<<UNTRUSTED-DATA>>> quoted"
    assert repair.named(checkpoint(escaped, escaped.replace("UNTRUSTED-DATA", "UNTRUSTED-DATA-ESCAPED"))) == [0]


def test_repair_leaves_what_it_cannot_match_exactly():
    frame = "[Recent Summary (d0, node 7)]\nSummary."
    assert repair.named(checkpoint(frame, "[Recent Summary (d0, node 7)]\nSomething else.")) is None
    assert repair.named(checkpoint(frame, frame, contextSummaries=[0])) is None
    partly = checkpoint(frame, frame)
    partly["details"]["lcm"]["nodes"].append({"frame": "[Session Arc Summary (d1, node 8)]\nNot in the context."})
    assert repair.named(partly) is None


def test_repair_changes_one_field_keeps_a_copy_and_reports_first(tmp_path):
    frame = "[Recent Summary (d0, node 7)]\nSummary."
    root = tmp_path / "sessions"
    root.mkdir()
    file = root / "a.jsonl"
    other = b'{"type":"message","note":"untouched  spacing"}\n'
    entry = checkpoint(frame, frame)
    original = b'{"type":"session","id":"s"}\n' + other + json.dumps(entry).encode() + b"\n"
    file.write_bytes(original)
    said = []
    repair.run(root=root, say=said.append)
    assert file.read_bytes() == original and "Would name" in said[0]
    said.clear()
    repair.run(apply=True, root=root, say=said.append)
    lines = file.read_bytes().splitlines(keepends=True)
    assert lines[1] == other
    written = json.loads(lines[2])
    assert written["contextSummaries"] == [0] and written["details"]["lcm"]["scaffolds"] == [0]
    assert {key: value for key, value in written.items() if key != "contextSummaries"}["contextMessages"] \
        == entry["contextMessages"]
    assert Path(said[-1].split(": ", 1)[1], "a.jsonl").read_bytes() == original
